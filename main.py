"""
Telegram-бот открытого бета-тестирования (ОБТ): приём отчётов о багах, проблемах и идеях по улучшению.

Любой желающий выбирает тип отчёта, указывает ник, описывает ситуацию и прикладывает доказательства.
Отчёт получает номер и попадает в группу администраторов, где его обрабатывают. О каждом
изменении статуса участник узнаёт в личных сообщениях. Библиотека: aiogram 3.x.
"""
import asyncio
import csv
import html
import io
import logging
import os
import sqlite3
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# ───────────────────────── НАСТРОЙКИ ─────────────────────────
def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def env_ids(name: str) -> set:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.strip("-").isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = env_int("ADMIN_CHAT_ID")  # группа администраторов
ADMIN_THREAD_ID = env_int("ADMIN_THREAD_ID") or None  # тема «ОБТ ОТЧЕТЫ» в группе администраторов
ADMIN_IDS = env_ids("ADMIN_IDS")  # если задано, статусы могут менять только эти Telegram ID
PROJECT_NAME = os.getenv("PROJECT_NAME", "Мой проект")
BANNER_PATH = os.getenv("BANNER_PATH", "banner.jpg")  # картинка над меню, необязательно
TZ_OFFSET = env_int("TZ_OFFSET_HOURS", 2)
MAX_REPORTS_PER_DAY = env_int("MAX_REPORTS_PER_DAY", 10)
COOLDOWN_SECONDS = env_int("COOLDOWN_SECONDS", 60)
MAX_EVIDENCE = 10
DB_PATH = os.getenv("DB_PATH", "feedback.db")

CATS = {"bug": ("🐞", "Баг"), "problem": ("⚠️", "Проблема"), "idea": ("💡", "Идея")}
STATUS = {"new": "🟡 Новый", "progress": "🔵 В работе", "done": "🟢 Решено", "rejected": "🔴 Отклонено"}
STATUS_PLAIN = {"new": "Новый", "progress": "В работе", "done": "Решено", "rejected": "Отклонено"}

EVIDENCE_TYPES = (
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
    ContentType.ANIMATION,
    ContentType.VOICE,
    ContentType.VIDEO_NOTE,
    ContentType.AUDIO,
)

# ───────────────────────── ТЕКСТЫ ─────────────────────────
T = {
    "start": (
        "Привет, <b>{name}</b>!\n\n"
        "Это бот открытого бета-тестирования (ОБТ) проекта <b>{project}</b>. Здесь можно сообщить о баге или проблеме "
        "либо предложить идею по улучшению.\n\n"
        "Каждый отчёт получает номер, а о решении администрации вы узнаете в этом чате."
    ),
    "info": (
        "ℹ️ <b>Как составить хороший отчёт</b>\n\n"
        "1. <b>Ваш ник</b> в игре.\n"
        "2. <b>Подробное описание:</b> что произошло, где и когда, как это повторить по шагам, что вы ожидали увидеть.\n"
        "3. <b>Доказательства:</b> скриншоты, видео, файлы или ссылки. Можно отправить до 10 сообщений.\n\n"
        "Чем подробнее отчёт, тем быстрее мы сможем разобраться. Один отчёт описывает одну ситуацию."
    ),
    "banned": "Доступ к отправке отчётов для вас ограничен администрацией.",
    "closed": "Приём отчётов сейчас приостановлен. Попробуйте позже.",
    "not_ready": "Бот ещё не настроен. Попробуйте позже.",
    "cooldown": "Подождите {s} с перед следующим отчётом.",
    "daily_limit": "Достигнут лимит: не больше {n} отчётов в сутки. Попробуйте завтра.",
    "ask_nick": "Тип отчёта: <b>{cat}</b>\n\n<b>1/3.</b> Укажите ваш ник в игре:",
    "bad_nick": "Ник должен быть от 2 до 40 символов. Попробуйте ещё раз.",
    "ask_desc_bug": (
        "<b>2/3.</b> Опишите баг подробно: что произошло, как это повторить (по шагам) "
        "и что вы ожидали увидеть."
    ),
    "ask_desc_problem": "<b>2/3.</b> Опишите проблему: что именно не работает или мешает играть, где и когда это происходит.",
    "ask_desc_idea": (
        "<b>2/3.</b> Опишите вашу идею: что предлагаете улучшить и зачем. "
        "Если есть примеры из других проектов, укажите их."
    ),
    "bad_desc": "Описание должно быть от 10 до 1500 символов. Попробуйте ещё раз.",
    "ask_evidence": (
        "<b>3/3.</b> Приложите доказательства: скриншоты, видео, файлы или ссылки. "
        "Можно отправить несколько сообщений (до {m}). Затем нажмите «Готово»."
    ),
    "ask_evidence_idea": (
        "<b>3/3.</b> По желанию приложите картинки или ссылки с примерами (до {m} сообщений). "
        "Затем нажмите «Готово» или «Пропустить»."
    ),
    "ev_count": "📎 Добавлено вложений: <b>{n}</b> из {m}. Отправьте ещё или нажмите «Готово».",
    "ev_limit": "Достигнут лимит вложений. Нажмите «Готово».",
    "ev_required": "Для бага или проблемы нужно приложить хотя бы одно доказательство: скриншот, видео или ссылку. Если доказательств нет, коротко напишите об этом сообщением.",
    "ev_unsupported": "Этот тип сообщения не поддерживается. Отправьте текст, фото, видео, файл или голосовое.",
    "bad_text": "Пожалуйста, отправьте ответ текстом.",
    "summary_head": "📝 <b>Проверьте отчёт</b>\n",
    "summary_foot": "\nПосле отправки изменить отчёт нельзя.",
    "submitted": "✅ <b>Отчёт #{id} отправлен!</b>\n\nАдминистрация рассмотрит его, а о статусе мы сообщим вам здесь. Все свои отчёты можно посмотреть в разделе «Мои отчёты».",
    "failed": "Не удалось отправить отчёт. Попробуйте ещё раз немного позже.",
    "no_reports": "У вас пока нет отчётов.",
    "st_progress": "🔵 Ваш отчёт #{id} взят в работу.",
    "st_done": "🟢 Ваш отчёт #{id} обработан: проблема решена или идея учтена. Спасибо за помощь проекту!",
    "st_rejected": "🔴 Ваш отчёт #{id} отклонён.",
    "st_new": "🟡 Ваш отчёт #{id} возвращён на рассмотрение.",
    "answer": "💬 <b>Ответ администрации по отчёту #{id}:</b>",
}

ADMIN_HELP = (
    "🛠 <b>Команды администратора</b>\n"
    "/list — открытые отчёты (новые и в работе)\n"
    "/report <code>номер</code> — показать отчёт\n"
    "/find <code>текст</code> — поиск по нику, @username, ID, описанию\n"
    "/progress <code>номер</code> — взять в работу\n"
    "/done <code>номер</code> [комментарий] — решено или учтено\n"
    "/reject <code>номер</code> [причина] — отклонить\n"
    "/reopen <code>номер</code> — вернуть в работу\n"
    "/answer <code>номер</code> <code>текст</code> — написать участнику\n"
    "Ответ на карточку отчёта (reply) тоже уходит участнику\n"
    "/ban <code>ID</code> и /unban <code>ID</code> — блокировка отправки отчётов\n"
    "/stats — статистика\n"
    "/close и /open — приостановить или возобновить приём\n"
    "/export — выгрузка всех отчётов в CSV"
)

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        full_name TEXT NOT NULL,
        category TEXT NOT NULL,
        nick TEXT NOT NULL,
        description TEXT NOT NULL,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',
        comment TEXT,
        handled_by TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER,
        admin_msg_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        last_nick TEXT,
        banned INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """
)


def get_setting(key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
    db.commit()


def reports_open() -> bool:
    return get_setting("open", "1") == "1"


def get_report(report_id: int):
    return db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()


def get_user(user_id: int):
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_banned(user_id: int, banned: bool):
    db.execute(
        "INSERT INTO users(user_id, banned) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned",
        (user_id, int(banned)),
    )
    db.commit()


def remember_nick(user_id: int, nick: str):
    db.execute(
        "INSERT INTO users(user_id, last_nick) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET last_nick=excluded.last_nick",
        (user_id, nick),
    )
    db.commit()


def cooldown_left(user_id: int) -> int:
    row = db.execute("SELECT MAX(created_at) AS t FROM reports WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row["t"]:
        return 0
    return max(0, COOLDOWN_SECONDS - (int(time.time()) - row["t"]))


def daily_count(user_id: int) -> int:
    since = int(time.time()) - 86400
    row = db.execute("SELECT COUNT(*) AS n FROM reports WHERE user_id=? AND created_at>?", (user_id, since)).fetchone()
    return row["n"]


def fmt_time(ts) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts + TZ_OFFSET * 3600))


def esc(text) -> str:
    return html.escape(str(text if text is not None else "—"))


def safe_cell(value) -> str:
    """Защита от формул при открытии CSV в Excel."""
    value = str(value if value is not None else "")
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def build_csv() -> bytes:
    rows = db.execute("SELECT * FROM reports ORDER BY id").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Номер", "Тип", "Статус", "Telegram ID", "Username", "Имя", "Ник", "Описание",
         "Вложений", "Комментарий", "Обработал", "Подан", "Обновлён"]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"], CATS[r["category"]][1], STATUS_PLAIN[r["status"]], r["user_id"],
                safe_cell(r["username"]), safe_cell(r["full_name"]), safe_cell(r["nick"]),
                safe_cell(r["description"]), r["evidence_count"], safe_cell(r["comment"]),
                safe_cell(r["handled_by"]), fmt_time(r["created_at"]),
                fmt_time(r["updated_at"]) if r["updated_at"] else "",
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


# ───────────────────────── КАРТОЧКИ И КЛАВИАТУРЫ ─────────────────────────
def card_text(r, limit: int = 0) -> str:
    icon, title = CATS[r["category"]]
    who = esc(r["full_name"]) + (f" @{esc(r['username'])}" if r["username"] else "")
    desc = r["description"]
    if limit and len(desc) > limit:
        desc = desc[:limit] + "…"
    lines = [
        f"{icon} <b>Отчёт #{r['id']}</b> · {title} · {STATUS[r['status']]}",
        f"Участник: {who} (<code>{r['user_id']}</code>)",
        f"Ник в игре: <b>{esc(r['nick'])}</b>",
        f"Подан: {fmt_time(r['created_at'])} (UTC{TZ_OFFSET:+d})",
        "",
        "<b>Описание:</b>",
        esc(desc),
        "",
        f"📎 Вложений: {r['evidence_count']}" + (" (в сообщениях ниже)" if r["evidence_count"] else ""),
    ]
    if r["handled_by"]:
        lines += ["", f"Обработал: {esc(r['handled_by'])}, {fmt_time(r['updated_at'])}"]
    if r["comment"]:
        lines.append(f"Комментарий: {esc(r['comment'])}")
    text = "\n".join(lines)
    if len(text) > 4000 and not limit:
        return card_text(r, limit=600)
    return text


def user_view_text(r) -> str:
    icon, title = CATS[r["category"]]
    lines = [
        f"{icon} <b>Отчёт #{r['id']}</b> · {title}",
        f"Статус: <b>{STATUS[r['status']]}</b>",
        f"Ник: {esc(r['nick'])}",
        f"Подан: {fmt_time(r['created_at'])}",
    ]
    if r["updated_at"] and r["status"] != "new":
        lines.append(f"Обновлён: {fmt_time(r['updated_at'])}")
    if r["comment"]:
        lines.append(f"Комментарий администрации: {esc(r['comment'])}")
    return "\n".join(lines)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐞 Сообщить о баге", callback_data="new:bug")],
            [InlineKeyboardButton(text="⚠️ Сообщить о проблеме", callback_data="new:problem")],
            [InlineKeyboardButton(text="💡 Предложить идею", callback_data="new:idea")],
            [
                InlineKeyboardButton(text="📋 Мои отчёты", callback_data="mylist"),
                InlineKeyboardButton(text="ℹ️ Как составить отчёт", callback_data="info"),
            ],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="cancel")]])


def nick_kb(last_nick) -> InlineKeyboardMarkup:
    rows = []
    if last_nick:
        rows.append([InlineKeyboardButton(text=f"Использовать «{last_nick}»"[:60], callback_data="usenick")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def evidence_kb(category: str, count: int) -> InlineKeyboardMarkup:
    rows = []
    if count:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="ev_done")])
        rows.append([InlineKeyboardButton(text="🗑 Сбросить вложения", callback_data="ev_clear")])
    elif category == "idea":
        rows.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="ev_done")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить отчёт", callback_data="submit")],
            [InlineKeyboardButton(text="🔄 Заполнить заново", callback_data="restart")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]
    )


def admin_kb(report_id: int, status: str) -> InlineKeyboardMarkup:
    if status == "new":
        rows = [
            [InlineKeyboardButton(text="🔵 В работу", callback_data=f"ra:prog:{report_id}")],
            [
                InlineKeyboardButton(text="🟢 Решено", callback_data=f"ra:done:{report_id}"),
                InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"ra:rej:{report_id}"),
            ],
        ]
    elif status == "progress":
        rows = [
            [
                InlineKeyboardButton(text="🟢 Решено", callback_data=f"ra:done:{report_id}"),
                InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"ra:rej:{report_id}"),
            ]
        ]
    else:
        rows = [[InlineKeyboardButton(text="↩️ Вернуть в работу", callback_data=f"ra:prog:{report_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_menu(bot: Bot, chat_id: int, name: str):
    caption = T["start"].format(name=esc(name), project=esc(PROJECT_NAME))
    if os.path.exists(BANNER_PATH):
        await bot.send_photo(chat_id, FSInputFile(BANNER_PATH), caption=caption, reply_markup=main_menu())
    else:
        await bot.send_message(chat_id, caption, reply_markup=main_menu())


# ───────────────────────── ДОСТУП ─────────────────────────
async def check_access(bot: Bot, user_id: int, new_report: bool = False):
    """Возвращает текст причины отказа или None, если доступ есть."""
    row = get_user(user_id)
    if row and row["banned"]:
        return T["banned"]
    if new_report:
        if not reports_open():
            return T["closed"]
        if not ADMIN_CHAT_ID:
            return T["not_ready"]
        left = cooldown_left(user_id)
        if left > 0:
            return T["cooldown"].format(s=left)
        if daily_count(user_id) >= MAX_REPORTS_PER_DAY:
            return T["daily_limit"].format(n=MAX_REPORTS_PER_DAY)
    return None


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def who(user) -> str:
    return user.full_name if user else "Администратор"


# ───────────────────────── ОТПРАВКА ОТЧЁТА ─────────────────────────
admin_lock = asyncio.Lock()
user_locks = defaultdict(asyncio.Lock)


async def submit_report(bot: Bot, user, category: str, nick: str, description: str, evidence_ids: list):
    """Сохраняет отчёт и публикует его в группе администраторов. Возвращает номер или None."""
    cur = db.execute(
        "INSERT INTO reports(user_id, username, full_name, category, nick, description, evidence_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user.id, user.username, user.full_name or "—", category, nick, description, len(evidence_ids), int(time.time())),
    )
    db.commit()
    report_id = cur.lastrowid
    async with admin_lock:
        try:
            card = await bot.send_message(
                ADMIN_CHAT_ID,
                card_text(get_report(report_id)),
                reply_markup=admin_kb(report_id, "new"),
                message_thread_id=ADMIN_THREAD_ID,
            )
        except TelegramAPIError:
            logging.exception("Не удалось отправить отчёт в группу администраторов")
            db.execute("DELETE FROM reports WHERE id=?", (report_id,))
            db.commit()
            return None
        db.execute("UPDATE reports SET admin_msg_id=? WHERE id=?", (card.message_id, report_id))
        db.commit()
        if evidence_ids:
            try:
                await bot.copy_messages(
                    chat_id=ADMIN_CHAT_ID,
                    from_chat_id=user.id,
                    message_ids=evidence_ids,
                    message_thread_id=ADMIN_THREAD_ID,
                )
            except TelegramAPIError:
                logging.exception("Не удалось скопировать вложения отчёта #%s", report_id)
                await notify_admins(bot, f"⚠️ К отчёту #{report_id} не удалось приложить вложения. Попросите участника прислать их заново.")
    remember_nick(user.id, nick)
    return report_id


async def notify_admins(bot: Bot, text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, message_thread_id=ADMIN_THREAD_ID)
    except TelegramAPIError:
        logging.warning("Не удалось отправить уведомление в группу администраторов")


async def refresh_card(bot: Bot, r):
    if not r or not r["admin_msg_id"]:
        return
    try:
        await bot.edit_message_text(
            card_text(r), chat_id=ADMIN_CHAT_ID, message_id=r["admin_msg_id"], reply_markup=admin_kb(r["id"], r["status"])
        )
    except TelegramBadRequest:
        pass


async def notify_user(bot: Bot, r):
    text = T[f"st_{r['status']}"].format(id=r["id"])
    if r["comment"] and r["status"] in ("done", "rejected"):
        text += f"\n\nКомментарий администрации: {esc(r['comment'])}"
    try:
        await bot.send_message(r["user_id"], text)
    except (TelegramForbiddenError, TelegramBadRequest):
        logging.warning("Не удалось уведомить пользователя %s", r["user_id"])


async def set_status(bot: Bot, report_id: int, status: str, by: str, comment=None):
    """Меняет статус, обновляет карточку и уведомляет участника. Возвращает отчёт или None."""
    r = get_report(report_id)
    if not r:
        return None
    if r["status"] == status and not comment:
        return r
    db.execute(
        "UPDATE reports SET status=?, comment=COALESCE(?, comment), handled_by=?, updated_at=? WHERE id=?",
        (status, comment or None, by, int(time.time()), report_id),
    )
    db.commit()
    r = get_report(report_id)
    await refresh_card(bot, r)
    await notify_user(bot, r)
    return r


# ───────────────────────── СОСТОЯНИЯ И РОУТЕРЫ ─────────────────────────
class Report(StatesGroup):
    nick = State()
    desc = State()
    evidence = State()
    confirm = State()


common_router = Router()
ops_router = Router()
user_router = Router()
ops_router.message.filter(F.chat.id == ADMIN_CHAT_ID)
user_router.message.filter(F.chat.type == "private")


@common_router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    text = f"ID этого чата: <code>{message.chat.id}</code>"
    if message.is_topic_message and message.message_thread_id:
        text += f"\nID этой темы: <code>{message.message_thread_id}</code>"
    await message.answer(text)


# ───────────────────────── УЧАСТНИК ─────────────────────────
@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    denied = await check_access(bot, message.from_user.id)
    if denied:
        await message.answer(denied)
        return
    await send_menu(bot, message.chat.id, message.from_user.first_name)


@user_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await send_menu(bot, message.chat.id, message.from_user.first_name)


@user_router.message(Command("my"))
async def cmd_my(message: Message, bot: Bot):
    denied = await check_access(bot, message.from_user.id)
    if denied:
        await message.answer(denied)
        return
    await show_my_list(message, message.from_user.id)


@user_router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    await state.clear()
    await send_menu(bot, call.message.chat.id, call.from_user.first_name)


@user_router.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery):
    await call.answer()
    await call.message.answer(T["info"], reply_markup=back_kb())


async def show_my_list(message: Message, user_id: int):
    rows = db.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)).fetchall()
    if not rows:
        await message.answer(T["no_reports"], reply_markup=back_kb())
        return
    buttons = [
        [InlineKeyboardButton(text=f"#{r['id']} {CATS[r['category']][0]} {STATUS[r['status']]}", callback_data=f"my:{r['id']}")]
        for r in rows
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="cancel")])
    await message.answer("📋 <b>Ваши отчёты:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@user_router.callback_query(F.data == "mylist")
async def cb_mylist(call: CallbackQuery, bot: Bot):
    await call.answer()
    denied = await check_access(bot, call.from_user.id)
    if denied:
        await call.message.answer(denied)
        return
    await show_my_list(call.message, call.from_user.id)


@user_router.callback_query(F.data.startswith("my:"))
async def cb_myview(call: CallbackQuery):
    await call.answer()
    try:
        r = get_report(int(call.data.split(":")[1]))
    except ValueError:
        return
    if not r or r["user_id"] != call.from_user.id:
        return
    await call.message.answer(user_view_text(r), reply_markup=back_kb())


@user_router.callback_query(F.data.startswith("new:"))
async def cb_new(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    category = call.data.split(":")[1]
    if category not in CATS:
        return
    denied = await check_access(bot, call.from_user.id, new_report=True)
    if denied:
        await call.message.answer(denied, reply_markup=back_kb())
        return
    await state.clear()
    await state.set_state(Report.nick)
    await state.update_data(category=category, ev=[], ack=None)
    row = get_user(call.from_user.id)
    last = row["last_nick"] if row else None
    icon, title = CATS[category]
    await call.message.answer(T["ask_nick"].format(cat=f"{icon} {title}"), reply_markup=nick_kb(last))


async def go_description(message: Message, state: FSMContext, nick: str):
    await state.update_data(nick=nick)
    await state.set_state(Report.desc)
    data = await state.get_data()
    await message.answer(T[f"ask_desc_{data['category']}"], reply_markup=cancel_kb())


@user_router.callback_query(Report.nick, F.data == "usenick")
async def cb_usenick(call: CallbackQuery, state: FSMContext):
    await call.answer()
    row = get_user(call.from_user.id)
    if not row or not row["last_nick"]:
        return
    await go_description(call.message, state, row["last_nick"])


@user_router.message(Report.nick, F.text)
async def msg_nick(message: Message, state: FSMContext):
    nick = message.text.strip()
    if not 2 <= len(nick) <= 40:
        await message.answer(T["bad_nick"], reply_markup=cancel_kb())
        return
    await go_description(message, state, nick)


@user_router.message(Report.desc, F.text)
async def msg_desc(message: Message, state: FSMContext):
    text = message.text.strip()
    if not 10 <= len(text) <= 1500:
        await message.answer(T["bad_desc"], reply_markup=cancel_kb())
        return
    data = await state.get_data()
    await state.update_data(desc=text)
    await state.set_state(Report.evidence)
    key = "ask_evidence_idea" if data["category"] == "idea" else "ask_evidence"
    await message.answer(T[key].format(m=MAX_EVIDENCE), reply_markup=evidence_kb(data["category"], 0))


@user_router.message(Report.nick)
@user_router.message(Report.desc)
async def msg_not_text(message: Message):
    await message.answer(T["bad_text"], reply_markup=cancel_kb())


@user_router.message(Report.evidence, F.content_type.in_(EVIDENCE_TYPES))
async def ev_add(message: Message, state: FSMContext, bot: Bot):
    # блокировка нужна, потому что альбом приходит несколькими сообщениями одновременно
    async with user_locks[message.from_user.id]:
        data = await state.get_data()
        ev = data.get("ev", [])
        if len(ev) >= MAX_EVIDENCE:
            await message.answer(T["ev_limit"])
            return
        ev.append(message.message_id)
        await state.update_data(ev=ev)
        text = T["ev_count"].format(n=len(ev), m=MAX_EVIDENCE)
        markup = evidence_kb(data["category"], len(ev))
        ack = data.get("ack")
        if ack:
            try:
                await bot.edit_message_text(text, chat_id=message.chat.id, message_id=ack, reply_markup=markup)
                return
            except TelegramBadRequest:
                pass
        sent = await message.answer(text, reply_markup=markup)
        await state.update_data(ack=sent.message_id)


@user_router.message(Report.evidence)
async def ev_unsupported(message: Message):
    await message.answer(T["ev_unsupported"])


@user_router.callback_query(Report.evidence, F.data == "ev_clear")
async def cb_ev_clear(call: CallbackQuery, state: FSMContext):
    await call.answer("Вложения сброшены")
    data = await state.get_data()
    await state.update_data(ev=[], ack=None)
    try:
        await call.message.edit_text("Вложения сброшены. Отправьте новые.", reply_markup=evidence_kb(data["category"], 0))
    except TelegramBadRequest:
        pass


def summary_text(data: dict) -> str:
    icon, title = CATS[data["category"]]
    return (
        T["summary_head"]
        + f"<b>Тип:</b> {icon} {title}\n"
        + f"<b>Ник:</b> {esc(data['nick'])}\n"
        + f"<b>Описание:</b> {esc(data['desc'])}\n"
        + f"<b>Вложений:</b> {len(data.get('ev', []))}"
        + T["summary_foot"]
    )


@user_router.callback_query(Report.evidence, F.data == "ev_done")
async def cb_ev_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    if data["category"] != "idea" and not data.get("ev"):
        await call.message.answer(T["ev_required"], reply_markup=evidence_kb(data["category"], 0))
        return
    await state.set_state(Report.confirm)
    await call.message.answer(summary_text(data), reply_markup=confirm_kb())


@user_router.callback_query(Report.confirm, F.data == "restart")
async def cb_restart(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    data = await state.get_data()
    category = data.get("category", "bug")
    await state.clear()
    await state.set_state(Report.nick)
    await state.update_data(category=category, ev=[], ack=None)
    row = get_user(call.from_user.id)
    icon, title = CATS[category]
    await call.message.answer(
        T["ask_nick"].format(cat=f"{icon} {title}"), reply_markup=nick_kb(row["last_nick"] if row else None)
    )


@user_router.callback_query(Report.confirm, F.data == "submit")
async def cb_submit(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    user = call.from_user
    data = await state.get_data()
    await state.clear()
    if not all(k in data for k in ("category", "nick", "desc")):
        await call.message.answer(T["failed"], reply_markup=back_kb())
        return
    denied = await check_access(bot, user.id, new_report=True)
    if denied:
        await call.message.answer(denied, reply_markup=back_kb())
        return
    report_id = await submit_report(bot, user, data["category"], data["nick"], data["desc"], data.get("ev", []))
    if report_id is None:
        await call.message.answer(T["failed"], reply_markup=back_kb())
        return
    await call.message.answer(T["submitted"].format(id=report_id), reply_markup=back_kb())


# Сообщения вне формы: показываем меню
@user_router.message(StateFilter(None))
async def fallback(message: Message, bot: Bot):
    denied = await check_access(bot, message.from_user.id)
    if denied:
        await message.answer(denied)
        return
    await send_menu(bot, message.chat.id, message.from_user.first_name)


# ───────────────────────── АДМИНИСТРАТОРЫ ─────────────────────────
@ops_router.callback_query(F.data.startswith("ra:"), F.message.chat.id == ADMIN_CHAT_ID)
async def cb_admin(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("У вас нет доступа к решениям.", show_alert=True)
        return
    try:
        _, action, raw_id = call.data.split(":")
        report_id = int(raw_id)
    except ValueError:
        await call.answer()
        return
    status = {"prog": "progress", "done": "done", "rej": "rejected"}.get(action)
    if status is None:
        await call.answer()
        return
    r = await set_status(bot, report_id, status, who(call.from_user))
    if r is None:
        await call.answer("Отчёт не найден.", show_alert=True)
        return
    await call.answer(STATUS_PLAIN[r["status"]])


def split_args(command: CommandObject):
    """Разбирает «номер текст». Возвращает (номер или None, остаток)."""
    args = (command.args or "").strip()
    first, _, rest = args.partition(" ")
    first = first.lstrip("#")
    return (int(first) if first.isdigit() else None), rest.strip()


def admin_only(message: Message) -> bool:
    return is_admin(message.from_user.id if message.from_user else 0)


async def status_command(message: Message, command: CommandObject, bot: Bot, status: str):
    if not admin_only(message):
        return
    report_id, rest = split_args(command)
    if report_id is None:
        await message.reply("Укажите номер отчёта, например: /done 12")
        return
    r = await set_status(bot, report_id, status, who(message.from_user), rest or None)
    await message.reply("Отчёт не найден." if r is None else f"Отчёт #{r['id']}: {STATUS[r['status']]}")


@ops_router.message(Command("progress", "reopen"))
async def cmd_progress(message: Message, command: CommandObject, bot: Bot):
    await status_command(message, command, bot, "progress")


@ops_router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject, bot: Bot):
    await status_command(message, command, bot, "done")


@ops_router.message(Command("reject"))
async def cmd_reject(message: Message, command: CommandObject, bot: Bot):
    await status_command(message, command, bot, "rejected")


@ops_router.message(Command("answer"))
async def cmd_answer(message: Message, command: CommandObject, bot: Bot):
    if not admin_only(message):
        return
    report_id, text = split_args(command)
    r = get_report(report_id) if report_id else None
    if not r or not text:
        await message.reply("Пример: /answer 12 Спасибо, баг подтверждён")
        return
    try:
        await bot.send_message(r["user_id"], T["answer"].format(id=r["id"]) + "\n" + esc(text))
        await message.reply("Ответ отправлен.")
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.reply("Не удалось отправить: участник заблокировал бота.")


@ops_router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject):
    if not admin_only(message):
        return
    report_id, _ = split_args(command)
    r = get_report(report_id) if report_id else None
    if not r:
        await message.reply("Отчёт не найден. Пример: /report 12")
        return
    await message.answer(card_text(r), reply_markup=admin_kb(r["id"], r["status"]))


def list_lines(rows) -> str:
    return "\n".join(
        f"#{r['id']} {CATS[r['category']][0]} · {esc(r['nick'])} · {STATUS[r['status']]} · {fmt_time(r['created_at'])}"
        for r in rows
    )


@ops_router.message(Command("list"))
async def cmd_list(message: Message):
    if not admin_only(message):
        return
    rows = db.execute("SELECT * FROM reports WHERE status IN ('new','progress') ORDER BY id LIMIT 30").fetchall()
    if not rows:
        await message.answer("Открытых отчётов нет. 🎉")
        return
    await message.answer("📂 <b>Открытые отчёты:</b>\n" + list_lines(rows) + "\n\nОткрыть: /report номер")


@ops_router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    if not admin_only(message):
        return
    term = (command.args or "").strip().lstrip("@")
    if not term:
        await message.reply("Что искать? Пример: /find Ivan")
        return
    like = f"%{term}%"
    rows = db.execute(
        "SELECT * FROM reports WHERE CAST(user_id AS TEXT)=? OR username LIKE ? OR full_name LIKE ? OR nick LIKE ? "
        "OR description LIKE ? ORDER BY id DESC LIMIT 15",
        (term, like, like, like, like),
    ).fetchall()
    if not rows:
        await message.reply("Ничего не найдено.")
        return
    await message.answer("🔎 <b>Найдено:</b>\n" + list_lines(rows) + "\n\nОткрыть: /report номер")


@ops_router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not admin_only(message):
        return
    by_status = {s: 0 for s in STATUS}
    for r in db.execute("SELECT status, COUNT(*) AS n FROM reports GROUP BY status"):
        by_status[r["status"]] = r["n"]
    by_cat = {c: 0 for c in CATS}
    for r in db.execute("SELECT category, COUNT(*) AS n FROM reports GROUP BY category"):
        by_cat[r["category"]] = r["n"]
    total = sum(by_status.values())
    text = (
        f"📊 <b>Статистика отчётов</b>\nВсего: {total}\n"
        + "\n".join(f"{STATUS[s]}: {n}" for s, n in by_status.items())
        + "\n\n"
        + "\n".join(f"{CATS[c][0]} {CATS[c][1]}: {n}" for c, n in by_cat.items())
        + f"\n\nПриём отчётов: {'открыт' if reports_open() else 'приостановлен'}"
    )
    await message.answer(text)


@ops_router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not admin_only(message):
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.reply("Пример: /ban 123456789 (Telegram ID участника)")
        return
    set_banned(int(arg), True)
    await message.reply(f"Участник {arg} больше не может отправлять отчёты.")


@ops_router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not admin_only(message):
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.reply("Пример: /unban 123456789")
        return
    set_banned(int(arg), False)
    await message.reply(f"Участник {arg} снова может отправлять отчёты.")


@ops_router.message(Command("close"))
async def cmd_close(message: Message):
    if admin_only(message):
        set_setting("open", "0")
        await message.reply("⛔ Приём новых отчётов приостановлен.")


@ops_router.message(Command("open"))
async def cmd_open(message: Message):
    if admin_only(message):
        set_setting("open", "1")
        await message.reply("✅ Приём отчётов возобновлён.")


@ops_router.message(Command("export"))
async def cmd_export(message: Message):
    if not admin_only(message):
        return
    await message.answer_document(BufferedInputFile(build_csv(), filename="reports.csv"), caption="Выгрузка всех отчётов")


@ops_router.message(Command("adminhelp", "help"))
async def cmd_adminhelp(message: Message):
    if admin_only(message):
        await message.answer(ADMIN_HELP)


# Ответ на карточку отчёта (reply) уходит участнику
@ops_router.message(F.reply_to_message)
async def relay_reply(message: Message, bot: Bot):
    if not admin_only(message) or (message.text and message.text.startswith("/")):
        return
    r = db.execute("SELECT * FROM reports WHERE admin_msg_id=?", (message.reply_to_message.message_id,)).fetchone()
    if not r:
        return
    try:
        await bot.send_message(r["user_id"], T["answer"].format(id=r["id"]))
        await message.copy_to(r["user_id"])
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.reply("Не удалось доставить: участник заблокировал бота.")


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if not ADMIN_CHAT_ID:
        logging.warning("ADMIN_CHAT_ID не задан: отчёты принимать некуда. Узнайте ID группы командой /chatid.")
    if not ADMIN_THREAD_ID:
        logging.warning("ADMIN_THREAD_ID не задан: отчёты придут в общий чат группы, а не в тему «ОБТ ОТЧЕТЫ».")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common_router)
    dp.include_router(ops_router)
    dp.include_router(user_router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
