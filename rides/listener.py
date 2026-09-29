"""Чтение групп попутчиков через отдельный Telegram-аккаунт (Telethon) в реальном времени.

Аккаунт-читатель подключается по строке сессии из переменной TG_SESSION
(получить её: python make_session.py на своём компьютере).
"""
import logging
import os
import re
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .db import RidesDB
from .notifier import Item, Notifier
from .parsing import UNKNOWN, parse_message

log = logging.getLogger("rides.listener")
SOURCES_FILE = Path(__file__).with_name("sources.txt")


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

    @client.on(events.NewMessage(chats=chats, incoming=True))
    async def on_message(event):
        text = event.raw_text or ""
        if len(text) < 6:
            return
        p = parse_message(text)
        if not p.places:
            return  # без населённого пункта это не заявка
        chat = await event.get_chat()
        title = getattr(chat, "title", "") or str(event.chat_id)
        duplicate = await db.seen_recently(p.fingerprint)
        req_id = await db.log_request(title, p, duplicate)
        if duplicate or (p.kind == UNKNOWN and not p.has_route):
            return
        sender = await event.get_sender()
        name = " ".join(x for x in (getattr(sender, "first_name", None),
                                    getattr(sender, "last_name", None)) if x) or None
        item = Item(parsed=p, chat_title=title, link=message_link(chat, event.chat_id, event.id),
                    sender_username=getattr(sender, "username", None), sender_name=name)
        sent = await notifier.dispatch(item)
        await db.set_delivered(req_id, sent)
        log.info("%s | %s → %s | отправлено %d", p.kind, p.from_place, p.to_place, sent)

    return True
