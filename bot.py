# language: Python, file: bot.py
# ҚИСМИ 1/5: сарлавҳа, import, FSM, клавиатураҳо

import asyncio
import io
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import db
import sender
from config import (
    BOT_TOKEN, API_ID, API_HASH,
    DEFAULT_DELAY, MIN_DELAY, MAX_DELAY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# =========================================================
# FSM — ҳолатҳои корбар ва админ
# =========================================================

class Auth(StatesGroup):
    phone = State()
    code = State()
    password = State()


class Broadcast(StatesGroup):
    text = State()
    confirm = State()


class TimerS(StatesGroup):
    text = State()
    when = State()


class AdminS(StatesGroup):
    # админҳо
    add_admin_id = State()
    add_user_id = State()
    # ситора
    give_stars_id = State()
    give_stars_amount = State()
    # нархҳо
    set_price_key = State()
    set_price_value = State()
    # обунаи ҳатмӣ
    fs_input = State()
    # рассылка аз номи корбар
    pick_user = State()
    msg_text = State()
    confirm_send = State()


# =========================================================
# Клавиатураҳо
# =========================================================

def format_delay(sec: int) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    if s:
        return f"{m} дақиқа {s} сония"
    return f"{m} дақиқа"


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔗 Пайваст кардани аккаунт", callback_data="connect")],
        [InlineKeyboardButton(text="📋 Рӯйхати гурухҳо", callback_data="groups")],
        [InlineKeyboardButton(text="📢 Фиристодани рассылка", callback_data="broadcast")],
        [InlineKeyboardButton(text="⏰ Таймер-рассылка", callback_data="timer")],
        [InlineKeyboardButton(text="⚙️ Танзимоти фосила", callback_data="delay")],
        [InlineKeyboardButton(text="⭐ Ситораҳои ман", callback_data="stars")],
        [InlineKeyboardButton(text="✍️ Нест кардани имзо", callback_data="unsign")],
        [InlineKeyboardButton(text="🔄 Навсозии маълумот", callback_data="refresh")],
    ]
    if db.is_admin(uid):
        rows.append([InlineKeyboardButton(text="👑 Админ-панел", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(target="main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data=target)]
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Корбарон", callback_data="a_users")],
        [InlineKeyboardButton(text="➕ Илова кардани корбар", callback_data="a_add_user")],
        [InlineKeyboardButton(text="➕ Илова кардани админ", callback_data="a_add_admin")],
        [InlineKeyboardButton(text="👑 Рӯйхати админҳо", callback_data="a_admins")],
        [InlineKeyboardButton(text="⭐ Додани ситора", callback_data="a_give_stars")],
        [InlineKeyboardButton(text="💰 Нархҳои функсияҳо", callback_data="a_prices")],
        [InlineKeyboardButton(text="🔔 Обунаи ҳатмӣ", callback_data="a_forcesub")],
        [InlineKeyboardButton(text="📨 Рассылка аз номи корбар", callback_data="a_user_msg")],
        [InlineKeyboardButton(text="📣 Рассылкаи умумӣ", callback_data="a_broadcast_all")],
        [InlineKeyboardButton(text="📤 Экспорт ба CSV", callback_data="a_export")],
        [InlineKeyboardButton(text="🔄 Навсозии корбар", callback_data="a_refresh_user")],
        [InlineKeyboardButton(text="📊 Омор", callback_data="a_stats")],
        [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="main")],
    ])


def admin_only(uid: int) -> bool:
    return db.is_admin(uid)

# language: Python, file: bot.py
# ҚИСМИ 2/5: /start, пайвасткунӣ, қатъкунӣ

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    db.upsert_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name)

    # санҷиши обунаи ҳатмӣ
    ok, missing = await check_force_subs(msg.from_user.id)
    if not ok:
        await msg.answer(
            "🔒 <b>Барои истифода бот, аввал обуна шавед:</b>\n\n"
            + "\n".join(f"• {m}" for m in missing),
            reply_markup=force_sub_kb(missing),
        )
        return

    u = db.get_user(msg.from_user.id)
    await msg.answer(_profile_text(u, msg.from_user.first_name),
                     reply_markup=main_menu(msg.from_user.id))


def _profile_text(u: dict, name: str) -> str:
    if not u:
        return "Салом!"
    status = "✅ пайваст" if u.get("connected") else "❌ пайваст нест"
    sig = "хомӯш" if u.get("unsigned") else "фаъол"
    return (
        f"<b>Салом, {name}!</b>\n\n"
        f"Ҳолат: {status}\n"
        f"Фосила: {format_delay(u.get('delay') or DEFAULT_DELAY)}\n"
        f"Ситораҳо: {u.get('stars', 0)} ⭐\n"
        f"Имзо: {sig}\n\n"
        f"Аз меню интихоб кун:"
    )


@dp.callback_query(F.data == "main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = db.get_user(cb.from_user.id)
    await cb.message.edit_text(_profile_text(u, cb.from_user.first_name),
                               reply_markup=main_menu(cb.from_user.id))
    await cb.answer()


# ---------- Пайваст ----------

@dp.callback_query(F.data == "connect")
async def cb_connect(cb: CallbackQuery, state: FSMContext):
    u = db.get_user(cb.from_user.id)
    if u and u.get("connected"):
        await cb.message.edit_text(
            f"✅ Аккаунт аллакай пайваст аст.\n"
            f"Телефон: <code>{u.get('phone')}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔌 Қатъ кардан", callback_data="disconnect")],
                [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="main")],
            ]),
        )
        await cb.answer()
        return

    # санҷиши нарх
    price = db.get_price("connect_price", 0)
    is_adm = db.is_admin(cb.from_user.id)
    if not is_adm and price > 0:
        if db.get_stars(cb.from_user.id) < price:
            await cb.answer(f"Ситора кофӣ нест. Лозим: {price} ⭐", show_alert=True)
            return
        db.spend_stars(cb.from_user.id, price)

    await cb.message.edit_text(
        "📱 <b>Пайваст кардани аккаунт</b>\n\n"
        "Рақами телефонро бо формати байналмилалӣ фирист:\n"
        "мисол: <code>+992901234567</code>",
        reply_markup=back_kb(),
    )
    await state.set_state(Auth.phone)
    await cb.answer()


@dp.callback_query(F.data == "disconnect")
async def cb_disconnect(cb: CallbackQuery):
    db.disconnect_user(cb.from_user.id)
    await cb.message.edit_text("🔌 Аккаунт қатъ шуд.", reply_markup=back_kb())
    await cb.answer()


@dp.message(Auth.phone)
async def auth_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith("+") or len(phone) < 8:
        await msg.answer("❌ Рақам нодуруст. Мисол: <code>+992901234567</code>")
        return

    client = TelegramClient(sender.session_path(msg.from_user.id), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        await msg.answer("📨 Рамз фиристода шуд. Рамзро нависед:")
        await state.set_state(Auth.code)
    except Exception as e:
        await msg.answer(f"❌ Хатогӣ: <code>{e}</code>", reply_markup=back_kb())
        await state.clear()
    finally:
        await client.disconnect()


@dp.message(Auth.code)
async def auth_code(msg: Message, state: FSMContext):
    code = msg.text.replace(" ", "").strip()
    data = await state.get_data()

    client = TelegramClient(sender.session_path(msg.from_user.id), API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone=data["phone"], code=code,
                             phone_code_hash=data["phone_code_hash"])
    except SessionPasswordNeededError:
        await msg.answer("🔐 2FA лозим аст. Паролро фирист:")
        await state.set_state(Auth.password)
        await client.disconnect()
        return
    except Exception as e:
        await msg.answer(f"❌ Хатогӣ: <code>{e}</code>", reply_markup=back_kb())
        await state.clear()
        await client.disconnect()
        return

    me = await client.get_me()
    db.set_session(msg.from_user.id, data["phone"], sender.session_path(msg.from_user.id))
    db.upsert_user(msg.from_user.id, me.username, me.first_name)
    await client.disconnect()

    await msg.answer(f"✅ Пайваст шуд: {me.first_name}", reply_markup=back_kb())
    await state.clear()


@dp.message(Auth.password)
async def auth_password(msg: Message, state: FSMContext):
    data = await state.get_data()
    client = TelegramClient(sender.session_path(msg.from_user.id), API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(password=msg.text.strip())
        me = await client.get_me()
        db.set_session(msg.from_user.id, data["phone"], sender.session_path(msg.from_user.id))
        db.upsert_user(msg.from_user.id, me.username, me.first_name)
        await msg.answer("✅ Пайваст шуд", reply_markup=back_kb())
        await state.clear()
    except Exception as e:
        await msg.answer(f"❌ Парол нодуруст: <code>{e}</code>")
    finally:
        await client.disconnect()

  # language: Python, file: bot.py
# ҚИСМИ 3/5: гурухҳо, рассылка, фосила, ситора, имзо, навсозӣ

# ---------- Гурухҳо ----------

@dp.callback_query(F.data == "groups")
async def cb_groups(cb: CallbackQuery):
    u = db.get_user(cb.from_user.id)
    if not u or not u.get("connected"):
        await cb.answer("Аввал аккаунтро пайваст кун!", show_alert=True)
        return

    # санҷиши нарх
    price = db.get_price("fetch_groups_price", 0)
    is_adm = db.is_admin(cb.from_user.id)
    if not is_adm and price > 0:
        if db.get_stars(cb.from_user.id) < price:
            await cb.answer(f"Ситора кофӣ нест. Лозим: {price} ⭐", show_alert=True)
            return
        db.spend_stars(cb.from_user.id, price)

    await cb.message.edit_text("⏳ Гурухҳоро мехонам...")
    await cb.answer()
    try:
        await sender.fetch_user_groups(cb.from_user.id)
    except Exception as e:
        await cb.message.edit_text(f"❌ Хатогӣ: <code>{e}</code>", reply_markup=back_kb())
        return
    await _render_groups(cb.message, cb.from_user.id)


async def _render_groups(message, uid):
    groups = db.list_groups(uid)
    if not groups:
        await message.edit_text("Гурух ёфт нашуд.", reply_markup=back_kb())
        return
    kb = []
    for g in groups:
        mark = "🟢" if g["selected"] else "⚪"
        kb.append([InlineKeyboardButton(
            text=f"{mark} {(g['title'] or '—')[:40]}",
            callback_data=f"tg_{g['group_id']}")])
    kb.append([InlineKeyboardButton(text="✅ Тайёр", callback_data="main")])
    await message.edit_text(
        f"<b>Гурухҳои ту ({len(groups)})</b>\n\n🟢 добавлено · ⚪ хомӯш",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@dp.callback_query(F.data.startswith("tg_"))
async def cb_toggle_group(cb: CallbackQuery):
    gid = int(cb.data.split("_", 1)[1])
    new = db.toggle_group(cb.from_user.id, gid)
    if new is None:
        await cb.answer("Гурух ёфт нашуд", show_alert=True)
        return
    await cb.answer("✅ Добавлено" if new else "⚪ Хомӯш")
    await _render_groups(cb.message, cb.from_user.id)


# ---------- Рассылка ----------

@dp.callback_query(F.data == "broadcast")
async def cb_broadcast(cb: CallbackQuery, state: FSMContext):
    u = db.get_user(cb.from_user.id)
    if not u or not u.get("connected"):
        await cb.answer("Аввал аккаунтро пайваст кун!", show_alert=True)
        return
    sel = db.selected_groups(cb.from_user.id)
    if not sel:
        await cb.answer("Ҳеҷ гурух интихоб нашудааст!", show_alert=True)
        return

    # санҷиши нарх
    price = db.get_price("broadcast_price", 0)
    is_adm = db.is_admin(cb.from_user.id)
    if not is_adm and price > 0:
        if db.get_stars(cb.from_user.id) < price:
            await cb.answer(f"Ситора кофӣ нест. Лозим: {price} ⭐", show_alert=True)
            return
        db.spend_stars(cb.from_user.id, price)

    await cb.message.edit_text(
        f"📢 <b>Рассылка</b>\n\n"
        f"Гурухҳо: {len(sel)}\n"
        f"Фосила: {format_delay(u.get('delay') or DEFAULT_DELAY)}\n\n"
        f"Матни паёмро фирист:",
        reply_markup=back_kb(),
    )
    await state.set_state(Broadcast.text)
    await cb.answer()


@dp.message(Broadcast.text)
async def broadcast_text(msg: Message, state: FSMContext):
    text = (msg.text or msg.caption or "").strip()
    if not text:
        await msg.answer("Матн холӣ аст.")
        return
    await state.update_data(text=text)
    u = db.get_user(msg.from_user.id)
    sel = db.selected_groups(msg.from_user.id)
    preview = text + ("" if u.get("unsigned") else "\n\nотправлено через @jingizbot")
    await msg.answer(
        f"<b>Пешнамоиш:</b>\n\n{preview}\n\n"
        f"→ {len(sel)} гурух. Фосила: {format_delay(u.get('delay') or DEFAULT_DELAY)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Оғоз", callback_data="b_start")],
            [InlineKeyboardButton(text="❌ Бекор", callback_data="main")],
        ]),
    )
    await state.set_state(Broadcast.confirm)


@dp.callback_query(F.data == "b_start", Broadcast.confirm)
async def broadcast_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("text", "")
    await state.clear()
    status = cb.message
    await cb.message.edit_text("🚀 Рассылка оғоз шуд...")
    await cb.answer()

    async def progress(i, total, title):
        try:
            await status.edit_text(f"🚀 [{i}/{total}] {title[:50]}")
        except Exception:
            pass

    res = await sender.send_campaign(cb.from_user.id, text, progress_cb=progress)
    if not res.get("ok"):
        await status.edit_text(f"❌ {res.get('reason')}", reply_markup=back_kb())
        return
    await status.edit_text(
        f"✅ Тамом!\n\n✅ {res['sent']}  ✗ {res['failed']}  / {res['total']}",
        reply_markup=back_kb(),
    )


# ---------- Фосила ----------

@dp.callback_query(F.data == "delay")
async def cb_delay(cb: CallbackQuery):
    u = db.get_user(cb.from_user.id)
    cur = (u.get("delay") or DEFAULT_DELAY) if u else DEFAULT_DELAY
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="2 дақиқа (хатар!)", callback_data="d_120")],
        [InlineKeyboardButton(text="3 дақиқа", callback_data="d_180")],
        [InlineKeyboardButton(text="5 дақиқа (стандарт)", callback_data="d_300")],
        [InlineKeyboardButton(text="10 дақиқа", callback_data="d_600")],
        [InlineKeyboardButton(text="15 дақиқа", callback_data="d_900")],
        [InlineKeyboardButton(text="20 дақиқа (бехатар)", callback_data="d_1200")],
        [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="main")],
    ])
    await cb.message.edit_text(
        f"⚙️ <b>Фосила</b>\n\nҲозира: {format_delay(cur)}\n"
        f"Ҳудуд: 2 – 20 дақиқа\n\n"
        f"⚠️ Ҳарчи камтар — хатари спам-бан зиёдтар.",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("d_"))
async def cb_set_delay(cb: CallbackQuery):
    sec = int(cb.data.split("_")[1])
    if sec < MIN_DELAY or sec > MAX_DELAY:
        await cb.answer("Хориҷ аз ҳудуд", show_alert=True)
        return
    db.set_delay(cb.from_user.id, sec)
    warn = ""
    if sec <= 180:
        warn = "\n\n⚠️ <b>Огоҳӣ:</b> фосилаи кам = хатари баланди спам-бан."
    await cb.message.edit_text(
        f"✅ Фосила: <b>{format_delay(sec)}</b>{warn}",
        reply_markup=back_kb(),
    )
    await cb.answer()


# ---------- Ситора ва имзо ----------

@dp.callback_query(F.data == "stars")
async def cb_stars(cb: CallbackQuery):
    u = db.get_user(cb.from_user.id)
    price = db.get_price("unsign_price", 100)
    await cb.message.edit_text(
        f"⭐ <b>Ситораҳои ту: {u.get('stars', 0)}</b>\n\n"
        f"Нархи нест кардани имзо: {price} ⭐\n"
        f"Ҳолати имзо: {'хомӯш ✅' if u.get('unsigned') else 'фаъол'}",
        reply_markup=back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "unsign")
async def cb_unsign(cb: CallbackQuery):
    u = db.get_user(cb.from_user.id)
    is_adm = db.is_admin(cb.from_user.id)

    if u.get("unsigned"):
        db.set_unsigned(cb.from_user.id, 0)
        await cb.message.edit_text(
            "✍️ Имзо барқарор шуд: <i>отправлено через @jingizbot</i>",
            reply_markup=back_kb())
        await cb.answer()
        return

    price = db.get_price("unsign_price", 100)
    if not is_adm and price > 0:
        if u.get("stars", 0) < price:
            await cb.message.edit_text(
                f"❌ Ситора кофӣ нест.\n\nЛозим: {price} ⭐\nДорад: {u.get('stars',0)} ⭐",
                reply_markup=back_kb())
            await cb.answer()
            return
        db.spend_stars(cb.from_user.id, price)

    db.set_unsigned(cb.from_user.id, 1)
    await cb.message.edit_text(
        f"✅ Имзо нест шуд." + (f" {price} ⭐ сарф шуд." if not is_adm and price > 0 else ""),
        reply_markup=back_kb())
    await cb.answer()


# ---------- Навсозӣ ----------

@dp.callback_query(F.data == "refresh")
async def cb_refresh(cb: CallbackQuery):
    u = db.get_user(cb.from_user.id)
    if not u or not u.get("connected"):
        await cb.answer("Аввал аккаунтро пайваст кун!", show_alert=True)
        return

    price = db.get_price("groups_refresh_price", 0)
    is_adm = db.is_admin(cb.from_user.id)
    if not is_adm and price > 0:
        if db.get_stars(cb.from_user.id) < price:
            await cb.answer(f"Ситора кофӣ нест. Лозим: {price} ⭐", show_alert=True)
            return
        db.spend_stars(cb.from_user.id, price)

    await cb.message.edit_text("🔄 Навсозӣ...")
    await cb.answer()
    try:
        info = await sender.refresh_user_info(cb.from_user.id)
        await sender.fetch_user_groups(cb.from_user.id)
        await cb.message.edit_text(
            f"✅ Навсозӣ шуд.\n\nНом: {info['first_name']}\n@{info['username']}",
            reply_markup=back_kb(),
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ {e}", reply_markup=back_kb())

  # language: Python, file: bot.py
# ҚИСМИ 4/5: админ-панел

@dp.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        await cb.answer("Ту админ нестӣ", show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text("👑 <b>Админ-панел</b>", reply_markup=admin_menu())
    await cb.answer()


@dp.callback_query(F.data == "a_stats")
async def a_stats(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    c = db.count_users()
    prices = db.all_prices()
    price_lines = "\n".join(f"• {k}: {v} ⭐" for k, v in prices.items())
    await cb.message.edit_text(
        f"📊 <b>Омор</b>\n\n"
        f"Ҳамагӣ: {c['total']}\n"
        f"Пайвастшуда: {c['connected']}\n\n"
        f"<b>Нархҳо:</b>\n{price_lines}",
        reply_markup=back_kb("admin"),
    )
    await cb.answer()


# ---------- Корбарон ----------

@dp.callback_query(F.data == "a_users")
async def a_users(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    users = db.list_users(limit=20)
    if not users:
        await cb.message.edit_text("Корбар нест.", reply_markup=back_kb("admin"))
        return
    lines = []
    for u in users:
        mark = "✅" if u.get("connected") else "❌"
        lines.append(f"{mark} <code>{u['user_id']}</code> — {u.get('first_name') or '—'}")
    await cb.message.edit_text(
        f"👥 <b>20 корбари охирин</b>\n\n" + "\n".join(lines),
        reply_markup=back_kb("admin"),
    )
    await cb.answer()


@dp.callback_query(F.data == "a_add_user")
async def a_add_user(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    await cb.message.edit_text(
        "➕ ID-и корбарро фирист (рақам):\n\n"
        "Корбар дар база сабт мешавад. Барои рассылка худаш аккаунтро пайваст кунад.",
        reply_markup=back_kb("admin"))
    await state.set_state(AdminS.add_user_id)
    await cb.answer()


@dp.message(AdminS.add_user_id)
async def a_add_user_id(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("ID рақам нест.")
        return
    db.add_user_manual(uid, None, None, added_by=msg.from_user.id)
    await msg.answer(f"✅ Корбар илова шуд: <code>{uid}</code>",
                     reply_markup=back_kb("admin"))
    await state.clear()


# ---------- Админҳо ----------

@dp.callback_query(F.data == "a_add_admin")
async def a_add_admin(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    await cb.message.edit_text("➕ ID-и корбари навро барои админ фирист:",
                               reply_markup=back_kb("admin"))
    await state.set_state(AdminS.add_admin_id)
    await cb.answer()


@dp.message(AdminS.add_admin_id)
async def a_add_admin_id(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("ID рақам нест.")
        return
    db.add_admin(uid, msg.from_user.id)
    await msg.answer(f"✅ Админ: <code>{uid}</code>", reply_markup=back_kb("admin"))
    await state.clear()


@dp.callback_query(F.data == "a_admins")
async def a_admins(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    admins = db.list_admins()
    lines = "\n".join(f"• <code>{a['user_id']}</code>" for a in admins)
    await cb.message.edit_text(f"👑 <b>Админҳо</b>\n\n{lines}",
                               reply_markup=back_kb("admin"))
    await cb.answer()


# ---------- Ситора ----------

@dp.callback_query(F.data == "a_give_stars")
async def a_give_stars(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    await cb.message.edit_text("⭐ ID-и корбарро фирист:", reply_markup=back_kb("admin"))
    await state.set_state(AdminS.give_stars_id)
    await cb.answer()


@dp.message(AdminS.give_stars_id)
async def a_give_stars_id(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("ID рақам нест.")
        return
    if not db.get_user(uid):
        await msg.answer("Корбар ёфт нашуд. Аввал илова кун.")
        return
    await state.update_data(target=uid)
    await msg.answer("Чанд ситора? (рақам)")
    await state.set_state(AdminS.give_stars_amount)


@dp.message(AdminS.give_stars_amount)
async def a_give_stars_amount(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    try:
        amt = int(msg.text.strip())
    except ValueError:
        await msg.answer("Рақам нависед.")
        return
    data = await state.get_data()
    total = db.add_stars(data["target"], amt)
    await msg.answer(f"✅ +{amt} ⭐. Ҳозира: {total} ⭐",
                     reply_markup=back_kb("admin"))
    await state.clear()


# ---------- Нархҳо ----------

@dp.callback_query(F.data == "a_prices")
async def a_prices(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    prices = db.all_prices()
    kb = []
    for key, val in prices.items():
        kb.append([InlineKeyboardButton(
            text=f"{key} → {val} ⭐",
            callback_data=f"price_{key}")])
    kb.append([InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="admin")])
    await cb.message.edit_text(
        "💰 <b>Нархҳои функсияҳо</b>\n\n"
        "Барои тағйир — тугмаро зер кун.\n"
        "0 = ройгон барои ҳама.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("price_"))
async def a_set_price(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    key = cb.data[6:]
    cur = db.get_price(key, 0)
    await state.update_data(price_key=key)
    await cb.message.edit_text(
        f"💰 <b>{key}</b>\n\nҲозира: {cur} ⭐\n\nНархи навро фирист (рақам):",
        reply_markup=back_kb("admin"))
    await state.set_state(AdminS.set_price_value)
    await cb.answer()


@dp.message(AdminS.set_price_value)
async def a_set_price_value(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    try:
        val = int(msg.text.strip())
    except ValueError:
        await msg.answer("Рақам нависед.")
        return
    data = await state.get_data()
    db.set_price(data["price_key"], val)
    await msg.answer(
        f"✅ {data['price_key']} → {val} ⭐",
        reply_markup=back_kb("admin"))
    await state.clear()


# ---------- Обунаи ҳатмӣ ----------

@dp.callback_query(F.data == "a_forcesub")
async def a_forcesub(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    subs = db.list_force_subs()
    lines = ""
    for s in subs:
        lines += f"• {s['title'] or '—'} | <code>{s['chat_id']}</code>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Илова кардан", callback_data="fs_add")],
        [InlineKeyboardButton(text="➖ Нест кардан", callback_data="fs_del")],
        [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="admin")],
    ])
    await cb.message.edit_text(
        f"🔔 <b>Обунаи ҳатмӣ</b>\n\n"
        f"Ҳозира: {len(subs)}\n{lines}\n"
        f"Барои илова кардан, ботро аввал дар канал/гурух админ кун, "
        f"баъд chat_id ё @username-ро фирист.",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "fs_add")
async def a_fs_add(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    await cb.message.edit_text(
        "➕ chat_id ё @username-и канал/гурухро фирист:\n\n"
        "Мисол: <code>@mychannel</code> ё <code>-1001234567890</code>\n\n"
        "⚠️ Бот бояд дар он ҷо админ бошад, то обунаро санҷад.",
        reply_markup=back_kb("admin"))
    await state.set_state(AdminS.fs_input)
    await cb.answer()


@dp.message(AdminS.fs_input)
async def a_fs_input(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    raw = msg.text.strip()
    try:
        if raw.startswith("-") or raw.lstrip("-").isdigit():
            chat = await bot.get_chat(int(raw))
        else:
            uname = raw if raw.startswith("@") else "@" + raw
            chat = await bot.get_chat(uname)
    except Exception as e:
        await msg.answer(f"❌ Ёфт нашуд: <code>{e}</code>")
        return

    invite = None
    if chat.invite_link:
        invite = chat.invite_link

    db.add_force_sub(
        chat_id=chat.id,
        username=chat.username,
        title=chat.title or chat.full_name or str(chat.id),
        invite=invite,
        added_by=msg.from_user.id,
    )
    await msg.answer(
        f"✅ Илова шуд:\n<b>{chat.title or chat.full_name}</b>\n"
        f"ID: <code>{chat.id}</code>",
        reply_markup=back_kb("admin"))
    await state.clear()


@dp.callback_query(F.data == "fs_del")
async def a_fs_del(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    subs = db.list_force_subs()
    if not subs:
        await cb.answer("Холӣ", show_alert=True)
        return
    kb = []
    for s in subs:
        kb.append([InlineKeyboardButton(
            text=f"❌ {s['title'] or s['chat_id']}",
            callback_data=f"fsdel_{s['chat_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="admin")])
    await cb.message.edit_text("Кадомро нест кунам?",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@dp.callback_query(F.data.startswith("fsdel_"))
async def a_fs_del_do(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    chat_id = int(cb.data.split("_", 1)[1])
    db.remove_force_sub(chat_id)
    await cb.message.edit_text("✅ Нест шуд.", reply_markup=back_kb("admin"))
    await cb.answer()


# ---------- Рассылка аз номи корбар ----------

@dp.callback_query(F.data == "a_user_msg")
async def a_user_msg(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    users = db.list_users(limit=200, only_connected=True)
    if not users:
        await cb.answer("Ҳеҷ корбари пайвастшуда нест", show_alert=True)
        return
    kb = []
    for u in users:
        label = f"{(u.get('first_name') or '—')[:20]} | {u['user_id']}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"pick_{u['user_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="admin")])
    await cb.message.edit_text(
        "📨 <b>Рассылка аз номи корбар</b>\n\nКорбарро интихоб кун:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@dp.callback_query(F.data.startswith("pick_"))
async def a_pick_user(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    uid = int(cb.data.split("_", 1)[1])
    u = db.get_user(uid)
    if not u or not u.get("connected"):
        await cb.answer("Корбар пайваст нест", show_alert=True)
        return
    groups = db.selected_groups(uid)
    await state.update_data(target=uid, broadcast_all=False)
    await cb.message.edit_text(
        f"📨 Аз номи: <b>{u.get('first_name') or uid}</b>\n\n"
        f"Гурухҳои интихобшуда: {len(groups)}\n\n"
        f"Матни паёмро фирист:",
        reply_markup=back_kb("admin"))
    await state.set_state(AdminS.msg_text)
    await cb.answer()


# ---------- Рассылкаи умумӣ ----------

@dp.callback_query(F.data == "a_broadcast_all")
async def a_broadcast_all(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    c = db.count_users()
    await cb.message.edit_text(
        f"📣 <b>Рассылкаи умумӣ</b>\n\n"
        f"Ҳамагӣ: {c['total']}\n"
        f"Пайвастшуда: {c['connected']}\n\n"
        f"Ҳар корбар аз номи ХУД ба гурухҳои ХУД мефиристад.\n"
        f"Ин вақт мегирад.\n\n"
        f"Матни паёмро фирист:",
        reply_markup=back_kb("admin"))
    await state.update_data(target=None, broadcast_all=True)
    await state.set_state(AdminS.msg_text)
    await cb.answer()


# ---------- Тасдиқи паёми админ ----------

@dp.message(AdminS.msg_text)
async def a_msg_text(msg: Message, state: FSMContext):
    if not admin_only(msg.from_user.id):
        return
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Матн холӣ.")
        return
    data = await state.get_data()
    await state.update_data(text=text)

    if data.get("broadcast_all"):
        c = db.count_users()
        await msg.answer(
            f"📣 Рассылкаи умумӣ\n\n"
            f"→ {c['connected']} корбар\n\n"
            f"Пешнамоиш:\n{text[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Оғоз", callback_data="a_all_go")],
                [InlineKeyboardButton(text="❌ Бекор", callback_data="admin")],
            ]))
        await state.set_state(AdminS.confirm_send)
        return

    target = data.get("target")
    u = db.get_user(target)
    groups = db.selected_groups(target)
    await msg.answer(
        f"Аз номи <b>{u.get('first_name') or target}</b>\n"
        f"→ {len(groups)} гурух\n\nПешнамоиш:\n{text[:200]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Фиристодан", callback_data="a_send_go")],
            [InlineKeyboardButton(text="❌ Бекор", callback_data="admin")],
        ]))
    await state.set_state(AdminS.confirm_send)


@dp.callback_query(F.data == "a_send_go", AdminS.confirm_send)
async def a_send_go(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    data = await state.get_data()
    target = data["target"]
    text = data["text"]
    await state.clear()
    status = cb.message
    await cb.message.edit_text("🚀 Фиристодан...")
    await cb.answer()

    async def progress(i, total, title):
        try:
            await status.edit_text(f"🚀 [{i}/{total}] {title[:50]}")
        except Exception:
            pass

    res = await sender.send_campaign(target, text, progress_cb=progress)
    if not res.get("ok"):
        await status.edit_text(f"❌ {res.get('reason')}", reply_markup=back_kb("admin"))
        return
    await status.edit_text(
        f"✅ Тамом!\n\n✅ {res['sent']}  ✗ {res['failed']}  / {res['total']}",
        reply_markup=back_kb("admin"))


@dp.callback_query(F.data == "a_all_go", AdminS.confirm_send)
async def a_all_go(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb.from_user.id):
        return
    data = await state.get_data()
    text = data.get("text", "")
    await state.clear()
    status = cb.message
    await cb.message.edit_text("📣 Рассылкаи умумӣ...")
    await cb.answer()

    async def progress(i, total, name):
        try:
            await status.edit_text(f"📣 [{i}/{total}] {name[:40]}")
        except Exception:
            pass

    res = await sender.broadcast_all(text, progress_cb=progress)
    await status.edit_text(
        f"✅ Тамом!\n\nКорбарон: {res['users']}\n"
        f"Паёмҳо: ✅ {res['sent']}  ✗ {res['failed']}",
        reply_markup=back_kb("admin"))


# ---------- Экспорт CSV ----------

@dp.callback_query(F.data == "a_export")
async def a_export(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    await cb.answer("Сохта мешавад...")
    csv_data = db.export_users_csv()
    buf = io.BytesIO(csv_data.encode("utf-8-sig"))
    buf.name = f"users_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    await cb.message.answer_document(buf, caption=f"📤 Корбарон: {db.count_users()['total']}")
    await cb.message.edit_text("✅ Экспорт тайёр шуд.", reply_markup=back_kb("admin"))


# ---------- Навсозии корбар аз админ ----------

@dp.callback_query(F.data == "a_refresh_user")
async def a_refresh_user(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    users = db.list_users(limit=200, only_connected=True)
    if not users:
        await cb.answer("Нест", show_alert=True)
        return
    kb = []
    for u in users:
        kb.append([InlineKeyboardButton(
            text=f"{(u.get('first_name') or '—')[:20]} | {u['user_id']}",
            callback_data=f"rf_{u['user_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="admin")])
    await cb.message.edit_text("🔄 Кадом корбарро навсозӣ кунам?",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@dp.callback_query(F.data.startswith("rf_"))
async def a_do_refresh(cb: CallbackQuery):
    if not admin_only(cb.from_user.id):
        return
    uid = int(cb.data.split("_", 1)[1])
    await cb.message.edit_text("🔄 Навсозӣ...")
    await cb.answer()
    try:
        await sender.refresh_user_info(uid)
        await sender.fetch_user_groups(uid)
        await cb.message.edit_text("✅ Навсозӣ шуд", reply_markup=back_kb("admin"))
    except Exception as e:
        await cb.message.edit_text(f"❌ {e}", reply_markup=back_kb("admin"))

  # language: Python, file: bot.py
# ҚИСМИ 5/5: таймер, санҷиши обуна, main

# ---------- Таймер (корбар) ----------

@dp.callback_query(F.data == "timer")
async def cb_timer(cb: CallbackQuery, state: FSMContext):
    u = db.get_user(cb.from_user.id)
    if not u or not u.get("connected"):
        await cb.answer("Аввал аккаунтро пайваст кун!", show_alert=True)
        return
    tasks = db.list_tasks(cb.from_user.id)
    lines = ""
    if tasks:
        lines = "\n\n<b>Таймерҳои ту:</b>\n"
        for t in tasks[:10]:
            emoji = {"pending": "⏳", "done": "✅", "failed": "❌"}.get(t["status"], "•")
            lines += f"{emoji} {t['run_at'][:16]} — {t['text'][:30]}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Таймери нав", callback_data="t_new")],
        [InlineKeyboardButton(text="⬅️ Бозгашт", callback_data="main")],
    ])
    await cb.message.edit_text(
        f"⏰ <b>Таймер-рассылка</b>\n\n"
        f"Рассылка дар вақти муайян оғоз мешавад.{lines}",
        reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "t_new")
async def cb_timer_new(cb: CallbackQuery, state: FSMContext):
    price = db.get_price("timer_price", 0)
    is_adm = db.is_admin(cb.from_user.id)
    if not is_adm and price > 0:
        if db.get_stars(cb.from_user.id) < price:
            await cb.answer(f"Ситора кофӣ нест. Лозим: {price} ⭐", show_alert=True)
            return
        db.spend_stars(cb.from_user.id, price)

    await cb.message.edit_text("⏰ Матни паёмро фирист, баъд вақтро мепурсам:",
                               reply_markup=back_kb("timer"))
    await state.set_state(TimerS.text)
    await cb.answer()


@dp.message(TimerS.text)
async def timer_text(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("Матн холӣ.")
        return
    await state.update_data(text=text)
    await msg.answer(
        "⏰ Вақти оғозро фирист.\n\n"
        "Форматҳо:\n"
        "• <code>+30</code> — баъд аз 30 дақиқа\n"
        "• <code>+2h</code> — баъд аз 2 соат\n"
        "• <code>14:30</code> — имрӯз соати 14:30 (UTC)\n"
        "• <code>2026-10-11 09:00</code> — сана ва вақт (UTC)")
    await state.set_state(TimerS.when)


@dp.message(TimerS.when)
async def timer_when(msg: Message, state: FSMContext):
    raw = msg.text.strip()
    when = None
    now = datetime.utcnow()

    try:
        if raw.startswith("+"):
            body = raw[1:].lower()
            if body.endswith("h"):
                when = now + timedelta(hours=int(body[:-1]))
            elif body.endswith("m"):
                when = now + timedelta(minutes=int(body[:-1]))
            else:
                when = now + timedelta(minutes=int(body))
        elif len(raw) == 5 and ":" in raw:
            h, m = map(int, raw.split(":"))
            when = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if when <= now:
                when += timedelta(days=1)
        else:
            when = datetime.fromisoformat(raw)
    except Exception:
        await msg.answer("❌ Формат нодуруст. Аз нав фирист.")
        return

    if when <= now:
        await msg.answer("❌ Вақт гузашта аст.")
        return

    data = await state.get_data()
    db.create_task(msg.from_user.id, data["text"], when.isoformat())
    await msg.answer(
        f"✅ Таймер сохта шуд.\n\nВақт: <b>{when.strftime('%Y-%m-%d %H:%M')} UTC</b>",
        reply_markup=back_kb("timer"))
    await state.clear()


# ---------- Санҷиши обунаи ҳатмӣ (барои /start ва check_subs) ----------

async def check_force_subs(uid: int) -> tuple[bool, list[str]]:
    """True = ҳама обуна ҳаст. False = рӯйхати номҳои обунанашуда."""
    subs = db.list_force_subs()
    if not subs:
        return True, []
    if db.is_admin(uid):
        return True, []

    missing = []
    for s in subs:
        try:
            member = await bot.get_chat_member(s["chat_id"], uid)
            if member.status in ("left", "kicked"):
                missing.append(s["title"] or s["username"] or str(s["chat_id"]))
        except Exception:
            missing.append(s["title"] or s["username"] or str(s["chat_id"]))
    if missing:
        return False, missing
    return True, []


def force_sub_kb(missing_names: list[str]) -> InlineKeyboardMarkup:
    subs = db.list_force_subs()
    rows = []
    for s in subs:
        name = s["title"] or s["username"] or str(s["chat_id"])
        if name not in missing_names:
            continue
        url = s.get("invite")
        if not url and s.get("username"):
            url = f"https://t.me/{s['username'].lstrip('@')}"
        if url:
            rows.append([InlineKeyboardButton(text=f"🔔 {name}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Санҷиш кардам", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "check_subs")
async def cb_check_subs(cb: CallbackQuery):
    ok, missing = await check_force_subs(cb.from_user.id)
    if not ok:
        await cb.answer("Ҳанӯз обуна нестед!", show_alert=True)
        return
    await cb.answer("✅ Ташаккур!")
    u = db.get_user(cb.from_user.id)
    await cb.message.edit_text(_profile_text(u, cb.from_user.first_name),
                               reply_markup=main_menu(cb.from_user.id))


# ---------- Worker таймерҳо ----------

async def timer_worker():
    """Ҳар 30 сония таскҳои вақташон расидаро месанҷад."""
    while True:
        try:
            tasks = db.due_tasks()
            for t in tasks:
                log.info(f"Таймер иҷро: task={t['id']} user={t['user_id']}")
                try:
                    res = await sender.send_campaign(t["user_id"], t["text"])
                    db.mark_task(t["id"], "done" if res.get("ok") else "failed")
                    try:
                        await bot.send_message(
                            t["user_id"],
                            f"⏰ Таймер иҷро шуд.\n"
                            f"✅ {res.get('sent',0)} ✗ {res.get('failed',0)}")
                    except Exception:
                        pass
                except Exception as e:
                    log.error(f"timer task {t['id']}: {e}")
                    db.mark_task(t["id"], "failed")
        except Exception as e:
            log.error(f"timer_worker: {e}")
        await asyncio.sleep(30)


# ---------- main ----------

async def main():
    db.init_db()
    log.info("Бот оғоз шуд")
    asyncio.create_task(timer_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
