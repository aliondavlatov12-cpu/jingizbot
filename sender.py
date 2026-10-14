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
    try:
        await client.connect()
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
            raw_id = int(entity.id)
            if isinstance(entity, Chat):
                chat_id = -abs(raw_id)
            else:
                # Канонический marked ID нельзя повторно оборачивать в -100.
                raw_id_text = str(raw_id)
                chat_id = (
                    raw_id
                    if raw_id_text.startswith("-100")
                    else int(f"-100{abs(raw_id)}")
                )
            result.append({
                "chat_id": chat_id,
                "title": dialog.name or str(entity.id),
                "username": getattr(entity, "username", None),
                "can_send": can_send,
                "is_basic_group": isinstance(entity, Chat),
            })
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("Ignoring error while disconnecting group-list client", exc_info=True)
    log.info("Loaded %s group chats from connected Telegram account", len(result))
    return result

async def _send_to_selected(
    session_string: str, targets, message: str, delay_seconds: int, *,
    live_user_id: int | None = None, base_message: str | None = None,
    allowed_chat_ids: set[int] | None = None, refresh_text: bool = True,
):
    """Send to each selected group independently; one bad group must not stop the rest.

    Telegram FloodWait and group Slow Mode are respected. A blocked/inaccessible
    group is recorded as a failure and the sender proceeds to the next group.
    """
    client = client_from_session(session_string)
    sent, failed = 0, 0
    errors: list[str] = []
    wait_seconds_total = 0
    log.info("Starting send cycle: targets=%s delay_between_groups=%ss", len(targets), delay_seconds)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия больше не авторизована.")

        # Resolve the dialog entities on this client. StringSession may not retain
        # entity access hashes across client instances. Store canonical marked IDs
        # and raw basic-group IDs to handle legacy database rows as well.
        entities_by_id = {}
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            try:
                entities_by_id[int(utils.get_peer_id(entity))] = entity
                if isinstance(entity, Chat):
                    entities_by_id[int(entity.id)] = entity  # legacy positive Chat IDs
                    entities_by_id[-int(entity.id)] = entity
                elif isinstance(entity, Channel):
                    entities_by_id[int(f"-100{entity.id}")] = entity
            except (TypeError, ValueError):
                continue

        # For an active auto-campaign, refresh its live configuration between
        # destinations. This lets edits to text, signature, selected groups,
        # account session and per-group delay take effect without restarting it.
        pending = list(targets)
        processed_ids: set[int] = set()
        while pending:
            if live_user_id is not None:
                live_user = await db.get_user(live_user_id)
                if not live_user or not live_user.get("session_string"):
                    raise RuntimeError("Аккаунт отключён во время рассылки.")
                if live_user["session_string"] != session_string:
                    raise RuntimeError("Аккаунт изменён во время рассылки; остановлено для безопасности.")
                async with db.get_pool().acquire() as con:
                    latest_text = await con.fetchval(
                        "SELECT value FROM settings WHERE key=$1", f"broadcast_text:{live_user_id}"
                    )
                if refresh_text and latest_text is not None and str(latest_text).strip():
                    message = str(latest_text).strip()
                else:
                    message = (base_message if base_message is not None else message).strip()
                if live_user.get("signature_enabled"):
                    message += config.signature
                # The user's selected auto-campaign interval is the pause BETWEEN
                # complete cycles, not a delay after every destination. Keep a short
                # per-group pause here so four groups do not accidentally add 4 x 5 min.
                delay_seconds = 1
                refreshed = await db.selected_groups(live_user_id)
                pending = [
                    g for g in refreshed
                    if int(g["chat_id"]) not in processed_ids
                    and (allowed_chat_ids is None or int(g["chat_id"]) in allowed_chat_ids)
                ]
                if not pending:
                    break
            group = pending[0]
            title = str(group.get("title") or group.get("chat_id") or "группа")
            chat_id = int(group["chat_id"])
            processed_ids.add(chat_id)
            if live_user_id is None:
                pending.pop(0)
            entity = entities_by_id.get(chat_id)
            if entity is None:
                failed += 1
                errors.append(f"{title}: группа не найдена в текущих диалогах; обновите группы/проверьте аккаунт")
                log.warning("Skipping unresolved group %s (%s)", title, chat_id)
            else:
                delivered = False
                # Retry once after Telegram's requested wait. Do not let one
                # restricted group abort the remaining campaign.
                for attempt in range(2):
                    try:
                        await client.send_message(entity, message)
                        sent += 1
                        delivered = True
                        log.info("Message sent to group %s (%s)", title, chat_id)
                        break
                    except (FloodWaitError, SlowModeWaitError) as exc:
                        wait_seconds = max(1, int(getattr(exc, "seconds", 1) or 1))
                        wait_seconds_total = max(wait_seconds_total, wait_seconds)
                        kind = "FloodWait" if isinstance(exc, FloodWaitError) else "медленный режим"
                        log.warning("Telegram %s for %s: wait %s sec (attempt %s)", kind, title, wait_seconds, attempt + 1)
                        errors.append(f"{title}: {kind}, ожидание {wait_seconds} сек.")
                        await asyncio.sleep(wait_seconds + 1)
                        if attempt == 1:
                            failed += 1
                    except (ChatWriteForbiddenError, ChannelPrivateError, UserBannedInChannelError) as exc:
                        failed += 1
                        errors.append(f"{title}: нет доступа для отправки ({type(exc).__name__})")
                        log.info("Cannot send to group %s (%s): %s", title, chat_id, exc)
                        break
                    except RPCError as exc:
                        failed += 1
                        errors.append(f"{title}: ошибка Telegram ({type(exc).__name__})")
                        log.warning("Telegram RPC error for group %s (%s): %s", title, chat_id, exc)
                        break
                    except Exception as exc:
                        failed += 1
                        errors.append(f"{title}: ошибка ({type(exc).__name__})")
                        log.exception("Unexpected send error for group %s (%s)", title, chat_id)
                        break
                if not delivered:
                    log.info("Message was not delivered to %s; continuing to next target", title)

            if live_user_id is None:
                # Static campaigns keep their original target snapshot.
                if len(processed_ids) < len(targets):
                    await asyncio.sleep(max(0, int(delay_seconds)))
            else:
                # Refresh the current delay before waiting, so edits apply now.
                live_user = await db.get_user(live_user_id)
                if not live_user or not live_user.get("session_string"):
                    raise RuntimeError("Аккаунт отключён во время рассылки.")
                # Auto interval is handled by run_auto_campaign after the whole cycle.
                # Only a small safety pause separates group sends within this cycle.
                wait_for = 1
                refreshed_after_send = await db.selected_groups(live_user_id)
                has_remaining = any(int(g["chat_id"]) not in processed_ids for g in refreshed_after_send)
                if has_remaining and wait_for:
                    await asyncio.sleep(wait_for)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("Ignoring error while disconnecting sender client", exc_info=True)
    log.info("Send cycle finished: sent=%s failed=%s targets=%s", sent, failed, len(targets))
    return {
        "sent": sent,
        "failed": failed,
        # Per-group failures and rate limits no longer terminate auto-broadcast.
        "stopped": False,
        "errors": errors[:20],
        "wait_seconds": wait_seconds_total or None,
    }

_account_send_locks: dict[int, asyncio.Lock] = {}

async def send_campaign(
    user_id: int,
    message_text: str,
    *,
    delay_seconds: int | None = None,
    group_chat_ids: list[int] | None = None,
    live_refresh: bool = False,
):
    """Send to the user's selected groups, or to an explicit isolated group list for an approved admin campaign.

    Per-account locking prevents the user's own auto-campaign and an approved admin campaign
    from opening simultaneous Telethon clients for the same Telegram session.
    """
    lock = _account_send_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        user = await db.get_user(user_id)
        if not user or not user["session_string"]:
            raise RuntimeError("Сначала подключите аккаунт Telegram.")
        if group_chat_ids is None:
            groups = await db.selected_groups(user_id)
        else:
            # The administrator's approved campaign has its own group selection and
            # must not overwrite or inherit changes to the user's personal selection.
            allowed = {int(chat_id) for chat_id in group_chat_ids}
            all_groups = await db.list_groups(user_id)
            groups = [group for group in all_groups if int(group["chat_id"]) in allowed and bool(group["can_send"])]
        if not groups:
            raise RuntimeError("Не выбраны доступные группы для отправки.")
        message = message_text
        if user["signature_enabled"] and not live_refresh:
            message += config.signature
        group_delay = int(user["delay_seconds"]) if delay_seconds is None else max(0, int(delay_seconds))
        return await _send_to_selected(
            user["session_string"], groups, message, group_delay,
            live_user_id=user_id if live_refresh else None,
            base_message=message_text if live_refresh else None,
            allowed_chat_ids=({int(chat_id) for chat_id in group_chat_ids} if group_chat_ids is not None and live_refresh else None),
            # A per-group message must not be replaced by the shared message in settings.
            refresh_text=(group_chat_ids is None),
        )

async def send_to_account(user_id: int, message_text: str, *, group_chat_ids: list[int] | None = None):
    return await send_campaign(user_id, message_text, group_chat_ids=group_chat_ids)

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
