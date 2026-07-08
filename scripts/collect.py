"""黑盒采集桩：生成样例素材，按 url/title 哈希去重后追加到当日 raw 文件。

真实阶段：把 SAMPLES 替换为搜索 API（如第三方搜索 + LLM），接口保持不变：
  - 读取 config/topics.json 拿到要搜集的类型
  - 产出 {id, topic, title, url, source, collected_at} 的一条条素材
  - 交给 dedup 过滤已见内容，再追加进 data/raw/YYYY-MM-DD.jsonl
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dedup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "topics.json"
RAW_DIR = ROOT / "data" / "raw"

# 黑盒样例素材库（真实阶段替换为搜索 API 返回）
SAMPLES = {
    "ai-news": [
        ("OpenAI 发布轻量版推理模型，长上下文成本下降 40%", "https://example.com/ai-news-1"),
        ("开源社区推出可本地运行的 7B 多模态模型", "https://example.com/ai-news-2"),
    ],
    "ai-learn": [
        ("通俗理解 RAG：把检索与生成串起来，让回答有据可依", "https://example.com/ai-learn-1"),
        ("LoRA 微调入门：只训练少量参数即可适配特定风格", "https://example.com/ai-learn-2"),
    ],
    "ai-exp": [
        ("写提示词先给角色+任务+格式+约束，命中率更高", "https://example.com/ai-exp-1"),
        ("把长文档拆块再总结，比一次性丢进去更稳", "https://example.com/ai-exp-2"),
    ],
}


def load_topics():
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("topics", [])


def collect(date_str: str) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_file = RAW_DIR / f"{date_str}.jsonl"
    dd = dedup.Dedup(date_str[:4])
    added = 0
    with raw_file.open("a", encoding="utf-8") as f:
        for t in load_topics():
            key = t.get("key")
            for title, url in SAMPLES.get(key, []):
                if dd.is_new(title, url):
                    item = {
                        "id": dedup.make_id(title, url),
                        "topic": key,
                        "title": title,
                        "url": url,
                        "source": t.get("name", key),
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    }
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    dd.add(title, url)
                    added += 1
    dd.flush()
    print(f"[collect] {date_str}: +{added} 条新素材（去重集 {len(dd.seen)}）")
    return added


if __name__ == "__main__":
    date_arg = None
    if "--date" in sys.argv:
        date_arg = sys.argv[sys.argv.index("--date") + 1]
    if not date_arg:
        date_arg = datetime.now().strftime("%Y-%m-%d")
    collect(date_arg)
