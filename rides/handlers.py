"""Бот «Попутчики»: для людей — одно приветствие и кнопка Mini App (всё остальное — внутри Mini App).
Для админа — команды: /grant, /stats, /findgroups, /addsource, /sources, /paste, /viberreset, /ingestlog, /fixes.
"""
import html
import os
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message,
                           ReplyKeyboardRemove, WebAppInfo)

from .db import RidesDB
from .parsing import DRIVER, PASSENGER, parse_message
from .posts import WEBAPP_URL
from .timeparse import TZ, trip_label

router = Router(name="rides")

TRIAL_DAYS = int(os.environ.get("RIDES_TRIAL_DAYS", "7"))
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_CHAT_ID", "1657186014").replace(" ", "").split(",") if x}

WELCOME = (
    "🚕🛣 <b>ПОПУТЧИКИ</b>\n"
    "🇲🇩 Молдова · 🔴 ПМР · 🇺🇦 Украина · 🇪🇺 Европа\n\n"
    "🚗 Машины и 🙋 пассажиры из групп попутчиков 💜 Viber и ✈️ Telegram — в одном месте.\n"
    "🚫 Без перевозчиков и рекламы · 🎁 Бесплатно\n\n"
    "🔎 <b>Найдите поездку</b> — и сразу 📞 позвоните водителю\n"
    "📢 <b>Разместите «Хочу поехать»</b> — водители увидят ⚡ сразу\n"
    "🔔 <b>Включите уведомления</b> — пришлю новые поездки по вашему маршруту\n\n"
    "👇👇👇 <b>Нажмите зелёную кнопку</b> 👇👇👇"
)


class PasteForm(StatesGroup):   # ручной ввод заявок из Viber и др. (админ)
    active = State()


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


def app_kb(styled: bool = True) -> InlineKeyboardMarkup:
    """Одна кнопка Mini App — зелёная (style в новых Telegram), заметнее обычных."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🚕🚕 ОТКРЫТЬ ПОПУТЧИКИ 🚕🚕", web_app=WebAppInfo(url=WEBAPP_URL), style="success" if styled else None)],
    ])


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y")


async def welcome(msg: Message, rides_db: RidesDB, user) -> None:
    _, is_new = await rides_db.ensure_user(user.id, user.username, TRIAL_DAYS)
    try:
        await rides_db.log_event(user.id, "start", {"new": is_new})
    except Exception:  # noqa: BLE001
        pass
    try:
        await msg.answer(WELCOME, reply_markup=app_kb(), parse_mode="HTML")
    except Exception:  # noqa: BLE001 — старый Telegram не принял цвет кнопки: та же кнопка без цвета
        await msg.answer(WELCOME, reply_markup=app_kb(styled=False), parse_mode="HTML")


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(msg: Message, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    try:  # убираем старую нижнюю клавиатуру, если она осталась у человека
        m = await msg.answer("…", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    await welcome(msg, rides_db, msg.from_user)


@router.callback_query(F.data.startswith("r:fmute:"))
async def mute_filter(cb: CallbackQuery, rides_db: RidesDB):
    """«🔕 Не присылать этот маршрут» под уведомлением: маршрут выключается (включить — в Mini App)."""
    await rides_db.set_filter_enabled(cb.from_user.id, int(cb.data.rsplit(":", 1)[1]), False)
    await cb.answer("Больше не пришлю по этому маршруту. Включить снова: 🚕 Попутчики → Уведомления.", show_alert=True)


@router.callback_query(F.data.startswith("r:"))
async def old_buttons(cb: CallbackQuery, rides_db: RidesDB):
    """Кнопки старого меню в прежних сообщениях — теперь всё в Mini App."""
    await cb.answer("Теперь всё здесь 👇")
    await welcome(cb.message, rides_db, cb.from_user)


# ---------------- админ ----------------

@router.message(Command("grant"), F.from_user.id.in_(ADMIN_IDS))
async def grant(msg: Message, command: CommandObject, rides_db: RidesDB):
    """/grant <user_id> <дней> — продлить доступ водителю после оплаты."""
    try:
        user_id, days = (int(x) for x in (command.args or "").split())
        until = await rides_db.grant(user_id, days)
    except (ValueError, TypeError) as e:
        await msg.answer(f"Формат: /grant user_id дней\n{e}")
        return
    await msg.answer(f"✅ Доступ для {user_id} до {fmt_date(until)}")
    try:
        await msg.bot.send_message(user_id, f"✅ Оплата получена. Доступ к заявкам до {fmt_date(until)}.")
    except Exception:  # noqa: BLE001
        pass


@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def stats(msg: Message, command: CommandObject, rides_db: RidesDB):
    """/stats [дней] — сколько заявок и откуда, чтобы оценить спрос."""
    days = int(command.args) if command.args and command.args.isdigit() else 7
    s = await rides_db.stats(days)
    k, r, b = s["by_kind"], s["roles"], s["bot_posts"]
    lines = [f"📊 <b>За {days} дн.</b>",
             f"🙋 Заявки пассажиров в группах: {k.get('passenger', 0)} "
             f"(≈{k.get('passenger', 0) / days:.1f}/день)",
             f"🚗 Водители с местами в группах: {k.get('driver', 0)}",
             f"📱 Объявления из бота: пассажиры {b.get('passenger', 0)}, водители {b.get('driver', 0)}",
             f"🟢 Актуальных объявлений сейчас: {s['live']}",
             f"♻️ Дубли: {s['dups']} · 📢 Реклама: {s['ads']}",
             f"👥 Пользователи: водители {r.get('driver', 0)}, пассажиры {r.get('passenger', 0)}, "
             f"не выбрали роль {r.get('?', 0)}",
             "\n<b>Группы (заявки пассажиров):</b>"]
    lines += [f"• {html.escape(x['chat'])}: {x['c']}" for x in s["by_chat"]] or ["—"]
    lines.append("\n<b>Топ маршрутов:</b>")
    lines += [f"• {x['from_place']} → {x['to_place']}: {x['c']}" for x in s["routes"]] or ["—"]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("myid"))
async def myid(msg: Message):
    await msg.answer(f"Ваш ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")


# ---------------- источники: группы Telegram (админ) ----------------

def _members(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


@router.message(Command("findgroups"), F.from_user.id.in_(ADMIN_IDS))
async def find_groups(msg: Message, command: CommandObject, watcher):
    """/findgroups [запрос] — найти публичные группы попутчиков в Telegram."""
    if watcher is None:
        await msg.answer("Чтение групп выключено: не заданы TG_API_ID / TG_API_HASH / TG_SESSION.")
        return
    from .listener import DEFAULT_QUERIES
    queries = [command.args] if command.args else DEFAULT_QUERIES
    wait = await msg.answer(f"🔎 Ищу группы в Telegram ({len(queries)} запросов)… это ~{len(queries) * 2} с")
    found = [g for g in await watcher.search(queries) if not g["watched"]]
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass
    if not found:
        await msg.answer("Новых групп не нашёл. Попробуйте свой запрос: /findgroups бельцы такси")
        return
    top = found[:25]
    lines = ["🔎 <b>Найденные группы</b> (👥 — участников, 📢 — канал, а не группа).\n"
             "Нажмите, чтобы бот начал их читать:\n"]
    for g in top:
        icon = "👥" if g["is_group"] else "📢"
        lines.append(f"{icon} {_members(g['members'])} · <a href=\"https://t.me/{g['username']}\">"
                     f"{html.escape(g['title'])}</a>")
    rows = [[(f"➕ {g['title'][:40]} ({_members(g['members'])})", f"src:add:{g['username']}")] for g in top]
    await msg.answer("\n".join(lines), reply_markup=kb(rows), parse_mode="HTML",
                     disable_web_page_preview=True)


async def _add_source(target: Message, ref_text: str, rides_db: RidesDB, watcher) -> None:
    from .listener import normalize_ref
    ref = normalize_ref(ref_text)
    if ref is None:
        await target.answer("Формат: /addsource @username или ссылка https://t.me/...")
        return
    if watcher is None:
        await rides_db.add_source(str(ref), None)
        await target.answer("Сохранил. Группа начнёт читаться, когда будет настроен аккаунт-читатель.")
        return
    try:
        title = await watcher.watch(ref)
    except Exception as e:  # noqa: BLE001
        await target.answer(f"❌ Не получилось добавить {html.escape(str(ref))}: {html.escape(str(e))}",
                            parse_mode="HTML")
        return
    await rides_db.add_source(str(ref), title)
    await target.answer(f"✅ Читаю «{html.escape(title)}». История за {watcher.backfill_hours} ч "
                        "загружается — заявки появятся в ленте через минуту.", parse_mode="HTML")


@router.callback_query(F.data.startswith("src:add:"), F.from_user.id.in_(ADMIN_IDS))
async def add_source_btn(cb: CallbackQuery, rides_db: RidesDB, watcher):
    await cb.answer("Добавляю…")
    await _add_source(cb.message, cb.data.split(":", 2)[2], rides_db, watcher)


@router.message(Command("addsource"), F.from_user.id.in_(ADMIN_IDS))
async def add_source_cmd(msg: Message, command: CommandObject, rides_db: RidesDB, watcher):
    """/addsource @group или ссылка (можно несколько через пробел/запятую)."""
    refs = [x for x in re.split(r"[\s,]+", command.args or "") if x]
    if not refs:
        await msg.answer("Формат: /addsource @username или https://t.me/... (можно несколько)")
        return
    for ref in refs:
        await _add_source(msg, ref, rides_db, watcher)


@router.message(Command("sources"), F.from_user.id.in_(ADMIN_IDS))
async def list_sources(msg: Message, rides_db: RidesDB, watcher):
    """/sources — какие группы и сайты читает бот."""
    from .listener import load_sources
    lines = ["📡 <b>Источники</b>"]
    if watcher is not None:
        lines.append(f"\n<b>Читаю сейчас ({len(watcher.chats)}):</b>")
        lines += [f"• {html.escape(getattr(e, 'title', '') or str(cid))}" for cid, e in watcher.chats.items()]
    else:
        lines.append("\n⚠️ Аккаунт-читатель не настроен — группы не читаются.")
    fixed = load_sources()
    if fixed:
        lines.append("\n<b>Из rides/sources.txt и RIDES_SOURCES</b> (меняются там):")
        lines += [f"• {html.escape(str(r))}" for r in fixed]
    added = await rides_db.list_sources()
    web = os.environ.get("RIDES_WEB", "makler")
    lines.append(f"\n🌐 <b>Сайты:</b> {'makler.md — перевозчики и такси' if 'makler' in web else 'выключены'}")
    lines.append("\nДобавить: /findgroups или /addsource @группа")
    rows = [[(f"🗑 {s['title'] or s['ref']}"[:60], f"src:del:{s['id']}")] for s in added]
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb(rows) if rows else None)


@router.callback_query(F.data.startswith("src:del:"), F.from_user.id.in_(ADMIN_IDS))
async def del_source(cb: CallbackQuery, rides_db: RidesDB, watcher):
    src = await rides_db.remove_source(int(cb.data.rsplit(":", 1)[1]))
    if src and watcher is not None:
        from .listener import normalize_ref
        await watcher.unwatch(normalize_ref(src["ref"]))
    await cb.answer("Удалено" if src else "Уже удалено")
    if src:
        await cb.message.answer(f"🗑 Больше не читаю «{html.escape(src['title'] or src['ref'])}».",
                                parse_mode="HTML")


# ---------------- ручной ввод: Viber и другие чаты, которые бот не читает сам ----------------

@router.message(Command("paste"), F.from_user.id.in_(ADMIN_IDS))
async def paste_start(msg: Message, command: CommandObject, state: FSMContext):
    """/paste [название группы] — дальше каждое вставленное/пересланное сообщение = заявка."""
    label = (command.args or "Viber").strip()[:60]
    await state.set_state(PasteForm.active)
    await state.update_data(label=label)
    await msg.answer(
        f"📥 <b>Режим вставки заявок</b> · источник: «{html.escape(label)}»\n\n"
        "Копируйте сообщения из Viber (или пересылайте из любого чата) и отправляйте сюда — "
        "каждое станет заявкой: попадёт в ленту и уйдёт водителям/пассажирам по фильтрам.\n"
        "Телефон из текста останется в заявке.\n\n"
        "Сменить источник: /paste Название · Выйти: /done", parse_mode="HTML")


@router.message(PasteForm.active, Command("done"))
async def paste_done(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Режим вставки выключен. /start — меню")


@router.message(PasteForm.active, F.text | F.caption)
async def paste_item(msg: Message, state: FSMContext, rides_db: RidesDB, notifier):
    from .posts import post_from_group
    text = msg.text or msg.caption
    p = parse_message(text)
    if not p.places or p.kind not in (PASSENGER, DRIVER):
        await msg.reply("⚠️ Не понял маршрут или кто пишет — пропускаю. "
                        "Нужен хотя бы один город и «ищу/нужна машина» или «есть места».")
        return
    label = (await state.get_data()).get("label", "Viber")
    origin = msg.forward_origin
    author = getattr(getattr(origin, "sender_user", None), "username", None)
    post = post_from_group(p, chat=label, link=None, posted=msg.forward_date or msg.date,
                           author_id=None, username=author, name=None)
    saved = await rides_db.save_post(post)
    if not saved:
        await msg.reply("♻️ Такая заявка сегодня уже есть.")
        return
    sent = await notifier.dispatch(saved)
    who = "водителям" if saved["kind"] == PASSENGER else "пассажирам"
    route = f"{saved['from_place'] or '?'} → {saved['to_place'] or '?'}"
    await msg.reply(f"✅ {'🙋' if saved['kind'] == PASSENGER else '🚗'} {route} · "
                    f"{trip_label(saved['trip_at'], saved['has_time'], saved['when_label'])} · "
                    f"отправлено {who}: {sent}")


@router.message(Command("viberreset"), F.from_user.id.in_(ADMIN_IDS))
async def viber_reset(msg: Message, rides_db: RidesDB):
    """/viberreset — удалить объявления из Viber (после неверной загрузки истории)."""
    n = await rides_db.reset_viber()
    await msg.answer(f"Удалено объявлений из Viber: {n}. Теперь нажмите «Загрузить историю» в приложении.")


@router.message(Command("ingestlog"), F.from_user.id.in_(ADMIN_IDS))
async def ingest_log(msg: Message):
    """/ingestlog — последние уведомления, пришедшие с телефона (Viber), и что с ними сделал бот."""
    from .ingest import RECENT
    from .timeparse import TZ as _TZ
    if not RECENT:
        await msg.answer("С телефона пока ничего не приходило с момента последнего запуска бота.")
        return
    reasons = {"not a rides group": "не группа попутчиков", "not a ride": "не заявка",
               "duplicate": "дубль", "empty": "пустое", "expired": "история: время уже прошло",
               "summary": "сводка Viber без текста — нужны поля lines/big"}
    lines = ["📥 <b>Последние уведомления с телефона</b>\n"]
    for ts, title, text, res, *rest in list(RECENT)[-10:]:
        raw = rest[0] if rest else {}
        t = datetime.fromtimestamp(ts, _TZ).strftime("%H:%M:%S")
        if res.get("kind"):
            verdict = (f"📜 история: {res.get('from')} → {res.get('to')}, в ленте" if res.get("history")
                       else f"✅ {res.get('from')} → {res.get('to')}, отправлено {res.get('sent')}")
        else:
            verdict = "⏭ " + reasons.get(res.get("skipped", ""), res.get("skipped") or res.get("error", "?"))
        fields = "\n".join(f"  <i>{html.escape(k)}</i>: {html.escape(v)}" for k, v in raw.items()
                           if k not in ("title", "text"))
        lines.append(f"<b>{t}</b> {verdict}\n<code>{html.escape(title or '(без заголовка)')}</code>\n"
                     f"{html.escape(text or '(без текста)')}\n" + (f"{fields}\n" if fields else
                                                                    "  <i>других полей нет</i>\n"))
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


@router.message(Command("fixes"), F.from_user.id.in_(ADMIN_IDS))
async def fixes_log(msg: Message, rides_db: RidesDB, command: CommandObject):
    """/fixes [дней] — журнал правок и удалений из Mini App файлом (что бот разобрал неверно)."""
    import json
    from aiogram.types import BufferedInputFile
    days = int(command.args) if command.args and command.args.strip().isdigit() else 30
    rows = await rides_db.fixes(days)
    if not rows:
        await msg.answer(f"За {days} дн. правок не было.")
        return
    out = []
    for r in rows:
        r = dict(r)
        for k in ("before_data", "after_data"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])
        r["time"] = datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d %H:%M")
        out.append(json.dumps(r, ensure_ascii=False, default=str))
    edits = sum(1 for r in rows if r["action"] == "edit")
    await msg.answer_document(BufferedInputFile("\n".join(out).encode(), filename=f"fixes-{days}d.jsonl"),
                              caption=f"Правок: {edits}, удалений: {len(rows) - edits} за {days} дн.")


@router.message(F.chat.type == "private")
async def anything_else(msg: Message, rides_db: RidesDB):
    """Любое другое сообщение — приветствие с кнопкой Mini App."""
    await welcome(msg, rides_db, msg.from_user)
