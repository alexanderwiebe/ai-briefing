# AI Briefing: From Twitter Lists to Telegram Summary

A self-hosted system that turns curated Twitter/X lists into a structured AI briefing
delivered to Telegram twice a day. No Twitter API key. No subscriptions. Runs locally.

---

## What It Does

Fetches RSS feeds from your Twitter lists via RSSHub, deduplicates posts, classifies
them by actionability and importance using Claude CLI, and delivers a formatted briefing
to Telegram at 8am and 4:30pm daily.

```
Twitter Lists → RSSHub → Python script → Claude CLI → Telegram
```

---

## Prerequisites

- A Linux machine (this was built on Arch Linux)
- Docker installed and running
- Claude Code CLI installed (`claude` available in PATH)
- A Telegram account

---

## Step 1: Set Up RSSHub via Docker

RSSHub is an open source project that turns social media feeds (including Twitter/X lists)
into standard RSS feeds. It runs as a Docker container on your local network.

```bash
docker run -d \
  --name rsshub \
  --restart always \
  -p 1200:1200 \
  diygod/rsshub
```

Verify it's running:
```bash
curl http://localhost:1200
```

You should see the RSSHub welcome page.

> **Note:** RSSHub needs to be reachable from wherever your briefing script runs.
> If running on a server, use its LAN IP (e.g. `192.168.1.50:1200`) instead of `localhost`.

---

## Step 2: Find Your Twitter List IDs

Twitter/X list URLs look like this:
```
https://x.com/i/lists/2030847297706160376
                        ^^^^^^^^^^^^^^^^^^ this is the list ID
```

For each list you want to monitor, note the ID from the URL.

The RSSHub URL format for a Twitter list is:
```
http://<your-rsshub-host>:1200/twitter/list/<LIST_ID>
```

Test a feed works:
```bash
curl -s "http://192.168.1.50:1200/twitter/list/2030847297706160376" | head -20
```

You should see RSS/XML output with recent tweets.

---

## Step 3: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to name your bot
3. BotFather will give you a token like:
   ```
   8614237422:AAGZXxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Keep this safe — it gives full control of your bot.

4. Open a chat with your new bot and send it any message (e.g. "hello")

5. Get your chat ID by calling the Telegram API:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":123456789}` in the response — that number is your chat ID.

---

## Step 4: Set Up the Briefing Script

Create a directory for the project:
```bash
mkdir -p ~/ai-briefing
cd ~/ai-briefing
```

Download or copy `briefing.py` into this directory (see the script in this repo).

Create the `.env` file with your credentials:
```bash
cat > ~/ai-briefing/.env << EOF
TELEGRAM_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
EOF
```

Edit `briefing.py` and update the `FEEDS` dictionary at the top with your own list IDs
and whatever category names make sense to you:

```python
FEEDS = {
    "AI Builders & Practitioners": [
        "http://192.168.1.50:1200/twitter/list/YOUR_LIST_ID",
    ],
    "AI Products & Tools": [
        "http://192.168.1.50:1200/twitter/list/YOUR_LIST_ID",
    ],
    # add more categories as needed
}
```

Test it runs:
```bash
python3 ~/ai-briefing/briefing.py
```

You should receive a Telegram message within a minute or two (Claude needs time to classify).

---

## Step 5: Schedule with systemd Timers

Create the service unit:
```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/ai-briefing.service << EOF
[Unit]
Description=AI Daily Briefing
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/YOUR_USERNAME/ai-briefing/briefing.py
WorkingDirectory=/home/YOUR_USERNAME/ai-briefing
Environment=PATH=/home/YOUR_USERNAME/.local/bin:/usr/local/bin:/usr/bin:/bin
StandardOutput=journal
StandardError=journal
TimeoutStartSec=300
EOF
```

Create the morning timer (8:00am):
```bash
cat > ~/.config/systemd/user/ai-briefing-morning.timer << EOF
[Unit]
Description=AI Briefing — 08:00 daily

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true
Unit=ai-briefing.service

[Install]
WantedBy=timers.target
EOF
```

Create the afternoon timer (4:30pm):
```bash
cat > ~/.config/systemd/user/ai-briefing-afternoon.timer << EOF
[Unit]
Description=AI Briefing — 16:30 daily

[Timer]
OnCalendar=*-*-* 16:30:00
Persistent=true
Unit=ai-briefing.service

[Install]
WantedBy=timers.target
EOF
```

Enable and start the timers:
```bash
systemctl --user daemon-reload
systemctl --user enable ai-briefing-morning.timer ai-briefing-afternoon.timer
systemctl --user start ai-briefing-morning.timer ai-briefing-afternoon.timer
```

Verify they're scheduled:
```bash
systemctl --user list-timers | grep ai-briefing
```

---

## Step 6: Enable Linger (Run Without Being Logged In)

By default, user systemd services only run while you have an active login session.
To make the timers fire even when you're not logged in (e.g. on a headless server):

```bash
loginctl enable-linger YOUR_USERNAME
```

Verify:
```bash
loginctl show-user YOUR_USERNAME | grep Linger
# Should show: Linger=yes
```

---

## How the Classification Works

Each post is classified into one of four quadrants based on two axes:

| | Important | Less Important |
|---|---|---|
| **Actionable** | 🔴 Act Now | 🟡 Queue |
| **Not Actionable** | 🔵 Inform | ⚪ Skip |

**Actionable** means: a tool, feature, or technique you or your team can use today.
**Important** means: significant capability shift, something clients will ask about,
or something that changes how your team works.

The classification prompt is written for a principal consultant context. Edit the
`CLASSIFICATION_PROMPT` in `briefing.py` to match your own role and priorities.

The script also surfaces **people to follow** — accounts that are frequently RT'd or
mentioned by the people you already follow, but who aren't in your lists yet.

---

## Useful Commands

```bash
# Check next scheduled runs
systemctl --user list-timers | grep ai-briefing

# Run the briefing manually right now
systemctl --user start ai-briefing.service

# Watch it run in real time
journalctl --user -fu ai-briefing.service

# View logs from the last run
journalctl --user -u ai-briefing.service -n 50

# Stop the timers temporarily
systemctl --user stop ai-briefing-morning.timer ai-briefing-afternoon.timer

# Disable permanently
systemctl --user disable ai-briefing-morning.timer ai-briefing-afternoon.timer
```

---

## Troubleshooting

**RSSHub returns empty feeds**
Twitter scraping can break when RSSHub's underlying parser hits rate limits or
Twitter changes its structure. Check the RSSHub logs:
```bash
docker logs rsshub --tail 50
```

**Claude CLI times out**
If you have a large number of posts, Claude may take longer than expected.
Increase `TimeoutStartSec` in the service file, or reduce the number of feeds.

**Telegram message not arriving**
Check the token and chat ID in `.env`. Make sure you sent the bot a message first
before trying to get your chat ID — the bot can't message you until you've initiated
contact.

**`claude` not found when running via systemd**
The `Environment=PATH=...` line in the service file must include the directory where
`claude` is installed. Check with `which claude` and update accordingly.

---

## File Structure

```
~/ai-briefing/
├── briefing.py       # main script
├── .env              # secrets (token + chat ID) — do not commit
├── state.json        # auto-generated, tracks last run time
└── README.md         # this file
```
