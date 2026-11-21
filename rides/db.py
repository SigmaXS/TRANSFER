"""PostgreSQL: пользователи (водители и пассажиры), фильтры, заявки, антидубли, статистика."""
import os
import json
import time
from dataclasses import dataclass

import asyncpg

from .filters import Filter

DAY = 86400
# Сколько дней хранить прошедшие объявления: пассажиру показываем и недавние машины по маршруту,
# если актуальных мало («этот водитель ездил тут вчера — позвоните»)
KEEP_DAYS = int(os.environ.get("RIDES_KEEP_DAYS", "30"))
DRIVER_ROLE, PASSENGER_ROLE = "driver", "passenger"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ride_users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    active BOOLEAN DEFAULT TRUE,
    mode TEXT DEFAULT 'passenger',
    trial_until DOUBLE PRECISION DEFAULT 0,
    paid_until DOUBLE PRECISION DEFAULT 0,
    created_at DOUBLE PRECISION
);
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS role TEXT;

CREATE TABLE IF NOT EXISTS ride_filters (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES ride_users(user_id) ON DELETE CASCADE,
    kind TEXT,
    from_place TEXT,
    to_place TEXT,
    both_ways BOOLEAN DEFAULT TRUE
);
-- want: какие объявления присылать по этому фильтру.
-- 'passenger' — заявки пассажиров (фильтр водителя), 'driver' — свободные машины (фильтр пассажира)
ALTER TABLE ride_filters ADD COLUMN IF NOT EXISTS want TEXT DEFAULT 'passenger';
-- Mini App: маршрут можно выключить, не удаляя; тихие часы (23–07 без звука)
ALTER TABLE ride_filters ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE;
-- В какое время поездки присылать по маршруту: «после работы» 17:00–20:00 и т.п. (NULL — любое время)
ALTER TABLE ride_filters ADD COLUMN IF NOT EXISTS time_from TEXT;
ALTER TABLE ride_filters ADD COLUMN IF NOT EXISTS time_to TEXT;
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS quiet BOOLEAN DEFAULT FALSE;
-- Тихие часы со своим временем и данные профиля (имя, телефон, где писать, машина…) для готовых заявок
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS quiet_from TEXT DEFAULT '23:00';
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS quiet_to TEXT DEFAULT '07:00';
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS profile JSONB;
ALTER TABLE ride_users ADD COLUMN IF NOT EXISTS profile_at DOUBLE PRECISION;

-- Что делают люди в боте и Mini App: открыл, искал, разместил, позвонил… — для админ-панели
CREATE TABLE IF NOT EXISTS ride_events (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION,
    user_id BIGINT,
    type TEXT,
    data JSONB
);
CREATE INDEX IF NOT EXISTS ride_events_ts ON ride_events (ts);

-- Кто пользовался ботом до появления ролей и уже настроил фильтры — водитель
UPDATE ride_users SET role = 'driver'
 WHERE role IS NULL AND user_id IN (SELECT user_id FROM ride_filters);

CREATE TABLE IF NOT EXISTS ride_seen (
    fingerprint TEXT PRIMARY KEY,
    ts DOUBLE PRECISION
);

-- Объявления: из групп и созданные в боте. Живут, пока актуальны (expires_at)
CREATE TABLE IF NOT EXISTS ride_posts (
    id SERIAL PRIMARY KEY,
    ts DOUBLE PRECISION,
    source TEXT,                 -- 'group' | 'bot' | 'site'
    chat TEXT, link TEXT,
    kind TEXT,                   -- 'passenger' ищет машину | 'driver' есть места
    from_place TEXT, to_place TEXT, places TEXT[],
    trip_at DOUBLE PRECISION, expires_at DOUBLE PRECISION, has_time BOOLEAN, when_label TEXT,
    people INT, seats INT, is_ad BOOLEAN DEFAULT FALSE,
    text TEXT, comment TEXT,
    author_id BIGINT, author_username TEXT, author_name TEXT, phone TEXT,
    fingerprint TEXT,
    active BOOLEAN DEFAULT TRUE
);
CREATE UNIQUE INDEX IF NOT EXISTS ride_posts_fp ON ride_posts (fingerprint);
-- Исправлено админом в Mini App: повторный разбор (reparse_live) его не трогает
ALTER TABLE ride_posts ADD COLUMN IF NOT EXISTS edited BOOLEAN DEFAULT FALSE;

-- Общий чат в Mini App (пожелания, вопросы), по дням
CREATE TABLE IF NOT EXISTS ride_chat (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION,
    user_id BIGINT,
    name TEXT,
    text TEXT,
    deleted BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ride_chat_ts ON ride_chat (ts);

-- Журнал правок админа: что бот разобрал неверно (для улучшения разбора)
CREATE TABLE IF NOT EXISTS ride_fixes (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION,
    admin_id BIGINT,
    post_id INT,
    action TEXT,                 -- 'edit' | 'delete'
    reason TEXT,
    text TEXT,                   -- исходный текст объявления
    before JSONB,
    after JSONB
);
CREATE INDEX IF NOT EXISTS ride_posts_live ON ride_posts (kind, expires_at);

-- Группы, добавленные админом из бота (в дополнение к sources.txt и RIDES_SOURCES)
CREATE TABLE IF NOT EXISTS ride_sources (
    id SERIAL PRIMARY KEY,
    ref TEXT UNIQUE,             -- @username, ссылка-приглашение или числовой id
    title TEXT,
    added_at DOUBLE PRECISION
);

-- Только метаданные заявок из групп: для подсчёта спроса (/stats)
CREATE TABLE IF NOT EXISTS ride_requests (
    id SERIAL PRIMARY KEY,
    ts DOUBLE PRECISION, chat TEXT, kind TEXT, is_ad BOOLEAN,
    from_place TEXT, to_place TEXT, duplicate BOOLEAN, delivered INT DEFAULT 0
);
"""

POST_FIELDS = ("ts", "source", "chat", "link", "kind", "from_place", "to_place", "places",
               "trip_at", "expires_at", "has_time", "when_label", "people", "seats", "is_ad",
               "text", "comment", "author_id", "author_username", "author_name", "phone",
               "fingerprint")


@dataclass
class RideUser:
    user_id: int
    active: bool
    role: str | None     # 'driver' | 'passenger' | None — ещё не выбрал
    trial_until: float
    paid_until: float

    def has_access(self, now: float | None = None) -> bool:
        return max(self.trial_until, self.paid_until) > (now or time.time())

    def access_until(self) -> float:
        return max(self.trial_until, self.paid_until)


def want_for(role: str | None) -> str:
    """Водитель получает заявки пассажиров, пассажир — свободные машины."""
    return "driver" if role == PASSENGER_ROLE else "passenger"


def _user(r) -> RideUser:
    return RideUser(r["user_id"], r["active"], r["role"], r["trial_until"], r["paid_until"])


def _filter(r) -> Filter:
    keys = r.keys() if hasattr(r, "keys") else r
    return Filter(r["kind"], r["from_place"], r["to_place"], r["both_ways"], r["id"],
                  r["time_from"] if "time_from" in keys else None, r["time_to"] if "time_to" in keys else None)


class RidesDB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def init(self):
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    # --- пользователи ---
    async def get_user(self, user_id: int) -> RideUser | None:
        r = await self.pool.fetchrow("SELECT * FROM ride_users WHERE user_id=$1", user_id)
        return _user(r) if r else None

    async def ensure_user(self, user_id: int, username: str | None, trial_days: int):
        now = time.time()
        r = await self.pool.fetchrow(
            """INSERT INTO ride_users (user_id, username, trial_until, created_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
               RETURNING *, (xmax = 0) AS is_new""",
            user_id, username, now + trial_days * DAY, now)
        return _user(r), r["is_new"]

    async def set_role(self, user_id: int, role: str):
        await self.pool.execute("UPDATE ride_users SET role=$2 WHERE user_id=$1", user_id, role)

    async def set_active(self, user_id: int, active: bool):
        await self.pool.execute("UPDATE ride_users SET active=$2 WHERE user_id=$1", user_id, active)

    async def grant(self, user_id: int, days: int) -> float:
        u = await self.get_user(user_id)
        if not u:
            raise ValueError("Этот пользователь ещё не запускал бота")
        until = max(time.time(), u.paid_until) + days * DAY
        await self.pool.execute("UPDATE ride_users SET paid_until=$2 WHERE user_id=$1", user_id, until)
        return until

    async def recipients(self, want: str):
        """Кому рассылать объявление вида want: активные пользователи с фильтрами этого вида.
        Заявки пассажиров (want='passenger') получают только водители с действующим доступом;
        свободные машины пассажирам — бесплатно."""
        rows = await self.pool.fetch(
            """SELECT f.*, u.active, u.role, u.trial_until, u.paid_until
                 FROM ride_filters f JOIN ride_users u USING (user_id)
                WHERE u.active AND f.want = $1 AND COALESCE(f.enabled, TRUE)
                ORDER BY f.id""", want)  # пока всё бесплатно: доступ водителя не проверяем
        by_user: dict[int, tuple[RideUser, list[Filter]]] = {}
        for r in rows:
            by_user.setdefault(r["user_id"], (_user(r), []))[1].append(_filter(r))
        return list(by_user.values())

    # --- фильтры ---
    async def add_filter(self, user_id: int, f: Filter, want: str):
        await self.pool.execute(
            "INSERT INTO ride_filters (user_id, kind, from_place, to_place, both_ways, want)"
            " VALUES ($1,$2,$3,$4,$5,$6)", user_id, f.kind, f.from_place, f.to_place, f.both_ways, want)

    async def get_filters(self, user_id: int, want: str) -> list[Filter]:
        rows = await self.pool.fetch(
            "SELECT * FROM ride_filters WHERE user_id=$1 AND want=$2 ORDER BY id", user_id, want)
        return [_filter(r) for r in rows]

    async def filter_rows(self, user_id: int) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM ride_filters WHERE user_id=$1 ORDER BY id", user_id)
        return [dict(r) for r in rows]

    async def set_filter_time(self, user_id: int, filter_id: int, frm: str | None, to: str | None):
        await self.pool.execute("UPDATE ride_filters SET time_from=$3, time_to=$4 WHERE id=$1 AND user_id=$2",
                                filter_id, user_id, frm, to)

    async def set_filter_enabled(self, user_id: int, filter_id: int, enabled: bool):
        await self.pool.execute("UPDATE ride_filters SET enabled=$3 WHERE id=$1 AND user_id=$2",
                                filter_id, user_id, enabled)

    async def set_quiet(self, user_id: int, quiet: bool):
        await self.pool.execute("UPDATE ride_users SET quiet=$2 WHERE user_id=$1", user_id, quiet)

    async def quiet_users(self) -> dict[int, tuple[str, str]]:
        """Кто включил тихие часы: user_id → (с, до), например ('23:00', '07:00')."""
        return {r["user_id"]: (r["quiet_from"] or "23:00", r["quiet_to"] or "07:00")
                for r in await self.pool.fetch("SELECT user_id, quiet_from, quiet_to FROM ride_users WHERE quiet")}

    async def set_quiet_hours(self, user_id: int, frm: str, to: str):
        await self.pool.execute("UPDATE ride_users SET quiet_from=$2, quiet_to=$3 WHERE user_id=$1", user_id, frm, to)

    async def set_profile(self, user_id: int, profile: dict):
        await self.pool.execute("UPDATE ride_users SET profile=$2::jsonb, profile_at=$3 WHERE user_id=$1",
                                user_id, json.dumps(profile, ensure_ascii=False), time.time())

    async def registered(self, limit: int = 100) -> list[dict]:
        """Заполнившие профиль (есть телефон): для админ-панели."""
        rows = await self.pool.fetch(
            """SELECT u.user_id, u.username, u.role, u.created_at, u.profile, u.profile_at,
                      (SELECT count(*) FROM ride_posts p WHERE p.author_id = u.user_id AND p.source = 'bot') AS posts
                 FROM ride_users u WHERE u.profile IS NOT NULL AND COALESCE(u.profile->>'phone', '') <> ''
                ORDER BY u.profile_at DESC NULLS LAST LIMIT $1""", limit)
        out = []
        for r in rows:
            d = dict(r)
            d["profile"] = json.loads(d["profile"]) if isinstance(d["profile"], str) else d["profile"]
            out.append(d)
        return out

    async def user_row(self, user_id: int) -> dict | None:
        r = await self.pool.fetchrow("SELECT * FROM ride_users WHERE user_id=$1", user_id)
        return dict(r) if r else None

    async def log_event(self, user_id: int | None, type_: str, data: dict | None = None):
        await self.pool.execute("INSERT INTO ride_events (ts, user_id, type, data) VALUES ($1,$2,$3,$4::jsonb)",
                                time.time(), user_id, type_, json.dumps(data or {}, ensure_ascii=False))

    async def admin_stats(self, day_start: float) -> dict:
        """Цифры для админ-панели. day_start — полночь сегодня по Кишинёву (unix)."""
        now = time.time()
        q = self.pool.fetchval
        users = {
            "total": await q("SELECT count(*) FROM ride_users"),
            "new_today": await q("SELECT count(*) FROM ride_users WHERE created_at >= $1", day_start),
            "today": await q("SELECT count(DISTINCT user_id) FROM ride_events WHERE ts >= $1 AND user_id IS NOT NULL", day_start),
            "week": await q("SELECT count(DISTINCT user_id) FROM ride_events WHERE ts >= $1 AND user_id IS NOT NULL", now - 7 * DAY),
            "month": await q("SELECT count(DISTINCT user_id) FROM ride_events WHERE ts >= $1 AND user_id IS NOT NULL", now - 30 * DAY),
            "with_routes": await q("SELECT count(DISTINCT user_id) FROM ride_filters WHERE COALESCE(enabled, TRUE)"),
        }
        types = {r["type"]: r["n"] for r in await self.pool.fetch(
            "SELECT type, count(*) AS n FROM ride_events WHERE ts >= $1 GROUP BY type", day_start)}
        notified = await q("SELECT COALESCE(sum((data->>'n')::int), 0) FROM ride_events WHERE type='notify' AND ts >= $1",
                           day_start)
        routes = [dict(r) for r in await self.pool.fetch(
            """SELECT data->>'from' AS frm, data->>'to' AS dst, count(*) AS n, count(DISTINCT user_id) AS people,
                      round(avg((data->>'found')::int))::int AS found
                 FROM ride_events WHERE type='search' AND ts >= $1
                  AND (COALESCE(data->>'from','') <> '' OR COALESCE(data->>'to','') <> '')
                GROUP BY 1, 2 ORDER BY n DESC LIMIT 12""", now - 7 * DAY)]
        empty = [dict(r) for r in await self.pool.fetch(
            """SELECT data->>'from' AS frm, data->>'to' AS dst, count(*) AS n
                 FROM ride_events WHERE type='search' AND ts >= $1 AND (data->>'found')::int = 0
                  AND (COALESCE(data->>'from','') <> '' OR COALESCE(data->>'to','') <> '')
                GROUP BY 1, 2 ORDER BY n DESC LIMIT 8""", now - 7 * DAY)]
        daily = [dict(r) for r in await self.pool.fetch(
            """SELECT to_char((to_timestamp(ts) AT TIME ZONE 'Europe/Chisinau')::date, 'DD.MM') AS day,
                      count(DISTINCT user_id) AS users,
                      count(*) FILTER (WHERE type='search') AS searches,
                      count(*) FILTER (WHERE type='publish') AS posts
                 FROM ride_events WHERE ts >= $1 AND user_id IS NOT NULL
                GROUP BY (to_timestamp(ts) AT TIME ZONE 'Europe/Chisinau')::date
                ORDER BY (to_timestamp(ts) AT TIME ZONE 'Europe/Chisinau')::date DESC LIMIT 14""",
            now - 14 * DAY)]
        posts = {r["kind"]: r["n"] for r in await self.pool.fetch(
            "SELECT kind, count(*) AS n FROM ride_posts WHERE active AND expires_at > $1 GROUP BY kind", now)}
        recent = [dict(r) for r in await self.pool.fetch(
            """SELECT e.ts, e.type, e.data, u.username FROM ride_events e LEFT JOIN ride_users u USING (user_id)
                WHERE e.type NOT IN ('notify') ORDER BY e.id DESC LIMIT 25""")]
        for r in recent:
            r["data"] = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        return {"users": users, "types": types, "notified": notified, "routes": routes, "empty": empty,
                "daily": daily, "live_posts": posts, "recent": recent}

    async def get_post(self, post_id: int) -> dict | None:
        r = await self.pool.fetchrow("SELECT * FROM ride_posts WHERE id=$1", post_id)
        return dict(r) if r else None

    async def delete_filter(self, user_id: int, filter_id: int):
        await self.pool.execute("DELETE FROM ride_filters WHERE id=$1 AND user_id=$2", filter_id, user_id)

    # --- объявления ---
    async def save_post(self, post: dict) -> dict | None:
        """Сохранить объявление. None — такое уже есть (дубль)."""
        cols = ", ".join(POST_FIELDS)
        args = ", ".join(f"${i}" for i in range(1, len(POST_FIELDS) + 1))
        r = await self.pool.fetchrow(
            f"INSERT INTO ride_posts ({cols}) VALUES ({args}) ON CONFLICT (fingerprint) DO NOTHING"
            " RETURNING *", *(post.get(k) for k in POST_FIELDS))
        return dict(r) if r else None

    async def upsert_site_post(self, post: dict) -> tuple[dict, bool]:
        """Объявление с сайта: при «поднятии» обновляем время. Возвращает (строка, новое ли)."""
        cols = ", ".join(POST_FIELDS)
        args = ", ".join(f"${i}" for i in range(1, len(POST_FIELDS) + 1))
        r = await self.pool.fetchrow(
            f"INSERT INTO ride_posts ({cols}) VALUES ({args}) ON CONFLICT (fingerprint) DO UPDATE SET"
            " ts=EXCLUDED.ts, trip_at=EXCLUDED.trip_at, expires_at=EXCLUDED.expires_at,"
            " text=EXCLUDED.text, active=TRUE RETURNING *, (xmax = 0) AS is_new",
            *(post.get(k) for k in POST_FIELDS))
        row = dict(r)
        return row, row.pop("is_new")

    # --- источники ---
    async def add_source(self, ref: str, title: str | None):
        await self.pool.execute(
            "INSERT INTO ride_sources (ref, title, added_at) VALUES ($1,$2,$3)"
            " ON CONFLICT (ref) DO UPDATE SET title=EXCLUDED.title", ref, title, time.time())

    async def list_sources(self) -> list[dict]:
        return [dict(r) for r in await self.pool.fetch("SELECT * FROM ride_sources ORDER BY id")]

    async def remove_source(self, source_id: int) -> dict | None:
        r = await self.pool.fetchrow("DELETE FROM ride_sources WHERE id=$1 RETURNING *", source_id)
        return dict(r) if r else None

    async def live_posts(self, kind: str, limit: int = 300) -> list[dict]:
        """Актуальные объявления: сначала ближайшие по времени поездки."""
        rows = await self.pool.fetch(
            "SELECT * FROM ride_posts WHERE active AND kind=$1 AND expires_at > $2"
            " ORDER BY is_ad, trip_at, ts DESC LIMIT $3", kind, time.time(), limit)
        return [dict(r) for r in rows]

    async def my_posts(self, user_id: int) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM ride_posts WHERE active AND source='bot' AND author_id=$1"
            " AND expires_at > $2 ORDER BY trip_at", user_id, time.time())
        return [dict(r) for r in rows]

    async def close_post(self, user_id: int, post_id: int):
        await self.pool.execute("UPDATE ride_posts SET active=FALSE WHERE id=$1 AND author_id=$2",
                                post_id, user_id)

    # --- правки админа ---
    FIX_KEYS = ("kind", "from_place", "to_place", "trip_at", "expires_at", "has_time", "when_label",
                "people", "seats", "phone")

    async def admin_edit_post(self, admin_id: int, post_id: int, changes: dict, reason: str | None) -> dict | None:
        """Исправить объявление (поля из FIX_KEYS + places) и записать в журнал, как было и как стало."""
        old = await self.get_post(post_id)
        if not old:
            return None
        changes = {k: v for k, v in changes.items() if k in self.FIX_KEYS or k == "places"}
        sets = ", ".join(f"{k}=${i}" for i, k in enumerate(changes, start=2))
        r = await self.pool.fetchrow(f"UPDATE ride_posts SET {sets}, edited=TRUE, active=TRUE WHERE id=$1 RETURNING *",
                                     post_id, *changes.values())
        await self._log_fix(admin_id, old, "edit", reason, dict(r))
        return dict(r)

    async def admin_delete_post(self, admin_id: int, post_id: int, reason: str | None) -> bool:
        old = await self.get_post(post_id)
        if not old:
            return False
        await self.pool.execute("UPDATE ride_posts SET active=FALSE WHERE id=$1", post_id)
        await self._log_fix(admin_id, old, "delete", reason, None)
        return True

    async def _log_fix(self, admin_id: int, old: dict, action: str, reason: str | None, new: dict | None):
        pick = lambda d: {k: d.get(k) for k in self.FIX_KEYS} if d else None  # noqa: E731
        await self.pool.execute(
            "INSERT INTO ride_fixes (ts, admin_id, post_id, action, reason, text, before, after)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)",
            time.time(), admin_id, old["id"], action, reason, old.get("text") or old.get("comment"),
            json.dumps(pick(old), ensure_ascii=False), json.dumps(pick(new), ensure_ascii=False) if new else None)

    async def fixes(self, days: int = 30) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM ride_fixes WHERE ts > $1 ORDER BY ts",
                                     time.time() - days * DAY)
        return [dict(r) for r in rows]

    # --- чат ---
    async def chat_messages(self, days: int = 7, limit: int = 300) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, ts, user_id, name, text FROM ride_chat WHERE NOT deleted AND ts > $1"
            " ORDER BY ts DESC LIMIT $2", time.time() - days * DAY, limit)
        return [dict(r) for r in reversed(rows)]

    async def chat_add(self, user_id: int, name: str, text: str) -> dict:
        r = await self.pool.fetchrow("INSERT INTO ride_chat (ts, user_id, name, text) VALUES ($1,$2,$3,$4)"
                                     " RETURNING id, ts, user_id, name, text", time.time(), user_id, name, text)
        return dict(r)

    async def chat_last_ts(self, user_id: int) -> float:
        return await self.pool.fetchval("SELECT COALESCE(MAX(ts), 0) FROM ride_chat WHERE user_id=$1", user_id)

    async def chat_delete(self, msg_id: int, user_id: int | None = None):
        """user_id=None — админ удаляет любое сообщение, иначе только своё."""
        if user_id is None:
            await self.pool.execute("UPDATE ride_chat SET deleted=TRUE WHERE id=$1", msg_id)
        else:
            await self.pool.execute("UPDATE ride_chat SET deleted=TRUE WHERE id=$1 AND user_id=$2", msg_id, user_id)

    async def recent_posts(self, kind: str, days: int = KEEP_DAYS, limit: int = 1000) -> list[dict]:
        """Уже прошедшие объявления за последние дни: сначала самые свежие."""
        now = time.time()
        rows = await self.pool.fetch(
            "SELECT * FROM ride_posts WHERE active AND kind=$1 AND expires_at <= $2 AND ts > $3"
            " ORDER BY ts DESC LIMIT $4", kind, now, now - days * DAY, limit)
        return [dict(r) for r in rows]

    async def reparse_live(self) -> int:
        """После правки разбора: заново определить «ищет машину / свободная машина» у актуальных объявлений из групп."""
        from datetime import datetime
        from .parsing import parse_message
        from .timeparse import TZ, trip_window
        rows = await self.pool.fetch(
            "SELECT id, ts, text, kind, people, seats, trip_at, has_time, when_label FROM ride_posts"
            " WHERE active AND source='group' AND expires_at > $1 AND text IS NOT NULL"
            " AND NOT COALESCE(edited, FALSE)", time.time())
        changed = 0
        for r in rows:
            p = parse_message(r["text"])
            kind = p.kind or r["kind"]
            # Время и дата тоже заново: «выезд 9.00», «17.00-17.30», «в течении часа»
            trip_at, expires_at, has_time = trip_window(p.when, p.date, p.time, datetime.fromtimestamp(r["ts"], TZ), p.in_min)
            if (kind, p.people, p.seats, has_time, p.when) != (r["kind"], r["people"], r["seats"], r["has_time"], r["when_label"]) \
                    or abs((r["trip_at"] or 0) - trip_at) > 60:
                await self.pool.execute(
                    "UPDATE ride_posts SET kind=$2, people=$3, seats=$4, trip_at=$5, expires_at=$6, has_time=$7, when_label=$8"
                    " WHERE id=$1", r["id"], kind, p.people, p.seats, trip_at, expires_at, has_time, p.when)
                changed += 1
        return changed

    async def cleanup(self):
        await self.pool.execute("DELETE FROM ride_posts WHERE expires_at < $1",
                                time.time() - max(KEEP_DAYS, 1) * DAY)
        # Старые сообщения из истории групп: дата поездки за неделю до сообщения («Еду сегодня 02.03»,
        # прочитанное в октябре) или через 3+ месяца после (раньше «02.03» в октябре считали мартом
        # следующего года) — не актуальны, удаляем.
        await self.pool.execute("DELETE FROM ride_posts WHERE trip_at < ts - $1 OR trip_at > ts + $2",
                                7 * DAY, 90 * DAY)

    async def reset_viber(self) -> int:
        """Удалить все объявления из Viber (например, после неверно загруженной истории)
        и сбросить антидубли, чтобы историю можно было загрузить заново."""
        res = await self.pool.execute("DELETE FROM ride_posts WHERE chat LIKE 'Viber ·%'")
        await self.pool.execute("DELETE FROM ride_seen")
        return int(res.split()[-1]) if res else 0

    # --- антидубли ---
    async def seen_recently(self, fingerprint: str, window_hours: int = 12, ts: float | None = None) -> bool:
        now = ts or time.time()
        async with self.pool.acquire() as c, c.transaction():
            prev = await c.fetchval("SELECT ts FROM ride_seen WHERE fingerprint=$1 FOR UPDATE", fingerprint)
            if prev is not None and abs(now - prev) < window_hours * 3600:
                return True
            await c.execute("INSERT INTO ride_seen VALUES ($1,$2) ON CONFLICT (fingerprint)"
                            " DO UPDATE SET ts=EXCLUDED.ts", fingerprint, now)
            await c.execute("DELETE FROM ride_seen WHERE ts < $1", time.time() - 2 * DAY)
        return False

    # --- статистика ---
    async def log_request(self, chat: str, p, duplicate: bool) -> int:
        return await self.pool.fetchval(
            "INSERT INTO ride_requests (ts, chat, kind, is_ad, from_place, to_place, duplicate)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            time.time(), chat, p.kind, p.is_ad, p.from_place, p.to_place, duplicate)

    async def set_delivered(self, request_id: int, count: int):
        await self.pool.execute("UPDATE ride_requests SET delivered=$2 WHERE id=$1", request_id, count)

    async def stats(self, days: int = 7) -> dict:
        since = time.time() - days * DAY
        f = self.pool.fetch
        by_kind = {r["kind"]: r["c"] for r in await f(
            "SELECT kind, COUNT(*) c FROM ride_requests WHERE ts>$1 AND NOT duplicate AND NOT is_ad"
            " GROUP BY kind", since)}
        by_chat = await f(
            "SELECT chat, COUNT(*) c FROM ride_requests WHERE ts>$1 AND NOT duplicate"
            " AND kind='passenger' GROUP BY chat ORDER BY c DESC LIMIT 10", since)
        routes = await f(
            "SELECT from_place, to_place, COUNT(*) c FROM ride_requests WHERE ts>$1 AND NOT duplicate"
            " AND kind='passenger' AND from_place IS NOT NULL AND to_place IS NOT NULL"
            " GROUP BY from_place, to_place ORDER BY c DESC LIMIT 10", since)
        dups = await self.pool.fetchval(
            "SELECT COUNT(*) FROM ride_requests WHERE ts>$1 AND duplicate", since)
        ads = await self.pool.fetchval("SELECT COUNT(*) FROM ride_requests WHERE ts>$1 AND is_ad", since)
        roles = {r["role"]: r["c"] for r in await f(
            "SELECT COALESCE(role, '?') role, COUNT(*) c FROM ride_users GROUP BY 1")}
        bot_posts = {r["kind"]: r["c"] for r in await f(
            "SELECT kind, COUNT(*) c FROM ride_posts WHERE source='bot' AND ts>$1 GROUP BY kind", since)}
        live = await self.pool.fetchval("SELECT COUNT(*) FROM ride_posts WHERE active AND expires_at>$1",
                                         time.time())
        return {"by_kind": by_kind, "by_chat": by_chat, "routes": routes, "dups": dups, "ads": ads,
                "roles": roles, "bot_posts": bot_posts, "live": live}
