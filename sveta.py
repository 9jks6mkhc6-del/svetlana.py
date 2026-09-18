"""
Telegram-Business бот на aiogram 3.x — всё в одном.
Один BOT_TOKEN. Business + подписки + админка в одном процессе.
"""

import asyncio
import html
import io
import json
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

DEFAULT_PLANS = {
    "free": {"title": "🆓 Бесплатный", "price": 0, "days": 0,
             "features": {"notify_deleted": True, "notify_edited": False, "notify_view_once": False,
                          "save_media": False, "mute": False, "history": False}},
    "pro": {"title": "💎 PRO", "price": 299, "days": 30,
            "features": {"notify_deleted": True, "notify_edited": True, "notify_view_once": True,
                         "save_media": True, "mute": True, "history": True}},
    "business": {"title": "🏢 BUSINESS", "price": 999, "days": 30,
                 "features": {"notify_deleted": True, "notify_edited": True, "notify_view_once": True,
                              "save_media": True, "mute": True, "history": True}},
}
PLANS: dict = {k: dict(v) for k, v in DEFAULT_PLANS.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    plan TEXT DEFAULT 'free', plan_until TEXT, is_banned INTEGER DEFAULT 0,
    created_at TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    key TEXT PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL,
    days INTEGER NOT NULL, features TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
    can_reply INTEGER DEFAULT 0, can_delete INTEGER, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conn_owner ON connections(owner_id);
CREATE TABLE IF NOT EXISTS chat_owners (
    chat_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chatowner_owner ON chat_owners(owner_id);
CREATE TABLE IF NOT EXISTS afk (owner_id INTEGER PRIMARY KEY, reason TEXT, since TEXT);
CREATE TABLE IF NOT EXISTS status (owner_id INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS message_cache (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    user_id INTEGER, user_name TEXT, text TEXT, content_type TEXT, file_id TEXT,
    file_path TEXT, created_at TEXT, PRIMARY KEY (owner_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS deleted_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, text TEXT,
    content_type TEXT, file_path TEXT, deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deleted_own ON deleted_messages(owner_id, chat_id);
CREATE TABLE IF NOT EXISTS edited_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, old_text TEXT,
    new_text TEXT, edited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_edited_own ON edited_messages(owner_id, chat_id);
CREATE TABLE IF NOT EXISTS edit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, version INTEGER NOT NULL, user_id INTEGER,
    user_name TEXT, text TEXT, edited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_own ON edit_history(owner_id, chat_id, message_id);
CREATE TABLE IF NOT EXISTS muted_chats (
    owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, muted_at TEXT,
    PRIMARY KEY (owner_id, chat_id)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan TEXT NOT NULL,
    amount INTEGER, days INTEGER, paid_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def init_plans():
    global PLANS
    async with aiosqlite.connect(DB_PATH) as db:
        for key, p in DEFAULT_PLANS.items():
            await db.execute(
                "INSERT OR IGNORE INTO plans (key, title, price, days, features, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, p["title"], p["price"], p["days"], json.dumps(p["features"]),
                 datetime.now(timezone.utc).isoformat()))
        await db.commit()
        await _reload_plans(db)
    logger.info(f"[plans] loaded: {list(PLANS.keys())}")

async def _reload_plans(db):
    global PLANS
    cur = await db.execute("SELECT key, title, price, days, features FROM plans")
    loaded = {}
    for key, title, price, days, feats_json in await cur.fetchall():
        loaded[key] = {"title": title, "price": price, "days": days, "features": json.loads(feats_json)}
    PLANS = loaded

async def update_plan(key, price=None, days=None, title=None):
    async with aiosqlite.connect(DB_PATH) as db:
        fields, values = [], []
        if price is not None: fields.append("price=?"); values.append(price)
        if days is not None: fields.append("days=?"); values.append(days)
        if title is not None: fields.append("title=?"); values.append(title)
        if not fields: return
        fields.append("updated_at=?"); values.append(datetime.now(timezone.utc).isoformat())
        values.append(key)
        await db.execute(f"UPDATE plans SET {', '.join(fields)} WHERE key=?", values)
        await db.commit()
        await _reload_plans(db)

async def reset_plan(key):
    p = DEFAULT_PLANS.get(key)
    if not p: return
    await update_plan(key, price=p["price"], days=p["days"], title=p["title"])

async def upsert_user(user_id, username=None, full_name=None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(excluded.username, users.username), "
            "full_name=COALESCE(excluded.full_name, users.full_name), last_seen=excluded.last_seen",
            (user_id, username, full_name, now, now))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()

async def list_users(limit=10, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
        return await cur.fetchall()

async def count_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]

async def stats_summary():
    async with aiosqlite.connect(DB_PATH) as db:
        out = {}
        cur = await db.execute("SELECT COUNT(*) FROM users"); out["users"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan")
        out["plans"] = {p: n for p, n in await cur.fetchall()}
        cur = await db.execute("SELECT COUNT(*) FROM connections"); out["connections"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM deleted_messages"); out["deleted"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM edited_messages"); out["edited"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM payments"); out["payments"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(amount),0) FROM payments"); out["revenue"] = (await cur.fetchone())[0]
        return out

async def set_user_plan(user_id, plan, days):
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET plan=?, plan_until=? WHERE user_id=?", (plan, until, user_id))
        await db.commit()

async def ban_user(user_id, banned=True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
        await db.commit()

async def log_payment(user_id, plan, amount, days, note=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments (user_id, plan, amount, days, paid_at, note) VALUES (?, ?, ?, ?, ?, ?)",
                         (user_id, plan, amount, days, datetime.now(timezone.utc).isoformat(), note))
        await db.commit()

def active_plan(user_row):
    if not user_row: return "free"
    try:
        plan = user_row["plan"] or "free"
        until = user_row["plan_until"]
    except Exception:
        return "free"
    if plan != "free" and until:
        try:
            if datetime.fromisoformat(until) < datetime.now(timezone.utc): return "free"
        except Exception:
            return "free"
    return plan

async def features_for(owner_id):
    row = await get_user(owner_id)
    return PLANS.get(active_plan(row), DEFAULT_PLANS["free"])["features"]

async def save_connection(conn_id, info):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO connections (connection_id, owner_id, can_reply, can_delete, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id) DO UPDATE SET owner_id=excluded.owner_id, can_reply=excluded.can_reply, "
            "can_delete=excluded.can_delete, updated_at=excluded.updated_at",
            (conn_id, info["owner_id"], int(info["can_reply"]),
             None if info["can_delete"] is None else int(info["can_delete"]),
             datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def delete_connection(conn_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM connections WHERE connection_id=?", (conn_id,))
        await db.commit()

async def load_all_connections():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT connection_id, owner_id, can_reply, can_delete FROM connections")
        out = {}
        for cid, oid, cr, cd in await cur.fetchall():
            out[cid] = {"owner_id": oid, "can_reply": bool(cr),
                        "can_delete": None if cd is None else bool(cd)}
        return out

async def remember_chat_owner(chat_id, owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_owners (chat_id, owner_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET owner_id=excluded.owner_id, updated_at=excluded.updated_at",
            (chat_id, owner_id, datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def load_chat_owner(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT owner_id FROM chat_owners WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def load_all_chat_owners():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id, owner_id FROM chat_owners")
        return {cid: oid for cid, oid in await cur.fetchall()}

async def set_afk(owner_id, reason, since_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO afk (owner_id, reason, since) VALUES (?, ?, ?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET reason=excluded.reason, since=excluded.since",
                         (owner_id, reason, since_iso))
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
        await db.execute("INSERT INTO status (owner_id, text) VALUES (?, ?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET text=excluded.text", (owner_id, text))
        await db.commit()

async def get_status(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT text FROM status WHERE owner_id=?", (owner_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def cache_message(owner_id, chat_id, message_id, user_id, user_name, text,
                        content_type="text", file_id=None, file_path=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO message_cache (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, chat_id, message_id) DO UPDATE SET text=excluded.text, file_id=excluded.file_id, "
            "file_path=COALESCE(excluded.file_path, message_cache.file_path), content_type=excluded.content_type",
            (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path,
             datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, user_name, text, content_type, file_id, file_path FROM message_cache "
            "WHERE owner_id=? AND chat_id=? AND message_id=?", (owner_id, chat_id, message_id))
        return await cur.fetchone()

async def drop_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM message_cache WHERE owner_id=? AND chat_id=? AND message_id=?",
                         (owner_id, chat_id, message_id))
        await db.commit()

async def log_deleted(owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deleted_messages (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path,
             datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_last_deleted(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at FROM deleted_messages "
            "WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?", (owner_id, chat_id, limit))
        return await cur.fetchall()

async def find_deleted_by_message_id(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at FROM deleted_messages "
            "WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
            (owner_id, chat_id, message_id))
        return await cur.fetchone()

async def log_edited(owner_id, chat_id, message_id, user_id, user_name, old_text, new_text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO edited_messages (owner_id, chat_id, message_id, user_id, user_name, old_text, new_text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, old_text, new_text,
             datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_last_edited(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, old_text, new_text, edited_at FROM edited_messages "
            "WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?", (owner_id, chat_id, limit))
        return await cur.fetchall()

async def add_edit_version(owner_id, chat_id, message_id, user_id, user_name, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM edit_history WHERE owner_id=? AND chat_id=? AND message_id=?",
            (owner_id, chat_id, message_id))
        row = await cur.fetchone()
        next_version = (row[0] or 0) + 1
        await db.execute(
            "INSERT INTO edit_history (owner_id, chat_id, message_id, version, user_id, user_name, text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, next_version, user_id, user_name, text,
             datetime.now(timezone.utc).isoformat()))
        await db.commit()
        return next_version

async def get_edit_history(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT version, text, edited_at FROM edit_history WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY version ASC",
            (owner_id, chat_id, message_id))
        return await cur.fetchall()

async def set_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO muted_chats (owner_id, chat_id, muted_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(owner_id, chat_id) DO UPDATE SET muted_at=excluded.muted_at",
                         (owner_id, chat_id, datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def is_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        return await cur.fetchone() is not None

async def clear_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        await db.commit()
        return cur.rowcount > 0

dp = Dispatcher()
_bot = None
_connections = {}
_chat_owners = {}
_recent_messages = {}
MAX_HISTORY = 50
_pending_admin_input = {}

def get_owner_by_connection(connection_id):
    if not connection_id: return None
    info = _connections.get(connection_id)
    return info["owner_id"] if info else None

def get_owner_by_chat(chat_id):
    return _chat_owners.get(chat_id)

async def load_state():
    global _connections, _chat_owners
    _connections = await load_all_connections()
    _chat_owners = await load_all_chat_owners()
    logger.info(f"[state] connections={len(_connections)} chats={len(_chat_owners)}")

def _user_name(user):
    if not user: return "unknown"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))

def _chat_title(obj):
    chat = getattr(obj, "chat", None)
    if not chat: return "unknown chat"
    return chat.title or chat.full_name or str(chat.id)

def _ext_from_path(file_path, content_type):
    if file_path and "." in file_path.split("/")[-1]:
        return "." + file_path.rsplit(".", 1)[-1].lower()
    return {"photo": ".jpg", "video": ".mp4", "voice": ".ogg", "audio": ".mp3",
            "video_note": ".mp4", "animation": ".mp4", "sticker": ".webp", "document": ".bin"}.get(content_type, ".bin")

def _media_file_id_and_type(message):
    if message.photo: return message.photo[-1].file_id, "photo"
    if message.video: return message.video.file_id, "video"
    if message.voice: return message.voice.file_id, "voice"
    if message.audio: return message.audio.file_id, "audio"
    if message.video_note: return message.video_note.file_id, "video_note"
    if message.animation: return message.animation.file_id, "animation"
    if message.sticker: return message.sticker.file_id, "sticker"
    if message.document: return message.document.file_id, "document"
    return None, None

def _is_view_once(message):
    return bool(getattr(message, "has_media_spoiler", False) or getattr(message, "ttl_seconds", None))

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def download_media(bot, file_id, content_type, owner_id, chat_id, message_id):
    try:
        file = await bot.get_file(file_id)
        ext = _ext_from_path(file.file_path, content_type)
        fname = f"{owner_id}_{chat_id}_{message_id}_{int(time.time())}{ext}"
        dest = MEDIA_DIR / fname
        await bot.download_file(file.file_path, destination=dest)
        return str(dest)
    except Exception as e:
        logger.warning(f"[media:{owner_id}] {e}")
        return None

async def forward_view_once(bot, owner_id, message, file_id, content_type, user_name):
    if not owner_id: return None
    try:
        file = await bot.get_file(file_id)
        buf = await bot.download_file(file.file_path)
        data = buf.read()
        ext = _ext_from_path(file.file_path, content_type)
        fname = f"{owner_id}_{message.chat.id}_{message.message_id}_{int(time.time())}{ext}"
        dest = MEDIA_DIR / fname
        with open(dest, "wb") as f: f.write(data)
        caption = (f"📸 <b>ОДНОРАЗОВОЕ ({content_type})</b>\n"
                   f"От: <b>{html.escape(user_name)}</b>\nЧат: <b>{html.escape(_chat_title(message))}</b>")
        await notify_owner_bytes(owner_id, data, f"view_once{ext}", caption,
                                 is_photo=(content_type == "photo"),
                                 is_video=(content_type in ("video", "video_note", "animation")))
        return str(dest)
    except Exception as e:
        logger.error(f"[view_once:{owner_id}] {e}")
        return None

async def cleanup_old_media():
    if MEDIA_TTL_DAYS <= 0: return
    cutoff_ts = time.time() - MEDIA_TTL_DAYS * 86400
    removed = 0
    for p in MEDIA_DIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff_ts:
                p.unlink(); removed += 1
        except Exception as e:
            logger.warning(f"[cleanup] {p}: {e}")
    if removed: logger.info(f"[cleanup] removed {removed}")

async def media_cleanup_loop():
    while True:
        try: await cleanup_old_media()
        except Exception as e: logger.warning(f"[cleanup] {e}")
        await asyncio.sleep(3600)

async def notify_owner(owner_id, text, file_path=None, is_photo=False, is_video=False, _attempt=1):
    if not _bot or not owner_id: return
    try:
        if file_path and os.path.exists(file_path):
            if is_photo:
                await _bot.send_photo(owner_id, FSInputFile(file_path), caption=text)
            elif is_video:
                try: await _bot.send_video(owner_id, FSInputFile(file_path), caption=text)
                except Exception: await _bot.send_document(owner_id, FSInputFile(file_path), caption=text)
            else:
                await _bot.send_document(owner_id, FSInputFile(file_path), caption=text)
        else:
            await _bot.send_message(owner_id, text)
    except Exception as e:
        logger.error(f"[notify→{owner_id}] attempt {_attempt}: {e}")
        if _attempt < 3:
            await asyncio.sleep(1.5 * _attempt)
            await notify_owner(owner_id, text, file_path, is_photo, is_video, _attempt + 1)

async def notify_owner_bytes(owner_id, buf_bytes, filename, text, is_photo=False, is_video=False, _attempt=1):
    if not _bot or not owner_id: return
    try:
        file = BufferedInputFile(buf_bytes, filename=filename)
        if is_photo:
            await _bot.send_photo(owner_id, file, caption=text)
        elif is_video:
            try: await _bot.send_video(owner_id, file, caption=text)
            except Exception:
                file2 = BufferedInputFile(buf_bytes, filename=filename)
                await _bot.send_document(owner_id, file2, caption=text)
        else:
            await _bot.send_document(owner_id, file, caption=text)
    except Exception as e:
        logger.error(f"[notify_bytes→{owner_id}] attempt {_attempt}: {e}")
        if _attempt < 3:
            await asyncio.sleep(1.5 * _attempt)
            await notify_owner_bytes(owner_id, buf_bytes, filename, text, is_photo, is_video, _attempt + 1)

async def reply_business(message, text, **kwargs):
    if not _bot or not message.business_connection_id: return
    try:
        await _bot.send_message(chat_id=message.chat.id, text=text,
                                business_connection_id=message.business_connection_id, **kwargs)
    except Exception as e:
        logger.error(f"[reply_business] {e}")

LEET_MAP = str.maketrans({"a": "4", "A": "4", "e": "3", "E": "3", "i": "1", "I": "1",
                          "o": "0", "O": "0", "s": "5", "S": "5", "t": "7", "T": "7",
                          "b": "6", "B": "6", "а": "4", "А": "4", "е": "3", "Е": "3",
                          "о": "0", "О": "0"})
KAWAII_SUFFIXES = ["Ꮚ˶ᐢ.ᐢ˶Ꮚ", "( ˶ˆ ᗜ ˆ˵ )", "•ᴗ•", "(๑>ᴗ<๑)", "uwu", "~"]
TSUNDERE_PREFIXES = ["Э-это не значит, что я рад, но... ", "Б-бака! ", "Не подумай ничего такого, но: "]
YANDERE_SUFFIXES = [" ...иначе я никому тебя не отдам.", " ...ты же будешь только моим?", " ня~ ♡ (это не угроза)"]
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
_RU = "йцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
_EN_TO_RU = str.maketrans(_EN, _RU)
_RU_TO_EN = str.maketrans(_RU, _EN)

def to_bold(t): return f"<b>{html.escape(t)}</b>"
def to_italic(t): return f"<i>{html.escape(t)}</i>"
def to_monospace(t): return f"<code>{html.escape(t)}</code>"
def to_underline(t): return f"<u>{html.escape(t)}</u>"
def to_leet(t): return t.translate(LEET_MAP)
def to_kawaii(t): return f"{t} {random.choice(KAWAII_SUFFIXES)}"
def to_tsundere(t): return f"{random.choice(TSUNDERE_PREFIXES)}{t}"
def to_yandere(t): return f"{t}{random.choice(YANDERE_SUFFIXES)}"

def swap_layout(text):
    cyrillic = sum(1 for c in text if "а" <= c.lower() <= "я")
    return text.translate(_EN_TO_RU) if cyrillic == 0 else text.translate(_RU_TO_EN)

FORMAT_COMMANDS = {"bold": to_bold, "italic": to_italic, "monospace": to_monospace,
                   "underline": to_underline, "leet": to_leet, "kawaii": to_kawaii,
                   "tsundere": to_tsundere, "yandere": to_yandere}

def is_cmd(text, name):
    if not text: return False
    return text.strip().split(maxsplit=1)[0].lower() == f".{name}"

def cmd_arg(text):
    parts = text.strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""

_ttt_games = {}
WIN_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

def ttt_keyboard(board, message_id):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            label = board[idx] if board[idx] else "·"
            row.append(InlineKeyboardButton(text=label, callback_data=f"ttt:{message_id}:{idx}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ttt_winner(board):
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]: return board[a]
    if all(board): return "draw"
    return None

_bw_games = {}

def bw_keyboard(game, message_id):
    size = game["size"]; board = game["board"]; rows = []
    for r in range(size):
        row = []
        for c in range(size):
            idx = r * size + c
            label = "⬛" if board[idx] else "⬜"
            row.append(InlineKeyboardButton(text=label, callback_data=f"bw:{message_id}:{idx}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def coinflip():
    return random.choice(["🪙 Орёл!", "🪙 Решка!"])

def degrade_image(data, scale_down=8, jpeg_quality=10):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    small = img.resize((max(1, w // scale_down), max(1, h // scale_down)))
    buf = io.BytesIO(); small.save(buf, format="JPEG", quality=jpeg_quality); buf.seek(0)
    compressed = Image.open(buf).convert("RGB")
    result = compressed.resize((w, h))
    out = io.BytesIO(); result.save(out, format="JPEG", quality=40); out.seek(0)
    return out.read()

def photo_to_gif(data):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    out = io.BytesIO(); img.save(out, format="GIF"); out.seek(0)
    return out.read()

HELP_BUSINESS = """<b>Команды (Business)</b>

<b>Утилиты</b>
.help .afk [причина]/off .status текст .time .sw текст .info .love .diag

<b>Формат</b> (ответом): .bold .italic .monospace .underline .leet .kawaii .tsundere .yandere

<b>Игры</b>: .dice .flip .ttt @user .bw [размер]

<b>Медиа</b> (ответом на фото): .lq .gif .get

<b>История</b>: .short .deleted .edited .edits

<b>Модерация</b>: .mute .unmute
"""

def main_menu_kb(is_adm):
    rows = [
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="menu:profile")],
        [InlineKeyboardButton(text="💎 Тарифы и подписка", callback_data="menu:plans")],
        [InlineKeyboardButton(text="📖 Как подключить", callback_data="menu:howto")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="menu:mystats")],
    ]
    if is_adm: rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb(target="menu:root"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]])

def plans_kb():
    rows = []
    for key in ("pro", "business"):
        p = PLANS.get(key)
        if p:
            rows.append([InlineKeyboardButton(text=f"{p['title']} — {p['price']}₽/{p['days']}дн",
                                              callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_root_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="💰 Тарифы", callback_data="admin:plans")],
        [InlineKeyboardButton(text="🔎 Найти юзера", callback_data="admin:find")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")],
    ])

def admin_user_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Выдать PRO", callback_data=f"admin:grant:{uid}:pro")],
        [InlineKeyboardButton(text="🏢 Выдать BUSINESS", callback_data=f"admin:grant:{uid}:business")],
        [InlineKeyboardButton(text="🚫 Обнулить (free)", callback_data=f"admin:grant:{uid}:free")],
        [InlineKeyboardButton(text="⛔ Бан / Разбан", callback_data=f"admin:ban:{uid}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users:0")],
    ])

def admin_plans_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить PRO", callback_data="admin:editplan:pro")],
        [InlineKeyboardButton(text="✏️ Изменить BUSINESS", callback_data="admin:editplan:business")],
        [InlineKeyboardButton(text="♻️ Сбросить PRO", callback_data="admin:resetplan:pro")],
        [InlineKeyboardButton(text="♻️ Сбросить BUSINESS", callback_data="admin:resetplan:business")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:root")],
    ])

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    if connection.is_enabled:
        rights = getattr(connection, "rights", None)
        can_delete = None
        if rights is not None:
            can_delete = bool(getattr(rights, "can_delete_all_messages", False) or
                              getattr(rights, "can_delete_sent_messages", False))
        info = {"owner_id": connection.user.id,
                "can_reply": bool(getattr(connection, "can_reply", False)),
                "can_delete": can_delete}
        _connections[connection.id] = info
        await save_connection(connection.id, info)
        await upsert_user(connection.user.id, connection.user.username, connection.user.full_name)
        logger.info(f"[conn] + {connection.id} owner={info['owner_id']}")
        try:
            await _bot.send_message(info["owner_id"],
                "✅ Бот подключён к твоему Telegram Business.\n"
                "Напиши в чате <code>.help</code> — увидишь команды.")
        except Exception as e:
            logger.warning(f"[conn] greet failed: {e}")
    else:
        _connections.pop(connection.id, None)
        await delete_connection(connection.id)
        logger.info(f"[conn] - {connection.id}")

class IncomingCacheMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        try: await self._cache_incoming(event, bot)
        except Exception as e: logger.error(f"[cache_mw] {event.message_id}: {e}")
        return await handler(event, data)

    @staticmethod
    async def _cache_incoming(message, bot):
        if not message.from_user: return
        owner_id = get_owner_by_connection(message.business_connection_id)
        if not owner_id: return
        _chat_owners[message.chat.id] = owner_id
        await remember_chat_owner(message.chat.id, owner_id)
        if message.from_user.id == owner_id: return
        feats = await features_for(owner_id)
        if message.text:
            await cache_message(owner_id, message.chat.id, message.message_id,
                                message.from_user.id, _user_name(message.from_user), message.text, "text")
            if not message.text.startswith("."):
                hist = _recent_messages.setdefault(message.chat.id, [])
                hist.append(f"{message.from_user.first_name}: {message.text}")
                if len(hist) > MAX_HISTORY: del hist[0]
            return
        file_id, content_type = _media_file_id_and_type(message)
        if not file_id: return
        user_name = _user_name(message.from_user)
        caption = message.caption or f"<{content_type}>"
        view_once_path = None
        if _is_view_once(message) and feats["notify_view_once"]:
            view_once_path = await forward_view_once(bot, owner_id, message, file_id, content_type, user_name)
        if view_once_path: file_path = view_once_path
        elif feats["save_media"]:
            file_path = await download_media(bot, file_id, content_type, owner_id,
                                             message.chat.id, message.message_id)
        else: file_path = None
        await cache_message(owner_id, message.chat.id, message.message_id,
                            message.from_user.id, user_name, caption, content_type, file_id, file_path)

dp.business_message.outer_middleware(IncomingCacheMiddleware())

@dp.edited_business_message()
async def on_edited(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id: return
    if message.from_user and message.from_user.id == owner_id: return
    feats = await features_for(owner_id)
    if not feats["notify_edited"]: return
    if message.text:
        cached = await get_cached_message(owner_id, message.chat.id, message.message_id)
        old_text = cached[2] if cached else "<нет в кэше>"
        user_name = _user_name(message.from_user)
        uid = message.from_user.id if message.from_user else None
        await log_edited(owner_id, message.chat.id, message.message_id, uid, user_name, old_text, message.text)
        await add_edit_version(owner_id, message.chat.id, message.message_id, uid, user_name, message.text)
        await cache_message(owner_id, message.chat.id, message.message_id, uid, user_name, message.text, "text")
        await notify_owner(owner_id,
            f"✏️ <b>Изменённое сообщение</b>\nЧат: <b>{html.escape(_chat_title(message))}</b>\n"
            f"Автор: <b>{html.escape(user_name)}</b>\n"
            f"Было: <i>{html.escape(old_text or '')}</i>\nСтало: <i>{html.escape(message.text)}</i>")
    if message.caption:
        cached = await get_cached_message(owner_id, message.chat.id, message.message_id)
        old_text = cached[2] if cached else "<нет в кэше>"
        user_name = _user_name(message.from_user)
        uid = message.from_user.id if message.from_user else None
        await add_edit_version(owner_id, message.chat.id, message.message_id, uid, user_name, f"[caption] {message.caption}")
        await notify_owner(owner_id,
            f"✏️ <b>Изменена подпись</b>\nЧат: <b>{html.escape(_chat_title(message))}</b>\n"
            f"Автор: <b>{html.escape(user_name)}</b>\n"
            f"Было: <i>{html.escape(old_text or '')}</i>\nСтало: <i>{html.escape(message.caption)}</i>")

@dp.deleted_business_messages()
async def on_deleted_business(deleted: BusinessMessagesDeleted):
    chat_id = deleted.chat.id
    owner_id = get_owner_by_chat(chat_id) or await load_chat_owner(chat_id)
    if not owner_id: return
    feats = await features_for(owner_id)
    if not feats["notify_deleted"]: return
    chat_label = html.escape(_chat_title(deleted))
    for msg_id in deleted.message_ids:
        cached = await get_cached_message(owner_id, chat_id, msg_id)
        if not cached:
            await notify_owner(owner_id,
                f"🗑 <b>Удалено сообщение</b>\nЧат: <b>{chat_label}</b>\nmsg_id: {msg_id}\n"
                f"<i>Текст/файл недоступны</i>")
            continue
        user_id, user_name, text, content_type, file_id, file_path = cached
        if user_id == owner_id:
            await drop_cached_message(owner_id, chat_id, msg_id)
            continue
        await log_deleted(owner_id, chat_id, msg_id, user_id, user_name, text, content_type, file_path)
        await drop_cached_message(owner_id, chat_id, msg_id)
        await notify_owner(owner_id,
            f"🗑 <b>Удалённое сообщение</b>\nЧат: <b>{chat_label}</b>\n"
            f"Автор: <b>{html.escape(user_name or 'unknown')}</b>\n"
            f"Тип: {content_type}\nТекст: <i>{html.escape(text or '')}</i>",
            file_path=file_path, is_photo=(content_type == "photo"),
            is_video=(content_type in ("video", "video_note", "animation")))

@dp.business_message(CommandStart())
async def b_start(message: Message):
    await reply_business(message, "Привет! Набери .help — увидишь список команд.")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "help")))
async def b_help(message: Message):
    await reply_business(message, HELP_BUSINESS)

@dp.business_message(F.text.func(lambda t: is_cmd(t, "love")))
async def b_love(message: Message):
    await reply_business(message, "❤️")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "time")))
async def b_time(message: Message):
    now = datetime.now(timezone.utc).astimezone()
    await reply_business(message, f"🕒 {now.strftime('%H:%M:%S %d.%m.%Y')}")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "diag")))
async def b_diag(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    row = await get_user(owner_id) if owner_id else None
    lines = ["<b>Диагностика</b>", f"owner_id: <code>{owner_id}</code>",
             f"connection: <code>{message.business_connection_id}</code>",
             f"plan: <b>{active_plan(row)}</b>"]
    if row: lines.append(f"plan_until: <code>{row['plan_until'] or '—'}</code>")
    feats = await features_for(owner_id) if owner_id else PLANS["free"]["features"]
    for k, v in feats.items(): lines.append(f"{k}: <b>{v}</b>")
    await reply_business(message, "\n".join(lines))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "info")))
async def b_info(message: Message):
    u = message.from_user
    owner_id = get_owner_by_connection(message.business_connection_id)
    status = await get_status(owner_id) if owner_id else None
    lines = [f"<b>{u.full_name}</b>", f"id: <code>{u.id}</code>"]
    if u.username: lines.append(f"username: @{u.username}")
    if status: lines.append(f"status: {html.escape(status)}")
    await reply_business(message, "\n".join(lines))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "status")))
async def b_status(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    text = cmd_arg(message.text)
    if not text:
        await reply_business(message, "Использование: .status текст"); return
    await set_status(owner_id, text)
    await reply_business(message, "Статус обновлён.")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "afk")))
async def b_afk(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    arg = cmd_arg(message.text).strip()
    if arg.lower() == "off":
        removed = await clear_afk(owner_id)
        await reply_business(message, "AFK снят." if removed else "Ты не был в AFK."); return
    reason = arg or "без причины"
    await set_afk(owner_id, reason, datetime.now(timezone.utc).isoformat())
    await reply_business(message, f"Включён AFK: {html.escape(reason)}")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "sw")))
async def b_sw(message: Message):
    text = cmd_arg(message.text)
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    if not text:
        await reply_business(message, "Использование: .sw текст (или ответом)"); return
    await reply_business(message, swap_layout(text))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "mute")))
async def b_mute(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    feats = await features_for(owner_id)
    if not feats["mute"]:
        await reply_business(message, "🔒 Mute доступен на 💎 PRO и 🏢 BUSINESS."); return
    await set_muted(owner_id, message.chat.id)
    await reply_business(message, "🔇 <b>Mute включён.</b>\nСмотреть: <code>.deleted</code>\nВыключить: <code>.unmute</code>")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "unmute")))
async def b_unmute(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    removed = await clear_muted(owner_id, message.chat.id)
    await reply_business(message, "🔊 Mute выключен." if removed else "Mute не был включён.")

def _replied_text(message):
    if message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text
    return cmd_arg(message.text) or None

def _is_format_cmd(t):
    if not t or not t.startswith("."): return False
    return t.strip().split(maxsplit=1)[0][1:].lower() in FORMAT_COMMANDS

@dp.business_message(F.text.func(_is_format_cmd))
async def b_format(message: Message):
    name = message.text.strip().split(maxsplit=1)[0][1:].lower()
    text = _replied_text(message)
    if not text:
        await reply_business(message, f"Использование: .{name} текст (или ответом)"); return
    await reply_business(message, FORMAT_COMMANDS[name](text))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "dice")))
async def b_dice(message: Message):
    if not message.business_connection_id: return
    try:
        await _bot.send_dice(chat_id=message.chat.id, emoji="🎲",
                             business_connection_id=message.business_connection_id)
    except Exception as e: logger.error(f"send_dice: {e}")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "flip")))
async def b_flip(message: Message):
    await reply_business(message, coinflip())

@dp.business_message(F.text.func(lambda t: is_cmd(t, "ttt")))
async def b_ttt(message: Message):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение соперника."); return
    p1 = message.from_user; p2 = message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id:
        await reply_business(message, "Нужен второй живой игрок."); return
    if not message.business_connection_id: return
    sent = await _bot.send_message(chat_id=message.chat.id,
        text=f"❌ {html.escape(p1.full_name)} vs ⭕ {html.escape(p2.full_name)}\nХодит: {html.escape(p1.full_name)}",
        business_connection_id=message.business_connection_id)
    _ttt_games[sent.message_id] = {"board": [""] * 9, "turn": "X", "players": {p1.id: "X", p2.id: "O"}}
    await _bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=sent.message_id,
        reply_markup=ttt_keyboard(_ttt_games[sent.message_id]["board"], sent.message_id),
        business_connection_id=message.business_connection_id)

@dp.callback_query(F.data.startswith("ttt:"))
async def ttt_move(callback: CallbackQuery):
    _, msg_id_s, idx_s = callback.data.split(":")
    msg_id, idx = int(msg_id_s), int(idx_s)
    game = _ttt_games.get(msg_id)
    if not game: await callback.answer("Игра закончена.", show_alert=True); return
    sym = game["players"].get(callback.from_user.id)
    if not sym: await callback.answer("Ты не участник.", show_alert=True); return
    if sym != game["turn"]: await callback.answer("Не твой ход.", show_alert=True); return
    if game["board"][idx]: await callback.answer("Занято.", show_alert=True); return
    game["board"][idx] = sym
    winner = ttt_winner(game["board"])
    bc_id = callback.message.business_connection_id
    if winner:
        _ttt_games.pop(msg_id, None)
        text = "Ничья!" if winner == "draw" else f"Победил {sym}: {html.escape(callback.from_user.full_name)}!"
        if bc_id:
            await _bot.edit_message_text(chat_id=callback.message.chat.id,
                message_id=callback.message.message_id, text=text, business_connection_id=bc_id)
        await callback.answer(); return
    game["turn"] = "O" if game["turn"] == "X" else "X"
    if bc_id:
        await _bot.edit_message_reply_markup(chat_id=callback.message.chat.id,
            message_id=callback.message.message_id, reply_markup=ttt_keyboard(game["board"], msg_id),
            business_connection_id=bc_id)
    await callback.answer()

@dp.business_message(F.text.func(lambda t: is_cmd(t, "bw")))
async def b_bw(message: Message):
    if not message.business_connection_id: return
    arg = cmd_arg(message.text).strip()
    size = int(arg) if arg.isdigit() and 2 <= int(arg) <= 6 else 4
    sent = await _bot.send_message(chat_id=message.chat.id, text=f"Закрась всё поле {size}x{size}!",
                                   business_connection_id=message.business_connection_id)
    _bw_games[sent.message_id] = {"board": [False] * (size * size), "size": size}
    await _bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=sent.message_id,
        reply_markup=bw_keyboard(_bw_games[sent.message_id], sent.message_id),
        business_connection_id=message.business_connection_id)

@dp.callback_query(F.data.startswith("bw:"))
async def bw_move(callback: CallbackQuery):
    _, msg_id_s, idx_s = callback.data.split(":")
    msg_id, idx = int(msg_id_s), int(idx_s)
    game = _bw_games.get(msg_id)
    if not game: await callback.answer("Игра закончена.", show_alert=True); return
    game["board"][idx] = True
    bc_id = callback.message.business_connection_id
    if all(game["board"]):
        _bw_games.pop(msg_id, None)
        if bc_id:
            await _bot.edit_message_text(chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                text=f"🎉 Поле закрашено! ({html.escape(callback.from_user.full_name)})",
                business_connection_id=bc_id)
        await callback.answer(); return
    if bc_id:
        await _bot.edit_message_reply_markup(chat_id=callback.message.chat.id,
            message_id=callback.message.message_id, reply_markup=bw_keyboard(game, msg_id),
            business_connection_id=bc_id)
    await callback.answer()

@dp.business_message(F.text.func(lambda t: is_cmd(t, "lq")))
async def b_lq(message: Message):
    if not (message.reply_to_message and message.reply_to_message.photo):
        await reply_business(message, "Ответь этой командой на фото."); return
    if not message.business_connection_id: return
    photo = message.reply_to_message.photo[-1]
    f = await _bot.get_file(photo.file_id)
    buf = await _bot.download_file(f.file_path)
    result = degrade_image(buf.read())
    await _bot.send_photo(chat_id=message.chat.id, photo=BufferedInputFile(result, filename="lq.jpg"),
                          business_connection_id=message.business_connection_id)

@dp.business_message(F.text.func(lambda t: is_cmd(t, "gif")))
async def b_gif(message: Message):
    if not (message.reply_to_message and message.reply_to_message.photo):
        await reply_business(message, "Ответь этой командой на фото."); return
    if not message.business_connection_id: return
    photo = message.reply_to_message.photo[-1]
    f = await _bot.get_file(photo.file_id)
    buf = await _bot.download_file(f.file_path)
    result = photo_to_gif(buf.read())
    await _bot.send_animation(chat_id=message.chat.id, animation=BufferedInputFile(result, filename="out.gif"),
                              business_connection_id=message.business_connection_id)

@dp.business_message(F.text.func(lambda t: is_cmd(t, "get")))
async def b_get(message: Message):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение."); return
    if not message.business_connection_id: return
    owner_id = get_owner_by_connection(message.business_connection_id)
    r = message.reply_to_message; chat_id = message.chat.id; msg_id = r.message_id
    row = await find_deleted_by_message_id(owner_id, chat_id, msg_id)
    if not row:
        cached = await get_cached_message(owner_id, chat_id, msg_id)
        if cached:
            _, _, _, content_type, _, file_path = cached
            row = (None, None, content_type, file_path, None)
    if not row or not row[3] or not os.path.exists(row[3]):
        await reply_business(message, "Для этого сообщения нет сохранённого медиа."); return
    file_path = row[3]; content_type = row[2] or "document"
    bc_id = message.business_connection_id
    try:
        if content_type == "photo":
            await _bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "video":
            await _bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "voice":
            await _bot.send_voice(chat_id=chat_id, voice=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "audio":
            await _bot.send_audio(chat_id=chat_id, audio=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "animation":
            await _bot.send_animation(chat_id=chat_id, animation=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "sticker":
            await _bot.send_sticker(chat_id=chat_id, sticker=FSInputFile(file_path), business_connection_id=bc_id)
        else:
            await _bot.send_document(chat_id=chat_id, document=FSInputFile(file_path), business_connection_id=bc_id)
    except Exception as e:
        logger.error(f"cmd_get: {e}")
        await reply_business(message, f"Не удалось отправить: {html.escape(str(e))}")

@dp.business_message(F.text.func(lambda t: is_cmd(t, "short")))
async def b_short(message: Message):
    history = _recent_messages.get(message.chat.id, [])
    if not history:
        await reply_business(message, "Пока нечего пересказывать."); return
    summary = "\n".join(f"• {html.escape(line)}" for line in history[-10:])
    await reply_business(message, "<b>Последние сообщения:</b>\n" + summary)

@dp.business_message(F.text.func(lambda t: is_cmd(t, "deleted")))
async def b_deleted(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    feats = await features_for(owner_id)
    if not feats["history"]:
        await reply_business(message, "🔒 История доступна на 💎 PRO и 🏢 BUSINESS."); return
    rows = await get_last_deleted(owner_id, message.chat.id, limit=10)
    if not rows:
        await reply_business(message, "Удалённых не зафиксировано."); return
    lines = ["<b>Последние удалённые:</b>"]
    for name, text, ctype, fpath, at in rows:
        mark = " 📎" if fpath and os.path.exists(fpath) else ""
        lines.append(f"• <b>{html.escape(name or 'unknown')}</b> [{at[:19]}] ({ctype}){mark}: {html.escape(text or '')}")
    await reply_business(message, "\n".join(lines))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "edited")))
async def b_edited(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    feats = await features_for(owner_id)
    if not feats["history"]:
        await reply_business(message, "🔒 История доступна на 💎 PRO и 🏢 BUSINESS."); return
    rows = await get_last_edited(owner_id, message.chat.id, limit=10)
    if not rows:
        await reply_business(message, "Изменённых не зафиксировано."); return
    lines = ["<b>Последние изменённые:</b>"]
    for name, old, new, at in rows:
        lines.append(f"• <b>{html.escape(name or 'unknown')}</b> [{at[:19]}]\n  было: <i>{html.escape(old or '')}</i>\n  стало: <i>{html.escape(new or '')}</i>")
    await reply_business(message, "\n".join(lines))

@dp.business_message(F.text.func(lambda t: is_cmd(t, "edits")))
async def b_edits(message: Message):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение."); return
    owner_id = get_owner_by_connection(message.business_connection_id)
    feats = await features_for(owner_id)
    if not feats["history"]:
        await reply_business(message, "🔒 История доступна на 💎 PRO и 🏢 BUSINESS."); return
    rows = await get_edit_history(owner_id, message.chat.id, message.reply_to_message.message_id)
    if not rows:
        await reply_business(message, "Нет истории правок."); return
    lines = ["<b>История правок:</b>"]
    for version, text, at in rows: lines.append(f"v{version} [{at[:19]}]: <i>{html.escape(text or '')}</i>")
    await reply_business(message, "\n".join(lines))

@dp.business_message()
async def b_mute_filter(message: Message):
    if not message.business_connection_id: return
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id: return
    feats = await features_for(owner_id)
    if not feats["mute"]: return
    if not await is_muted(owner_id, message.chat.id): return
    if message.from_user and message.from_user.id == owner_id: return
    try:
        await _bot.delete_business_messages(business_connection_id=message.business_connection_id,
                                            message_ids=[message.message_id])
    except Exception as e:
        logger.warning(f"[mute] {message.message_id}: {e}")

@dp.message(CommandStart())
async def pm_start(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    is_adm = is_admin(message.from_user.id)
    text = (f"👋 Привет, <b>{html.escape(message.from_user.full_name)}</b>!\n\n"
            "Я — бот для <b>Telegram Business</b>.\n"
            "• 🗑 удаления собеседника\n• ✏️ правки\n• 📸 view-once\n\n"
            "<b>У каждого владельца — свои уведомления.</b>\nВыбери действие:")
    await message.answer(text, reply_markup=main_menu_kb(is_adm))

@dp.message(Command("menu"))
async def pm_menu(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer("Главное меню:", reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(Command("help"))
async def pm_help(message: Message):
    await message.answer(
        "Business (в чатах) — команды через точку: <code>.help</code>\n"
        "Личка — меню подписки.\n\n/menu /me /plans /admin")

@dp.message(Command("me"))
async def pm_me(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    row = await get_user(message.from_user.id)
    await message.answer(_profile_text(row), reply_markup=back_kb())

@dp.message(Command("plans"))
async def pm_plans(message: Message):
    await message.answer(_plans_text(), reply_markup=plans_kb())

def _profile_text(row):
    if not row: return "Профиль не найден."
    plan = active_plan(row)
    plan_title = PLANS.get(plan, DEFAULT_PLANS["free"])["title"]
    until = row["plan_until"] or "—"
    conns = sum(1 for info in _connections.values() if info["owner_id"] == row["user_id"])
    return (f"👤 <b>Профиль</b>\n\nID: <code>{row['user_id']}</code>\n"
            f"Имя: {html.escape(row['full_name'] or '—')}\n"
            f"Тариф: <b>{plan_title}</b>\nДействует до: <code>{until}</code>\n"
            f"Активных подключений: <b>{conns}</b>\n")

def _plans_text():
    lines = ["💎 <b>Тарифы</b>\n"]
    for key in ("free", "pro", "business"):
        p = PLANS.get(key) or DEFAULT_PLANS[key]
        price = "бесплатно" if p["price"] == 0 else f"{p['price']}₽ / {p['days']} дн."
        feats = p["features"]
        lines.append(f"<b>{p['title']}</b> — {price}")
        lines.append(f"  • удаления: {'✅' if feats['notify_deleted'] else '❌'}")
        lines.append(f"  • правки: {'✅' if feats['notify_edited'] else '❌'}")
        lines.append(f"  • view-once: {'✅' if feats['notify_view_once'] else '❌'}")
        lines.append(f"  • медиа: {'✅' if feats['save_media'] else '❌'}")
        lines.append(f"  • mute: {'✅' if feats['mute'] else '❌'}")
        lines.append(f"  • история: {'✅' if feats['history'] else '❌'}")
        lines.append("")
    lines.append("<i>Оплата — напиши админу.</i>")
    return "\n".join(lines)

@dp.callback_query(F.data.startswith("menu:"))
async def menu_cb(cb: CallbackQuery):
    action = cb.data.split(":", 1)[1]
    if action == "root":
        await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb(is_admin(cb.from_user.id)))
    elif action == "profile":
        row = await get_user(cb.from_user.id)
        await cb.message.edit_text(_profile_text(row), reply_markup=back_kb())
    elif action == "plans":
        await cb.message.edit_text(_plans_text(), reply_markup=plans_kb())
    elif action == "howto":
        await cb.message.edit_text(
            "📖 <b>Как подключить</b>\n\n1. Настройки → Telegram Business\n"
            "2. Чат-боты → найди бота\n3. Разреши отвечать и удалять\n"
            "4. Готово! Уведомления приходят в личку.\n\n"
            "<i>Команды в чате: .help, .diag, .deleted...</i>", reply_markup=back_kb())
    elif action == "mystats":
        owner_id = cb.from_user.id
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM deleted_messages WHERE owner_id=?", (owner_id,))
            d = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM edited_messages WHERE owner_id=?", (owner_id,))
            e = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM connections WHERE owner_id=?", (owner_id,))
            c = (await cur.fetchone())[0]
        await cb.message.edit_text(
            f"📊 <b>Твоя статистика</b>\n\nПодключений: <b>{c}</b>\n"
            f"Удалений: <b>{d}</b>\nПравок: <b>{e}</b>\n", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_cb(cb: CallbackQuery):
    plan = cb.data.split(":", 1)[1]
    p = PLANS.get(plan)
    if not p: await cb.answer("Неизвестный тариф.", show_alert=True); return
    adm = next(iter(ADMIN_IDS)) if ADMIN_IDS else None
    hint = f"Напиши админу: <code>{adm}</code>" if adm else "Напиши админу."
    await cb.message.answer(f"💳 <b>{p['title']}</b> — {p['price']}₽ / {p['days']} дн.\n\n{hint}")
    await cb.answer()

@dp.message(Command("admin"))
async def pm_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа."); return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_root_kb())

@dp.callback_query(F.data.startswith("admin:"))
async def admin_cb(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа.", show_alert=True); return
    parts = cb.data.split(":"); action = parts[1]

    if action == "root":
        await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_root_kb())

    elif action == "stats":
        s = await stats_summary(); plans = s["plans"]
        text = (f"📊 <b>Статистика</b>\n\n👥 Юзеров: <b>{s['users']}</b>\n"
                f"🔌 Подключений: <b>{s['connections']}</b>\n"
                f"🗑 Удалений: <b>{s['deleted']}</b>\n"
                f"✏️ Правок: <b>{s['edited']}</b>\n"
                f"💳 Платежей: <b>{s['payments']}</b>\n"
                f"💰 Выручка: <b>{s['revenue']}₽</b>\n\n"
                f"<b>По тарифам:</b>\n  free: {plans.get('free', 0)}\n"
                f"  pro: {plans.get('pro', 0)}\n  business: {plans.get('business', 0)}")
        await cb.message.edit_text(text, reply_markup=back_kb("admin:root"))

    elif action == "users":
        offset = int(parts[2]) if len(parts) > 2 else 0
        users = await list_users(limit=10, offset=offset)
        total = await count_users()
        lines = [f"👥 <b>Юзеры</b> ({offset + 1}–{offset + len(users)} из {total})\n"]
        kb_rows = []
        for u in users:
            plan = active_plan(u); ban = " 🚫" if u["is_banned"] else ""
            lines.append(f"• <code>{u['user_id']}</code> {html.escape(u['full_name'] or '—')} [{plan}]{ban}")
            kb_rows.append([InlineKeyboardButton(text=f"{u['user_id']} — {plan}",
                                                 callback_data=f"admin:user:{u['user_id']}")])
        nav = []
        if offset > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{max(0, offset - 10)}"))
        if offset + 10 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{offset + 10}"))
        if nav: kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:root")])
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

    elif action == "user":
        uid = int(parts[2]); row = await get_user(uid)
        if not row: await cb.answer("Юзер не найден.", show_alert=True); return
        conns = [cid for cid, info in _connections.items() if info["owner_id"] == uid]
        text = (f"👤 <b>Юзер</b>\n\nID: <code>{uid}</code>\n"
                f"Имя: {html.escape(row['full_name'] or '—')}\n"
                f"Username: @{row['username'] or '—'}\n"
                f"Тариф: <b>{active_plan(row)}</b>\n"
                f"До: <code>{row['plan_until'] or '—'}</code>\n"
                f"Забанен: <b>{'да' if row['is_banned'] else 'нет'}</b>\n"
                f"Подключений: <b>{len(conns)}</b>")
        await cb.message.edit_text(text, reply_markup=admin_user_kb(uid))

    elif action == "grant":
        uid = int(parts[2]); plan = parts[3]
        if plan == "free":
            await set_user_plan(uid, "free", 0); await cb.answer("Сброшено на free.")
        else:
            p = PLANS.get(plan)
            if not p: await cb.answer("Неизвестный тариф.", show_alert=True); return
            await set_user_plan(uid, plan, p["days"])
            await log_payment(uid, plan, p["price"], p["days"], note=f"granted by {cb.from_user.id}")
            await cb.answer(f"Выдан {plan} на {p['days']} дн.")
            try:
                await _bot.send_message(uid, f"🎉 Выдан тариф <b>{p['title']}</b> на {p['days']} дней!\n/me")
            except Exception: pass
        row = await get_user(uid)
        await cb.message.edit_text(
            f"👤 <b>Юзер</b> <code>{uid}</code>\nТариф: <b>{active_plan(row)}</b>\n"
            f"До: <code>{row['plan_until'] or '—'}</code>", reply_markup=admin_user_kb(uid))

    elif action == "ban":
        uid = int(parts[2]); row = await get_user(uid)
        new_state = not bool(row["is_banned"])
        await ban_user(uid, new_state)
        await cb.answer("Забанен." if new_state else "Разбанен.")
        row = await get_user(uid)
        await cb.message.edit_text(f"👤 <code>{uid}</code>\nЗабанен: <b>{'да' if row['is_banned'] else 'нет'}</b>",
                                   reply_markup=admin_user_kb(uid))

    elif action == "find":
        _pending_admin_input[cb.from_user.id] = {"type": "find"}
        await cb.message.edit_text("🔎 Отправь ID юзера или <code>cancel</code>.", reply_markup=back_kb("admin:root"))

    elif action == "broadcast":
        _pending_admin_input[cb.from_user.id] = {"type": "broadcast"}
        await cb.message.edit_text("📣 Отправь текст рассылки или <code>cancel</code>.", reply_markup=back_kb("admin:root"))

    elif action == "plans":
        text = "💰 <b>Управление тарифами</b>\n\n"
        for key in ("free", "pro", "business"):
            p = PLANS.get(key) or DEFAULT_PLANS[key]
            price = "бесплатно" if p["price"] == 0 else f"{p['price']}₽"
            text += f"<b>{p['title']}</b> (<code>{key}</code>): {price} / {p['days']} дн.\n"
        text += "\n<i>Цены и сроки сохраняются в БД.</i>"
        await cb.message.edit_text(text, reply_markup=admin_plans_kb())

    elif action == "editplan":
        key = parts[2]; p = PLANS.get(key)
        if not p: await cb.answer("Нет такого тарифа.", show_alert=True); return
        _pending_admin_input[cb.from_user.id] = {"type": "editplan", "key": key}
        await cb.message.edit_text(
            f"✏️ <b>{p['title']}</b>\n\nЦена: <b>{p['price']}₽</b>\nСрок: <b>{p['days']} дн.</b>\n\n"
            f"Отправь: <code>ЦЕНА СРОК</code>, например <code>499 30</code>\n"
            f"Или <code>cancel</code>.", reply_markup=back_kb("admin:plans"))

    elif action == "resetplan":
        key = parts[2]
        await reset_plan(key)
        await cb.answer(f"{key} сброшен.")
        text = "💰 <b>Управление тарифами</b>\n\n"
        for k in ("free", "pro", "business"):
            pp = PLANS.get(k) or DEFAULT_PLANS[k]
            price = "бесплатно" if pp["price"] == 0 else f"{pp['price']}₽"
            text += f"<b>{pp['title']}</b> (<code>{k}</code>): {price} / {pp['days']} дн.\n"
        await cb.message.edit_text(text, reply_markup=admin_plans_kb())

    await cb.answer()

@dp.message(F.text & ~F.text.startswith("/"))
async def admin_text_input(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    state = _pending_admin_input.pop(message.from_user.id, None)
    if not state: return
    text = (message.text or "").strip()
    if text.lower() == "cancel":
        await message.answer("Отменено.", reply_markup=admin_root_kb()); return

    if state["type"] == "editplan":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await message.answer("❌ Формат: <code>ЦЕНА СРОК</code>, например <code>499 30</code>")
            _pending_admin_input[message.from_user.id] = state; return
        price, days = int(parts[0]), int(parts[1]); key = state["key"]
        await update_plan(key, price=price, days=days)
        p = PLANS.get(key, {})
        await message.answer(f"✅ <b>{p.get('title', key)}</b>: {p['price']}₽ / {p['days']} дн.",
                             reply_markup=admin_plans_kb()); return

    if state["type"] == "find":
        if not text.isdigit():
            await message.answer("❌ Нужен числовой ID.")
            _pending_admin_input[message.from_user.id] = state; return
        uid = int(text); row = await get_user(uid)
        if not row: await message.answer("Не найден.", reply_markup=admin_root_kb()); return
        conns = [cid for cid, info in _connections.items() if info["owner_id"] == uid]
        await message.answer(
            f"👤 <code>{uid}</code>\nИмя: {html.escape(row['full_name'] or '—')}\n"
            f"Тариф: <b>{active_plan(row)}</b>\nДо: <code>{row['plan_until'] or '—'}</code>\n"
            f"Забанен: <b>{'да' if row['is_banned'] else 'нет'}</b>\n"
            f"Подключений: <b>{len(conns)}</b>", reply_markup=admin_user_kb(uid)); return

    if state["type"] == "broadcast":
        if not text: await message.answer("Пустое сообщение."); return
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id FROM users WHERE is_banned=0")
            ids = [r[0] for r in await cur.fetchall()]
        ok, fail = 0, 0
        await message.answer(f"📣 Рассылаю {len(ids)} юзерам...")
        for uid in ids:
            try:
                await _bot.send_message(uid, f"📣 <b>Рассылка</b>\n\n{html.escape(text)}")
                ok += 1; await asyncio.sleep(0.05)
            except Exception: fail += 1
        await message.answer(f"Готово: ✅ {ok}, ❌ {fail}", reply_markup=admin_root_kb()); return

@dp.errors()
async def on_dispatcher_error(event):
    logger.error(f"[dispatcher] {event.exception!r}")
    return True

async def set_commands(bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="me", description="Профиль"),
            BotCommand(command="plans", description="Тарифы"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="admin", description="Админ-панель"),
        ], scope=BotCommandScopeDefault())
    except Exception as e: logger.warning(f"set_commands: {e}")

async def main():
    global _bot
    if not BOT_TOKEN: raise SystemExit("Set BOT_TOKEN environment variable")
    await init_db()
    await init_plans()
    await load_state()
    logger.info(f"DB_PATH={DB_PATH}")
    logger.info(f"MEDIA_DIR={MEDIA_DIR}")
    logger.info(f"ADMIN_IDS={ADMIN_IDS}")
    _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await set_commands(_bot)
    asyncio.create_task(media_cleanup_loop())
    restart_delay = 5
    while True:
        try:
            logger.info("Starting polling...")
            await dp.start_polling(_bot); break
        except Exception as e:
            logger.error(f"[main] polling crashed: {e!r}")
            await asyncio.sleep(restart_delay)

if __name__ == "__main__":
    asyncio.run(main())
