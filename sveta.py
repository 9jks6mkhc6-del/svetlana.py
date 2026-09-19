"""Telegram-Business бот. Business + подписки + админка + Stars + view-once + промокоды + рефералка + игры + аниме + игнор."""
import asyncio, html, io, json, logging, os, random, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import aiosqlite
from PIL import Image
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import (Message, CallbackQuery, BufferedInputFile, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, BusinessConnection, BusinessMessagesDeleted,
    BotCommand, BotCommandScopeDefault, LabeledPrice, PreCheckoutQuery, SuccessfulPayment)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
try: DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception: DATA_DIR = Path(".")
DB_PATH = str(DATA_DIR / "bot.db")
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_TTL_DAYS = int(os.environ.get("MEDIA_TTL_DAYS", "30"))
STARS_PER_RUB = 1
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
PLANS = {k: dict(v) for k, v in DEFAULT_PLANS.items()}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    plan TEXT DEFAULT 'free', plan_until TEXT, is_banned INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0,
    created_at TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL,
    days INTEGER NOT NULL, features TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS connections (connection_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
    can_reply INTEGER DEFAULT 0, can_delete INTEGER, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_conn_owner ON connections(owner_id);
CREATE TABLE IF NOT EXISTS chat_owners (chat_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_chatowner_owner ON chat_owners(owner_id);
CREATE TABLE IF NOT EXISTS afk (owner_id INTEGER PRIMARY KEY, reason TEXT, since TEXT);
CREATE TABLE IF NOT EXISTS status (owner_id INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS message_cache (owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, text TEXT, content_type TEXT,
    file_id TEXT, file_path TEXT, created_at TEXT, is_view_once INTEGER DEFAULT 0,
    PRIMARY KEY (owner_id, chat_id, message_id));
CREATE TABLE IF NOT EXISTS deleted_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT, text TEXT,
    content_type TEXT, file_path TEXT, deleted_at TEXT);
CREATE INDEX IF NOT EXISTS idx_deleted_own ON deleted_messages(owner_id, chat_id);
CREATE TABLE IF NOT EXISTS edited_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER, user_name TEXT,
    old_text TEXT, new_text TEXT, edited_at TEXT);
CREATE INDEX IF NOT EXISTS idx_edited_own ON edited_messages(owner_id, chat_id);
CREATE TABLE IF NOT EXISTS edit_history (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, version INTEGER NOT NULL, user_id INTEGER,
    user_name TEXT, text TEXT, edited_at TEXT);
CREATE INDEX IF NOT EXISTS idx_history_own ON edit_history(owner_id, chat_id, message_id);
CREATE TABLE IF NOT EXISTS muted_chats (owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    muted_at TEXT, PRIMARY KEY (owner_id, chat_id));
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    plan TEXT NOT NULL, amount INTEGER, days INTEGER, paid_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, discount INTEGER NOT NULL,
    days_alive INTEGER NOT NULL, created_at TEXT, until TEXT, uses_left INTEGER DEFAULT -1);
CREATE TABLE IF NOT EXISTS used_promos (user_id INTEGER, code TEXT, used_at TEXT,
    PRIMARY KEY (user_id, code));
CREATE TABLE IF NOT EXISTS referrals (referrer_id INTEGER NOT NULL, referred_id INTEGER PRIMARY KEY,
    created_at TEXT, paid INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);
CREATE TABLE IF NOT EXISTS ignored_chats (owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
    mode TEXT NOT NULL, added_at TEXT, PRIMARY KEY (owner_id, chat_id));
CREATE TABLE IF NOT EXISTS ignore_mode (owner_id INTEGER PRIMARY KEY, whitelist_mode INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pay_attempts (user_id INTEGER PRIMARY KEY, cancels INTEGER DEFAULT 0,
    blocked_until TEXT, last_attempt TEXT);
"""
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA); await db.commit()
async def init_plans():
    global PLANS
    async with aiosqlite.connect(DB_PATH) as db:
        for key, p in DEFAULT_PLANS.items():
            await db.execute("INSERT OR IGNORE INTO plans (key, title, price, days, features, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, p["title"], p["price"], p["days"], json.dumps(p["features"]), datetime.now(timezone.utc).isoformat()))
        await db.commit(); await _reload_plans(db)
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
        fields.append("updated_at=?"); values.append(datetime.now(timezone.utc).isoformat()); values.append(key)
        await db.execute(f"UPDATE plans SET {', '.join(fields)} WHERE key=?", values)
        await db.commit(); await _reload_plans(db)
async def reset_plan(key):
    p = DEFAULT_PLANS.get(key)
    if not p: return
    await update_plan(key, price=p["price"], days=p["days"], title=p["title"])
async def upsert_user(user_id, username=None, full_name=None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (user_id, username, full_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?) "
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
        cur = await db.execute("SELECT COUNT(*) FROM users"); return (await cur.fetchone())[0]
async def stats_summary():
    async with aiosqlite.connect(DB_PATH) as db:
        out = {}
        cur = await db.execute("SELECT COUNT(*) FROM users"); out["users"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan"); out["plans"] = {p: n for p, n in await cur.fetchall()}
        cur = await db.execute("SELECT COUNT(*) FROM connections"); out["connections"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM deleted_messages"); out["deleted"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM edited_messages"); out["edited"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM payments"); out["payments"] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COALESCE(SUM(amount),0) FROM payments"); out["revenue"] = (await cur.fetchone())[0]
        return out
async def set_user_plan(user_id, plan, days):
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET plan=?, plan_until=? WHERE user_id=?", (plan, until, user_id)); await db.commit()
async def ban_user(user_id, banned=True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id)); await db.commit()
async def log_payment(user_id, plan, amount, days, note=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments (user_id, plan, amount, days, paid_at, note) VALUES (?, ?, ?, ?, ?, ?)",
                         (user_id, plan, amount, days, datetime.now(timezone.utc).isoformat(), note)); await db.commit()
async def get_payments(limit=15, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return await cur.fetchall()
def stars_price(rub): return max(1, int(rub * STARS_PER_RUB))
async def grant_plan(user_id, plan_key, note=""):
    p = PLANS.get(plan_key)
    if not p: return
    await set_user_plan(user_id, plan_key, p["days"])
    await log_payment(user_id, plan_key, p["price"], p["days"], note=note)
    try:
        await _bot.send_message(user_id, f"🎉 <b>Оплата получена!</b>\n\nТариф: <b>{p['title']}</b>\nСрок: <b>{p['days']} дн.</b>\n\nПроверить: /me")
    except Exception: pass
def active_plan(user_row):
    if not user_row: return "free"
    try:
        plan = user_row["plan"] or "free"; until = user_row["plan_until"]
    except Exception: return "free"
    if plan != "free" and until:
        try:
            if datetime.fromisoformat(until) < datetime.now(timezone.utc): return "free"
        except Exception: return "free"
    return plan
async def features_for(owner_id):
    row = await get_user(owner_id); return PLANS.get(active_plan(row), DEFAULT_PLANS["free"])["features"]
async def save_connection(conn_id, info):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO connections (connection_id, owner_id, can_reply, can_delete, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id) DO UPDATE SET owner_id=excluded.owner_id, can_reply=excluded.can_reply, "
            "can_delete=excluded.can_delete, updated_at=excluded.updated_at",
            (conn_id, info["owner_id"], int(info["can_reply"]),
             None if info["can_delete"] is None else int(info["can_delete"]), datetime.now(timezone.utc).isoformat()))
        await db.commit()
async def delete_connection(conn_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM connections WHERE connection_id=?", (conn_id,)); await db.commit()
async def load_all_connections():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT connection_id, owner_id, can_reply, can_delete FROM connections")
        out = {}
        for cid, oid, cr, cd in await cur.fetchall():
            out[cid] = {"owner_id": oid, "can_reply": bool(cr), "can_delete": None if cd is None else bool(cd)}
        return out
async def remember_chat_owner(chat_id, owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO chat_owners (chat_id, owner_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET owner_id=excluded.owner_id, updated_at=excluded.updated_at",
            (chat_id, owner_id, datetime.now(timezone.utc).isoformat())); await db.commit()
async def load_chat_owner(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT owner_id FROM chat_owners WHERE chat_id=?", (chat_id,)); row = await cur.fetchone()
        return row[0] if row else None
async def load_all_chat_owners():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id, owner_id FROM chat_owners"); return {cid: oid for cid, oid in await cur.fetchall()}
async def set_afk(owner_id, reason, since_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO afk (owner_id, reason, since) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET reason=excluded.reason, since=excluded.since", (owner_id, reason, since_iso))
        await db.commit()
async def clear_afk(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM afk WHERE owner_id=?", (owner_id,)); await db.commit(); return cur.rowcount > 0
async def set_status(owner_id, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO status (owner_id, text) VALUES (?, ?) ON CONFLICT(owner_id) DO UPDATE SET text=excluded.text", (owner_id, text))
        await db.commit()
async def get_status(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT text FROM status WHERE owner_id=?", (owner_id,)); row = await cur.fetchone()
        return row[0] if row else None
async def cache_message(owner_id, chat_id, message_id, user_id, user_name, text, content_type="text", file_id=None, file_path=None, is_view_once=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO message_cache (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path, created_at, is_view_once) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id, chat_id, message_id) DO UPDATE SET "
            "text=excluded.text, file_id=excluded.file_id, file_path=COALESCE(excluded.file_path, message_cache.file_path), "
            "content_type=excluded.content_type, is_view_once=excluded.is_view_once",
            (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path, datetime.now(timezone.utc).isoformat(), is_view_once))
        await db.commit()
async def get_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, user_name, text, content_type, file_id, file_path, is_view_once FROM message_cache "
            "WHERE owner_id=? AND chat_id=? AND message_id=?", (owner_id, chat_id, message_id))
        return await cur.fetchone()
async def drop_cached_message(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM message_cache WHERE owner_id=? AND chat_id=? AND message_id=?", (owner_id, chat_id, message_id)); await db.commit()
async def log_deleted(owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO deleted_messages (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, text, content_type, file_path, datetime.now(timezone.utc).isoformat()))
        await db.commit()
async def get_last_deleted(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_name, text, content_type, file_path, deleted_at FROM deleted_messages "
            "WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?", (owner_id, chat_id, limit))
        return await cur.fetchall()
async def find_deleted_by_message_id(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_name, text, content_type, file_path, deleted_at FROM deleted_messages "
            "WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1", (owner_id, chat_id, message_id))
        return await cur.fetchone()
async def log_edited(owner_id, chat_id, message_id, user_id, user_name, old_text, new_text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO edited_messages (owner_id, chat_id, message_id, user_id, user_name, old_text, new_text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, user_id, user_name, old_text, new_text, datetime.now(timezone.utc).isoformat()))
        await db.commit()
async def get_last_edited(owner_id, chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_name, old_text, new_text, edited_at FROM edited_messages "
            "WHERE owner_id=? AND chat_id=? ORDER BY id DESC LIMIT ?", (owner_id, chat_id, limit))
        return await cur.fetchall()
async def add_edit_version(owner_id, chat_id, message_id, user_id, user_name, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(version), 0) FROM edit_history WHERE owner_id=? AND chat_id=? AND message_id=?",
                               (owner_id, chat_id, message_id))
        row = await cur.fetchone(); next_version = (row[0] or 0) + 1
        await db.execute("INSERT INTO edit_history (owner_id, chat_id, message_id, version, user_id, user_name, text, edited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, chat_id, message_id, next_version, user_id, user_name, text, datetime.now(timezone.utc).isoformat()))
        await db.commit(); return next_version
async def get_edit_history(owner_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT version, text, edited_at FROM edit_history WHERE owner_id=? AND chat_id=? AND message_id=? ORDER BY version ASC",
                               (owner_id, chat_id, message_id))
        return await cur.fetchall()
async def set_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO muted_chats (owner_id, chat_id, muted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id, chat_id) DO UPDATE SET muted_at=excluded.muted_at",
            (owner_id, chat_id, datetime.now(timezone.utc).isoformat())); await db.commit()
async def is_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        return await cur.fetchone() is not None
async def clear_muted(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM muted_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        await db.commit(); return cur.rowcount > 0
async def add_promo(code, discount, days_alive, uses_left=-1):
    until = (datetime.now(timezone.utc) + timedelta(days=days_alive)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO promocodes (code, discount, days_alive, created_at, until, uses_left) VALUES (?, ?, ?, ?, ?, ?)",
            (code.upper(), discount, days_alive, datetime.now(timezone.utc).isoformat(), until, uses_left))
        await db.commit()
async def get_promo(code):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promocodes WHERE code=?", (code.upper(),))
        return await cur.fetchone()
async def delete_promo(code):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM promocodes WHERE code=?", (code.upper(),))
        await db.commit(); return cur.rowcount > 0
async def list_promos():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promocodes ORDER BY created_at DESC")
        return await cur.fetchall()
async def mark_promo_used(user_id, code):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO used_promos (user_id, code, used_at) VALUES (?, ?, ?)",
                         (user_id, code.upper(), datetime.now(timezone.utc).isoformat()))
        await db.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code=? AND uses_left > 0", (code.upper(),))
        await db.commit()
async def was_promo_used(user_id, code):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM used_promos WHERE user_id=? AND code=?", (user_id, code.upper()))
        return await cur.fetchone() is not None
async def add_referral(referrer_id, referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
                         (referrer_id, referred_id, datetime.now(timezone.utc).isoformat()))
        await db.commit()
async def mark_referral_paid(referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE referrals SET paid=1 WHERE referred_id=? AND paid=0", (referred_id,))
        await db.commit(); return cur.rowcount
async def get_referrer(referred_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referrer_id FROM referrals WHERE referred_id=?", (referred_id,))
        row = await cur.fetchone(); return row[0] if row else None
async def get_ref_stats(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)); total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND paid=1", (user_id,)); paid = (await cur.fetchone())[0]
        return total, paid
async def get_ref_top(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referrer_id, COUNT(*) as c, SUM(paid) FROM referrals GROUP BY referrer_id ORDER BY c DESC LIMIT ?", (limit,))
        return await cur.fetchall()
async def add_ignored_chat(owner_id, chat_id, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO ignored_chats (owner_id, chat_id, mode, added_at) VALUES (?, ?, ?, ?)",
                         (owner_id, chat_id, mode, datetime.now(timezone.utc).isoformat()))
        await db.commit()
async def remove_ignored_chat(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM ignored_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        await db.commit(); return cur.rowcount > 0
async def get_chat_ignore_mode(owner_id, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT mode FROM ignored_chats WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        row = await cur.fetchone(); return row[0] if row else None
async def get_ignore_mode(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT whitelist_mode FROM ignore_mode WHERE owner_id=?", (owner_id,))
        row = await cur.fetchone(); return bool(row[0]) if row else False
async def set_ignore_mode(owner_id, whitelist_mode):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO ignore_mode (owner_id, whitelist_mode) VALUES (?, ?)",
                         (owner_id, 1 if whitelist_mode else 0)); await db.commit()
async def is_pay_blocked(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT blocked_until FROM pay_attempts WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or not row[0]: return None
        try:
            until = datetime.fromisoformat(row[0])
            if until > datetime.now(timezone.utc): return until
        except Exception: pass
        return None
async def reset_pay_cancels(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pay_attempts WHERE user_id=?", (user_id,)); await db.commit()
dp = Dispatcher()
_bot = None
_connections = {}
_chat_owners = {}
_recent_messages = {}
MAX_HISTORY = 50
_pending_admin_input = {}
_hangman_games = {}
_quiz_games = {}
_guess_games = {}
_ttt_games = {}
_bw_games = {}
def get_owner_by_connection(connection_id):
    if not connection_id: return None
    info = _connections.get(connection_id); return info["owner_id"] if info else None
def get_owner_by_chat(chat_id): return _chat_owners.get(chat_id)
def has_active_connection(owner_id):
    if not owner_id: return False
    for info in _connections.values():
        if info.get("owner_id") == owner_id: return True
    return False
async def is_connected(owner_id):
    if not owner_id: return False
    if has_active_connection(owner_id): return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM connections WHERE owner_id=? LIMIT 1", (owner_id,))
        return await cur.fetchone() is not None
async def load_state():
    global _connections, _chat_owners
    raw = await load_all_connections()
    _chat_owners = await load_all_chat_owners()
    valid = {}
    for conn_id, info in raw.items():
        try:
            bc = await _bot.get_business_connection(conn_id)
            if bc and bc.is_enabled: valid[conn_id] = info
            else: await delete_connection(conn_id)
        except Exception: await delete_connection(conn_id)
    _connections = valid
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
def is_admin(user_id): return user_id in ADMIN_IDS
async def is_chat_ignored(owner_id, chat_id):
    mode = await get_chat_ignore_mode(owner_id, chat_id)
    whitelist_mode = await get_ignore_mode(owner_id)
    if whitelist_mode: return mode != "track"
    return mode == "ignore"
async def download_media(bot, file_id, content_type, owner_id, chat_id, message_id):
    try:
        file = await bot.get_file(file_id); ext = _ext_from_path(file.file_path, content_type)
        fname = f"{owner_id}_{chat_id}_{message_id}_{int(time.time())}{ext}"
        dest = MEDIA_DIR / fname; await bot.download_file(file.file_path, destination=dest); return str(dest)
    except Exception as e:
        logger.warning(f"[media:{owner_id}] {e}"); return None
async def save_view_once(bot, owner_id, message, file_id, content_type, user_name):
    if not owner_id: return None
    try:
        file = await bot.get_file(file_id); buf = await bot.download_file(file.file_path); data = buf.read()
        ext = _ext_from_path(file.file_path, content_type)
        fname = f"{owner_id}_{message.chat.id}_{message.message_id}_{int(time.time())}{ext}"
        dest = MEDIA_DIR / fname
        with open(dest, "wb") as f: f.write(data)
        return str(dest)
    except Exception as e:
        logger.error(f"[view_once:{owner_id}] {e}"); return None
async def cleanup_old_media():
    if MEDIA_TTL_DAYS <= 0: return
    cutoff_ts = time.time() - MEDIA_TTL_DAYS * 86400
    for p in MEDIA_DIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff_ts: p.unlink()
        except Exception: pass
async def media_cleanup_loop():
    while True:
        try: await cleanup_old_media()
        except Exception: pass
        await asyncio.sleep(3600)
async def subscription_loop():
    while True:
        try: await _check_subscriptions()
        except Exception as e: logger.warning(f"[subs] {e}")
        await asyncio.sleep(3600)
async def _check_subscriptions():
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id, plan, plan_until FROM users WHERE plan != 'free' AND plan_until IS NOT NULL")
        rows = await cur.fetchall()
    for row in rows:
        try:
            until = datetime.fromisoformat(row["plan_until"])
        except Exception: continue
        delta = until - now
        if delta.total_seconds() < 0:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET plan='free', plan_until=NULL WHERE user_id=?", (row["user_id"],)); await db.commit()
            continue
        days_left = delta.days
        if days_left == 3:
            try: await _bot.send_message(row["user_id"], f"⚠️ Подписка <b>{row['plan'].upper()}</b> заканчивается через 3 дня.\n/plans")
            except Exception: pass
        elif days_left == 0:
            try: await _bot.send_message(row["user_id"], f"⚠️ Подписка <b>{row['plan'].upper()}</b> сегодня заканчивается.\n/plans")
            except Exception: pass
async def notify_owner(owner_id, text, file_path=None, is_photo=False, is_video=False, _attempt=1):
    if not _bot or not owner_id: return
    if not await is_connected(owner_id): return
    try:
        if file_path and os.path.exists(file_path):
            if is_photo: await _bot.send_photo(owner_id, FSInputFile(file_path), caption=text)
            elif is_video:
                try: await _bot.send_video(owner_id, FSInputFile(file_path), caption=text)
                except Exception: await _bot.send_document(owner_id, FSInputFile(file_path), caption=text)
            else: await _bot.send_document(owner_id, FSInputFile(file_path), caption=text)
        else: await _bot.send_message(owner_id, text)
    except Exception as e:
        logger.error(f"[notify→{owner_id}] attempt {_attempt}: {e}")
        if _attempt < 3:
            await asyncio.sleep(1.5 * _attempt)
            await notify_owner(owner_id, text, file_path, is_photo, is_video, _attempt + 1)
async def reply_business(message, text, **kwargs):
    if not _bot or not message.business_connection_id: return
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    try:
        await _bot.send_message(chat_id=message.chat.id, text=text, business_connection_id=message.business_connection_id, **kwargs)
    except Exception as e: logger.error(f"[reply_business] {e}")
class OwnerOnlyCommandsMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not getattr(event, "business_connection_id", None): return await handler(event, data)
        owner_id = get_owner_by_connection(event.business_connection_id)
        if not owner_id: return await handler(event, data)
        if event.from_user and event.from_user.id == owner_id: return await handler(event, data)
        text = event.text or event.caption or ""
        if text.startswith("."):
            logger.info(f"[owner_only] blocked {event.from_user.id if event.from_user else '?'}: {text[:50]!r}"); return
        return await handler(event, data)
def is_cmd(text, name):
    if not text: return False
    return text.strip().split(maxsplit=1)[0].lower() == f".{name}"
def cmd_arg(text):
    parts = text.strip().split(maxsplit=1); return parts[1] if len(parts) > 1 else ""
LEET_MAP = str.maketrans({"a": "4", "A": "4", "e": "3", "E": "3", "i": "1", "I": "1",
                          "o": "0", "O": "0", "s": "5", "S": "5", "t": "7", "T": "7",
                          "b": "6", "B": "6", "а": "4", "А": "4", "е": "3", "Е": "3", "о": "0", "О": "0"})
KAWAII_SUFFIXES = ["Ꮚ˶ᐢ.ᐢ˶Ꮚ", "( ˶ˆ ᗜ ˆ˵ )", "•ᴗ•", "(๑>ᴗ<๑)", "uwu", "~"]
TSUNDERE_PREFIXES = ["Э-это не значит, что я рад, но... ", "Б-бака! ", "Не подумай ничего такого, но: "]
YANDERE_SUFFIXES = [" ...иначе я никому тебя не отдам.", " ...ты же будешь только моим?", " ня~ ♡ (это не угроза)"]
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
_RU = "йцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
def to_bold(t): return f"<b>{html.escape(t)}</b>"
def to_italic(t): return f"<i>{html.escape(t)}</i>"
def to_monospace(t): return f"<code>{html.escape(t)}</code>"
def to_underline(t): return f"<u>{html.escape(t)}</u>"
def to_leet(t): return t.translate(LEET_MAP)
def to_kawaii(t): return f"{t} {random.choice(KAWAII_SUFFIXES)}"
def to_tsundere(t): return f"{random.choice(TSUNDERE_PREFIXES)}{t}"
def to_yandere(t): return f"{t}{random.choice(YANDERE_SUFFIXES)}"
FORMAT_COMMANDS = {"bold": to_bold, "italic": to_italic, "monospace": to_monospace,
                   "underline": to_underline, "leet": to_leet, "kawaii": to_kawaii,
                   "tsundere": to_tsundere, "yandere": to_yandere}
def _replied_text(message):
    if message.reply_to_message and message.reply_to_message.text: return message.reply_to_message.text
    return cmd_arg(message.text) or None
def _is_format_cmd(t):
    if not t or not t.startswith("."): return False
    return t.strip().split(maxsplit=1)[0][1:].lower() in FORMAT_COMMANDS
HELP_BUSINESS = """<b>Команды</b>
<b>Утилиты:</b> .help .afk .status .time .sw .info .love .diag
<b>Формат:</b> .bold .italic .monospace .underline .leet .kawaii .tsundere .yandere
<b>Игры:</b> .dice .flip .ttt .bw .hangman .quiz .guess .duel .answer .try
<b>Аниме:</b> .hug .slap .pat .kiss
<b>Медиа:</b> .lq .get
<b>История:</b> .short .deleted .edited
<b>Модерация:</b> .mute .unmute
<b>Игнор:</b> .ignore .unignore .track .untrack .ignoremode
"""
ANIME_HUG = "🤗 {a} обнимает {b}"
ANIME_SLAP = "👋 {a} даёт леща {b}"
ANIME_PAT = "🫶 {a} гладит {b} по голове"
ANIME_KISS = "💋 {a} целует {b}"
QUIZ_QUESTIONS = [
    ("Сколько планет в Солнечной системе?", "8", ["7", "8", "9", "10"]),
    ("Кто написал «Войну и мир»?", "Толстой", ["Толстой", "Достоевский", "Пушкин", "Чехов"]),
    ("Столица Франции?", "Париж", ["Лондон", "Париж", "Берлин", "Рим"]),
    ("Сколько будет 7 * 8?", "56", ["48", "56", "64", "72"]),
    ("Газ в атмосфере Земли?", "Азот", ["Кислород", "Азот", "CO2", "Аргон"]),
]
WIN_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
async def anime_cmd(message, template, self_template):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if message.reply_to_message and message.reply_to_message.from_user:
        a = _user_name(message.from_user); b = _user_name(message.reply_to_message.from_user)
        await reply_business(message, template.format(a=html.escape(a), b=html.escape(b)))
    else:
        await reply_business(message, self_template)
async def start_hangman(message):
    words = ["программирование", "телеграм", "разработка", "интернет", "кодирование"]
    word = random.choice(words)
    _hangman_games[message.chat.id] = {"word": word, "guessed": set(), "tries": 6}
    masked = " ".join(c if c in " " else "_" for c in word)
    await reply_business(message, f"🎯 <b>Виселица</b>\n<code>{masked}</code>\nПопыток: 6\nПиши: <code>.try БУКВА</code>")
async def process_hangman_guess(message, letter):
    game = _hangman_games.get(message.chat.id)
    if not game: return
    if len(letter) != 1 or not letter.isalpha():
        await reply_business(message, "Нужна одна буква."); return
    letter = letter.lower()
    if letter in game["guessed"]:
        await reply_business(message, "Уже была."); return
    game["guessed"].add(letter)
    word = game["word"]
    if letter in word:
        masked = " ".join(c if c in game["guessed"] else "_" for c in word)
        if all(c in game["guessed"] for c in word):
            _hangman_games.pop(message.chat.id, None)
            await reply_business(message, f"🎉 Победа! <b>{word}</b>"); return
        await reply_business(message, f"✅ <code>{masked}</code>\nПопыток: {game['tries']}")
    else:
        game["tries"] -= 1
        if game["tries"] <= 0:
            _hangman_games.pop(message.chat.id, None)
            await reply_business(message, f"💀 Проигрыш. <b>{word}</b>"); return
        await reply_business(message, f"❌ Попыток: {game['tries']}")
def main_menu_kb(is_adm):
    rows = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
        [InlineKeyboardButton(text="💎 Тарифы", callback_data="menu:plans")],
        [InlineKeyboardButton(text="🎁 Пробный PRO", callback_data="menu:trial")],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="menu:ref")],
        [InlineKeyboardButton(text="📖 Как подключить", callback_data="menu:howto")],
    ]
    if is_adm: rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="admin:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def back_kb(target="menu:root"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]])
def plans_kb():
    rows = []
    for key in ("pro", "business"):
        p = PLANS.get(key)
        if p: rows.append([InlineKeyboardButton(text=f"{p['title']} — {p['price']}₽/{p['days']}дн", callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def admin_root_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="💰 Тарифы", callback_data="admin:plans")],
        [InlineKeyboardButton(text="📜 Платежи", callback_data="admin:payments:0")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin:promos")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="admin:refs")],
        [InlineKeyboardButton(text="🔎 Найти", callback_data="admin:find")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:root")],
    ])
def admin_user_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 PRO", callback_data=f"admin:grant:{uid}:pro")],
        [InlineKeyboardButton(text="🏢 BUSINESS", callback_data=f"admin:grant:{uid}:business")],
        [InlineKeyboardButton(text="🚫 free", callback_data=f"admin:grant:{uid}:free")],
        [InlineKeyboardButton(text="⛔ Бан/Разбан", callback_data=f"admin:ban:{uid}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users:0")],
    ])
def admin_plans_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ PRO", callback_data="admin:editplan:pro")],
        [InlineKeyboardButton(text="✏️ BUSINESS", callback_data="admin:editplan:business")],
        [InlineKeyboardButton(text="♻️ Сброс PRO", callback_data="admin:resetplan:pro")],
        [InlineKeyboardButton(text="♻️ Сброс BUSINESS", callback_data="admin:resetplan:business")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:root")],
    ])
@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    if connection.is_enabled:
        rights = getattr(connection, "rights", None); can_delete = None
        if rights is not None:
            can_delete = bool(getattr(rights, "can_delete_all_messages", False) or getattr(rights, "can_delete_sent_messages", False))
        info = {"owner_id": connection.user.id, "can_reply": bool(getattr(connection, "can_reply", False)), "can_delete": can_delete}
        _connections[connection.id] = info
        await save_connection(connection.id, info)
        await upsert_user(connection.user.id, connection.user.username, connection.user.full_name)
        try: await _bot.send_message(info["owner_id"], "✅ Бот подключён.\n.help в чате")
        except Exception: pass
    else:
        _connections.pop(connection.id, None)
        await delete_connection(connection.id)
class IncomingCacheMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        try: await self._cache_incoming(event, bot)
        except Exception as e: logger.error(f"[cache_mw] {getattr(event,'message_id','?')}: {e}")
        return await handler(event, data)
    @staticmethod
    async def _cache_incoming(message, bot):
        if not message.from_user: return
        owner_id = get_owner_by_connection(message.business_connection_id)
        if not owner_id: return
        if not await is_connected(owner_id): return
        _chat_owners[message.chat.id] = owner_id
        await remember_chat_owner(message.chat.id, owner_id)
        if message.from_user.id == owner_id: return
        if await is_chat_ignored(owner_id, message.chat.id): return
        feats = await features_for(owner_id)
        if message.text:
            await cache_message(owner_id, message.chat.id, message.message_id, message.from_user.id, _user_name(message.from_user), message.text, "text")
            if not message.text.startswith("."):
                hist = _recent_messages.setdefault(message.chat.id, [])
                hist.append(f"{message.from_user.first_name}: {message.text}")
                if len(hist) > MAX_HISTORY: del hist[0]
            return
        file_id, content_type = _media_file_id_and_type(message)
        if not file_id: return
        user_name = _user_name(message.from_user); caption = message.caption or f"<{content_type}>"
        view_once_path = None; is_vo = 0
        if _is_view_once(message) and feats["notify_view_once"]:
            view_once_path = await save_view_once(bot, owner_id, message, file_id, content_type, user_name)
            if view_once_path:
                is_vo = 1
                try: await _bot.send_message(owner_id, f"📸 <b>Одноразовое ({content_type})</b>\nОт: <b>{html.escape(user_name)}</b>\n<i>Ответь на сообщение в чате — пришлю файл.</i>")
                except Exception: pass
        if view_once_path: file_path = view_once_path
        elif feats["save_media"]: file_path = await download_media(bot, file_id, content_type, owner_id, message.chat.id, message.message_id)
        else: file_path = None
        await cache_message(owner_id, message.chat.id, message.message_id, message.from_user.id, user_name, caption, content_type, file_id, file_path, is_view_once=is_vo)
dp.business_message.outer_middleware(OwnerOnlyCommandsMiddleware())
dp.business_message.outer_middleware(IncomingCacheMiddleware())
@dp.edited_business_message()
async def on_edited(message: Message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if message.from_user and message.from_user.id == owner_id: return
    if await is_chat_ignored(owner_id, message.chat.id): return
    feats = await features_for(owner_id)
    if not feats["notify_edited"]: return
    if message.text:
        cached = await get_cached_message(owner_id, message.chat.id, message.message_id)
        old_text = cached[2] if cached else "<нет в кэше>"
        user_name = _user_name(message.from_user); uid = message.from_user.id if message.from_user else None
        await log_edited(owner_id, message.chat.id, message.message_id, uid, user_name, old_text, message.text)
        await add_edit_version(owner_id, message.chat.id, message.message_id, uid, user_name, message.text)
        await cache_message(owner_id, message.chat.id, message.message_id, uid, user_name, message.text, "text")
        await notify_owner(owner_id, f"✏️ <b>Изменено</b>\nЧат: <b>{html.escape(_chat_title(message))}</b>\nАвтор: <b>{html.escape(user_name)}</b>\nБыло: <i>{html.escape(old_text or '')}</i>\nСтало: <i>{html.escape(message.text)}</i>")
@dp.deleted_business_messages()
async def on_deleted_business(deleted: BusinessMessagesDeleted):
    chat_id = deleted.chat.id
    owner_id = get_owner_by_chat(chat_id) or await load_chat_owner(chat_id)
    if not owner_id or not await is_connected(owner_id): return
    if await is_chat_ignored(owner_id, chat_id): return
    feats = await features_for(owner_id)
    if not feats["notify_deleted"]: return
    chat_label = html.escape(_chat_title(deleted))
    for msg_id in deleted.message_ids:
        cached = await get_cached_message(owner_id, chat_id, msg_id)
        if not cached:
            await notify_owner(owner_id, f"🗑 <b>Удалено</b>\nЧат: <b>{chat_label}</b>\nmsg_id: {msg_id}"); continue
        user_id, user_name, text, content_type, file_id, file_path, is_vo = cached
        if user_id == owner_id:
            await drop_cached_message(owner_id, chat_id, msg_id); continue
        await log_deleted(owner_id, chat_id, msg_id, user_id, user_name, text, content_type, file_path)
        await drop_cached_message(owner_id, chat_id, msg_id)
        await notify_owner(owner_id, f"🗑 <b>Удалено</b>\nЧат: <b>{chat_label}</b>\nАвтор: <b>{html.escape(user_name or '?')}</b>\nТип: {content_type}\nТекст: <i>{html.escape(text or '')}</i>",
                          file_path=file_path, is_photo=(content_type=="photo"), is_video=(content_type in ("video","video_note","animation")))
@dp.business_message(CommandStart())
async def b_start(message): await reply_business(message, "Привет! .help")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "help")))
async def b_help(message): await reply_business(message, HELP_BUSINESS)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "diag")))
async def b_diag(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    row = await get_user(owner_id)
    lines = ["<b>Диагностика</b>", f"owner_id: <code>{owner_id}</code>", f"plan: <b>{active_plan(row)}</b>"]
    if row: lines.append(f"до: <code>{row['plan_until'] or '—'}</code>")
    feats = await features_for(owner_id)
    for k, v in feats.items(): lines.append(f"{k}: <b>{v}</b>")
    await reply_business(message, "\n".join(lines))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "info")))
async def b_info(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    u = message.from_user; status = await get_status(owner_id)
    lines = [f"<b>{u.full_name}</b>", f"id: <code>{u.id}</code>"]
    if u.username: lines.append(f"@{u.username}")
    if status: lines.append(f"status: {html.escape(status)}")
    await reply_business(message, "\n".join(lines))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "status")))
async def b_status(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    t = cmd_arg(message.text)
    if not t: await reply_business(message, ".status текст"); return
    await set_status(owner_id, t); await reply_business(message, "Ок")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "afk")))
async def b_afk(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    arg = cmd_arg(message.text).strip()
    if arg.lower() == "off":
        removed = await clear_afk(owner_id); await reply_business(message, "AFK снят." if removed else "Ты не был в AFK."); return
    reason = arg or "без причины"
    await set_afk(owner_id, reason, datetime.now(timezone.utc).isoformat()); await reply_business(message, f"AFK: {html.escape(reason)}")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "time")))
async def b_time(message): await reply_business(message, f"🕒 {datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S %d.%m.%Y')}")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "love")))
async def b_love(message): await reply_business(message, "❤️")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "sw")))
async def b_sw(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    t = cmd_arg(message.text)
    if not t and message.reply_to_message and message.reply_to_message.text: t = message.reply_to_message.text
    if not t: await reply_business(message, ".sw текст"); return
    cyr = sum(1 for c in t if "а" <= c.lower() <= "я")
    if cyr == 0: await reply_business(message, t.translate(str.maketrans(_EN, _RU)))
    else: await reply_business(message, t.translate(str.maketrans(_RU, _EN)))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "mute")))
async def b_mute(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    feats = await features_for(owner_id)
    if not feats["mute"]: await reply_business(message, "🔒 PRO/BUSINESS"); return
    await set_muted(owner_id, message.chat.id); await reply_business(message, "🔇 Mute вкл")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "unmute")))
async def b_unmute(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    removed = await clear_muted(owner_id, message.chat.id); await reply_business(message, "🔊 Mute off" if removed else "не был")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "ignore")))
async def b_ignore(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    await add_ignored_chat(owner_id, message.chat.id, "ignore"); await reply_business(message, "🚫 Чат в игноре")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "unignore")))
async def b_unignore(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    ok = await remove_ignored_chat(owner_id, message.chat.id); await reply_business(message, "✅ Убрано" if ok else "не был")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "track")))
async def b_track(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    await add_ignored_chat(owner_id, message.chat.id, "track"); await reply_business(message, "✅ В whitelist")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "untrack")))
async def b_untrack(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    ok = await remove_ignored_chat(owner_id, message.chat.id); await reply_business(message, "✅ Убрано" if ok else "не был")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "ignoremode")))
async def b_ignoremode(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    arg = cmd_arg(message.text).strip().lower()
    if arg == "on": await set_ignore_mode(owner_id, True); await reply_business(message, "🔒 Whitelist ВКЛ")
    elif arg == "off": await set_ignore_mode(owner_id, False); await reply_business(message, "✅ Blacklist")
    else: await reply_business(message, ".ignoremode on/off")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "hug")))
async def b_hug(message): await anime_cmd(message, ANIME_HUG, "🤗 *обнимает себя*")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "slap")))
async def b_slap(message): await anime_cmd(message, ANIME_SLAP, "👋 *шлёпает воздух*")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "pat")))
async def b_pat(message): await anime_cmd(message, ANIME_PAT, "🫶 *гладит себя*")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "kiss")))
async def b_kiss(message): await anime_cmd(message, ANIME_KISS, "💋 *воздушный поцелуй*")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "hangman")))
async def b_hangman(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    await start_hangman(message)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "quiz")))
async def b_quiz(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    q = random.choice(QUIZ_QUESTIONS); options = q[2][:]; random.shuffle(options)
    _quiz_games[message.chat.id] = {"correct": q[1], "score": 0, "options": options}
    lines = [f"❓ <b>{html.escape(q[0])}</b>\n"]
    for i, opt in enumerate(options, 1): lines.append(f"{i}. {html.escape(opt)}")
    lines.append("\nОтвет: <code>.answer N</code>")
    await reply_business(message, "\n".join(lines))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "answer")))
async def b_answer(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    game = _quiz_games.get(message.chat.id)
    if not game: return
    try: idx = int(cmd_arg(message.text).strip()) - 1
    except Exception: await reply_business(message, "Нужно число."); return
    if idx < 0 or idx >= len(game["options"]): await reply_business(message, "Неверный номер."); return
    chosen = game["options"][idx]
    if chosen == game["correct"]: await reply_business(message, "✅ Верно!")
    else: await reply_business(message, f"❌ Правильно: <b>{game['correct']}</b>")
    _quiz_games.pop(message.chat.id, None)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "guess")))
async def b_guess(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    n = random.randint(1, 100)
    _guess_games[message.chat.id] = {"n": n, "tries": 0}
    await reply_business(message, "🎲 От 1 до 100. <code>.try N</code>")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "try")))
async def b_try(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    game = _guess_games.get(message.chat.id)
    if not game: return
    v = cmd_arg(message.text).strip()
    if v and len(v) == 1 and v.isalpha():
        await process_hangman_guess(message, v); return
    try: n = int(v)
    except Exception: await reply_business(message, "Число/буква?"); return
    game["tries"] += 1
    if n == game["n"]:
        _guess_games.pop(message.chat.id, None); await reply_business(message, f"🎉 {n} за {game['tries']} попыток!"); return
    if n < game["n"]: await reply_business(message, "⬆️ Больше")
    else: await reply_business(message, "⬇️ Меньше")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "duel")))
async def b_duel(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if not message.reply_to_message or not message.reply_to_message.from_user: await reply_business(message, "Ответь на сообщение."); return
    p1 = message.from_user; p2 = message.reply_to_message.from_user
    r1 = random.randint(1,6); r2 = random.randint(1,6)
    w = p1.full_name if r1>r2 else (p2.full_name if r2>r1 else "ничья")
    await reply_business(message, f"🎲 <b>Дуэль</b>\n{html.escape(p1.full_name)}: {r1}\n{html.escape(p2.full_name)}: {r2}\n\nПобедитель: <b>{html.escape(w)}</b>")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "dice")))
async def b_dice(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    try: await _bot.send_dice(chat_id=message.chat.id, emoji="🎲", business_connection_id=message.business_connection_id)
    except Exception: pass
@dp.business_message(F.text.func(lambda t: is_cmd(t, "flip")))
async def b_flip(message): await reply_business(message, random.choice(["🪙 Орёл!", "🪙 Решка!"]))
def _ttt_kb(b, mid):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r*3+c; row.append(InlineKeyboardButton(text=b[i] or "·", callback_data=f"ttt:{mid}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "ttt")))
async def b_ttt(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if not message.reply_to_message: await reply_business(message, "Ответь на сообщение."); return
    p1 = message.from_user; p2 = message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id: await reply_business(message, "Нужен второй игрок."); return
    sent = await _bot.send_message(chat_id=message.chat.id, text=f"❌ {html.escape(p1.full_name)} vs ⭕ {html.escape(p2.full_name)}", business_connection_id=message.business_connection_id)
    _ttt_games[sent.message_id] = {"board": [""]*9, "turn": "X", "players": {p1.id:"X", p2.id:"O"}}
    await _bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=sent.message_id, reply_markup=_ttt_kb(_ttt_games[sent.message_id]["board"], sent.message_id), business_connection_id=message.business_connection_id)
@dp.callback_query(F.data.startswith("ttt:"))
async def ttt_move(cb):
    _, mid, idx = cb.data.split(":"); mid, idx = int(mid), int(idx)
    game = _ttt_games.get(mid)
    if not game: await cb.answer("Игра закончена"); return
    sym = game["players"].get(cb.from_user.id)
    if not sym: await cb.answer("Не участник"); return
    if sym != game["turn"]: await cb.answer("Не твой ход"); return
    if game["board"][idx]: await cb.answer("Занято"); return
    game["board"][idx] = sym
    winner = None
    for a,b,c in WIN_LINES:
        if game["board"][a] and game["board"][a]==game["board"][b]==game["board"][c]: winner=game["board"][a]; break
    bc = cb.message.business_connection_id
    if winner or all(game["board"]):
        _ttt_games.pop(mid, None); t = "Ничья!" if not winner else f"Победил {sym}"
        if bc: await _bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id, text=t, business_connection_id=bc)
        await cb.answer(); return
    game["turn"] = "O" if game["turn"]=="X" else "X"
    if bc: await _bot.edit_message_reply_markup(chat_id=cb.message.chat.id, message_id=cb.message.message_id, reply_markup=_ttt_kb(game["board"], mid), business_connection_id=bc)
    await cb.answer()
def _bw_kb(g, mid):
    rows = []
    for r in range(g["size"]):
        row = []
        for c in range(g["size"]):
            i = r*g["size"]+c
            row.append(InlineKeyboardButton(text="⬛" if g["board"][i] else "⬜", callback_data=f"bw:{mid}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "bw")))
async def b_bw(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    arg = cmd_arg(message.text).strip()
    size = int(arg) if arg.isdigit() and 2 <= int(arg) <= 6 else 4
    sent = await _bot.send_message(chat_id=message.chat.id, text=f"Поле {size}x{size}!", business_connection_id=message.business_connection_id)
    _bw_games[sent.message_id] = {"board": [False]*(size*size), "size": size}
    await _bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=sent.message_id, reply_markup=_bw_kb(_bw_games[sent.message_id], sent.message_id), business_connection_id=message.business_connection_id)
@dp.callback_query(F.data.startswith("bw:"))
async def bw_move(cb):
    _, mid, idx = cb.data.split(":"); mid, idx = int(mid), int(idx)
    g = _bw_games.get(mid)
    if not g: await cb.answer("end"); return
    g["board"][idx] = True
    bc = cb.message.business_connection_id
    if all(g["board"]):
        _bw_games.pop(mid, None)
        if bc: await _bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id, text="🎉 Поле!", business_connection_id=bc)
        await cb.answer(); return
    if bc: await _bot.edit_message_reply_markup(chat_id=cb.message.chat.id, message_id=cb.message.message_id, reply_markup=_bw_kb(g, mid), business_connection_id=bc)
    await cb.answer()
@dp.business_message(F.text.func(lambda t: is_cmd(t, "lq")))
async def b_lq(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if not (message.reply_to_message and message.reply_to_message.photo): await reply_business(message, "Ответь на фото."); return
    photo = message.reply_to_message.photo[-1]
    f = await _bot.get_file(photo.file_id); buf = await _bot.download_file(f.file_path)
    img = Image.open(io.BytesIO(buf.read())).convert("RGB"); w,h = img.size
    small = img.resize((max(1,w//8), max(1,h//8))); b2 = io.BytesIO(); small.save(b2, format="JPEG", quality=10); b2.seek(0)
    comp = Image.open(b2).convert("RGB"); res = comp.resize((w,h)); out = io.BytesIO(); res.save(out, format="JPEG", quality=40); out.seek(0)
    await _bot.send_photo(chat_id=message.chat.id, photo=BufferedInputFile(out.read(), filename="lq.jpg"), business_connection_id=message.business_connection_id)
@dp.business_message(F.text.func(lambda t: is_cmd(t, "get")))
async def b_get(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if not message.reply_to_message: return
    chat_id = message.chat.id; msg_id = message.reply_to_message.message_id
    row = await find_deleted_by_message_id(owner_id, chat_id, msg_id)
    if not row:
        cached = await get_cached_message(owner_id, chat_id, msg_id)
        if cached: _,_,_,ct,_,fp,_ = cached; row = (None, None, ct, fp, None)
    if not row or not row[3] or not os.path.exists(row[3]): await reply_business(message, "нет медиа"); return
    fp = row[3]; ct = row[2] or "document"; bc = message.business_connection_id
    try:
        if ct == "photo": await _bot.send_photo(chat_id=chat_id, photo=FSInputFile(fp), business_connection_id=bc)
        elif ct == "video": await _bot.send_video(chat_id=chat_id, video=FSInputFile(fp), business_connection_id=bc)
        elif ct == "voice": await _bot.send_voice(chat_id=chat_id, voice=FSInputFile(fp), business_connection_id=bc)
        else: await _bot.send_document(chat_id=chat_id, document=FSInputFile(fp), business_connection_id=bc)
    except Exception as e: await reply_business(message, f"err: {e}")
@dp.business_message(F.text.func(lambda t: is_cmd(t, "short")))
async def b_short(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    h = _recent_messages.get(message.chat.id, [])
    if not h: await reply_business(message, "пусто"); return
    await reply_business(message, "<b>Последние:</b>\n" + "\n".join(f"• {html.escape(x)}" for x in h[-10:]))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "deleted")))
async def b_deleted(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    feats = await features_for(owner_id)
    if not feats["history"]: await reply_business(message, "🔒 PRO"); return
    rows = await get_last_deleted(owner_id, message.chat.id, 10)
    if not rows: await reply_business(message, "нет"); return
    lines = ["<b>Удалённые:</b>"]
    for name, text, ct, fp, at in rows: lines.append(f"• <b>{html.escape(name or '?')}</b> [{at[:19]}] ({ct}): {html.escape(text or '')[:80]}")
    await reply_business(message, "\n".join(lines))
@dp.business_message(F.text.func(lambda t: is_cmd(t, "edited")))
async def b_edited(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    feats = await features_for(owner_id)
    if not feats["history"]: await reply_business(message, "🔒 PRO"); return
    rows = await get_last_edited(owner_id, message.chat.id, 10)
    if not rows: await reply_business(message, "нет"); return
    lines = ["<b>Изменённые:</b>"]
    for name, old, new, at in rows: lines.append(f"• <b>{html.escape(name or '?')}</b> [{at[:19]}]\n  <i>{html.escape(old or '')[:60]}</i> → <i>{html.escape(new or '')[:60]}</i>")
    await reply_business(message, "\n".join(lines))
@dp.business_message(F.text.func(_is_format_cmd))
async def b_format(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    name = message.text.strip().split(maxsplit=1)[0][1:].lower()
    text = _replied_text(message)
    if not text: await reply_business(message, f".{name} текст (или ответом)"); return
    await reply_business(message, FORMAT_COMMANDS[name](text))
@dp.business_message(F.reply_to_message)
async def b_reply_view_once(message):
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    if not message.from_user or message.from_user.id != owner_id: return
    r = message.reply_to_message
    cached = await get_cached_message(owner_id, message.chat.id, r.message_id)
    if not cached: return
    is_vo = cached[6] if len(cached) > 6 else 0
    if not is_vo: return
    fp = cached[5]; ct = cached[3]
    if not fp or not os.path.exists(fp): await reply_business(message, "файл удалён"); return
    bc = message.business_connection_id; chat_id = message.chat.id
    try:
        if ct == "photo": await _bot.send_photo(chat_id=chat_id, photo=FSInputFile(fp), business_connection_id=bc)
        elif ct == "video": await _bot.send_video(chat_id=chat_id, video=FSInputFile(fp), business_connection_id=bc)
        elif ct == "voice": await _bot.send_voice(chat_id=chat_id, voice=FSInputFile(fp), business_connection_id=bc)
        elif ct == "audio": await _bot.send_audio(chat_id=chat_id, audio=FSInputFile(fp), business_connection_id=bc)
        elif ct == "video_note": await _bot.send_video_note(chat_id=chat_id, video_note=FSInputFile(fp), business_connection_id=bc)
        else: await _bot.send_document(chat_id=chat_id, document=FSInputFile(fp), business_connection_id=bc)
    except Exception as e: logger.error(f"[vo] {e}")
@dp.business_message()
async def b_mute_filter(message):
    if not message.business_connection_id: return
    owner_id = get_owner_by_connection(message.business_connection_id)
    if not owner_id or not await is_connected(owner_id): return
    feats = await features_for(owner_id)
    if not feats["mute"]: return
    if not await is_muted(owner_id, message.chat.id): return
    if message.from_user and message.from_user.id == owner_id: return
    try: await _bot.delete_business_messages(business_connection_id=message.business_connection_id, message_ids=[message.message_id])
    except Exception: pass
@dp.message(CommandStart())
async def pm_start(message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1][4:])
            if ref_id != message.from_user.id: await add_referral(ref_id, message.from_user.id)
        except Exception: pass
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    text = (f"👋 <b>{html.escape(message.from_user.full_name)}</b>!\n\nБот для Telegram Business.\n"
            "• 🗑 удаления\n• ✏️ правки\n• 📸 view-once\n\nВыбери:")
    await message.answer(text, reply_markup=main_menu_kb(is_admin(message.from_user.id)))
@dp.message(Command("menu"))
async def pm_menu(message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer("Меню:", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
@dp.message(Command("me"))
async def pm_me(message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    row = await get_user(message.from_user.id)
    await message.answer(_profile_text(row), reply_markup=back_kb())
@dp.message(Command("plans"))
async def pm_plans(message): await message.answer(_plans_text(), reply_markup=plans_kb())
@dp.message(Command("ref"))
async def pm_ref(message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    total, paid = await get_ref_stats(message.from_user.id)
    me = await _bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(f"👥 <b>Рефералка</b>\n\n<code>{link}</code>\n\nПриглашено: <b>{total}</b>\nОплатили: <b>{paid}</b>\n+1 день PRO за оплату.", reply_markup=back_kb())
@dp.message(Command("promo"))
async def pm_promo(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2: await message.answer("/promo КОД"); return
    code = parts[1].strip().upper()
    if await was_promo_used(message.from_user.id, code): await message.answer("Уже использовал."); return
    p = await get_promo(code)
    if not p: await message.answer("❌ не найден."); return
    if p["until"]:
        try:
            if datetime.fromisoformat(p["until"]) < datetime.now(timezone.utc): await message.answer("❌ истёк."); return
        except Exception: pass
    if p["uses_left"] == 0: await message.answer("❌ кончился."); return
    await mark_promo_used(message.from_user.id, code)
    await message.answer(f"✅ Активирован! Скидка <b>{p['discount']}%</b>\n/plans")
@dp.message(Command("trial"))
async def pm_trial(message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    row = await get_user(message.from_user.id)
    if not row: await message.answer("Сначала /start"); return
    if row["trial_used"]: await message.answer("Пробный уже использован."); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (message.from_user.id,)); await db.commit()
    await set_user_plan(message.from_user.id, "pro", 3)
    await message.answer("🎁 Пробный PRO активирован на 3 дня!")
@dp.message(Command("admin"))
async def pm_admin(message):
    if not is_admin(message.from_user.id): await message.answer("Нет доступа."); return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_root_kb())
@dp.message(Command("find"))
async def pm_find(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit(): await message.answer("/find ID"); return
    uid = int(parts[1]); row = await get_user(uid)
    if not row: await message.answer("не найден"); return
    conns = [c for c, i in _connections.items() if i["owner_id"] == uid]
    await message.answer(f"👤 <code>{uid}</code>\n{html.escape(row['full_name'] or '—')}\nТариф: <b>{active_plan(row)}</b>\nДо: <code>{row['plan_until'] or '—'}</code>\nПодключений: {len(conns)}", reply_markup=admin_user_kb(uid))
@dp.message(Command("broadcast"))
async def pm_broadcast(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2: await message.answer("/broadcast текст"); return
    text = parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned=0"); ids = [r[0] for r in await cur.fetchall()]
    ok, fail = 0, 0
    for uid in ids:
        try: await _bot.send_message(uid, f"📣 <b>Рассылка</b>\n\n{html.escape(text)}"); ok += 1; await asyncio.sleep(0.05)
        except Exception: fail += 1
    await message.answer(f"✅ {ok} / ❌ {fail}")
@dp.message(Command("newpromo"))
async def pm_newpromo(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) < 4: await message.answer("/newpromo КОД СКИДКА ДНЕЙ [ЛИМИТ]"); return
    code = parts[1].upper()
    try: discount = int(parts[2]); days_alive = int(parts[3])
    except Exception: await message.answer("Числа!"); return
    uses = int(parts[4]) if len(parts) > 4 else -1
    await add_promo(code, discount, days_alive, uses)
    await message.answer(f"✅ {code}: скидка {discount}%, живёт {days_alive} дн, лимит: {'∞' if uses < 0 else uses}")
@dp.message(Command("delpromo"))
async def pm_delpromo(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2: await message.answer("/delpromo КОД"); return
    ok = await delete_promo(parts[1].strip())
    await message.answer("Удалён." if ok else "не найден")
def _profile_text(row):
    if not row: return "Не найден."
    plan = active_plan(row); pt = PLANS.get(plan, DEFAULT_PLANS["free"])["title"]
    until = row["plan_until"] or "—"
    return f"👤 <b>Профиль</b>\n\nID: <code>{row['user_id']}</code>\nИмя: {html.escape(row['full_name'] or '—')}\nТариф: <b>{pt}</b>\nДо: <code>{until}</code>"
def _plans_text():
    lines = ["💎 <b>Тарифы</b>\n"]
    for key in ("free", "pro", "business"):
        p = PLANS.get(key) or DEFAULT_PLANS[key]
        price = "бесплатно" if p["price"] == 0 else f"{p['price']}₽ ({stars_price(p['price'])}⭐)"
        f = p["features"]
        lines.append(f"<b>{p['title']}</b> — {price}")
        lines.append(f"  удаления: {'✅' if f['notify_deleted'] else '❌'} | правки: {'✅' if f['notify_edited'] else '❌'}")
        lines.append(f"  view-once: {'✅' if f['notify_view_once'] else '❌'} | mute: {'✅' if f['mute'] else '❌'}")
        lines.append("")
    lines.append("<i>Оплата Stars. /promo — промокод.</i>")
    return "\n".join(lines)
@dp.callback_query(F.data.startswith("menu:"))
async def menu_cb(cb):
    action = cb.data.split(":", 1)[1]
    if action == "root": await cb.message.edit_text("Меню:", reply_markup=main_menu_kb(is_admin(cb.from_user.id)))
    elif action == "profile":
        row = await get_user(cb.from_user.id); await cb.message.edit_text(_profile_text(row), reply_markup=back_kb())
    elif action == "plans": await cb.message.edit_text(_plans_text(), reply_markup=plans_kb())
    elif action == "trial":
        row = await get_user(cb.from_user.id)
        if row and row["trial_used"]: await cb.answer("Уже использован", show_alert=True)
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (cb.from_user.id,)); await db.commit()
            await set_user_plan(cb.from_user.id, "pro", 3)
            await cb.answer("PRO на 3 дня активирован!", show_alert=True)
    elif action == "ref":
        total, paid = await get_ref_stats(cb.from_user.id); me = await _bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
        await cb.message.edit_text(f"👥 <b>Рефералка</b>\n\n<code>{link}</code>\n\nПриглашено: <b>{total}</b>\nОплатили: <b>{paid}</b>", reply_markup=back_kb())
    elif action == "howto":
        await cb.message.edit_text("📖 Настройки → Telegram Business → Чат-боты → бот.\n.help в чате", reply_markup=back_kb())
    await cb.answer()
@dp.callback_query(F.data.startswith("buy:"))
async def buy_cb(cb):
    plan = cb.data.split(":", 1)[1]; p = PLANS.get(plan)
    if not p or p["price"] <= 0: await cb.answer("Ошибка", show_alert=True); return
    blocked = await is_pay_blocked(cb.from_user.id)
    if blocked: await cb.answer("Слишком много отмен. Подожди.", show_alert=True); return
    stars = stars_price(p["price"])
    await cb.message.answer_invoice(title=p["title"], description=f"{p['title']} на {p['days']} дней",
        payload=f"plan:{plan}:{cb.from_user.id}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label=p["title"], amount=stars)])
    await cb.answer()
@dp.pre_checkout_query()
async def pre_checkout(q):
    try:
        parts = q.invoice_payload.split(":")
        if len(parts) != 3 or parts[0] != "plan" or parts[1] not in PLANS: await q.answer(ok=False, error_message="Ошибка"); return
        await q.answer(ok=True)
    except Exception: await q.answer(ok=False, error_message="Ошибка")
@dp.message(F.successful_payment)
async def on_successful_payment(message):
    sp = message.successful_payment; parts = (sp.invoice_payload or "").split(":")
    if len(parts) != 3 or parts[0] != "plan": return
    plan_key = parts[1]
    try: buyer_id = int(parts[2])
    except Exception: buyer_id = message.from_user.id
    if plan_key not in PLANS: return
    await reset_pay_cancels(buyer_id)
    await grant_plan(buyer_id, plan_key, note=f"stars:{sp.total_amount}")
    referrer = await get_referrer(buyer_id)
    if referrer:
        marked = await mark_referral_paid(buyer_id)
        if marked:
            row = await get_user(referrer)
            if row:
                cur_plan = active_plan(row); base = "pro" if cur_plan == "free" else cur_plan
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute("SELECT plan_until FROM users WHERE user_id=?", (referrer,)); r2 = await cur.fetchone()
                cur_until = r2["plan_until"] if r2 and r2["plan_until"] else None
                if cur_until:
                    try: new_until = datetime.fromisoformat(cur_until) + timedelta(days=1)
                    except Exception: new_until = datetime.now(timezone.utc) + timedelta(days=1)
                else: new_until = datetime.now(timezone.utc) + timedelta(days=1)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE users SET plan=?, plan_until=? WHERE user_id=?", (base, new_until.isoformat(), referrer)); await db.commit()
                try: await _bot.send_message(referrer, f"🎉 +1 день {base.upper()} за реферала!")
                except Exception: pass
@dp.callback_query(F.data.startswith("admin:"))
async def admin_cb(cb):
    if cb.from_user.id not in ADMIN_IDS: await cb.answer("Нет доступа", show_alert=True); return
    parts = cb.data.split(":"); action = parts[1]
    if action == "root": await cb.message.edit_text("🛠 Админ", reply_markup=admin_root_kb())
    elif action == "stats":
        s = await stats_summary(); pl = s["plans"]
        text = (f"📊 Юзеров: <b>{s['users']}</b>\n🔌 Подключений: <b>{s['connections']}</b>\n"
                f"🗑 {s['deleted']} | ✏️ {s['edited']}\n💳 {s['payments']} | 💰 {s['revenue']}₽\n\n"
                f"free: {pl.get('free',0)}\npro: {pl.get('pro',0)}\nbusiness: {pl.get('business',0)}")
        await cb.message.edit_text(text, reply_markup=back_kb("admin:root"))
    elif action == "users":
        offset = int(parts[2]) if len(parts) > 2 else 0
        users = await list_users(10, offset); total = await count_users()
        lines = [f"👥 Юзеры ({offset+1}–{offset+len(users)} из {total}):"]
        kb_rows = []
        for u in users:
            plan = active_plan(u); ban = " 🚫" if u["is_banned"] else ""
            lines.append(f"• <code>{u['user_id']}</code> {html.escape(u['full_name'] or '—')} [{plan}]{ban}")
            kb_rows.append([InlineKeyboardButton(text=f"{u['user_id']} — {plan}", callback_data=f"admin:user:{u['user_id']}")])
        nav = []
        if offset > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{max(0,offset-10)}"))
        if offset + 10 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{offset+10}"))
        if nav: kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton(text="⬅️", callback_data="admin:root")])
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    elif action == "user":
        uid = int(parts[2]); row = await get_user(uid)
        if not row: await cb.answer("не найден", show_alert=True); return
        conns = [c for c, i in _connections.items() if i["owner_id"] == uid]
        text = f"👤 <code>{uid}</code>\n{html.escape(row['full_name'] or '—')}\nТариф: <b>{active_plan(row)}</b>\nДо: <code>{row['plan_until'] or '—'}</code>\nПодключений: {len(conns)}"
        await cb.message.edit_text(text, reply_markup=admin_user_kb(uid))
    elif action == "grant":
        uid = int(parts[2]); plan = parts[3]
        if plan == "free": await set_user_plan(uid, "free", 0); await cb.answer("→ free")
        else:
            p = PLANS.get(plan)
            if not p: await cb.answer("нет"); return
            await set_user_plan(uid, plan, p["days"]); await log_payment(uid, plan, p["price"], p["days"], f"by {cb.from_user.id}")
            try: await _bot.send_message(uid, f"🎉 {p['title']} на {p['days']} дней!")
            except Exception: pass
            await cb.answer(f"→ {plan}")
        row = await get_user(uid)
        await cb.message.edit_text(f"👤 <code>{uid}</code>\nТариф: <b>{active_plan(row)}</b>", reply_markup=admin_user_kb(uid))
    elif action == "ban":
        uid = int(parts[2]); row = await get_user(uid); new = not bool(row["is_banned"]); await ban_user(uid, new)
        await cb.answer("Бан" if new else "Разбан")
        row = await get_user(uid)
        await cb.message.edit_text(f"👤 <code>{uid}</code>\nБан: {'да' if row['is_banned'] else 'нет'}", reply_markup=admin_user_kb(uid))
    elif action == "plans":
        text = "💰 Тарифы:\n"
        for k in ("free","pro","business"):
            p = PLANS.get(k) or DEFAULT_PLANS[k]
            text += f"<b>{p['title']}</b> ({k}): {p['price']}₽ / {p['days']} дн\n"
        await cb.message.edit_text(text, reply_markup=admin_plans_kb())
    elif action == "editplan":
        key = parts[2]; p = PLANS.get(key)
        if not p: await cb.answer("нет"); return
        _pending_admin_input[cb.from_user.id] = {"type": "editplan", "key": key}
        await cb.message.edit_text(f"✏️ {p['title']}\nЦена: {p['price']}₽ Срок: {p['days']} дн\n\nОтправь: ЦЕНА СРОК (напр. 499 30)", reply_markup=back_kb("admin:plans"))
    elif action == "resetplan":
        key = parts[2]; await reset_plan(key); await cb.answer(f"{key} сброшен")
    elif action == "payments":
        offset = int(parts[2]) if len(parts) > 2 else 0
        rows = await get_payments(15, offset)
        if not rows: await cb.message.edit_text("Платежей нет.", reply_markup=back_kb("admin:root")); await cb.answer(); return
        lines = ["📜 <b>Платежи:</b>"]
        for r in rows: lines.append(f"• <code>{r['user_id']}</code> {r['plan']} {r['amount']}₽ [{r['paid_at'][:19]}]")
        await cb.message.edit_text("\n".join(lines), reply_markup=back_kb("admin:root"))
    elif action == "promos":
        rows = await list_promos()
        text = "🎟 <b>Промокоды:</b>\n\n"
        if not rows: text += "<i>нет</i>\n"
        for r in rows:
            left = "∞" if r["uses_left"] < 0 else r["uses_left"]
            text += f"• <code>{r['code']}</code> — {r['discount']}% (осталось: {left})\n"
        text += "\nСоздать: /newpromo КОД СКИДКА ДНЕЙ [ЛИМИТ]\nУдалить: /delpromo КОД"
        await cb.message.edit_text(text, reply_markup=back_kb("admin:root"))
    elif action == "refs":
        top = await get_ref_top(10)
        text = "👥 <b>Топ рефоводов:</b>\n\n"
        if not top: text += "<i>нет</i>"
        for rid, cnt, paid in top: text += f"• <code>{rid}</code>: {cnt} ({paid or 0} оплат)\n"
        await cb.message.edit_text(text, reply_markup=back_kb("admin:root"))
    elif action == "find":
        _pending_admin_input[cb.from_user.id] = {"type": "find"}
        await cb.message.edit_text("🔎 Отправь ID.", reply_markup=back_kb("admin:root"))
    elif action == "broadcast":
        _pending_admin_input[cb.from_user.id] = {"type": "broadcast"}
        await cb.message.edit_text("📣 Отправь текст.", reply_markup=back_kb("admin:root"))
    await cb.answer()
@dp.message(F.text & ~F.text.startswith("/"))
async def admin_text_input(message):
    if message.from_user.id not in ADMIN_IDS: return
    state = _pending_admin_input.pop(message.from_user.id, None)
    if not state: return
    text = (message.text or "").strip()
    if text.lower() == "cancel": await message.answer("Отменено.", reply_markup=admin_root_kb()); return
    if state["type"] == "editplan":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await message.answer("Формат: ЦЕНА СРОК"); _pending_admin_input[message.from_user.id] = state; return
        await update_plan(state["key"], price=int(parts[0]), days=int(parts[1]))
        await message.answer("✅ Обновлено.", reply_markup=admin_plans_kb()); return
    if state["type"] == "find":
        if not text.isdigit(): _pending_admin_input[message.from_user.id] = state; await message.answer("ID числом."); return
        uid = int(text); row = await get_user(uid)
        if not row: await message.answer("нет", reply_markup=admin_root_kb()); return
        conns = [c for c, i in _connections.items() if i["owner_id"] == uid]
        await message.answer(f"👤 <code>{uid}</code>\nТариф: <b>{active_plan(row)}</b>\nПодключений: {len(conns)}", reply_markup=admin_user_kb(uid)); return
    if state["type"] == "broadcast":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id FROM users WHERE is_banned=0"); ids = [r[0] for r in await cur.fetchall()]
        ok, fail = 0, 0
        for uid in ids:
            try: await _bot.send_message(uid, f"📣 {html.escape(text)}"); ok += 1; await asyncio.sleep(0.05)
            except Exception: fail += 1
        await message.answer(f"✅ {ok} / ❌ {fail}", reply_markup=admin_root_kb()); return
@dp.errors()
async def on_dispatcher_error(event): logger.error(f"[dispatcher] {event.exception!r}"); return True
async def set_commands(bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="menu", description="Меню"),
            BotCommand(command="me", description="Профиль"),
            BotCommand(command="plans", description="Тарифы"),
            BotCommand(command="ref", description="Рефералка"),
            BotCommand(command="promo", description="Промокод"),
            BotCommand(command="trial", description="Пробный PRO"),
            BotCommand(command="admin", description="Админ"),
        ], scope=BotCommandScopeDefault())
    except Exception as e: logger.warning(f"set_commands: {e}")
async def main():
    global _bot
    if not BOT_TOKEN: raise SystemExit("Set BOT_TOKEN env")
    await init_db(); await init_plans()
    _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await set_commands(_bot)
    await load_state()
    asyncio.create_task(media_cleanup_loop())
    asyncio.create_task(subscription_loop())
    while True:
        try:
            await dp.start_polling(_bot); break
        except Exception as e:
            logger.error(f"[main] {e!r}"); await asyncio.sleep(5)
if __name__ == "__main__":
    asyncio.run(main())
