import html
import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "bot.db"
ENV_PATH = Path(".env")


def load_env() -> None:
    if not ENV_PATH.exists():
        return

    with ENV_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                blocked_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                suggestion_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                admin_content_message_id INTEGER,
                admin_panel_message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def is_blocked(user_id: int) -> bool:
    with get_db() as connection:
        row = connection.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def block_user(user_id: int, info: dict) -> None:
    with get_db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO blocked_users (user_id, full_name, username)
            VALUES (?, ?, ?)
            """,
            (user_id, info.get("full_name") or str(user_id), info.get("username")),
        )


def unblock_user(user_id: int) -> None:
    with get_db() as connection:
        connection.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))


def get_blocked_users() -> List[dict]:
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT user_id, full_name, username
            FROM blocked_users
            ORDER BY blocked_at DESC, user_id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_suggestion(
    suggestion_id: str,
    user_id: int,
    info: dict,
    admin_content_message_id: int,
    admin_panel_message_id: int,
) -> None:
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO suggestions (
                suggestion_id,
                user_id,
                full_name,
                username,
                admin_content_message_id,
                admin_panel_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                suggestion_id,
                user_id,
                info.get("full_name") or str(user_id),
                info.get("username"),
                admin_content_message_id,
                admin_panel_message_id,
            ),
        )


def get_suggestion(suggestion_id: str) -> Optional[dict]:
    with get_db() as connection:
        row = connection.execute(
            "SELECT * FROM suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
    return dict(row) if row else None


def get_latest_suggestion_by_user(user_id: int) -> Optional[dict]:
    with get_db() as connection:
        row = connection.execute(
            """
            SELECT * FROM suggestions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_suggestion(suggestion_id: str) -> Optional[dict]:
    suggestion = get_suggestion(suggestion_id)
    if not suggestion:
        return None

    with get_db() as connection:
        connection.execute("DELETE FROM suggestions WHERE suggestion_id = ?", (suggestion_id,))
    return suggestion


def get_admin_id() -> int:
    admin_id = os.getenv("ADMIN_ID")
    if not admin_id or not admin_id.isdigit():
        raise RuntimeError("Поставь ADMIN_ID в .env.")
    return int(admin_id)


def user_link(user) -> str:
    name = html.escape(user.full_name or str(user.id))
    profile = f'<a href="tg://user?id={user.id}">{name}</a>'
    if user.username:
        username = html.escape(user.username)
        return f'{profile} (@{username})'
    return profile


def user_info(user) -> dict:
    return {
        "full_name": user.full_name or str(user.id),
        "username": user.username,
    }


def stored_user_label(user_id: int, info: Optional[dict]) -> str:
    if not info:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'

    name = html.escape(info.get("full_name") or str(user_id))
    profile = f'<a href="tg://user?id={user_id}">{name}</a>'
    username = info.get("username")
    if username:
        return f'{profile} (@{html.escape(username)})'
    return profile


def admin_keyboard(suggestion_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🙋‍♂️ Ответить", callback_data=f"reply:{suggestion_id}")],
            [
                InlineKeyboardButton("❌ Удалить", callback_data=f"delete:{suggestion_id}"),
                InlineKeyboardButton("🤬 Заблокировать", callback_data=f"block:{user_id}"),
            ],
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚫 Заблокированные", callback_data="panel:blocked")],
            [InlineKeyboardButton("↩️ Отменить ответ", callback_data="panel:cancel")],
            [InlineKeyboardButton("ℹ️ Как работать", callback_data="panel:help")],
        ]
    )


def blocked_keyboard(blocked_users: List[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Разблокировать {user_id}", callback_data=f"unblock:{user_id}")]
         for user_id in blocked_users]
    )


def admin_panel_text() -> str:
    return (
        "<b>Админ-панель</b>\n\n"
        "Доступные действия:\n"
        "• смотреть и разблокировать пользователей;\n"
        "• отменять режим ответа;\n"
        "• отвечать, удалять и блокировать через кнопки под каждой предложкой."
    )


async def send_blocked_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    blocked_users = get_blocked_users()
    if not blocked_users:
        await message.reply_text("Список заблокированных пуст.")
        return

    lines = ["<b>Заблокированные пользователи</b>"]
    for index, user in enumerate(blocked_users, start=1):
        user_id = user["user_id"]
        lines.append(f"{index}. {stored_user_label(user_id, user)} — <code>{user_id}</code>")

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=blocked_keyboard([user["user_id"] for user in blocked_users]),
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id == context.bot_data["admin_id"]:
        await update.message.reply_text(
            "Бот предложки запущен. Пользователи могут присылать сюда текст, фото, видео, документы, голосовые и другой контент."
        )
        return

    await update.message.reply_text(
        "📤 Отправь сюда текст, картинку, видео, документ или другой контент для предложки. "
        "Админ увидит сообщение и сможет ответить."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != context.bot_data["admin_id"]:
        return

    context.user_data.pop("reply_to_user_id", None)
    await update.message.reply_text("Режим ответа отменен.")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != context.bot_data["admin_id"]:
        return

    await update.message.reply_text(
        admin_panel_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_keyboard(),
    )


async def blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != context.bot_data["admin_id"]:
        return

    await send_blocked_users(update, context)


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.user_data.get("reply_to_user_id")
    if not user_id:
        await update.message.reply_text("Нажми кнопку «Ответить» под предложкой, потом отправь ответ.")
        return

    try:
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except Forbidden:
        await update.message.reply_text("Не удалось отправить ответ: пользователь заблокировал бота.")
        return
    except TelegramError as error:
        await update.message.reply_text(f"Не удалось отправить ответ: {error}")
        return

    context.user_data.pop("reply_to_user_id", None)
    await update.message.reply_text("Ответ отправлен пользователю.")


async def handle_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    admin_id = context.bot_data["admin_id"]

    if is_blocked(user.id):
        await update.message.reply_text("Ты заблокирован в предложке.")
        return

    suggestion_id = uuid4().hex[:10]

    try:
        copied = await context.bot.copy_message(
            chat_id=admin_id,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except TelegramError as error:
        await update.message.reply_text("Не удалось отправить предложку админу. Попробуй позже.")
        await context.bot.send_message(admin_id, f"Ошибка при копировании предложки: {error}")
        return

    panel_text = (
        "<b>Новая предложка</b>\n"
        f"Отправитель: {user_link(user)}\n"
        f"ID: <code>{user.id}</code>"
    )
    panel = await context.bot.send_message(
        chat_id=admin_id,
        text=panel_text,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(suggestion_id, user.id),
        disable_web_page_preview=True,
    )

    save_suggestion(
        suggestion_id=suggestion_id,
        user_id=user.id,
        info=user_info(user),
        admin_content_message_id=copied.message_id,
        admin_panel_message_id=panel.message_id,
    )

    await update.message.reply_text("Предложка отправлена админу.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query.from_user.id != context.bot_data["admin_id"]:
        await query.answer("Эта кнопка только для админа.", show_alert=True)
        return

    await query.answer()

    action, value = query.data.split(":", 1)

    if action == "panel":
        if value == "blocked":
            await send_blocked_users(update, context)
            return

        if value == "cancel":
            context.user_data.pop("reply_to_user_id", None)
            await query.message.reply_text("Режим ответа отменен.")
            return

        if value == "help":
            await query.message.reply_text(admin_panel_text(), parse_mode=ParseMode.HTML)
            return

    if action == "reply":
        suggestion = get_suggestion(value)
        if not suggestion:
            await query.edit_message_text("Предложка не найдена. Возможно, бот был перезапущен со старой кнопкой.")
            return

        context.user_data["reply_to_user_id"] = suggestion["user_id"]
        await query.message.reply_text("Отправь следующее сообщение, и оно уйдет пользователю. /cancel - отмена.")
        return

    if action == "delete":
        suggestion = delete_suggestion(value)
        if not suggestion:
            await query.edit_message_text("Предложка уже удалена или не найдена.")
            return

        await delete_admin_message(context, suggestion.get("admin_content_message_id"))
        await query.edit_message_text("Предложка удалена.")
        return

    if action == "block":
        user_id = int(value)
        suggestion = get_latest_suggestion_by_user(user_id)
        info = {
            "full_name": suggestion["full_name"] if suggestion else str(user_id),
            "username": suggestion["username"] if suggestion else None,
        }
        block_user(user_id, info)
        await query.message.reply_text(f"Пользователь <code>{user_id}</code> заблокирован.", parse_mode=ParseMode.HTML)
        return

    if action == "unblock":
        user_id = int(value)
        unblock_user(user_id)

        await query.message.reply_text(f"Пользователь <code>{user_id}</code> разблокирован.", parse_mode=ParseMode.HTML)

        blocked_users = get_blocked_users()
        if blocked_users:
            lines = ["<b>Заблокированные пользователи</b>"]
            for index, user in enumerate(blocked_users, start=1):
                blocked_user_id = user["user_id"]
                lines.append(f"{index}. {stored_user_label(blocked_user_id, user)} — <code>{blocked_user_id}</code>")

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=blocked_keyboard([user["user_id"] for user in blocked_users]),
                disable_web_page_preview=True,
            )
        else:
            await query.edit_message_text("Список заблокированных пуст.")


async def delete_admin_message(context: ContextTypes.DEFAULT_TYPE, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await context.bot.delete_message(context.bot_data["admin_id"], message_id)
    except BadRequest:
        pass


def main() -> None:
    load_env()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env.")

    application = Application.builder().token(token).build()
    application.bot_data["admin_id"] = get_admin_id()
    init_db()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("blocked", blocked))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(
        MessageHandler(filters.Chat(application.bot_data["admin_id"]) & ~filters.COMMAND, handle_admin_message)
    )
    application.add_handler(MessageHandler(~filters.COMMAND, handle_suggestion))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
