"""Чтение групп попутчиков через отдельный Telegram-аккаунт (Telethon) в реальном времени.

Аккаунт-читатель подключается по строке сессии из переменной TG_SESSION
(получить её: python make_session.py на своём компьютере).
"""
import asyncio
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .db import RidesDB
from .notifier import Notifier
from .parsing import UNKNOWN, parse_message
from .posts import post_from_group

log = logging.getLogger("rides.listener")
SOURCES_FILE = Path(__file__).with_name("sources.txt")
_BACKGROUND: set = set()  # ссылки на фоновые задачи, чтобы их не собрал сборщик мусора


def load_sources() -> list[str | int]:
    """Группы из rides/sources.txt и из переменной RIDES_SOURCES (через запятую)."""
    raw = []
    if SOURCES_FILE.exists():
        raw += [line.split("#", 1)[0].strip() for line in SOURCES_FILE.read_text("utf-8").splitlines()]
    raw += [x.strip() for x in os.environ.get("RIDES_SOURCES", "").split(",")]
    out: list[str | int] = []
    for line in raw:
        if not line:
            continue
        if re.fullmatch(r"-?\d+", line):
            item: str | int = int(line)
        elif "t.me/+" in line:
            item = line  # ссылка-приглашение в закрытую группу
        else:
            m = re.match(r"(?:https?://)?t\.me/(?:s/)?(\w+)", line)
            item = (m.group(1) if m else line.lstrip("@")).lower()
        if item not in out:
            out.append(item)
    return out


def message_link(chat, chat_id: int, msg_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"  # откроется только у участников группы
    return None


async def _resolve(client: TelegramClient, source, auto_join: bool):
    if isinstance(source, str) and "t.me/+" in source:
        invite_hash = source.split("t.me/+", 1)[1].split("?")[0]
        try:
            upd = await client(ImportChatInviteRequest(invite_hash))
            return upd.chats[0]
        except Exception as e:  # noqa: BLE001 — уже участник или ссылка устарела
            log.info("Приглашение %s: %s (если уже в группе — укажите её числовой id)", source, e)
            return None
    entity = await client.get_entity(source)
    if auto_join:
        try:
            await client(JoinChannelRequest(entity))
        except Exception as e:  # noqa: BLE001 — уже участник / это не супергруппа
            log.debug("Вступление в %s: %s", source, e)
    return entity


def make_client() -> TelegramClient | None:
    api_id, api_hash, session = (os.environ.get(k, "").strip()
                                 for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"))
    if not (api_id and api_hash and session):
        log.warning("TG_API_ID / TG_API_HASH / TG_SESSION не заданы — чтение групп выключено "
                    "(раздел для водителей в боте работает, но заявки не приходят)")
        return None
    return TelegramClient(StringSession(session), int(api_id), api_hash)


async def start_listener(client: TelegramClient, db: RidesDB, notifier: Notifier):
    await client.connect()
    if not await client.is_user_authorized():
        log.error("TG_SESSION недействительна — сгенерируйте заново: python make_session.py")
        return False
    me = await client.get_me()
    log.info("Аккаунт-читатель: %s", me.username or me.id)

    auto_join = os.environ.get("RIDES_AUTO_JOIN", "1") == "1"
    chats = []
    for source in load_sources():
        try:
            entity = await _resolve(client, source, auto_join)
            if entity is not None:
                chats.append(entity)
                log.info("Слушаю: %s", getattr(entity, "title", source))
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось открыть %s: %s", source, e)
    if not chats:
        log.error("Нет ни одной доступной группы — проверьте rides/sources.txt")
        return False

    async def handle(msg, chat, notify: bool) -> None:
        """Одно сообщение из группы → объявление (+ рассылка, если это новое)."""
        text = msg.raw_text or ""
        if len(text) < 6 or msg.out:
            return
        p = parse_message(text)
        if not p.places or p.kind == UNKNOWN:
            return  # без населённого пункта или непонятно, кто пишет
        title = getattr(chat, "title", "") or str(msg.chat_id)
        duplicate = await db.seen_recently(p.fingerprint, ts=msg.date.timestamp())
        if notify:
            req_id = await db.log_request(title, p, duplicate)
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
        saved = await db.save_post(post)
        if saved and notify:
            sent = await notifier.dispatch(saved)
            await db.set_delivered(req_id, sent)
            log.info("%s | %s → %s | отправлено %d", p.kind, p.from_place, p.to_place, sent)

    @client.on(events.NewMessage(chats=chats, incoming=True))
    async def on_message(event):
        try:
            await handle(event.message, await event.get_chat(), notify=True)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки сообщения")

    async def backfill():
        """Заявки, написанные до запуска бота: берём историю групп за последние часы.
        Ничего не рассылаем — они появятся в «Актуальных заявках»."""
        hours = int(os.environ.get("RIDES_BACKFILL_HOURS", "24"))
        since = time.time() - hours * 3600
        for chat in chats:
            count = 0
            try:
                msgs = []
                async for msg in client.iter_messages(chat, limit=1000):
                    if msg.date.timestamp() < since:
                        break
                    msgs.append(msg)
                for msg in reversed(msgs):  # от старых к новым
                    try:
                        await handle(msg, chat, notify=False)
                        count += 1
                    except Exception as e:  # noqa: BLE001
                        log.debug("История: %s", e)
                log.info("История %s: просмотрено %d сообщений за %d ч",
                         getattr(chat, "title", chat), count, hours)
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось загрузить историю %s: %s", getattr(chat, "title", chat), e)

    async def housekeeping():
        while True:
            try:
                await db.cleanup()
            except Exception as e:  # noqa: BLE001
                log.warning("Очистка: %s", e)
            await asyncio.sleep(3600)

    _BACKGROUND.update({asyncio.create_task(backfill()), asyncio.create_task(housekeeping())})
    return True
