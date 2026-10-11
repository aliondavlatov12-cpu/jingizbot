import asyncio
import logging
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError, ChannelPrivateError,
    UserBannedInChannelError, SlowModeWaitError, RPCError
)
from telethon.tl.types import Channel, Chat
import db
from config import config

log = logging.getLogger(__name__)

def client_from_session(session_string: str) -> TelegramClient:
    return TelegramClient(StringSession(session_string), config.api_id, config.api_hash)

async def fetch_user_groups(session_string: str) -> list[dict]:
    client = client_from_session(session_string)
    result = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия истекла или не авторизована. Подключите аккаунт заново.")
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            # Каналы вещания исключаются; тип чата определяет Telegram.
            if isinstance(entity, Channel) and getattr(entity, "broadcast", False):
                continue
            can_send = True
            try:
                perms = await client.get_permissions(entity, "me")
                if getattr(entity, "megagroup", False) or isinstance(entity, Chat):
                    can_send = not bool(getattr(perms, "is_banned", False))
            except Exception:
                # Окончательные права Telegram проверяет при отправке.
                can_send = True
            # Telethon requires basic groups to use a negative marked ID.
            # Positive IDs can be resolved as users, causing group sends to fail.
            chat_id = -int(entity.id) if isinstance(entity, Chat) else int(f"-100{entity.id}")
            result.append({
                "chat_id": chat_id,
                "title": dialog.name or str(entity.id),
                "username": getattr(entity, "username", None),
                "can_send": can_send,
                "is_basic_group": isinstance(entity, Chat),
            })
    finally:
        await client.disconnect()
    return result

async def _send_to_selected(session_string: str, targets, message: str, delay_seconds: int):
    client = client_from_session(session_string)
    sent, failed, stopped = 0, 0, False
    errors: list[str] = []
    wait_seconds = None
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия больше не авторизована.")

        # StringSession stores authorization credentials but does not reliably retain
        # the full entity/access-hash cache between separate client instances. Resolve
        # dialogs on this connected client and match Telegram's marked peer IDs.
        # Calling get_entity(-100...) directly can fail with "Could not find input entity".
        entities_by_id = {}
        async for dialog in client.iter_dialogs():
            try:
                entities_by_id[int(utils.get_peer_id(dialog.entity))] = dialog.entity
            except (TypeError, ValueError):
                continue

        for index, group in enumerate(targets):
            if stopped:
                break
            try:
                chat_id = int(group["chat_id"])
                entity = entities_by_id.get(chat_id)
                if entity is None:
                    failed += 1
                    errors.append(f"{group.get('title', chat_id)}: группа не найдена в аккаунте; обновите список групп")
                    continue
                await client.send_message(entity, message)
                sent += 1
            except FloodWaitError as exc:
                wait_seconds = int(getattr(exc, "seconds", 0) or 0)
                log.warning("Telegram FloodWait: %s seconds; stopping campaign", wait_seconds)
                failed += 1
                stopped = True
                errors.append(f"{group.get('title', group['chat_id'])}: ограничение Telegram, пауза {wait_seconds} сек.")
            except SlowModeWaitError as exc:
                wait_seconds = int(getattr(exc, "seconds", 0) or 0)
                log.warning("Slow mode: %s seconds; stopping campaign", wait_seconds)
                failed += 1
                stopped = True
                errors.append(f"{group.get('title', group['chat_id'])}: включён медленный режим ({wait_seconds} сек.)")
            except (ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError) as exc:
                log.info("Cannot send to chat %s: %s", group["chat_id"], exc)
                failed += 1
                errors.append(f"{group.get('title', group['chat_id'])}: {type(exc).__name__}")
            except RPCError as exc:
                log.warning("Telegram RPC error for chat %s: %s", group["chat_id"], exc)
                failed += 1
                errors.append(f"{group.get('title', group['chat_id'])}: {type(exc).__name__}")
            except Exception as exc:
                log.exception("Unexpected send error for chat %s", group["chat_id"])
                failed += 1
                errors.append(f"{group.get('title', group['chat_id'])}: {type(exc).__name__}")
            if index < len(targets) - 1 and not stopped:
                await asyncio.sleep(delay_seconds)
    finally:
        await client.disconnect()
    return {
        "sent": sent,
        "failed": failed,
        "stopped": stopped,
        "errors": errors[:10],
        "wait_seconds": wait_seconds,
    }

async def send_campaign(user_id: int, message_text: str):
    user = await db.get_user(user_id)
    if not user or not user["session_string"]:
        raise RuntimeError("Аввал аккаунти Telegram-ро пайваст кунед.")
    groups = await db.selected_groups(user_id)
    if not groups:
        raise RuntimeError("Нет выбранных групп, в которые разрешена отправка.")
    message = message_text
    if user["signature_enabled"]:
        message += config.signature
    return await _send_to_selected(user["session_string"], groups, message, int(user["delay_seconds"]))

async def send_to_account(user_id: int, message_text: str):
    return await send_campaign(user_id, message_text)

async def broadcast_all(message_text: str):
    users = await db.list_users()
    results = {}
    for user in users:
        if not user["connected"]:
            continue
        try:
            result = await send_campaign(user["user_id"], message_text)
            results[user["user_id"]] = result
        except Exception as exc:
            log.warning("Global broadcast skipped for user %s: %s", user["user_id"], exc)
            results[user["user_id"]] = {"error": str(exc)}
    return results
