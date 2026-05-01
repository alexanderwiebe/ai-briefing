#!/usr/bin/env python3
"""
AI Briefing Bot — Telegram callback query listener.
Long-polls getUpdates and dispatches echo:{section} button presses.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# ── Obsidian vault ─────────────────────────────────────────────────────────
VAULT_BASE = "/home/alexander/vaults/Bitovi/research"

SECTION_CATEGORIES = {
    "act_now": "urgent",
    "queue":   "queue",
    "inform":  "reading",
    "people":  "people",
}

# ── Env loading (mirrors briefing.py) ─────────────────────────────────────
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    env["TELEGRAM_TOKEN"] = os.environ.get("TELEGRAM_TOKEN", env.get("TELEGRAM_TOKEN", ""))
    env["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", env.get("TELEGRAM_CHAT_ID", ""))
    return env

# ── Telegram helpers ───────────────────────────────────────────────────────
def telegram_post(token, method, payload):
    url = TELEGRAM_API.format(token=token, method=method)
    req = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=35) as resp:
        return json.loads(resp.read())


def get_updates(token, offset, timeout=30):
    result = telegram_post(token, "getUpdates", {
        "offset": offset,
        "timeout": timeout,
        "allowed_updates": ["callback_query"],
    })
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result}")
    return result.get("result", [])


def answer_callback_query(token, callback_query_id):
    telegram_post(token, "answerCallbackQuery", {"callback_query_id": callback_query_id})


def send_message(token, chat_id, text):
    telegram_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

# ── Redis ──────────────────────────────────────────────────────────────────
def get_redis(redis_host):
    import redis as _redis
    return _redis.Redis(host=redis_host, port=6379, decode_responses=True)


def get_section_from_redis(section_key, redis_host):
    """Returns (text, count) or (None, None) if key is expired or Redis unavailable."""
    try:
        raw = get_redis(redis_host).get(f"section:{section_key}")
        if raw is None:
            return None, None
        data = json.loads(raw)
        return data["text"], data["count"]
    except Exception:
        return None, None


def get_item_from_redis(item_id, redis_host):
    """Returns item dict or None if expired/unavailable."""
    try:
        raw = get_redis(redis_host).get(f"item:{item_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ── Obsidian helpers ───────────────────────────────────────────────────────
def slugify(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    return slug[:60]


def infer_tags(item: dict) -> list:
    if item.get("section") == "people":
        prompt = (
            "Generate 3-5 kebab-case tags for this AI/tech person to follow. "
            f"Return only a JSON array.\n\nHandle: {item['handle']}\nReason: {item['reason']}"
        )
    else:
        prompt = (
            "Generate 3-5 kebab-case tags for this AI/tech news item. "
            f"Return only a JSON array.\n\nTitle: {item['title']}\nSummary: {item['summary']}"
        )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(result.stdout.strip())
    except Exception:
        return []


def render_note(item: dict, tags: list) -> str:
    now      = datetime.now(timezone.utc)
    saved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date     = now.strftime("%Y-%m-%d")
    tag_str  = json.dumps(tags)
    section  = item.get("section", "inform")

    if section == "people":
        title = item["handle"]
        extra = ""
        body  = f"## Why Follow\n{item['reason']}\n\n## Notes\n<!-- Add your own notes here -->\n"
    else:
        title = item.get("title", "")
        extra = (
            f'source: "{item.get("source", "")}"\n'
            f'url: {item.get("url", "")}\n'
        )
        body = (
            f"{item.get('summary', '')}\n\n"
            f"## Source\n"
            f"- **Posted by:** {item.get('source', '')}\n"
            f"- **URL:** {item.get('url', '')}\n"
            f"- **Section:** {section}\n\n"
            f"## Notes\n<!-- Add your own notes here -->\n\n"
            f"## Related\n<!-- Backlinks added automatically by connection agent -->\n"
        )

    return (
        f"---\n"
        f"title: {title}\n"
        f"date: {date}\n"
        f"saved_at: {saved_at}\n"
        f"section: {section}\n"
        f"tags: {tag_str}\n"
        f"{extra}"
        f"status: saved\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{body}"
    )


# ── Callback handler ───────────────────────────────────────────────────────
VALID_SECTIONS = {"act_now", "queue", "inform", "people"}


def handle_callback_query(token, chat_id, redis_host, cq):
    cq_id = cq["id"]
    data = cq.get("data", "")
    user = cq.get("from", {})
    username = user.get("username") or user.get("first_name", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    # Must answer within 10s or Telegram shows a permanent loading spinner
    answer_callback_query(token, cq_id)

    if data.startswith("echo:"):
        section_key = data[len("echo:"):]
        if section_key not in VALID_SECTIONS:
            send_message(token, chat_id, f"⚠️ Unknown section: <code>{section_key}</code>")
            return
        section_text, count = get_section_from_redis(section_key, redis_host)
        if section_text is None:
            send_message(token, chat_id,
                f"⏳ <b>Section expired</b>\nThe <code>{section_key}</code> section "
                f"is no longer in Redis (sections expire after 24h).")
            print(f"[ECHO] Section {section_key!r} requested by {username!r} — key expired")
            return
        sep = "─" * 41
        print(f"[ECHO] {sep}")
        print(f"[ECHO] Section:  {section_key}")
        print(f"[ECHO] User:     {username}")
        print(f"[ECHO] Time:     {timestamp}")
        print(f"[ECHO] Items:    {count}")
        print(f"[ECHO] {sep}")
        send_message(token, chat_id, section_text)

    elif data.startswith("item:"):
        item_id = data[len("item:"):]
        item = get_item_from_redis(item_id, redis_host)
        if item is None:
            send_message(token, chat_id,
                f"⏳ <b>Item expired</b>\nThis item is no longer in Redis (items expire after 24h).")
            print(f"[ITEM] {item_id!r} requested by {username!r} — key expired")
            return
        sep = "─" * 41
        print(f"[ITEM] {sep}")
        print(f"[ITEM] ID:       {item_id}")
        print(f"[ITEM] Section:  {item.get('section')}")
        print(f"[ITEM] Index:    {item.get('index')}")
        print(f"[ITEM] User:     {username}")
        print(f"[ITEM] Time:     {timestamp}")
        print(f"[ITEM] {sep}")
        # Format item detail card
        section = item.get("section", "")
        if section == "people":
            text = f"👤 <b>{item['handle']}</b>\n{item['reason']}"
        else:
            text = f"<b>{item['title']}</b>\n{item['summary']}"
            if item.get("url"):
                text += f'\n<a href="{item["url"]}">↗ Read more</a>'
            if item.get("source"):
                text += f"\n<i>{item['source']}</i>"
        send_message(token, chat_id, text)

    elif data.startswith("save:"):
        item_id = data[len("save:"):]
        item = get_item_from_redis(item_id, redis_host)
        if item is None:
            send_message(token, chat_id,
                "⏳ <b>Item expired</b>\nThis item is no longer in Redis (items expire after 24h).")
            print(f"[SAVE] {item_id!r} requested by {username!r} — key expired")
            return

        try:
            tags     = infer_tags(item)
            raw      = item["handle"] if item.get("section") == "people" else item.get("title", item_id)
            slug     = slugify(raw)
            category = SECTION_CATEGORIES.get(item.get("section", "inform"), "reading")
            filepath = f"{VAULT_BASE}/{category}/{slug}.md"

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w") as f:
                f.write(render_note(item, tags))

            sep = "─" * 41
            print(f"[SAVE] {sep}")
            print(f"[SAVE] ID:       {item_id}")
            print(f"[SAVE] File:     research/{category}/{slug}.md")
            print(f"[SAVE] User:     {username}")
            print(f"[SAVE] Time:     {timestamp}")
            print(f"[SAVE] {sep}")
            send_message(token, chat_id,
                f"💾 Saved: <code>research/{category}/{slug}.md</code>")
        except Exception as e:
            print(f"[SAVE] ERROR for {item_id!r}: {e}", file=sys.stderr)
            send_message(token, chat_id,
                f"❌ <b>Save failed</b>\n<code>{e}</code>")

# ── Main polling loop ──────────────────────────────────────────────────────
def main():
    env = load_env()
    token = env.get("TELEGRAM_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    redis_host = env.get("REDIS_HOST", "localhost")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env", file=sys.stderr)
        sys.exit(1)

    print("[bot] Starting — long-polling for callback queries")

    offset = 0
    while True:
        try:
            updates = get_updates(token, offset, timeout=30)
            for update in updates:
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if cq:
                    handle_callback_query(token, chat_id, redis_host, cq)
        except Exception as e:
            print(f"[bot] Error: {e} — retrying in 5s", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
