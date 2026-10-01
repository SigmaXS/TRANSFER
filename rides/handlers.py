"""Меню бота: водитель (ищет пассажиров) и пассажир (ищет машину).

Водитель: фильтры, лента актуальных заявок пассажиров, «есть свободная машина».
Пассажир: лента свободных машин, «хочу поехать» — своя заявка, фильтры.
"""
import html
import os
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)

from .db import DRIVER_ROLE, PASSENGER_ROLE, RidesDB, want_for
from .filters import ALL, EUROPE, MOLDOVA, NEARBY_KM, PMR_ONLY, ROUTE, UKRAINE, Filter, matches
from .parsing import DRIVER, PASSENGER, parse_message
from .places import POPULAR, direction_label, resolve_place
from .posts import format_post, post_from_bot, post_keyboard, post_to_parsed
from .timeparse import TZ, trip_label

router = Router(name="rides")

TRIAL_DAYS = int(os.environ.get("RIDES_TRIAL_DAYS", "7"))
PAYMENT_TEXT = os.environ.get("RIDES_PAYMENT_TEXT", "Для продления доступа напишите администратору.")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_CHAT_ID", "1657186014").replace(" ", "").split(",") if x}
FEED_PAGE = 5
MAX_ACTIVE_POSTS = 5
TITLE = "🚕 <b>Попутчики · Молдова · ПМР · UA · EU</b>"


class RouteForm(StatesGroup):   # фильтр-маршрут
    from_place = State()
    to_place = State()


class PasteForm(StatesGroup):   # ручной ввод заявок из Viber и др.
    active = State()


class PostForm(StatesGroup):    # своё объявление
    from_place = State()
    to_place = State()
    when = State()
    count = State()
    comment = State()
    contact = State()
    confirm = State()


# ---------------- клавиатуры ----------------

def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


def role_kb() -> InlineKeyboardMarkup:
    return kb([[("🚗 Я водитель — ищу пассажиров", "r:role:driver")],
               [("🙋 Я пассажир — ищу машину / попутку", "r:role:passenger")]])


def main_menu(role: str, active: bool) -> InlineKeyboardMarkup:
    pause = ("⏸ Пауза" if active else "▶️ Включить", "r:toggle")
    if role == PASSENGER_ROLE:
        return kb([
            [("🔎 Свободные машины сейчас", "r:feed:0")],
            [("📢 Хочу поехать — разместить заявку", "r:post")],
            [("➕ Фильтр уведомлений", "r:add"), ("📋 Мои фильтры", "r:list")],
            [("🗂 Мои заявки", "r:my"), ("ℹ️ Помощь", "r:help")],
            [pause, ("🔄 Я водитель", "r:role:driver")],
        ])
    return kb([
        [("🔎 Актуальные заявки пассажиров", "r:feed:0")],
        [("➕ Добавить фильтр", "r:add"), ("📋 Мои фильтры", "r:list")],
        [("📢 Есть свободная машина", "r:post")],
        [("🗂 Мои объявления", "r:my"), ("ℹ️ Помощь", "r:help")],
        [pause, ("🔄 Я пассажир", "r:role:passenger")],
    ])


def region_kinds() -> InlineKeyboardMarkup:
    """Старые фильтры «целым регионом» — на отдельном экране, основной путь: откуда → куда."""
    return kb([
        [("🔴 Все заявки ПМР", "r:add:pmr"), ("🇲🇩 Вся Молдова", "r:add:md")],
        [("🇺🇦 Украина", "r:add:ua"), ("🇪🇺 Европа", "r:add:eu")],
        [("🌍 Вообще все", "r:add:all")],
        [("« Назад", "r:add")],
    ])


def to_kb() -> InlineKeyboardMarkup:
    rows = [[(name, f"r:to:{i}") for i, name in list(enumerate(POPULAR))[j:j + 3]]
            for j in range(0, len(POPULAR), 3)]
    rows.append([("➡️ Куда угодно", "r:to:any")])
    rows.append([("✖️ Отмена", "r:home")])
    return kb(rows)


def places_kb(prefix: str, allow_any: bool) -> InlineKeyboardMarkup:
    rows = [[(name, f"{prefix}:{i}") for i, name in list(enumerate(POPULAR))[j:j + 3]]
            for j in range(0, len(POPULAR), 3)]
    if allow_any:
        rows.append([("Любой город", f"{prefix}:any")])
    rows.append([("✖️ Отмена", "r:home")])
    return kb(rows)


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y")


async def answer_or_edit(target: Message, text: str, markup=None, edit: bool = False):
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup, parse_mode="HTML",
                                   disable_web_page_preview=True)
            return
        except Exception:  # noqa: BLE001 — это сообщение нельзя отредактировать
            pass
    await target.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


# ---------------- главный экран ----------------

async def status_text(db: RidesDB, user_id: int) -> str:
    u = await db.get_user(user_id)
    fs = await db.get_filters(user_id, want_for(u.role))
    lines = [TITLE, ""]
    if u.role == PASSENGER_ROLE:
        lines.append("Вы: 🙋 <b>пассажир</b> — присылаю свободные машины по вашим фильтрам.")
    else:
        lines.append("Вы: 🚗 <b>водитель</b> — присылаю людей, которые ищут машину.")
        lines.append(f"✅ Доступ до {fmt_date(u.access_until())}" if u.has_access()
                     else "⛔️ Доступ закончился — новые заявки не приходят.")
    lines.append("▶️ Уведомления включены" if u.active else "⏸ Уведомления на паузе")
    lines.append("\n<b>Фильтры уведомлений:</b>" if fs else
                 "\nФильтров пока нет — уведомления не приходят. Добавьте фильтр 👇")
    lines += [f"• {html.escape(f.title())}" for f in fs]
    return "\n".join(lines)


async def show_home(target: Message, db: RidesDB, user_id: int, edit: bool = False):
    u = await db.get_user(user_id)
    await answer_or_edit(target, await status_text(db, user_id), main_menu(u.role, u.active), edit)


@router.message(CommandStart())
@router.message(Command("menu"))
@router.message(Command("driver"))
async def start(msg: Message, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    try:  # убираем старую нижнюю кнопку «Заказать трансфер», если она осталась у человека
        m = await msg.answer("…", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    u, _ = await rides_db.ensure_user(msg.from_user.id, msg.from_user.username, TRIAL_DAYS)
    if not u.role:
        await msg.answer(
            f"{TITLE}\n\nСоединяю водителей и пассажиров: беру заявки из групп попутчиков в Telegram "
            "и объявления прямо из бота.\n\n<b>Кто вы?</b>", reply_markup=role_kb(), parse_mode="HTML")
        return
    await show_home(msg, rides_db, msg.from_user.id)


@router.callback_query(F.data.in_({"r:home", "r:open"}))
async def home(cb: CallbackQuery, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    u, _ = await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    if not u.role:
        await answer_or_edit(cb.message, "<b>Кто вы?</b>", role_kb(), edit=True)
    else:
        await show_home(cb.message, rides_db, cb.from_user.id, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("r:role:"))
async def choose_role(cb: CallbackQuery, rides_db: RidesDB, state: FSMContext):
    await state.clear()
    role = cb.data.rsplit(":", 1)[1]
    await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    await rides_db.set_role(cb.from_user.id, role)
    await cb.answer("🚗 Режим водителя" if role == DRIVER_ROLE else "🙋 Режим пассажира")
    if await rides_db.get_filters(cb.from_user.id, want_for(role)):
        await show_home(cb.message, rides_db, cb.from_user.id, edit=True)
        return
    if role == DRIVER_ROLE:
        u = await rides_db.get_user(cb.from_user.id)
        intro = (f"🚗 Буду присылать людей, которые ищут машину по вашему маршруту — "
                 f"чтобы не ехать пустым.\n🎁 Доступ до {fmt_date(u.access_until())}.")
    else:
        intro = ("🙋 Буду присылать водителей, у которых есть свободные места по вашему "
                 "маршруту. Бесплатно.")
    await _route_ask_from(cb.message, state, role, f"{TITLE}\n\n{intro}\n\n", edit=True)


# ---------------- фильтры ----------------

REGION_FILTERS = {"r:add:md": MOLDOVA, "r:add:pmr": PMR_ONLY, "r:add:ua": UKRAINE,
                  "r:add:eu": EUROPE, "r:add:all": ALL}


@router.callback_query(F.data == "r:add")
async def add(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    u, _ = await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    await _route_ask_from(cb.message, state, u.role, "", edit=True)
    await cb.answer()


@router.callback_query(F.data == "r:add:regions")
async def add_regions(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await answer_or_edit(cb.message, "Целый регион — все заявки, где он упоминается:", region_kinds(),
                         edit=True)
    await cb.answer()


@router.callback_query(F.data.in_(set(REGION_FILTERS)))
async def add_region(cb: CallbackQuery, rides_db: RidesDB):
    kind = REGION_FILTERS[cb.data]
    u, _ = await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    want = want_for(u.role)
    if kind in {f.kind for f in await rides_db.get_filters(cb.from_user.id, want)}:
        await cb.answer("Такой фильтр уже есть")
    else:
        await rides_db.add_filter(cb.from_user.id, Filter(kind), want)
        await cb.answer("Фильтр добавлен ✅")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


async def _route_ask_from(target: Message, state: FSMContext, role, prefix: str, edit: bool):
    """Шаг 1 фильтра: откуда. Дальше — куда, направление бот определяет сам."""
    q = "Откуда вы едете?" if role == PASSENGER_ROLE else "Откуда вы выезжаете / где забираете пассажиров?"
    await state.clear()
    await state.set_state(RouteForm.from_place)
    rows = places_kb("r:from", allow_any=True).inline_keyboard
    rows.insert(-1, [Btn(text="🗺 Целый регион (ПМР, Молдова, Украина…)", callback_data="r:add:regions")])
    await answer_or_edit(target, f"{prefix}🔔 <b>Фильтр: откуда → куда</b>\n\n📍 <b>{q}</b>\n"
                         "Выберите или напишите город/село.\n"
                         f"<i>Близкие места тоже подойдут: Тирасполь — это ещё Бендеры, Парканы, "
                         f"Слободзея (до {int(NEARBY_KM)} км). Рыбница или Бельцы — уже нет.</i>",
                         InlineKeyboardMarkup(inline_keyboard=rows), edit)


@router.callback_query(F.data == "r:add:route")
async def route_start(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    u = await rides_db.get_user(cb.from_user.id)
    await _route_ask_from(cb.message, state, u.role if u else None, "", edit=True)
    await cb.answer()


async def _route_ask_to(target: Message, state: FSMContext, from_place, edit: bool):
    await state.update_data(from_place=from_place)
    await state.set_state(RouteForm.to_place)
    frm = f"{from_place} и рядом" if from_place else "любой город"
    await answer_or_edit(target, f"Откуда: <b>{frm}</b>\n\n🏁 <b>Куда?</b>\n"
                         "Если направление не важно — «Куда угодно».", to_kb(), edit)


async def _route_save(target: Message, state: FSMContext, db: RidesDB, user, to_spec, both: bool):
    data = await state.get_data()
    u, _ = await db.ensure_user(user.id, user.username, TRIAL_DAYS)
    f = Filter(ROUTE, data.get("from_place"), to_spec, both_ways=both)
    await db.add_filter(user.id, f, want_for(u.role))
    await state.clear()
    await show_home(target, db, user.id, edit=True)


async def _route_ask_dir(target: Message, state: FSMContext, to_place, edit: bool, db: RidesDB, user):
    data = await state.update_data(to_place=to_place)
    frm = data.get("from_place")
    if frm and to_place and frm == to_place:
        await target.answer("Откуда и куда совпадают — выберите другой пункт.")
        return
    if not frm:  # «из любого города → Тирасполь»: обратное направление не нужно
        await _route_save(target, state, db, user, to_place, False)
        return
    direction = direction_label(frm, to_place)
    await answer_or_edit(
        target, f"Маршрут: <b>{frm} → {to_place}</b> (и ближайшие места)"
                + (f"\nНаправление: <b>{direction}</b>" if direction else "") + "\n\nВ обе стороны?",
        kb([[("⇄ Туда и обратно", "r:dir:1"), ("→ Только туда", "r:dir:0")], [("✖️ Отмена", "r:home")]]),
        edit)


@router.callback_query(RouteForm.from_place, F.data.startswith("r:from:"))
async def route_from_btn(cb: CallbackQuery, state: FSMContext):
    val = cb.data.rsplit(":", 1)[1]
    await _route_ask_to(cb.message, state, None if val == "any" else POPULAR[int(val)], edit=True)
    await cb.answer()


@router.message(RouteForm.from_place, F.text)
async def route_from_text(msg: Message, state: FSMContext):
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _route_ask_to(msg, state, place, edit=False)


@router.callback_query(RouteForm.to_place, F.data.startswith("r:to:"))
async def route_to_btn(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    val = cb.data.split(":", 2)[2]
    if val == "any" or val.startswith("@"):
        await cb.answer("Фильтр сохранён ✅")
        await _route_save(cb.message, state, rides_db, cb.from_user, None if val == "any" else val, False)
        return
    await _route_ask_dir(cb.message, state, POPULAR[int(val)], True, rides_db, cb.from_user)
    await cb.answer()


@router.message(RouteForm.to_place, F.text)
async def route_to_text(msg: Message, state: FSMContext, rides_db: RidesDB):
    if msg.text.strip().lower() in {"любой", "любое", "куда угодно", "везде", "все", "всё", "-"}:
        await _route_save(msg, state, rides_db, msg.from_user, None, False)
        return
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _route_ask_dir(msg, state, place, False, rides_db, msg.from_user)


@router.callback_query(RouteForm.to_place, F.data.startswith("r:dir:"))
async def route_save(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    data = await state.get_data()
    await cb.answer("Маршрут сохранён ✅")
    await _route_save(cb.message, state, rides_db, cb.from_user, data.get("to_place"), cb.data == "r:dir:1")


@router.callback_query(F.data == "r:list")
async def list_filters(cb: CallbackQuery, rides_db: RidesDB):
    u = await rides_db.get_user(cb.from_user.id)
    fs = await rides_db.get_filters(cb.from_user.id, want_for(u.role))
    if not fs:
        await cb.answer("Фильтров нет")
        return
    rows = [[(f"🗑 {f.title()}", f"r:del:{f.id}")] for f in fs] + [[("« Назад", "r:home")]]
    await answer_or_edit(cb.message, "Нажмите на фильтр, чтобы удалить его:", kb(rows), edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("r:del:"))
async def delete_filter(cb: CallbackQuery, rides_db: RidesDB):
    await rides_db.delete_filter(cb.from_user.id, int(cb.data.rsplit(":", 1)[1]))
    u = await rides_db.get_user(cb.from_user.id)
    if await rides_db.get_filters(cb.from_user.id, want_for(u.role)):
        await list_filters(cb, rides_db)
    else:
        await cb.answer("Удалено")
        await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


@router.callback_query(F.data == "r:toggle")
async def toggle(cb: CallbackQuery, rides_db: RidesDB):
    u = await rides_db.get_user(cb.from_user.id)
    await rides_db.set_active(cb.from_user.id, not u.active)
    await cb.answer("Включено" if not u.active else "Пауза")
    await show_home(cb.message, rides_db, cb.from_user.id, edit=True)


# ---------------- лента актуальных объявлений ----------------

@router.callback_query(F.data.startswith("r:feed:"))
async def feed(cb: CallbackQuery, rides_db: RidesDB):
    u, _ = await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    await cb.answer()
    if u.role != PASSENGER_ROLE and not u.has_access():
        await cb.message.answer(f"⛔️ Пробный доступ закончился.\n\n💳 {html.escape(PAYMENT_TEXT)}",
                                reply_markup=kb([[("« Меню", "r:home")]]))
        return
    want = want_for(u.role)
    offset = int(cb.data.rsplit(":", 1)[1])
    filters = await rides_db.get_filters(cb.from_user.id, want)
    posts = [p for p in await rides_db.live_posts(want)
             if p.get("author_id") != cb.from_user.id
             and (not filters or any(matches(f, post_to_parsed(p)) for f in filters))]
    what = "заявок пассажиров" if want == PASSENGER else "свободных машин"
    if not posts:
        hint = " по вашим фильтрам" if filters else ""
        extra = ("\n\nРазместите заявку «📢 Хочу поехать» — водители увидят её сразу."
                 if u.role == PASSENGER_ROLE else "")
        await cb.message.answer(f"Сейчас нет актуальных {what}{hint} 🤷{extra}",
                                reply_markup=kb([[("« Меню", "r:home")]]))
        return
    page = posts[offset:offset + FEED_PAGE]
    for p in page:
        await cb.message.answer(format_post(p, show_age=True), reply_markup=post_keyboard(p),
                                parse_mode="HTML", disable_web_page_preview=True)
    shown = offset + len(page)
    nav = []
    if shown < len(posts):
        nav.append((f"Ещё ({len(posts) - shown}) →", f"r:feed:{shown}"))
    nav.append(("« Меню", "r:home"))
    scope = "по вашим фильтрам" if filters else "все (фильтров нет)"
    await cb.message.answer(
        f"Показано {shown} из {len(posts)} актуальных {what} · {scope}.\n"
        "Сначала ближайшие по времени, прошедшие скрыты.", reply_markup=kb([nav]))


# ---------------- своё объявление ----------------

@router.callback_query(F.data == "r:post")
async def post_start(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB):
    u, _ = await rides_db.ensure_user(cb.from_user.id, cb.from_user.username, TRIAL_DAYS)
    if len(await rides_db.my_posts(cb.from_user.id)) >= MAX_ACTIVE_POSTS:
        await cb.answer(f"Не больше {MAX_ACTIVE_POSTS} активных объявлений. Снимите старое в «Мои».",
                        show_alert=True)
        return
    await state.clear()
    await state.update_data(kind=PASSENGER if u.role == PASSENGER_ROLE else DRIVER)
    await state.set_state(PostForm.from_place)
    title = "📢 <b>Заявка: хочу поехать</b>" if u.role == PASSENGER_ROLE else "📢 <b>Есть свободная машина</b>"
    await answer_or_edit(cb.message, f"{title}\n\n📍 <b>Откуда?</b> Выберите или напишите.",
                         places_kb("r:pf", allow_any=False), edit=True)
    await cb.answer()


async def _post_ask_to(target: Message, state: FSMContext, place: str, edit: bool):
    await state.update_data(from_place=place)
    await state.set_state(PostForm.to_place)
    await answer_or_edit(target, f"Откуда: <b>{place}</b>\n\n🏁 <b>Куда?</b> Выберите или напишите.",
                         places_kb("r:pt", allow_any=False), edit)


async def _post_ask_when(target: Message, state: FSMContext, place: str, edit: bool):
    data = await state.get_data()
    if place == data.get("from_place"):
        await target.answer("Откуда и куда совпадают — выберите другой пункт.")
        return
    await state.update_data(to_place=place)
    await state.set_state(PostForm.when)
    await answer_or_edit(
        target,
        f"📍 <b>{data['from_place']} → {place}</b>\n\n🕐 <b>Когда?</b>\n"
        "Нажмите кнопку или напишите: «19:00», «завтра 7:30», «05.10 18:00».",
        kb([[("🔥 Сейчас", "r:pw:now")],
            [("Сегодня (время любое)", "r:pw:today"), ("Завтра (время любое)", "r:pw:tomorrow")],
            [("✖️ Отмена", "r:home")]]), edit)


@router.callback_query(PostForm.from_place, F.data.startswith("r:pf:"))
async def post_from_btn(cb: CallbackQuery, state: FSMContext):
    await _post_ask_to(cb.message, state, POPULAR[int(cb.data.rsplit(":", 1)[1])], edit=True)
    await cb.answer()


@router.message(PostForm.from_place, F.text)
async def post_from_text(msg: Message, state: FSMContext):
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _post_ask_to(msg, state, place, edit=False)


@router.callback_query(PostForm.to_place, F.data.startswith("r:pt:"))
async def post_to_btn(cb: CallbackQuery, state: FSMContext):
    await _post_ask_when(cb.message, state, POPULAR[int(cb.data.rsplit(":", 1)[1])], edit=True)
    await cb.answer()


@router.message(PostForm.to_place, F.text)
async def post_to_text(msg: Message, state: FSMContext):
    place = resolve_place(msg.text)
    if not place:
        await msg.answer("Не нашёл такой населённый пункт 🤔 Попробуйте иначе или выберите кнопкой.")
        return
    await _post_ask_when(msg, state, place, edit=False)


async def _post_ask_count(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    await state.set_state(PostForm.count)
    q = "👤 <b>Сколько человек едет?</b>" if data["kind"] == PASSENGER else "💺 <b>Сколько свободных мест?</b>"
    await answer_or_edit(target, q, kb([[(str(n), f"r:pn:{n}") for n in range(1, 5)] + [("5+", "r:pn:5")],
                                        [("✖️ Отмена", "r:home")]]), edit)


@router.callback_query(PostForm.when, F.data.startswith("r:pw:"))
async def post_when_btn(cb: CallbackQuery, state: FSMContext):
    when = {"now": "сейчас", "today": "сегодня", "tomorrow": "завтра"}[cb.data.rsplit(":", 1)[1]]
    await state.update_data(when=when, date=None, time=None)
    await _post_ask_count(cb.message, state, edit=True)
    await cb.answer()


@router.message(PostForm.when, F.text)
async def post_when_text(msg: Message, state: FSMContext):
    text = msg.text.strip()
    m = re.fullmatch(r"(\d{1,2})(?:[\s:.](\d{2}))?", text)  # «19», «19 00», «7.30»
    if m and int(m.group(1)) < 24:
        text = f"в {int(m.group(1))}:{m.group(2) or '00'}"
    p = parse_message(text)
    if not (p.time or p.date or p.when):
        await msg.answer("Не понял время 🤔 Напишите, например: «19:00», «завтра 7:30» или «05.10».")
        return
    await state.update_data(when=p.when, date=p.date, time=p.time)
    await _post_ask_count(msg, state, edit=False)


@router.callback_query(PostForm.count, F.data.startswith("r:pn:"))
async def post_count(cb: CallbackQuery, state: FSMContext):
    await state.update_data(count=int(cb.data.rsplit(":", 1)[1]))
    await state.set_state(PostForm.comment)
    await answer_or_edit(cb.message, "💬 <b>Комментарий</b> (необязательно)\n"
                         "Цена, багаж, место встречи, животные… Напишите одним сообщением.",
                         kb([[("Пропустить →", "r:pc:skip")], [("✖️ Отмена", "r:home")]]), edit=True)
    await cb.answer()


async def _post_ask_contact(target: Message, state: FSMContext, user):
    await state.set_state(PostForm.contact)
    tg = f"@{user.username}" if user.username else "Telegram"
    note = "" if user.username else ("\n⚠️ У вас нет @username — написать смогут не все. "
                                     "Лучше добавьте номер телефона.")
    await target.answer(
        f"📞 <b>Как с вами связаться?</b>\nВ объявлении будет ваш {tg}.{note}",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Добавить мой номер телефона", request_contact=True)],
                      [KeyboardButton(text="Только Telegram")]],
            resize_keyboard=True, one_time_keyboard=True))


@router.callback_query(PostForm.comment, F.data == "r:pc:skip")
async def post_comment_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(comment=None)
    await _post_ask_contact(cb.message, state, cb.from_user)
    await cb.answer()


@router.message(PostForm.comment, F.text)
async def post_comment_text(msg: Message, state: FSMContext):
    await state.update_data(comment=msg.text.strip()[:300])
    await _post_ask_contact(msg, state, msg.from_user)


def _draft(data: dict, user) -> dict:
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or None
    return post_from_bot(kind=data["kind"], from_place=data["from_place"], to_place=data["to_place"],
                         when=data.get("when"), date=data.get("date"), time_str=data.get("time"),
                         count=data.get("count"), comment=data.get("comment"), user_id=user.id,
                         username=user.username, name=name, phone=data.get("phone"))


async def _post_confirm(msg: Message, state: FSMContext):
    await state.set_state(PostForm.confirm)
    await msg.answer("Проверьте объявление 👇", reply_markup=ReplyKeyboardRemove())
    draft = _draft(await state.get_data(), msg.from_user)
    who = "водители" if draft["kind"] == PASSENGER else "пассажиры"
    await msg.answer(format_post(draft) + f"\n\n<i>Его сразу получат {who} с подходящим фильтром, "
                     "и оно будет в ленте, пока актуально.</i>", parse_mode="HTML",
                     reply_markup=kb([[("✅ Опубликовать", "r:pok")], [("✖️ Отмена", "r:home")]]))


@router.message(PostForm.contact, F.contact)
async def post_contact_phone(msg: Message, state: FSMContext):
    if msg.contact.user_id and msg.contact.user_id != msg.from_user.id:
        await msg.answer("Отправьте свой номер кнопкой «📱 Добавить мой номер телефона».")
        return
    phone = msg.contact.phone_number
    await state.update_data(phone=phone if phone.startswith("+") else "+" + phone)
    await _post_confirm(msg, state)


@router.message(PostForm.contact, F.text)
async def post_contact_text(msg: Message, state: FSMContext):
    await state.update_data(phone=None)
    await _post_confirm(msg, state)


@router.callback_query(PostForm.confirm, F.data == "r:pok")
async def post_publish(cb: CallbackQuery, state: FSMContext, rides_db: RidesDB, notifier):
    data = await state.get_data()
    await state.clear()
    if len(await rides_db.my_posts(cb.from_user.id)) >= MAX_ACTIVE_POSTS:
        await cb.answer(f"Не больше {MAX_ACTIVE_POSTS} активных объявлений.", show_alert=True)
        return
    post = await rides_db.save_post(_draft(data, cb.from_user))
    await cb.answer("Опубликовано ✅")
    sent = await notifier.dispatch(post)

    # Автор сам получит встречные объявления по этому маршруту
    u = await rides_db.get_user(cb.from_user.id)
    want = want_for(u.role)
    auto = ""
    if not any(matches(f, post_to_parsed(post)) for f in await rides_db.get_filters(cb.from_user.id, want)):
        route = Filter(ROUTE, post["from_place"], post["to_place"], both_ways=False)
        await rides_db.add_filter(cb.from_user.id, route, want)
        auto = (f"\n🔔 Добавил фильтр {html.escape(route.title())} — пришлю подходящие "
                f"{'машины' if want == DRIVER else 'заявки'}.")
    who = "водителям" if post["kind"] == PASSENGER else "пассажирам"
    until = trip_label(post["expires_at"], True, None)
    await answer_or_edit(
        cb.message,
        f"✅ <b>Опубликовано</b>\n📍 {post['from_place']} → {post['to_place']}\n"
        f"Отправлено {who}: {sent}. В ленте до {until}.{auto}",
        kb([[("🔎 Смотреть " + ("машины" if post["kind"] == PASSENGER else "заявки"), "r:feed:0")],
            [("🗂 Мои объявления", "r:my"), ("« Меню", "r:home")]]), edit=True)


@router.callback_query(F.data == "r:my")
async def my_posts(cb: CallbackQuery, rides_db: RidesDB):
    posts = await rides_db.my_posts(cb.from_user.id)
    await cb.answer()
    if not posts:
        await answer_or_edit(cb.message, "У вас нет активных объявлений.",
                             kb([[("📢 Разместить", "r:post")], [("« Меню", "r:home")]]), edit=True)
        return
    rows = [[(f"🗑 {p['from_place']} → {p['to_place']}, "
              f"{trip_label(p['trip_at'], p['has_time'], p['when_label'])}", f"r:pdel:{p['id']}")]
            for p in posts] + [[("« Меню", "r:home")]]
    await answer_or_edit(cb.message, "🗂 <b>Ваши активные объявления</b>\n"
                         "Нажмите, чтобы снять (если уже нашли машину или пассажиров):",
                         kb(rows), edit=True)


@router.callback_query(F.data.startswith("r:pdel:"))
async def close_post(cb: CallbackQuery, rides_db: RidesDB):
    await rides_db.close_post(cb.from_user.id, int(cb.data.rsplit(":", 1)[1]))
    await my_posts(cb, rides_db)


# ---------------- помощь ----------------

HELP_DRIVER = (
    "<b>🚗 Для водителя</b>\n"
    "🔎 <b>Актуальные заявки</b> — люди, которые ищут машину: и те, кто написал до того, как вы "
    "открыли бота, и заявки на будущее время («на 19:00»). Прошедшие скрываются.\n"
    "🔔 <b>Фильтры</b> — новые заявки приходят сразу: 🛣 маршрут, 🇲🇩 Молдова, 🔴 ПМР, "
    "🇺🇦 Украина, 🇪🇺 Европа.\n"
    "📢 <b>Есть свободная машина</b> — объявление увидят пассажиры.\n\n"
    "Кто быстрее ответит — тот и везёт 😉"
)
HELP_PASSENGER = (
    "<b>🙋 Для пассажира</b> — бесплатно\n"
    "🔎 <b>Свободные машины</b> — водители и перевозчики, которые едут по вашему маршруту.\n"
    "📢 <b>Хочу поехать</b> — ваша заявка сразу уходит водителям с подходящим маршрутом.\n"
    "🔔 <b>Фильтры</b> — пришлю новые машины, как только появятся.\n\n"
    "О цене и месте встречи договаривайтесь напрямую с водителем."
)


@router.callback_query(F.data == "r:help")
async def help_cb(cb: CallbackQuery, rides_db: RidesDB):
    u = await rides_db.get_user(cb.from_user.id)
    if u and u.role == PASSENGER_ROLE:
        text = HELP_PASSENGER
    else:
        text = HELP_DRIVER + f"\n\n💳 {html.escape(PAYMENT_TEXT)}"
    await answer_or_edit(cb.message, text + "\n\n/start — главное меню", kb([[("« Назад", "r:home")]]),
                         edit=True)
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
