"""Разбор сообщения из группы: кто пишет (пассажир / водитель), откуда, куда, когда."""
import hashlib
import re
from dataclasses import dataclass, field

from .places import find_places, normalize, region_of

PASSENGER = "passenger"   # ищет машину / попутку
DRIVER = "driver"         # едет и предлагает места
UNKNOWN = "unknown"

# Ключевые фразы (в нормализованном виде: без ё/й и диакритики)
_PASSENGER_KW = [
    "ищу машин", "ищу водител", "ищу попутк", "ищу транспорт", "ищу такси",
    "нужна машин", "нужно такси", "нужен водител", "нужна попутк", "нужен транспорт",
    "кто едет", "кто поедет", "кто будет ехать", "кто-то едет", "кто то едет",
    "кто нибудь едет", "кто-нибудь едет", "кто едет?", "есть кто едет",
    "едет кто", "едет ли кто", "поедет кто", "будет кто ехать", "кто едит", "кто поедит",
    "кто нибудь поедет", "кто-нибудь поедет", "есть кто в ", "есть кто из ", "есть машина",
    "cine merge", "merge cineva", "pleaca cineva",
    "подвезите", "подбросьте", "подвезет", "подкинете", "подкиньте",
    "возьмите", "заберите", "нужно уехать", "надо уехать", "нужно доехать",
    "надо доехать", "нужно добраться", "надо добраться", "хочу уехать",
    "caut masin", "caut transport", "caut loc", "caut sofer", "caut o masin",
    "cine merge", "cine pleaca", "cine pleca", "am nevoie de transport",
    "am nevoie de masin", "ma poate lua", "ma ia cineva",
]
_DRIVER_KW = [
    "есть места", "есть место", "есть свободн", "свободные места", "свободное место",
    "свободных мест", "возьму", "возьмем", "возьмём", "ищу пассажир", "ищу попутчик",
    "выезжаю", "выезд в", "отправление", "еду из", "еду в ", "едем из", "поеду из",
    "могу взять", "могу отвезти", "могу подвезти", "могу забрать", "могу сейчас", "могу в ",
    "locuri libere", "loc liber", "iau pasager", "caut pasager", "plec din", "plec spre",
    "plecam", "plecăm", "am locuri", "dispun de",
]
_AD_KW = [
    "ежедневно", "круглосуточно", "24/7", "трансфер", "без выходных", "zilnic",
    "rezervari", "rezervări", "бронирование", "по всем направлениям",
]

_FROM_PREPS = {"из", "с", "со", "от", "din"}
_TO_PREPS = {"в", "во", "на", "до", "к", "ко", "spre", "la", "in", "catre"}

_WHEN_WORDS = {
    "сегодня": "сегодня", "завтра": "завтра", "послезавтра": "послезавтра",
    "сейчас": "сейчас", "срочно": "срочно", "ближайшее время": "сейчас", "как можно скорее": "сейчас",
    "azi": "сегодня", "astazi": "сегодня", "maine": "завтра",
    "poimaine": "послезавтра", "acum": "сейчас", "urgent": "срочно",
}
# Ключевые слова приводим к тому же виду, что и текст (иначе «сейчас» ≠ «сеичас»)
_PASSENGER_KW = [normalize(k) for k in _PASSENGER_KW]
_DRIVER_KW = [normalize(k) for k in _DRIVER_KW]
_AD_KW = [normalize(k) for k in _AD_KW]
_WHEN_WORDS = {normalize(k): v for k, v in _WHEN_WORDS.items()}

_TIME_RE = re.compile(r"(?<![\d.])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.])")
_DATE_RE = re.compile(r"(?<![\d:])([0-3]?\d)[./]([01]?\d)(?:[./](\d{2,4}))?(?![\d:])")
# «в 19 ч», «на 19 часов», «к 7 утра», «в 7 вечера», «la ora 8», «на 19»
_TIME_WORD_RE = re.compile(
    r"(?:\bв|\bво|\bна|\bк|\bla|\bora|\bpe la)\s+(?:ora\s+)?(\d{1,2})(?![\d:.])\s*"
    r"(ч\b|час\w*|утра|вечера|веч\b|дня|ночи|ore\b|ora\b|dimineata|seara)?")
_PHONE_RE = re.compile(r"(?:\+?373|\b0)[\s-]?\d{2}[\s-]?\d{2,3}[\s-]?\d{2,3}[\s-]?\d{0,3}")
_PEOPLE_RE = re.compile(r"(\d)\s*(?:чел|человек|пассаж|pers|persoan|oameni)")
_SEATS_RE = re.compile(r"(\d)\s*(?:мест|места|место|locur|loc\b)")
# «Нужно 1 место», «нужно место», «ищу 2 места», «caut 1 loc» — это пассажир, а не свободные места
_NEED_SEATS_RE = re.compile(
    r"(?:нужн\w*|надо|ищу|требуется|caut|am nevoie de)\s+(?:(\d)\s*)?(?:мест|locur|loc\b)")


@dataclass
class Parsed:
    text: str
    kind: str = UNKNOWN
    from_place: str | None = None
    to_place: str | None = None
    places: list[str] = field(default_factory=list)
    when: str | None = None
    time: str | None = None
    date: str | None = None
    people: int | None = None
    seats: int | None = None
    is_ad: bool = False
    fingerprint: str = ""

    @property
    def regions(self) -> set[str]:
        ends = [p for p in (self.from_place, self.to_place) if p] or self.places
        return {region_of(p) for p in ends if region_of(p)}

    @property
    def has_route(self) -> bool:
        return bool(self.from_place or self.to_place)


def _last_words(norm: str, pos: int, n: int = 2) -> list[str]:
    return re.findall(r"\w+", norm[:pos])[-n:]


def _route(norm: str):
    mentions = find_places(norm, already_normalized=True)
    places, roles = [], []
    for start, _end, name in mentions:
        if places and places[-1] == name:
            continue  # «Кишинёв ... Кишинёв» подряд — одно упоминание
        words = _last_words(norm, start)
        prev = words[-1] if words else ""
        if words[-2:] == ["de", "la"] or prev in _FROM_PREPS:
            role = "from"
        elif prev in _TO_PREPS:
            role = "to"
        else:
            role = None
        places.append(name)
        roles.append(role)

    frm = next((p for p, r in zip(places, roles) if r == "from"), None)
    to = next((p for p, r in zip(places, roles) if r == "to" and p != frm), None)
    free = [p for p, r in zip(places, roles) if r is None]
    if frm is None and to is None:
        if len(places) >= 2:
            frm = places[0]
            to = next((p for p in places[1:] if p != frm), None)
    elif frm is None:
        frm = next((p for p in free if p != to), None)
    elif to is None:
        to = next((p for p in free if p != frm), None)
    uniq = list(dict.fromkeys(places))
    return frm, to, uniq


def _when(norm: str, p: Parsed):
    for word, label in _WHEN_WORDS.items():
        if re.search(r"(?<!\w)" + word + r"(?!\w)", norm):
            p.when = label
            break
    for m in _TIME_RE.finditer(norm):
        sep = norm[m.start(2) - 1]
        before = _last_words(norm, m.start(), 1)
        if sep == ":" or (before and before[0] in {"в", "la", "к", "ora"}):
            p.time = f"{int(m.group(1))}:{m.group(2)}"
            break
    if not p.time:
        for m in _TIME_WORD_RE.finditer(norm):
            hour, suffix = int(m.group(1)), m.group(2) or ""
            after = norm[m.end(1):m.end(1) + 12]
            if not suffix and re.match(r"\s*(?:чел|человек|мест|пассаж|pers|loc|лей|lei|€|\$|евро)", after):
                continue  # «на 2 человека», «в 3 места» — это не время
            if hour > 23:
                continue
            if suffix.startswith(("веч", "seara")) and hour < 12:
                hour += 12
            elif suffix == "дня" and hour < 7:
                hour += 12
            elif suffix == "ночи" and hour == 12:
                hour = 0
            if not suffix and hour < 5 and not re.search(r"\bв\b|\bla\b", norm[max(0, m.start() - 1):m.start() + 3]):
                continue  # «на 2» без уточнения — скорее количество
            p.time = f"{hour}:00"
            break
    for m in _DATE_RE.finditer(norm):
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(0) == "24/7":
            continue
        if p.time and m.group(0).replace(".", ":") == p.time.zfill(5):
            continue
        before = _last_words(norm, m.start(), 1)
        if 1 <= day <= 31 and 1 <= month <= 12 and not (before and before[0] in {"в", "la", "к"}):
            p.date = f"{day:02d}.{month:02d}"
            break


_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
           "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
           "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6, "iulie": 7,
           "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12}
_MONTH_DATE_RE = re.compile(r"(?<!\d)([0-3]?\d)\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яий]|июн\w*|июл\w*|"
                            r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|ianuarie|februarie|martie|"
                            r"aprilie|mai|iunie|iulie|august|septembrie|octombrie|noiembrie|decembrie)\b")


def _month_date(norm: str) -> str | None:
    m = _MONTH_DATE_RE.search(norm)
    if not m:
        return None
    word = m.group(2)
    month = next((v for k, v in _MONTHS.items() if word.startswith(k) and (k != "ма" or len(word) <= 3)), None)
    day = int(m.group(1))
    return f"{day:02d}.{month:02d}" if month and 1 <= day <= 31 else None


def parse_message(text: str) -> Parsed:
    norm = normalize(text)
    p = Parsed(text=text)
    p.fingerprint = hashlib.sha1(re.sub(r"\s+", " ", norm).strip().encode()).hexdigest()
    p.from_place, p.to_place, p.places = _route(norm)
    _when(norm, p)
    if not p.date:
        p.date = _month_date(norm)

    m = _PEOPLE_RE.search(norm)
    if m:
        p.people = int(m.group(1))
    need = _NEED_SEATS_RE.search(norm)
    if need:
        p.people = p.people or int(need.group(1) or 1)
    else:
        m = _SEATS_RE.search(norm)
        if m:
            p.seats = int(m.group(1))

    pass_score = sum(kw in norm for kw in _PASSENGER_KW) + (1 if p.people else 0) + (2 if need else 0)
    drv_score = sum(kw in norm for kw in _DRIVER_KW) + (1 if p.seats else 0)
    ad_score = sum(kw in norm for kw in _AD_KW) + len(_PHONE_RE.findall(norm))
    p.is_ad = ad_score >= 2 or (ad_score >= 1 and len(text) > 300)

    if pass_score > drv_score:
        p.kind = PASSENGER
    elif drv_score > 0 or p.is_ad:
        p.kind = DRIVER
    elif p.has_route and (p.time or p.date or p.when) and _PHONE_RE.search(norm):
        # «Завтра 01.10 из Комрата до Кишинёва в 14:00, 060304870» — так пишут водители:
        # маршрут + время + телефон, без «ищу/нужна машина»
        p.kind = DRIVER
    elif p.has_route and len(text) <= 160 and not _PHONE_RE.search(norm):
        # Короткое «Тирасполь - Кишинёв завтра 7:00» без телефона и без «есть места»:
        # в группах попутчиков так чаще всего пишут пассажиры.
        p.kind = PASSENGER
    return p
