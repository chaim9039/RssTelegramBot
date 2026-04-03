import json
import logging
import re
import time
from os import getenv
from pathlib import Path
from typing import Iterable, Optional, Set
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
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]
RSS_FEEDS = [
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


HEBREW_SUMMARY_PROMPT = (
    "אתה מסכם כתבות תיירות לפורמט קצר ביותר.\n\n"
    "החזר רק עותק JSON חוקי בפורמט הזה בדיוק:\n"
    '{"title":"...","bullets":["...","...","..."]}\n\n'
    "אסור להחזיר כל טקסט נוסף. אין הסברים, אין הקדמות, אין כותרות מסגרת, ואין עטיפת Markdown (ללא ```json).\n\n"
    "חוקי סינון חובה:\n"
    '- אתה סוכן תיירות בלבד. אם הכתבה אינה עוסקת בתיירות, תעופה, או חופשות - החזר בדיוק את ה-JSON הבא: {"title":"SKIP","bullets":[]}\n\n'
    "חוקים ל-title:\n"
    "- משפט שלם וקצר.\n"
    "- חייב להיות ברור ולא חתוך באמצע.\n"
    "- אם אפשר לקצר עוד, קצר.\n\n"
    "חוקים ל-bullets:\n"
    "- בין 1 ל-3 bullets לכל היותר.\n"
    "- כל bullet הוא משפט אחד קצר מאוד.\n"
    "- אין משפטים ארוכים, אין הסברים, אין חזרות.\n\n"
    "חוקי תוכן:\n"
    "- רק מידע חשוב ומעשי (מחירים, תאריכים, יעדים, שינויים והשפעות מעשיות).\n"
    "- אין מילים שיווקיות, אין hype, אין ניסוחי פרסום, אין מבוא.\n\n"
    "חוק שפה:\n"
    "- גם אם הכתבה באנגלית, הסיכום חייב להיות בעברית.\n\n"
    "חוק על:\n"
    "- אם התוצאה ארוכה מדי, קצר שוב עד שכל שדה יהיה קצר ביותר.\n"
    "טקסט הכתבה:\n"
)
BULLET = "\u2022"
DEFAULT_HEBREW_TITLE = "עדכון"
MIN_ARTICLE_TEXT_LENGTH = 80
MAX_ARTICLE_TEXT_LENGTH = 12000
MAX_BULLET_LENGTH = 65
MAX_TITLE_LENGTH = 60
PROMOTIONAL_PATTERNS = (
    "תוכן שיווקי",
    "בשיתוף",
    "בשיתוף פעולה",
    "ממומן",
    "המודעה",
    "פרסומת",
    "רוצה לקרוא עוד",
    "לכתבה המלאה",
    "לרכישה",
    "למידע נוסף",
    "לחצו כאן",
    "הצלחה אמיתית",
    "שביעות רצון",
    "טיפולי הרזיה",
    "בואו להכיר",
    "המיונים בעיצומם",
    "לחצו",
    "לחצו כאן",
    "להרשמה",
    "הרשמו",
    "קנו עכשיו",
)


def load_environment() -> tuple[str, str, Optional[str]]:
    load_dotenv()

    bot_token = getenv("BOT_TOKEN")
    chat_id = getenv("CHAT_ID")
    gemini_api_key = getenv("GEMINI_API_KEY")

    if not bot_token or not chat_id:
        raise ValueError("BOT_TOKEN and CHAT_ID must be set.")

    if not gemini_api_key:
        logging.warning("GEMINI_API_KEY is not set. Falling back to sending links only.")

    return bot_token, chat_id, gemini_api_key


def get_gemini_models() -> list[str]:
    configured_model = (getenv("GEMINI_MODEL") or "").strip()
    if configured_model:
        return [configured_model]

    return DEFAULT_GEMINI_MODELS[:]


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


def send_telegram_message(text: str, bot_token: str, chat_id: str) -> bool:
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
    except requests.RequestException as error:
        logging.error("Failed to send Telegram message: %s", error)
        return False

    logging.info("Sent Telegram message.")
    return True


def send_link_to_telegram(link: str, bot_token: str, chat_id: str) -> bool:
    if not send_telegram_message(link, bot_token, chat_id):
        return False

    logging.info("Sent link to Telegram: %s", link)
    return True


def fetch_article_content(url: str) -> Optional[str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except requests.RequestException as error:
        logging.warning("Failed to fetch article HTML for %s: %s", url, error)
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and response.text.strip().lower().startswith("<!doctype html") is False:
        logging.warning("Unexpected content type for %s: %s", url, content_type or "unknown")
        return None

    return response.text


def _pick_main_container(soup: BeautifulSoup):
    selectors = [
        "article",
        "main",
        "[role='main']",
        ".post-content",
        ".entry-content",
        ".article-content",
        ".article-body",
        ".post-body",
        ".story-content",
        ".content",
    ]

    for selector in selectors:
        match = soup.select_one(selector)
        if match:
            return match

    candidates = soup.find_all(
        ["div", "section"],
        class_=lambda value: value and any(
            token in (
                " ".join(value).lower() if isinstance(value, list) else str(value).lower()
            )
            for token in ("article", "content", "post", "entry", "story", "body", "text")
        ),
    )
    if candidates:
        return max(candidates, key=lambda element: len(" ".join(element.stripped_strings)))

    return soup.body


def _is_promotional_text(text: str) -> bool:
    normalized = " ".join(text.split()).strip().lower()
    if not normalized:
        return True

    if any(pattern in normalized for pattern in PROMOTIONAL_PATTERNS):
        return True

    if len(normalized) < 20:
        return True

    return False


def extract_main_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "iframe",
            "header",
            "footer",
            "nav",
            "form",
            "aside",
        ]
    ):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    h1 = soup.find("h1")
    if h1:
        h1_text = " ".join(h1.stripped_strings).strip()
        if h1_text:
            title = h1_text

    meta_description = ""
    description_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta",
        property="og:description",
    )
    if description_tag and description_tag.get("content"):
        meta_description = " ".join(description_tag["content"].split()).strip()

    main_container = _pick_main_container(soup)

    if main_container is None:
        return title, meta_description

    text_blocks: list[str] = []
    for element in main_container.find_all(["h2", "h3", "p", "li"]):
        text = " ".join(element.stripped_strings).strip()
        if _is_promotional_text(text):
            continue
        text_blocks.append(text)

    if not text_blocks:
        fallback_text = " ".join(main_container.stripped_strings).strip()
        if meta_description and meta_description not in fallback_text:
            fallback_text = f"{meta_description}\n{fallback_text}".strip()
        return title, fallback_text

    extracted_text = "\n".join(text_blocks)
    if meta_description and meta_description not in extracted_text:
        extracted_text = f"{meta_description}\n{extracted_text}".strip()

    return title, extracted_text


def _extract_summary_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "\n".join(part.get("text", "").strip() for part in parts if part.get("text")).strip()


def _extract_summary_json(data: dict) -> Optional[dict]:
    raw_text = _extract_summary_text(data)
    if not raw_text:
        return None

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        logging.warning("Gemini returned non-JSON summary payload.")
        return None

    if not isinstance(parsed, dict):
        logging.warning("Gemini summary JSON is not an object.")
        return None

    return parsed


def _clean_summary_line(line: str) -> str:
    cleaned = " ".join(line.split()).strip()
    if cleaned.lower().startswith("כותרת:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if cleaned.lower().startswith("נקודות:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if cleaned.startswith((BULLET, "-", "*")):
        cleaned = cleaned[1:].strip()
    return cleaned.strip(f" -{BULLET}*")

def _is_low_value_sentence(sentence: str, title: str = "") -> bool:
    normalized = " ".join(str(sentence).split()).strip()
    if len(normalized) < 12:
        return True

    lowered = normalized.casefold()
    if any(pattern.casefold() in lowered for pattern in PROMOTIONAL_PATTERNS):
        return True

    noisy_phrases = (
        "בואו",
        "להכיר את",
        "לחצו",
        "הקליקו",
        "הירשמו",
        "הרשמו",
        "אל תפספסו",
        "קראו עוד",
        "לכתבה המלאה",
        "למידע נוסף",
    )
    if any(phrase in lowered for phrase in noisy_phrases):
        return True

    if title and _word_overlap_ratio(normalized, title) >= 0.75:
        return True

    return False

def _normalize_structured_summary(payload: dict) -> Optional[tuple[str, list[str]]]:
    raw_title = payload.get("title", "")
    raw_bullets = payload.get("bullets", [])

    title = _shorten_text(raw_title, MAX_TITLE_LENGTH)
    if not isinstance(raw_bullets, list):
        return None

    bullets: list[str] = []
    for item in raw_bullets:
        cleaned = " ".join(str(item).split()).strip().strip(f"-{BULLET}* ")
        cleaned = _shorten_text(cleaned, MAX_BULLET_LENGTH)
        if len(cleaned) < 6 or _is_low_value_sentence(cleaned, title):
            continue
        bullets.append(cleaned)

    bullets = _dedupe_preserve_order(bullets)
    if not bullets:
        return None

    if len(bullets) == 1:
        return title, bullets[:1]

    return title, bullets[:3]


def _parse_text_summary(raw_text: str) -> Optional[dict]:
    lines_local = [" ".join(line.split()).strip() for line in raw_text.splitlines()]
    lines_local = [line for line in lines_local if line]
    if not lines_local:
        return None

    title = ""
    bullets: list[str] = []
    for line in lines_local:
        cleaned = _clean_summary_line(line)
        if not cleaned:
            continue
        if not title and not line.startswith((BULLET, "-", "*")):
            title = cleaned
            continue
        bullets.append(cleaned)

    if len(bullets) < 2:
        compact = " ".join(lines_local)
        sentence_parts = [part.strip() for part in compact.replace(BULLET, ". ").split(".") if part.strip()]
        if sentence_parts:
            if not title:
                title = sentence_parts[0]
                bullets = sentence_parts[1:4]
            else:
                bullets = sentence_parts[:3]

    title = _shorten_text(title, MAX_TITLE_LENGTH)
    bullets = [
        _compact_bullet(item)
        for item in bullets
        if len(item) >= 6 and not _is_low_value_sentence(item, title)
    ][:3]
    bullets = _dedupe_preserve_order(bullets)
    if not bullets:
        return None

    return {"title": title, "bullets": bullets}


def _dedupe_preserve_order(lines: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []

    for line in lines:
        normalized = " ".join(str(line).split()).strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)

    return deduped


def _shorten_text(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split()).strip().rstrip(" ,.-")
    if len(cleaned) <= limit:
        return cleaned

    candidate = cleaned[: limit + 1]
    last_space = candidate.rfind(" ")
    if last_space >= max(10, limit // 2):
        candidate = candidate[:last_space]
    else:
        candidate = candidate[:limit]

    return candidate.rstrip(" ,.-")


def _compact_bullet(text: str) -> str:
    cleaned = " ".join(str(text).split()).strip().rstrip(" ,.-")
    if not cleaned:
        return ""

    splitters = (
        ", אבל",
        ", אולם",
        ", לאחר",
        ", אלם",
        ", ולכן",
        ", כך",
        ", כש",
        ", אשר",
    )
    for splitter in splitters:
        if splitter in cleaned:
            head = cleaned.split(splitter, 1)[0].strip().rstrip(" ,.-")
            if len(head) >= 20:
                cleaned = head
                break

    return _shorten_text(cleaned, MAX_BULLET_LENGTH)

def _split_sentences(text: str) -> list[str]:
    if not text or not text.strip():
        return []

    rough_parts: list[str] = []
    for block in re.split(r"\n+", text):
        block = " ".join(block.split()).strip()
        if not block:
            continue
        rough_parts.extend(re.split(r"(?<=[.!?ג€¦])\s+|(?<=:)\s+", block))

    sentences: list[str] = []
    for part in rough_parts:
        cleaned = " ".join(part.split()).strip(" -ג€¢*\t\r\n")
        if len(cleaned) < 20:
            continue
        sentences.append(cleaned)

    return _dedupe_preserve_order(sentences)


def _score_sentence(sentence: str) -> tuple[int, int]:
    score = 0
    lowered = sentence.lower()

    if any(char.isdigit() for char in sentence):
        score += 3
    if any(token in lowered for token in ("מ-", "ב-", "על", "ממומנים", "אלף", "%", "שקל", "דולר", "אירו")):
        score += 2
    if any(token in lowered for token in ("מיל", "מצותי", "מועד", "חול", "יעד", "טיסות", "עולם", "דרום", "מזרח")):
        score += 2
    if '"' in sentence or "׳" in sentence:
        score -= 1
    if _is_low_value_sentence(sentence):
        score -= 5

    return score, len(sentence)

def _trim_hype_prefix(sentence: str) -> str:
    prefixes = (
        "הכתבה המלאה",
        "לפרטים",
        "בשורה",
        "לראשונה",
        "טרם ראינו",
        "השירות",
        "עליון",
    )
    lowered = sentence.lower()
    for prefix in prefixes:
        prefix_lower = prefix.lower()
        if lowered.startswith(prefix_lower) and ":" in sentence:
            sentence = sentence.split(":", 1)[1].strip()
            break
    return sentence.strip()

def _word_overlap_ratio(left: str, right: str) -> float:
    left_words = {word for word in re.findall(r"[\w\u0590-\u05FF]+", left.casefold()) if len(word) >= 3}
    right_words = {word for word in re.findall(r"[\w\u0590-\u05FF]+", right.casefold()) if len(word) >= 3}
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / min(len(left_words), len(right_words))


def summarize_locally(text: str, fallback_title: str = "") -> Optional[dict]:
    cleaned_text = " ".join(text.split()).strip()
    if len(cleaned_text) < MIN_ARTICLE_TEXT_LENGTH:
        return None

    title = " ".join((fallback_title or "").split()).strip()
    sentences = _split_sentences(text)
    if not sentences:
        return None

    filtered_sentences = [
        sentence
        for sentence in sentences
        if sentence.casefold() != title.casefold()
    ]
    ranked_sentences = sorted(
        filtered_sentences or sentences,
        key=_score_sentence,
        reverse=True,
    )

    scored_sentences: list[tuple[int, str]] = []
    for sentence in ranked_sentences:
        sentence = _trim_hype_prefix(sentence)
        if title and sentence.casefold() == title.casefold():
            continue
        if title and _word_overlap_ratio(sentence, title) >= 0.75:
            continue
        if _is_low_value_sentence(sentence, title):
            continue
        if len(sentence) > 110:
            continue
        if sentence.endswith(":"):
            continue
        scored_sentences.append((_score_sentence(sentence)[0], sentence.rstrip(".")))
        if len(scored_sentences) == 4:
            break

    bullets = [sentence for score, sentence in scored_sentences if score > 0]
    bullets = _dedupe_preserve_order(bullets)
    if not bullets and scored_sentences:
        bullets = _dedupe_preserve_order([sentence for _, sentence in scored_sentences[:1]])
    if len(bullets) < 2 and len(sentences) >= 2:
        bullets = _dedupe_preserve_order([sentence.rstrip(".") for sentence in sentences[:3]])

    bullets = [
        _compact_bullet(bullet)
        for bullet in bullets
        if len(bullet) >= 20
    ][:3]
    bullets = _dedupe_preserve_order(bullets)
    if not bullets:
        return None

    summary_title = _shorten_text(title or bullets[0], MAX_TITLE_LENGTH)
    if summary_title in bullets:
        bullets = [bullet for bullet in bullets if bullet != summary_title]

    if not bullets:
        return None

    if len(bullets) == 1:
        return {"title": summary_title, "bullets": bullets[:1]}

    return {"title": summary_title, "bullets": bullets[:3]}


def _request_gemini_rest(
    api_key: str,
    model_name: str,
    prompt: str,
    response_mime_type: Optional[str] = None,
) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={api_key}"
    )
    generation_config = {
        "temperature": 0.2,
        "maxOutputTokens": 500,
        "topP": 0.8,
    }
    if response_mime_type:
        generation_config["responseMimeType"] = response_mime_type

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ]
            }
        ],
        "generationConfig": generation_config,
    }
    response = requests.post(
        url,
        headers={"Content-Type": "application/json; charset=utf-8"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=45,
    )
    response.raise_for_status()

    body = response.json()
    for candidate in body.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = (part.get("text") or "").strip()
            if text:
                return text

    return ""


def summarize_with_gemini(text: str) -> Optional[dict]:
    load_dotenv()
    gemini_api_key = getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        logging.warning("Cannot summarize without GEMINI_API_KEY.")
        return None

    article_text = text.strip()
    if not article_text:
        return None

    trimmed_text = article_text[:MAX_ARTICLE_TEXT_LENGTH]
    prompt = f"{HEBREW_SUMMARY_PROMPT}{trimmed_text}"

    last_error = None
    for model_name in get_gemini_models():
        try:
            raw_text = _request_gemini_rest(
                gemini_api_key,
                model_name,
                prompt,
                response_mime_type="application/json",
            )
        except Exception as error:
            logging.warning("Gemini REST request failed for model %s: %s", model_name, error)
            last_error = error
            raw_text = ""

        if raw_text:
            try:
                summary_payload = json.loads(raw_text)
            except json.JSONDecodeError:
                summary_payload = None

            normalized = _normalize_structured_summary(summary_payload) if summary_payload else None
            if normalized:
                summary_title, summary_bullets = normalized
                return {"title": summary_title, "bullets": summary_bullets}

            parsed = _parse_text_summary(raw_text)
            if parsed:
                return parsed

    try:
        import google.generativeai as genai
    except ImportError as error:
        logging.warning("google-generativeai is not installed: %s", error)
        if last_error:
            logging.warning("All Gemini model attempts failed.")
        return None

    genai.configure(api_key=gemini_api_key)

    for model_name in get_gemini_models():
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": 500,
                    "top_p": 0.8,
                },
            )
            raw_text = (getattr(response, "text", "") or "").strip()
        except Exception as error:
            logging.warning("Gemini SDK text fallback failed for model %s: %s", model_name, error)
            last_error = error
            continue

        parsed = _parse_text_summary(raw_text)
        if parsed:
            return parsed

        logging.warning("Gemini returned no usable summary for model %s.", model_name)

    if last_error:
        logging.warning("All Gemini model attempts failed.")

    return None

def format_telegram_message(title: str, summary: dict, link: str) -> str:
    cleaned_title = " ".join((title or "").split()).strip()
    summary_title = " ".join(str(summary.get("title", "")).split()).strip()
    bullet_candidates = [
        " ".join(str(item).split()).strip().strip(f"-{BULLET}* ")
        for item in summary.get("bullets", [])
    ]
    bullet_candidates = [line for line in bullet_candidates if len(line) >= 6]

    final_title = cleaned_title or summary_title or DEFAULT_HEBREW_TITLE
    bullet_lines = [f"{BULLET} {line}" for line in bullet_candidates[:3] if not _is_low_value_sentence(line, final_title)]
    if len(bullet_lines) < 2:
        return ""

    return f"{final_title}\n\n" + "\n".join(bullet_lines) + f"\n\n\u05e7\u05e8\u05d0 \u05e2\u05d5\u05d3:\n{link}"

def build_telegram_message(link: str) -> Optional[str]:
    html = fetch_article_content(link)
    if not html:
        logging.warning("Falling back to link only for %s because article HTML could not be fetched.", link)
        return None

    title, text = extract_main_text(html)
    article_text = "\n\n".join(part for part in [title, text] if part).strip()
    if len(article_text) < MIN_ARTICLE_TEXT_LENGTH:
        logging.warning("Falling back to link only for %s because extracted article text is too short.", link)
        return None

    summary = summarize_with_gemini(article_text) or summarize_locally(article_text, title)
    if not summary:
        logging.warning("Falling back to link only for %s because no summary could be generated.", link)
        return None

    formatted_message = format_telegram_message(title, summary, link)
    if not formatted_message or f"{BULLET} " not in formatted_message:
        logging.warning("Falling back to link only for %s because formatted summary is invalid.", link)
        return None

    return formatted_message


def fetch_entries(feed_url: str):
    try:
        response = requests.get(
            feed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            },
            timeout=20,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except requests.RequestException as error:
        logging.error("Failed to fetch feed %s: %s", feed_url, error)
        return []
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
        "׳§׳¨׳ ׳¢׳•׳“",
        "׳׳¨׳©׳™׳׳” ׳”׳׳׳׳”",
        "׳¢׳•׳“",
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
    processed_count = 0
    duplicate_count = 0

    for link in links:
        if not link or link in sent_links:
            if link in sent_links:
                duplicate_count += 1
            continue

        processed_count += 1
        message = build_telegram_message(link)
        send_succeeded = (
            send_telegram_message(message, bot_token, chat_id)
            if message
            else send_link_to_telegram(link, bot_token, chat_id)
        )

        if send_succeeded:
            sent_links.add(link)
            updated = True

    if processed_count == 0:
        logging.info("No new links to send. Skipped %s duplicate links.", duplicate_count)
    else:
        logging.info("Processed %s new links. Skipped %s duplicate links.", processed_count, duplicate_count)

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
        bot_token, chat_id, _ = load_environment()
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
