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
