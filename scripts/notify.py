"""企微通知：发布完成后向群机器人 Webhook 推送一条文本消息。

Webhook 来自 GitHub Secrets（WECHAT_WEBHOOK），不写进代码。
"""
import json
import sys
import urllib.error
import urllib.request


def notify(webhook: str, text: str):
    payload = {"msgtype": "text", "text": {"content": text}}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[notify] 状态码 {r.status}")
    except urllib.error.URLError as e:
        print(f"[notify] 发送失败: {e}")


if __name__ == "__main__":
    wh = None
    txt = ""
    if "--webhook" in sys.argv:
        wh = sys.argv[sys.argv.index("--webhook") + 1]
    if "--text" in sys.argv:
        txt = sys.argv[sys.argv.index("--text") + 1]
    if wh:
        notify(wh, txt)
    else:
        print("[notify] 未提供 webhook，跳过")
