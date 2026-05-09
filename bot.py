#!/usr/bin/env python3
"""
AI Briefing Bot — Telegram callback query listener.
Long-polls getUpdates and dispatches echo:{section} button presses.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import otel
from opentelemetry import trace

log = logging.getLogger(__name__)

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

tracer: trace.Tracer | None = None


def handle_callback_query(token, chat_id, redis_host, cq):
    cq_id = cq["id"]
    data = cq.get("data", "")
    user = cq.get("from", {})
    username = user.get("username") or user.get("first_name", "unknown")

    action = data.split(":")[0] if ":" in data else data
    subject = data.split(":", 1)[1] if ":" in data else ""

    with tracer.start_as_current_span("bot.callback_query") as span:
        span.set_attribute("callback.action", action)
        span.set_attribute("callback.subject", subject)
        span.set_attribute("user.name", username)

        # Must answer within 10s or Telegram shows a permanent loading spinner
        answer_callback_query(token, cq_id)

        if data.startswith("echo:"):
            section_key = subject
            if section_key not in VALID_SECTIONS:
                send_message(token, chat_id, f"⚠️ Unknown section: <code>{section_key}</code>")
                return
            section_text, count = get_section_from_redis(section_key, redis_host)
            if section_text is None:
                send_message(token, chat_id,
                    f"⏳ <b>Section expired</b>\nThe <code>{section_key}</code> section "
                    f"is no longer in Redis (sections expire after 24h).")
                log.info("echo section=%s user=%s status=expired", section_key, username)
                return
            log.info("echo section=%s user=%s items=%d", section_key, username, count)
            span.set_attribute("section.items", count)
            send_message(token, chat_id, section_text)

        elif data.startswith("item:"):
            item_id = subject
            item = get_item_from_redis(item_id, redis_host)
            if item is None:
                send_message(token, chat_id,
                    f"⏳ <b>Item expired</b>\nThis item is no longer in Redis (items expire after 24h).")
                log.info("item id=%s user=%s status=expired", item_id, username)
                return
            log.info("item id=%s section=%s index=%s user=%s",
                     item_id, item.get("section"), item.get("index"), username)
            span.set_attribute("item.section", item.get("section", ""))
            span.set_attribute("item.index", item.get("index", 0))
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
            item_id = subject
            item = get_item_from_redis(item_id, redis_host)
            if item is None:
                send_message(token, chat_id,
                    "⏳ <b>Item expired</b>\nThis item is no longer in Redis (items expire after 24h).")
                log.info("save id=%s user=%s status=expired", item_id, username)
                return

            try:
                with tracer.start_as_current_span("bot.save_note") as save_span:
                    tags     = infer_tags(item)
                    raw      = item["handle"] if item.get("section") == "people" else item.get("title", item_id)
                    slug     = slugify(raw)
                    category = SECTION_CATEGORIES.get(item.get("section", "inform"), "reading")
                    filepath = f"{VAULT_BASE}/{category}/{slug}.md"
                    note     = render_note(item, tags)
                    save_span.set_attribute("note.category", category)
                    save_span.set_attribute("note.slug", slug)
                    save_span.set_attribute("obsidian.file_path", filepath)
                    save_span.set_attribute("obsidian.note_length", len(note))
                    save_span.set_attribute("pipeline.stage", "save")

                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    with open(filepath, "w") as f:
                        f.write(note)

                log.info("save id=%s file=research/%s/%s.md user=%s", item_id, category, slug, username)
                span.set_attribute("note.path", f"research/{category}/{slug}.md")
                send_message(token, chat_id,
                    f"💾 Saved: <code>research/{category}/{slug}.md</code>")
            except Exception as e:
                log.error("save failed id=%s error=%s", item_id, e)
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, str(e))
                send_message(token, chat_id,
                    f"❌ <b>Save failed</b>\n<code>{e}</code>")

# ── Main polling loop ──────────────────────────────────────────────────────
def main():
    global tracer
    tracer = otel.setup("ai-briefing-bot")
    env = load_env()
    token = env.get("TELEGRAM_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    redis_host = env.get("REDIS_HOST", "localhost")

    if not token or not chat_id:
        log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    log.info("Starting — long-polling for callback queries")

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
            log.warning("Poll error: %s — retrying in 5s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
