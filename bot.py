import html
import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

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


def delete_suggestion(suggestion_id: str) -> Optional[dict]:
    suggestion = get_suggestion(suggestion_id)
    if not suggestion:
        return None

    with get_db() as connection:
        connection.execute("DELETE FROM suggestions WHERE suggestion_id = ?", (suggestion_id,))
    return suggestion


# Define FSM States for Admin
class AdminStates(StatesGroup):
    waiting_for_reply = State()


# Helper to get Admin ID
def get_admin_id() -> int:
    admin_id = os.getenv("ADMIN_ID")
    if not admin_id or not admin_id.isdigit():
        raise RuntimeError("Поставь ADMIN_ID в .env.")
    return int(admin_id)


def user_link(user_id: int, full_name: str, username: Optional[str]) -> str:
    name = html.escape(full_name or str(user_id))
    profile = f'<a href="tg://user?id={user_id}">{name}</a>'
    if username:
        return f'{profile} (@{html.escape(username)})'
    return profile


def is_anonymous_info(info: Optional[dict]) -> bool:
    return bool(info and info.get("full_name") == "Аноним" and not info.get("username"))


def stored_user_label(user_id: int, info: Optional[dict]) -> str:
    if is_anonymous_info(info):
        return "Анонимный пользователь"

    if not info:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'

    return user_link(user_id, info.get("full_name") or str(user_id), info.get("username"))


# Keyboards Builder
def get_privacy_keyboard(message_id: int) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Анонимно", callback_data=f"privacy:anonymous:{message_id}")
    builder.button(text="🙋‍♂️ С именем и ID", callback_data=f"privacy:public:{message_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_keyboard(suggestion_id: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🙋‍♂️ Ответить", callback_data=f"reply:{suggestion_id}")
    builder.button(text="❌ Удалить", callback_data=f"delete:{suggestion_id}")
    builder.button(text="🤬 Заблокировать", callback_data=f"block:{suggestion_id}")
    builder.adjust(1, 2)
    return builder.as_markup()


def get_admin_panel_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Заблокированные", callback_data="panel:blocked")
    builder.button(text="↩️ Отменить ответ", callback_data="panel:cancel")
    builder.button(text="ℹ️ Как работать", callback_data="panel:help")
    builder.adjust(1)
    return builder.as_markup()


def get_blocked_keyboard(blocked_users: List[dict]) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in blocked_users:
        label = "Разблокировать анонима" if is_anonymous_info(user) else f"Разблокировать {user['user_id']}"
        builder.button(text=label, callback_data=f"unblock:{user['user_id']}")
    builder.adjust(1)
    return builder.as_markup()


def admin_panel_text() -> str:
    return (
        "<b>Админ-панель</b>\n\n"
        "Доступные действия:\n"
        "• смотреть и разблокировать пользователей;\n"
        "• отменять режим ответа;\n"
        "• отвечать, удалять и блокировать через кнопки под каждой предложкой."
    )


# Router creation
router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    admin_id = get_admin_id()
    if message.from_user.id == admin_id:
        await message.answer(
            "Бот предложки запущен. Пользователи могут присылать сюда текст, фото, видео, документы, голосовые и другой контент."
        )
        return

    await message.answer(
        "📤 Отправь сюда текст, картинку, видео, документ или другой контент для предложки. "
        "После каждого сообщения бот спросит, показывать админу твое имя и ID или отправить анонимно."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    admin_id = get_admin_id()
    if message.from_user.id != admin_id:
        return

    await state.clear()
    await message.answer("Режим ответа отменен.")


@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    admin_id = get_admin_id()
    if message.from_user.id != admin_id:
        return

    await message.answer(
        admin_panel_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_panel_keyboard(),
    )


async def send_blocked_users_list(message: types.Message):
    blocked_users = get_blocked_users()
    if not blocked_users:
        await message.answer("Список заблокированных пуст.")
        return

    lines = ["<b>Заблокированные пользователи</b>"]
    for index, user in enumerate(blocked_users, start=1):
        user_id = user["user_id"]
        if is_anonymous_info(user):
            lines.append(f"{index}. {stored_user_label(user_id, user)}")
        else:
            lines.append(f"{index}. {stored_user_label(user_id, user)} — <code>{user_id}</code>")

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=get_blocked_keyboard(blocked_users),
        disable_web_page_preview=True,
    )


@router.message(Command("blocked"))
async def cmd_blocked(message: types.Message):
    admin_id = get_admin_id()
    if message.from_user.id != admin_id:
        return

    await send_blocked_users_list(message)


# Handlers for admin FSM states
@router.message(AdminStates.waiting_for_reply)
async def handle_admin_reply(message: types.Message, state: FSMContext, bot: Bot):
    admin_id = get_admin_id()
    if message.from_user.id != admin_id:
        return

    state_data = await state.get_data()
    user_id = state_data.get("reply_to_user_id")

    if not user_id:
        await message.answer("Пользователь для ответа не найден. Попробуй нажать кнопку «Ответить» еще раз.")
        await state.clear()
        return

    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await message.answer("Ответ отправлен пользователю.")
        await state.clear()
    except TelegramAPIError as e:
        if "Forbidden" in str(e) or "blocked" in str(e).lower():
            await message.answer("Не удалось отправить ответ: пользователь заблокировал бота.")
        else:
            await message.answer(f"Не удалось отправить ответ: {e}")
        await state.clear()


# Fallback for Admin
@router.message(F.chat.id == F.bot.id)  # placeholder / checking if admin messages in private
async def admin_messages(message: types.Message, state: FSMContext):
    # This gets handled below after suggestion handler if not admin
    pass


# Callback Handlers
@router.callback_query(F.data.startswith("privacy:"))
async def on_privacy_choice(callback: types.CallbackQuery, bot: Bot):
    user = callback.from_user
    if is_blocked(user.id):
        await callback.answer("Ты заблокирован в предложке.", show_alert=True)
        await callback.message.edit_text("Ты заблокирован в предложке.")
        return

    parts = callback.data.split(":")
    privacy_mode = parts[1]
    message_id = int(parts[2])

    admin_id = get_admin_id()
    suggestion_id = uuid4().hex[:10]

    try:
        # Copy user suggestion to admin
        copied = await bot.copy_message(
            chat_id=admin_id,
            from_chat_id=callback.message.chat.id,
            message_id=message_id,
        )
    except TelegramAPIError as e:
        await callback.message.edit_text("Не удалось отправить предложку админу. Попробуй позже.")
        await bot.send_message(admin_id, f"Ошибка при копировании предложки: {e}")
        await callback.answer()
        return

    if privacy_mode == "public":
        panel_text = (
            "<b>Новая предложка</b>\n"
            f"Отправитель: {user_link(user.id, user.full_name, user.username)}\n"
            f"ID: <code>{user.id}</code>"
        )
        info = {"full_name": user.full_name or str(user.id), "username": user.username}
    else:
        panel_text = "<b>Новая предложка</b>\nОтправитель: анонимно"
        info = {"full_name": "Аноним", "username": None}

    # Send admin options panel
    panel = await bot.send_message(
        chat_id=admin_id,
        text=panel_text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_keyboard(suggestion_id),
        disable_web_page_preview=True,
    )

    save_suggestion(
        suggestion_id=suggestion_id,
        user_id=user.id,
        info=info,
        admin_content_message_id=copied.message_id,
        admin_panel_message_id=panel.message_id,
    )

    await callback.message.edit_text("Предложка отправлена админу.")
    await callback.answer()


@router.callback_query(F.data.startswith("reply:"))
async def on_admin_reply_btn(callback: types.CallbackQuery, state: FSMContext):
    admin_id = get_admin_id()
    if callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для админа.", show_alert=True)
        return

    suggestion_id = callback.data.split(":")[1]
    suggestion = get_suggestion(suggestion_id)
    if not suggestion:
        await callback.message.edit_text("Предложка не найдена. Возможно, бот был перезапущен со старой кнопкой.")
        await callback.answer()
        return

    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(reply_to_user_id=suggestion["user_id"])
    await callback.message.answer("Отправь следующее сообщение, и оно уйдет пользователю. /cancel — отмена.")
    await callback.answer()


@router.callback_query(F.data.startswith("delete:"))
async def on_admin_delete_btn(callback: types.CallbackQuery, bot: Bot):
    admin_id = get_admin_id()
    if callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для админа.", show_alert=True)
        return

    suggestion_id = callback.data.split(":")[1]
    suggestion = delete_suggestion(suggestion_id)
    if not suggestion:
        await callback.message.edit_text("Предложка уже удалена или не найдена.")
        await callback.answer()
        return

    # Delete content message
    if suggestion.get("admin_content_message_id"):
        try:
            await bot.delete_message(admin_id, suggestion["admin_content_message_id"])
        except TelegramAPIError:
            pass

    await callback.message.edit_text("Предложка удалена.")
    await callback.answer()


@router.callback_query(F.data.startswith("block:"))
async def on_admin_block_btn(callback: types.CallbackQuery):
    admin_id = get_admin_id()
    if callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для админа.", show_alert=True)
        return

    suggestion_id = callback.data.split(":")[1]
    suggestion = get_suggestion(suggestion_id)
    if not suggestion:
        await callback.message.edit_text("Предложка не найдена.")
        await callback.answer()
        return

    user_id = suggestion["user_id"]
    is_anon = is_anonymous_info(suggestion)
    info = {
        "full_name": suggestion["full_name"],
        "username": suggestion["username"],
    }
    block_user(user_id, info)

    if is_anon:
        await callback.message.answer("Анонимный отправитель заблокирован.")
    else:
        await callback.message.answer(f"Пользователь {user_id} заблокирован.")
    await callback.answer()


@router.callback_query(F.data.startswith("unblock:"))
async def on_admin_unblock_btn(callback: types.CallbackQuery):
    admin_id = get_admin_id()
    if callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для админа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    blocked_list = get_blocked_users()
    blocked_info = next((u for u in blocked_list if u["user_id"] == user_id), None)
    unblock_user(user_id)

    if is_anonymous_info(blocked_info):
        await callback.message.answer("Анонимный пользователь разблокирован.")
    else:
        await callback.message.answer(f"Пользователь {user_id} разблокирован.")

    # Update panel
    blocked_users = get_blocked_users()
    if blocked_users:
        lines = ["<b>Заблокированные пользователи</b>"]
        for index, user in enumerate(blocked_users, start=1):
            blocked_user_id = user["user_id"]
            if is_anonymous_info(user):
                lines.append(f"{index}. {stored_user_label(blocked_user_id, user)}")
            else:
                lines.append(f"{index}. {stored_user_label(blocked_user_id, user)} — <code>{blocked_user_id}</code>")

        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=get_blocked_keyboard(blocked_users),
            disable_web_page_preview=True,
        )
    else:
        await callback.message.edit_text("Список заблокированных пуст.")
    await callback.answer()


@router.callback_query(F.data.startswith("panel:"))
async def on_admin_panel_btn(callback: types.CallbackQuery, state: FSMContext):
    admin_id = get_admin_id()
    if callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для админа.", show_alert=True)
        return

    action = callback.data.split(":")[1]

    if action == "blocked":
        await send_blocked_users_list(callback.message)
    elif action == "cancel":
        await state.clear()
        await callback.message.answer("Режим ответа отменен.")
    elif action == "help":
        await callback.message.answer(admin_panel_text(), parse_mode=ParseMode.HTML)

    await callback.answer()


# Suggestions Handler
@router.message(F.chat.type == "private")
async def handle_user_suggestion(message: types.Message):
    admin_id = get_admin_id()
    if message.from_user.id == admin_id:
        # Admin sending general text without being in waiting_for_reply state
        if not message.text or not message.text.startswith("/"):
            await message.answer("Нажми кнопку «Ответить» под предложкой, потом отправь ответ.")
        return

    if is_blocked(message.from_user.id):
        await message.answer("Ты заблокирован в предложке.")
        return

    # Prompt user for anonymity choice
    await message.answer(
        "Как отправить эту предложку админу?",
        reply_markup=get_privacy_keyboard(message.message_id),
    )


async def main() -> None:
    load_env()
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env.")

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
