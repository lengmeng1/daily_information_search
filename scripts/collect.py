"""真实搜索采集内核：读取 config/topics.json，对每个搜集类型按配置的数据源做真实网络搜索，
去重后追加到 data/raw/YYYY-MM-DD.jsonl。输出字段与黑盒桩一致，下游 summarize.py 无需改动。

数据源（sources，除 tavily 外均无需 key 即可用）：
  - google_news : Google News RSS 搜索（实时新闻，中文友好；链接为中转页，无正文，AI 仅读标题）
  - arxiv       : arXiv 论文 API（前沿研究 / 学习类）
  - hn          : Hacker News（Algolia API，返回真实文章直链，比 RSS 稳定）
  - duckduckgo  : DuckDuckGo HTML 搜索（通用网页，作为兜底）
  - tavily      : Tavily Search API（需环境变量 TAVILY_API_KEY，质量最高，可选）
  - rss         : 直链 RSS 源（如 量子位/36氪/机器之心），返回真实文章 URL + 导语正文，
                 需在 topic 下配 "feeds":[{"url":..., "name":...}]，AI 可读取真实正文做摘要

依赖：仅 Python 标准库（urllib / xml / gzip / html / re），CI 无需 pip install。
"""
import gzip
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dedup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "topics.json"
RAW_DIR = ROOT / "data" / "raw"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PER_SOURCE_MAX = 6        # 每个数据源每次最多取多少条
PER_TOPIC_RUN_CAP = 14    # 每个类型单次运行最多入库多少条（控制每日增长）
REQUEST_TIMEOUT = 20
SOURCE_PAUSE = 1.0         # 不同数据源之间稍作停顿，避免被限流


def fetch(url, data=None, headers=None, timeout=REQUEST_TIMEOUT):
    """简单 GET/POST，自动解 gzip，失败返回 None（不抛异常）。"""
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[fetch] 失败 {url[:80]}: {e}")
        return None


# ---------- XML 解析小工具（忽略命名空间） ----------
def _local(tag):
    return tag.split("}")[-1]


def _find_children(elem, name):
    return [c for c in elem if _local(c.tag) == name]


def _text(elem, name):
    for c in _find_children(elem, name):
        return (c.text or "").strip()
    return ""


def _attr(elem, name, attr):
    for c in _find_children(elem, name):
        return c.get(attr)
    return None


def _iter_items(text, tag):
    """从 RSS/Atom 文本里取出所有 item/entry 元素。"""
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except Exception as e:
        print(f"[parse] XML 解析失败: {e}")
        return []
    return [c for c in root.iter() if _local(c.tag) == tag]


def _clean_summary(s, limit=220):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()[:limit]


def _strip_html(s):
    """去掉 HTML 标签，保留纯文本（用于 RSS description）。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc or "web"
    except Exception:
        return "web"


# ---------- 各数据源实现 ----------
def fetch_google_news(query, max_items=PER_SOURCE_MAX, api_key=""):
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-CN"
    text = fetch(url)
    out = []
    for it in _iter_items(text, "item"):
        title = _text(it, "title")
        if not title:
            continue
        publisher = _text(it, "source")
        link = _text(it, "link")
        # Google News 标题形如 "标题 - 媒体"，剥离媒体名更干净
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -(len(publisher) + 3)].strip()
        out.append({
            "title": title,
            "url": link,
            "source": publisher or "Google News",
            "summary": "",
        })
        if len(out) >= max_items:
            break
    return out


def fetch_arxiv(query, max_items=PER_SOURCE_MAX, api_key=""):
    q = urllib.parse.quote(query)
    url = (f"https://export.arxiv.org/api/query?search_query=all:{q}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={max_items}")
    text = fetch(url)
    out = []
    for e in _iter_items(text, "entry"):
        title = re.sub(r"\s+", " ", _text(e, "title") or "").strip()
        link = _text(e, "id") or _attr(e, "id", "href") or ""
        summary = _clean_summary(_text(e, "summary"), 260)
        if title:
            out.append({"title": title, "url": link, "source": "arXiv", "summary": summary})
    return out


def fetch_hn(query, max_items=PER_SOURCE_MAX, api_key=""):
    """Hacker News：用 Algolia 公开 API（返回真实文章直链，比 hnrss 稳定）。"""
    q = urllib.parse.quote(query)
    url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage={max_items}"
    text = fetch(url)
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for h in data.get("hits", []):
        title = (h.get("title") or "").strip()
        if not title:
            continue
        u = h.get("url") or h.get("story_url") or ""
        out.append({
            "title": title,
            "url": u,
            "source": "Hacker News",
            "summary": _clean_summary(h.get("story_text") or "", 160),
        })
    return out


class _DDGParser(HTMLParser):
    """解析 DuckDuckGo HTML 结果页，收集标题/链接/摘要。"""

    def __init__(self):
        super().__init__()
        self.titles, self.snips, self.hrefs = [], [], []
        self._mode = None
        self._buf = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._mode, self._buf, self._href = "title", [], attrs.get("href", "")
        elif (tag in ("a", "td")) and "result__snippet" in cls:
            self._mode, self._buf = "snip", []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a":
            self.titles.append(html.unescape("".join(self._buf)).strip())
            self.hrefs.append(self._href)
            self._mode = None
        elif self._mode == "snip" and tag in ("a", "td"):
            self.snips.append(html.unescape("".join(self._buf)).strip())
            self._mode = None

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


def fetch_duckduckgo(query, max_items=PER_SOURCE_MAX, api_key=""):
    q = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={q}"
    text = fetch(url)
    if not text:
        return []
    p = _DDGParser()
    try:
        p.feed(text)
    except Exception as e:
        print(f"[ddg] 解析失败: {e}")
        return []
    out = []
    for i, title in enumerate(p.titles[:max_items]):
        href = p.hrefs[i] if i < len(p.hrefs) else ""
        real = ""
        if href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                real = urllib.parse.unquote(m.group(1))
        snip = p.snips[i] if i < len(p.snips) else ""
        out.append({
            "title": title,
            "url": real or ("https:" + href if href.startswith("//") else href),
            "source": _domain(real or href),
            "summary": _clean_summary(snip, 200),
        })
    return out


def fetch_tavily(query, max_items=PER_SOURCE_MAX, api_key=""):
    if not api_key:
        return []
    url = "https://api.tavily.com/search"
    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_items,
        "search_depth": "basic",
    }).encode("utf-8")
    text = fetch(url, data=payload, headers={"Content-Type": "application/json"})
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for r in data.get("results", []):
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": "Tavily",
            "summary": _clean_summary(r.get("content", ""), 260),
        })
    return out


def fetch_rss_feed(feed_url, max_items=PER_SOURCE_MAX, feed_name="", api_key=""):
    """抓取直链 RSS 源（如 量子位 / 36氪 / 机器之心），返回真实文章 URL + 导语正文。
    用正则容错解析（部分中文 feed 的 XML 不规范），失败时返回空列表。
    """
    text = fetch(feed_url)
    if not text:
        return []
    blocks = re.findall(r"<item\b[^>]*>(.*?)</item>", text, flags=re.S | re.I)
    out = []
    for b in blocks:
        if len(out) >= max_items:
            break

        def grab(tag):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", b, flags=re.S | re.I)
            if not m:
                return ""
            s = m.group(1).strip()
            # 去除 CDATA 包裹
            cm = re.search(r"<!\[CDATA\[(.*?)\]\]>", s, flags=re.S)
            if cm:
                s = cm.group(1).strip()
            return s

        title = _strip_html(grab("title"))
        link = grab("link")
        if not link:
            lm = re.search(r'<link[^>]*href="([^"]+)"', b, re.I)
            link = lm.group(1) if lm else ""
        desc = _strip_html(grab("description"))
        src = feed_name or grab("source") or _domain(feed_url)
        if not title or not link:
            continue
        out.append({"title": title, "url": link, "source": src, "summary": desc[:300]})
    return out


SOURCE_FUNCS = {
    "google_news": fetch_google_news,
    "arxiv": fetch_arxiv,
    "hn": fetch_hn,
    "duckduckgo": fetch_duckduckgo,
    "tavily": fetch_tavily,
}
DEFAULT_SOURCES = ["google_news", "arxiv", "hn"]


def load_topics():
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("topics", [])


def search_topic(topic, api_key=""):
    """对一个搜集类型执行所有数据源×查询，返回去重后的素材列表。"""
    queries = topic.get("queries") or topic.get("keywords") or []
    sources = topic.get("sources") or DEFAULT_SOURCES
    seen_urls, collected = set(), []
    cap = PER_TOPIC_RUN_CAP

    def accept(it):
        t, u = it.get("title", ""), it.get("url", "")
        if not t:
            return None
        # 同一次运行内按 url/title 去重，避免多源重复
        if u in seen_urls or any(t == c["title"] for c in collected):
            return None
        if u:
            seen_urls.add(u)
        rec = {
            "title": t,
            "url": u,
            "source": it.get("source", ""),
            "summary": it.get("summary", ""),
        }
        collected.append(rec)
        return rec

    for src in sources:
        if len(collected) >= cap:
            break
        if src == "rss":
            # RSS 直链源：按 topic 的 feeds 列表抓取（每个 feed 给真实 URL + 导语）
            for feed in (topic.get("feeds") or []):
                if len(collected) >= cap:
                    break
                furl = feed.get("url") if isinstance(feed, dict) else feed
                fname = feed.get("name") if isinstance(feed, dict) else ""
                try:
                    items = fetch_rss_feed(furl, PER_SOURCE_MAX, fname, api_key)
                except Exception as e:
                    print(f"[collect] rss {furl} 出错: {e}")
                    items = []
                for it in items:
                    accept(it)
            time.sleep(SOURCE_PAUSE)
            continue
        fn = SOURCE_FUNCS.get(src)
        if not fn:
            print(f"[collect] 未知数据源 {src}，跳过")
            continue
        for q in queries:
            if len(collected) >= cap:
                break
            try:
                items = fn(q, PER_SOURCE_MAX, api_key)
            except Exception as e:
                print(f"[collect] {src} 搜索 '{q}' 出错: {e}")
                items = []
            for it in items:
                accept(it)
        time.sleep(SOURCE_PAUSE)
    return collected


def collect(date_str: str, api_key: str = "") -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_file = RAW_DIR / f"{date_str}.jsonl"
    year = date_str[:4]
    dd = dedup.Dedup(year)
    added = 0
    with raw_file.open("a", encoding="utf-8") as f:
        for t in load_topics():
            key = t.get("key")
            for it in search_topic(t, api_key):
                if dd.is_new(it["title"], it["url"]):
                    rec = {
                        "id": dedup.make_id(it["title"], it["url"]),
                        "topic": key,
                        "title": it["title"],
                        "url": it["url"],
                        "source": it["source"],
                        "summary": it.get("summary", ""),
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    dd.add(it["title"], it["url"])
                    added += 1
    dd.flush()
    print(f"[collect] {date_str}: +{added} 条真实素材（去重集 {len(dd.seen)}）")
    return added


if __name__ == "__main__":
    date_arg, api_key = None, ""
    if "--date" in sys.argv:
        date_arg = sys.argv[sys.argv.index("--date") + 1]
    if "--api-key" in sys.argv:
        api_key = sys.argv[sys.argv.index("--api-key") + 1]
    if not date_arg:
        date_arg = datetime.now().strftime("%Y-%m-%d")
    collect(date_arg, api_key)
