"""总结渲染：读取当日素材（优先 enrich 后的富化数据），按类型分块，
生成 docs/年/月/年-月-日.md。

- 优先读 data/enriched/YYYY-MM-DD.jsonl（含 AI 摘要 + 广告标记）
- 渲染每条的 AI 摘要（ai_summary），无则退回搜索 snippet
- 跳过 is_promo=True 的推广/广告项
- 链接优先用 real_url（已解析的真实文章地址）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
ENRICH_DIR = ROOT / "data" / "enriched"
DOC_ROOT = ROOT / "docs"
CONFIG = ROOT / "config" / "topics.json"


def load_topics():
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("topics", [])


def load_items(date_str):
    enr = ENRICH_DIR / f"{date_str}.jsonl"
    raw = RAW_DIR / f"{date_str}.jsonl"
    f = enr if enr.exists() else raw
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def summarize(date_str: str) -> bool:
    items = load_items(date_str)
    if not items:
        print(f"[summarize] 无素材 {date_str}，跳过")
        return False

    # 过滤推广项
    kept = [it for it in items if not it.get("is_promo")]
    n_promo = len(items) - len(kept)

    by_topic = {}
    for it in kept:
        by_topic.setdefault(it.get("topic", "other"), []).append(it)

    topics = load_topics()
    year, month, _ = date_str.split("-")
    out = DOC_ROOT / year / month
    out.mkdir(parents=True, exist_ok=True)
    doc = out / f"{date_str}.md"

    lines = [
        f"# {date_str} AI 每日总结",
        "",
        f"> 自动化生成 · 共 {len(kept)} 条素材（AI 已读正文并摘要）",
    ]
    if n_promo:
        lines.append(f"> 已自动过滤 {n_promo} 条推广/广告")
    lines.append("")

    for t in topics:
        key = t.get("key")
        its = by_topic.get(key, [])
        if not its:
            continue
        lines.append(f"## {t.get('name', key)}")
        for it in its:
            title = it.get("title", "")
            url = it.get("real_url") or it.get("url", "")
            src = it.get("source", "")
            summary = it.get("ai_summary") or it.get("summary", "")
            if url:
                line = f"- [{title}]({url})"
            else:
                line = f"- {title}"
            if src:
                line += f"  _{src}_"
            lines.append(line)
            if summary:
                lines.append(f"  > {summary}")
        lines.append("")

    doc.write_text("\n".join(lines), encoding="utf-8")
    print(f"[summarize] 生成 {doc}（保留 {len(kept)} 条，过滤 {n_promo} 条）")
    return True


if __name__ == "__main__":
    date_arg = None
    if "--date" in sys.argv:
        date_arg = sys.argv[sys.argv.index("--date") + 1]
    if not date_arg:
        from datetime import datetime, timedelta
        date_arg = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    summarize(date_arg)
