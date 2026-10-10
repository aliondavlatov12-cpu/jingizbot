import asyncio
import logging
from telethon import TelegramClient
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
            raise RuntimeError("Сессия истекла или не авторизована. Аккаунтро аз нав пайваст кунед.")
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            # Каналҳои пахшӣ ва гурӯҳҳои оддӣ нишон дода мешаванд; навъи чатро Telegram муайян мекунад.
            if isinstance(entity, Channel) and getattr(entity, "broadcast", False):
                continue
            can_send = True
            try:
                perms = await client.get_permissions(entity, "me")
                if getattr(entity, "megagroup", False) or isinstance(entity, Chat):
                    can_send = not bool(getattr(perms, "is_banned", False))
            except Exception:
                # Ҳуқуқи ниҳоӣ ҳангоми фиристодан аз ҷониби Telegram санҷида мешавад.
                can_send = True
            result.append({
                "chat_id": int(entity.id) if isinstance(entity, Chat) else int(f"-100{entity.id}"),
                "title": dialog.name or str(entity.id),
                "username": getattr(entity, "username", None),
                "can_send": can_send,
            })
    finally:
        await client.disconnect()
    return result

async def _send_to_selected(session_string: str, targets, message: str, delay_seconds: int):
    client = client_from_session(session_string)
    sent, failed, stopped = 0, 0, False
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия дигар авторизатсия нашудааст.")
        for index, group in enumerate(targets):
            if stopped:
                break
            try:
                entity = await client.get_entity(group["chat_id"])
                await client.send_message(entity, message)
                sent += 1
            except FloodWaitError as exc:
                log.warning("Telegram FloodWait: %s seconds; stopping campaign", exc.seconds)
                failed += 1
                stopped = True
            except SlowModeWaitError as exc:
                log.warning("Slow mode: %s seconds; stopping campaign", exc.seconds)
                failed += 1
                stopped = True
            except (ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError) as exc:
                log.info("Cannot send to chat %s: %s", group["chat_id"], exc)
                failed += 1
            except RPCError as exc:
                log.warning("Telegram RPC error for chat %s: %s", group["chat_id"], exc)
                failed += 1
            except Exception:
                log.exception("Unexpected send error for chat %s", group["chat_id"])
                failed += 1
            if index < len(targets) - 1 and not stopped:
                await asyncio.sleep(delay_seconds)
    finally:
        await client.disconnect()
    return {"sent": sent, "failed": failed, "stopped": stopped}

async def send_campaign(user_id: int, message_text: str):
    user = await db.get_user(user_id)
    if not user or not user["session_string"]:
        raise RuntimeError("Аввал аккаунти Telegram-ро пайваст кунед.")
    groups = await db.selected_groups(user_id)
    if not groups:
        raise RuntimeError("Ягон гурӯҳи интихобшудаи қобили фиристодан нест.")
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
