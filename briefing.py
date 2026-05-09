#!/usr/bin/env python3
"""
AI Daily Briefing
Fetches RSS feeds, preprocesses posts, classifies via Claude CLI,
delivers via Telegram.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError

import otel
from opentelemetry import trace

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
ENV_FILE = BASE_DIR / ".env"
FOLLOWING_FILE = Path.home() / ".config" / "ai-briefing" / "following.txt"

# ── Config ─────────────────────────────────────────────────────────────────
FEEDS = {
    "AI Builders & Practitioners": [
        "http://192.168.1.50:1200/twitter/list/2030847297706160376",
        "http://192.168.1.50:1200/twitter/list/2030848232775905785",
        "http://192.168.1.50:1200/twitter/list/2026501401497514453",
    ],
    "AI Products & Tools": [
        "http://192.168.1.50:1200/twitter/list/2026507090412400682",
        "http://192.168.1.50:1200/twitter/list/2026507364262617204",
    ],
    "AI Research & Thinking": [
        "http://192.168.1.50:1200/twitter/list/2030849075961090388",
        "http://192.168.1.50:1200/twitter/list/2030846285218009307",
    ],
    "General AI Pulse": [
        "http://192.168.1.50:1200/twitter/list/2026505126639267970",
        "http://192.168.1.50:1200/twitter/list/2026507364262617204",
    ],
    "AI Shill": [
        "http://192.168.1.50:1200/twitter/list/2041987069321322689",
    ],
}

# Flat list of all feed URLs (deduplicated)
ALL_FEEDS = list(dict.fromkeys(url for urls in FEEDS.values() for url in urls))

# ── Env / secrets ──────────────────────────────────────────────────────────
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    # Allow overrides from real environment
    env["TELEGRAM_TOKEN"] = os.environ.get("TELEGRAM_TOKEN", env.get("TELEGRAM_TOKEN", ""))
    env["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", env.get("TELEGRAM_CHAT_ID", ""))
    return env

# ── State (last run tracking) ──────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def get_window_start(state):
    """Return the datetime we should collect posts from."""
    last = state.get("last_run")
    if last:
        dt = datetime.fromisoformat(last)
        # Never look back more than 12h to avoid flooding
        cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        return max(dt, cutoff)
    # First run: last 8 hours
    return datetime.now(timezone.utc) - timedelta(hours=8)

# ── Feed fetching ──────────────────────────────────────────────────────────
def strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    for ent, rep in [('&amp;','&'),('&lt;','<'),('&gt;','>'),('&quot;','"'),('&#39;',"'"),('&nbsp;',' ')]:
        text = text.replace(ent, rep)
    return re.sub(r'\s+', ' ', text).strip()

def fetch_feed(url):
    try:
        req = Request(url, headers={'User-Agent': 'AI-Briefing/1.0'})
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        channel = root.find('channel')
        if channel is None:
            return url, []
        items = []
        for item in channel.findall('item'):
            pub_el = item.find('pubDate')
            if pub_el is None or not pub_el.text:
                continue
            try:
                dt = parsedate_to_datetime(pub_el.text)
            except Exception:
                continue
            author_el = item.find('author')
            title_el = item.find('title')
            desc_el = item.find('description')
            link_el = item.find('link')
            guid_el = item.find('guid')
            items.append({
                'guid': (guid_el.text if guid_el is not None else link_el.text if link_el is not None else ''),
                'author': author_el.text.strip() if author_el is not None and author_el.text else 'unknown',
                'title': strip_html(title_el.text if title_el is not None else ''),
                'desc': strip_html(desc_el.text if desc_el is not None else ''),
                'link': link_el.text.strip() if link_el is not None and link_el.text else '',
                'published': dt,
            })
        return url, items
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return url, []

def fetch_all_feeds(since: datetime):
    """Fetch all feeds in parallel, deduplicate, and filter by time window."""
    all_items = {}  # guid → item
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, url): url for url in ALL_FEEDS}
        for future in as_completed(futures):
            _, items = future.result()
            for item in items:
                if item['published'] >= since and item['guid'] not in all_items:
                    all_items[item['guid']] = item

    # Sort chronologically
    return sorted(all_items.values(), key=lambda x: x['published'])

# ── Following list ────────────────────────────────────────────────────────
def load_following():
    """Load set of already-followed handles (lowercased, without @)."""
    if not FOLLOWING_FILE.exists():
        return set()
    handles = set()
    for line in FOLLOWING_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        handles.add(line.lstrip("@").lower())
    return handles

# ── People-to-follow extraction ────────────────────────────────────────────
def extract_mentioned_accounts(items):
    """
    Find @handles that appear in RT headers or @mentions but are NOT already
    in our feed list (we can't check against feed list easily, so we return
    all candidates with frequency counts and let Claude filter).
    """
    rt_pattern = re.compile(r'^RT\s+([A-Za-z][^:]{1,40}):', re.IGNORECASE)
    mention_pattern = re.compile(r'@([A-Za-z0-9_]{1,50})')

    rt_sources = []
    all_mentions = []

    for item in items:
        text = item['desc']
        # RT source name (display name before colon)
        rt_match = rt_pattern.match(text)
        if rt_match:
            rt_sources.append(rt_match.group(1).strip())
        # @handle mentions
        all_mentions.extend(mention_pattern.findall(text))

    rt_counts = Counter(rt_sources)
    mention_counts = Counter(all_mentions)

    # Top RT'd (by display name) and top @mentioned handles
    top_rt = rt_counts.most_common(10)
    top_mentions = [(h, c) for h, c in mention_counts.most_common(20)
                    if h.lower() not in ('i', 'the', 'you', 'we')]

    return top_rt, top_mentions

# ── Claude CLI invocation ──────────────────────────────────────────────────
BATCH_PROMPT = """You are preparing an AI industry briefing for a principal consultant who leads a team of consultants. Classify and summarise this batch of recent posts from AI industry figures on Twitter/X.

CLASSIFICATION QUADRANTS:
- ACT NOW: Actionable AND Important. New tool/feature/release available today, significant capability change, something the consultant or their team can adopt immediately. Must be concrete and usable.
- QUEUE: Actionable but less urgent. Worth evaluating, minor updates, useful techniques that don't need immediate attention.
- INFORM: Not directly actionable but important for strategic awareness. Industry trends, research findings, CEO/leader statements about AI's direction, things clients will ask about.
- SKIP: Not actionable, not strategically important. Memes, politics, social commentary, personal posts unrelated to AI practice.

OUTPUT FORMAT: respond with a single JSON code block and nothing else.

```json
{{
  "act_now": [{{"title": "...", "summary": "1-2 sentence summary", "url": "...", "source": "@handle"}}],
  "queue":   [{{"title": "...", "summary": "1-2 sentence summary", "url": "...", "source": "@handle"}}],
  "inform":  [{{"title": "...", "summary": "1-2 sentence summary", "url": "...", "source": "@handle"}}],
  "skip":    {{"count": 0}}
}}
```

Keep each summary to 1-2 sentences. If a section has no items, use an empty list [].

Here are the posts to classify:

{posts}
"""

PEOPLE_PROMPT = """You are preparing an AI industry briefing for a principal consultant. Based on the RT and mention frequency data below, recommend 3-5 accounts worth following. Prioritise people sharing original tools, research, or practitioner insights — not just commentators. Do NOT recommend any account in the ALREADY FOLLOWING list.

OUTPUT FORMAT: respond with a single JSON code block and nothing else.

```json
{{
  "people": [{{"handle": "@...", "reason": "one line on why"}}]
}}
```

{context}
"""

CLAUDE_TIMEOUT = 120  # per-batch; well within range for ~15 posts
CLAUDE_MODEL = "claude-sonnet"
BATCH_SIZE = 15


def _call_claude(prompt):
    """Invoke `claude -p` and return (stdout, error_string)."""
    try:
        result = subprocess.run(
            ['claude', '-p', prompt],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env={**os.environ, 'TERM': 'dumb'},
        )
        if result.returncode != 0:
            err = result.stderr.strip()[:500]
            return None, f"Claude CLI error (exit {result.returncode}): {err}"
        output = result.stdout.strip()
        if not output:
            return None, "Claude CLI returned empty output"
        return output, None
    except subprocess.TimeoutExpired:
        return None, f"Claude CLI timed out after {CLAUDE_TIMEOUT}s"
    except FileNotFoundError:
        return None, "claude CLI not found in PATH"


def _format_batch(items):
    lines = [f"Total posts in batch: {len(items)}\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"[{i}] @{item['author']} | {item['published'].strftime('%H:%M UTC')}")
        lines.append(f"    {item['desc'][:400]}")
        lines.append(f"    {item['link']}")
        lines.append("")
    return "\n".join(lines)


def _extract_batch_json(text):
    """Pull the JSON object out of a batch response (no 'people' key expected)."""
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"JSON decode error: {e}"
    for key in ("act_now", "queue", "inform"):
        if key not in data:
            data[key] = []
    return data, None


def classify_in_batches(items, top_rt, top_mentions, following, tracer):
    """
    Classify posts in batches of BATCH_SIZE, then fetch PEOPLE TO FOLLOW
    in a final lightweight call. Returns (merged_data, error_string).
    """
    merged = {"act_now": [], "queue": [], "inform": [], "skip": {"count": 0}}

    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    log.info("Classifying %d posts in %d batches (size %d)", len(items), len(batches), BATCH_SIZE)

    for idx, batch in enumerate(batches, 1):
        with tracer.start_as_current_span("briefing.classify_batch") as span:
            span.set_attribute("batch.index", idx)
            span.set_attribute("batch.size", len(batch))
            span.set_attribute("tweet.count", len(batch))
            span.set_attribute("tweet.ids", ",".join(item.get("guid", "") for item in batch))
            span.set_attribute("classification.model", CLAUDE_MODEL)
            span.set_attribute("classification.timeout_sec", CLAUDE_TIMEOUT)
            span.set_attribute("pipeline.stage", "classification")
            log.info("Batch %d/%d: %d posts", idx, len(batches), len(batch))

            prompt = BATCH_PROMPT.format(posts=_format_batch(batch))
            output, error = _call_claude(prompt)
            if error:
                span.set_status(trace.StatusCode.ERROR, error)
                return None, f"Batch {idx}/{len(batches)} failed: {error}"

            data, parse_error = _extract_batch_json(output)
            if parse_error:
                span.set_status(trace.StatusCode.ERROR, parse_error)
                return None, f"Batch {idx}/{len(batches)} parse error: {parse_error}"

            for key in ("act_now", "queue", "inform"):
                merged[key].extend(data.get(key, []))
            merged["skip"]["count"] += data.get("skip", {}).get("count", 0)

    # People-to-follow as a separate lightweight call
    with tracer.start_as_current_span("briefing.classify_people") as span:
        context_lines = []
        context_lines.append("FREQUENTLY RT'd (display name, count):")
        for name, count in top_rt:
            context_lines.append(f"  {name} ({count}x)")
        context_lines.append("\nFREQUENTLY MENTIONED (@handle, count):")
        for handle, count in top_mentions[:10]:
            context_lines.append(f"  @{handle} ({count}x)")
        if following:
            context_lines.append("\nALREADY FOLLOWING (exclude from recommendations):")
            for handle in sorted(following):
                context_lines.append(f"  @{handle}")

        prompt = PEOPLE_PROMPT.format(context="\n".join(context_lines))
        output, error = _call_claude(prompt)
        if error:
            span.set_status(trace.StatusCode.ERROR, error)
            log.warning("People-to-follow call failed: %s — omitting section", error)
            merged["people"] = []
        else:
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', output, re.DOTALL)
            raw = m.group(1) if m else output.strip()
            try:
                merged["people"] = json.loads(raw).get("people", [])
            except json.JSONDecodeError:
                log.warning("People JSON parse failed — omitting section")
                merged["people"] = []

    # Stamp section, index, and stable id onto every item
    for section_key in ("act_now", "queue", "inform"):
        for idx, item in enumerate(merged.get(section_key, []), 1):
            item["section"] = section_key
            item["index"] = idx
            key_str = (item.get("url") or item.get("title") or "") + str(idx)
            item["id"] = "item_" + hashlib.sha1(key_str.encode()).hexdigest()[:8]
    for idx, item in enumerate(merged.get("people", []), 1):
        item["section"] = "people"
        item["index"] = idx
        item["id"] = "item_" + hashlib.sha1((item.get("handle", "") + str(idx)).encode()).hexdigest()[:8]

    return merged, None


# ── Redis item storage ─────────────────────────────────────────────────────
def store_in_redis(sections, redis_host="localhost"):
    """Store each item and section display text in Redis with 24h TTL. Silent no-op if unavailable."""
    try:
        import redis as _redis
        r = _redis.Redis(host=redis_host, port=6379, decode_responses=True)
        for key in ("act_now", "queue", "inform", "people"):
            items = sections.get(key, [])
            for item in items:
                r.setex(f"item:{item['id']}", 86400, json.dumps(item))
            if items:
                r.setex(f"section:{key}", 86400, json.dumps({
                    "text": format_section_text(key, items),
                    "count": len(items),
                }))
    except Exception:
        pass


# ── Section formatting ─────────────────────────────────────────────────────
SECTION_HEADERS = {
    "act_now": "🔴 <b>ACT NOW</b>",
    "queue":   "🟡 <b>QUEUE</b>",
    "inform":  "🔵 <b>INFORM</b>",
    "people":  "👥 <b>PEOPLE TO FOLLOW</b>",
}


def format_section_text(key, items):
    parts = [SECTION_HEADERS[key], ""]
    if key in ("act_now", "queue"):
        for item in items:
            line = f"{item['index']}. <b>{item['title']}</b> — {item['summary']}"
            if item.get("url"):
                line += f'\n   <a href="{item["url"]}">↗</a>'
            parts.append(line)
    elif key == "inform":
        for item in items:
            parts.append(f"{item['index']}. {item['title']} — {item['summary']}")
    elif key == "people":
        for item in items:
            parts.append(f"{item['index']}. {item['handle']} — {item['reason']}")
    return "\n".join(parts)


def make_item_keyboard(items):
    """Build [N][💾] inline keyboard rows, one per item."""
    rows = []
    for item in items:
        rows.append([
            {"text": f"{item['index']} 💾", "callback_data": f"save:{item['id']}"},
        ])
    return {"inline_keyboard": rows}


def send_briefing(token, chat_id, sections, timestamp, item_count):
    section_count = sum(1 for k in SECTION_HEADERS if sections.get(k))
    header = (
        f"📡 <b>AI Briefing — {timestamp}</b>\n"
        f"{item_count} posts processed · {section_count} sections"
    )
    send_telegram(token, chat_id, header)
    for key in SECTION_HEADERS:
        items = sections.get(key, [])
        if not items:
            continue
        send_telegram(token, chat_id, format_section_text(key, items), reply_markup=make_item_keyboard(items))


# ── Telegram delivery ──────────────────────────────────────────────────────
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MSG_LEN = 4000

def send_telegram(token, chat_id, text, reply_markup=None):
    """Send text to Telegram, splitting if needed. reply_markup attached to last chunk only."""
    chunks = []
    while len(text) > MAX_MSG_LEN:
        split_at = text.rfind('\n', 0, MAX_MSG_LEN)
        if split_at == -1:
            split_at = MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)

    errors = []
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        req = Request(
            TELEGRAM_API.format(token=token),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    errors.append(str(result))
        except Exception as e:
            errors.append(str(e))
    return errors

# ── Main ───────────────────────────────────────────────────────────────────
MAX_POSTS = 50

def main():
    tracer = otel.setup("ai-briefing")
    env = load_env()
    token = env.get("TELEGRAM_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    redis_host = env.get("REDIS_HOST", "localhost")

    if not token or not chat_id:
        log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        otel.shutdown()
        sys.exit(1)

    try:
        with tracer.start_as_current_span("briefing.run") as root_span:
            try:
                state = load_state()
                window_start = get_window_start(state)
                window_end = datetime.now(timezone.utc)
                root_span.set_attribute("window.start", window_start.isoformat())
                root_span.set_attribute("window.end", window_end.isoformat())

                log.info("Fetching posts %s → %s",
                         window_start.strftime('%Y-%m-%d %H:%M UTC'),
                         window_end.strftime('%Y-%m-%d %H:%M UTC'))

                with tracer.start_as_current_span("briefing.fetch_feeds") as fetch_span:
                    items = fetch_all_feeds(window_start)
                    fetch_span.set_attribute("posts.fetched", len(items))
                    fetch_span.set_attribute("pipeline.stage", "fetch")

                log.info("Found %d unique posts after deduplication", len(items))

                if len(items) > MAX_POSTS:
                    log.info("Capping to %d most recent posts (dropped %d)", MAX_POSTS, len(items) - MAX_POSTS)
                    items = items[-MAX_POSTS:]

                root_span.set_attribute("posts.count", len(items))

                if not items:
                    msg = f"📭 AI Briefing — {window_end.strftime('%a %d %b %H:%M')}\nNo new posts since {window_start.strftime('%H:%M')}."
                    send_telegram(token, chat_id, msg)
                    save_state({"last_run": window_end.isoformat()})
                    return

                top_rt, top_mentions = extract_mentioned_accounts(items)
                following = load_following()

                log.info("Sending %d posts to Claude for classification", len(items))
                sections, error = classify_in_batches(items, top_rt, top_mentions, following, tracer)

                if error:
                    log.error("Claude classification failed: %s", error)
                    send_telegram(token, chat_id, f"⚠️ AI Briefing failed: {error}")
                    root_span.set_status(trace.StatusCode.ERROR, error)
                    sys.exit(1)

                timestamp = window_end.strftime('%a %d %b, %H:%M')

                log.info("Sending briefing to Telegram")
                with tracer.start_as_current_span("briefing.deliver") as deliver_span:
                    deliver_span.set_attribute("pipeline.stage", "deliver")
                    store_in_redis(sections, redis_host)
                    errors = []
                    send_briefing(token, chat_id, sections, timestamp, len(items))
                    delivered = sum(len(sections.get(k, [])) for k in ("act_now", "queue", "inform", "people"))
                    deliver_span.set_attribute("items.delivered", delivered)
                    for k in ("act_now", "queue", "inform", "people"):
                        deliver_span.set_attribute(f"section.{k}", len(sections.get(k, [])))
                    if errors:
                        deliver_span.set_attribute("telegram.errors", len(errors))

                if errors:
                    log.warning("Telegram errors: %s", errors)
                else:
                    log.info("Delivered successfully")

                save_state({"last_run": window_end.isoformat()})

            except SystemExit:
                raise
            except Exception as e:
                root_span.record_exception(e)
                root_span.set_status(trace.StatusCode.ERROR, str(e))
                raise
    finally:
        otel.shutdown()

if __name__ == "__main__":
    main()
