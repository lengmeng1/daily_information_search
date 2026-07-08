"""构建索引：扫描 docs/ 生成 site/index.json 树形结构（年→月→日）。

网页只拉这个 index.json 渲染目录；点开某天再按需 fetch 对应 md。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_ROOT = ROOT / "docs"
SITE = ROOT / "site"


def build():
    tree = {}
    total = 0
    if DOC_ROOT.exists():
        for yp in sorted(DOC_ROOT.iterdir()):
            if not yp.is_dir():
                continue
            year = yp.name
            tree[year] = {}
            for mp in sorted(yp.iterdir()):
                if not mp.is_dir():
                    continue
                month = mp.name
                days = []
                for dp in sorted(mp.glob("*.md")):
                    date_str = dp.stem  # YYYY-MM-DD
                    day = date_str.split("-")[-1]
                    count = sum(
                        1 for l in dp.read_text(encoding="utf-8").splitlines()
                        if l.strip().startswith("- ")
                    )
                    days.append({"day": day, "file": f"docs/{year}/{month}/{dp.name}", "count": count})
                    total += 1
                if days:
                    tree[year][month] = days
    index = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "tree": tree,
    }
    SITE.mkdir(exist_ok=True)
    (SITE / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[build_index] 写入 site/index.json，共 {total} 篇")


if __name__ == "__main__":
    build()
