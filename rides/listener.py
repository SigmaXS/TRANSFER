"""Чтение групп попутчиков через отдельный Telegram-аккаунт (Telethon) в реальном времени.

Аккаунт-читатель подключается по строке сессии из переменной TG_SESSION
(получить её: python make_session.py на своём компьютере).

Группы: rides/sources.txt + переменная RIDES_SOURCES + добавленные админом в боте (/findgroups,
/addsource). Новые группы начинают читаться сразу, без перезапуска.
"""
import asyncio
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat

from .db import RidesDB
from .notifier import Notifier
from .parsing import UNKNOWN, parse_message
from .posts import post_from_group

log = logging.getLogger("rides.listener")
SOURCES_FILE = Path(__file__).with_name("sources.txt")
_BACKGROUND: set = set()  # ссылки на фоновые задачи, чтобы их не собрал сборщик мусора

# Что искать в Telegram по команде /findgroups без аргументов
DEFAULT_QUERIES = [
    "попутчики", "попутка", "попутчики молдова", "попутчики пмр", "попутчики приднестровья",
    "попутка кишинев", "такси кишинев", "такси тирасполь", "такси бендеры", "бельцы попутка",
    "рыбница такси", "комрат", "кагул", "кишинев одесса", "кишинев яссы", "молдова италия",
    "молдова пассажирские перевозки", "chisinau transport", "transport pasageri", "caut transport",
    "calatorii chisinau", "moldova italia transport", "taxi chisinau",
]


def normalize_ref(line) -> str | int | None:
    if isinstance(line, int):
        return line
    line = str(line).split("#", 1)[0].strip()
    if not line:
        return None
    if re.fullmatch(r"-?\d+", line):
        return int(line)
    if "t.me/+" in line or "joinchat/" in line:
        return line  # ссылка-приглашение в закрытую группу
    m = re.match(r"(?:https?://)?t\.me/(?:s/)?(\w+)", line)
    return (m.group(1) if m else line.lstrip("@")).lower()


def load_sources() -> list[str | int]:
    """Группы из rides/sources.txt и из переменной RIDES_SOURCES (через запятую)."""
    raw = []
    if SOURCES_FILE.exists():
        raw += SOURCES_FILE.read_text("utf-8").splitlines()
    raw += os.environ.get("RIDES_SOURCES", "").split(",")
    out: list[str | int] = []
    for line in raw:
        ref = normalize_ref(line)
        if ref is not None and ref not in out:
            out.append(ref)
    return out


def message_link(chat, chat_id: int, msg_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"  # откроется только у участников группы
    return None


def make_client() -> TelegramClient | None:
    api_id, api_hash, session = (os.environ.get(k, "").strip()
                                 for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    if not (api_id and api_hash and session):
        log.warning("TG_API_ID / TG_API_HASH / TG_SESSION не заданы — чтение групп выключено")
        return None
    return TelegramClient(StringSession(session), int(api_id), api_hash)


class Watcher:
    """Читает группы, сохраняет объявления, рассылает новые. Группы можно добавлять на ходу."""

    def __init__(self, client: TelegramClient, db: RidesDB, notifier: Notifier):
        self.client, self.db, self.notifier = client, db, notifier
        self.chats: dict[int, object] = {}   # chat_id → entity
        self.auto_join = os.environ.get("RIDES_AUTO_JOIN", "1") == "1"
        self.backfill_hours = int(os.environ.get("RIDES_BACKFILL_HOURS", "24"))

    # ---------- подключение групп ----------
    async def _resolve(self, ref):
        if isinstance(ref, str) and ("t.me/+" in ref or "joinchat/" in ref):
            invite_hash = re.split(r"t\.me/\+|joinchat/", ref, maxsplit=1)[1].split("?")[0]
            try:
                upd = await self.client(ImportChatInviteRequest(invite_hash))
                return upd.chats[0]
            except Exception as e:  # noqa: BLE001 — уже участник или ссылка устарела
                raise RuntimeError(f"приглашение не сработало ({e}). Если аккаунт уже в группе — "
                                   "добавьте её по числовому id") from e
        entity = await self.client.get_entity(ref)
        if self.auto_join and isinstance(entity, Channel):
            try:
                await self.client(JoinChannelRequest(entity))
            except FloodWaitError as e:
                raise RuntimeError(f"Telegram просит подождать {e.seconds} с перед вступлением") from e
            except Exception as e:  # noqa: BLE001 — уже участник
                log.debug("Вступление в %s: %s", ref, e)
        return entity

    async def watch(self, ref, backfill: bool = True) -> str:
        """Начать читать группу. Возвращает её название."""
        entity = await self._resolve(ref)
        chat_id = await self.client.get_peer_id(entity)
        title = getattr(entity, "title", None) or str(ref)
        new = chat_id not in self.chats
        self.chats[chat_id] = entity
        if new:
            log.info("Слушаю: %s", title)
            if backfill:
                self._spawn(self.backfill(entity))
        return title

    async def unwatch(self, ref) -> None:
        try:
            entity = await self.client.get_entity(ref)
            self.chats.pop(await self.client.get_peer_id(entity), None)
        except Exception as e:  # noqa: BLE001
            log.info("Отключение %s: %s", ref, e)

    # ---------- поиск групп ----------
    async def search(self, queries: list[str]) -> list[dict]:
        found: dict[int, dict] = {}
        for q in queries:
            try:
                res = await self.client(SearchRequest(q=q, limit=50))
            except FloodWaitError as e:
                log.warning("Поиск: подождать %s с", e.seconds)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("Поиск «%s»: %s", q, e)
                continue
            for ch in res.chats:
                if not isinstance(ch, (Channel, Chat)) or not getattr(ch, "username", None):
                    continue
                cid = await self.client.get_peer_id(ch)
                if cid in found:
                    continue
                found[cid] = {
                    "id": cid, "title": ch.title, "username": ch.username,
                    "members": getattr(ch, "participants_count", None) or 0,
                    "is_group": bool(getattr(ch, "megagroup", False)) or isinstance(ch, Chat),
                    "watched": cid in self.chats, "query": q,
                }
            await asyncio.sleep(1)  # не злим Telegram
        return sorted(found.values(), key=lambda x: (x["watched"], not x["is_group"], -x["members"]))

    # ---------- обработка сообщений ----------
    async def handle(self, msg, chat, notify: bool) -> None:
        """Одно сообщение из группы → объявление (+ рассылка, если оно новое)."""
        text = msg.raw_text or ""
        if len(text) < 6 or msg.out:
            return
        p = parse_message(text)
        if not p.places or p.kind == UNKNOWN:
            if notify and p.kind != UNKNOWN:
                log.info("Похоже на заявку, но город не распознан: %s", text[:200].replace("\n", " "))
            return  # без населённого пункта или непонятно, кто пишет
        title = getattr(chat, "title", "") or str(msg.chat_id)
        duplicate = await self.db.seen_recently(p.fingerprint, ts=msg.date.timestamp())
        req_id = None
        if notify:
            req_id = await self.db.log_request(title, p, duplicate)
            if duplicate:
                return
        sender = await msg.get_sender()
        name = " ".join(x for x in (getattr(sender, "first_name", None),
                                    getattr(sender, "last_name", None)) if x) or None
        post = post_from_group(p, chat=title, link=message_link(chat, msg.chat_id, msg.id),
                               posted=msg.date, author_id=getattr(sender, "id", None),
                               username=getattr(sender, "username", None), name=name)
        if not notify and post["expires_at"] < time.time():
            return  # старая заявка, уже неактуальна
        saved = await self.db.save_post(post)
        if saved and notify:
            sent = await self.notifier.dispatch(saved)
            await self.db.set_delivered(req_id, sent)
            log.info("%s | %s → %s | отправлено %d", p.kind, p.from_place, p.to_place, sent)

    async def backfill(self, chat):
        """Заявки, написанные до запуска: история за последние часы. Не рассылаем —
        они видны в ленте «Актуальные»."""
        since = time.time() - self.backfill_hours * 3600
        count = 0
        try:
            msgs = []
            async for msg in self.client.iter_messages(chat, limit=1000):
                if msg.date.timestamp() < since:
                    break
                msgs.append(msg)
            for msg in reversed(msgs):  # от старых к новым
                try:
                    await self.handle(msg, chat, notify=False)
                    count += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("История: %s", e)
            log.info("История %s: %d сообщений за %d ч", getattr(chat, "title", chat), count,
                     self.backfill_hours)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось загрузить историю %s: %s", getattr(chat, "title", chat), e)

    async def _housekeeping(self):
        while True:
            try:
                await self.db.cleanup()
            except Exception as e:  # noqa: BLE001
                log.warning("Очистка: %s", e)
            await asyncio.sleep(3600)

    @staticmethod
    def _spawn(coro):
        task = asyncio.create_task(coro)
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    # ---------- запуск ----------
    async def start(self) -> bool:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error("TG_SESSION недействительна — сгенерируйте заново: python make_session.py")
            return False
        me = await self.client.get_me()
        log.info("Аккаунт-читатель: %s", me.username or me.id)

        refs = load_sources() + [normalize_ref(r["ref"]) for r in await self.db.list_sources()]
        for ref in dict.fromkeys(r for r in refs if r is not None):
            try:
                await self.watch(ref)
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось открыть %s: %s", ref, e)
        if not self.chats:
            log.warning("Пока нет ни одной группы — добавьте через /findgroups или /addsource")

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.chat_id in self.chats))
        async def on_message(event):
            try:
                await self.handle(event.message, await event.get_chat(), notify=True)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки сообщения")

        self._spawn(self._housekeeping())
        return True
