import asyncio
import logging
import os
import re
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8594928547:AAEBswHuJYtFWwjKSUAhb4Jx_LFyOmIEJ4M")
DB_NAME = "finance_bot.db"

logging.basicConfig(level=logging.INFO)

DEFAULT_CATEGORIES = [
    "🛒 Продукты",
    "🚕 Транспорт",
    "💅 Красота",
    "☕ Кафе",
    "🎬 Развлечения",
    "🐱 Кошка",
    "👗 Одежда",
]

# Состояния для FSM (добавление категории и редактирование суммы)
class Form(StatesGroup):
    waiting_for_category_name = State()
    waiting_for_new_amount = State()

# Форматирование даты из БД
def format_datetime(dt_str: str) -> str:
    try:
        parts = dt_str.split(" ")
        date_parts = parts[0].split("-")
        time_part = parts[1][:5]
        return f"{date_parts[2]}.{date_parts[1]}.{date_parts[0]} {time_part}"
    except Exception:
        return dt_str

def format_short_date(dt_str: str) -> str:
    try:
        parts = dt_str.split(" ")
        date_parts = parts[0].split("-")
        time_part = parts[1][:5]
        return f"{date_parts[2]}.{date_parts[1]} {time_part}"
    except Exception:
        return dt_str


# ---------------- БАЗА ДАННЫХ ----------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category_name TEXT,
                amount REAL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def ensure_default_categories(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
        if count == 0:
            for cat in DEFAULT_CATEGORIES:
                await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, cat))
            await db.commit()

async def get_user_categories(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchall()

async def add_expense(user_id: int, category_name: str, amount: float, description: str = ""):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO expenses (user_id, category_name, amount, description) VALUES (?, ?, ?, ?)",
            (user_id, category_name, amount, description)
        )
        await db.commit()


# ---------------- КЛАВИАТУРЫ ----------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📂 Мои категории"), KeyboardButton(text="➕ Добавить категорию")]
        ],
        resize_keyboard=True
    )

def build_categories_add_keyboard(categories, amount: float):
    buttons = []
    row = []
    for cat_id, name in categories:
        row.append(InlineKeyboardButton(text=name, callback_data=f"add_{cat_id}_{amount}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_categories_view_keyboard(categories):
    buttons = []
    row = []
    for cat_id, name in categories:
        row.append(InlineKeyboardButton(text=name, callback_data=f"vcat_{cat_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------- ХЭНДЛЕРЫ ----------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await ensure_default_categories(message.from_user.id)
    text = (
        "👋 Привет! Я твой бот учета расходов.\n\n"
        "💡 **Как пользоваться:**\n"
        "1. **Внести расход:** просто отправь сумму (например, `350`) и нажми на категорию.\n"
        "2. **Посмотреть/удалить/изменить:** нажми кнопку **📂 Мои категории**, выбери категорию и нажми на любую сумму!\n\n"
        "Статистика всегда доступна по кнопке **📊 Статистика**."
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# Просмотр статистики
@dp.message(F.text == "📊 Статистика")
async def show_stats(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT category_name, SUM(amount) FROM expenses WHERE user_id = ? GROUP BY category_name ORDER BY SUM(amount) DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("У вас пока нет записанных расходов.")
        return

    total = sum(row[1] for row in rows)
    report = ["📊 **Ваша статистика расходов:**\n"]
    for cat_name, sum_amount in rows:
        percent = (sum_amount / total) * 100
        report.append(f"• **{cat_name}**: {sum_amount:.2f} руб. ({percent:.1f}%)")

    report.append(f"\n💰 **Всего потрачено:** {total:.2f} руб.")
    await message.answer("\n".join(report), parse_mode="Markdown")

# Список категорий для просмотра трат
@dp.message(F.text == "📂 Мои категории")
async def list_categories(message: types.Message):
    await ensure_default_categories(message.from_user.id)
    categories = await get_user_categories(message.from_user.id)
    keyboard = build_categories_view_keyboard(categories)
    await message.answer(
        "📂 **Ваши категории:**\nВыберите категорию, чтобы посмотреть историю трат, изменить или удалить сумму:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# Добавление новой категории
@dp.message(F.text == "➕ Добавить категорию")
async def start_add_category(message: types.Message, state: FSMContext):
    await message.answer("Введите название новой категории (можно сразу добавить смайлик):")
    await state.set_state(Form.waiting_for_category_name)

@dp.message(Form.waiting_for_category_name)
async def process_category_name(message: types.Message, state: FSMContext):
    cat_name = message.text.strip()
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, cat_name))
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Категория **«{cat_name}»** сохранена!", reply_markup=get_main_keyboard(), parse_mode="Markdown")

# Обработка ввода суммы для записи
@dp.message(F.text)
async def handle_expense_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not match:
        await message.answer("Напишите сумму расхода (например, `450`), чтобы выбрать категорию.", parse_mode="Markdown")
        return

    amount_str = match.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return

    await ensure_default_categories(user_id)
    categories = await get_user_categories(user_id)
    keyboard = build_categories_add_keyboard(categories, amount)
    await message.answer(f"Куда записать **{amount:.2f} руб.**?", reply_markup=keyboard, parse_mode="Markdown")

# Запись суммы в категорию
@dp.callback_query(F.data.startswith("add_"))
async def callback_add_expense(callback: types.CallbackQuery):
    _, cat_id, amount = callback.data.split("_")
    cat_id = int(cat_id)
    amount = float(amount)
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.message.edit_text("Ошибка: категория не найдена.")
                return
            category_name = row[0]

    await add_expense(user_id, category_name, amount)
    await callback.message.edit_text(f"✅ Записано: **{amount:.2f} руб.** в категорию **{category_name}**", parse_mode="Markdown")
    await callback.answer()

# Возврат ко всем категориям
@dp.callback_query(F.data == "b_cats")
async def callback_back_to_cats(callback: types.CallbackQuery):
    categories = await get_user_categories(callback.from_user.id)
    keyboard = build_categories_view_keyboard(categories)
    await callback.message.edit_text(
        "📂 **Ваши категории:**\nВыберите категорию для просмотра трат:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# Просмотр расходов внутри конкретной категории
@dp.callback_query(F.data.startswith("vcat_"))
async def callback_view_category(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            cat_row = await cursor.fetchone()
            if not cat_row:
                await callback.message.edit_text("Категория не найдена.")
                return
            category_name = cat_row[0]

        async with db.execute(
            "SELECT id, amount, created_at FROM expenses WHERE user_id = ? AND category_name = ? ORDER BY id DESC LIMIT 15",
            (user_id, category_name)
        ) as cursor:
            expenses = await cursor.fetchall()

    if not expenses:
        buttons = [[InlineKeyboardButton(text="◀️ Назад ко всем категориям", callback_data="b_cats")]]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(f"В категории **{category_name}** пока нет расходов.", reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    buttons = []
    for exp_id, amount, created_at in expenses:
        date_short = format_short_date(created_at)
        btn_text = f"{amount:.2f} руб.  ({date_short})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"exp_{exp_id}_{cat_id}")])

    buttons.append([InlineKeyboardButton(text="◀️ Назад ко всем категориям", callback_data="b_cats")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"📂 Расходы в категории **{category_name}**:\n*Нажмите на любую запись, чтобы изменить или удалить:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# Просмотр конкретного расхода (карточка действия)
@dp.callback_query(F.data.startswith("exp_"))
async def callback_expense_details(callback: types.CallbackQuery):
    _, exp_id, cat_id = callback.data.split("_")
    exp_id = int(exp_id)
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT category_name, amount, created_at FROM expenses WHERE id = ? AND user_id = ?", (exp_id, user_id)) as cursor:
            exp = await cursor.fetchone()

    if not exp:
        await callback.message.edit_text("Расход не найден или уже удален.")
        return

    cat_name, amount, created_at = exp
    date_formatted = format_datetime(created_at)

    text = (
        f"🧾 **Детали расхода:**\n\n"
        f"• Категория: **{cat_name}**\n"
        f"• Сумма: **{amount:.2f} руб.**\n"
        f"• Дата: **{date_formatted}**\n\n"
        f"Выберите действие:"
    )

    buttons = [
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{exp_id}_{cat_id}"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"del_{exp_id}_{cat_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"vcat_{cat_id}")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# Удаление расхода
@dp.callback_query(F.data.startswith("del_"))
async def callback_delete_expense(callback: types.CallbackQuery):
    _, exp_id, cat_id = callback.data.split("_")
    exp_id = int(exp_id)
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (exp_id, user_id))
        await db.commit()

    buttons = [[InlineKeyboardButton(text="◀️ Вернуться к расходам", callback_data=f"vcat_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("🗑️ **Расход успешно удален!**", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# Начало редактирования расхода
@dp.callback_query(F.data.startswith("edit_"))
async def callback_edit_expense(callback: types.CallbackQuery, state: FSMContext):
    _, exp_id, cat_id = callback.data.split("_")
    await state.update_data(exp_id=int(exp_id), cat_id=int(cat_id))
    await state.set_state(Form.waiting_for_new_amount)

    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"exp_{exp_id}_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text("Введите новую сумму для этого расхода:", reply_markup=keyboard)
    await callback.answer()

# Прием новой суммы расхода
@dp.message(Form.waiting_for_new_amount)
async def process_new_amount(message: types.Message, state: FSMContext):
    text = message.text
    match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not match:
        await message.answer("Пожалуйста, введите корректное число (например, `500`):")
        return

    new_amount = float(match.group(1).replace(",", "."))
    data = await state.get_data()
    exp_id = data.get("exp_id")
    cat_id = data.get("cat_id")
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE expenses SET amount = ? WHERE id = ? AND user_id = ?", (new_amount, exp_id, user_id))
        await db.commit()

    await state.clear()
    buttons = [[InlineKeyboardButton(text="◀️ Вернуться к списку расходов", callback_data=f"vcat_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"✅ Сумма успешно изменена на **{new_amount:.2f} руб.**!", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data == "cancel")
async def callback_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено.")
    await callback.answer()

# Сервер активности для Render
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await init_db()
    await start_web_server()
    print(">>> Бот запущен на Render! <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
