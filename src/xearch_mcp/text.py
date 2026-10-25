"""Small text and HTML helpers."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")
STATUS_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/"
    r"([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{1,25})",
    re.IGNORECASE,
)
BARE_TWEET_ID_RE = re.compile(r"^\d{1,25}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def clean_inline(value: str | None) -> str:
    """Collapse HTML whitespace for a one-line snippet."""
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_multiline(value: str | None) -> str:
    """Trim a block of text without destroying intentional newlines."""
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def strip_html(fragment: str | None) -> str:
    if not fragment:
        return ""
    return clean_inline(BeautifulSoup(fragment, "html.parser").get_text(" "))


def normalize_x_url(url: str) -> str:
    """Rewrite twitter.com links to x.com (the canonical host)."""
    parsed = urlparse(url)
    if parsed.netloc.lower() in {"twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        return parsed._replace(netloc="x.com").geturl()
    return url


def extract_tco_urls(text: str) -> list[str]:
    """Return unique t.co links in first-appearance order."""
    seen: set[str] = set()
    result: list[str] = []
    for match in TCO_RE.finditer(text or ""):
        url = match.group(0)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def replace_tco_urls(text: str, mapping: dict[str, str]) -> str:
    """Replace t.co links using the supplied mapping."""
    if not text or not mapping:
        return text
    # Longest first avoids a short link prefix swallowing a longer one.
    for short in sorted(mapping, key=len, reverse=True):
        text = text.replace(short, mapping[short])
    return text


def _meta_content(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def parse_profile_html(page_html: str, username: str) -> dict[str, Any]:
    """Extract public profile metadata from an x.com profile page."""
    soup = BeautifulSoup(page_html, "html.parser")
    title = _meta_content(soup, "og:title", "twitter:title")
    description = _meta_content(soup, "og:description", "description")
    avatar = _meta_content(soup, "og:image", "twitter:image")
    banner = _meta_content(soup, "twitter:image")
    first_name = _meta_content(soup, "profile:first_name")
    last_name = _meta_content(soup, "profile:last_name")
    meta_username = _meta_content(soup, "profile:username")

    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not name and title:
        name = re.sub(r"\s*\(@[A-Za-z0-9_]+\)\s*on X\s*$", "", title).strip()

    if description and description.lower().startswith("the latest posts from"):
        description = None
    if avatar:
        avatar = re.sub(
            r"_(normal|200x200|400x400)(\.(?:jpg|jpeg|png|webp))$",
            r"_400x400\2",
            avatar,
        )

    if not name and not description:
        raise ValueError("Profile page did not contain public metadata")

    return {
        "username": meta_username or username,
        "name": name or None,
        "bio": description or None,
        "avatar_url": avatar or None,
        "banner_url": banner or None,
        "url": f"https://x.com/{meta_username or username}",
        "source": "x.com profile meta tags",
        "note": "Public profile meta only. Follower and following counts are not exposed by this page.",
    }


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
