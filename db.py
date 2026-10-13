import asyncpg
import json
from typing import Any

pool: asyncpg.Pool | None = None

DEFAULT_PRICES = {
    "connect_price": 0,
    "broadcast_price": 0,
    "unsign_price": 0,
    "timer_price": 0,
    "groups_refresh_price": 0,
    "fetch_groups_price": 0,
}

async def init_db(database_url: str):
    global pool
    pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=5,
        command_timeout=30,
        server_settings={"application_name": "VortenaBot"},
    )
    async with pool.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT NOT NULL DEFAULT '',
            stars BIGINT NOT NULL DEFAULT 0 CHECK (stars >= 0),
            session_string TEXT,
            phone TEXT,
            delay_seconds INTEGER NOT NULL DEFAULT 300,
            signature_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            connected_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS groups (
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            username TEXT,
            is_selected BOOLEAN NOT NULL DEFAULT FALSE,
            can_send BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS prices (
            key TEXT PRIMARY KEY,
            amount BIGINT NOT NULL DEFAULT 0 CHECK (amount >= 0)
        );
        CREATE TABLE IF NOT EXISTS force_subs (
            id BIGSERIAL PRIMARY KEY,
            chat_ref TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            invite_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE force_subs ADD COLUMN IF NOT EXISTS invite_url TEXT;
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            message_text TEXT NOT NULL,
            run_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'pending',
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_campaigns (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            message_text TEXT NOT NULL,
            notify_chat_id BIGINT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS admin_actions (
            id BIGSERIAL PRIMARY KEY,
            admin_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_id BIGINT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS pro_subscriptions (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            granted_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expiry_notified BOOLEAN NOT NULL DEFAULT FALSE
        );
        """)

        # Safe, additive migrations for databases created by earlier VortenaBot versions.
        # These statements preserve existing user sessions and saved campaigns.
        await con.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT NOT NULL DEFAULT '';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS stars BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS session_string TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS delay_seconds INTEGER NOT NULL DEFAULT 300;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS signature_enabled BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS connected_at TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE admins ADD COLUMN IF NOT EXISTS added_by BIGINT;
        ALTER TABLE admins ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE groups ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS username TEXT;
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_selected BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS can_send BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE prices ADD COLUMN IF NOT EXISTS amount BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE force_subs ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT '';
        ALTER TABLE force_subs ADD COLUMN IF NOT EXISTS invite_url TEXT;
        ALTER TABLE force_subs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'legacy';
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS message_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_at TIMESTAMPTZ;
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE settings ADD COLUMN IF NOT EXISTS value TEXT NOT NULL DEFAULT '';

        ALTER TABLE auto_campaigns ADD COLUMN IF NOT EXISTS message_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE auto_campaigns ADD COLUMN IF NOT EXISTS notify_chat_id BIGINT;
        UPDATE auto_campaigns SET notify_chat_id = user_id WHERE notify_chat_id IS NULL;
        ALTER TABLE auto_campaigns ALTER COLUMN notify_chat_id SET NOT NULL;
        ALTER TABLE auto_campaigns ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE auto_campaigns ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE auto_campaigns ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE admin_actions ADD COLUMN IF NOT EXISTS admin_id BIGINT;
        ALTER TABLE admin_actions ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'legacy';
        ALTER TABLE admin_actions ADD COLUMN IF NOT EXISTS target_id BIGINT;
        ALTER TABLE admin_actions ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE admin_actions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE pro_subscriptions ADD COLUMN IF NOT EXISTS expiry_notified BOOLEAN NOT NULL DEFAULT FALSE;
        """)
        for key, amount in DEFAULT_PRICES.items():
            await con.execute(
                "INSERT INTO prices(key, amount) VALUES($1, $2) ON CONFLICT(key) DO NOTHING",
                key, amount
            )
    return pool

def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool has not been initialized")
    return pool

async def upsert_user(user_id: int, username: str | None, full_name: str):
    async with get_pool().acquire() as con:
        await con.execute("""
        INSERT INTO users(user_id, username, full_name)
        VALUES($1, $2, $3)
        ON CONFLICT(user_id) DO UPDATE SET
          username=EXCLUDED.username, full_name=EXCLUDED.full_name, updated_at=NOW()
        """, user_id, username, full_name)

async def get_user(user_id: int):
    async with get_pool().acquire() as con:
        return await con.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

async def list_users():
    async with get_pool().acquire() as con:
        return await con.fetch("SELECT user_id, username, full_name, stars, (session_string IS NOT NULL) AS connected, created_at FROM users ORDER BY created_at DESC")

async def save_session(user_id: int, session_string: str, phone: str):
    async with get_pool().acquire() as con:
        await con.execute("""
        UPDATE users SET session_string=$2, phone=$3, connected_at=NOW(), updated_at=NOW()
        WHERE user_id=$1
        """, user_id, session_string, phone)

async def clear_session(user_id: int):
    async with get_pool().acquire() as con:
        await con.execute("UPDATE users SET session_string=NULL, phone=NULL, connected_at=NULL WHERE user_id=$1", user_id)

async def set_delay(user_id: int, delay: int):
    async with get_pool().acquire() as con:
        await con.execute("UPDATE users SET delay_seconds=$2, updated_at=NOW() WHERE user_id=$1", user_id, delay)

async def set_signature(user_id: int, enabled: bool):
    async with get_pool().acquire() as con:
        await con.execute("UPDATE users SET signature_enabled=$2, updated_at=NOW() WHERE user_id=$1", user_id, enabled)

async def upsert_groups(user_id: int, groups: list[dict[str, Any]]):
    async with get_pool().acquire() as con:
        async with con.transaction():
            # Сохраняем выбор до удаления старого списка групп.
            old_rows = await con.fetch(
                "SELECT chat_id, is_selected FROM groups WHERE user_id=$1",
                user_id,
            )
            selected_chat_ids = {
                int(row["chat_id"])
                for row in old_rows
                if row["is_selected"]
            }

            # Заменяем список групп пользователя атомарно.
            await con.execute(
                "DELETE FROM groups WHERE user_id=$1",
                user_id,
            )

            for item in groups:
                chat_id = int(item["chat_id"])
                # Сохраняем выбор при переходе старых положительных ID
                # базовых групп на канонический отрицательный ID.
                legacy_selected = (
                    bool(item.get("is_basic_group", False))
                    and -chat_id in selected_chat_ids
                )
                is_selected = (
                    chat_id in selected_chat_ids or legacy_selected
                )

                await con.execute(
                    """
                    INSERT INTO groups(
                        user_id, chat_id, title, username,
                        can_send, is_selected, updated_at
                    )
                    VALUES($1, $2, $3, $4, $5, $6, NOW())
                    """,
                    user_id,
                    chat_id,
                    item["title"],
                    item.get("username"),
                    item.get("can_send", True),
                    is_selected,
                )

async def list_groups(user_id: int):
    async with get_pool().acquire() as con:
        return await con.fetch("SELECT * FROM groups WHERE user_id=$1 ORDER BY title", user_id)

async def toggle_group(user_id: int, chat_id: int):
    async with get_pool().acquire() as con:
        return await con.fetchrow("""
        UPDATE groups SET is_selected=NOT is_selected, updated_at=NOW()
        WHERE user_id=$1 AND chat_id=$2 RETURNING *
        """, user_id, chat_id)

async def selected_groups(user_id: int):
    async with get_pool().acquire() as con:
        return await con.fetch("SELECT * FROM groups WHERE user_id=$1 AND is_selected=TRUE AND can_send=TRUE ORDER BY title", user_id)

async def save_selected_groups(user_id: int, chat_ids: set[int] | list[int]):
    """Atomically replace the user's selected group set with the explicit saved choice."""
    normalized = {int(chat_id) for chat_id in chat_ids}
    async with get_pool().acquire() as con:
        async with con.transaction():
            await con.execute("UPDATE groups SET is_selected=FALSE, updated_at=NOW() WHERE user_id=$1", user_id)
            if normalized:
                await con.execute(
                    "UPDATE groups SET is_selected=TRUE, updated_at=NOW() "
                    "WHERE user_id=$1 AND chat_id = ANY($2::bigint[])",
                    user_id, list(normalized),
                )
            return await con.fetch("SELECT * FROM groups WHERE user_id=$1 ORDER BY title", user_id)

async def is_admin(user_id: int, super_admins: set[int]):
    if user_id in super_admins:
        return True
    async with get_pool().acquire() as con:
        return await con.fetchval("SELECT EXISTS(SELECT 1 FROM admins WHERE user_id=$1)", user_id)

async def add_admin(user_id: int, added_by: int):
    async with get_pool().acquire() as con:
        await con.execute("INSERT INTO admins(user_id, added_by) VALUES($1,$2) ON CONFLICT(user_id) DO NOTHING", user_id, added_by)

async def remove_admin(user_id: int):
    async with get_pool().acquire() as con:
        await con.execute("DELETE FROM admins WHERE user_id=$1", user_id)

async def list_admins(super_admins: set[int]):
    async with get_pool().acquire() as con:
        rows = await con.fetch("SELECT user_id, added_by, created_at FROM admins ORDER BY created_at")
    return rows, sorted(super_admins)

async def get_pro_info(user_id: int):
    """Return an active PRO subscription, or None if missing/expired."""
    async with get_pool().acquire() as con:
        return await con.fetchrow(
            "SELECT user_id, expires_at, granted_by, created_at FROM pro_subscriptions "
            "WHERE user_id=$1 AND expires_at > NOW()",
            user_id,
        )

async def is_pro(user_id: int) -> bool:
    return await get_pro_info(user_id) is not None

async def grant_pro(user_id: int, days: int, granted_by: int):
    """Grant/extend PRO from the later of now or the existing active expiry."""
    if days < 1 or days > 36500:
        raise ValueError("Срок PRO должен быть от 1 до 36500 дней")
    async with get_pool().acquire() as con:
        async with con.transaction():
            await con.execute(
                "INSERT INTO users(user_id, full_name) VALUES($1, '') ON CONFLICT(user_id) DO NOTHING",
                user_id,
            )
            row = await con.fetchrow(
                """
                INSERT INTO pro_subscriptions(user_id, expires_at, granted_by, created_at, updated_at)
                VALUES($1, NOW() + ($2 * INTERVAL '1 day'), $3, NOW(), NOW())
                ON CONFLICT(user_id) DO UPDATE SET
                    expires_at = GREATEST(pro_subscriptions.expires_at, NOW()) + ($2 * INTERVAL '1 day'),
                    granted_by = EXCLUDED.granted_by,
                    expiry_notified = FALSE,
                    updated_at = NOW()
                RETURNING user_id, expires_at, granted_by, created_at
                """,
                user_id, days, granted_by,
            )
            return row

async def remove_pro(user_id: int) -> bool:
    async with get_pool().acquire() as con:
        result = await con.execute("DELETE FROM pro_subscriptions WHERE user_id=$1", user_id)
    return result.endswith("1")

async def list_pro_users(limit: int = 500, offset: int = 0):
    async with get_pool().acquire() as con:
        return await con.fetch(
            """
            SELECT p.user_id, p.expires_at, p.granted_by, u.username, u.full_name
            FROM pro_subscriptions p
            JOIN users u ON u.user_id=p.user_id
            WHERE p.expires_at > NOW()
            ORDER BY p.expires_at ASC
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )

async def take_expired_pro_notifications(limit: int = 100):
    """Atomically mark expired PRO rows as notified and return user IDs once."""
    async with get_pool().acquire() as con:
        return await con.fetch(
            """
            WITH due AS (
                SELECT user_id FROM pro_subscriptions
                WHERE expires_at <= NOW() AND expiry_notified=FALSE
                ORDER BY expires_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE pro_subscriptions p
            SET expiry_notified=TRUE, updated_at=NOW()
            FROM due
            WHERE p.user_id=due.user_id
            RETURNING p.user_id, p.expires_at
            """,
            limit,
        )

async def count_pro_users() -> int:
    async with get_pool().acquire() as con:
        return int(await con.fetchval(
            "SELECT COUNT(*) FROM pro_subscriptions WHERE expires_at > NOW()"
        ) or 0)

async def add_stars(user_id: int, amount: int):
    async with get_pool().acquire() as con:
        async with con.transaction():
            await con.execute("""
            INSERT INTO users(user_id, full_name) VALUES($1,'')
            ON CONFLICT(user_id) DO NOTHING
            """, user_id)
            row = await con.fetchrow("UPDATE users SET stars=stars+$2, updated_at=NOW() WHERE user_id=$1 RETURNING stars", user_id, amount)
            return row["stars"]

async def spend_stars(user_id: int, amount: int) -> bool:
    async with get_pool().acquire() as con:
        row = await con.fetchrow("""
        UPDATE users SET stars=stars-$2, updated_at=NOW()
        WHERE user_id=$1 AND stars >= $2 RETURNING stars
        """, user_id, amount)
        return row is not None

async def get_price(key: str) -> int:
    async with get_pool().acquire() as con:
        result = await con.fetchval("SELECT amount FROM prices WHERE key=$1", key)
        return int(result or 0)

async def set_price(key: str, amount: int):
    async with get_pool().acquire() as con:
        await con.execute("""
        INSERT INTO prices(key,amount) VALUES($1,$2)
        ON CONFLICT(key) DO UPDATE SET amount=EXCLUDED.amount
        """, key, amount)

async def add_force_sub(chat_ref: str, label: str, invite_url: str | None = None):
    async with get_pool().acquire() as con:
        await con.execute(
            "INSERT INTO force_subs(chat_ref,label,invite_url) VALUES($1,$2,$3) "
            "ON CONFLICT(chat_ref) DO UPDATE SET label=EXCLUDED.label, invite_url=EXCLUDED.invite_url",
            chat_ref, label, invite_url,
        )

async def list_force_subs():
    async with get_pool().acquire() as con:
        return await con.fetch("SELECT * FROM force_subs ORDER BY id")

async def delete_force_sub(sub_id: int):
    async with get_pool().acquire() as con:
        await con.execute("DELETE FROM force_subs WHERE id=$1", sub_id)

async def create_task(owner_id: int, task_type: str, message_text: str, run_at=None, details=None):
    async with get_pool().acquire() as con:
        return await con.fetchval("""
        INSERT INTO tasks(owner_id,task_type,message_text,run_at,details)
        VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id
        """, owner_id, task_type, message_text, run_at, json.dumps(details or {}))

async def list_tasks(owner_id: int | None = None, limit: int = 100):
    async with get_pool().acquire() as con:
        if owner_id is None:
            return await con.fetch("SELECT * FROM tasks ORDER BY created_at DESC LIMIT $1", limit)
        return await con.fetch("SELECT * FROM tasks WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2", owner_id, limit)

async def update_task(task_id: int, status: str, details=None):
    async with get_pool().acquire() as con:
        if details is None:
            await con.execute("UPDATE tasks SET status=$2, updated_at=NOW() WHERE id=$1", task_id, status)
        else:
            await con.execute("UPDATE tasks SET status=$2, details=$3::jsonb, updated_at=NOW() WHERE id=$1", task_id, status, json.dumps(details))

async def log_admin_action(admin_id: int, action: str, target_id: int | None = None, details=None):
    async with get_pool().acquire() as con:
        await con.execute("INSERT INTO admin_actions(admin_id,action,target_id,details) VALUES($1,$2,$3,$4::jsonb)", admin_id, action, target_id, json.dumps(details or {}))

async def stats():
    async with get_pool().acquire() as con:
        return {
            "users": await con.fetchval("SELECT COUNT(*) FROM users"),
            "connected": await con.fetchval("SELECT COUNT(*) FROM users WHERE session_string IS NOT NULL"),
            "groups": await con.fetchval("SELECT COUNT(*) FROM groups"),
            "tasks_pending": await con.fetchval("SELECT COUNT(*) FROM tasks WHERE status='pending'"),
            "admins": await con.fetchval("SELECT COUNT(*) FROM admins"),
            "pro_users": await con.fetchval("SELECT COUNT(*) FROM pro_subscriptions WHERE expires_at > NOW()"),
        }


async def save_auto_campaign(user_id: int, message_text: str, notify_chat_id: int):
    """Persist an enabled recurring campaign so it can resume after process restarts."""
    async with get_pool().acquire() as con:
        await con.execute("""
        INSERT INTO auto_campaigns(user_id, message_text, notify_chat_id, enabled, created_at, updated_at)
        VALUES($1, $2, $3, TRUE, NOW(), NOW())
        ON CONFLICT(user_id) DO UPDATE SET
          message_text=EXCLUDED.message_text,
          notify_chat_id=EXCLUDED.notify_chat_id,
          enabled=TRUE,
          updated_at=NOW()
        """, user_id, message_text, notify_chat_id)

async def get_active_auto_campaigns():
    async with get_pool().acquire() as con:
        return await con.fetch(
            "SELECT user_id, message_text, notify_chat_id FROM auto_campaigns WHERE enabled=TRUE ORDER BY updated_at"
        )

async def disable_auto_campaign(user_id: int):
    async with get_pool().acquire() as con:
        await con.execute(
            "UPDATE auto_campaigns SET enabled=FALSE, updated_at=NOW() WHERE user_id=$1",
            user_id,
        )

async def get_auto_campaign(user_id: int):
    async with get_pool().acquire() as con:
        return await con.fetchrow("SELECT user_id, message_text, notify_chat_id, enabled, updated_at FROM auto_campaigns WHERE user_id=$1", user_id)

async def set_auto_campaign_enabled(user_id: int, enabled: bool):
    async with get_pool().acquire() as con:
        await con.execute("UPDATE auto_campaigns SET enabled=$2, updated_at=NOW() WHERE user_id=$1", user_id, enabled)
