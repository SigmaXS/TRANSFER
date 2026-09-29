"""PostgreSQL: водители, фильтры, антидубли, статистика заявок."""
import time
from dataclasses import dataclass

import asyncpg

from .filters import Filter

DAY = 86400

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
CREATE TABLE IF NOT EXISTS ride_filters (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES ride_users(user_id) ON DELETE CASCADE,
    kind TEXT,
    from_place TEXT,
    to_place TEXT,
    both_ways BOOLEAN DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS ride_seen (
    fingerprint TEXT PRIMARY KEY,
    ts DOUBLE PRECISION
);
-- Только метаданные заявок, без текста и контактов: для подсчёта спроса
CREATE TABLE IF NOT EXISTS ride_requests (
    id SERIAL PRIMARY KEY,
    ts DOUBLE PRECISION, chat TEXT, kind TEXT, is_ad BOOLEAN,
    from_place TEXT, to_place TEXT, duplicate BOOLEAN, delivered INT DEFAULT 0
);
"""


@dataclass
class RideUser:
    user_id: int
    active: bool
    mode: str            # "passenger" — только пассажиры; "all" — ещё и водители с местами
    trial_until: float
    paid_until: float

    def has_access(self, now: float | None = None) -> bool:
        return max(self.trial_until, self.paid_until) > (now or time.time())

    def access_until(self) -> float:
        return max(self.trial_until, self.paid_until)


def _user(r) -> RideUser:
    return RideUser(r["user_id"], r["active"], r["mode"], r["trial_until"], r["paid_until"])


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

    async def set_active(self, user_id: int, active: bool):
        await self.pool.execute("UPDATE ride_users SET active=$2 WHERE user_id=$1", user_id, active)

    async def set_mode(self, user_id: int, mode: str):
        await self.pool.execute("UPDATE ride_users SET mode=$2 WHERE user_id=$1", user_id, mode)

    async def grant(self, user_id: int, days: int) -> float:
        u = await self.get_user(user_id)
        if not u:
            raise ValueError("Этот пользователь ещё не открывал раздел для водителей")
        until = max(time.time(), u.paid_until) + days * DAY
        await self.pool.execute("UPDATE ride_users SET paid_until=$2 WHERE user_id=$1", user_id, until)
        return until

    async def recipients(self):
        """Активные водители с действующим доступом и их фильтры."""
        now = time.time()
        users = await self.pool.fetch(
            "SELECT * FROM ride_users WHERE active AND GREATEST(trial_until, paid_until) > $1", now)
        rows = await self.pool.fetch(
            "SELECT * FROM ride_filters WHERE user_id = ANY($1::bigint[]) ORDER BY id",
            [u["user_id"] for u in users])
        by_user: dict[int, list[Filter]] = {}
        for r in rows:
            by_user.setdefault(r["user_id"], []).append(_filter(r))
        return [(_user(u), by_user[u["user_id"]]) for u in users if u["user_id"] in by_user]

    # --- фильтры ---
    async def add_filter(self, user_id: int, f: Filter):
        await self.pool.execute(
            "INSERT INTO ride_filters (user_id, kind, from_place, to_place, both_ways)"
            " VALUES ($1,$2,$3,$4,$5)", user_id, f.kind, f.from_place, f.to_place, f.both_ways)

    async def get_filters(self, user_id: int) -> list[Filter]:
        rows = await self.pool.fetch("SELECT * FROM ride_filters WHERE user_id=$1 ORDER BY id", user_id)
        return [_filter(r) for r in rows]

    async def delete_filter(self, user_id: int, filter_id: int):
        await self.pool.execute("DELETE FROM ride_filters WHERE id=$1 AND user_id=$2", filter_id, user_id)

    # --- антидубли ---
    async def seen_recently(self, fingerprint: str, window_hours: int = 12) -> bool:
        now = time.time()
        async with self.pool.acquire() as c, c.transaction():
            ts = await c.fetchval("SELECT ts FROM ride_seen WHERE fingerprint=$1 FOR UPDATE", fingerprint)
            if ts is not None and now - ts < window_hours * 3600:
                return True
            await c.execute("INSERT INTO ride_seen VALUES ($1,$2) ON CONFLICT (fingerprint)"
                            " DO UPDATE SET ts=EXCLUDED.ts", fingerprint, now)
            await c.execute("DELETE FROM ride_seen WHERE ts < $1", now - 2 * DAY)
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
        users = await self.pool.fetchval("SELECT COUNT(*) FROM ride_users")
        active = len(await self.recipients())
        return {"by_kind": by_kind, "by_chat": by_chat, "routes": routes, "dups": dups,
                "ads": ads, "users": users, "active": active}


def _filter(r) -> Filter:
    return Filter(r["kind"], r["from_place"], r["to_place"], r["both_ways"], r["id"])
