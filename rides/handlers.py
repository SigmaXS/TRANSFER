"""Раздел для водителей: заявки попутчиков с фильтрами Молдова / ПМР / маршрут."""
import html
import os
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

from .db import RidesDB
from .filters import ALL, MOLDOVA, PMR_ONLY, ROUTE, Filter
from .places import POPULAR, resolve_place

router = Router(name="rides")

TRIAL_DAYS = int(os.environ.get("RIDES_TRIAL_DAYS", "7"))
PAYMENT_TEXT = os.environ.get("RIDES_PAYMENT_TEXT", "Для продления доступа напишите администратору.")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_CHAT_ID", "1657186014").replace(" ", "").split(",") if x}


class RouteForm(StatesGroup):
    from_place = State()
    to_place = State()


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


def main_menu(active: bool) -> InlineKeyboardMarkup:
    return kb([
        [("➕ Добавить фильтр", "r:add")],
        [("📋 Мои фильтры", "r:list"), ("⚙️ Что присылать", "r:mode")],
        [("⏸ Пауза" if active else "▶️ Включить", "r:toggle"), ("ℹ️ Помощь", "r:help")],
    ])


def filter_kinds() -> InlineKeyboardMarkup:
    return kb([
        [("🛣 Маршрут: откуда → куда", "r:add:route")],
        [("🇲🇩 Вся Молдова", "r:add:md"), ("🔴 Только ПМР", "r:add:pmr")],
        [("🌍 Все заявки", "r:add:all")],
        [("« Назад", "r:home")],
    ])


def places_kb(prefix: str, allow_any: bool) -> InlineKeyboardMarkup:
    rows = [[(name, f"{prefix}:{i}") for i, name in list(enumerate(POPULAR))[j:j + 3]]
            for j in range(0, len(POPULAR), 3)]
    if allow_any:
        rows.append([("Любой город", f"{prefix}:any")])
    rows.append([("✖️ Отмена", "r:home")])
    return kb(rows)


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


async def status_text(db: RidesDB, user_id: int) -> str:
    u = await db.get_user(user_id)
    fs = await db.get_filters(user_id)
    lines = ["🚕 <b>Заявки попутчиков — Молдова и ПМР</b>\n"]
    lines.append(f"✅ Доступ до {fmt_date(u.access_until())}" if u.has_access()
                 else "⛔️ Доступ закончился — заявки не приходят.")
    lines.append("▶️ Уведомления включены" if u.active else "⏸ Уведомления на паузе")
    lines.append("Присылаю: " + ("только пассажиров" if u.mode == "passenger"
                                 else "пассажиров и водителей с местами"))
    lines.append("\n<b>Фильтры:</b>" if fs else "\nФильтров пока нет — добавьте хотя бы один 👇")
    lines += [f"• {html.escape(f.title())}" for f in fs]
    return "\n".join(lines)


async def show_home(target: Message, db: RidesDB, user_id: int, edit: bool = False):
    u = await db.get_user(user_id)
    text, markup = await status_text(db, user_id), main_menu(u.active)
    if edit:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


async def open_section(target: Message, user, db: RidesDB, edit: bool):
    _, is_new = await db.ensure_user(user.id, user.username, TRIAL_DAYS)
    if is_new:
        text = ("🚕 <b>Заявки попутчиков</b>\n\n"
                "Я читаю группы попутчиков и присылаю вам людей, которые ищут машину по вашему "
                "маршруту, — чтобы не ехать пустым.\n\n"
                f"🎁 Бесплатно {TRIAL_DAYS} дней. Какие заявки присылать?")
        if edit:
            await target.edit_text(text, reply_markup=filter_kinds(), parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=filter_kinds(), parse_mode="HTML")
        return
    await show_home(target, db, user.id, edit=edit)


# ---------------- вход в раздел ----------------

@router.message(Command("driver"))
async def driver_cmd(msg: Message, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    await open_section(msg, msg.from_user, rides_db, edit=False)


@router.callback_query(F.data == "r:open")
async def driver_open(cb: CallbackQuery, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    await open_section(cb.message, cb.from_user, rides_db, edit=False)
    await cb.answer()


@router.callback_query(F.data == "r:home")
async def home(cb: CallbackQuery, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)
    await cb.answer()


@router.callback_query(F.data == "r:add")
async def add(cb: CallbackQuery):
    await cb.message.edit_text("Какие заявки присылать?", reply_markup=filter_kinds())
    await cb.answer()


@router.callback_query(F.data.in_({"r:add:md", "r:add:pmr", "r:add:all"}))
async def add_region(cb: CallbackQuery, rides_db: RidesDB):
    kind = {"r:add:md": MOLDOVA, "r:add:pmr": PMR_ONLY, "r:add:all": ALL}[cb.data]
    await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    if kind in {f.kind for f in await rides_db.get_filters(cb.from_user.id)}:
        await cb.answer("Такой фильтр уже есть")
    else:
        await rides_db.add_filter(cb.from_user.id, Filter(kind))
        await cb.answer("Фильтр добавлен ✅")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


# ---------------- маршрут откуда → куда ----------------

@router.callback_query(F.data == "r:add:route")
async def route_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(RouteForm.from_place)
    await cb.message.edit_text("📍 <b>Откуда?</b>\nВыберите или напишите название города/села.",
                               reply_markup=places_kb("r:from", allow_any=True), parse_mode="HTML")
    await cb.answer()


async def _ask_to(target: Message, state: FSMContext, from_place: str | None, edit: bool):
    await state.update_data(from_place=from_place)
    await state.set_state(RouteForm.to_place)
    text = f"Откуда: <b>{from_place or 'любой город'}</b>\n\n🏁 <b>Куда?</b>"
    markup = places_kb("r:to", allow_any=from_place is not None)
    send = target.edit_text if edit else target.answer
    await send(text, reply_markup=markup, parse_mode="HTML")


async def _ask_direction(target: Message, state: FSMContext, to_place: str | None, edit: bool):
    data = await state.update_data(to_place=to_place)
    frm = data.get("from_place")
    if frm and to_place and frm == to_place:
        await target.answer("Откуда и куда совпадают — выберите другой пункт.")
        return
    text = f"Маршрут: <b>{frm or 'любой'} → {to_place or 'любой'}</b>\n\nВ обе стороны?"
    markup = kb([[("⇄ Туда и обратно", "r:dir:1"), ("→ Только туда", "r:dir:0")],
                 [("✖️ Отмена", "r:home")]])
    send = target.edit_text if edit else target.answer
    await send(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(RouteForm.from_place, F.data.startswith("r:from:"))
async def route_from_btn(cb: CallbackQuery, state: FSMContext):
    val = cb.data.rsplit(":", 1)[1]
    await _ask_to(cb.message, state, None if val == "any" else POPULAR[int(val)], edit=True)
    await cb.answer()


@router.message(RouteForm.from_place, F.text)
async def route_from_text(msg: Message, state: FSMContext):
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _ask_to(msg, state, place, edit=False)


@router.callback_query(RouteForm.to_place, F.data.startswith("r:to:"))
async def route_to_btn(cb: CallbackQuery, state: FSMContext):
    val = cb.data.rsplit(":", 1)[1]
    await _ask_direction(cb.message, state, None if val == "any" else POPULAR[int(val)], edit=True)
    await cb.answer()


@router.message(RouteForm.to_place, F.text)
async def route_to_text(msg: Message, state: FSMContext):
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _ask_direction(msg, state, place, edit=False)


@router.callback_query(RouteForm.to_place, F.data.startswith("r:dir:"))
async def route_save(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    data = await state.get_data()
    await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    await rides_db.add_filter(cb.from_user.id, Filter(ROUTE, data.get("from_place"), data.get("to_place"),
                                                      both_ways=cb.data == "r:dir:1"))
    await state.clear()
    await cb.answer("Маршрут сохранён ✅")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


# ---------------- список, режим, пауза, помощь ----------------

@router.callback_query(F.data == "r:list")
async def list_filters(cb: CallbackQuery, rides_db: RidesDB):
    fs = await rides_db.get_filters(cb.from_user.id)
    if not fs:
        await cb.answer("Фильтров нет")
        return
    rows = [[(f"🗑 {f.title()}", f"r:del:{f.id}")] for f in fs] + [[("« Назад", "r:home")]]
    await cb.message.edit_text("Нажмите на фильтр, чтобы удалить его:", reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("r:del:"))
async def delete_filter(cb: CallbackQuery, rides_db: RidesDB):
    await rides_db.delete_filter(cb.from_user.id, int(cb.data.rsplit(":", 1)[1]))
    if await rides_db.get_filters(cb.from_user.id):
        await list_filters(cb, rides_db)
    else:
        await cb.answer("Удалено")
        await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


@router.callback_query(F.data == "r:mode")
async def mode(cb: CallbackQuery):
    await cb.message.edit_text(
        "Что присылать?",
        reply_markup=kb([[("🙋 Только пассажиров (ищут машину)", "r:mode:passenger")],
                         [("🙋+🚗 Ещё и водителей с местами", "r:mode:all")],
                         [("« Назад", "r:home")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("r:mode:"))
async def set_mode(cb: CallbackQuery, rides_db: RidesDB):
    await rides_db.set_mode(cb.from_user.id, cb.data.rsplit(":", 1)[1])
    await cb.answer("Сохранено")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


@router.callback_query(F.data == "r:toggle")
async def toggle(cb: CallbackQuery, rides_db: RidesDB):
    u = await rides_db.get_user(cb.from_user.id)
    await rides_db.set_active(cb.from_user.id, not u.active)
    await cb.answer("Включено" if not u.active else "Пауза")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


HELP = (
    "<b>Как это работает</b>\n"
    "Бот читает группы попутчиков и присылает вам заявки, которые подходят под ваши фильтры.\n\n"
    "🛣 <b>Маршрут</b> — например, Тирасполь ⇄ Кишинёв. Если в заявке указан только один город "
    "(«кто едет в Кишинёв?»), она тоже придёт.\n"
    "🇲🇩 <b>Вся Молдова</b> — любая заявка, где есть город Молдовы (в том числе поездки в ПМР и за границу).\n"
    "🔴 <b>ПМР</b> — любая заявка, где есть Тирасполь, Бендеры, Рыбница и др.\n\n"
    "Нажмите «✉️ Написать», чтобы связаться с человеком. Кто быстрее ответит — тот и везёт 😉\n\n"
    "/driver — меню водителя · /start — заказ трансфера"
)


@router.callback_query(F.data == "r:help")
async def help_cb(cb: CallbackQuery):
    await cb.message.edit_text(HELP + f"\n\n💳 {html.escape(PAYMENT_TEXT)}",
                               reply_markup=kb([[("« Назад", "r:home")]]), parse_mode="HTML")
    await cb.answer()


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
    k = s["by_kind"]
    lines = [f"📊 <b>Заявки попутчиков за {days} дн.</b>",
             f"🙋 Пассажиры: {k.get('passenger', 0)} (≈{k.get('passenger', 0) / days:.1f}/день)",
             f"🚗 Водители: {k.get('driver', 0)}",
             f"❔ Прочее с городами: {k.get('unknown', 0)}",
             f"♻️ Дубли: {s['dups']} · 📢 Реклама: {s['ads']}",
             f"👥 Водителей в боте: {s['users']}, получают заявки: {s['active']}",
             "\n<b>Группы (заявки пассажиров):</b>"]
    lines += [f"• {html.escape(r['chat'])}: {r['c']}" for r in s["by_chat"]] or ["—"]
    lines.append("\n<b>Топ маршрутов:</b>")
    lines += [f"• {r['from_place']} → {r['to_place']}: {r['c']}" for r in s["routes"]] or ["—"]
    await msg.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("myid"))
async def myid(msg: Message):
    await msg.answer(f"Ваш ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")
