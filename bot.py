import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError

import db
import sender
from config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jingizbot")

bot = Bot(config.bot_token)
dp = Dispatcher()
campaign_tasks: dict[int, asyncio.Task] = {}
timer_tasks: dict[int, asyncio.Task] = {}
login_clients: dict[int, TelegramClient] = {}

class LoginFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()

class TextFlow(StatesGroup):
    broadcast_text = State()
    timer_text = State()
    timer_datetime = State()

class AdminFlow(StatesGroup):
    add_user = State()
    add_admin = State()
    give_stars = State()
    price_value = State()
    force_sub = State()
    admin_user_broadcast_id = State()
    admin_user_broadcast_text = State()
    global_broadcast_text = State()
    refresh_user = State()
    remove_admin = State()

MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔗 Пайваст кардани аккаунт"), KeyboardButton(text="👥 Гурӯҳҳо")],
    [KeyboardButton(text="📨 Рассылка"), KeyboardButton(text="⏱ Таймер-рассылка")],
    [KeyboardButton(text="⏳ Фосила"), KeyboardButton(text="✍️ Имзо")],
    [KeyboardButton(text="⭐ Ситораҳои ман"), KeyboardButton(text="🔄 Навсозии гурӯҳҳо")],
    [KeyboardButton(text="🛑 Қатъи рассылка"), KeyboardButton(text="📋 Вазифаҳои ман")],
], resize_keyboard=True)

ADMIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Иловаи корбар", callback_data="adm:add_user"),
     InlineKeyboardButton(text="➕ Иловаи админ", callback_data="adm:add_admin")],
    [InlineKeyboardButton(text="👑 Рӯйхати админҳо", callback_data="adm:admins"),
     InlineKeyboardButton(text="⭐ Додани ситора", callback_data="adm:stars")],
    [InlineKeyboardButton(text="💰 Нархҳо", callback_data="adm:prices"),
     InlineKeyboardButton(text="📢 Обунаи ҳатмӣ", callback_data="adm:subs")],
    [InlineKeyboardButton(text="📨 Аз аккаунти корбар", callback_data="adm:user_broadcast"),
     InlineKeyboardButton(text="🌐 Рассылкаи умумӣ", callback_data="adm:global_broadcast")],
    [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="adm:csv"),
     InlineKeyboardButton(text="📊 Омор", callback_data="adm:stats")],
    [InlineKeyboardButton(text="🔄 Навсозии маълумоти корбар", callback_data="adm:refresh_user"),
     InlineKeyboardButton(text="🗑 Нест кардани админ", callback_data="adm:remove_admin")],
])

PRICE_KEYS = {
    "connect_price": "Пайваст кардан",
    "broadcast_price": "Рассылка",
    "unsign_price": "Нест кардани имзо",
    "timer_price": "Таймер",
    "groups_refresh_price": "Навсозии гурӯҳҳо",
    "fetch_groups_price": "Хондани гурӯҳҳо",
}

def is_super(user_id: int) -> bool:
    return user_id in config.super_admins

async def admin_check(user_id: int) -> bool:
    return await db.is_admin(user_id, config.super_admins)

async def charge_if_needed(user_id: int, price_key: str) -> bool:
    if await admin_check(user_id):
        return True
    price = await db.get_price(price_key)
    if price <= 0:
        return True
    return await db.spend_stars(user_id, price)

async def force_sub_check(user_id: int) -> tuple[bool, list[str]]:
    subs = await db.list_force_subs()
    missing = []
    for sub in subs:
        ref = sub["chat_ref"]
        try:
            member = await bot.get_chat_member(ref, user_id)
            if member.status not in ("creator", "administrator", "member"):
                missing.append(ref)
        except Exception:
            # Агар бот дар канал/гурӯҳ ҳуқуқи санҷиш надошта бошад, обуна тасдиқ намешавад.
            missing.append(ref)
    return not missing, missing

def force_sub_kb(refs: list[str]):
    rows = []
    for ref in refs:
        url = ref if ref.startswith("https://") else (f"https://t.me/{ref.lstrip('@')}" if not ref.startswith("-100") else "")
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 Обуна: {ref}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Санҷидани обуна", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_home(message: Message):
    await message.answer("Салом! JingizBot омода аст. Аз меню интихоб кунед.", reply_markup=MAIN_KB)
    if await admin_check(message.from_user.id):
        await message.answer("👑 Барои идоракунӣ: Админ-панел", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Админ-панел", callback_data="adm:open")]
        ]))

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)
    allowed, missing = await force_sub_check(user.id)
    if not allowed:
        await message.answer("Барои истифодаи бот аввал ба канал/гурӯҳҳои зерин обуна шавед:", reply_markup=force_sub_kb(missing))
        return
    await show_home(message)

@dp.callback_query(F.data == "check_subs")
async def check_subs(callback: CallbackQuery):
    allowed, missing = await force_sub_check(callback.from_user.id)
    if allowed:
        await callback.message.answer("Обуна тасдиқ шуд.")
        await show_home(callback.message)
    else:
        await callback.answer("Ҳанӯз ба ҳамаи каналҳо/гурӯҳҳо обуна нашудаед.", show_alert=True)
    await callback.answer()

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not await admin_check(message.from_user.id):
        await message.answer("Дастрасӣ нест.")
        return
    await message.answer("👑 Админ-панел", reply_markup=ADMIN_KB)

@dp.callback_query(F.data == "adm:open")
async def admin_open(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Дастрасӣ нест", show_alert=True)
        return
    await callback.message.answer("👑 Админ-панел", reply_markup=ADMIN_KB)
    await callback.answer()

async def require_user(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        user = await db.get_user(message.from_user.id)
    return user

@dp.message(F.text == "🔗 Пайваст кардани аккаунт")
async def connect_start(message: Message, state: FSMContext):
    await require_user(message)
    if not await charge_if_needed(message.from_user.id, "connect_price"):
        await message.answer("Ситораҳо кофӣ нестанд.")
        return
    await state.set_state(LoginFlow.phone)
    await message.answer("Рақами телефони аккаунти Telegram-ро бо формати байналмилалӣ фиристед, масалан +992900000000.\nРамз ва пароли 2FA-ро ба каси дигар надиҳед.")

@dp.message(LoginFlow.phone)
async def connect_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    if not phone.startswith("+") or len(phone) < 8 or len(phone) > 20:
        await message.answer("Рақам нодуруст аст. Масалан: +992900000000")
        return
    client = TelegramClient(StringSession(), config.api_id, config.api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        login_clients[message.from_user.id] = client
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        await state.set_state(LoginFlow.code)
        await message.answer("Коди аз Telegram гирифтаатонро ворид кунед.")
    except PhoneNumberInvalidError:
        await client.disconnect()
        await message.answer("Рақами телефон нодуруст аст. Аз нав кӯшиш кунед.")
    except Exception as exc:
        await client.disconnect()
        log.exception("Failed to request login code")
        await message.answer(f"Ирсоли код нашуд: {type(exc).__name__}. Баъдтар кӯшиш кунед.")

@dp.message(LoginFlow.code)
async def connect_code(message: Message, state: FSMContext):
    code = (message.text or "").replace(" ", "").strip()
    data = await state.get_data()
    client = login_clients.get(message.from_user.id)
    if not client:
        await state.clear()
        await message.answer("Сессияи воридшавӣ гум шуд. Аз нав оғоз кунед.")
        return
    try:
        await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await db.save_session(message.from_user.id, client.session.save(), data["phone"])
        await state.clear()
        login_clients.pop(message.from_user.id, None)
        await client.disconnect()
        await message.answer("✅ Аккаунт пайваст шуд.")
    except SessionPasswordNeededError:
        await state.set_state(LoginFlow.password)
        await message.answer("Пароли 2FA-и Telegram-ро ворид кунед. Он дар база сабт намешавад.")
    except PhoneCodeInvalidError:
        await message.answer("Код нодуруст аст. Коди дурустро ворид кунед.")
    except Exception as exc:
        log.exception("Login code failed")
        await message.answer(f"Воридшавӣ нашуд: {type(exc).__name__}.")
        await cleanup_login(message.from_user.id, state)

@dp.message(LoginFlow.password)
async def connect_password(message: Message, state: FSMContext):
    client = login_clients.get(message.from_user.id)
    data = await state.get_data()
    if not client:
        await state.clear()
        await message.answer("Сессияи воридшавӣ гум шуд. Аз нав оғоз кунед.")
        return
    password = (message.text or "").strip()
    try:
        await client.sign_in(password=password)
        await db.save_session(message.from_user.id, client.session.save(), data["phone"])
        await state.clear()
        login_clients.pop(message.from_user.id, None)
        await client.disconnect()
        await message.answer("✅ Аккаунт пайваст шуд.")
    except Exception as exc:
        log.exception("2FA login failed")
        await message.answer(f"Парол қабул нашуд: {type(exc).__name__}. Аз нав ворид кунед ё пайвастшавиро қатъ кунед.")

async def cleanup_login(user_id: int, state: FSMContext):
    client = login_clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.disconnect()
    await state.clear()

@dp.message(F.text == "👥 Гурӯҳҳо")
async def groups_menu(message: Message):
    await require_user(message)
    groups = await db.list_groups(message.from_user.id)
    if not groups:
        await message.answer("Рӯйхати гурӯҳҳо холӣ аст. Тугмаи «🔄 Навсозии гурӯҳҳо»-ро пахш кунед.")
        return
    rows = []
    for group in groups:
        mark = "🟢" if group["is_selected"] else "⚪"
        status = "" if group["can_send"] else " (ҳуқуқи фиристодан номаълум/нест)"
        title = (group["title"] or str(group["chat_id"]))[:45]
        rows.append([InlineKeyboardButton(text=f"{mark} {title}{status}", callback_data=f"grp:{group['chat_id']}")])
    await message.answer("Гурӯҳро пахш кунед, то интихоб/бекор шавад:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("grp:"))
async def group_toggle(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    row = await db.toggle_group(callback.from_user.id, chat_id)
    if row:
        mark = "интихоб шуд" if row["is_selected"] else "бекор шуд"
        await callback.answer(f"Гурӯҳ {mark}")
    else:
        await callback.answer("Гурӯҳ ёфт нашуд", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "🔄 Навсозии гурӯҳҳо")
async def refresh_groups(message: Message):
    user_id = message.from_user.id
    user = await require_user(message)
    if not await charge_if_needed(user_id, "groups_refresh_price"):
        await message.answer("Ситораҳо кофӣ нестанд.")
        return
    if not user["session_string"]:
        await message.answer("Аввал аккаунтро пайваст кунед.")
        return
    await message.answer("Гурӯҳҳоро мегирам...")
    try:
        groups = await sender.fetch_user_groups(user["session_string"])
        await db.upsert_groups(user_id, groups)
        await message.answer(f"✅ Навсозӣ шуд. Гурӯҳҳо: {len(groups)}")
    except Exception as exc:
        log.exception("Group refresh failed")
        await message.answer(f"Гурӯҳҳоро гирифта натавонистам: {type(exc).__name__}: {exc}")

@dp.message(F.text == "📨 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    if not await charge_if_needed(message.from_user.id, "broadcast_price"):
        await message.answer("Ситораҳо кофӣ нестанд.")
        return
    user = await require_user(message)
    if not user["session_string"]:
        await message.answer("Аввал аккаунтро пайваст кунед.")
        return
    groups = await db.selected_groups(message.from_user.id)
    if not groups:
        await message.answer("Аввал гурӯҳҳоро интихоб кунед.")
        return
    await state.set_state(TextFlow.broadcast_text)
    await message.answer(f"Матни паёмро фиристед. Ба {len(groups)} гурӯҳи интихобшуда меравад. Фосила: {user['delay_seconds']} сония.")

@dp.message(TextFlow.broadcast_text)
async def broadcast_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Матн холӣ аст. Матнро фиристед.")
        return
    user_id = message.from_user.id
    await state.clear()
    old = campaign_tasks.get(user_id)
    if old and not old.done():
        await message.answer("Рассылка аллакай иҷро шуда истодааст. Аввал онро қатъ кунед.")
        return
    task = asyncio.create_task(run_campaign(user_id, text, message.chat.id))
    campaign_tasks[user_id] = task
    await message.answer("Рассылка оғоз шуд. Агар Telegram маҳдудият диҳад, вазифа таваққуф мекунад.")

async def run_campaign(user_id: int, text: str, chat_id: int):
    try:
        result = await sender.send_campaign(user_id, text)
        msg = f"Рассылка анҷом ёфт.\nФиристода шуд: {result['sent']}\nХато: {result['failed']}"
        if result["stopped"]:
            msg += "\n⚠️ Telegram маҳдудият/таваққуф баргардонд. Рассылка қатъ шуд."
        await bot.send_message(chat_id, msg)
    except Exception as exc:
        log.exception("Campaign failed")
        await bot.send_message(chat_id, f"Рассылка иҷро нашуд: {type(exc).__name__}: {exc}")
    finally:
        campaign_tasks.pop(user_id, None)

@dp.message(F.text == "🛑 Қатъи рассылка")
async def stop_campaign(message: Message):
    task = campaign_tasks.get(message.from_user.id)
    if task and not task.done():
        task.cancel()
        await message.answer("Дархости қатъ фиристода шуд.")
    else:
        await message.answer("Рассылкаи фаъол нест.")
    timer = timer_tasks.get(message.from_user.id)
    if timer and not timer.done():
        timer.cancel()
        await message.answer("Таймер низ қатъ карда шуд.")

@dp.message(F.text == "⏳ Фосила")
async def delay_menu(message: Message):
    user = await require_user(message)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="2 дақ", callback_data="delay:120"),
         InlineKeyboardButton(text="3 дақ", callback_data="delay:180"),
         InlineKeyboardButton(text="5 дақ", callback_data="delay:300")],
        [InlineKeyboardButton(text="10 дақ", callback_data="delay:600"),
         InlineKeyboardButton(text="15 дақ", callback_data="delay:900"),
         InlineKeyboardButton(text="20 дақ", callback_data="delay:1200")],
    ])
    await message.answer(f"Фосилаи ҷорӣ: {user['delay_seconds']//60} дақиқа.\n⚠️ Фосилаи кам метавонад боиси маҳдуд шудани аккаунт гардад.", reply_markup=kb)

@dp.callback_query(F.data.startswith("delay:"))
async def set_delay_callback(callback: CallbackQuery):
    delay = int(callback.data.split(":")[1])
    if delay < config.min_delay or delay > config.max_delay:
        await callback.answer("Фосила берун аз ҳудуди иҷозатшуда аст.", show_alert=True)
        return
    await db.set_delay(callback.from_user.id, delay)
    await callback.answer(f"Фосила: {delay//60} дақ")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "✍️ Имзо")
async def signature_menu(message: Message):
    user = await require_user(message)
    enabled = user["signature_enabled"]
    if enabled:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Хомӯш кардани имзо", callback_data="signature:off")]])
        await message.answer("Имзо фаъол аст:\nотправлено через @jingizbot", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Фаъол кардани имзо", callback_data="signature:on")]])
        await message.answer("Имзо хомӯш аст.", reply_markup=kb)

@dp.callback_query(F.data.startswith("signature:"))
async def signature_toggle(callback: CallbackQuery):
    enabled = callback.data.endswith("on")
    if not enabled and not await charge_if_needed(callback.from_user.id, "unsign_price"):
        await callback.answer("Ситораҳо кофӣ нестанд.", show_alert=True)
        return
    await db.set_signature(callback.from_user.id, enabled)
    await callback.answer("Имзо фаъол шуд" if enabled else "Имзо хомӯш шуд")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "⭐ Ситораҳои ман")
async def my_stars(message: Message):
    user = await require_user(message)
    await message.answer(f"⭐ Баланси шумо: {user['stars']} ситора")

@dp.message(F.text == "⏱ Таймер-рассылка")
async def timer_start(message: Message, state: FSMContext):
    if not await charge_if_needed(message.from_user.id, "timer_price"):
        await message.answer("Ситораҳо кофӣ нестанд.")
        return
    user = await require_user(message)
    if not user["session_string"]:
        await message.answer("Аввал аккаунтро пайваст кунед.")
        return
    if not await db.selected_groups(message.from_user.id):
        await message.answer("Аввал гурӯҳҳоро интихоб кунед.")
        return
    await state.set_state(TextFlow.timer_text)
    await message.answer("Матни рассылкаро фиристед.")

@dp.message(TextFlow.timer_text)
async def timer_text(message: Message, state: FSMContext):
    await state.update_data(timer_text=(message.text or "").strip())
    await state.set_state(TextFlow.timer_datetime)
    await message.answer("Вақти оғозро бо формати YYYY-MM-DD HH:MM ворид кунед (вақти UTC), масалан 2026-12-31 18:30.")

@dp.message(TextFlow.timer_datetime)
async def timer_datetime(message: Message, state: FSMContext):
    try:
        run_at = datetime.strptime((message.text or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer("Формат нодуруст аст. Мисол: 2026-12-31 18:30")
        return
    if run_at <= datetime.now(timezone.utc):
        await message.answer("Вақт бояд дар оянда бошад.")
        return
    data = await state.get_data()
    text = data.get("timer_text", "")
    await state.clear()
    task_id = await db.create_task(message.from_user.id, "timer", text, run_at)
    previous = timer_tasks.get(message.from_user.id)
    if previous and not previous.done():
        previous.cancel()
    timer_tasks[message.from_user.id] = asyncio.create_task(run_timer(message.from_user.id, task_id, text, run_at, message.chat.id))
    await message.answer(f"⏱ Таймер #{task_id} сабт шуд. Оғоз: {run_at.strftime('%Y-%m-%d %H:%M UTC')}")

async def run_timer(user_id: int, task_id: int, text: str, run_at: datetime, chat_id: int):
    try:
        delay = max(0, (run_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        await db.update_task(task_id, "running")
        result = await sender.send_campaign(user_id, text)
        status = "completed" if not result["stopped"] else "stopped"
        await db.update_task(task_id, status, result)
        await bot.send_message(chat_id, f"Таймер #{task_id} анҷом ёфт. Фиристода шуд: {result['sent']}; хато: {result['failed']}.")
    except asyncio.CancelledError:
        await db.update_task(task_id, "cancelled")
        raise
    except Exception as exc:
        log.exception("Timer failed")
        await db.update_task(task_id, "failed", {"error": str(exc)})
        await bot.send_message(chat_id, f"Таймер #{task_id} хато дод: {type(exc).__name__}.")
    finally:
        timer_tasks.pop(user_id, None)

@dp.message(F.text == "📋 Вазифаҳои ман")
async def my_tasks(message: Message):
    tasks = await db.list_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Вазифаҳо нестанд.")
        return
    lines = [f"#{t['id']} · {t['task_type']} · {t['status']} · {t['created_at'].strftime('%Y-%m-%d %H:%M')}" for t in tasks[:30]]
    await message.answer("\n".join(lines))

# Админ-панел: ҳамаи амалҳои админ бепул мебошанд; нархҳо танҳо барои корбарони оддӣ татбиқ мешаванд.
@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Дастрасӣ нест.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "add_user":
        await state.set_state(AdminFlow.add_user)
        await callback.message.answer("Telegram ID-и корбарро фиристед.")
    elif action == "add_admin":
        await state.set_state(AdminFlow.add_admin)
        await callback.message.answer("Telegram ID-и админи навро фиристед.")
    elif action == "admins":
        rows, supers = await db.list_admins(config.super_admins)
        text = "👑 Super admins:\n" + ("\n".join(map(str, supers)) or "—")
        text += "\n\nАдминҳои иловагӣ:\n" + ("\n".join(str(r["user_id"]) for r in rows) or "—")
        await callback.message.answer(text)
    elif action == "stars":
        await state.set_state(AdminFlow.give_stars)
        await callback.message.answer("ID ва миқдори ситораро дар як сатр фиристед: ID 100")
    elif action == "prices":
        prices = [f"{key} = {await db.get_price(key)} ⭐" for key in PRICE_KEYS]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{label}: {await db.get_price(key)} ⭐", callback_data=f"price:{key}")]
            for key, label in PRICE_KEYS.items()
        ])
        await callback.message.answer("Нархҳоро пахш карда тағйир диҳед:\n" + "\n".join(prices), reply_markup=kb)
    elif action == "subs":
        subs = await db.list_force_subs()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"❌ #{s['id']} {s['chat_ref']}", callback_data=f"subdel:{s['id']}")]
            for s in subs
        ] + [[InlineKeyboardButton(text="➕ Илова", callback_data="subadd")]])
        await callback.message.answer("Обунаи ҳатмӣ. Бот бояд дар канал/гурӯҳ бошад ва ҳуқуқи санҷиши аъзо дошта бошад.", reply_markup=kb)
    elif action == "user_broadcast":
        await state.set_state(AdminFlow.admin_user_broadcast_id)
        await callback.message.answer("ID-и корбареро фиристед, ки аз аккаунти пайвастшудааш паём равад.")
    elif action == "global_broadcast":
        await state.set_state(AdminFlow.global_broadcast_text)
        await callback.message.answer("Матни рассылкаи умумиро фиристед. Он ба гурӯҳҳои интихобшудаи ҳар аккаунти пайвастшуда меравад.")
    elif action == "csv":
        users = await db.list_users()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "full_name", "stars", "connected", "created_at"])
        for u in users:
            writer.writerow([u["user_id"], u["username"], u["full_name"], u["stars"], u["connected"], u["created_at"]])
        await callback.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="jingizbot_users.csv"))
    elif action == "stats":
        s = await db.stats()
        await callback.message.answer(
            f"📊 Омор\nКорбарон: {s['users']}\nАккаунтҳои пайвастшуда: {s['connected']}\nГурӯҳҳо: {s['groups']}\nВазифаҳои интизор: {s['tasks_pending']}\nАдминҳои иловагӣ: {s['admins']}"
        )
    elif action == "refresh_user":
        await state.set_state(AdminFlow.refresh_user)
        await callback.message.answer("ID-и корбарро фиристед, то маълумоташ нав шавад.")
    elif action == "remove_admin":
        await state.set_state(AdminFlow.remove_admin)
        await callback.message.answer("ID-и админро барои хориҷ кардан фиристед. Super-admin аз ин роҳ хориҷ намешавад.")

@dp.callback_query(F.data.startswith("price:"))
async def price_select(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Дастрасӣ нест.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in PRICE_KEYS:
        await callback.answer("Нарх ёфт нашуд.", show_alert=True)
        return
    await state.update_data(price_key=key)
    await state.set_state(AdminFlow.price_value)
    await callback.message.answer(f"Нархи нав барои «{PRICE_KEYS[key]}»-ро бо ситора фиристед. 0 = ройгон.")
    await callback.answer()

@dp.callback_query(F.data == "subadd")
async def subadd_callback(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Дастрасӣ нест.", show_alert=True)
        return
    await state.set_state(AdminFlow.force_sub)
    await callback.message.answer("Username ё ID-и канал/гурӯҳро фиристед (мисол @mychannel). Барои тугмаи обуна беҳтар аст канал username ё link дошта бошад.")
    await callback.answer()

@dp.callback_query(F.data.startswith("subdel:"))
async def subdel_callback(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Дастрасӣ нест.", show_alert=True)
        return
    await db.delete_force_sub(int(callback.data.split(":")[1]))
    await callback.answer("Нест шуд")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(AdminFlow.add_user)
async def admin_add_user(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        await db.upsert_user(user_id, None, "")
        await db.log_admin_action(message.from_user.id, "add_user", user_id)
        await message.answer(f"Корбар {user_id} илова шуд.")
    except Exception as exc:
        await message.answer(f"ID нодуруст ё хатои база: {exc}")
    await state.clear()

@dp.message(AdminFlow.add_admin)
async def admin_add_admin(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        await db.add_admin(user_id, message.from_user.id)
        await db.log_admin_action(message.from_user.id, "add_admin", user_id)
        await message.answer(f"Корбар {user_id} админ шуд.")
    except Exception as exc:
        await message.answer(f"ID нодуруст ё хатои база: {exc}")
    await state.clear()

@dp.message(AdminFlow.give_stars)
async def admin_give_stars(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        parts = (message.text or "").split()
        user_id, amount = int(parts[0]), int(parts[1])
        if amount <= 0:
            raise ValueError("Миқдор бояд мусбат бошад.")
        balance = await db.add_stars(user_id, amount)
        await db.log_admin_action(message.from_user.id, "give_stars", user_id, {"amount": amount})
        await message.answer(f"Ба {user_id} {amount} ситора дода шуд. Баланс: {balance}")
    except Exception as exc:
        await message.answer(f"Формат: ID 100. Хато: {exc}")
    await state.clear()

@dp.message(AdminFlow.price_value)
async def admin_price_value(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    try:
        amount = int((message.text or "").strip())
        if amount < 0:
            raise ValueError("Нарх манфӣ буда наметавонад.")
        await db.set_price(data["price_key"], amount)
        await message.answer(f"Нарх нав шуд: {PRICE_KEYS[data['price_key']]} = {amount} ⭐. Админҳо ҳамоно ройгон истифода мебаранд.")
    except Exception as exc:
        await message.answer(f"Нарх бояд адади 0 ё калон бошад: {exc}")
    await state.clear()

@dp.message(AdminFlow.force_sub)
async def admin_force_sub(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    ref = (message.text or "").strip()
    if not ref:
        await message.answer("Username ё ID фиристед.")
        return
    label = ref
    try:
        await db.add_force_sub(ref, label)
        await message.answer(f"Обунаи ҳатмӣ илова шуд: {ref}")
    except Exception as exc:
        await message.answer(f"Илова нашуд: {exc}")
    await state.clear()

@dp.message(AdminFlow.admin_user_broadcast_id)
async def admin_user_broadcast_id(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        target = int((message.text or "").strip())
        user = await db.get_user(target)
        if not user or not user["session_string"]:
            await message.answer("Ин корбар сабт нашудааст ё аккаунташ пайваст нест.")
            await state.clear()
            return
        await state.update_data(target_id=target)
        await state.set_state(AdminFlow.admin_user_broadcast_text)
        await message.answer("Матни паёмро фиристед.")
    except ValueError:
        await message.answer("ID бояд адад бошад.")

@dp.message(AdminFlow.admin_user_broadcast_text)
async def admin_user_broadcast_text(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    try:
        result = await sender.send_to_account(data["target_id"], (message.text or "").strip())
        await message.answer(f"Анҷом ёфт. Фиристода шуд: {result['sent']}; хато: {result['failed']}.")
        await db.log_admin_action(message.from_user.id, "user_broadcast", data["target_id"], result)
    except Exception as exc:
        await message.answer(f"Рассылка иҷро нашуд: {type(exc).__name__}: {exc}")
    await state.clear()

@dp.message(AdminFlow.global_broadcast_text)
async def admin_global_broadcast_text(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    await state.clear()
    await message.answer("Рассылкаи умумӣ оғоз шуд; ин метавонад вақт гирад.")
    results = await sender.broadcast_all((message.text or "").strip())
    await message.answer(f"Рассылкаи умумӣ анҷом ёфт. Аккаунтҳо коркард шуданд: {len(results)}.")

@dp.message(AdminFlow.refresh_user)
async def admin_refresh_user(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        user = await db.get_user(user_id)
        if not user:
            await message.answer("Корбар дар база нест.")
        else:
            await message.answer(
                f"ID: {user['user_id']}\nUsername: @{user['username'] or '—'}\nНом: {user['full_name']}\nСитора: {user['stars']}\nПайваст: {'ҳа' if user['session_string'] else 'не'}\nФосила: {user['delay_seconds']} сония\nИмзо: {'фаъол' if user['signature_enabled'] else 'хомӯш'}"
            )
    except Exception as exc:
        await message.answer(f"Хато: {exc}")
    await state.clear()

@dp.message(AdminFlow.remove_admin)
async def admin_remove_admin(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        if is_super(user_id):
            await message.answer("Super-admin-ро аз ин ҷо хориҷ кардан мумкин нест.")
        else:
            await db.remove_admin(user_id)
            await db.log_admin_action(message.from_user.id, "remove_admin", user_id)
            await message.answer(f"Админ {user_id} хориҷ карда шуд.")
    except Exception as exc:
        await message.answer(f"Хато: {exc}")
    await state.clear()

@dp.message()
async def unknown_message(message: Message):
    if message.from_user:
        await message.answer("Фармонро аз меню интихоб кунед ё /start-ро пахш кунед.")

async def health(request):
    return web.Response(text="JingizBot is alive", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    log.info("Health server listening on port %s", config.port)
    return runner

async def main():
    await db.init_db(config.database_url)
    # Super-admin IDs аз ENV ба admins низ ворид мешаванд, аммо иҷозаташон ҳамеша аз ENV санҷида мешавад.
    async with db.get_pool().acquire() as con:
        for admin_id in config.super_admins:
            await con.execute("INSERT INTO admins(user_id, added_by) VALUES($1,$1) ON CONFLICT(user_id) DO NOTHING", admin_id)
    runner = await start_health_server()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in list(campaign_tasks.values()) + list(timer_tasks.values()):
            task.cancel()
        for client in list(login_clients.values()):
            with suppress(Exception):
                await client.disconnect()
        await runner.cleanup()
        await bot.session.close()
        if db.pool:
            await db.pool.close()

if __name__ == "__main__":
    asyncio.run(main())
