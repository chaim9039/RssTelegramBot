import json
import logging
import time
from os import getenv
from pathlib import Path
from typing import Iterable, Set
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.packages.urllib3.exceptions import InsecureRequestWarning


BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "sent_links.json"
CHECK_INTERVAL_SECONDS = 30 * 60
USER_AGENT = "Mozilla/5.0 (compatible; RSSTelegramBot/1.0; +https://t.me/)"
RSS_FEEDS = [
    "https://passportnews.co.il/feed",
    # "https://example.com/feed",
    # "https://another-example.com/rss",
]
SCRAPED_SOURCES = [
    "http://tnet.org.il",
]
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


def load_environment() -> tuple[str, str]:
    load_dotenv()

    bot_token = getenv("BOT_TOKEN")
    chat_id = getenv("CHAT_ID")

    if not bot_token or not chat_id:
        raise ValueError("BOT_TOKEN and CHAT_ID must be set in the .env file.")

    return bot_token, chat_id


def load_sent_links() -> Set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        logging.warning("Could not read %s: %s", STATE_FILE, error)
        return set()

    if not isinstance(data, list):
        logging.warning("%s does not contain a JSON list. Starting with empty state.", STATE_FILE)
        return set()

    return {item for item in data if isinstance(item, str) and item.strip()}


def save_sent_links(links: Iterable[str]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as file:
            json.dump(sorted(set(links)), file, ensure_ascii=False, indent=2)
    except OSError as error:
        logging.error("Could not write %s: %s", STATE_FILE, error)


def normalize_link(link: str, base_url: str) -> str:
    absolute_link = urljoin(base_url, link.strip())
    parsed = urlparse(absolute_link)

    cleaned = parsed._replace(
        fragment="",
        query="",
        scheme=parsed.scheme or urlparse(base_url).scheme,
    )
    return urlunparse(cleaned).rstrip("/")


def send_link_to_telegram(link: str, bot_token: str, chat_id: str) -> bool:
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": link,
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Failed to send link to Telegram: %s", error)
        return False

    logging.info("Sent link to Telegram: %s", link)
    return True


def fetch_entries(feed_url: str):
    try:
        feed = feedparser.parse(feed_url)
    except Exception as error:
        logging.error("Failed to parse feed %s: %s", feed_url, error)
        return []

    if getattr(feed, "bozo", 0):
        logging.warning(
            "Feed parsing warning for %s: %s",
            feed_url,
            getattr(feed, "bozo_exception", "unknown error"),
        )

    return getattr(feed, "entries", [])


def scrape_tnet() -> list[str]:
    base_url = "http://tnet.org.il"
    headers = {"User-Agent": USER_AGENT}
    ignored_domains = {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "instagram.com",
        "www.instagram.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "t.me",
        "telegram.me",
        "wa.me",
        "whatsapp.com",
        "www.whatsapp.com",
        "linkedin.com",
        "www.linkedin.com",
    }
    ignored_path_tokens = {
        "/tag/",
        "/tags/",
        "/category/",
        "/categories/",
        "/author/",
        "/authors/",
        "/search",
        "/video",
        "/videos",
        "/gallery",
        "/galleries",
        "/podcast",
        "/podcasts",
    }
    ignored_link_texts = {
        "",
        "facebook",
        "twitter",
        "x",
        "instagram",
        "youtube",
        "telegram",
        "whatsapp",
        "linkedin",
        "קרא עוד",
        "לרשימה המלאה",
        "עוד",
        "more",
        "read more",
    }

    try:
        response = requests.get(
            base_url,
            headers=headers,
            timeout=20,
            verify=False,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Failed to fetch TNET homepage: %s", error)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    collected_links: list[str] = []
    seen_links: set[str] = set()

    main_content = (
        soup.find("main")
        or soup.find("div", id="content")
        or soup.find("div", class_="content")
        or soup.find("div", class_="td-main-content-wrap")
        or soup.find("body")
    )

    if main_content is None:
        return []

    for anchor in main_content.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue

        parsed_href = urlparse(href)
        if not parsed_href.scheme or not parsed_href.netloc:
            continue

        normalized = normalize_link(href, base_url)
        parsed = urlparse(normalized)
        domain = parsed.netloc.lower()

        if domain in {"tnet.org.il", "www.tnet.org.il"}:
            continue
        if domain in ignored_domains:
            continue
        if any(token in parsed.path.lower() for token in ignored_path_tokens):
            continue

        link_text = " ".join(anchor.stripped_strings).strip().lower()
        if link_text in ignored_link_texts:
            continue
        if len(link_text) < 12:
            continue

        path_segments = [segment for segment in parsed.path.split("/") if segment]
        last_segment = path_segments[-1] if path_segments else ""
        article_like_path = (
            len(path_segments) >= 2
            or any(char.isdigit() for char in parsed.path)
            or "-" in last_segment
            or len(last_segment) >= 20
        )
        if not article_like_path:
            continue

        if normalized in seen_links:
            continue

        seen_links.add(normalized)
        collected_links.append(normalized)

    return collected_links


def process_links(links: Iterable[str], sent_links: Set[str], bot_token: str, chat_id: str) -> bool:
    updated = False

    for link in links:
        if not link or link in sent_links:
            continue

        if send_link_to_telegram(link, bot_token, chat_id):
            sent_links.add(link)
            updated = True

    return updated


def process_sources(bot_token: str, chat_id: str) -> None:
    sent_links = load_sent_links()
    updated = False

    for feed_url in RSS_FEEDS:
        logging.info("Checking feed: %s", feed_url)
        entries = fetch_entries(feed_url)
        feed_links = [getattr(entry, "link", "").strip() for entry in entries]
        updated = process_links(feed_links, sent_links, bot_token, chat_id) or updated

    for source_url in SCRAPED_SOURCES:
        if "tnet.org.il" in source_url:
            logging.info("Checking scraped source: %s", source_url)
            scraped_links = scrape_tnet()
            updated = process_links(scraped_links, sent_links, bot_token, chat_id) or updated

    if updated:
        save_sent_links(sent_links)


def main() -> None:
    try:
        bot_token, chat_id = load_environment()
    except Exception as error:
        logging.error("Startup failed: %s", error)
        return

    logging.info("RSS Telegram bot started. Checking feeds every %s minutes.", CHECK_INTERVAL_SECONDS // 60)

    while True:
        try:
            process_sources(bot_token, chat_id)
        except Exception as error:
            logging.exception("Unexpected error during feed processing: %s", error)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
