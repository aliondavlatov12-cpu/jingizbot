# language: Python, file: sender.py
# Рассылка аз номи корбар. Ботро намешиносад — танҳо db ва config.

import asyncio
import logging
import random
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
)
from telethon.tl.types import Channel, Chat

import db
from config import API_ID, API_HASH, SESSIONS_DIR

log = logging.getLogger("sender")

SIGNATURE = "\n\nотправлено через @jingizbot"


def session_path(uid: int) -> str:
    Path(SESSIONS_DIR).mkdir(exist_ok=True)
    return str(Path(SESSIONS_DIR) / f"user_{uid}")


async def make_client(uid: int) -> TelegramClient:
    return TelegramClient(session_path(uid), API_ID, API_HASH)


async def fetch_user_groups(uid: int) -> list[dict]:
    client = await make_client(uid)
    await client.start()

    groups = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        is_group = isinstance(ent, Chat) or (
            isinstance(ent, Channel) and getattr(ent, "megagroup", False)
        )
        if is_group:
            groups.append({
                "id": ent.id,
                "title": dialog.name,
                "username": getattr(ent, "username", None),
            })

    db.clear_user_groups(uid)
    for g in groups:
        db.add_group(uid, g["id"], g["title"], g["username"])

    await client.disconnect()
    return groups


def _signature_for(user: dict) -> str:
    return "" if user.get("unsigned") else SIGNATURE


async def send_campaign(uid: int, text: str, progress_cb=None) -> dict:
    user = db.get_user(uid)
    if not user or not user.get("connected"):
        return {"ok": False, "reason": f"корбар {uid} пайваст нест"}

    groups = db.selected_groups(uid)
    if not groups:
        return {"ok": False, "reason": "гурухҳо интихоб нашудаанд"}

    delay = user.get("delay") or 300
    full_text = text + _signature_for(user)

    client = await make_client(uid)
    await client.start()

    sent, failed = 0, 0
    for i, g in enumerate(groups, 1):
        if progress_cb:
            try:
                await progress_cb(i, len(groups), g["title"])
            except Exception:
                pass

        try:
            entity = await client.get_entity(g["group_id"])
            await client.send_message(entity, full_text)
            sent += 1
            log.info(f"✓ [{uid}] {g['title']}")

        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s [{uid}] — интизорӣ")
            await asyncio.sleep(e.seconds + 5)
            try:
                entity = await client.get_entity(g["group_id"])
                await client.send_message(entity, full_text)
                sent += 1
            except Exception as e2:
                failed += 1
                log.error(f"✗ [{uid}] {g['title']} — {e2}")

        except (ChatWriteForbiddenError, UserBannedInChannelError):
            failed += 1
            log.warning(f"⊘ [{uid}] {g['title']} — наметавон навишт")

        except Exception as e:
            failed += 1
            log.error(f"✗ [{uid}] {g['title']} — {type(e).__name__}: {e}")

        if i < len(groups):
            jitter = random.randint(-15, 15)
            await asyncio.sleep(max(60, delay + jitter))

    await client.disconnect()
    return {"ok": True, "sent": sent, "failed": failed, "total": len(groups)}


async def send_raw_from_user(uid: int, target: str, text: str) -> bool:
    client = await make_client(uid)
    await client.start()
    try:
        entity = await client.get_entity(target)
        await client.send_message(entity, text)
        return True
    except Exception as e:
        log.error(f"send_raw_from_user [{uid}]: {e}")
        return False
    finally:
        await client.disconnect()


async def refresh_user_info(uid: int) -> dict:
    client = await make_client(uid)
    await client.start()
    me = await client.get_me()
    db.upsert_user(uid, me.username, me.first_name)
    await client.disconnect()
    return {"id": me.id, "username": me.username, "first_name": me.first_name}


async def broadcast_all(text: str, only_connected=True, progress_cb=None) -> dict:
    users = db.list_users(limit=10000, only_connected=only_connected)
    total = len(users)
    sent_total, failed_total = 0, 0

    for idx, u in enumerate(users, 1):
        if progress_cb:
            try:
                await progress_cb(idx, total, u.get("first_name") or str(u["user_id"]))
            except Exception:
                pass

        try:
            res = await send_campaign(u["user_id"], text)
            if res.get("ok"):
                sent_total += res["sent"]
                failed_total += res["failed"]
            else:
                failed_total += 1
        except Exception as e:
            log.error(f"broadcast_all [{u['user_id']}]: {e}")
            failed_total += 1

    return {"ok": True, "users": total, "sent": sent_total, "failed": failed_total}
