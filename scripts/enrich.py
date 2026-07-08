"""富化环节：对当日 raw 素材「抓正文 + AI 摘要 + 广告过滤」。

输入：data/raw/YYYY-MM-DD.jsonl （collect.py 产出，含标题/链接/来源/搜索snippet）
输出：data/enriched/YYYY-MM-DD.jsonl （在原始字段上追加 real_url / ai_summary / is_promo / enriched）
缓存：data/enriched/cache.json （按标题缓存，避免重复抓网页、重复调模型）

设计要点：
- 有 LLM_API_KEY：调用模型产出「1-2 句中文摘要 + 是否推广(is_promo)」
- 无 Key：降级为「关键词广告过滤 + 抽取正文前 200 字粗预览」，网页照常工作
- 正文抽取用 trafilatura；arXiv 直接复用摘要，不抓网页；GitHub/代码类链接跳过抽取
- 每条只抓一次、只调一次模型（缓存），控制 GitHub Actions 时长与 API 成本
- Google News 中转链接先解析 302 重定向拿到真实文章 URL 再抓
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
ENRICH_DIR = ROOT / "data" / "enriched"
CACHE_FILE = ENRICH_DIR / "cache.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_TIMEOUT = 15
ENRICH_RUN_CAP = 60          # 单次运行最多新富化多少条（日新增通常≤50，一次基本富化完）
LLM_TIMEOUT = 30
TEXT_MAX = 4000              # 送模型的正文上限（字符）
PREVIEW = 200                # 无模型时粗预览字数

# 广告/推广关键词：STRONG 出现在任何位置即判推广；WEAK 仅标题出现才判推广（降误杀）
STRONG = ["卖课", "加盟", "代理", "领取资料", "限时免费", "扫码", "加微信", "私域",
          "引流", "免费领", "0元", "赚钱", "副业", "变现", "招生", "训练营",
          "知识付费", "付费社群", "会员招募", "扫码进群", "添加客服", "微信咨询",
          "长按识别", "福利领取"]
WEAK = ["培训", "课程", "优惠", "折扣", "促销", "下单", "购买", "咨询",
        "公开课", "套课", "海报"]

# 可选依赖：trafilatura 用于网页正文抽取
try:
    import trafilatura
    HAVE_TRAF = True
except Exception:
    HAVE_TRAF = False


# ---------- 网络 ----------
def resolve_redirect(url):
    """跟随重定向拿到真实文章 URL（Google News 中转页用）。"""
    if not url:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return r.geturl()
    except Exception:
        return url


def extract_text(url):
    """用 trafilatura 抽取干净正文；失败返回 None。"""
    if not HAVE_TRAF or not url:
        return None
    try:
        txt = trafilatura.fetch_url(url, timeout=REQUEST_TIMEOUT)
        if txt:
            return re.sub(r"\s+", " ", txt).strip()
    except Exception:
        return None
    return None


# ---------- 广告判定 ----------
def keyword_promo(title, source, text):
    blob = f"{title} {source} {(text or '')[:400]}"
    if any(k in blob for k in STRONG):
        return True
    if any(k in (title or "") for k in WEAK):
        return True
    return False


# ---------- LLM 摘要（OpenAI 兼容，默认 DeepSeek） ----------
def llm_summarize(title, source, text, api_key, base_url, model):
    base = (base_url or "https://api.deepseek.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    sys_p = (
        "你是中文资讯助理。阅读网页正文，只输出一个 JSON 对象："
        "{\"summary\": \"1-2句中文摘要，说清文章主旨与价值\", "
        "\"is_promo\": false}。"
        "is_promo 为 true 表示这是卖课/培训/加盟/引流加微信/限时免费领资料等推广广告内容。"
        "不要输出多余文字或解释。"
    )
    usr = f"标题：{title}\n来源：{source}\n正文：\n{(text or '')[:TEXT_MAX]}"
    payload = json.dumps({
        "model": model or "deepseek-chat",
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": usr},
        ],
        "temperature": 0.2,
        "max_tokens": 220,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[enrich] LLM 调用失败: {e}")
        return None, False
    return parse_llm(content)


def parse_llm(content):
    c = (content or "").strip()
    if c.startswith("```"):
        c = re.sub(r"^```[a-zA-Z]*\n?", "", c)
        c = re.sub(r"\n?```$", "", c).strip()
    try:
        obj = json.loads(c)
        return (obj.get("summary") or "").strip(), bool(obj.get("is_promo", False))
    except Exception:
        # 退化：整段当摘要
        return c[:PREVIEW], False


# ---------- 单条富化 ----------
def enrich_one(it, api_key, base_url, model, cache):
    title = it.get("title", "")
    url = it.get("url", "")
    source = it.get("source", "")
    snippet = it.get("summary", "")

    # 缓存命中：直接复用
    if title in cache:
        c = cache[title]
        return {**it, "real_url": c.get("real_url", url),
                "ai_summary": c.get("ai_summary", ""),
                "is_promo": c.get("is_promo", False), "enriched": True}

    # arXiv：直接复用摘要，不抓网页
    if "arxiv.org" in (url or ""):
        res = {"real_url": url, "ai_summary": snippet or "", "is_promo": False}
        cache[title] = res
        return {**it, **res, "enriched": True}

    is_gnews = "news.google.com" in (url or "")
    # Google News 当前格式无法可靠解码真实来源，保留中转页链接；
    # 其余直链源才尝试解析重定向拿到最终地址。
    real = url if is_gnews else (resolve_redirect(url) if url else url)

    # 抓取正文：
    #  - Google News 是 JS 渲染页，trafilatura 抽不到正文，且 RSS 无导语 -> AI 只读标题；
    #  - 直链源（量子位/36氪/HN 等）尝试 trafilatura 抓全文，失败则退回 RSS 导语。
    if is_gnews:
        text = snippet  # Google News RSS 导语通常为空
    else:
        text = extract_text(real) if real else None
        if not text:
            text = snippet

    # AI 阅读素材：有正文用正文，没有则退到标题
    read_text = text or title

    # 有模型走 LLM，否则关键词降级
    if api_key:
        ai_summary, is_promo = llm_summarize(title, source, read_text, api_key, base_url, model)
        if not ai_summary:
            ai_summary = read_text[:PREVIEW]
    else:
        is_promo = keyword_promo(title, source, read_text)
        ai_summary = read_text[:PREVIEW]

    res = {"real_url": real, "ai_summary": (ai_summary or "").strip(),
           "is_promo": bool(is_promo)}
    cache[title] = res
    return {**it, **res, "enriched": True}


# ---------- 当日批量 ----------
def enrich(date_str, api_key="", base_url="", model=""):
    ENRICH_DIR.mkdir(parents=True, exist_ok=True)
    raw_file = RAW_DIR / f"{date_str}.jsonl"
    if not raw_file.exists():
        print(f"[enrich] 无素材 {date_str}，跳过")
        return 0
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    items = [json.loads(l) for l in raw_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    out_file = ENRICH_DIR / f"{date_str}.jsonl"
    new_count = 0
    results = []
    for it in items:
        title = it.get("title", "")
        if title in cache:
            results.append(enrich_one(it, api_key, base_url, model, cache))
        elif new_count < ENRICH_RUN_CAP:
            results.append(enrich_one(it, api_key, base_url, model, cache))
            new_count += 1
        else:
            # 超出本次上限，先原样写出，留待下次运行富化
            results.append({**it, "real_url": it.get("url", ""),
                            "ai_summary": "", "is_promo": False, "enriched": False})

    with out_file.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    n_promo = sum(1 for r in results if r.get("is_promo"))
    print(f"[enrich] {date_str}: 新富化 {new_count} 条，其中推广 {n_promo} 条；累计缓存 {len(cache)}")
    return new_count


if __name__ == "__main__":
    date_arg = api_key = base_url = model = None
    if "--date" in sys.argv:
        date_arg = sys.argv[sys.argv.index("--date") + 1]
    if "--api-key" in sys.argv:
        api_key = sys.argv[sys.argv.index("--api-key") + 1]
    if "--base-url" in sys.argv:
        base_url = sys.argv[sys.argv.index("--base-url") + 1]
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    if not date_arg:
        date_arg = datetime.now().strftime("%Y-%m-%d")
    # 环境变量优先于默认值，CLI 可覆盖
    api_key = api_key or os.environ.get("LLM_API_KEY", "")
    base_url = base_url or os.environ.get("LLM_BASE_URL", "")
    model = model or os.environ.get("LLM_MODEL", "")
    enrich(date_arg, api_key, base_url, model)
