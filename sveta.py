"""
Telegram-бот на aiogram 3.x для Telegram Business.

Пересылает владельцу в личку:
  * удалённые сообщения собеседника
  * изменённые сообщения собеседника
  * одноразовые (view-once) фото/видео собеседника

Обычные сообщения и медиа собеседника сохраняются в БД, но уведомления
в личку НЕ отправляются — чтобы не засорять чат.

Данные пишутся в /app/data — это сохраняется при пересборках Bothost.

Запуск:
  export BOT_TOKEN=твой_токен
  python svetlana.py
"""

import asyncio
import html
import io
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, BusinessMessagesDeleted,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    logger.warning(f"Cannot create DATA_DIR={DATA_DIR}: {e}, fallback to cwd")
    DATA_DIR = Path(".")

DB_PATH = str(DATA_DIR / "bot.db")
MEDIA_DIR = DATA_DIR / "media"
MEDIA_TTL_DAYS = int(os.environ.get("MEDIA_TTL_DAYS", "30"))

dp = Dispatcher()

_bot = None
_business_owner_id = None
_business_can_reply = False
_business_can_delete = None


# ============================================================
#  Helpers
# ============================================================

def is_from_owner(message):
    if not _business_owner_id or not message.from_user:
        return False
    return message.from_user.id == _business_owner_id


def _user_name(user):
    if not user:
        return "unknown"
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def _chat_title(message_or_deleted):
    chat = getattr(message_or_deleted, "chat", None)
    if not chat:
        return "unknown chat"
    return chat.title or chat.full_name or str(chat.id)


async def reply_business(message, text, **kwargs):
    if not _bot:
        logger.warning("reply_business: _bot is None")
        return
    if not message.business_connection_id:
        logger.warning("reply_business: business_connection_id is None")
        return
    try:
        await _bot.send_message(
            chat_id=message.chat.id,
            text=text,
            business_connection_id=message.business_connection_id,
            **kwargs,
        )
    except Exception as e:
        logger.error(f"reply_business failed: {e}")


async def notify_owner(text, file_path=None, is_photo=False, is_video=False):
    """Шлёт уведомление владельцу в личку. Логирует успех/провал."""
    if not _bot:
        logger.warning("[notify] skip — _bot is None")
        return
    if not _business_owner_id:
        logger.warning("[notify] skip — _business_owner_id is None "
                       "(Business connection не получен или бот запущен до подключения)")
        return
    try:
        if file_path and os.path.exists(file_path):
            if is_photo:
                await _bot.send_photo(_business_owner_id, FSInputFile(file_path), caption=text)
            elif is_video:
                try:
                    await _bot.send_video(_business_owner_id, FSInputFile(file_path), caption=text)
                except Exception:
                    await _bot.send_document(_business_owner_id, FSInputFile(file_path), caption=text)
            else:
                await _bot.send_document(_business_owner_id, FSInputFile(file_path), caption=text)
        else:
            await _bot.send_message(_business_owner_id, text)
        logger.info(f"[notify] sent to owner {_business_owner_id}")
    except Exception as e:
        logger.error(f"[notify] failed to send to {_business_owner_id}: {e}")


async def notify_owner_bytes(buf_bytes, filename, text, is_photo=False, is_video=False):
    """Шлёт уведомление с файлом из памяти (для view-once)."""
    if not _bot or not _business_owner_id:
        logger.warning("[notify_bytes] skip — no bot or no owner_id")
        return
    try:
        file = BufferedInputFile(buf_bytes, filename=filename)
        if is_photo:
            await _bot.send_photo(_business_owner_id, file, caption=text)
        elif is_video:
            try:
                await _bot.send_video(_business_owner_id, file, caption=text)
            except Exception:
                file2 = BufferedInputFile(buf_bytes, filename=filename)
                await _bot.send_document(_business_owner_id, file2, caption=text)
        else:
            await _bot.send_document(_business_owner_id, file, caption=text)
        logger.info(f"[notify_bytes] sent to owner {_business_owner_id}")
    except Exception as e:
        logger.error(f"[notify_bytes] failed: {e}")


# ============================================================
#  DB
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS afk (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    since  TEXT
);

CREATE TABLE IF NOT EXISTS status (
    user_id INTEGER PRIMARY KEY,
    text TEXT
);

CREATE TABLE IF NOT EXISTS message_cache (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    user_id       INTEGER,
    user_name     TEXT,
    text          TEXT,
    content_type  TEXT,
    file_id       TEXT,
    file_path     TEXT,
    created_at    TEXT,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS deleted_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    text        TEXT,
    content_type TEXT,
    file_path   TEXT,
    deleted_at  TEXT
);

CREATE TABLE IF NOT EXISTS edited_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    old_text    TEXT,
    new_text    TEXT,
    edited_at   TEXT
);

CREATE TABLE IF NOT EXISTS edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    user_id     INTEGER,
    user_name   TEXT,
    text        TEXT,
    edited_at   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS muted_chats (
    chat_id     INTEGER PRIMARY KEY,
    muted_at    TEXT,
    muted_by    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_deleted_chat ON deleted_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_edited_chat  ON edited_messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_history_msg  ON edit_history(chat_id, message_id);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- AFK ----------

async def set_afk(user_id, reason, since_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO afk (user_id, reason, since) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, since=excluded.since",
            (user_id, reason, since_iso),
        )
        await db.commit()


async def get_afk(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT reason, since FROM afk WHERE user_id=?", (user_id,))
        return await cur.fetchone()


async def clear_afk(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM afk WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0


# ---------- Status ----------

async def set_status(user_id, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO status (user_id, text) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET text=excluded.text",
            (user_id, text),
        )
        await db.commit()


async def get_status(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT text FROM status WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------- Cache ----------

async def cache_message(chat_id, message_id, user_id, user_name, text,
                        content_type="text", file_id=None, file_path=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO message_cache "
            "(chat_id, message_id, user_id, user_name, text, content_type, file_id, file_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, message_id) DO UPDATE SET "
            "  text=excluded.text,"
            "  file_id=excluded.file_id,"
            "  file_path=COALESCE(excluded.file_path, message_cache.file_path),"
            "  content_type=excluded.content_type",
            (chat_id, message_id, user_id, user_name, text, content_type,
             file_id, file_path, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_cached_message(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, user_name, text, content_type, file_id, file_path "
            "FROM message_cache WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        return await cur.fetchone()


async def drop_cached_message(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM message_cache WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        await db.commit()


# ---------- Deleted log ----------

async def log_deleted(chat_id, message_id, user_id, user_name, text,
                      content_type, file_path):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deleted_messages "
            "(chat_id, message_id, user_id, user_name, text, content_type, file_path, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, user_name, text, content_type,
             file_path, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_last_deleted(chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at "
            "FROM deleted_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return await cur.fetchall()


async def find_deleted_by_message_id(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, text, content_type, file_path, deleted_at "
            "FROM deleted_messages WHERE chat_id=? AND message_id=? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, message_id),
        )
        return await cur.fetchone()


# ---------- Edited log ----------

async def log_edited(chat_id, message_id, user_id, user_name, old_text, new_text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO edited_messages "
            "(chat_id, message_id, user_id, user_name, old_text, new_text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, user_name, old_text, new_text,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_last_edited(chat_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_name, old_text, new_text, edited_at FROM edited_messages "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return await cur.fetchall()


# ---------- Edit history ----------

async def add_edit_version(chat_id, message_id, user_id, user_name, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM edit_history "
            "WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        row = await cur.fetchone()
        next_version = (row[0] or 0) + 1
        await db.execute(
            "INSERT INTO edit_history "
            "(chat_id, message_id, version, user_id, user_name, text, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, next_version, user_id, user_name, text,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return next_version


async def get_edit_history(chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT version, text, edited_at FROM edit_history "
            "WHERE chat_id=? AND message_id=? ORDER BY version ASC",
            (chat_id, message_id),
        )
        return await cur.fetchall()


# ---------- Settings ----------

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


# ---------- Mute ----------

async def set_muted(chat_id, by_user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO muted_chats (chat_id, muted_at, muted_by) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET muted_at=excluded.muted_at, muted_by=excluded.muted_by",
            (chat_id, datetime.now(timezone.utc).isoformat(), by_user_id),
        )
        await db.commit()


async def is_muted(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM muted_chats WHERE chat_id=?", (chat_id,))
        return await cur.fetchone() is not None


async def clear_muted(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM muted_chats WHERE chat_id=?", (chat_id,))
        await db.commit()
        return cur.rowcount > 0


# ============================================================
#  Media
# ============================================================

MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _ext_from_path(file_path, content_type):
    if file_path and "." in file_path.split("/")[-1]:
        return "." + file_path.rsplit(".", 1)[-1].lower()
    return {
        "photo": ".jpg", "video": ".mp4", "voice": ".ogg", "audio": ".mp3",
        "video_note": ".mp4", "animation": ".mp4", "sticker": ".webp",
        "document": ".bin",
    }.get(content_type, ".bin")


def _media_file_id_and_type(message):
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


async def download_media(bot, file_id, content_type, chat_id, message_id):
    try:
        file = await bot.get_file(file_id)
        ext = _ext_from_path(file.file_path, content_type)
        filename = f"{chat_id}_{message_id}_{int(time.time())}{ext}"
        dest = MEDIA_DIR / filename
        await bot.download_file(file.file_path, destination=dest)
        logger.info(f"[media] saved: {dest}")
        return str(dest)
    except Exception as e:
        logger.warning(f"[media] failed {content_type} ({file_id}): {e}")
        return None


async def cleanup_old_media():
    if MEDIA_TTL_DAYS <= 0:
        return
    cutoff_ts = time.time() - MEDIA_TTL_DAYS * 86400
    removed = 0
    for p in MEDIA_DIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff_ts:
                p.unlink()
                removed += 1
        except Exception as e:
            logger.warning(f"[cleanup] failed {p}: {e}")
    if removed:
        logger.info(f"[cleanup] removed {removed} files")


async def media_cleanup_loop():
    while True:
        try:
            await cleanup_old_media()
        except Exception as e:
            logger.warning(f"[cleanup] error: {e}")
        await asyncio.sleep(3600)


# ============================================================
#  View-once
# ============================================================

def _is_view_once(message):
    if getattr(message, "has_media_spoiler", False):
        return True
    if getattr(message, "ttl_seconds", None):
        return True
    return False


async def forward_view_once(bot, message, file_id, content_type, user_name):
    """Скачивает view-once медиа в память и сразу шлёт владельцу."""
    if not _business_owner_id:
        logger.warning("[view_once] skip — no owner_id")
        return
    try:
        file = await bot.get_file(file_id)
        buf = await bot.download_file(file.file_path)
        data = buf.read()

        caption = (
            f"📸 <b>ОДНОРАЗОВОЕ ({content_type})</b>\n"
            f"От: <b>{html.escape(user_name)}</b>\n"
            f"Чат: <b>{html.escape(_chat_title(message))}</b>"
        )

        if content_type == "photo":
            await notify_owner_bytes(data, "view_once.jpg", caption, is_photo=True)
        elif content_type == "video":
            await notify_owner_bytes(data, "view_once.mp4", caption, is_video=True)
        else:
            await notify_owner_bytes(data, f"view_once_{content_type}", caption)

        logger.info(f"[view_once] forwarded {content_type} from {user_name}")
    except Exception as e:
        logger.error(f"[view_once] failed: {e}")


# ============================================================
#  Text fun
# ============================================================

LEET_MAP = str.maketrans({
    "a": "4", "A": "4", "e": "3", "E": "3", "i": "1", "I": "1",
    "o": "0", "O": "0", "s": "5", "S": "5", "t": "7", "T": "7",
    "b": "6", "B": "6", "а": "4", "А": "4", "е": "3", "Е": "3",
    "о": "0", "О": "0",
})

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
    ru_to_en = text.translate(_RU_TO_EN)
    en_to_ru = text.translate(_EN_TO_RU)
    cyrillic = sum(1 for c in text if "а" <= c.lower() <= "я")
    return en_to_ru if cyrillic == 0 else ru_to_en


FORMAT_COMMANDS = {
    "bold": to_bold, "italic": to_italic, "monospace": to_monospace,
    "underline": to_underline, "leet": to_leet, "kawaii": to_kawaii,
    "tsundere": to_tsundere, "yandere": to_yandere,
}


# ============================================================
#  Games
# ============================================================

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
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def start_ttt(message_id, p1, p2):
    game = {"board": [""] * 9, "turn": "X", "players": {p1: "X", p2: "O"}}
    _ttt_games[message_id] = game
    return game


_bw_games = {}


def start_bw(message_id, size=4):
    game = {"board": [False] * (size * size), "size": size}
    _bw_games[message_id] = game
    return game


def bw_keyboard(game, message_id):
    size = game["size"]
    board = game["board"]
    rows = []
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


# ============================================================
#  Image
# ============================================================

def degrade_image(data, scale_down=8, jpeg_quality=10):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    small = img.resize((max(1, w // scale_down), max(1, h // scale_down)))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=jpeg_quality)
    buf.seek(0)
    compressed = Image.open(buf).convert("RGB")
    result = compressed.resize((w, h))
    out = io.BytesIO()
    result.save(out, format="JPEG", quality=40)
    out.seek(0)
    return out.read()


def photo_to_gif(data):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="GIF")
    out.seek(0)
    return out.read()


# ============================================================
#  Text
# ============================================================

HELP_TEXT = """<b>Доступные команды</b>

<b>Утилиты</b>
.help — это сообщение
.afk [причина] / .afk off
.status текст
.time
.sw текст
.info
.love
.diag — состояние бота
.testnotify — тестовое уведомление в личку

<b>Форматирование</b> (ответь на сообщение)
.bold / .italic / .monospace / .underline
.leet / .kawaii / .tsundere / .yandere

<b>Игры</b>
.dice / .flip
.ttt @user (ответом)
.bw [размер]

<b>Медиа</b> (ответь на фото)
.lq / .gif / .get

<b>История</b>
.short — сводка последних
.deleted — последние удалённые
.edited — последние изменённые
.edits — история правок (ответом)

<b>Модерация</b>
.mute / .unmute

<i>Уведомления в личку приходят только на:</i>
<i>удаления, правки и одноразовые фото/видео от собеседника.</i>
"""


def is_cmd(text, name):
    if not text:
        return False
    return text.strip().split(maxsplit=1)[0].lower() == f".{name}"


def cmd_arg(text):
    parts = text.strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


# ============================================================
#  Business connection
# ============================================================

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    global _business_owner_id, _business_can_reply, _business_can_delete
    if connection.is_enabled:
        _business_owner_id = connection.user.id
        _business_can_reply = bool(getattr(connection, "can_reply", False))

        rights = getattr(connection, "rights", None)
        if rights is not None:
            _business_can_delete = bool(
                getattr(rights, "can_delete_all_messages", False)
                or getattr(rights, "can_delete_sent_messages", False)
            )
        else:
            _business_can_delete = None

        await set_setting("business_owner_id", str(connection.user.id))
        logger.info(
            f"Business connected: owner={connection.user.id} "
            f"connection={connection.id} can_reply={_business_can_reply} "
            f"can_delete={_business_can_delete}"
        )
        await notify_owner(
            "✅ Бот подключён к Telegram Business.\n"
            f"owner_id={_business_owner_id}\n"
            f"can_reply={_business_can_reply}, can_delete={_business_can_delete}\n"
            "Проверка: .diag"
        )
    else:
        logger.info(f"Business disconnected from {connection.user.id}")
        await notify_owner("⚠️ Бот отключён от Telegram Business.")


# ============================================================
#  Commands
# ============================================================

@dp.business_message(CommandStart())
async def start(message: Message):
    await reply_business(message, "Привет! Набери .help — увидишь список команд.")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "help")))
async def cmd_help(message: Message):
    await reply_business(message, HELP_TEXT)


@dp.business_message(F.text.func(lambda t: is_cmd(t, "love")))
async def cmd_love(message: Message):
    await reply_business(message, "❤️")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "time")))
async def cmd_time(message: Message):
    now = datetime.now(timezone.utc).astimezone()
    await reply_business(message, f"🕒 {now.strftime('%H:%M:%S %d.%m.%Y')}")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "diag")))
async def cmd_diag(message: Message):
    lines = ["<b>Диагностика</b>", ""]
    lines.append(f"business_owner_id: <code>{_business_owner_id}</code>")
    lines.append(f"business_connection_id: <code>{message.business_connection_id}</code>")
    lines.append(f"can_reply: <b>{_business_can_reply}</b>")
    lines.append(f"can_delete: <b>{_business_can_delete}</b>")
    lines.append(f"DB_PATH: <code>{DB_PATH}</code>")
    lines.append(f"DB exists: <b>{os.path.exists(DB_PATH)}</b>")
    lines.append(f"MEDIA_DIR: <code>{MEDIA_DIR}</code>")
    lines.append(f"MEDIA exists: <b>{MEDIA_DIR.exists()}</b>")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM message_cache")
            cache_n = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM deleted_messages")
            deleted_n = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM edited_messages")
            edited_n = (await cur.fetchone())[0]
        lines.append(f"message_cache: <b>{cache_n}</b>")
        lines.append(f"deleted_messages: <b>{deleted_n}</b>")
        lines.append(f"edited_messages: <b>{edited_n}</b>")
    except Exception as e:
        lines.append(f"DB error: <code>{html.escape(str(e))}</code>")
    try:
        media_count = sum(1 for _ in MEDIA_DIR.glob("*") if _.is_file())
        lines.append(f"files in media: <b>{media_count}</b>")
    except Exception as e:
        lines.append(f"Media error: <code>{html.escape(str(e))}</code>")
    await reply_business(message, "\n".join(lines))


@dp.business_message(F.text.func(lambda t: is_cmd(t, "testnotify")))
async def cmd_testnotify(message: Message):
    await reply_business(message, "Отправляю тестовое уведомление в личку...")
    await notify_owner(
        "🧪 <b>Тестовое уведомление</b>\n"
        f"owner_id = <code>{_business_owner_id}</code>\n"
        "Если ты это видишь — notify_owner работает."
    )


@dp.business_message(F.text.func(lambda t: is_cmd(t, "info")))
async def cmd_info(message: Message):
    user = message.from_user
    status = await get_status(user.id)
    lines = [
        f"<b>{user.full_name}</b>",
        f"id: <code>{user.id}</code>",
        f"username: @{user.username}" if user.username else "username: —",
    ]
    if status:
        lines.append(f"status: {status}")
    await reply_business(message, "\n".join(lines))


@dp.business_message(F.text.func(lambda t: is_cmd(t, "status")))
async def cmd_status(message: Message):
    text = cmd_arg(message.text)
    if not text:
        await reply_business(message, "Использование: .status текст")
        return
    await set_status(message.from_user.id, text)
    await reply_business(message, "Статус обновлён. Посмотреть — .info")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "sw")))
async def cmd_sw(message: Message):
    text = cmd_arg(message.text)
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    if not text:
        await reply_business(message, "Использование: .sw текст (или ответом)")
        return
    await reply_business(message, swap_layout(text))


@dp.business_message(F.text.func(lambda t: is_cmd(t, "afk")))
async def cmd_afk(message: Message):
    arg = cmd_arg(message.text).strip()
    if arg.lower() == "off":
        removed = await clear_afk(message.from_user.id)
        await reply_business(message, "AFK снят." if removed else "Ты не был в AFK.")
        return
    reason = arg or "без причины"
    await set_afk(message.from_user.id, reason, datetime.now(timezone.utc).isoformat())
    await reply_business(message, f"Включён AFK: {reason}")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "mute")))
async def cmd_mute(message: Message):
    await set_muted(message.chat.id, message.from_user.id)
    await reply_business(
        message,
        "🔇 <b>Mute включён.</b>\n"
        "Входящие от собеседника будут удаляться.\n"
        "Смотреть сохранённое: <code>.deleted</code>\n"
        "Выключить: <code>.unmute</code>"
    )


@dp.business_message(F.text.func(lambda t: is_cmd(t, "unmute")))
async def cmd_unmute(message: Message):
    removed = await clear_muted(message.chat.id)
    await reply_business(message, "🔊 Mute выключен." if removed else "Mute не был включён.")


# ============================================================
#  Formatting
# ============================================================

def _replied_text(message):
    if message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text
    return cmd_arg(message.text) or None


def _is_format_cmd(t):
    if not t or not t.startswith("."):
        return False
    return t.strip().split(maxsplit=1)[0][1:].lower() in FORMAT_COMMANDS


@dp.business_message(F.text.func(_is_format_cmd))
async def cmd_format(message: Message):
    name = message.text.strip().split(maxsplit=1)[0][1:].lower()
    text = _replied_text(message)
    if not text:
        await reply_business(message, f"Использование: .{name} текст (или ответом)")
        return
    await reply_business(message, FORMAT_COMMANDS[name](text))


# ============================================================
#  Games
# ============================================================

@dp.business_message(F.text.func(lambda t: is_cmd(t, "dice")))
async def cmd_dice(message: Message, bot: Bot):
    if not message.business_connection_id:
        return
    try:
        await bot.send_dice(
            chat_id=message.chat.id, emoji="🎲",
            business_connection_id=message.business_connection_id,
        )
    except Exception as e:
        logger.error(f"send_dice: {e}")


@dp.business_message(F.text.func(lambda t: is_cmd(t, "flip")))
async def cmd_flip(message: Message):
    await reply_business(message, coinflip())


@dp.business_message(F.text.func(lambda t: is_cmd(t, "ttt")))
async def cmd_ttt(message: Message, bot: Bot):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение соперника.")
        return
    p1 = message.from_user
    p2 = message.reply_to_message.from_user
    if p2.is_bot or p1.id == p2.id:
        await reply_business(message, "Нужен второй живой игрок.")
        return
    if not message.business_connection_id:
        return
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=f"❌ {p1.full_name} vs ⭕ {p2.full_name}\nХодит: {p1.full_name}",
        business_connection_id=message.business_connection_id,
    )
    game = start_ttt(sent.message_id, p1.id, p2.id)
    await bot.edit_message_reply_markup(
        chat_id=message.chat.id, message_id=sent.message_id,
        reply_markup=ttt_keyboard(game["board"], sent.message_id),
        business_connection_id=message.business_connection_id,
    )


@dp.callback_query(F.data.startswith("ttt:"))
async def ttt_move(callback: CallbackQuery, bot: Bot):
    _, msg_id_s, idx_s = callback.data.split(":")
    msg_id, idx = int(msg_id_s), int(idx_s)
    game = _ttt_games.get(msg_id)
    if not game:
        await callback.answer("Игра закончена.", show_alert=True)
        return
    player_symbol = game["players"].get(callback.from_user.id)
    if not player_symbol:
        await callback.answer("Ты не участник.", show_alert=True)
        return
    if player_symbol != game["turn"]:
        await callback.answer("Не твой ход.", show_alert=True)
        return
    if game["board"][idx]:
        await callback.answer("Клетка занята.", show_alert=True)
        return
    game["board"][idx] = player_symbol
    winner = ttt_winner(game["board"])
    bc_id = callback.message.business_connection_id
    if winner:
        _ttt_games.pop(msg_id, None)
        text = "Ничья!" if winner == "draw" else f"Победил {player_symbol}: {callback.from_user.full_name}!"
        if bc_id:
            await bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                text=text, business_connection_id=bc_id,
            )
        await callback.answer()
        return
    game["turn"] = "O" if game["turn"] == "X" else "X"
    if bc_id:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=ttt_keyboard(game["board"], msg_id),
            business_connection_id=bc_id,
        )
    await callback.answer()


@dp.business_message(F.text.func(lambda t: is_cmd(t, "bw")))
async def cmd_bw(message: Message, bot: Bot):
    if not message.business_connection_id:
        return
    arg = cmd_arg(message.text).strip()
    size = int(arg) if arg.isdigit() and 2 <= int(arg) <= 6 else 4
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=f"Закрась всё поле {size}x{size}!",
        business_connection_id=message.business_connection_id,
    )
    game = start_bw(sent.message_id, size)
    await bot.edit_message_reply_markup(
        chat_id=message.chat.id, message_id=sent.message_id,
        reply_markup=bw_keyboard(game, sent.message_id),
        business_connection_id=message.business_connection_id,
    )


@dp.callback_query(F.data.startswith("bw:"))
async def bw_move(callback: CallbackQuery, bot: Bot):
    _, msg_id_s, idx_s = callback.data.split(":")
    msg_id, idx = int(msg_id_s), int(idx_s)
    game = _bw_games.get(msg_id)
    if not game:
        await callback.answer("Игра закончена.", show_alert=True)
        return
    game["board"][idx] = True
    bc_id = callback.message.business_connection_id
    if all(game["board"]):
        _bw_games.pop(msg_id, None)
        if bc_id:
            await bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                text=f"🎉 Поле закрашено! ({callback.from_user.full_name})",
                business_connection_id=bc_id,
            )
        await callback.answer()
        return
    if bc_id:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=bw_keyboard(game, msg_id),
            business_connection_id=bc_id,
        )
    await callback.answer()


# ============================================================
#  Media commands
# ============================================================

@dp.business_message(F.text.func(lambda t: is_cmd(t, "lq")))
async def cmd_lq(message: Message, bot: Bot):
    if not (message.reply_to_message and message.reply_to_message.photo):
        await reply_business(message, "Ответь этой командой на фото.")
        return
    if not message.business_connection_id:
        return
    photo = message.reply_to_message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    result = degrade_image(buf.read())
    await bot.send_photo(
        chat_id=message.chat.id,
        photo=BufferedInputFile(result, filename="lq.jpg"),
        business_connection_id=message.business_connection_id,
    )


@dp.business_message(F.text.func(lambda t: is_cmd(t, "gif")))
async def cmd_gif(message: Message, bot: Bot):
    if not (message.reply_to_message and message.reply_to_message.photo):
        await reply_business(message, "Ответь этой командой на фото.")
        return
    if not message.business_connection_id:
        return
    photo = message.reply_to_message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = await bot.download_file(file.file_path)
    result = photo_to_gif(buf.read())
    await bot.send_animation(
        chat_id=message.chat.id,
        animation=BufferedInputFile(result, filename="out.gif"),
        business_connection_id=message.business_connection_id,
    )


@dp.business_message(F.text.func(lambda t: is_cmd(t, "get")))
async def cmd_get(message: Message, bot: Bot):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение.")
        return
    if not message.business_connection_id:
        return
    r = message.reply_to_message
    chat_id = message.chat.id
    msg_id = r.message_id

    row = await find_deleted_by_message_id(chat_id, msg_id)
    if not row:
        cached = await get_cached_message(chat_id, msg_id)
        if cached:
            _, _, _, content_type, _, file_path = cached
            row = (None, None, content_type, file_path, None)

    if not row or not row[3] or not os.path.exists(row[3]):
        await reply_business(message, "Для этого сообщения нет сохранённого медиа.")
        return

    file_path = row[3]
    content_type = row[2] or "document"
    bc_id = message.business_connection_id

    try:
        if content_type == "photo":
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "video":
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "voice":
            await bot.send_voice(chat_id=chat_id, voice=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "audio":
            await bot.send_audio(chat_id=chat_id, audio=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "animation":
            await bot.send_animation(chat_id=chat_id, animation=FSInputFile(file_path), business_connection_id=bc_id)
        elif content_type == "sticker":
            await bot.send_sticker(chat_id=chat_id, sticker=FSInputFile(file_path), business_connection_id=bc_id)
        else:
            await bot.send_document(chat_id=chat_id, document=FSInputFile(file_path), business_connection_id=bc_id)
    except Exception as e:
        logger.error(f"cmd_get: {e}")
        await reply_business(message, f"Не удалось отправить: {e}")


# ============================================================
#  History commands
# ============================================================

_recent_messages = {}
MAX_HISTORY = 50


@dp.business_message(F.text.func(lambda t: is_cmd(t, "short")))
async def cmd_short(message: Message):
    history = _recent_messages.get(message.chat.id, [])
    if not history:
        await reply_business(message, "Пока нечего пересказывать.")
        return
    last_n = history[-10:]
    summary = "\n".join(f"• {html.escape(line)}" for line in last_n)
    await reply_business(message, "<b>Последние сообщения:</b>\n" + summary)


@dp.business_message(F.text.func(lambda t: is_cmd(t, "deleted")))
async def cmd_deleted(message: Message):
    rows = await get_last_deleted(message.chat.id, limit=10)
    if not rows:
        await reply_business(message, "Удалённых не зафиксировано.")
        return
    lines = ["<b>Последние удалённые (от собеседника):</b>"]
    for name, text, ctype, fpath, at in rows:
        safe = html.escape(text or "")
        mark = " 📎" if fpath and os.path.exists(fpath) else ""
        lines.append(f"• <b>{html.escape(name or 'unknown')}</b> [{at[:19]}] ({ctype or 'text'}){mark}: {safe}")
    await reply_business(message, "\n".join(lines))


@dp.business_message(F.text.func(lambda t: is_cmd(t, "edited")))
async def cmd_edited(message: Message):
    rows = await get_last_edited(message.chat.id, limit=10)
    if not rows:
        await reply_business(message, "Изменённых не зафиксировано.")
        return
    lines = ["<b>Последние изменённые (от собеседника):</b>"]
    for name, old, new, at in rows:
        lines.append(
            f"• <b>{html.escape(name or 'unknown')}</b> [{at[:19]}]\n"
            f"  было: <i>{html.escape(old or '')}</i>\n"
            f"  стало: <i>{html.escape(new or '')}</i>"
        )
    await reply_business(message, "\n".join(lines))


@dp.business_message(F.text.func(lambda t: is_cmd(t, "edits")))
async def cmd_edits(message: Message):
    if not message.reply_to_message:
        await reply_business(message, "Ответь этой командой на сообщение.")
        return
    rows = await get_edit_history(message.chat.id, message.reply_to_message.message_id)
    if not rows:
        await reply_business(message, "Нет истории правок.")
        return
    lines = ["<b>История правок:</b>"]
    for version, text, at in rows:
        lines.append(f"v{version} [{at[:19]}]: <i>{html.escape(text or '')}</i>")
    await reply_business(message, "\n".join(lines))


# ============================================================
#  Edits (contact only) — уведомление владельцу
# ============================================================

@dp.edited_business_message()
async def on_edited(message: Message):
    if is_from_owner(message):
        logger.info(f"[edited] skip own {message.message_id}")
        return

    if message.text:
        cached = await get_cached_message(message.chat.id, message.message_id)
        old_text = cached[2] if cached else "<нет в кэше>"
        user_name = _user_name(message.from_user)

        await log_edited(
            chat_id=message.chat.id, message_id=message.message_id,
            user_id=message.from_user.id if message.from_user else None,
            user_name=user_name, old_text=old_text, new_text=message.text,
        )
        await add_edit_version(
            chat_id=message.chat.id, message_id=message.message_id,
            user_id=message.from_user.id if message.from_user else None,
            user_name=user_name, text=message.text,
        )
        await cache_message(
            chat_id=message.chat.id, message_id=message.message_id,
            user_id=message.from_user.id if message.from_user else None,
            user_name=user_name, text=message.text, content_type="text",
        )

        preview = (
            f"✏️ <b>Изменённое сообщение</b>\n"
            f"Чат: <b>{html.escape(_chat_title(message))}</b>\n"
            f"Автор: <b>{html.escape(user_name)}</b>\n"
            f"Было: <i>{html.escape(old_text or '')}</i>\n"
            f"Стало: <i>{html.escape(message.text)}</i>"
        )
        await notify_owner(preview)

    if message.caption:
        cached = await get_cached_message(message.chat.id, message.message_id)
        old_text = cached[2] if cached else "<нет в кэше>"
        user_name = _user_name(message.from_user)

        await add_edit_version(
            chat_id=message.chat.id, message_id=message.message_id,
            user_id=message.from_user.id if message.from_user else None,
            user_name=user_name, text=f"[caption] {message.caption}",
        )

        preview = (
            f"✏️ <b>Изменена подпись</b>\n"
            f"Чат: <b>{html.escape(_chat_title(message))}</b>\n"
            f"Автор: <b>{html.escape(user_name)}</b>\n"
            f"Стало: <i>{html.escape(message.caption)}</i>"
        )
        await notify_owner(preview)


# ============================================================
#  Deletions (contact only) — уведомление владельцу
# ============================================================

@dp.deleted_business_messages()
async def on_deleted_business(deleted: BusinessMessagesDeleted):
    chat_id = deleted.chat.id
    chat_label = html.escape(_chat_title(deleted))

    for msg_id in deleted.message_ids:
        cached = await get_cached_message(chat_id, msg_id)

        if not cached:
            logger.info(f"[deleted] skip {msg_id} — not in cache")
            continue

        user_id, user_name, text, content_type, file_id, file_path = cached

        if _business_owner_id and user_id == _business_owner_id:
            logger.info(f"[deleted] skip {msg_id} — owner message")
            await drop_cached_message(chat_id, msg_id)
            continue

        await log_deleted(
            chat_id=chat_id, message_id=msg_id, user_id=user_id,
            user_name=user_name, text=text, content_type=content_type,
            file_path=file_path,
        )
        await drop_cached_message(chat_id, msg_id)

        logger.info(f"[deleted] chat={chat_id} msg={msg_id} user={user_name} type={content_type}")

        header = (
            f"🗑 <b>Удалённое сообщение</b>\n"
            f"Чат: <b>{chat_label}</b>\n"
            f"Автор: <b>{html.escape(user_name or 'unknown')}</b>\n"
            f"Тип: {content_type}\n"
            f"Текст: <i>{html.escape(text or '')}</i>"
        )
        await notify_owner(
            header,
            file_path=file_path,
            is_photo=(content_type == "photo"),
            is_video=(content_type in ("video", "video_note", "animation")),
        )


# ============================================================
#  Cache + View-once forward (contact only)
# ============================================================

@dp.business_message(F.text)
async def collect_history(message: Message):
    if not message.from_user:
        return
    if is_from_owner(message):
        logger.info(f"[cache] skip own {message.message_id}")
        return

    await cache_message(
        chat_id=message.chat.id, message_id=message.message_id,
        user_id=message.from_user.id, user_name=_user_name(message.from_user),
        text=message.text, content_type="text",
    )
    if not message.text.startswith("."):
        chat_history = _recent_messages.setdefault(message.chat.id, [])
        chat_history.append(f"{message.from_user.first_name}: {message.text}")
        if len(chat_history) > MAX_HISTORY:
            del chat_history[0]


@dp.business_message()
async def cache_media(message: Message, bot: Bot):
    if is_from_owner(message):
        logger.info(f"[cache_media] skip own {message.message_id}")
        return

    file_id, content_type = _media_file_id_and_type(message)
    if not file_id:
        return

    user_name = _user_name(message.from_user)
    caption = message.caption or f"<{content_type}>"
    user_id = message.from_user.id if message.from_user else None

    # view-once → сразу пересылаем владельцу в личку
    if _is_view_once(message):
        await forward_view_once(bot, message, file_id, content_type, user_name)

    # всё равно сохраняем на диск + в БД (для .deleted / .get)
    file_path = await download_media(
        bot=bot, file_id=file_id, content_type=content_type,
        chat_id=message.chat.id, message_id=message.message_id,
    )
    await cache_message(
        chat_id=message.chat.id, message_id=message.message_id,
        user_id=user_id, user_name=user_name, text=caption,
        content_type=content_type, file_id=file_id, file_path=file_path,
    )


# ============================================================
#  Mute
# ============================================================

@dp.business_message()
async def mute_filter(message: Message, bot: Bot):
    if not message.business_connection_id:
        return
    if not await is_muted(message.chat.id):
        return
    if is_from_owner(message):
        return

    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
        logger.info(f"[mute] deleted {message.message_id}")
    except Exception as e:
        err = str(e)
        logger.warning(f"[mute] failed {message.message_id}: {err}")
        if "not enough rights" in err.lower() or "no rights" in err.lower():
            await reply_business(
                message,
                "⚠️ <b>Mute не может удалить сообщение.</b>\n"
                "Включи <b>Delete messages</b> для бота:\n"
                "<i>Settings → Telegram Business → Chatbots → бот → Delete messages</i>"
            )


# ============================================================
#  Entry point
# ============================================================

async def main():
    global _bot, _business_owner_id
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable")

    await init_db()
    logger.info(f"DB_PATH={DB_PATH}")
    logger.info(f"MEDIA_DIR={MEDIA_DIR}")

    saved_owner = await get_setting("business_owner_id")
    if saved_owner and saved_owner.isdigit():
        _business_owner_id = int(saved_owner)
        logger.info(f"Owner from DB: {_business_owner_id}")
    else:
        logger.warning("Owner not in DB yet — waiting for business_connection event")

    _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    if _business_owner_id:
        await notify_owner(
            "🚀 <b>Бот запущен</b>\n"
            f"owner_id = <code>{_business_owner_id}</code>\n"
            "Буду присылать сюда:\n"
            "• удалённые сообщения собеседника\n"
            "• изменённые сообщения собеседника\n"
            "• одноразовые фото/видео собеседника\n\n"
            "Проверка: <code>.diag</code> или <code>.testnotify</code>"
        )

    asyncio.create_task(media_cleanup_loop())

    logger.info("Starting polling...")
    await dp.start_polling(_bot)


if __name__ == "__main__":
    asyncio.run(main())