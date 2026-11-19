"""Перевозчики и реклама: в Mini App и в уведомлениях их не показываем — только частные водители и пассажиры.

Признаки перевозчика: объявление с сайта, ссылки (сайт, t.me, Viber-приглашение), несколько разных телефонов,
слова «ежедневно / рейсы / посылки / трансфер…», расписание из нескольких дат, длинная реклама.
"""
import re

_LINK_RE = re.compile(r"https?://|www\.|t\.me/|invite\.viber|viber\.com|\b[\w-]+\.(?:md|com|ru|ua|ro|net|org)\b", re.I)
_PHONE_RE = re.compile(r"(?:\+?373|\+?380|\+?40|\b0)[\s-]?\d{2}[\s-]?\d{2,3}[\s-]?\d{2,3}|\b7[0-9]\d{6}\b")
_DATE_RE = re.compile(r"(?<![\d:])[0-3]?\d\.[01]\d(?![\d:])")
_KW = [
    "ежедневно", "каждый день", "круглосуточно", "24/7", "без выходных", "перевозки", "перевозка пассажир",
    "пассажирские", "трансфер", "микроавтобус", "бус ", "рейс", "посылки", "посылок", "бронир",
    "по всем направлениям", "от дома до дома", "забираем от дома", "даты выезда", "расписание",
    "zilnic", "transport pasageri", "rezerv", "colete", "microbuz",
]


def is_carrier(post: dict) -> bool:
    if post.get("source") == "bot":
        return False                      # объявления из бота и Mini App — всегда частные
    if post.get("is_ad") or post.get("source") == "site":
        return True
    text = post.get("text") or ""
    if not text:
        return False
    if _LINK_RE.search(text):
        return True
    phones = {re.sub(r"\D", "", m)[-8:] for m in _PHONE_RE.findall(text)}
    if len(phones) >= 2:
        return True
    low = text.lower()
    hits = sum(k in low for k in _KW)
    if hits >= 2 or len(_DATE_RE.findall(text)) >= 3:
        return True
    return len(text) > 450 and hits >= 1
