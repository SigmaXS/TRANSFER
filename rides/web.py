"""Объявления с сайтов. Сейчас: makler.md → «Пассажирские перевозки».

На makler.md почти только перевозчики и такси, поэтому их объявления идут в ленту
«Свободные машины» для пассажиров (с пометкой «перевозчик») и НЕ рассылаются уведомлениями —
перевозчики «поднимают» объявления каждый час, это был бы спам.
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta

import aiohttp

from .db import RidesDB
from .parsing import DRIVER, parse_message
from .places import resolve_place
from .timeparse import TZ

log = logging.getLogger("rides.web")

MAKLER_URL = "https://makler.md/ru/transport/transportation/passenger-transportation"
MAKLER_BASE = "https://makler.md"
SITE_POST_HOURS = 12          # сколько показывать объявление после последнего «поднятия»
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ru,ro;q=0.9",
}

_ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.S)
_ID_RE = re.compile(r"/an/(\d+)")
_HREF_RE = re.compile(r'<a href="([^"]*/an/\d+[^"]*)"\s+class="ls-detail_anUrl"')
_TITLE_RE = re.compile(r'class="ls-detail_anUrl"[^>]*>\s*<span>(.*?)</span>', re.S)
_TEXT_RE = re.compile(r'<div class="subfir">(.*?)</div>', re.S)
_DATE_RE = re.compile(r'ls-detail_timeDate">\s*([^<]+?)\s*<')
_CLOCK_RE = re.compile(r'ls-detail_timeClock">\s*(\d{1,2}):(\d{2})\s*<')
_CITY_RE = re.compile(r'ls-jobMeta_chip--city">\s*([^<]+?)\s*<')


def _clean(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _posted(date_word: str | None, clock: tuple[int, int] | None, now: datetime) -> datetime | None:
    if not clock:
        return None
    word = (date_word or "").strip().lower()
    if word.startswith("сегодня") or word.startswith("azi"):
        day = now.date()
    elif word.startswith("вчера") or word.startswith("ieri"):
        day = now.date() - timedelta(days=1)
    else:
        return None  # старше вчерашнего дня — не берём
    return datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=TZ)


def parse_makler(page: str, now: datetime | None = None) -> list[dict]:
    """HTML страницы списка → объявления (dict в формате ride_posts)."""
    now = now or datetime.now(TZ)
    posts, seen = [], set()
    for block in _ARTICLE_RE.findall(page):
        href = _HREF_RE.search(block)
        ad_id = _ID_RE.search(href.group(1)) if href else None
        if not ad_id or ad_id.group(1) in seen:
            continue  # ТОП-объявления повторяются в общем списке
        seen.add(ad_id.group(1))
        clock = _CLOCK_RE.search(block)
        date_word = _DATE_RE.search(block)
        posted = _posted(date_word.group(1) if date_word else None,
                         (int(clock.group(1)), int(clock.group(2))) if clock else None, now)
        if not posted or posted < now - timedelta(hours=SITE_POST_HOURS):
            continue
        title = _clean(_TITLE_RE.search(block).group(1)) if _TITLE_RE.search(block) else ""
        body = _clean(_TEXT_RE.search(block).group(1)) if _TEXT_RE.search(block) else ""
        city = _CITY_RE.search(block)
        city_place = resolve_place(city.group(1)) if city else None
        p = parse_message(f"{title}\n{body}")
        places = list(p.places)
        if city_place and city_place not in places:
            places.insert(0, city_place)
        if not places:
            continue
        # Маршрут берём из заголовка («Такси Тирасполь-Кишинёв»), иначе — город перевозчика
        t = parse_message(title)
        if t.from_place and t.to_place:
            from_place, to_place = t.from_place, t.to_place
        else:
            from_place, to_place = city_place or places[0], None
        text = title + ("\n" + body[:400] if body and body[:60] not in title else "")
        posts.append({
            "ts": posted.timestamp(), "source": "site", "chat": "makler.md",
            "link": MAKLER_BASE + href.group(1).split("?")[0],
            "kind": DRIVER, "from_place": from_place, "to_place": to_place, "places": places,
            "trip_at": posted.timestamp(),
            "expires_at": (posted + timedelta(hours=SITE_POST_HOURS)).timestamp(),
            "has_time": False, "when_label": None, "people": None, "seats": None, "is_ad": True,
            "text": text, "comment": None, "author_id": None, "author_username": None,
            "author_name": None, "phone": None,
            "fingerprint": f"makler:{ad_id.group(1)}",
        })
    return posts


async def poll_sites(db: RidesDB):
    """Раз в RIDES_WEB_INTERVAL секунд (по умолчанию 10 мин) забираем свежие объявления."""
    sites = {s.strip() for s in os.environ.get("RIDES_WEB", "makler").split(",") if s.strip()}
    if "makler" not in sites:
        log.info("Сайты выключены (RIDES_WEB)")
        return
    interval = int(os.environ.get("RIDES_WEB_INTERVAL", "600"))
    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as http:
        while True:
            try:
                async with http.get(MAKLER_URL) as resp:
                    page = await resp.text()
                posts = parse_makler(page)
                new = 0
                for post in posts:
                    _, is_new = await db.upsert_site_post(post)
                    new += is_new
                log.info("makler.md: %d свежих объявлений, новых %d", len(posts), new)
            except Exception as e:  # noqa: BLE001
                log.warning("makler.md: %s", e)
            await asyncio.sleep(interval)
