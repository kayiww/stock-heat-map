from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def default_site_url() -> str:
    explicit = os.getenv("SITE_URL") or os.getenv("PAGES_URL")
    if explicit:
        return explicit.rstrip("/")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repository:
        owner, repo = repository.split("/", 1)
        return f"https://{owner}.github.io/{repo}".rstrip("/")
    return ""


def build_message(latest: dict[str, Any], site_url: str) -> str:
    lines = [
        "A股重点板块热度日报",
        f"数据日期：{latest.get('data_time') or '-'}",
        f"状态：{latest.get('status') or '-'}",
    ]
    if latest.get("status") == "failed":
        lines.append("今日更新失败，当前展示上一交易日数据。")

    rankings = latest.get("rankings") or {}
    for key, title in [("strong", "强势"), ("overheat", "过热"), ("low", "低位"), ("rotation", "高低切换")]:
        items = rankings.get(key) or []
        if items:
            compact = "；".join(f"{item['board_name']} {pct(item.get('equal_weight_change_pct'))}" for item in items[:3])
            lines.append(f"{title}：{compact}")

    errors = latest.get("error_flags") or []
    if errors:
        lines.append("异常：" + "；".join(str(error) for error in errors[:5]))
    if site_url:
        lines.append(f"网页：{site_url}")
    lines.append("仅用于复盘观察，不提供买入或卖出建议。")
    return "\n".join(lines)


def send_message(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Telegram summary")
    parser.add_argument("--latest", default="public/data/latest.json")
    parser.add_argument("--site-url", default="")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets are not configured; skipping.")
        return 0

    latest = json.loads(Path(args.latest).read_text(encoding="utf-8"))
    site_url = (args.site_url or default_site_url()).rstrip("/")
    send_message(token, chat_id, build_message(latest, site_url))
    print("Telegram summary sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
