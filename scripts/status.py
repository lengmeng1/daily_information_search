"""维护状态：写 site/status.json。

publish 工作流在部署前设为 maintenance（网页显示维护中遮罩），部署完成后再设为 open。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def set_status(state: str, message: str = ""):
    SITE.mkdir(exist_ok=True)
    data = {
        "status": state,  # "open" | "maintenance"
        "message": message,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    (SITE / "status.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[status] -> {state}")


if __name__ == "__main__":
    state = "open"
    msg = ""
    if "--set" in sys.argv:
        state = sys.argv[sys.argv.index("--set") + 1]
    if "--message" in sys.argv:
        msg = sys.argv[sys.argv.index("--message") + 1]
    set_status(state, msg)
