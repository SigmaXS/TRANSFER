"""Когда поездка и до какого момента заявка актуальна (время Молдовы)."""
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Chisinau")

NOW_HOURS = 2       # «сейчас», «срочно», «на ближайшее время»
DEFAULT_HOURS = 4   # время не указано
AFTER_TRIP = 1      # сколько ещё держать заявку после указанного времени отъезда


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, dtime(23, 59), TZ)


def trip_window(when: str | None, date_str: str | None, time_str: str | None,
                posted: datetime) -> tuple[float, float, bool]:
    """(время поездки, до какого момента показывать, известно ли точное время) — в unix-секундах.

    «на 19 часов», написанное в 15:00, будет видно до 20:00. Написанное вчера «завтра в 7:00» —
    сегодня до 8:00. Без времени и даты заявка живёт 4 часа, «сейчас» — 2 часа.
    """
    posted = posted.astimezone(TZ)
    day, explicit_day = None, False
    if date_str:
        try:
            d, m = (int(x) for x in date_str.split("."))
            cand = date(posted.year, m, d)
            if cand < posted.date() - timedelta(days=7):  # «05.01», написанное в декабре
                cand = date(posted.year + 1, m, d)
            day, explicit_day = cand, True
        except ValueError:
            pass
    if day is None:
        offset = {"завтра": 1, "послезавтра": 2}.get(when or "", 0)
        day = posted.date() + timedelta(days=offset)
        explicit_day = offset > 0 or when == "сегодня"

    if time_str:
        h, mi = (int(x) for x in time_str.split(":"))
        trip = datetime.combine(day, dtime(h, mi), TZ)
        if not explicit_day and trip < posted - timedelta(hours=2):
            trip += timedelta(days=1)  # в 23:00 пишут «в 6:00» — это про утро
        return trip.timestamp(), (trip + timedelta(hours=AFTER_TRIP)).timestamp(), True

    if explicit_day:
        start = max(posted, datetime.combine(day, dtime(0, 0), TZ))
        return start.timestamp(), _end_of_day(day).timestamp(), False

    hours = NOW_HOURS if when in ("сейчас", "срочно") else DEFAULT_HOURS
    return posted.timestamp(), (posted + timedelta(hours=hours)).timestamp(), False


def trip_label(trip_at: float, has_time: bool, when: str | None, now: datetime | None = None) -> str:
    """«сегодня 19:00», «завтра 7:30», «05.10», «сейчас», «время не указано»."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    trip = datetime.fromtimestamp(trip_at, TZ)
    delta = (trip.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(delta, trip.strftime("%d.%m"))
    if has_time:
        return f"{day} {trip:%H:%M}"
    if when in ("сейчас", "срочно"):
        return "сейчас"
    if delta != 0 or when == "сегодня":
        return day
    return "время не указано"


def ago(ts: float, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    minutes = int((now.timestamp() - ts) // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    return f"{hours} ч назад" if hours < 24 else f"{hours // 24} дн назад"
