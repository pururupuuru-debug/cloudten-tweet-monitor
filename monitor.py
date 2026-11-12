"""Personal X -> Telegram monitor, using publicly available Nitter RSS mirrors.

No X login or paid API key required. Availability depends on volunteer mirrors.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

HANDLE = os.getenv("X_HANDLE", "cloudten_member").strip().lstrip("@")
STATE_PATH = Path("state.json")
MIRRORS = [
    "https://nitter.mie.foundation",
    "https://nitter.mizuki.st",
    "https://nitter.meowing.monster",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalRSSMonitor/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
STATUS_RE = re.compile(r"/status/(\d{12,25})(?:\D|$)")


@dataclass(frozen=True)
class Tweet:
    id: str
    text: str
    image_urls: list[str]
    url: str


def download_feed() -> tuple[str, str]:
    errors = []
    for host in MIRRORS:
        url = f"{host}/{HANDLE}/rss"
        try:
            response = SESSION.get(url, timeout=20)
            response.raise_for_status()
            # Bot challenges often return HTML with status 200; reject them.
            root = ET.fromstring(response.content)
            if root.tag != "rss" or root.find("channel") is None:
                raise ValueError("response is not an RSS feed")
            if not root.findall("./channel/item"):
                raise ValueError("RSS contains no posts (possibly temporarily blocked)")
            print(f"RSS OK: {host}")
            return response.text, host
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
            print(f"RSS unavailable: {host}: {exc}")
    raise RuntimeError("No working RSS mirror. Check later.\n" + "\n".join(errors))


def image_url(src: str, base: str) -> str | None:
    """Turn Nitter's /pic/ proxy path into a Twitter CDN photo URL.

    Only photos from pbs.twimg.com/media are accepted. This avoids avatar images,
    quoted-post artwork and third-party URLs.
    """
    if not src:
        return None
    absolute = urljoin(base, src.strip())
    parts = urlsplit(absolute)
    if parts.hostname == "pbs.twimg.com":
        direct = absolute
    elif "/pic/" in parts.path:
        tail = unquote(parts.path.split("/pic/", 1)[1])
        if tail.startswith("orig/"):
            tail = tail[5:]
        if not tail.startswith(("pbs.twimg.com/", "https://pbs.twimg.com/")):
            return None
        direct = tail if tail.startswith("https://") else "https://" + tail
        if parts.query:
            direct += ("&" if "?" in direct else "?") + parts.query
    else:
        return None
    check = urlsplit(direct)
    if check.hostname != "pbs.twimg.com" or not check.path.startswith("/media/"):
        return None
    return direct


def extract_images(html: str, mirror: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    result = []
    for img in soup.find_all("img"):
        # A quoted tweet's photos should not be confused with the author's own photos.
        if img.find_parent("blockquote"):
            continue
        link = image_url(img.get("src", ""), mirror)
        if link and link not in result:
            result.append(link)
    return result[:10]


def extract_text(html: str, title: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    # Nitter puts the author's tweet text in the first <p>; subsequent elements
    # contain media, quote tweets, polls, previews and other incidental text.
    paragraph = soup.find("p")
    if paragraph:
        for br in paragraph.find_all("br"):
            br.replace_with("\n")
        text = paragraph.get_text("", strip=False).strip()
        if text:
            return text
    # In rare image-only posts, no text is present.
    return "" if title.strip() in ("Image", "Video", "Gif", "Media") else title.strip()


def parse_feed(xml_string: str, mirror: str) -> list[Tweet]:
    root = ET.fromstring(xml_string)
    tweets: dict[str, Tweet] = {}
    for entry in root.findall("./channel/item"):
        title = entry.findtext("title", default="") or ""
        if title.startswith(("RT by @", "RT: ")):
            # Ignore retweets of unrelated accounts; still include original posts
            # and replies by the watched account.
            continue
        link = entry.findtext("link", default="") or ""
        guid = entry.findtext("guid", default="") or ""
        match = STATUS_RE.search(link) or STATUS_RE.search(guid)
        if not match:
            # Modern Nitter GUID can be an ID without a status URL.
            match = re.match(r"^(\d{12,25})$", guid)
        if not match:
            continue
        tweet_id = match.group(1)
        desc = entry.findtext("description", default="") or ""
        tweets[tweet_id] = Tweet(
            id=tweet_id,
            text=extract_text(desc, title),
            image_urls=extract_images(desc, mirror),
            url=f"https://x.com/{HANDLE}/status/{tweet_id}",
        )
    return sorted(tweets.values(), key=lambda tw: int(tw.id))


def get_photo_bytes(src: str) -> tuple[bytes, str]:
    """Prefer original-size Twitter images, with fallbacks to smaller or mirrored."""
    parts = urlsplit(src)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    candidates = []
    if "name" in query:
        for size in ("orig", "large"):
            adjusted = dict(query, name=size)
            candidates.append(urlunsplit(parts._replace(query=urlencode(adjusted))))
    candidates.append(src)
    # One direct CDN request per size; no proxying of unrelated third-party URLs.
    for candidate in dict.fromkeys(candidates):
        try:
            resp = SESSION.get(candidate, timeout=25)
            resp.raise_for_status()
            if not resp.headers.get("content-type", "").lower().startswith("image/"):
                raise ValueError("not an image")
            if not resp.content or len(resp.content) > 10 * 1024 * 1024:
                raise ValueError("image empty or exceeds Telegram photo limit")
            content_type = resp.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            return resp.content, content_type
        except (requests.RequestException, ValueError) as exc:
            print(f"  Photo download failed ({candidate}): {exc}")
    raise RuntimeError(f"Cannot download photo: {src}")


def telegram(method: str, token: str, fields: dict, files=None) -> None:
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=fields,
            files=files,
            timeout=70,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram returned ok=false"))
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Telegram {method} failed: {exc}") from exc


def send_tweet(tweet: Tweet, token: str, chat_id: str) -> None:
    # Japanese text, emoji and hashtags remain unchanged, ready to copy for translation.
    text = (tweet.text + "\n\n" if tweet.text else "") + "🔗 " + tweet.url
    photo_files = []
    for index, url in enumerate(tweet.image_urls):
        binary, mime = get_photo_bytes(url)
        ext = "png" if mime == "image/png" else "jpg"
        photo_files.append((f"file{index}", f"cloudten_{tweet.id}_{index+1}.{ext}", binary, mime))

    if not photo_files:
        telegram("sendMessage", token, {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": "true",
        })
        return

    caption = text if len(text) <= 1024 else ""
    if not caption:
        telegram("sendMessage", token, {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": "true",
        })
    if len(photo_files) == 1:
        name, filename, data, mime = photo_files[0]
        telegram("sendPhoto", token,
                 {"chat_id": chat_id, "caption": caption},
                 {"photo": (filename, io.BytesIO(data), mime)})
    else:
        # Telegram accepts albums of 2-10 images.
        for offset in range(0, len(photo_files), 10):
            batch = photo_files[offset:offset+10]
            media = []
            file_args = {}
            for index, (_name, filename, data, mime) in enumerate(batch):
                attachment = f"f{offset+index}"
                item = {"type": "photo", "media": f"attach://{attachment}"}
                if offset == 0 and index == 0 and caption:
                    item["caption"] = caption
                media.append(item)
                file_args[attachment] = (filename, io.BytesIO(data), mime)
            if len(batch) == 1:
                # Extremely rare case: more than 10 photos split at 10+1.
                name = next(iter(file_args))
                telegram("sendPhoto", token, {"chat_id": chat_id},
                         {"photo": file_args[name]})
            else:
                telegram("sendMediaGroup", token,
                         {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                         file_args)
    print(f"Sent tweet {tweet.id}: {len(photo_files)} photo(s)")


def load_state() -> tuple[set[str], bool]:
    if not STATE_PATH.exists():
        return set(), True
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(str(v) for v in raw.get("seen_ids", [])), False
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Cannot read state.json; not sending to avoid duplicates: {exc}")


def save_state(seen: set[str]) -> None:
    # Latest 500 IDs are plenty for a feed of 20 recent posts and one account.
    latest = sorted(seen, key=int, reverse=True)[:500]
    STATE_PATH.write_text(
        json.dumps({"seen_ids": latest}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Add BOT_TOKEN and CHAT_ID under GitHub Actions secrets first")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", HANDLE):
        raise RuntimeError("Invalid X_HANDLE")

    feed, mirror = download_feed()
    posts = parse_feed(feed, mirror)
    if not posts:
        raise RuntimeError("RSS was received but no posts had readable X IDs")
    print(f"Found {len(posts)} post(s) in RSS")
    seen, first_run = load_state()

    if os.getenv("TEST_MODE", "").lower() == "true":
        test = next((post for post in reversed(posts) if post.image_urls), posts[-1])
        print(f"Test mode: sending {test.id}; image count={len(test.image_urls)}")
        send_tweet(test, token, chat_id)
        return  # Never change saved IDs in test mode.

    if first_run:
        # Send only one initial tweet to prove everything works; don't flood old posts.
        latest = posts[-1]
        print(f"First run: sending latest tweet {latest.id} and marking feed as seen")
        send_tweet(latest, token, chat_id)
        save_state({post.id for post in posts})
        return

    fresh = [post for post in posts if post.id not in seen]
    print(f"New posts: {len(fresh)}")
    for post in fresh:
        send_tweet(post, token, chat_id)
        seen.add(post.id)
        # Persist after each successfully sent post. Workflow commits even on failure.
        save_state(seen)
    if not fresh:
        print("Nothing new: Telegram message not sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
