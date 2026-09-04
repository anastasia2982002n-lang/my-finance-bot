import asyncio
import csv
import datetime
import logging
import os
import re
import shutil
import tempfile
import aiosqlite
import openpyxl
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, StateFilter
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

MONTH_NAMES = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}

PENDING_EXPENSES = {}

class Form(StatesGroup):
    waiting_for_category_name = State()
    waiting_for_new_cat_name = State()
    waiting_for_new_amount = State()
    waiting_for_expense_desc = State()

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

def get_month_title(ym_str: str) -> str:
    try:
        year, month = ym_str.split("-")
        return f"{MONTH_NAMES.get(month, month)} {year}"
    except Exception:
        return ym_str

def normalize_date_string(val):
    if not val:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).replace("T", " ").replace("Z", "").strip()
    match = re.search(r'(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})', s)
    if match:
        p1, p2, p3 = match.groups()
        if len(p1) == 4:
            return f"{p1}-{int(p2):02d}-{int(p3):02d} 12:00:00"
        elif len(p3) == 4:
            return f"{p3}-{int(p2):02d}-{int(p1):02d} 12:00:00"
    if len(s) >= 19:
        return s[:19]
    return s


# ---------------- БАЗА ДАННЫХ ----------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category_name TEXT, amount REAL, description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
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

async def add_expense(user_id: int, category_name: str, amount: float, description: str = "", created_at: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        if created_at:
            await db.execute(
                "INSERT INTO expenses (user_id, category_name, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, category_name, amount, description, created_at)
            )
        else:
            await db.execute(
                "INSERT INTO expenses (user_id, category_name, amount, description) VALUES (?, ?, ?, ?)",
                (user_id, category_name, amount, description)
            )
        await db.commit()

async def fetch_month_stats(user_id: int, ym_period: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        if ym_period and ym_period != "all":
            query = "SELECT category_name, SUM(amount) FROM expenses WHERE user_id = ? AND strftime('%Y-%m', created_at) = ? GROUP BY category_name ORDER BY SUM(amount) DESC"
            params = (user_id, ym_period)
        else:
            query = "SELECT category_name, SUM(amount) FROM expenses WHERE user_id = ? GROUP BY category_name ORDER BY SUM(amount) DESC"
            params = (user_id,)

        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


# ---------------- УНИВЕРСАЛЬНЫЙ ПАРСИНГ EXCEL И CSV ----------------
def parse_excel_or_csv(file_path):
    rows_data = []

    # 1. Читаем файл (CSV или XLSX)
    if file_path.lower().endswith(".csv"):
        for enc in ["utf-8-sig", "utf-8", "cp1251"]:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    sample = f.read(2048)
                    f.seek(0)
                    delimiter = ";" if ";" in sample else ","
                    reader = csv.reader(f, delimiter=delimiter)
                    rows_data = [row for row in reader if any(cell.strip() for cell in row)]
                if rows_data:
                    break
            except Exception:
                continue
    else:
        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            sheet = wb.active
            for row in sheet.iter_rows(values_only=True):
                if any(row):
                    rows_data.append([str(c) if c is not None else "" for c in row])
        except Exception as e:
            logging.error(f"Excel read error: {e}")

    if not rows_data or len(rows_data) < 2:
        return []

    # 2. Ищем заголовок таблицы
    header_idx = -1
    col_date = -1
    col_cat = -1
    col_amt = -1
    col_type = -1
    col_desc = -1

    for idx, row in enumerate(rows_data[:5]):
        lower_row = [str(c).lower().strip() for c in row]
        for c_i, cell in enumerate(lower_row):
            if any(w in cell for w in ["дат", "date", "time", "время"]):
                col_date = c_i
            elif any(w in cell for w in ["категор", "category", "статья"]):
                col_cat = c_i
            elif any(w in cell for w in ["сумм", "amount", "цена", "расход", "стоимость", "money", "sum"]):
                col_amt = c_i
            elif any(w in cell for w in ["тип", "type", "вид"]):
                col_type = c_i
            elif any(w in cell for w in ["примечан", "комментар", "описан", "заметк", "note", "comment", "desc", "memo"]):
                col_desc = c_i

        if col_date != -1 and col_cat != -1 and col_amt != -1:
            header_idx = idx
            break

    # Если точных заголовков не нашли, пробуем стандартную расстановку
    if header_idx == -1:
        header_idx = 0
        col_date = 0
        col_cat = 1
        col_amt = 2

    # 3. Извлекаем расходы
    expenses = []
    for row in rows_data[header_idx + 1:]:
        if len(row) <= max(col_date, col_cat, col_amt):
            continue

        raw_type = str(row[col_type]).lower().strip() if (col_type != -1 and col_type < len(row)) else ""
        if any(w in raw_type for w in ["доход", "income", "перевод", "transfer"]):
            continue

        raw_cat = str(row[col_cat]).strip()
        raw_amt = str(row[col_amt]).strip()
        raw_date = row[col_date]
        raw_desc = str(row[col_desc]).strip() if (col_desc != -1 and col_desc < len(row)) else ""

        if not raw_cat or not raw_amt:
            continue

        clean_amt_str = re.sub(r"[^\d.,\-]", "", raw_amt).replace(",", ".")
        try:
            amt = abs(float(clean_amt_str))
            if amt == 0:
                continue
        except ValueError:
            continue

        dt_str = normalize_date_string(raw_date)
        expenses.append((raw_cat, amt, raw_desc, dt_str))

    return expenses


# ---------------- КЛАВИАТУРЫ ----------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Текущий месяц"), KeyboardButton(text="📅 Выбрать месяц")],
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
        "👋 Привет! Я твой бот учета финансов с умной аналитикой.\n\n"
        "💡 **Как вносить траты:**\n"
        "• С названием товара: `420 шампунь` или `890 корм` ➔ выбери категорию.\n"
        "• Или просто сумму: `350` ➔ категорию выбери кнопкой.\n\n"
        "📁 **Импорт истории:** отправьте сюда файл Excel (`.xlsx`) или `.csv`, выгруженный из вашего приложения!"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# Команда для полной очистки трат
@dp.message(F.text == "/clear_expenses")
async def cmd_clear_expenses(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
        await db.commit()
    await message.answer("🧹 База расходов полностью очищена.")

# ПРИЕМ ФАЙЛОВ EXCEL И CSV
@dp.message(F.document)
async def handle_excel_document(message: types.Message):
    doc = message.document
    fname = doc.file_name.lower()
    user_id = message.from_user.id

    if not (fname.endswith(".xlsx") or fname.endswith(".xls") or fname.endswith(".csv")):
        await message.answer("Пожалуйста, отправьте файл таблицы в формате **`.xlsx`** или **`.csv`**.")
        return

    # Автоматически убираем тестовую категорию "Другое"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM expenses WHERE user_id = ? AND category_name = '📦 Другое'", (user_id,))
        await db.execute("DELETE FROM categories WHERE user_id = ? AND name = '📦 Другое'", (user_id,))
        await db.commit()

    status_msg = await message.answer("⏳ Читаю таблицу Excel и переношу расходы...")
    temp_dir = tempfile.mkdtemp()

    try:
        file_info = await bot.get_file(doc.file_id)
        local_path = os.path.join(temp_dir, doc.file_name)
        await bot.download_file(file_info.file_path, local_path)

        expenses = parse_excel_or_csv(local_path)

        if not expenses:
            await status_msg.edit_text("❌ В таблице не удалось распознать строки с расходами. Проверьте, есть ли в файле колонки с датой, категорией и суммой.")
            return

        await ensure_default_categories(user_id)
        existing_cats = {name.lower(): name for _, name in await get_user_categories(user_id)}
        imported_cats = set(e[0] for e in expenses)

        async with aiosqlite.connect(DB_NAME) as db:
            for c_name in imported_cats:
                if c_name.lower() not in existing_cats:
                    await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, c_name))
                    existing_cats[c_name.lower()] = c_name

            for cat_name, amt, desc, dt_str in expenses:
                final_cat = existing_cats.get(cat_name.lower(), cat_name)
                await db.execute(
                    "INSERT INTO expenses (user_id, category_name, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, final_cat, amt, desc, dt_str)
                )
            await db.commit()

        total_sum = sum(e[1] for e in expenses)
        dates = [e[3][:10] for e in expenses if len(e[3]) >= 10]
        date_range = f"с {min(dates)} по {max(dates)}" if dates else ""

        report_text = (
            f"🎉 **Импорт из Excel успешно завершен!**\n\n"
            f"• Перенесено трат: **{len(expenses)}**\n"
            f"• Категорий: **{len(imported_cats)}**\n"
