# language: Python, file: db.py
# SQLite. Ягон ORM нест — равшан ва фаҳмо.

import sqlite3
import csv
import io
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH, SUPER_ADMINS, DEFAULT_PRICES


@contextmanager
def conn():
    """Контекст-менеҷер: кушояд, commit кунад, пӯшад."""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row  # барои дастрасӣ бо ном: row["user_id"]
    try:
        yield c
        c.commit()
    finally:
        c.close()
def init_db():
    """Ҳама ҷадвалҳоро месозад агар набошанд."""
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            phone        TEXT,
            session_file TEXT,
            delay        INTEGER DEFAULT 300,
            stars        INTEGER DEFAULT 0,
            unsigned     INTEGER DEFAULT 0,
            connected    INTEGER DEFAULT 0,
            added_by     INTEGER,
            joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            group_id   INTEGER,
            title      TEXT,
            username   TEXT,
            selected   INTEGER DEFAULT 0,
            UNIQUE(user_id, group_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS prices (
            key   TEXT PRIMARY KEY,
            value INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS force_subs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER UNIQUE,
            username   TEXT,
            title      TEXT,
            invite     TEXT,
            added_by   INTEGER,
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            text     TEXT,
            run_at   TEXT,
            status   TEXT DEFAULT 'pending',
            created  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Админҳои аввал
        for a in SUPER_ADMINS:
            c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (a,))

        # Нархҳои пешфарз
        for key, val in DEFAULT_PRICES.items():
            c.execute("INSERT OR IGNORE INTO prices(key,value) VALUES(?,?)", (key, val))

def get_user(uid):
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None


def upsert_user(uid, username=None, first_name=None, added_by=None):
    with conn() as c:
        c.execute("""
            INSERT INTO users(user_id, username, first_name, added_by)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                updated_at=CURRENT_TIMESTAMP
        """, (uid, username, first_name, added_by))


def add_user_manual(uid, username, first_name, added_by):
    """админ корбарро дастӣ илова мекунад"""
    upsert_user(uid, username, first_name, added_by=added_by)


def set_session(uid, phone, session_file):
    with conn() as c:
        c.execute("""
            UPDATE users SET phone=?, session_file=?, connected=1,
            updated_at=CURRENT_TIMESTAMP WHERE user_id=?
        """, (phone, session_file, uid))


def disconnect_user(uid):
    with conn() as c:
        c.execute("""
            UPDATE users SET connected=0, session_file=NULL, phone=NULL
            WHERE user_id=?
        """, (uid,))


def set_delay(uid, delay):
    with conn() as c:
        c.execute("UPDATE users SET delay=? WHERE user_id=?", (delay, uid))


def set_unsigned(uid, val):
    with conn() as c:
        c.execute("UPDATE users SET unsigned=? WHERE user_id=?",
                  (1 if val else 0, uid))


def add_stars(uid, amount):
    with conn() as c:
        c.execute("UPDATE users SET stars = stars + ? WHERE user_id=?", (amount, uid))
        r = c.execute("SELECT stars FROM users WHERE user_id=?", (uid,)).fetchone()
        return r["stars"] if r else 0


def spend_stars(uid, amount):
    """True = сарф шуд, False = кофӣ нест"""
    if amount <= 0:
        return True
    with conn() as c:
        r = c.execute("SELECT stars FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r or r["stars"] < amount:
            return False
        c.execute("UPDATE users SET stars = stars - ? WHERE user_id=?", (amount, uid))
        return True


def get_stars(uid):
    u = get_user(uid)
    return u["stars"] if u else 0


def list_users(limit=100, only_connected=False):
    with conn() as c:
        q = "SELECT * FROM users"
        if only_connected:
            q += " WHERE connected=1"
        q += " ORDER BY joined_at DESC LIMIT ?"
        return [dict(r) for r in c.execute(q, (limit,)).fetchall()]


def count_users():
    with conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        conn_ = c.execute("SELECT COUNT(*) AS n FROM users WHERE connected=1").fetchone()["n"]
        return {"total": total, "connected": conn_}

def is_admin(uid):
    with conn() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None


def add_admin(uid, by):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO admins(user_id, added_by) VALUES(?,?)", (uid, by))


def remove_admin(uid):
    with conn() as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))


def list_admins():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM admins").fetchall()]


# ---------- Нархҳо ----------
def get_price(key, default=0):
    """нархи функсия. 0 = ройгон"""
    with conn() as c:
        r = c.execute("SELECT value FROM prices WHERE key=?", (key,)).fetchone()
        return int(r["value"]) if r else default


def set_price(key, value):
    with conn() as c:
        c.execute("""
            INSERT INTO prices(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, int(value)))


def all_prices():
    with conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT * FROM prices").fetchall()}


# ---------- Обунаи ҳатмӣ ----------
def add_force_sub(chat_id, username, title, invite, added_by):
    with conn() as c:
        c.execute("""
            INSERT INTO force_subs(chat_id, username, title, invite, added_by)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username,
                title=excluded.title,
                invite=excluded.invite
        """, (chat_id, username, title, invite, added_by))


def remove_force_sub(chat_id):
    with conn() as c:
        c.execute("DELETE FROM force_subs WHERE chat_id=?", (chat_id,))


def list_force_subs():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM force_subs ORDER BY id").fetchall()]


# ---------- Танзимот ----------
def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with conn() as c:
        c.execute("""
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))

def clear_user_groups(uid):
    with conn() as c:
        c.execute("DELETE FROM groups WHERE user_id=?", (uid,))


def add_group(uid, gid, title, username):
    with conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO groups(user_id, group_id, title, username)
            VALUES(?,?,?,?)
        """, (uid, gid, title, username))


def toggle_group(uid, gid):
    with conn() as c:
        r = c.execute("SELECT selected FROM groups WHERE user_id=? AND group_id=?",
                      (uid, gid)).fetchone()
        if not r:
            return None
        new = 0 if r["selected"] else 1
        c.execute("UPDATE groups SET selected=? WHERE user_id=? AND group_id=?",
                  (new, uid, gid))
        return new


def list_groups(uid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM groups WHERE user_id=? ORDER BY title", (uid,)).fetchall()]


def selected_groups(uid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM groups WHERE user_id=? AND selected=1", (uid,)).fetchall()]


# ---------- Таймерҳо ----------
def create_task(uid, text, run_at):
    with conn() as c:
        cur = c.execute("""
            INSERT INTO tasks(user_id, text, run_at, status)
            VALUES(?,?,?, 'pending')
        """, (uid, text, run_at))
        return cur.lastrowid


def due_tasks():
    now = datetime.utcnow().isoformat()
    with conn() as c:
        return [dict(r) for r in c.execute("""
            SELECT * FROM tasks WHERE status='pending' AND run_at <= ?
        """, (now,)).fetchall()]


def list_tasks(uid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY run_at", (uid,)).fetchall()]


def mark_task(task_id, status):
    with conn() as c:
        c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def delete_task(task_id):
    with conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------- CSV ----------
def export_users_csv():
    with conn() as c:
        rows = c.execute("""
            SELECT user_id, username, first_name, phone, delay, stars,
                   unsigned, connected, joined_at, updated_at
            FROM users ORDER BY joined_at DESC
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id","username","first_name","phone","delay_sec",
                "stars","unsigned","connected","joined_at","updated_at"])
    for r in rows:
        w.writerow([r["user_id"], r["username"] or "", r["first_name"] or "",
                    r["phone"] or "", r["delay"], r["stars"],
                    1 if r["unsigned"] else 0, 1 if r["connected"] else 0,
                    r["joined_at"], r["updated_at"]])
    return buf.getvalue()
