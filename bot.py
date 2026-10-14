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
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand, LabeledPrice, PreCheckoutQuery
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError

import db
import sender
from config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VertonaBot")

bot = Bot(config.bot_token)
dp = Dispatcher()
campaign_tasks: dict[int, asyncio.Task] = {}
timer_tasks: dict[int, asyncio.Task] = {}
auto_tasks: dict[int, asyncio.Task] = {}
admin_campaign_tasks: dict[int, asyncio.Task] = {}  # target user -> admin-controlled message campaign
admin_group_pages: dict[tuple[int, int], int] = {}  # (admin_id, target_user_id) -> group selector page
pending_admin_campaigns: dict[int, tuple[str, int, int]] = {}  # target -> (text, admin_id, interval), awaiting user consent
auto_intervals: dict[int, int] = {}  # cached seconds; persisted in PostgreSQL settings table
auto_panel_refs: dict[int, tuple[int, int]] = {}  # user_id -> (chat_id, message_id)
group_selection_drafts: dict[int, set[int]] = {}  # uncommitted checkbox selection by user
group_selection_pages: dict[int, int] = {}  # pagination position for large group lists
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
    edit_broadcast_text = State()
    group_auto_text = State()

class AdminFlow(StatesGroup):
    add_user = State()
    add_admin = State()
    give_stars = State()
    price_value = State()
    force_sub = State()
    admin_user_broadcast_id = State()
    admin_user_broadcast_text = State()
    admin_user_broadcast_interval = State()
    global_broadcast_text = State()
    refresh_user = State()
    remove_admin = State()
    pro_user_id = State()
    pro_days = State()

def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Compact user-facing menu inspired by the supplied reference video.

    The admin entry is only rendered for users who passed the DB/ENV admin check.
    """
    rows = [
        [KeyboardButton(text="🤖 Авторассылка"), KeyboardButton(text="✉️ Текст сообщения")],
        [KeyboardButton(text="⏱ Интервал"), KeyboardButton(text="👥 Настройка групп")],
        [KeyboardButton(text="🔁 Автоответчик"), KeyboardButton(text="🧰 Полезные функции")],
        [KeyboardButton(text="👤 Профили"), KeyboardButton(text="💎 Pro тариф")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="📖 Инструкция")],
        [KeyboardButton(text="🛒 Маркет")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

MAIN_KB = main_keyboard(False)
UTILITY_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📨 Рассылка")],
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
    [InlineKeyboardButton(text="💎 Добавить PRO", callback_data="adm:pro_add"),
     InlineKeyboardButton(text="💎 Список PRO", callback_data="adm:pro_list")],
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
    # Admins and active PRO users can use all paid bot features for free.
    if await admin_check(user_id) or await db.is_pro(user_id):
        return True
    price = await db.get_price(price_key)
    if price <= 0:
        return True
    return await db.spend_stars(user_id, price)

async def force_sub_check(user_id: int) -> tuple[bool, list[dict]]:
    """Check configured required channels; cache a successful check per user/channel.

    Empty channel list means no subscription gate. Adding a new channel creates a
    new verification key and prompts users for that channel until they join it.
    """
    subs = await db.list_force_subs()
    if not subs:
        return True, []
    missing = []
    for sub in subs:
        item = dict(sub)
        verified_key = f"force_sub_verified:{user_id}:{item['id']}"
        try:
            if await get_setting(verified_key, "0") == "1":
                continue
            member = await bot.get_chat_member(item["chat_ref"], user_id)
            if member.status in ("creator", "administrator", "member"):
                await set_setting(verified_key, "1")
            else:
                missing.append(item)
        except Exception:
            log.warning(
                "Could not verify required subscription user=%s channel=%s",
                user_id, item.get("chat_ref"), exc_info=True,
            )
            # Do not silently bypass a configured channel if Telegram cannot verify it.
            missing.append(item)
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

def phone_request_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)],
                  [KeyboardButton(text="❌ Закрыть действие")]],
        resize_keyboard=True, one_time_keyboard=True
    )

async def get_broadcast_text(user_id: int) -> str:
    return (await get_setting(f"broadcast_text:{user_id}", "") or "").strip()

async def save_broadcast_text(user_id: int, text: str) -> None:
    await set_setting(f"broadcast_text:{user_id}", text.strip())


class ForceSubscriptionMiddleware(BaseMiddleware):
    """Enforce configured channel subscriptions for every feature, not only /start."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        # /start performs its own friendly subscription flow; check_subs is the escape hatch.
        if isinstance(event, Message) and event.text and event.text.split(maxsplit=1)[0] == "/start":
            return await handler(event, data)
        # Do not interrupt an already-started, user-initiated login flow between
        # phone, Telegram code, and optional 2FA steps. The gate still applies
        # before a user starts connecting an account.
        fsm = data.get("state")
        if fsm is not None:
            try:
                current_state = await fsm.get_state()
                if current_state and current_state.startswith("LoginFlow:"):
                    return await handler(event, data)
            except Exception:
                log.exception("Could not read FSM state in subscription middleware")
        if isinstance(event, CallbackQuery) and event.data == "check_subs":
            return await handler(event, data)
        try:
            if await admin_check(user.id):
                return await handler(event, data)
            allowed, missing = await force_sub_check(user.id)
        except Exception:
            log.exception("Mandatory-subscription middleware failed")
            try:
                missing = await db.list_force_subs()
            except Exception:
                if isinstance(event, Message):
                    await event.answer("Временная ошибка проверки. Попробуйте ещё раз позже.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Временная ошибка проверки. Попробуйте позже.", show_alert=True)
                return None
            # If there are no configured channels, a DB/check error must not
            # invent a subscription requirement.
            if not missing:
                return await handler(event, data)
            allowed = False
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


async def show_home(message: Message, user_id: int | None = None):
    # callback.message.from_user is the bot, so callbacks must pass callback.from_user.id.
    user_id = int(user_id or (message.from_user.id if message.from_user else 0))
    is_admin = await admin_check(user_id)
    text = "Здравствуйте! VertonaBot готов к работе. Выберите действие в меню."
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
        await message.answer("Для использования бота сначала подпишитесь на следующие каналы/группы:", reply_markup=force_sub_kb(missing))
        return
    await show_home(message)

@dp.callback_query(F.data == "check_subs")
async def check_subs(callback: CallbackQuery):
    allowed, missing = (True, []) if await admin_check(callback.from_user.id) else await force_sub_check(callback.from_user.id)
    if allowed:
        await callback.message.answer("Подписка подтверждена.")
        await show_home(callback.message, callback.from_user.id)
    else:
        await callback.answer("Вы ещё не подписались на все каналы/группы.", show_alert=True)
        return
    await callback.answer()

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
    text = ("🔁 <b>Автоответчик</b>\n\n"
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
    return ("🔁 <b>Автоответчик</b>\n\n"
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


async def show_pro_info(message: Message) -> None:
    user_id = message.from_user.id
    info = await db.get_pro_info(user_id)
    if info:
        expires = info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status = f"🟢 <b>PRO активен</b>\nСрок действия: <code>{expires}</code>"
    else:
        status = "⚪ <b>PRO не активен</b>"
    text = (
        "💎 <b>VertonaBot PRO</b>\n\n"
        f"{status}\n\n"
        "<b>Возможности PRO:</b>\n"
        "• Все платные функции бесплатны: подключение аккаунта, рассылка, таймер и обновление групп.\n"
        "• Отключение подписи «Отправлено через @VertonaBot» бесплатно.\n"
        "• Выбор групп без тарифного лимита; фактическое количество зависит от групп аккаунта и ограничений Telegram.\n"
        "• Авторассылка, автоответчик, интервалы и другие настройки без дополнительной оплаты.\n"
        "• До 10 подключённых аккаунтов для PRO и ADMIN.\n"
        "• Отдельная рассылка для каждой группы: отдельный текст и настройки рассылки.\n"
        "• Группы должны быть привязаны к выбранному аккаунту; рассылка не должна автоматически отправляться через другие аккаунты.\n"
        "• Существующие функции бота сохраняются; для этого пользователя стоимость платных функций равна нулю.\n\n"
        "PRO выдаётся администратором по Telegram ID и количеству дней. После окончания срока снова действуют обычные цены."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=with_close([
        [InlineKeyboardButton(text="🔄 Проверить PRO", callback_data="pro:refresh")]
    ]))


@dp.callback_query(F.data == "pro:refresh")
async def pro_refresh(callback: CallbackQuery):
    await callback.answer("Данные PRO обновлены.")
    if callback.message:
        await show_pro_info_from_callback(callback)


async def show_pro_info_from_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    info = await db.get_pro_info(user_id)
    if info:
        expires = info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status = f"🟢 <b>PRO активен</b>\nСрок действия: <code>{expires}</code>"
    else:
        status = "⚪ <b>PRO не активен</b>"
    text = (
        "💎 <b>VertonaBot PRO</b>\n\n" + status + "\n\n"
        "<b>Возможности PRO:</b> все платные функции бота доступны без дополнительной оплаты; "
        "до 10 подключённых аккаунтов для PRO и ADMIN; отключение подписи бесплатно; "
        "выбор групп без тарифного лимита; авторассылка, автоответчик, интервалы и обновление групп.\n"
        "• <b>Отдельная рассылка для каждой группы</b> — отдельный текст и настройки.\n"
        "• Группы и отправка должны быть привязаны к выбранному аккаунту, чтобы рассылка не уходила через другие аккаунты.\n\n"
        "Ограничения Telegram и права конкретного аккаунта сохраняются."
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=with_close([[InlineKeyboardButton(text="🔄 Проверить PRO", callback_data="pro:refresh")]])
        )


MENU_BUTTONS = {
    # Main menu (reference-video labels)
    "🤖 Авторассылка", "✉️ Текст сообщения", "⏱ Интервал", "👥 Настройка групп",
    "🔁 Автоответчик", "🧰 Полезные функции", "👤 Профили", "💎 Pro тариф",
    "📊 Статистика", "❓ Помощь", "📖 Инструкция", "🛒 Маркет", "👑 Админ-панель",
    # Utilities / legacy aliases
    "🔗 Подключить аккаунт", "🔌 Отключить аккаунт", "👥 Группы", "📨 Рассылка", "⏱ Рассылка по таймеру",
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
        await message.answer("✅ Действие закрыто. Выберите следующий пункт меню.", reply_markup=main_keyboard(await admin_check(message.from_user.id)))
    elif message.text == "🔗 Подключить аккаунт":
        await connect_start(message, state)
    elif message.text == "🔌 Отключить аккаунт":
        await disconnect_account(message)
    elif message.text == "👤 Профили":
        await profile_menu(message)
    elif message.text in ("👥 Группы", "👥 Настройка групп"):
        await groups_menu(message)
    elif message.text == "✉️ Текст сообщения":
        await show_broadcast_text_menu(message)
    elif message.text == "📨 Рассылка":
        await broadcast_start(message, state)
    elif message.text == "🧰 Полезные функции":
        await message.answer("🧰 <b>Полезные функции</b>\nВыберите нужную функцию в меню ниже.", reply_markup=UTILITY_KB, parse_mode="HTML")
    elif message.text == "👑 Админ-панель":
        if await admin_check(message.from_user.id):
            await message.answer("👑 Панель администратора", reply_markup=ADMIN_KB)
        else:
            await message.answer("Доступ запрещён.")
    elif message.text == "🔁 Автоответчик":
        await show_autoreply_panel(message)
    elif message.text == "💎 Pro тариф":
        await show_pro_info(message)
    elif message.text == "📊 Статистика":
        user = await db.get_user(message.from_user.id)
        groups = await db.list_groups(message.from_user.id)
        selected = sum(1 for g in groups if g["is_selected"])
        available = sum(1 for g in groups if g["is_selected"] and g["can_send"])
        await message.answer(f"📊 <b>Ваша статистика</b>\nID: <code>{message.from_user.id}</code>\nАккаунт: {'подключён' if user and user['session_string'] else 'не подключён'}\nВсего групп: {len(groups)}\nВыбрано групп: {selected}\nДоступно для отправки: {available}\nЗвёзды: {user['stars'] if user else 0}", parse_mode="HTML")
    elif message.text == "❓ Помощь":
        await message.answer("❓ <b>Помощь</b>\nПодключите аккаунт, обновите список групп, выберите группы и сохраните выбор. Для разовой рассылки нажмите «Текст сообщения», для повторяющейся — «Авторассылка».\n\nПо вопросам напишите @ffxdavlatov.", parse_mode="HTML")
    elif message.text == "📖 Инструкция":
        await message.answer(
            "📖 <b>ПОЛНАЯ ИНСТРУКЦИЯ VERTONABOT</b>\n\n"
            "👤 <b>1. Профиль и подключение</b>\n"
            "Откройте «Профили». Здесь отображаются статус аккаунта, телефон, PRO и статистика групп. "
            "Подключение, переподключение и отключение аккаунта находятся только внутри профиля. "
            "Никому не сообщайте код входа или пароль двухэтапной проверки.\n\n"
            "👥 <b>2. Список групп</b>\n"
            "Откройте «Полезные функции» → «Обновить группы», затем «Настройка групп». "
            "Отметьте нужные группы и обязательно сохраните выбор. «Выбрано» — все отмеченные группы; "
            "«Доступно для отправки» — группы, где аккаунт может писать. Telegram может запретить отправку в отдельные чаты.\n\n"
            "✉️ <b>3. Текст сообщения</b>\n"
            "Задайте и сохраните текст для рассылки. Перед запуском проверьте текст и список групп.\n\n"
            "📣 <b>4. Разовая рассылка</b>\n"
            "Откройте «Текст сообщения» или «Полезные функции» → «Рассылка». Бот покажет ход отправки, "
            "число успешных сообщений и ошибки с доступными причинами.\n\n"
            "⏱ <b>5. Авторассылка и интервал</b>\n"
            "В «Интервал» выберите время между завершением одного полного цикла и началом следующего. "
            "Например, 2 минуты — это 120 секунд после завершения цикла. Время самой отправки не входит в ожидание. "
            "Ограничения Telegram, FloodWait и медленный режим могут увеличить задержку.\n\n"
            "⏹ <b>6. Остановка</b>\n"
            "Используйте «Авторассылка» → «Автостоп» или «Полезные функции» → «Стоп авторассылки». "
            "При отключении аккаунта связанные задачи также останавливаются.\n\n"
            "🔁 <b>7. Автоответчик</b>\n"
            "Настройте ответы и включайте функцию только если она вам нужна. Проверьте сохранённые правила перед запуском.\n\n"
            "💎 <b>8. PRO</b>\n"
            "В «Pro тариф» можно посмотреть состояние и срок действия PRO. Администратор управляет доступом через админ-панель. "
            "PRO снимает тарифные ограничения самого бота, но не отменяет ограничения Telegram.\n\n"
            "📊 <b>9. Статистика и помощь</b>\n"
            "В статистике проверьте аккаунт, группы и число доступных чатов. Если отправка не удалась, читайте причины в сообщении "
            "о завершении цикла и логи Render.\n\n"
            "⚠️ <b>Правила безопасности</b>\n"
            "Рассылайте сообщения только в группы, где у вас есть право писать, соблюдайте правила групп и Telegram. "
            "Не пытайтесь обходить ограничения платформы.",
            parse_mode="HTML"
        )
    elif message.text == "🛒 Маркет":
        prices = [f"• {label}: {await db.get_price(key)} ⭐" for key, label in PRICE_KEYS.items()]
        await message.answer("🛒 <b>Маркет / цены</b>\n" + "\n".join(prices), parse_mode="HTML")
    elif message.text == "🤖 Авторассылка":
        await auto_menu(message)
    elif message.text == "⏹ Стоп авторассылки":
        await stop_auto(message)
    elif message.text == "⏱ Рассылка по таймеру":
        await timer_start(message, state)
    elif message.text in ("⏱ Интервал", "⏳ Интервал"):
        # Match both the current main-menu label and the legacy label.
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
    await message.answer("✅ Текущее действие отменено.", reply_markup=main_keyboard(await admin_check(message.from_user.id)))

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
    selected = sum(1 for group in groups if group["is_selected"])
    selected_available = sum(1 for group in groups if group["is_selected"] and group["can_send"])
    connected = bool(user and user["session_string"])
    account_count = await db.count_accounts(message.from_user.id)
    account_limit = 10 if (await admin_check(message.from_user.id) or await db.is_pro(message.from_user.id)) else 3
    pro_info = await db.get_pro_info(message.from_user.id)
    pro_status = (
        "💎 активен до " + pro_info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if pro_info else "не активен"
    )
    phone = str(user["phone"] or "") if user else ""
    safe_phone = (phone[:4] + "••••" + phone[-2:]) if len(phone) > 8 else ("скрыт" if phone else "не указан")
    text = (
        "👤 <b>Профиль Telegram</b>\n\n"
        f"Статус: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"Телефон: {html.escape(safe_phone)}\n"
        f"Аккаунтов: {account_count}/{account_limit}\n"
        f"PRO: {html.escape(pro_status)}\n"
        f"Групп найдено: {len(groups)}\nВыбрано групп: {selected}\nДоступно для отправки: {selected_available}\n\n"
        "Подключение пользовательского аккаунта используется для работы с вашими группами. "
        "Никому не передавайте коды входа и пароль 2FA."
    )
    # Keep both account actions visible in the profile at all times.
    rows = [
        [InlineKeyboardButton(text="🟢 Подключить аккаунт", callback_data="profile:connect")],
        [InlineKeyboardButton(text="🔴 Отключить аккаунт", callback_data="profile:disconnect")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ]
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "profile:disconnect")
async def profile_disconnect_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if not user or not user["session_string"]:
        await callback.answer("Аккаунт уже отключён.", show_alert=True)
        return
    for tasks in (auto_tasks, admin_campaign_tasks):
        task = tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
    await stop_autoreply_listener(user_id)
    await set_setting(f"autoreply_enabled:{user_id}", "0")
    await db.disable_auto_campaign(user_id)
    await db.clear_session(user_id)
    await set_setting(f"admin_campaign_enabled:{user_id}", "0")
    remaining_accounts = await db.count_accounts(user_id)
    await callback.answer("Аккаунт отключён.")
    if callback.message:
        status_text = (f"Активный аккаунт отключён. Осталось подключено: {remaining_accounts}.\nДругой аккаунт выбран активным." if remaining_accounts else "Аккаунт отключён. Подключённых аккаунтов больше нет.")
        await callback.message.edit_text(
            "🔌 <b>Управление аккаунтами</b>\n" + status_text + "\nАвторассылка и автоответчик остановлены.",
            parse_mode="HTML",
            reply_markup=with_close([[InlineKeyboardButton(text="👤 Открыть профиль", callback_data="profile:open")]])
        )


@dp.callback_query(F.data == "profile:open")
async def profile_open_callback(callback: CallbackQuery):
    if callback.message:
        await callback.message.delete()
        await profile_menu_from_callback(callback)
    await callback.answer()


async def profile_menu_from_callback(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    groups = await db.list_groups(callback.from_user.id)
    selected = sum(1 for group in groups if group["is_selected"])
    available = sum(1 for group in groups if group["is_selected"] and group["can_send"])
    connected = bool(user and user["session_string"])
    account_count = await db.count_accounts(callback.from_user.id)
    account_limit = 10 if (await admin_check(callback.from_user.id) or await db.is_pro(callback.from_user.id)) else 3
    pro_info = await db.get_pro_info(callback.from_user.id)
    pro_status = ("💎 активен до " + pro_info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")) if pro_info else "не активен"
    text = (
        "👤 <b>Профиль Telegram</b>\\n\\n"
        f"Статус аккаунта: {'🟢 подключён' if connected else '🔴 не подключён'}\\n"
        f"Выбрано групп: {selected}\\nДоступно для отправки: {available}\\n"
        f"PRO: {html.escape(pro_status)}"
    )
    rows = [
        [InlineKeyboardButton(text="🟢 Подключить аккаунт", callback_data="profile:connect")],
        [InlineKeyboardButton(text="🔴 Отключить аккаунт", callback_data="profile:disconnect")],
    ]
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "profile:connect")
async def profile_connect_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    current_accounts = await db.count_accounts(user_id)
    account_limit = 10 if (await admin_check(user_id) or await db.is_pro(user_id)) else 3
    if current_accounts >= account_limit:
        await callback.answer(f"Лимит аккаунтов: {account_limit}. Отключите один аккаунт или подключите PRO.", show_alert=True)
        return
    if not await charge_if_needed(user_id, "connect_price"):
        await callback.answer("Недостаточно звёзд.", show_alert=True)
        return
    await state.clear()
    await state.set_state(LoginFlow.phone)
    if callback.message:
        await callback.message.answer("Отправьте номер телефона.", reply_markup=phone_request_keyboard())
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
    current_accounts = await db.count_accounts(message.from_user.id)
    account_limit = 10 if (await admin_check(message.from_user.id) or await db.is_pro(message.from_user.id)) else 3
    if current_accounts >= account_limit:
        await message.answer(f"Достигнут лимит: {account_limit} аккаунтов. Отключите один аккаунт или подключите PRO.")
        return
    if not await charge_if_needed(message.from_user.id, "connect_price"):
        await message.answer("Недостаточно звёзд.")
        return
    await state.set_state(LoginFlow.phone)
    await message.answer("Отправьте номер телефона.", reply_markup=phone_request_keyboard())

@dp.message(LoginFlow.phone)
async def connect_phone(message: Message, state: FSMContext):
    if message.contact:
        if message.contact.user_id and message.contact.user_id != message.from_user.id:
            await message.answer("Отправьте именно свой номер через кнопку ниже.", reply_markup=phone_request_keyboard())
            return
        raw_phone = (message.contact.phone_number or "").strip()
    else:
        raw_phone = (message.text or "").strip()

    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    # Keep the known-working phone normalization for international and Tajik numbers.
    if raw_phone.startswith("+"):
        phone = "+" + digits
    elif digits.startswith("00"):
        phone = "+" + digits[2:]
    elif digits.startswith("992"):
        phone = "+" + digits
    elif digits.startswith("7"):
        phone = "+" + digits
    elif len(digits) == 9 and digits.startswith("9"):
        phone = "+992" + digits
    else:
        await message.answer(
            "Не удалось определить страну номера. Отправьте номер в международном формате, "
            "например +992XXXXXXXXX или +7XXXXXXXXXX, либо нажмите «📱 Отправить номер».",
            reply_markup=phone_request_keyboard(),
        )
        return

    if len(phone) < 8 or len(phone) > 20:
        await message.answer("Неверный номер. Нажмите «📱 Отправить номер» и попробуйте ещё раз.", reply_markup=phone_request_keyboard())
        return

    user_id = message.from_user.id
    # Discard any stale in-memory login client before starting a fresh login attempt.
    old_client = login_clients.pop(user_id, None)
    if old_client:
        with suppress(Exception):
            await old_client.disconnect()

    # Use the same straightforward Telethon connection/request sequence as the older
    # version that worked; avoid wrapping the login request in asyncio.wait_for.
    client = TelegramClient(StringSession(), config.api_id, config.api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        login_clients[user_id] = client
        requested_at = time.time()
        timeout = max(50, int(getattr(sent, "timeout", 0) or 0))
        next_type = getattr(sent, "next_type", None)
        await state.update_data(
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            login_code_requested_at=requested_at,
            login_code_timeout=timeout,
            login_code_next_type=type(next_type).__name__ if next_type else None,
        )
        await state.set_state(LoginFlow.code)

        delivery_type = type(getattr(sent, "type", None)).__name__
        delivery_hints = {
            "SentCodeTypeApp": "Telegram направил код в приложение Telegram на одном из уже авторизованных устройств; проверьте официальный чат Telegram.",
            "SentCodeTypeSms": "Telegram выбрал доставку кода по SMS на этот номер.",
            "SentCodeTypeCall": "Telegram выбрал доставку кода голосовым звонком.",
            "SentCodeTypeFlashCall": "Telegram выбрал автоматический звонок для подтверждения номера.",
            "SentCodeTypeMissedCall": "Telegram выбрал подтверждение через пропущенный звонок.",
            "SentCodeTypeEmailCode": "Telegram выбрал доставку кода на связанный адрес электронной почты.",
        }
        delivery_hint = delivery_hints.get(
            delivery_type,
            "Telegram принял запрос, но не сообщил обычный способ доставки кода.",
        )
        await message.answer(
            "✅ Запрос на вход принят Telegram!\n\n"
            f"📱 Номер: {html.escape(phone)}\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ ВАЖНО\n"
            "Проверьте официальный чат Telegram на своих уже авторизованных устройствах.\n"
            "Не отправляйте код и пароль 2FA посторонним лицам.\n\n"
            f"Способ доставки по ответу Telegram: {delivery_type}.\n{delivery_hint}\n\n"
            "Если Telegram отправил вам код и вы решили продолжить вход, следуйте подсказкам ниже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁 Посмотреть код", callback_data="login_view_code")],
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="login_resend_code")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="login_cancel")],
            ]),
        )
        log.info(
            "Telegram login code request accepted: user_id=%s delivery_type=%s next_type=%s timeout=%s phone_suffix=%s",
            user_id, delivery_type,
            type(next_type).__name__ if next_type else "None",
            timeout, phone[-4:],
        )
    except Exception as exc:
        login_clients.pop(user_id, None)
        with suppress(Exception):
            await client.disconnect()
        error_name = type(exc).__name__
        log.exception("Failed to request Telegram login code for user_id=%s (%s)", user_id, error_name)
        if error_name in {"PhoneNumberInvalidError", "PhoneNumberUnoccupiedError"}:
            error_text = "Неверный номер телефона. Проверьте код страны и попробуйте ещё раз."
        elif error_name in {"ApiIdInvalidError", "ApiIdPublishedFloodError"}:
            error_text = "Telegram отклонил API_ID/API_HASH. Проверьте их в переменных окружения Render."
        elif error_name in {"PhoneNumberBannedError", "PhoneNumberAppSignupForbiddenError"}:
            error_text = "Telegram не разрешает вход с этим номером. Используйте другой номер или обратитесь в поддержку Telegram."
        elif error_name in {"PhoneNumberFloodError", "FloodWaitError", "PhonePasswordFloodError"}:
            error_text = "Слишком много попыток входа. Подождите указанное Telegram время и попробуйте снова."
        else:
            error_text = f"Telegram не отправил код ({error_name}). Проверьте API_ID/API_HASH и попробуйте позже."
        await message.answer(error_text, reply_markup=phone_request_keyboard())

@dp.callback_query(F.data == "login_view_code")
async def login_view_code(callback: CallbackQuery):
    await callback.answer(
        "Проверьте официальный чат Telegram на уже авторизованных устройствах. "
        "Бот не может показать код, который Telegram отправил в другое приложение.",
        show_alert=True,
    )

@dp.callback_query(F.data == "login_cancel")
async def login_cancel(callback: CallbackQuery, state: FSMContext):
    await cleanup_login(callback.from_user.id, state)
    if callback.message:
        await callback.message.answer(
            "❌ Подключение аккаунта отменено.",
            reply_markup=main_keyboard(await admin_check(callback.from_user.id)),
        )
    await callback.answer("Подключение отменено")

@dp.callback_query(F.data == "login_resend_code")
async def resend_login_code(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    current_state = await state.get_state()
    if current_state != LoginFlow.code.state:
        await callback.answer("Сначала начните подключение аккаунта заново.", show_alert=True)
        return

    data = await state.get_data()
    client = login_clients.get(user_id)
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    if not client or not phone or not phone_code_hash:
        await callback.answer("Сессия входа потеряна. Начните подключение заново.", show_alert=True)
        await cleanup_login(user_id, state)
        return

    requested_at = float(data.get("login_code_requested_at") or 0)
    timeout = max(50, int(data.get("login_code_timeout") or 0))
    remaining = max(0, int(timeout - (time.time() - requested_at)))
    if remaining > 0:
        await callback.answer(f"Подождите ещё {remaining} сек. перед повторным запросом.", show_alert=True)
        return

    # Do not reject the resend locally just because next_type is absent.
    # Telegram's auth.resendCode response is authoritative; let the server return
    # SEND_CODE_UNAVAILABLE / FLOOD errors instead of inventing a local denial.
    try:
        sent = await client(functions.auth.ResendCodeRequest(phone, phone_code_hash))
        requested_at = time.time()
        next_type = getattr(sent, "next_type", None)
        timeout = max(50, int(getattr(sent, "timeout", 0) or 0))
        await state.update_data(
            phone_code_hash=sent.phone_code_hash,
            login_code_requested_at=requested_at,
            login_code_timeout=timeout,
            login_code_next_type=type(next_type).__name__ if next_type else None,
        )
        delivery_type = type(getattr(sent, "type", None)).__name__
        log.info(
            "Telegram login code resent: user_id=%s delivery_type=%s phone_suffix=%s",
            user_id, delivery_type, phone[-4:],
        )
        await callback.answer("Повторный запрос принят Telegram.")
        if callback.message:
            await callback.message.answer(
                "✅ Запрос кода принят Telegram.\n\n"
                "Каждый новый запрос может сделать предыдущий код недействительным. "
                "Подождите и проверьте официальный чат Telegram на своих устройствах.\n"
                "Повторный запрос будет доступен не раньше чем через 50 секунд "
                "(если Telegram требует больше времени — действует его ограничение).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👁 Посмотреть код", callback_data="login_view_code")],
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="login_resend_code")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="login_cancel")],
                ]),
            )
    except Exception as exc:
        log.exception("Telegram login code resend failed for user_id=%s", user_id)
        name = type(exc).__name__
        if name == "FloodWaitError":
            seconds = int(getattr(exc, "seconds", 0) or 0)
            message_text = (
                f"Telegram временно ограничил запросы. {seconds} секунд и повторите попытку."
                if seconds else
                "Telegram временно ограничил запросы. Подождите немного и попробуйте позже."
            )
        elif name in {"PhoneNumberFloodError", "PhonePasswordFloodError"}:
            message_text = "Telegram ограничил количество попыток. Не запрашивайте новый код сейчас; попробуйте позже."
        elif name in {"SendCodeUnavailableError", "PhoneCodeEmptyError"}:
            message_text = (
                "Telegram сейчас не предлагает другой способ отправки кода. "
                "Это ограничение сервера, а не ошибка бота. Проверьте код в активных сеансах Telegram."
            )
        elif name in {"PhoneCodeExpiredError", "PhoneCodeHashEmptyError", "PhoneCodeHashInvalidError"}:
            await cleanup_login(user_id, state)
            message_text = "Запрос кода больше не действует. Начните заново через «Подключить аккаунт»."
        else:
            message_text = f"Telegram дархости такрориро рад кард: {name}. Каме интизор шавед; рамзро пайдарпай талаб накунед."
        await callback.answer(message_text[:190], show_alert=True)


@dp.message(LoginFlow.code)
async def connect_code(message: Message, state: FSMContext):
    code_raw = (message.text or "").strip()
    # Пользователь может вводить код как 52.301; Telegram API получает только цифры.
    code = "".join(ch for ch in code_raw if ch.isdigit())
    if not code:
        await message.answer("Введите код, который прислал Telegram, например 52.301.")
        return
    data = await state.get_data()
    client = login_clients.get(message.from_user.id)
    if not client or not data.get("phone") or not data.get("phone_code_hash"):
        await cleanup_login(message.from_user.id, state)
        await message.answer("Сессия входа потеряна. Нажмите «🔗 Подключить аккаунт» и начните заново.")
        return
    # Telegram codes are usually 5 digits; reject obvious non-code input before
    # calling sign_in so a user's 2FA password is not mistaken for a login code.
    if len(code) not in (5, 6):
        await message.answer(
            "Это не похоже на код входа Telegram. Код обычно состоит из 5 цифр. "
            "Не отправляйте пароль 2FA на этом шаге — сначала нужен код Telegram."
        )
        return
    log.info(
        "Telegram login code submitted: user_id=%s code_length=%s phone_suffix=%s",
        message.from_user.id, len(code), str(data["phone"])[-4:],
    )
    try:
        await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await db.save_session(message.from_user.id, client.session.save(), data["phone"])
        await state.clear()
        login_clients.pop(message.from_user.id, None)
        await client.disconnect()
        await message.answer("✅ Аккаунт успешно подключён.")
    except SessionPasswordNeededError:
        log.info("Telegram accepted login code and requires 2FA password for user_id=%s", message.from_user.id)
        await state.set_state(LoginFlow.password)
        await message.answer("Код Telegram принят. Для этого аккаунта включена двухэтапная аутентификация (2FA). Теперь введите пароль 2FA. Пароль не сохраняется в базе данных.")
    except PhoneCodeInvalidError:
        await message.answer("Неверный код. Проверьте код Telegram и введите его ещё раз в формате 52.301.")
    except PhoneCodeExpiredError:
        await cleanup_login(message.from_user.id, state)
        await message.answer("Срок действия кода истёк. Нажмите «🔗 Подключить аккаунт» и запросите новый код.")
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

@dp.message(F.text == "🔌 Отключить аккаунт")
async def disconnect_account(message: Message):
    user_id = message.from_user.id
    user = await require_user(message)
    if not user or not user["session_string"]:
        await message.answer("⚪ Аккаунт не подключён. Нажмите «🔗 Подключить аккаунт», чтобы подключить его.", reply_markup=main_keyboard(await admin_check(user_id)))
        return
    for tasks in (auto_tasks, admin_campaign_tasks):
        task = tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
    await stop_autoreply_listener(user_id)
    await set_setting(f"autoreply_enabled:{user_id}", "0")
    await db.disable_auto_campaign(user_id)
    await db.clear_session(user_id)
    await set_setting(f"admin_campaign_enabled:{user_id}", "0")
    remaining_accounts = await db.count_accounts(user_id)
    status_text = (f"Активный аккаунт отключён. Осталось подключено: {remaining_accounts}; другой аккаунт выбран активным." if remaining_accounts else "Аккаунт отключён. Подключённых аккаунтов больше нет.")
    await message.answer("✅ " + status_text + " Авторассылка и рассылка администратора остановлены.", reply_markup=main_keyboard(await admin_check(user_id)))


async def cleanup_login(user_id: int, state: FSMContext):
    client = login_clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.disconnect()
    await state.clear()

def groups_keyboard(groups, selected_ids=None, page: int = 0):
    # Telegram inline keyboards have practical button-count limits. Pagination
    # allows selecting any number of groups without creating an oversized markup.
    page_size = 20
    pages = max(1, (len(groups) + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    visible = groups[page * page_size:(page + 1) * page_size]
    rows = []
    for group in visible:
        selected = (int(group["chat_id"]) in selected_ids) if selected_ids is not None else bool(group["is_selected"])
        mark = "✅" if selected else "⬜️"
        status = "" if group["can_send"] else " · нет прав"
        title = (group["title"] or str(group["chat_id"]))[:42]
        rows.append([InlineKeyboardButton(text=f"{mark} {title}{status}", callback_data=f"grp:{group['chat_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"grp:page:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"grp:page:{page+1}"))
    # Do not render a dead "1/1" pagination button when there is only one page.
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Обновить группы", callback_data="grp:refresh")])
    rows.append([InlineKeyboardButton(text="💾 Сохранить выбор", callback_data="grp:save")])
    return with_close(rows)

@dp.message(F.text == "👥 Группы")
async def groups_menu(message: Message):
    user = await require_user(message)
    user_id = message.from_user.id
    groups = await db.list_groups(user_id)
    group_selection_drafts[user_id] = {int(g["chat_id"]) for g in groups if g["is_selected"]}
    group_selection_pages[user_id] = 0

    if user and user["session_string"]:
        try:
            fresh = await sender.fetch_user_groups(user["session_string"])
            await db.upsert_groups(user_id, fresh)
            groups = await db.list_groups(user_id)
            if not groups:
                await message.answer("Группы не найдены.")
                return
        except Exception:
            log.exception("Automatic group refresh failed for user %s", user_id)
            await message.answer(
                "Не удалось обновить список групп. "
                "Показан последний сохранённый список."
            )

    if not groups:
        await message.answer("Список групп пуст. Подключите аккаунт и обновите группы.")
        return

    await message.answer(
        "Выберите группы для рассылки. Список обновлён автоматически. "
        "После выбора нажмите «Сохранить выбор».",
        reply_markup=groups_keyboard(groups, group_selection_drafts.get(user_id, set()), group_selection_pages.get(user_id, 0)),
    )

@dp.callback_query(F.data.startswith("grp:"))
async def group_toggle(callback: CallbackQuery):
    user_id = callback.from_user.id

    if callback.data == "grp:refresh":
        user = await db.get_user(user_id)
        if not user or not user["session_string"]:
            await callback.answer("Сначала подключите аккаунт.", show_alert=True)
            return
        if not await charge_if_needed(user_id, "groups_refresh_price"):
            await callback.answer("Недостаточно звёзд.", show_alert=True)
            return
        await callback.answer("🔄 Обновляю список групп…")
        status = await callback.message.answer("🔄 <b>Обновляем группы</b>\n⏳ Подключаемся к Telegram…", parse_mode="HTML") if callback.message else None
        try:
            async def _refresh_groups_operation():
                fresh = await sender.fetch_user_groups(user["session_string"])
                await db.upsert_groups(user_id, fresh)
                return await db.list_groups(user_id)
            if status:
                groups = await run_with_progress(status, _refresh_groups_operation(), (
                    "🔄 <b>Обновляем группы</b>\n⏳ Подключаемся к Telegram…",
                    "🔄 <b>Обновляем группы</b>\n📥 Загружаем список чатов…",
                    "🔄 <b>Обновляем группы</b>\n⚙️ Сохраняем изменения…",
                ))
                await status.edit_text(
                    "⚠️ Группы не найдены." if not groups else
                    "✅ <b>Группы обновлены!</b> Выберите нужные группы:",
                    reply_markup=groups_keyboard(groups, group_selection_drafts.get(user_id, set()), group_selection_pages.get(user_id, 0)),
                    parse_mode="HTML",
                )
            else:
                groups = await _refresh_groups_operation()
            if not status and callback.message:
                await callback.message.answer("Группы обновлены." if groups else "Группы не найдены.")
        except Exception as exc:
            log.exception("Group refresh failed for user %s", user_id)
            if status:
                with suppress(Exception):
                    await status.edit_text(f"❌ Не удалось обновить группы: <code>{html.escape(type(exc).__name__)}</code>", parse_mode="HTML")
            else:
                with suppress(Exception):
                    await bot.send_message(user_id, f"Не удалось обновить группы: {type(exc).__name__}")
        return

    if callback.data.startswith("grp:page:"):
        try:
            page = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректная страница", show_alert=True)
            return
        groups = await db.list_groups(user_id)
        pages = max(1, (len(groups) + 19) // 20)
        group_selection_pages[user_id] = max(0, min(page, pages - 1))
        draft = group_selection_drafts.setdefault(
            user_id, {int(g["chat_id"]) for g in groups if g["is_selected"]}
        )
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=groups_keyboard(groups, draft, group_selection_pages[user_id])
            )
        await callback.answer()
        return

    if callback.data == "grp:save":
        selected_ids = group_selection_drafts.get(user_id, set())
        groups = await db.save_selected_groups(user_id, selected_ids)
        group_selection_drafts.pop(user_id, None)
        chosen = sum(1 for g in groups if g["is_selected"])
        available = sum(1 for g in groups if g["is_selected"] and g["can_send"])
        await callback.answer(f"Сохранено: {chosen}; доступны для отправки: {available}", show_alert=True)
        if callback.message:
            await callback.message.edit_text(
                f"✅ Выбор сохранён.\n👥 Выбрано групп: {chosen}\n📨 Доступно для отправки: {available}\n\n"
                "Группы без прав остаются в выборе, но получить сообщение не смогут, пока права не появятся.",
                reply_markup=groups_keyboard(groups, page=group_selection_pages.get(user_id, 0)),
            )
        await refresh_auto_panel(user_id)
        return

    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная группа", show_alert=True)
        return
    groups = await db.list_groups(user_id)
    if not any(int(g["chat_id"]) == chat_id for g in groups):
        await callback.answer("Группа не найдена. Обновите список групп.", show_alert=True)
        return
    draft = group_selection_drafts.setdefault(
        user_id, {int(g["chat_id"]) for g in groups if g["is_selected"]}
    )
    if chat_id in draft:
        draft.remove(chat_id)
        selected = False
    else:
        draft.add(chat_id)
        selected = True
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=groups_keyboard(groups, draft, group_selection_pages.get(user_id, 0)))
    await callback.answer("Группа отмечена" if selected else "Группа снята; нажмите «Сохранить выбор»")

@dp.message(F.text == "🔄 Обновить группы")
async def refresh_groups(message: Message):
    user_id = message.from_user.id
    user = await require_user(message)
    if not user or not user["session_string"]:
        await message.answer("Сначала подключите аккаунт.")
        return
    if not await charge_if_needed(user_id, "groups_refresh_price"):
        await message.answer("Недостаточно звёзд.")
        return
    status = await message.answer("🔄 <b>Обновляем группы</b>\n⏳ Подключаемся к Telegram…", parse_mode="HTML")
    try:
        async def _refresh_groups_operation():
            fresh = await sender.fetch_user_groups(user["session_string"])
            await db.upsert_groups(user_id, fresh)
            return await db.list_groups(user_id)
        groups = await run_with_progress(status, _refresh_groups_operation(), (
            "🔄 <b>Обновляем группы</b>\n⏳ Подключаемся к Telegram…",
            "🔄 <b>Обновляем группы</b>\n📥 Загружаем список чатов…",
            "🔄 <b>Обновляем группы</b>\n⚙️ Сохраняем изменения…",
        ))
        if not groups:
            await status.edit_text("⚠️ Группы не найдены. Проверьте подключение аккаунта.")
            return
        await status.edit_text(
            f"✅ <b>Группы обновлены!</b>\n📊 Найдено: <b>{len(groups)}</b>",
            reply_markup=groups_keyboard(groups, group_selection_drafts.get(user_id, set()), group_selection_pages.get(user_id, 0)),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("Group refresh failed for user %s", user_id)
        with suppress(Exception):
            await status.edit_text(f"❌ Не удалось обновить группы: <code>{html.escape(type(exc).__name__)}</code>", parse_mode="HTML")

async def show_broadcast_text_menu(message: Message):
    user_id = message.from_user.id
    await require_user(message)
    current = await get_broadcast_text(user_id)
    preview = current if current else "Текст ещё не задан."
    if len(preview) > 3500:
        preview = preview[:3500] + "…"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="broadcast:edit")],
        [InlineKeyboardButton(text="📨 Отправить сейчас", callback_data="broadcast:send")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])
    await message.answer("✉️ <b>Текст сообщения</b>\n\n<pre>" + html.escape(preview) + "</pre>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "broadcast:edit")
async def edit_broadcast_text_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TextFlow.edit_broadcast_text)
    await callback.message.answer("Отправьте новый текст сообщения. Он сохранится и будет использоваться при следующих рассылках.")
    await callback.answer()

@dp.message(TextFlow.edit_broadcast_text)
async def edit_broadcast_text_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой. Отправьте новый текст сообщением.")
        return
    user_id = message.from_user.id
    await save_broadcast_text(user_id, text)
    try:
        async with db.get_pool().acquire() as con:
            await con.execute(
                "UPDATE auto_campaigns SET message_text=$2, updated_at=NOW() WHERE user_id=$1 AND enabled=TRUE",
                user_id, text,
            )
    except Exception:
        log.exception("Could not sync updated message into active auto-campaign for user %s", user_id)
        await message.answer("⚠️ Текст сохранён, но не удалось обновить активную авторассылку в базе. Проверьте соединение с БД.")
    await state.clear()
    await message.answer("✅ Новый текст сохранён. Обычная рассылка использует его сразу, авторассылка — со следующего цикла.")
    await show_broadcast_text_menu(message)

@dp.callback_query(F.data == "broadcast:send")
async def broadcast_send_saved(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    if not user or not user["session_string"]:
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    groups = await db.selected_groups(user_id)
    if not groups:
        try:
            await db.upsert_groups(user_id, await sender.fetch_user_groups(user["session_string"]))
            groups = await db.selected_groups(user_id)
        except Exception:
            log.exception("Automatic refresh before broadcast failed for user %s", user_id)
    if not groups:
        await callback.answer("Список групп пуст.", show_alert=True)
        return
    text = await get_broadcast_text(user_id)
    if not text:
        await callback.answer("Сначала нажмите «Изменить текст» и сохраните текст.", show_alert=True)
        return
    old = campaign_tasks.get(user_id)
    if old and not old.done():
        await callback.answer("Рассылка уже выполняется. Сначала остановите её.", show_alert=True)
        return
    if not await charge_if_needed(user_id, "broadcast_price"):
        await callback.answer("Недостаточно звёзд.", show_alert=True)
        return
    campaign_tasks[user_id] = asyncio.create_task(run_campaign(user_id, text, callback.message.chat.id))
    await callback.answer("Рассылка запущена")
    await callback.message.answer("📨 Рассылка запущена с сохранённым текстом.")

@dp.message(F.text == "📨 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    current = await get_broadcast_text(message.from_user.id)
    if not current:
        await message.answer("Сначала откройте «✉️ Текст сообщения» → «Изменить текст» и сохраните текст.")
        return
    user = await require_user(message)
    if not user["session_string"]:
        await message.answer("Сначала подключите аккаунт.")
        return
    groups = await db.selected_groups(message.from_user.id)
    if not groups:
        await message.answer("Список групп пуст.")
        return
    old = campaign_tasks.get(message.from_user.id)
    if old and not old.done():
        await message.answer("Рассылка уже выполняется. Сначала остановите её.")
        return
    if not await charge_if_needed(message.from_user.id, "broadcast_price"):
        await message.answer("Недостаточно звёзд.")
        return
    campaign_tasks[message.from_user.id] = asyncio.create_task(run_campaign(message.from_user.id, current, message.chat.id))
    await message.answer("📨 Рассылка запущена с сохранённым текстом.")

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
    stopped = await cancel_task_and_wait(campaign_tasks, message.from_user.id)
    if stopped:
        await message.answer("Рассылка остановлена. Теперь можно запустить новую.")
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

async def run_with_progress(status_message: Message, awaitable, frames: tuple[str, ...], interval: float = 1.1):
    """Run a slow Telegram/DB operation while updating a friendly progress message."""
    task = asyncio.create_task(awaitable)
    frame_index = 0
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            frame_index = (frame_index + 1) % len(frames)
            with suppress(Exception):
                await status_message.edit_text(frames[frame_index], parse_mode="HTML")
    return await task


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
        [InlineKeyboardButton(text="✉️ Текст сообщения", callback_data="auto:text")],
        [InlineKeyboardButton(text="👥 Группы авторассылки", callback_data="auto:groups")],
        [InlineKeyboardButton(text="📝 Сообщение для каждой группы", callback_data="auto:grouptexts")],
        [InlineKeyboardButton(text=f"⏱ Интервал: {AUTO_INTERVAL_LABELS.get(seconds, '5 мин')}", callback_data="auto:interval")],
        [action],
        [InlineKeyboardButton(text="✍️ Подпись: изменить", callback_data="auto:signature")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="ui:close")],
    ])


async def auto_panel_text(user_id: int) -> str:
    user = await db.get_user(user_id)
    all_groups = await db.list_groups(user_id)
    selected_total = sum(1 for group in all_groups if group["is_selected"])
    groups = [group for group in all_groups if group["is_selected"] and group["can_send"]]
    seconds = await get_auto_interval(user_id)
    running = user_id in auto_tasks and not auto_tasks[user_id].done()
    status = "🟢 Включена" if running else "🔴 Отключена"
    account = "✅ Подключён" if user and user["session_string"] else "❌ Не подключён"
    message_text = await get_broadcast_text(user_id)
    preview = html.escape(message_text[:700]) if message_text else "<i>Текст не задан</i>"
    if message_text and len(message_text) > 700:
        preview += "…"
    return (
        "🤖 <b>ПАНЕЛЬ АВТОРАССЫЛКИ</b>\n\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"👤 <b>Аккаунт:</b> {account}\n"
        f"👥 <b>Выбрано групп:</b> {selected_total}\n"
        f"📨 <b>Доступно для отправки:</b> {len(groups)}\n"
        f"✍️ <b>Подпись:</b> {'ВКЛ' if user and user['signature_enabled'] else 'ВЫКЛ'}\n"
        f"⏱ <b>Интервал:</b> {AUTO_INTERVAL_LABELS[seconds]}\n\n"
        f"✉️ <b>Текущий текст:</b>\n<pre>{preview}</pre>\n"
        "Сообщение отправляется только в группы, где аккаунт может писать. Изменения текста и выбора групп применяются в ходе следующего цикла.\n"
        "ℹ️ Интервал общий для меню «⏱ Интервал» и авторассылки: это пауза между завершением одного полного цикла и началом следующего. Между группами используется короткая техническая пауза."
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


@dp.callback_query(F.data == "auto:groups")
async def auto_groups_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    groups = await db.list_groups(user_id)
    group_selection_drafts[user_id] = {int(g["chat_id"]) for g in groups if g["is_selected"]}
    group_selection_pages[user_id] = 0
    refresh_error = None

    if not user or not user["session_string"]:
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return

    await callback.answer("🔄 Обновляю группы авторассылки…")
    status = await callback.message.answer("🔄 <b>Обновляем группы авторассылки</b>\n⏳ Загружаем чаты Telegram…", parse_mode="HTML") if callback.message else None
    try:
        async def _refresh_auto_groups_operation():
            fresh = await sender.fetch_user_groups(user["session_string"])
            await db.upsert_groups(user_id, fresh)
            return await db.list_groups(user_id)
        if status:
            groups = await run_with_progress(status, _refresh_auto_groups_operation(), (
                "🔄 <b>Обновляем группы авторассылки</b>\n⏳ Загружаем чаты Telegram…",
                "🔄 <b>Обновляем группы авторассылки</b>\n📥 Получаем актуальный список…",
                "🔄 <b>Обновляем группы авторассылки</b>\n⚙️ Обновляем данные…",
            ))
            await status.delete()
        else:
            groups = await _refresh_auto_groups_operation()
    except Exception as exc:
        log.exception("Could not refresh groups from auto panel for user %s", user_id)
        refresh_error = type(exc).__name__

    if not groups:
        if callback.message:
            with suppress(Exception):
                await callback.message.answer(
                    f"⚠️ Не удалось обновить группы: {refresh_error}" if refresh_error else "⚠️ Группы не найдены."
                )
        return

    if callback.message:
        text = (
            "👥 <b>Группы авторассылки</b>\n"
            "Отметьте нужные группы и нажмите «Сохранить выбор». "
            "Выбор здесь используется для вашей собственной авторассылки."
        )
        if refresh_error:
            text += "\n\nНе удалось обновить список. Показан последний сохранённый список."
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=groups_keyboard(groups, group_selection_drafts.get(user_id, set()), group_selection_pages.get(user_id, 0)),
        )


@dp.callback_query(F.data == "auto:grouptexts")
async def auto_group_texts_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer("Открываю список групп…")
    groups = await db.list_groups(user_id)
    selected_ids = {int(g["chat_id"]) for g in groups if bool(g["is_selected"])}
    groups = [g for g in groups if int(g["chat_id"]) in selected_ids]
    if not groups:
        await callback.answer("Сначала выберите группы в «Настройка групп» и сохраните выбор.", show_alert=True)
        return
    overrides = await db.get_group_auto_messages(user_id)
    rows = []
    for group in groups:
        chat_id = int(group["chat_id"])
        title = str(group["title"] or chat_id).strip()
        marker = "📝" if chat_id in overrides else "📄"
        label = f"{marker} {title[:38]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"auto:grouptext:{chat_id}")])
    rows.append([InlineKeyboardButton(text="🧹 Удалить отдельные тексты", callback_data="auto:grouptexts:clear")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="auto:back")])
    await callback.message.answer(
        "📝 <b>Отдельный текст для каждой группы</b>\n\n"
        "📄 — используется общий текст авторассылки.\n"
        "📝 — для группы сохранён отдельный текст. Нажмите на группу, чтобы изменить текст.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("auto:grouptext:"))
async def auto_group_text_edit_start(callback: CallbackQuery, state: FSMContext):
    try:
        chat_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная группа", show_alert=True); return
    groups = await db.list_groups(callback.from_user.id)
    group = next((g for g in groups if int(g["chat_id"]) == chat_id), None)
    if group is None:
        await callback.answer("Группа не найдена. Обновите список групп.", show_alert=True); return
    await state.set_state(TextFlow.group_auto_text)
    await state.update_data(group_auto_chat_id=chat_id)
    existing = (await db.get_group_auto_messages(callback.from_user.id)).get(chat_id)
    await callback.answer("Открываю редактор текста…")
    await callback.message.answer(
        f"📝 <b>Редактор текста группы</b>\n\n"
        f"Отправьте отдельный текст для группы <b>{html.escape(str(group['title']))}</b>.\n\n"
        "Этот текст будет использоваться только для этой группы в авторассылке. "
        "Для отмены нажмите кнопку ниже или «❌ Закрыть действие».",
        parse_mode="HTML",
        reply_markup=with_close([[InlineKeyboardButton(text="❌ Закрыть действие", callback_data="ui:close")]])
    )
    if existing:
        await callback.message.answer("Текущий отдельный текст:\n\n" + html.escape(existing[:3500]), parse_mode="HTML")
    await callback.answer()

@dp.message(TextFlow.group_auto_text)
async def auto_group_text_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пуст. Отправьте текст сообщением или отмените действие."); return
    data = await state.get_data()
    chat_id = data.get("group_auto_chat_id")
    if chat_id is None:
        await state.clear(); await message.answer("Сеанс редактирования истёк. Откройте авторассылку заново."); return
    try:
        await db.set_group_auto_message(message.from_user.id, int(chat_id), text)
    except Exception as exc:
        log.exception("Could not save group-specific auto message")
        await message.answer(f"Не удалось сохранить текст: {type(exc).__name__}"); return
    await state.clear()
    groups = await db.list_groups(message.from_user.id)
    group = next((g for g in groups if int(g["chat_id"]) == int(chat_id)), None)
    title = str(group["title"] or chat_id) if group else str(chat_id)
    await message.answer(
        f"✅ <b>Текст сохранён для группы:</b> {html.escape(title)}\n\n"
        "При следующем цикле авторассылки этот текст будет отправлен только в эту группу. "
        "В остальных выбранных группах останется общий текст или их собственный текст.",
        parse_mode="HTML",
        reply_markup=main_keyboard(await admin_check(message.from_user.id)),
    )

@dp.callback_query(F.data == "auto:grouptexts:clear")
async def auto_group_texts_clear(callback: CallbackQuery):
    async with db.get_pool().acquire() as con:
        await con.execute("DELETE FROM group_auto_messages WHERE user_id=$1", callback.from_user.id)
    await callback.answer("Отдельные тексты удалены. Для всех групп используется общий текст.", show_alert=True)

@dp.callback_query(F.data == "auto:text")
async def auto_text_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TextFlow.auto_text)
    if callback.message:
        await callback.message.answer("Отправьте новый текст авторассылки. После сохранения он будет запущен автоматически для выбранных групп.")
    await callback.answer()


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
        await db.set_delay(callback.from_user.id, seconds)
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
    # Acknowledge the inline-button press promptly so Telegram stops showing the spinner.
    await callback.answer()
    await callback.message.answer(
        "✍️ <b>Отправьте текст для авторассылки.</b>\n\n"
        "Сообщение будет регулярно отправляться в выбранные группы.\n"
        "Для отмены нажмите «❌ Закрыть действие» или отправьте /cancel.",
        reply_markup=main_keyboard(await admin_check(callback.from_user.id)), parse_mode="HTML")


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
        await message.answer("❌ Не удалось запустить. Проверьте подключение аккаунта и выбранные группы.", reply_markup=main_keyboard(await admin_check(message.from_user.id)))
        await refresh_auto_panel(user_id)
        return
    old = auto_tasks.get(user_id)
    if old and old.done():
        auto_tasks.pop(user_id, None)
        old = None
    if old and not old.cancelling() and not old.cancelled():
        await state.clear()
        await message.answer("⚠️ Авторассылка ещё выполняется. Нажмите «⏹ Стоп авторассылки» и дождитесь подтверждения.", reply_markup=main_keyboard(await admin_check(user_id)))
        await refresh_auto_panel(user_id)
        return
    if old and (old.cancelling() or old.cancelled()):
        await cancel_task_and_wait(auto_tasks, user_id)
    await state.clear()
    try:
        await save_broadcast_text(user_id, text)
        await db.save_auto_campaign(user_id, text, message.chat.id)
    except Exception as exc:
        log.exception("Could not persist auto-broadcast campaign for user %s", user_id)
        await message.answer(
            f"❌ Не удалось сохранить авторассылку: {html.escape(type(exc).__name__)}. Попробуйте ещё раз.",
            reply_markup=main_keyboard(await admin_check(message.from_user.id)),
            parse_mode="HTML",
        )
        await refresh_auto_panel(user_id)
        return
    seconds = await get_auto_interval(user_id)
    auto_tasks[user_id] = asyncio.create_task(run_auto_campaign(user_id, text, message.chat.id))
    await message.answer(
        f"🟢 <b>Авторассылка запущена!</b>\nИнтервал между циклами: {AUTO_INTERVAL_LABELS[seconds]}.\n"
        "Для остановки нажмите «⏹ Стоп авторассылки» или кнопку остановки в панели.",
        reply_markup=main_keyboard(await admin_check(message.from_user.id)), parse_mode="HTML")
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
            # Keep the configured interval measured start-to-start, not as an
            # additional sleep after a potentially long cycle. This avoids a
            # 5-minute setting behaving like 5 minutes + the campaign duration.
            cycle_started_at = asyncio.get_running_loop().time()
            progress = None
            try:
                log.info("Auto-broadcast cycle starting for user %s", user_id)
                current_text = await get_broadcast_text(user_id)
                if not current_text.strip():
                    campaign = await db.get_auto_campaign(user_id)
                    current_text = str(campaign["message_text"] or "").strip() if campaign else text
                if not current_text.strip():
                    raise RuntimeError("Текст авторассылки пустой; сохраните новый текст.")
                # Show an animated progress status while Telegram sends to all groups.
                progress = await bot.send_message(chat_id, "📨 <b>Авторассылка выполняется</b>\n⏳ Подготавливаю отправку…", parse_mode="HTML")
                async def _send_auto_texts_per_group():
                    overrides = await db.get_group_auto_messages(user_id)
                    selected = await db.selected_groups(user_id)
                    log.info("Auto campaign user=%s selected_groups=%s individual_texts=%s",
                             user_id, len(selected), len(overrides))
                    if not overrides:
                        return await sender.send_campaign(user_id, current_text, live_refresh=True)
                    totals = {"sent": 0, "failed": 0, "errors": [], "stopped": False}
                    for group in selected:
                        group_id = int(group["chat_id"])
                        group_title = str(group["title"] or group_id)
                        group_text = str(overrides.get(group_id, current_text) or "").strip()
                        if not group_text:
                            totals["failed"] += 1
                            totals["errors"].append(f"{group_title}: пустой текст")
                            continue
                        try:
                            # Selected groups use their own saved text, or the shared text as fallback.
                            result_one = await sender.send_campaign(
                                user_id, group_text, group_chat_ids=[group_id], live_refresh=True
                            )
                            totals["sent"] += int(result_one.get("sent", 0))
                            totals["failed"] += int(result_one.get("failed", 0))
                            totals["errors"].extend(result_one.get("errors", []))
                            totals["stopped"] = totals["stopped"] or bool(result_one.get("stopped"))
                            if totals["stopped"]:
                                break
                        except asyncio.CancelledError:
                            raise
                        except Exception as group_exc:
                            totals["failed"] += 1
                            totals["errors"].append(f"{group_title}: {type(group_exc).__name__}: {group_exc}")
                            log.exception("Per-group auto broadcast failed user=%s group=%s", user_id, group_id)
                    totals["errors"] = totals["errors"][:20]
                    return totals
                result = await run_with_progress(progress, _send_auto_texts_per_group(), (
                    "📨 <b>Авторассылка выполняется</b>\n⏳ Подключаемся к Telegram…",
                    "📨 <b>Авторассылка выполняется</b>\n📤 Отправляем сообщения в группы…",
                    "📨 <b>Авторассылка выполняется</b>\n🔄 Проверяем актуальные настройки…",
                ), interval=1.2)
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
                    await progress.edit_text(msg, parse_mode="HTML")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # An unauthorized Telegram session is not transient: stop the loop
                # and ask the owner to reconnect instead of retrying forever.
                if isinstance(exc, RuntimeError) and any(
                    marker in str(exc).lower()
                    for marker in ("сессия больше не авторизована", "сессия истекла", "не авторизована")
                ):
                    log.error("Auto-broadcast stopped for user %s because Telegram session is unauthorized", user_id)
                    with suppress(Exception):
                        await db.disable_auto_campaign(user_id)
                    with suppress(Exception):
                        await bot.send_message(
                            chat_id,
                            "⚠️ Авторассылка остановлена: сессия Telegram больше не авторизована. "
                            "Подключите аккаунт заново и запустите рассылку снова.",
                        )
                    break
                # Retry transient DB/network failures after the configured interval.
                log.exception("Auto-broadcast cycle failed for user %s; retrying after interval", user_id)
                with suppress(Exception):
                    error_text = f"⚠️ <b>Цикл завершился с ошибкой</b>\n<code>{html.escape(type(exc).__name__)}</code>\nПовторю через выбранный интервал."
                    if progress is not None:
                        await progress.edit_text(error_text, parse_mode="HTML")
                    else:
                        await bot.send_message(chat_id, error_text, parse_mode="HTML")
            interval = await get_auto_interval(user_id)
            elapsed = asyncio.get_running_loop().time() - cycle_started_at
            remaining = max(0.0, float(interval) - elapsed)
            log.info(
                "Auto-broadcast user %s cycle elapsed=%.1fs configured_interval=%ss remaining_wait=%.1fs",
                user_id, elapsed, interval, remaining,
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
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
    await message.answer(text, reply_markup=main_keyboard(await admin_check(user_id)))
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
    # Keep the interval controls consistent instead of showing different values.
    if delay in AUTO_INTERVALS:
        await save_auto_interval(callback.from_user.id, delay)
    await callback.answer(f"Интервал сохранён: {delay//60} мин во всех меню")
    await refresh_auto_panel(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "✍️ Подпись")
async def signature_menu(message: Message):
    user = await require_user(message)
    enabled = user["signature_enabled"]
    if enabled:
        kb = with_close([[InlineKeyboardButton(text="⭐ Отключить подпись", callback_data="signature:off")]])
        await message.answer("Подпись включена:\nотправлено через @VertonaBot", reply_markup=kb)
    else:
        kb = with_close([[InlineKeyboardButton(text="✅ Включить подпись", callback_data="signature:on")]])
        await message.answer("Подпись выключена.", reply_markup=kb)

async def turn_signature_off_or_invoice(user_id: int, chat_id: int, callback: CallbackQuery | None = None):
    user = await db.get_user(user_id)
    if not user:
        if callback: await callback.answer("Пользователь не найден", show_alert=True)
        return
    if not user["signature_enabled"]:
        if callback: await callback.answer("Подпись уже отключена")
        return
    price = await db.get_price("unsign_price")
    if price <= 0 or await admin_check(user_id) or await db.is_pro(user_id):
        await db.set_signature(user_id, False)
        if callback: await callback.answer("Подпись отключена бесплатно")
        await bot.send_message(chat_id, "✅ Подпись отключена.")
        await refresh_auto_panel(user_id)
        return
    await bot.send_invoice(
        chat_id=chat_id,
        title="Отключение подписи VertonaBot",
        description="Отключение автоматической подписи в ваших сообщениях.",
        payload=f"unsign:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Отключение подписи", amount=price)],
        provider_token="",
    )
    if callback: await callback.answer("Оплатите счёт Telegram Stars для отключения подписи")


@dp.callback_query(F.data.startswith("signature:"))
async def signature_toggle(callback: CallbackQuery):
    enabled = callback.data.endswith("on")
    if enabled:
        await db.set_signature(callback.from_user.id, True)
        await callback.answer("Подпись включена")
        await refresh_auto_panel(callback.from_user.id)
        if callback.message: await callback.message.edit_reply_markup(reply_markup=None)
        return
    await turn_signature_off_or_invoice(callback.from_user.id, callback.from_user.id, callback)


@dp.callback_query(F.data == "auto:signature")
async def auto_signature_toggle(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True); return
    if user["signature_enabled"]:
        await turn_signature_off_or_invoice(callback.from_user.id, callback.from_user.id, callback)
    else:
        await db.set_signature(callback.from_user.id, True)
        await callback.answer("Подпись включена")
        await refresh_auto_panel(callback.from_user.id)


@dp.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload or ""
    if payload != f"unsign:{query.from_user.id}" or query.currency != "XTR":
        await query.answer(ok=False, error_message="Счёт недействителен. Откройте меню подписи и попробуйте снова.")
        return
    user = await db.get_user(query.from_user.id)
    if not user or not user["signature_enabled"]:
        await query.answer(ok=False, error_message="Подпись уже отключена или пользователь не найден.")
        return
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def stars_payment_success(message: Message):
    payment = message.successful_payment
    if not payment or payment.invoice_payload != f"unsign:{message.from_user.id}":
        return
    await db.set_signature(message.from_user.id, False)
    await message.answer("⭐ Оплата получена. Подпись отключена.")
    await refresh_auto_panel(message.from_user.id)

@dp.message(F.text == "⭐ Мои звёзды")
async def my_stars(message: Message):
    user = await require_user(message)
    await message.answer(f"⭐ Ваш баланс: {user['stars']} звёзд")

@dp.message(F.text == "⏱ Рассылка по таймеру")
async def timer_start(message: Message, state: FSMContext):
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
    text = (data.get("timer_text", "") or "").strip()
    if not text:
        await message.answer("Текст таймера пуст. Начните создание таймера заново.")
        await state.clear()
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("session_string"):
        await message.answer("Сначала подключите аккаунт Telegram.")
        await state.clear()
        return
    if not await db.selected_groups(message.from_user.id):
        await message.answer("Сначала выберите группы.")
        await state.clear()
        return
    if not await charge_if_needed(message.from_user.id, "timer_price"):
        await message.answer("Недостаточно звёзд.")
        await state.clear()
        return
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
    rows = []
    for u in chunk:
        username = f"@{u['username']}" if u["username"] else "без username"
        connected = "🟢 подключён" if u["connected"] else "⚪ не подключён"
        safe_name = html.escape(str(u['full_name'] or 'Без имени'))
        safe_username = html.escape(username)
        pro_info = await db.get_pro_info(int(u["user_id"]))
        pro_badge = (
            " · 💎 PRO до " + pro_info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
            if pro_info else ""
        )
        lines.append(f"• <b>{safe_name}</b>\n  ID: <code>{u['user_id']}</code> · {safe_username}\n  ⭐ {u['stars']} · {connected}{pro_badge}")
        display_name = ("@" + str(u["username"])) if u["username"] else str(u["user_id"])
        rows.append([
            InlineKeyboardButton(text=f"⚙️ Управление {display_name[:18]}", callback_data=f"user:detail:{u['user_id']}"),
            InlineKeyboardButton(text="➕ Рассылка", callback_data=f"user:send:{u['user_id']}")
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
            "Отправьте имя пользователя канала (@username) или ID канала. Для приватного канала укажите ID и ссылку-приглашение через символ |\n"
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
        writer.writerow(["user_id", "username", "full_name", "stars", "connected", "pro_expires_at", "created_at"])
        for u in users:
            pro_info = await db.get_pro_info(int(u["user_id"]))
            writer.writerow([
                u["user_id"], u["username"], u["full_name"], u["stars"], u["connected"],
                pro_info["expires_at"].isoformat() if pro_info else "",
                u["created_at"],
            ])
        await callback.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="VertonaBot_users.csv"))
    elif action == "pro_add":
        await state.set_state(AdminFlow.pro_user_id)
        await callback.message.answer("💎 <b>Выдать PRO</b>\n\nОтправьте Telegram ID пользователя.", parse_mode="HTML")
    elif action == "pro_list":
        await show_pro_users_page(callback.message, 0)
    elif action == "users":
        await show_users_page(callback.message, 0)
    elif action == "stats":
        s = await db.stats()
        await callback.message.answer(
            f"📊 Статистика\nПользователи: {s['users']}\nПодключённые аккаунты: {s['connected']}\nГруппы: {s['groups']}\nОжидающие задачи: {s['tasks_pending']}\nДополнительные администраторы: {s['admins']}\nАктивные PRO: {s['pro_users']}"
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
    await callback.message.answer("Отправьте имя пользователя (@username) или ID канала/группы (например, @mychannel). Для кнопки подписки лучше использовать публичный username или ссылку.")
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
        await message.answer("Отправьте имя пользователя канала (@username) или ID канала.")
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
        await message.answer("Отправьте текст для отдельной рассылки администратора. Сохранённый текст самого пользователя не изменится.")
    except ValueError:
        await message.answer("ID должен быть числом.")

@dp.message(AdminFlow.admin_user_broadcast_text)
async def admin_user_broadcast_text(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear(); return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой. Отправьте текст обычным сообщением.")
        return
    await state.update_data(admin_campaign_text=text)
    await state.set_state(AdminFlow.admin_user_broadcast_interval)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 минута", callback_data="admincamp:interval:60"), InlineKeyboardButton(text="5 минут", callback_data="admincamp:interval:300")],
        [InlineKeyboardButton(text="10 минут", callback_data="admincamp:interval:600"), InlineKeyboardButton(text="30 минут", callback_data="admincamp:interval:1800")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admincamp:cancel")],
    ])
    await message.answer("⏱ Выберите интервал только для текста администратора. Текст и интервал авторассылки пользователя останутся без изменений.", reply_markup=kb)

@dp.callback_query(F.data.startswith("admincamp:"))
async def admin_campaign_controls(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split(":")
    if parts[1] == "cancel":
        await state.clear()
        await callback.message.edit_text("Рассылка администратора отменена.")
        await callback.answer()
        return
    if parts[1] == "stop":
        target_id = int(parts[2])
        await set_setting(f"admin_campaign_enabled:{target_id}", "0")
        task = admin_campaign_tasks.get(target_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            await callback.answer("Рассылка остановлена")
            if callback.message:
                await callback.message.answer(f"⏹ Рассылка администратора для пользователя {target_id} остановлена. Авторассылка самого пользователя не затронута.")
        else:
            await callback.answer("Рассылка уже остановлена")
        return
    if parts[1] == "start":
        target_id = int(parts[2])
        target = await db.get_user(target_id)
        saved_text = await get_setting(f"admin_campaign_text:{target_id}", "") or ""
        saved_interval = int(await get_setting(f"admin_campaign_interval:{target_id}", "300") or "300")
        admin_owner = int(await get_setting(f"admin_campaign_admin:{target_id}", str(callback.from_user.id)) or callback.from_user.id)
        if not target or not target["session_string"]:
            await callback.answer("Аккаунт пользователя не подключён", show_alert=True); return
        if not saved_text:
            await callback.answer("Сначала добавьте текст рассылки", show_alert=True); return
        chosen_raw = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
        if not [x for x in chosen_raw.split(",") if x.strip().lstrip("-").isdigit()]:
            await callback.answer("Сначала выберите группы в карточке пользователя", show_alert=True); return
        # A prior owner authorization is reused; no per-campaign approval is needed.
        approved = (await get_setting(f"admin_campaign_approved:{target_id}", "0")) == "1"
        if not approved:
            pending_admin_campaigns[target_id] = (saved_text, admin_owner, saved_interval)
            consent_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Разрешить рассылки администратора", callback_data=f"userconsent:approve:{target_id}")],
                [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"userconsent:decline:{target_id}")],
            ])
            await bot.send_message(target_id, "🔔 Для использования вашего подключённого аккаунта в рассылках администратора требуется ваше разрешение один раз. После этого администратор сможет запускать и останавливать только свою отдельную рассылку.", reply_markup=consent_kb)
            await callback.answer("Ожидается первоначальное разрешение пользователя")
            return
        await set_setting(f"admin_campaign_enabled:{target_id}", "1")
        old = admin_campaign_tasks.get(target_id)
        if old and not old.done():
            old.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await old
        admin_campaign_tasks[target_id] = asyncio.create_task(run_admin_campaign(target_id, saved_text, admin_owner, saved_interval))
        await callback.answer("Отдельная рассылка администратора запущена")
        if callback.message:
            await callback.message.answer(f"▶️ Рассылка администратора для пользователя {target_id} запущена. Авторассылка пользователя не затронута.")
        return
    if parts[1] != "interval":
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    seconds = int(parts[2])
    data = await state.get_data()
    target_id = data.get("target_id")
    text = data.get("admin_campaign_text")
    if not target_id or not text:
        await state.clear()
        await callback.answer("Данные рассылки устарели. Начните заново.", show_alert=True)
        return
    target = await db.get_user(int(target_id))
    if not target or not target["session_string"]:
        await state.clear()
        await callback.answer("Аккаунт пользователя не подключён", show_alert=True)
        return
    await set_setting(f"admin_campaign_text:{target_id}", str(text))
    await set_setting(f"admin_campaign_interval:{target_id}", str(seconds))
    await set_setting(f"admin_campaign_admin:{target_id}", str(callback.from_user.id))
    if await get_setting(f"admin_campaign_groups:{target_id}", None) is None:
        selected = await db.selected_groups(int(target_id))
        await set_setting(f"admin_campaign_groups:{target_id}", ",".join(str(int(g["chat_id"])) for g in selected))
    chosen_raw = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
    if not [x for x in chosen_raw.split(",") if x.strip().lstrip("-").isdigit()]:
        await callback.answer("Сначала выберите группы в карточке пользователя → «Группы рассылки администратора».", show_alert=True)
        return
    selected_admin_ids = {int(x) for x in chosen_raw.split(",") if x.strip().lstrip("-").isdigit()}
    all_target_groups = await db.list_groups(int(target_id))
    selected_admin_names = [str(g["title"] or g["chat_id"]) for g in all_target_groups if int(g["chat_id"]) in selected_admin_ids and g["can_send"]]
    await state.clear()
    target_id = int(target_id)
    approved = (await get_setting(f"admin_campaign_approved:{target_id}", "0")) == "1"
    if approved:
        await set_setting(f"admin_campaign_enabled:{target_id}", "1")
        old = admin_campaign_tasks.get(target_id)
        if old and not old.done():
            old.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await old
        admin_campaign_tasks[target_id] = asyncio.create_task(run_admin_campaign(target_id, str(text), callback.from_user.id, seconds))
        await callback.message.answer(
            f"▶️ Отдельная рассылка администратора для пользователя <code>{target_id}</code> запущена.\n"
            "Собственная авторассылка пользователя, её текст и группы не изменены.", parse_mode="HTML")
        await callback.answer("Рассылка запущена")
        return
    pending_admin_campaigns[target_id] = (str(text), callback.from_user.id, seconds)
    consent_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разрешить рассылки администратора", callback_data=f"userconsent:approve:{target_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"userconsent:decline:{target_id}")],
    ])
    try:
        await bot.send_message(
            target_id,
            "🔔 Для использования вашего подключённого аккаунта в рассылках администратора требуется первоначальное разрешение. Оно сохранится для следующих запусков; свою авторассылку вы сохраняете отдельно.",
            reply_markup=consent_kb)
        await callback.message.answer(
            f"⏳ Запрос первоначального разрешения отправлен пользователю <code>{target_id}</code>.\n"
            "После разрешения повторное подтверждение для каждой рассылки не потребуется.", parse_mode="HTML")
        await callback.answer("Ожидается первоначальное разрешение")
    except Exception as exc:
        pending_admin_campaigns.pop(target_id, None)
        await callback.answer("Не удалось отправить запрос разрешения", show_alert=True)
        log.warning("Could not request initial campaign authorization from %s: %s", target_id, exc)

@dp.callback_query(F.data.startswith("userconsent:"))
async def user_consent_admin_campaign(callback: CallbackQuery):
    parts = callback.data.split(":")
    target_id = int(parts[2])
    if callback.from_user.id != target_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return
    if parts[1] == "revoke":
        await set_setting(f"admin_campaign_enabled:{target_id}", "0")
        await set_setting(f"admin_campaign_approved:{target_id}", "0")
        task = admin_campaign_tasks.get(target_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        await callback.answer("Разрешение отозвано")
        if callback.message:
            await callback.message.edit_text("🚫 Разрешение на отдельную рассылку администратора отозвано. Ваша собственная авторассылка не затронута.")
        admin_id = int(await get_setting(f"admin_campaign_admin:{target_id}", "0") or "0")
        if admin_id:
            with suppress(Exception): await bot.send_message(admin_id, f"Пользователь {target_id} отозвал разрешение на рассылку администратора.")
        return
    pending = pending_admin_campaigns.get(target_id)
    if not pending:
        await callback.answer("Запрос больше не действует.", show_alert=True)
        return
    text, admin_id, interval = pending
    pending_admin_campaigns.pop(target_id, None)
    if parts[1] == "decline":
        await set_setting(f"admin_campaign_enabled:{target_id}", "0")
        await set_setting(f"admin_campaign_approved:{target_id}", "0")
        with suppress(Exception):
            await bot.send_message(admin_id, f"❌ Пользователь {target_id} отклонил рассылку администратора.")
        await callback.message.edit_text("Вы отклонили эту рассылку. Ваш аккаунт не будет использоваться для неё.")
        await callback.answer("Отклонено")
        return
    await set_setting(f"admin_campaign_enabled:{target_id}", "1")
    await set_setting(f"admin_campaign_approved:{target_id}", "1")
    await set_setting(f"admin_campaign_text:{target_id}", text)
    await set_setting(f"admin_campaign_interval:{target_id}", str(interval))
    await set_setting(f"admin_campaign_admin:{target_id}", str(admin_id))
    old = admin_campaign_tasks.get(target_id)
    if old and not old.done():
        old.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await old
    admin_campaign_tasks[target_id] = asyncio.create_task(run_admin_campaign(target_id, text, admin_id, interval))
    await callback.message.edit_text(
        "✅ Вы разрешили эту рассылку. Ваш сохранённый текст и авторассылка не изменены. Можно отозвать разрешение в любой момент.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отозвать разрешение", callback_data=f"userconsent:revoke:{target_id}")]])
    )
    await bot.send_message(admin_id, f"🟢 Рассылка началась после согласия пользователя {target_id}. Интервал: {interval // 60 if interval >= 60 else interval} {'мин.' if interval >= 60 else 'сек.'}.")
    await callback.answer("Рассылка разрешена")

async def run_admin_campaign(target_id: int, text: str, admin_id: int, interval: int):
    """Separate admin-authored recurring campaign; does not overwrite user's saved text."""
    try:
        while True:
            try:
                raw_group_ids = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
                group_ids = [int(x) for x in raw_group_ids.split(",") if x.strip().lstrip("-").isdigit()]
                if not group_ids:
                    raise RuntimeError("Не выбраны группы для рассылки администратора. Откройте «Группы рассылки администратора».")
                result = await sender.send_to_account(target_id, text, group_chat_ids=group_ids)
                report = (f"📨 <b>Цикл рассылки администратора завершён</b>\n"
                          f"Пользователь: <code>{target_id}</code>\n"
                          f"✅ Отправлено: {result.get('sent', 0)}\n"
                          f"⚠️ Ошибок: {result.get('failed', 0)}")
                if result.get("errors"):
                    report += "\n\n" + "\n".join(f"• {html.escape(str(e))}" for e in result["errors"][:5])
                await bot.send_message(admin_id, report, parse_mode="HTML")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Admin campaign failed for target user %s", target_id)
                with suppress(Exception):
                    await bot.send_message(admin_id, f"⚠️ Ошибка рассылки для пользователя {target_id}: {html.escape(type(exc).__name__)}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("Admin campaign stopped for target user %s", target_id)
        raise
    finally:
        current = admin_campaign_tasks.get(target_id)
        if current is asyncio.current_task():
            admin_campaign_tasks.pop(target_id, None)

async def show_pro_users_page(message: Message, page: int = 0):
    page_size = 10
    total = await db.count_pro_users()
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    users = await db.list_pro_users(page_size, page * page_size)
    rows = []
    lines = [f"💎 <b>Пользователи PRO</b> · активных: {total} · страница {page + 1}/{pages}", ""]
    if not users:
        lines.append("Список PRO пуст.")
    for item in users:
        username = "@" + str(item["username"]) if item["username"] else "без username"
        expiry = item["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"• {html.escape(str(item['full_name'] or 'Без имени'))}\n"
            f"  ID: <code>{item['user_id']}</code> · {html.escape(username)}\n"
            f"  Окончание: <code>{expiry}</code>"
        )
        rows.append([InlineKeyboardButton(
            text=f"⚙️ {('@' + str(item['username'])) if item['username'] else str(item['user_id'])}"[:55],
            callback_data=f"user:detail:{item['user_id']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prolist:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Далее ➡️", callback_data=f"prolist:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Выдать PRO", callback_data="adm:pro_add")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"prolist:{page}")])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=with_close(rows))


@dp.callback_query(F.data.startswith("prolist:"))
async def pro_users_page_callback(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        page = 0
    await show_pro_users_page(callback.message, page)
    await callback.answer()


async def begin_pro_grant(target_id: int, admin_id: int, state: FSMContext, message: Message):
    await state.update_data(pro_target_id=target_id)
    await state.set_state(AdminFlow.pro_days)
    await message.answer(
        f"💎 На сколько дней выдать PRO пользователю <code>{target_id}</code>?\n"
        "Отправьте число, например: <code>30</code>.\n"
        "При продлении новые дни добавляются к текущему сроку действия.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("user:pro:grant:"))
async def admin_pro_grant_from_detail(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        target_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID", show_alert=True)
        return
    await begin_pro_grant(target_id, callback.from_user.id, state, callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("user:pro:remove:"))
async def admin_pro_remove(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        target_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID", show_alert=True)
        return
    removed = await db.remove_pro(target_id)
    await db.log_admin_action(callback.from_user.id, "remove_pro", target_id)
    if removed:
        with suppress(Exception):
            await bot.send_message(
                target_id,
                "⚪ Срок вашего VertonaBot PRO был завершён администратором. "
                "Функции, которые входят в платный тариф, снова работают по обычным условиям.",
            )
        await admin_user_detail(callback)
    else:
        await callback.answer("У пользователя нет записи PRO", show_alert=True)


@dp.message(AdminFlow.pro_user_id)
async def admin_pro_user_id(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        target_id = int(raw)
        if target_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Неверный Telegram ID. Отправьте положительный числовой ID.")
        return
    await begin_pro_grant(target_id, message.from_user.id, state, message)


@dp.message(AdminFlow.pro_days)
async def admin_pro_days(message: Message, state: FSMContext):
    if not await admin_check(message.from_user.id):
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
        if days < 1 or days > 36500:
            raise ValueError
    except ValueError:
        await message.answer("Введите число дней от 1 до 36500, например 30.")
        return
    data = await state.get_data()
    target_id = int(data.get("pro_target_id") or 0)
    if target_id <= 0:
        await state.clear()
        await message.answer("ID пользователя потерян. Начните выдачу PRO заново.")
        return
    try:
        info = await db.grant_pro(target_id, days, message.from_user.id)
        await db.log_admin_action(message.from_user.id, "grant_pro", target_id, {"days": days, "expires_at": info["expires_at"].isoformat()})
    except Exception as exc:
        log.exception("Could not grant PRO to user %s", target_id)
        await state.clear()
        await message.answer(f"Не удалось выдать PRO: {type(exc).__name__}.")
        return
    await state.clear()
    expiry = info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await message.answer(
        f"✅ PRO выдан пользователю <code>{target_id}</code> на {days} дн.\n"
        f"Срок окончания: <code>{expiry}</code>.",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_id,
            "💎 <b>Вам активирован VertonaBot PRO!</b>\n\n"
            f"Срок: {days} дн.\nДо: <code>{expiry}</code>\n\n"
            "Вам доступны бесплатно платные функции, отключение подписи и выбор групп без тарифного лимита. "
            "Технические ограничения Telegram и права аккаунта сохраняются.",
            parse_mode="HTML",
        )
        await message.answer("📩 Уведомление пользователю отправлено.")
    except Exception as exc:
        await message.answer(
            "⚠️ PRO сохранён, но уведомление не отправлено. Пользователь должен открыть бота через /start. "
            f"Причина: {type(exc).__name__}."
        )


@dp.callback_query(F.data.startswith("user:detail:"))
async def admin_user_detail(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    user = await db.get_user(target_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    groups = await db.list_groups(target_id)
    selected = [g for g in groups if g["is_selected"] and g["can_send"]]
    own_campaign = await db.get_auto_campaign(target_id)
    own_running = bool(own_campaign and own_campaign["enabled"])
    admin_running = bool((admin_campaign_tasks.get(target_id) and not admin_campaign_tasks[target_id].done()))
    admin_text = await get_setting(f"admin_campaign_text:{target_id}", "") or ""
    username = "@" + str(user["username"]) if user["username"] else "не указан"
    text_preview = html.escape(admin_text[:500]) if admin_text else "не задан"
    if len(admin_text) > 500:
        text_preview += "…"
    pro_info = await db.get_pro_info(target_id)
    pro_status = (
        "🟢 PRO до " + pro_info["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if pro_info else "⚪ PRO не активен"
    )
    pro_buttons = (
        [InlineKeyboardButton(text="➕ Продлить / дать PRO", callback_data=f"user:pro:grant:{target_id}"),
         InlineKeyboardButton(text="🗑 Удалить PRO", callback_data=f"user:pro:remove:{target_id}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        pro_buttons,
        [InlineKeyboardButton(text="🤖 Авторассылка: добавить / изменить текст", callback_data=f"user:send:{target_id}")],
        [InlineKeyboardButton(text="👥 Группы рассылки администратора", callback_data=f"user:groups:{target_id}")],
        [InlineKeyboardButton(text=("⏹ Отключить рассылку админа" if admin_running else "▶️ Запустить рассылку админа"), callback_data=f"admincamp:{'stop' if admin_running else 'start'}:{target_id}")],
        [InlineKeyboardButton(text="📩 Попросить подключить аккаунт", callback_data=f"user:request_connect:{target_id}")],
        [InlineKeyboardButton(text="🔄 Обновить данные", callback_data=f"user:detail:{target_id}"), InlineKeyboardButton(text="⬅️ К пользователям", callback_data="users:0")],
    ])
    await callback.message.answer(
        "👤 <b>Управление пользователем</b>\n"
        f"Username: {html.escape(username)}\nID: <code>{target_id}</code>\n"
        f"Аккаунт: {'✅ Подключён' if user['session_string'] else '❌ Не подключён'}\n"
        f"PRO: {html.escape(pro_status)}\n"
        f"Групп найдено: {len(groups)} · выбрано пользователем: {len(selected)}\n"
        f"Авторассылка пользователя: {'🟢 включена' if own_running else '🔴 выключена'}\n"
        f"Рассылка администратора: {'🟢 работает' if admin_running else '🔴 остановлена'}\n"
        f"Подпись: {'включена' if user['signature_enabled'] else 'выключена'}\n\n"
        f"<b>Текст рассылки администратора:</b>\n<pre>{text_preview}</pre>\n"
        "Текст и выбранные группы рассылки администратора хранятся отдельно и не заменяют настройки самого пользователя.",
        reply_markup=kb, parse_mode="HTML")
    await callback.answer()


def admin_user_groups_keyboard(admin_id: int, target_id: int, groups, selected_ids: set[int], page: int = 0):
    page_size = 20
    pages = max(1, (len(groups) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    admin_group_pages[(admin_id, target_id)] = page
    rows = []
    for group in groups[page * page_size:(page + 1) * page_size]:
        chat_id = int(group["chat_id"])
        checked = chat_id in selected_ids
        label = str(group["title"] or chat_id)
        if not group["can_send"]:
            label += " (недоступна)"
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if checked else '▫️'} {label[:45]}",
            callback_data=f"usergroup:{target_id}:{chat_id}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"usergroupspage:{target_id}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{pages}", callback_data=f"usergroupspage:{target_id}:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"usergroupspage:{target_id}:{page+1}"))
    rows.append(nav)
    rows.extend([
        [InlineKeyboardButton(text="🔄 Обновить группы", callback_data=f"user:groups_refresh:{target_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к управлению", callback_data=f"user:detail:{target_id}")],
    ])
    return with_close(rows)


@dp.callback_query(F.data.startswith("usergroupspage:"))
async def admin_user_groups_page(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        _, raw_target, raw_page = callback.data.split(":", 2)
        target_id, page = int(raw_target), int(raw_page)
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    groups = await db.list_groups(target_id)
    raw = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
    selected_ids = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=admin_user_groups_keyboard(callback.from_user.id, target_id, groups, selected_ids, page)
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("user:groups:"))
async def admin_user_groups(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True); return
    target_id = int(callback.data.rsplit(":", 1)[1])
    user = await db.get_user(target_id)
    if not user or not user["session_string"]:
        await callback.answer("Аккаунт пользователя не подключён", show_alert=True); return
    groups = await db.list_groups(target_id)
    if not groups:
        await callback.answer("Список групп пуст. Обновите группы пользователя.", show_alert=True); return
    selected_raw = await get_setting(f"admin_campaign_groups:{target_id}", "")
    selected_ids = {int(x) for x in selected_raw.split(",") if x.strip().lstrip("-").isdigit()} if selected_raw else set()
    if selected_raw is None or selected_raw == "":
        selected_ids = {int(g["chat_id"]) for g in groups if g["is_selected"] and g["can_send"]}
        await set_setting(f"admin_campaign_groups:{target_id}", ",".join(map(str, sorted(selected_ids))) if selected_ids else "__none__")
    admin_group_pages[(callback.from_user.id, target_id)] = 0
    await callback.message.answer(
        f"👥 <b>Группы отдельной рассылки администратора</b>\nПользователь: <code>{target_id}</code>\n"
        "Выбор независим от групп, выбранных самим пользователем. Отправка разрешена только после согласия владельца аккаунта.",
        parse_mode="HTML",
        reply_markup=admin_user_groups_keyboard(callback.from_user.id, target_id, groups, selected_ids, 0))
    await callback.answer()


@dp.callback_query(F.data.startswith("usergroup:"))
async def admin_user_group_toggle(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True); return
    _, raw_target, raw_chat = callback.data.split(":", 2)
    target_id, chat_id = int(raw_target), int(raw_chat)
    raw = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
    ids = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    if chat_id in ids: ids.remove(chat_id)
    else: ids.add(chat_id)
    await set_setting(f"admin_campaign_groups:{target_id}", ",".join(map(str, sorted(ids))) if ids else "__none__")
    await callback.answer("Выбор сохранён")
    # Re-render the selector on the same page; the saved set is not capped.
    groups = await db.list_groups(target_id)
    page = admin_group_pages.get((callback.from_user.id, target_id), 0)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=admin_user_groups_keyboard(callback.from_user.id, target_id, groups, ids, page)
        )


@dp.callback_query(F.data.startswith("user:groups_refresh:"))
async def admin_user_groups_refresh(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True); return
    target_id = int(callback.data.rsplit(":", 1)[1])
    user = await db.get_user(target_id)
    if not user or not user["session_string"]:
        await callback.answer("Аккаунт пользователя не подключён", show_alert=True); return
    try:
        fresh = await sender.fetch_user_groups(user["session_string"])
        await db.upsert_groups(target_id, fresh)
        await callback.answer(f"Групп обновлено: {len(fresh)}")
    except Exception as exc:
        log.exception("Could not refresh target groups for %s", target_id)
        await callback.answer(f"Ошибка обновления: {type(exc).__name__}", show_alert=True)
        return
    # Re-render the separate group selector without a fixed 40-group cap.
    groups = await db.list_groups(target_id)
    raw = await get_setting(f"admin_campaign_groups:{target_id}", "") or ""
    ids = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    page = admin_group_pages.get((callback.from_user.id, target_id), 0)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=admin_user_groups_keyboard(callback.from_user.id, target_id, groups, ids, page)
        )


@dp.callback_query(F.data.startswith("user:send:"))
async def admin_user_send_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    user = await db.get_user(target_id)
    if not user or not user["session_string"]:
        await callback.answer("Аккаунт пользователя не подключён", show_alert=True)
        return
    await state.update_data(target_id=target_id)
    await state.set_state(AdminFlow.admin_user_broadcast_text)
    await callback.message.answer("Отправьте текст отдельной рассылки для этого пользователя. Сохранённый текст пользователя не будет заменён.")
    await callback.answer()

@dp.callback_query(F.data.startswith("user:request_connect:"))
async def admin_request_user_connect(callback: CallbackQuery):
    if not await admin_check(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    try:
        await bot.send_message(target_id, "🔐 Администратор просит переподключить аккаунт Telegram. Откройте меню бота и нажмите «🔗 Подключить аккаунт». Код входа вводите только в диалог с ботом; он не будет пересылаться администратору.")
        await callback.answer("Запрос отправлен пользователю")
        await callback.message.answer(f"📩 Пользователю {target_id} отправлена просьба подключить аккаунт самостоятельно. Коды входа администратору не передаются.")
    except Exception as exc:
        await callback.answer("Не удалось написать пользователю", show_alert=True)
        log.warning("Could not request account reconnection from %s: %s", target_id, exc)

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
                f"ID: {user['user_id']}\nИмя пользователя: @{user['username'] or '—'}\nИмя: {user['full_name']}\nЗвёзды: {user['stars']}\nПодключён: {'да' if user['session_string'] else 'нет'}\nИнтервал: {user['delay_seconds']} сек.\nПодпись: {'включена' if user['signature_enabled'] else 'выключена'}"
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
    return web.Response(text="VertonaBot is alive", status=200)

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

async def pro_expiry_notifier():
    """Notify users once when their PRO period expires."""
    while True:
        try:
            expired = await db.take_expired_pro_notifications()
            for item in expired:
                user_id = int(item["user_id"])
                with suppress(Exception):
                    await bot.send_message(
                        user_id,
                        "⌛ <b>Срок VertonaBot PRO завершился.</b>\n\n"
                        "Платные функции снова работают по обычным условиям. "
                        "Если нужно продлить PRO, свяжитесь с администратором.",
                        parse_mode="HTML",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("PRO expiry notifier failed")
        await asyncio.sleep(60)


async def main():
    # Verify the Bot API token before doing the rest of startup; fail with a clear log.
    try:
        bot_identity = await bot.get_me()
        log.info("Connected to Telegram Bot API as @%s (id=%s)", bot_identity.username, bot_identity.id)
    except Exception:
        log.exception("Could not connect to Telegram Bot API. Check BOT_TOKEN in Render environment.")
        raise
    await db.init_db(config.database_url)
    log.info("Connected to PostgreSQL and initialized schema")
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Открыть главное меню"),
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="users", description="Список пользователей (админ)"),
    ])
    # Super-admin IDs из ENV также добавляются в admins, но их права всегда проверяются по ENV.
    async with db.get_pool().acquire() as con:
        for admin_id in config.super_admins:
            await con.execute("INSERT INTO admins(user_id, added_by) VALUES($1,$1) ON CONFLICT(user_id) DO NOTHING", admin_id)
    runner = await start_health_server()
    pro_notifier_task = asyncio.create_task(pro_expiry_notifier(), name="pro-expiry-notifier")
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
    # Restore pending timer jobs after a restart. A timer already past its
    # scheduled time runs immediately; status changes prevent repeated restore.
    try:
        pending_timers = await db.list_tasks(limit=500)
        for row in pending_timers:
            if str(row.get("task_type")) != "timer" or str(row.get("status")) != "pending":
                continue
            owner_id = int(row["owner_id"])
            task_id = int(row["id"])
            run_at = row.get("run_at")
            if run_at is None:
                await db.update_task(task_id, "failed", {"error": "Missing run_at"})
                continue
            existing = timer_tasks.get(owner_id)
            if existing and not existing.done():
                continue
            timer_tasks[owner_id] = asyncio.create_task(
                run_timer(owner_id, task_id, str(row["message_text"]), run_at, owner_id)
            )
            log.info("Restored pending timer task %s for user %s", task_id, owner_id)
    except Exception:
        log.exception("Could not restore pending timer tasks")
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
    # Restore only separate admin campaigns previously approved by the account owner.
    try:
        async with db.get_pool().acquire() as con:
            admin_rows = await con.fetch("SELECT key FROM settings WHERE key LIKE 'admin_campaign_enabled:%' AND value='1'")
        for row in admin_rows:
            try:
                target_id = int(str(row["key"]).split(":", 1)[1])
                if (await get_setting(f"admin_campaign_approved:{target_id}", "0")) != "1":
                    continue
                target = await db.get_user(target_id)
                text_value = await get_setting(f"admin_campaign_text:{target_id}", "") or ""
                interval = int(await get_setting(f"admin_campaign_interval:{target_id}", "300") or "300")
                admin_id = int(await get_setting(f"admin_campaign_admin:{target_id}", "0") or "0")
                group_ids = [int(x) for x in (await get_setting(f"admin_campaign_groups:{target_id}", "") or "").split(",") if x.strip().lstrip("-").isdigit()]
                if target and target["session_string"] and text_value and group_ids and admin_id:
                    admin_campaign_tasks[target_id] = asyncio.create_task(run_admin_campaign(target_id, text_value, admin_id, interval))
                    log.info("Restored approved admin campaign for user %s", target_id)
                else:
                    await set_setting(f"admin_campaign_enabled:{target_id}", "0")
            except Exception:
                log.exception("Could not restore admin campaign setting %s", row["key"])
    except Exception:
        log.exception("Could not load approved admin campaigns")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        global shutting_down
        shutting_down = True
        pro_notifier_task.cancel()
        with suppress(asyncio.CancelledError):
            await pro_notifier_task
        for task in list(campaign_tasks.values()) + list(timer_tasks.values()) + list(auto_tasks.values()) + list(admin_campaign_tasks.values()):
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
