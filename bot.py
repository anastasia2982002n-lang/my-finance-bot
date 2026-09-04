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

# Токен берется из настроек сервера Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "СЮДА_ВСТАВИТЬ_ТОКЕН")
DB_NAME = "finance_bot.db"

logging.basicConfig(level=logging.INFO)

DEFAULT_CATEGORIES = [
    "Продукты",
    "Транспорт",
    "Красота",
    "Кафе",
    "Развлечения",
    "Кошка",
    "Одежда",
]

KEYWORD_MAPPING = {
    "Кошка": ["кошк", "кот", "корм", "ветеринар", "наполнитель", "вискас"],
    "Продукты": ["продукт", "еда", "пятерочк", "магнит", "хлеб", "молок", "мясо", "перекресток"],
    "Транспорт": ["такси", "метро", "автобус", "бензин", "проезд", "заправк", "uber", "яндекс"],
    "Кафе": ["кафе", "ресторан", "кофе", "пицц", "бургер", "макдон", "обед", "ланч"],
    "Красота": ["стрижк", "маникюр", "ногти", "косметик", "парикмахер", "барбер", "бров"],
    "Развлечения": ["кино", "театр", "концерт", "игр", "книг", "боулинг", "подписк"],
    "Одежда": ["одежд", "кроссовк", "куртк", "джинс", "обувь", "футболк", "zara"],
}

class Form(StatesGroup):
    waiting_for_category_name = State()

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

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="➕ Добавить категорию"), KeyboardButton(text="📂 Мои категории")]
        ],
        resize_keyboard=True
    )

def build_categories_inline_keyboard(categories, amount: float):
    buttons = []
    for cat_id, name in categories:
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"add_{cat_id}_{amount}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await ensure_default_categories(message.from_user.id)
    text = (
        "👋 Привет! Я бот для учета финансов.\n\n"
        "💡 **Как вносить расходы:**\n"
        "• Число: `200` — выберите категорию кнопкой.\n"
        "• Текст: `200 рублей на корм кошке` или `500 такси` — определю сам!\n\n"
        "Внизу есть кнопки для статистики и добавления категорий."
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

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

    report.append(f"\n💰 **Итого расходов:** {total:.2f} руб.")
    await message.answer("\n".join(report), parse_mode="Markdown")

@dp.message(F.text == "📂 Мои категории")
async def list_categories(message: types.Message):
    await ensure_default_categories(message.from_user.id)
    categories = await get_user_categories(message.from_user.id)
    cat_names = [f"• {name}" for _, name in categories]
    await message.answer("📂 **Ваши категории:**\n\n" + "\n".join(cat_names), parse_mode="Markdown")

@dp.message(F.text == "➕ Добавить категорию")
async def start_add_category(message: types.Message, state: FSMContext):
    await message.answer("Введите название новой категории:")
    await state.set_state(Form.waiting_for_category_name)

@dp.message(Form.waiting_for_category_name)
async def process_category_name(message: types.Message, state: FSMContext):
    cat_name = message.text.strip().capitalize()
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, cat_name))
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Категория **«{cat_name}»** добавлена!", reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text)
async def handle_expense_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text.lower()

    match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not match:
        await message.answer("Укажите сумму, например: `300` или `300 кафе`.", parse_mode="Markdown")
        return

    amount_str = match.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return

    await ensure_default_categories(user_id)
    categories = await get_user_categories(user_id)

    remainder = re.sub(r'(\d+(?:[.,]\d+)?)', '', text)
    remainder = re.sub(r'\b(руб|рубл|рублей|р)\b', '', remainder).strip()

    matched_category = None
    if remainder:
        for cat_name, keywords in KEYWORD_MAPPING.items():
            for kw in keywords:
                if kw in remainder:
                    if any(c[1].lower() == cat_name.lower() for c in categories):
                        matched_category = cat_name
                        break
            if matched_category:
                break

        if not matched_category:
            for _, cat_name in categories:
                if cat_name.lower() in remainder:
                    matched_category = cat_name
                    break

    if matched_category:
        await add_expense(user_id, matched_category, amount, description=message.text)
        await message.answer(f"✅ Записано: **{amount:.2f} руб.** в категорию **«{matched_category}»**", parse_mode="Markdown")
    else:
        keyboard = build_categories_inline_keyboard(categories, amount)
        await message.answer(f"Куда отнести **{amount:.2f} руб.**?", reply_markup=keyboard, parse_mode="Markdown")

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
    await callback.message.edit_text(f"✅ Записано: **{amount:.2f} руб.** в категорию **«{category_name}»**", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "cancel")
async def callback_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено.")
    await callback.answer()

# Простой веб-сервер для Render
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
