# RSS Telegram Bot

A lightweight Python bot that monitors news sources and automatically posts new articles to a Telegram chat or channel.

It combines two collection methods:
- **RSS feeds**: parsed with `feedparser`
- **Web scraping**: for sites without a usable feed, using `BeautifulSoup` with heuristic filtering to pick out real article links

Every link is sent once. The bot keeps a small persistent state file, so it doesn't send duplicates, even after a restart.

## Features

- **Multi-source monitoring**: any number of RSS feeds plus scraped homepages
- **Smart link filtering** for scraped pages:
  - ignores social-media domains (Facebook, X, Instagram, YouTube, WhatsApp, LinkedIn, Telegram)
  - skips tag, category, author, search, video and gallery pages
  - drops navigation-style anchors ("read more", icons, very short link text)
  - keeps only article-like URLs, based on path depth, numeric IDs and slugs
- **URL normalization**: strips query strings, fragments and trailing slashes, so the same article isn't sent twice under different URLs
- **Deduplication with persistent state** in `sent_links.json`
- **Resilient loop**: network, parsing and Telegram API errors are logged and never crash the bot
- **Secrets kept out of the code**: the bot token and chat ID are loaded from environment variables or a `.env` file
- **Structured logging** with timestamps and log levels

## How it works

```
 ┌──────────────┐     ┌──────────────┐
 │  RSS feeds   │     │ Scraped site │
 └──────┬───────┘     └──────┬───────┘
        │ feedparser         │ requests + BeautifulSoup + filters
        └─────────┬──────────┘
                  ▼
        normalize URLs & drop already-sent links  ◄── sent_links.json
                  ▼
        Telegram Bot API (sendMessage)
                  ▼
        save updated state ──► sent_links.json
                  ▼
        sleep 30 minutes, repeat
```

## Tech stack

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/): HTTP and the Telegram Bot API
- [`feedparser`](https://pypi.org/project/feedparser/): RSS and Atom parsing
- [`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/): HTML parsing
- [`python-dotenv`](https://pypi.org/project/python-dotenv/): configuration from `.env`

## Getting started

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`. You'll receive a **bot token**.
2. Add the bot to your target chat or channel. In a channel, make it an admin.
3. Get the **chat ID**, for example by sending a message to the chat and opening
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`.

### 2. Install

```bash
git clone https://github.com/chaim9039/RssTelegramBot.git
cd RssTelegramBot

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

```env
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz
CHAT_ID=-1001234567890
```

`.env` is listed in `.gitignore`, so your credentials are never committed.

### 4. Run

```bash
python rss_telegram_bot.py
```

```
2026-03-20 10:00:00 | INFO | RSS Telegram bot started. Checking feeds every 30 minutes.
2026-03-20 10:00:00 | INFO | Checking feed: https://passportnews.co.il/feed
2026-03-20 10:00:01 | INFO | Sent link to Telegram: https://passportnews.co.il/article/...
```

## Customization

All settings are constants at the top of `rss_telegram_bot.py`:

| Setting | Description | Default |
|---|---|---|
| `RSS_FEEDS` | List of RSS/Atom feed URLs to monitor | `passportnews.co.il/feed` |
| `SCRAPED_SOURCES` | Sites to scrape for article links | `tnet.org.il` |
| `CHECK_INTERVAL_SECONDS` | Time between checks | `1800` (30 min) |
| `STATE_FILE` | Where already-sent links are stored | `sent_links.json` |

To add a feed, append its URL to `RSS_FEEDS`.

## Project structure

```
RssTelegramBot/
├── rss_telegram_bot.py   # Main bot: fetching, filtering, sending, scheduling
├── requirements.txt      # Python dependencies
├── .env.example          # Template for required environment variables
└── .gitignore
```

`sent_links.json` is created automatically at runtime.

## Possible improvements

- Move the source list into a config file (YAML or JSON) instead of code constants
- Send richer messages with the title and preview, using Telegram's HTML formatting
- Containerize with Docker and run it as a scheduled job
- Add unit tests for the link-filtering and normalization logic
