"""前回の通知以降に見つかったセール記事を、LINE に1通にまとめて送る。

GitHub Actions で毎朝1回実行する想定。必要な環境変数:
  LINE_CHANNEL_ACCESS_TOKEN … LINE Developers の「チャネルアクセストークン（長期）」
  LINE_USER_ID              … LINE Developers の「あなたのユーザーID」（U から始まる文字列）
  PAGE_URL（任意）           … 一覧ページのURL。メッセージの最後に付けます
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)
MAX_NEW = 10      # リンク付きで載せる新着の数（多すぎると読みにくいので）
MAX_CHARS = 4800  # LINE のテキストは1通5000文字まで
WEEK = "月火水木金土日"


def md(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({WEEK[d.weekday()]})"


def build_message(state: dict) -> str:
    items = list(state["items"].values())
    since = state.get("last_notified") or (NOW - timedelta(hours=24)).isoformat()
    today = NOW.date().isoformat()
    week_later = (NOW.date() + timedelta(days=7)).isoformat()

    new = sorted([i for i in items if i.get("first_seen", "") > since],
                 key=lambda i: (not i["announce"], i["published"]), reverse=False)
    soon = sorted([i for i in items if any(today <= d <= week_later for d in i["dates"])],
                  key=lambda i: min(d for d in i["dates"] if d >= today))

    lines = [f"✈️ 旅行セールのお知らせ {NOW.month}/{NOW.day}"]

    lines.append(f"\n■ 新着 {len(new)}件")
    if not new:
        lines.append("新しい記事はありませんでした")
    for i in new[:MAX_NEW]:
        mark = "★" if i["announce"] else "・"
        d = f"（{md(min(i['dates']))}〜）" if i["dates"] else ""
        lines.append(f"{mark}{i['title']}{d}\n{i['link']}")
    if len(new) > MAX_NEW:
        lines.append(f"…ほか{len(new) - MAX_NEW}件は一覧ページで")

    if soon:
        lines.append("\n■ 1週間以内に始まるセール")
        for i in soon[:10]:
            first = min(d for d in i["dates"] if d >= today)
            lines.append(f"{md(first)} {i['title']}")

    page = os.getenv("PAGE_URL")
    if page:
        lines.append(f"\n📋 一覧ページ\n{page}")

    msg = "\n".join(lines)
    if len(msg) > MAX_CHARS:
        msg = msg[:MAX_CHARS] + "\n…（続きは一覧ページで）"
    return msg


def main() -> int:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user = os.getenv("LINE_USER_ID")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    msg = build_message(state)
    print(msg)

    if not token or not user:
        print("\nLINE の設定（Secrets）が無いため、送信せず表示のみにしました。")
        return 0

    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": user, "messages": [{"type": "text", "text": msg}]},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"\nLINE 送信に失敗: {r.status_code} {r.text}")
        return 1  # Actions で赤✖になるので気づける

    state["last_notified"] = NOW.isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nLINE に送信しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
