"""
Мультитенантный Telegram-Business бот на aiogram 3.x.

Один BOT_TOKEN на всех. У каждого владельца — свой Business-connection
и свой owner_id. Все данные изолированы по owner_id — никто не видит
чужое.

Возможности:
  * удалённые / изменённые / view-once сообщения собеседника
  * тарифы: free / pro / business
  * админ-панель (в личке с ботом у админа)
  * mute, история правок, .get медиа, игры, форматирование

Запуск:
  export BOT_TOKEN=токен_бота
  export ADMIN_IDS=123456,789012       # через запятую
  export ADMIN_BOT_TOKEN=токен_админ_бота   # опционально, отдельный бот для админки
  python sveta.py

Если ADMIN_BOT_TOKEN не задан — админка работает в ЛС основного бота
(но тогда основной бот должен быть доступен и в Business, и в ЛС).
"""

import asyncio
import html
import io
import logging
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import aiosqlite
from PIL import Image

from aiogram import Bot, Dispatcher, F, BaseMiddleware, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, BusinessMessagesDeleted,
    BotCommand, BotCommandScopeDefault,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path(".")

DB_PATH = str(DATA_DIR / "bot.db")
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_TTL_DAYS = int(os.environ.get("MEDIA_TTL_DAYS", "30"))


# ============================================================
#  Тарифы
# ============================================================

PLANS = {
    "free": {
        "title": "🆓 Бесплатный",
        "price": 0,
        "days": 0,
        "features": {
            "notify_deleted":    True,
            "notify_edited":     False,
            "notify_view_once":  False,
            "save_media":        False,
            "mute":              False,
            "history":           False,
        },
    },
    "pro": {
        "title": "💎 PRO",
        "price": 299,
        "days": 30,
        "features": {
            "notify_deleted":    True,
            "notify_edited":     True,
            "notify_view_once":  True,
            "save_media":        True,
            "mute":              True,
            "history":           True,
        },
    },
    "business": {
        "title": "🏢 BUSINESS",
        "price": 999,
        "days": 30,
        "features": {
            "notify_deleted":    True,
            "notify_edited":     True,
            "notify_view_once":  True,
            "save_media":        True,
            "mute":              True,
            "history":           True,
        },
    },
}


# ============================================================
#  DB
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    full_name    TEXT,
    plan         TEXT DEFAULT 'free',
    plan_until   TEXT,
    is_banned    INTEGER DEFAULT 0,
    created_at   TEXT,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    owner_id      INTEGER NOT NULL,
    can_reply     INTEGER DEFAULT 0,
    can_delete    INTEGER,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_conn_owner ON connections(owner_id);

CREATE TABLE IF NOT EXISTS chat_owners (
    chat_id    INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chatowner_owner ON chat_owners(owner_id);

CREATE TABLE IF NOT EXISTS afk (
    owner_id INTEGER PRIMARY KEY, reason TEXT, since TEXT
);
CREATE TABLE IF NOT EXISTS status (
    owner_id INTEGER PRIMARY KEY, text TEXT
);

CREATE TABLE IF NOT EXISTS message_cache (
    owner_id     INTEGER NOT NULL,
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    user_id      INTEGER,
    user_name    TEXT,
    text         TEXT,
    content_type TEXT,
    file_id      TEXT,
    file_path    TEXT,
    created_at   TEXT,
    PRIMARY KEY (owner_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_cache_owner ON message_cache(owner_id, chat_id);

CREATE TABLE IF NOT EXISTS deleted_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    user_id      INTEGER,
    user_name    TEXT,
    text         TEXT,
    content_type TEXT,
    file_path    TEXT,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_deleted_own ON deleted_messages(owner_id, chat_id);

CREATE TABLE IF NOT EXISTS edited_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    old_text    TEXT,
    new_text    TEXT,
    edited_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_edited_own ON edited_messages(owner_id, chat_id);

CREATE TABLE IF NOT EXISTS edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    text        TEXT,
    edited_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_own ON edit_history(owner_id, chat_id, message_id);

CREATE TABLE IF NOT EXISTS muted_chats (
    owner_id INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    muted_at TEXT,
    PRIMARY KEY (owner_id, chat_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    plan     TEXT NOT NULL,
    amount   INTEGER,
    days     INTEGER,
    paid_at  TEXT,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- Users ----------

async def upsert_user(user_id, username=None, full_name=None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=COALESCE(excluded.username, users.username), "
            "full_name=COALESCE(excluded.full_name, users.full_name), "
            "last_seen=excluded.last_seen",
            (user_id, username, full_name, now, now),
        )
        await db.commit()


async def get_user(user_id) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()


async def get_user_by_owner_id(owner_id) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (owner_id,))
        return await cur.fetchone()


async def list_users(limit=10, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return await cur.fetchall()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]


async def count_users_by_plan() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan")
        return {plan: n for plan, n in await cur.fetchall()}


async def set_user_plan(user_id, plan, days):
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET plan=?, plan_until=? WHERE user_id=?",
            (plan, until, user_id),
        )
        await db.commit()


async def ban_user(user_id, banned=True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?",
                         (1 if banned else 0, user_id))
        await db.commit()


async def log_payment(user_id, plan, amount, days, note=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (user_id, plan, amount, days, paid_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, plan, amount, days, datetime.now(timezone.utc).isoformat(), note),
        )
        await db.commit()


def active_plan(user_row) -> str:
    if not user_row:
        return "free"
    try:
        plan = user_row["plan"] or "free"
        until = user_row["plan_until"]
    except Exception:
        return "free"
    if plan != "free" and until:
        try:
            if datetime.fromisoformat(until) < datetime.now(timezone.utc):
                return "free"
        except Exception:
            return "free"
    return plan


async def features_for(owner_id: int) -> dict:
    row = await get_user(owner_id)
    return PLANS.get(active_plan(row), PLANS["free"])["features"]


# ---------- Connections ----------

async def save_connection(conn_id: str, info: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO connections (connection_id, owner_id, can_reply, can_delete, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id) DO UPDATE SET "
            "owner_id=excluded.owner_id, can_reply=excluded.can_reply, "
            "can_delete=excluded.can_delete, updated_at=excluded.updated_at",
            (conn_id, info["owner_id"], int(info["can_reply"]),
             None if info["can_delete"] is None else int(info["can_delete"]),
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def delete_connection(conn_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM connections WHERE connection_id=?", (conn_id,))
        await db.commit()


async def load_all_connections() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT connection_id, owner_id, can_reply, can_delete FROM connections"
        )
        out = {}
        for cid, oid, cr, cd in await cur.fetchall():
            out[cid] = {
                "owner_id": oid,
                "can_reply": bool(cr),
                "can_delete": None if cd is None else bool(cd),
            }
        return out


async def remember_chat_owner(chat_id: int, owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_owners (chat_id, owner_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET owner_id=excluded.owner_id, "
            "updated_at=excluded.updated_at",
            (chat_id, owner_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def load_chat_owner(chat_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT owner_id FROM chat_owners WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def load_all_chat_owners() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id, owner_id FROM chat_owners")
        return {cid: oid for cid, oid in await cur.fetchall()}


# ---------- AFK / Status ----------

async def set_afk(owner_id, reason, since_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO afk (owner_id, reason, since) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET reason=excluded.reason, since=excluded.since",
            (owner_id, reason, since_iso),
        )
        await db.commit()


async def get_afk(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT reason, since FROM afk WHERE owner_id=?", (owner_id,))
        return await cur.fetchone()


async def clear_afk(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM afk WHERE owner_id=?", (owner_id,))
        await db.commit()
        return cur.rowcount > 0


async def set_status(owner_id, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO status (owner_id, text) VALUES (?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET text=excluded.text",
            (owner_id, text),
        )
        await db.commit()


async def get_status(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT text FROM status WHERE owner_id=?", (owner_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------- Cache ----------

async def cache_message(owner_id, chat_id, message_id, user_id, user_name, text,
                        content_type="text", file_id=None, file_path=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO message_cache "
            "(owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, chat_id, message_id) DO UPDATE SET "
            "text=excluded.text, file_id=excluded.file_id, "
            "file_path=COALESCE(excluded.file_path, message_cache.file_path), "
            "content_type=excluded.content_type",
            (owner_id, chat_id, message_id, user_id, user_name, text,
             content_type, file_id, file_path, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, user_name, text, content_type, file_id, file_path "
            "FROM message_cache WHERE owner_id=? AND chat_id=? AND message_id=?",
            (owner_id, chat_id, message_id),
        )
        return await cur.fetchone()


async def drop_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM message_cache WHERE owner_id=? AND chat_id=? AND message_id=?",
            (owner_id, chat_id, message_id),
        )
        await db.commit()


# ---------- Logs ----------

async def log_deleted(owner_id, chat_id, message_id, user_id, user_name, text,
                      content_type, file_path):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deleted_messages "
            "(owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, text, content_type,
             file_path, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_last_deleted(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at "
            "FROM deleted_messages WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
            (owner_id, chat_id, limit),
        )
        return await cur.fetchall()


async def find_deleted_by_message_id(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at "
            "FROM deleted_messages WHERE owner_id=? AND chat_id=? AND message_id=? "
            "ORDER BY id DESC LIMIT 1",
            (owner_id, chat_id, message_id),
        )
        return await cur.fetchone()


async def log_edited(owner_id, chat_id, message_id, user_id, user_name, old_text, new_text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO edited_messages "
            "(owner_id, chat_id, message_id, user_id, user_name, old_text, new_text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, old_text, new_text,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_last_edited(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, old_text, new_text, edited_at FROM edited_messages "
            "WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
            (owner_id, chat_id, limit),
        )
        return await cur.fetchall()


async def add_edit_version(owner_id, chat_id, message_id, user_id, user_name, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM edit_history "
            "WHERE owner_id=? AND chat_id=? AND message_id=?",
            (owner_id, chat_id, message_id),
        )
        row = await cur.fetchone()
        next_version = (row[0] or 0) + 1
        await db.execute(
            "INSERT INTO edit_history "
            "(owner_id, chat_id, message_id, version, user_id, user_name, text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, next_version, user_id, user_name, text,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return next_version


async def get_edit_history(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT version, text, edited_at FROM edit_history "
            "WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY version ASC",
            (owner_id, chat_id, message_id),
        )
        return await cur.fetchall()


# ---------- Mute ----------

async def set_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO muted_chats (owner_id, chat_id, muted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id, chat_id) DO UPDATE SET muted_at=excluded.muted_at",
            (owner_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def is_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?",
            (owner_id, chat_id),
        )
        return await cur.fetchone() is not None


async def clear_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM muted_chats WHERE owner_id=? AND chat_id=?",
            (owner_id, chat_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ============================================================
#  Глобалы и хелперы
# ============================================================

dp = Dispatcher()
admin_router = Router()          # админка в ЛС (основной бот)
_bot: Optional[Bot] = None
_admin_bot: Optional[Bot] = None

_connections: Dict[str, dict] = {}   # connection_id -> info
_chat_owners: Dict[int, int] = {}    # chat_id -> owner_id
_recent_messages: Dict[int, list] = {}


def get_owner_by_connection(connection_id: Optional[str]) -> Optional[int]:
    if not connection_id:
        return None
    info = _connections.get(connection_id)
    return info["owner_id"] if info else None


def get_owner_by_chat(chat_id: int) -> Optional[int]:
    return _chat_owners.get(chat_id)


async def load_state():
    global _connections, _chat_owners
    _connections = await load_all_connections()
    _chat_owners = await load_all_chat_owners()
    logger.info(f"[state] connections={len(_connections)} chats={len(_chat_owners)}")


def _user_name(user) -> str:
    if not user:
        return "unknown"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def _chat_title(obj) -> str:
    chat = getattr(obj, "chat", None)
    if not chat:
        return "unknown chat"
    return chat.title or chat.full_name or str(chat.id)


def _ext_from_path(file_path, content_type):
    if file_path and "." in file_path.split("/")[-1]:
        return "." + file_path.rsplit(".", 1)[-1].lower()
    return {
        "photo": ".jpg", "video": ".mp4", "voice": ".ogg", "audio": ".mp3",
        "video_note": ".mp4", "animation": ".mp4", "sticker": ".webp",
        "document": ".bin",
    }.get(content_type, ".bin")


def _media_file_id_and_type(message: Message):
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    if message.voice:
        return message.voice.file_id, "voice"
    if message.audio:
        return message.audio.file_id, "audio"
    if message.video_note:
        return message.video_note.file_id, "video_note"
    if message.animation:
        return message.animation.file_id, "animation"
    if message.sticker:
        return message.sticker.file_id, "sticker"
    if message.document:
        return message.document.file_id, "document"
    return None, None


def _is_view_once(message: Message) -> bool:
    return bool(getattr(message, "has_media_spoiler", False)
                or getattr(message, "ttl_seconds", None))


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================================
#  Media
# ============================================================

async def download_media(b
