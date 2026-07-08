"""去重模块：按 url/title 哈希生成稳定 id，按年分桶存储已见集合。

黑盒阶段：collect 每 2 小时运行，同一素材跨次/跨天不重复入库。
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEDUP_DIR = ROOT / "data" / "dedup"


def make_id(title: str, url: str = "") -> str:
    """素材唯一 id = sha256(url|title)，归一化大小写与首尾空格。"""
    return hashlib.sha256(f"{url}|{title}".strip().lower().encode("utf-8")).hexdigest()[:16]


def _file(year: str) -> Path:
    DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    return DEDUP_DIR / f"seen_{year}.json"


def load_seen(year: str) -> set:
    f = _file(year)
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8")).get("ids", []))
        except Exception:
            return set()
    return set()


def save_seen(year: str, seen: set) -> None:
    _file(year).write_text(
        json.dumps({"ids": sorted(seen)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class Dedup:
    """按年维护去重集合，collect 用。"""

    def __init__(self, year: str):
        self.year = year
        self.seen = load_seen(year)

    def is_new(self, title: str, url: str = "") -> bool:
        return make_id(title, url) not in self.seen

    def add(self, title: str, url: str = "") -> None:
        self.seen.add(make_id(title, url))

    def flush(self) -> None:
        save_seen(self.year, self.seen)
