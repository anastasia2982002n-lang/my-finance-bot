import asyncio
import csv
import datetime
import logging
import os
import re
import shutil
import tempfile
import asyncpg
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

raw_token = os.getenv("BOT_TOKEN", "")
BOT_TOKEN = raw_token.strip().replace(" ", "")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip().replace("postgres://", "postgresql://")

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
db_pool = None

class Form(StatesGroup):
    waiting_for_category_name = State()
    waiting_for_new_cat_name = State()
    waiting_for_new_amount = State()
    waiting_for_expense_desc = State()

def format_datetime(dt_val) -> str:
    if isinstance(dt_val, (datetime.datetime, datetime.date)):
        return dt_val.strftime("%d.%m.%Y %H:%M")
    s = str(dt_val)
    try:
        parts = s.split(" ")
        date_parts = parts[0].split("-")
        time_part = parts[1][:5]
        return f"{date_parts[2]}.{date_parts[1]}.{date_parts[0]} {time_part}"
    except Exception:
        return s

def format_short_date(dt_val) -> str:
    if isinstance(dt_val, (datetime.datetime, datetime.date)):
        return dt_val.strftime("%d.%m %H:%M")
    s = str(dt_val)
    try:
        parts = s.split(" ")
        date_parts = parts[0].split("-")
        time_part = parts[1][:5]
        return f"{date_parts[2]}.{date_parts[1]} {time_part}"
    except Exception:
        return s

def get_month_title(ym_str: str) -> str:
    try:
        year, month = ym_str.split("-")
        return f"{MONTH_NAMES.get(month, month)} {year}"
    except Exception:
        return ym_str

def normalize_date_obj(val):
    if not val:
        return datetime.datetime.utcnow()
    if isinstance(val, datetime.datetime):
        return val
    if isinstance(val, datetime.date):
        return datetime.datetime.combine(val, datetime.time(12, 0))
    s = str(val).replace("T", " ").replace("Z", "").strip()
    match = re.search(r'(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})', s)
    if match:
        p1, p2, p3 = match.groups()
        if len(p1) == 4:
            return datetime.datetime(int(p1), int(p2), int(p3), 12, 0)
        elif len(p3) == 4:
            return datetime.datetime(int(p3), int(p2), int(p1), 12, 0)
    try:
        return datetime.datetime.fromisoformat(s[:19])
    except Exception:
        return datetime.datetime.utcnow()


# ---------------- БАЗА ДАННЫХ (POSTGRESQL / NEON) ----------------
async def get_pool():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    return db_pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS categories (id SERIAL PRIMARY KEY, user_id BIGINT, name TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS expenses (id SERIAL PRIMARY KEY, user_id BIGINT, category_name TEXT, amount NUMERIC(12, 2), description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

async def ensure_default_categories(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM categories WHERE user_id = $1", user_id)
        if count == 0:
            for cat in DEFAULT_CATEGORIES:
                await conn.execute("INSERT INTO categories (user_id, name) VALUES ($1, $2)", user_id, cat)

async def get_user_categories(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name FROM categories WHERE user_id = $1 ORDER BY id", user_id)
        return [(r["id"], r["name"]) for r in rows]

async def add_expense(user_id: int, category_name: str, amount: float, description: str = "", created_at=None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if created_at:
            dt_obj = normalize_date_obj(created_at)
            await conn.execute(
                "INSERT INTO expenses (user_id, category_name, amount, description, created_at) VALUES ($1, $2, $3, $4, $5)",
                user_id, category_name, amount, description, dt_obj
            )
        else:
            await conn.execute(
                "INSERT INTO expenses (user_id, category_name, amount, description) VALUES ($1, $2, $3, $4)",
                user_id, category_name, amount, description
            )

async def fetch_month_stats(user_id: int, ym_period: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if ym_period and ym_period != "all":
            query = "SELECT category_name, SUM(amount) as s FROM expenses WHERE user_id = $1 AND TO_CHAR(created_at, 'YYYY-MM') = $2 GROUP BY category_name ORDER BY s DESC"
            rows = await conn.fetch(query, user_id, ym_period)
        else:
            query = "SELECT category_name, SUM(amount) as s FROM expenses WHERE user_id = $1 GROUP BY category_name ORDER BY s DESC"
            rows = await conn.fetch(query, user_id)
        return [(r["category_name"], float(r["s"])) for r in rows]


# ---------------- ПАРСИНГ EXCEL ----------------
def parse_excel_or_csv(file_path):
    expenses = []
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheets = wb.worksheets
    except Exception as e:
        logging.error(f"Excel read error: {e}")
        sheets = []

    for sheet in sheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows or len(rows) < 2:
            continue

        header_idx = -1
        col_date = 0
        col_cat = 1
        col_amt = 3
        col_desc = 8

        for r_i, r in enumerate(rows[:6]):
            r_str = [str(c).lower().strip() if c is not None else "" for c in r]
            for c_i, cell in enumerate(r_str):
                if "дата" in cell:
                    col_date = c_i
                    header_idx = r_i
                elif "категория" in cell:
                    col_cat = c_i
                    header_idx = r_i
                elif "сумма в валюте" in cell:
                    col_amt = c_i
                elif "комментарий" in cell or "теги" in cell:
                    col_desc = c_i
            if header_idx != -1:
                break

        start_row = (header_idx + 1) if header_idx != -1 else 2

        for r in rows[start_row:]:
            if not r or len(r) <= max(col_date, col_cat, col_amt):
                continue

            raw_date = r[col_date]
            raw_cat = r[col_cat]
            raw_amt = r[col_amt]
            raw_desc = r[col_desc] if (col_desc != -1 and col_desc < len(r)) else ""

            if raw_date is None or raw_cat is None or raw_amt is None:
                continue

            cat_str = str(raw_cat).strip()
            if not cat_str or any(cat_str.lower().startswith(w) for w in ["список", "категория", "дата"]):
                continue

            amt_str = str(raw_amt).replace(" ", "").replace("\xa0", "").replace(",", ".").strip()
            match = re.search(r"(\d+(?:\.\d+)?)", amt_str)
            if not match:
                continue
            amt = float(match.group(1))
            if amt == 0:
                continue

            dt_obj = normalize_date_obj(raw_date)
            desc_str = str(raw_desc).strip() if raw_desc is not None else ""
            expenses.append((cat_str, amt, desc_str, dt_obj))

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
    text = "👋 Привет! Я твой бот учета финансов с надежным облачным хранением в PostgreSQL.\n\n💡 **Как вносить траты:**\n• С названием товара: `420 шампунь` или `890 корм` ➔ выбери категорию.\n• Или просто сумму: `350` ➔ категорию выбери кнопкой.\n\n📁 **Импорт истории:** отправьте сюда файл Excel (`.xlsx`), выгруженный из вашего приложения!"
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "/clear_expenses")
async def cmd_clear_expenses(message: types.Message):
    user_id = message.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM expenses WHERE user_id = $1", user_id)
    await message.answer("🧹 База расходов полностью очищена.")

# ПРИЕМ ФАЙЛОВ EXCEL
@dp.message(F.document)
async def handle_excel_document(message: types.Message):
    doc = message.document
    fname = doc.file_name.lower()
    user_id = message.from_user.id

    if not (fname.endswith(".xlsx") or fname.endswith(".xls") or fname.endswith(".csv")):
        await message.answer("Пожалуйста, отправьте файл таблицы в формате **`.xlsx`**.")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM expenses WHERE user_id = $1 AND category_name = '📦 Другое'", user_id)
        await conn.execute("DELETE FROM categories WHERE user_id = $1 AND name = '📦 Другое'", user_id)

    status_msg = await message.answer("⏳ Читаю таблицу Excel и переношу расходы в постоянную базу...")
    temp_dir = tempfile.mkdtemp()

    try:
        file_info = await bot.get_file(doc.file_id)
        local_path = os.path.join(temp_dir, doc.file_name)
        await bot.download_file(file_info.file_path, local_path)

        expenses = parse_excel_or_csv(local_path)

        if not expenses:
            await status_msg.edit_text("❌ В таблице не удалось распознать строки с расходами.")
            return

        await ensure_default_categories(user_id)
        existing_cats = {name.lower(): name for _, name in await get_user_categories(user_id)}
        imported_cats = set(e[0] for e in expenses)

        async with pool.acquire() as conn:
            for c_name in imported_cats:
                if c_name.lower() not in existing_cats:
                    await conn.execute("INSERT INTO categories (user_id, name) VALUES ($1, $2)", user_id, c_name)
                    existing_cats[c_name.lower()] = c_name

            for cat_name, amt, desc, dt_obj in expenses:
                final_cat = existing_cats.get(cat_name.lower(), cat_name)
                await conn.execute(
                    "INSERT INTO expenses (user_id, category_name, amount, description, created_at) VALUES ($1, $2, $3, $4, $5)",
                    user_id, final_cat, amt, desc, dt_obj
                )

        total_sum = sum(e[1] for e in expenses)
        dates = [e[3].strftime("%d.%m.%Y") for e in expenses]
        date_range = f"с {min(dates)} по {max(dates)}" if dates else ""

        report_text = f"🎉 **Импорт из Excel успешно завершен!**\n\n• Перенесено трат: **{len(expenses)}**\n• Категорий: **{len(imported_cats)}**\n• Общая сумма: **{total_sum:.2f} руб.**\n• Период: **{date_range}**\n\nВсе данные сохранены в облачную базу данных навсегда!"
        await status_msg.edit_text(report_text, parse_mode="Markdown")

    except Exception as ex:
        logging.error(f"Excel import error: {ex}")
        await status_msg.edit_text(f"❌ Ошибка при чтении таблицы: {ex}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# Статистика за текущий месяц
@dp.message(F.text == "📊 Текущий месяц")
async def show_current_month_stats(message: types.Message):
    user_id = message.from_user.id
    current_ym = datetime.datetime.now().strftime("%Y-%m")
    month_name = get_month_title(current_ym)

    rows = await fetch_month_stats(user_id, current_ym)
    if not rows:
        await message.answer(f"В этом месяце ({month_name}) расходов пока нет.")
        return

    total = sum(r[1] for r in rows)
    report = [f"📊 **Статистика за {month_name}:**\n"]
    for cat_name, sum_amount in rows:
        percent = (sum_amount / total) * 100
        report.append(f"• **{cat_name}**: {sum_amount:.2f} руб. ({percent:.1f}%)")

    report.append(f"\n💰 **Итого за {month_name}:** {total:.2f} руб.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Выбрать другой месяц", callback_data="b_months")]
    ])
    await message.answer("\n".join(report), reply_markup=keyboard, parse_mode="Markdown")

# Выбор месяца
@dp.message(F.text == "📅 Выбрать месяц")
async def choose_month_menu(message: types.Message):
    user_id = message.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT TO_CHAR(created_at, 'YYYY-MM') as ym FROM expenses WHERE user_id = $1 ORDER BY ym DESC", user_id)

    if not rows:
        await message.answer("У вас пока нет сохраненных расходов.")
        return

    buttons = []
    for r in rows:
        ym = r["ym"]
        title = get_month_title(ym)
        buttons.append([InlineKeyboardButton(text=f"📅 {title}", callback_data=f"mstats_{ym}")])

    buttons.append([InlineKeyboardButton(text="♾ За всё время", callback_data="mstats_all")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("📅 **Выберите период для просмотра статистики:**", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("mstats_"))
async def callback_show_month_stats(callback: types.CallbackQuery):
    period = callback.data.split("_")[1]
    user_id = callback.from_user.id
    period_title = "всё время" if period == "all" else get_month_title(period)

    rows = await fetch_month_stats(user_id, period)
    if not rows:
        await callback.message.edit_text(f"За {period_title} расходов не найдено.")
        await callback.answer()
        return

    total = sum(r[1] for r in rows)
    report = [f"📊 **Статистика за {period_title}:**\n"]
    for cat_name, sum_amount in rows:
        percent = (sum_amount / total) * 100
        report.append(f"• **{cat_name}**: {sum_amount:.2f} руб. ({percent:.1f}%)")

    report.append(f"\n💰 **Итого:** {total:.2f} руб.")
    buttons = [[InlineKeyboardButton(text="◀️ Назад к выбору периода", callback_data="b_months")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("\n".join(report), reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "b_months")
async def callback_back_to_months(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT TO_CHAR(created_at, 'YYYY-MM') as ym FROM expenses WHERE user_id = $1 ORDER BY ym DESC", user_id)

    buttons = []
    for r in rows:
        ym = r["ym"]
        title = get_month_title(ym)
        buttons.append([InlineKeyboardButton(text=f"📅 {title}", callback_data=f"mstats_{ym}")])

    buttons.append([InlineKeyboardButton(text="♾ За всё время", callback_data="mstats_all")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("📅 **Выберите период:**", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.message(F.text == "📂 Мои категории")
async def list_categories(message: types.Message):
    await ensure_default_categories(message.from_user.id)
    categories = await get_user_categories(message.from_user.id)
    keyboard = build_categories_view_keyboard(categories)
    await message.answer("📂 **Ваши категории:**\nВыберите категорию для просмотра трат:", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.text == "➕ Добавить категорию")
async def start_add_category(message: types.Message, state: FSMContext):
    await message.answer("Введите название новой категории:")
    await state.set_state(Form.waiting_for_category_name)

@dp.message(Form.waiting_for_category_name)
async def process_category_name(message: types.Message, state: FSMContext):
    cat_name = message.text.strip()
    user_id = message.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO categories (user_id, name) VALUES ($1, $2)", user_id, cat_name)

    await state.clear()
    await message.answer(f"✅ Категория **«{cat_name}»** сохранена!", reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(Form.waiting_for_new_cat_name)
async def process_rename_category(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    data = await state.get_data()
    cat_id = data.get("cat_id")
    user_id = message.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        old_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)
        if old_name:
            await conn.execute("UPDATE categories SET name = $1 WHERE id = $2 AND user_id = $3", new_name, cat_id, user_id)
            await conn.execute("UPDATE expenses SET category_name = $1 WHERE category_name = $2 AND user_id = $3", new_name, old_name, user_id)

    await state.clear()
    buttons = [[InlineKeyboardButton(text="◀️ Вернуться к категории", callback_data=f"vcat_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"✅ Категория переименована в **«{new_name}»**!", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Form.waiting_for_new_amount)
async def process_new_amount(message: types.Message, state: FSMContext):
    text = message.text
    match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not match:
        await message.answer("Пожалуйста, введите число (например, `450`):")
        return

    new_amount = float(match.group(1).replace(",", "."))
    data = await state.get_data()
    exp_id = data.get("exp_id")
    cat_id = data.get("cat_id")
    user_id = message.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE expenses SET amount = $1 WHERE id = $2 AND user_id = $3", new_amount, exp_id, user_id)
        cat_name = await conn.fetchval("SELECT category_name FROM expenses WHERE id = $1", exp_id)

    await state.clear()
    buttons = [
        [InlineKeyboardButton(text="🧾 К карточке расхода", callback_data=f"exp_{exp_id}_{cat_id}")],
        [InlineKeyboardButton(text="◀️ Ко всем расходам категории", callback_data=f"vcat_{cat_id}")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"✅ Сумма изменена на **{new_amount:.2f} руб.** в категории **{cat_name}**!", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Form.waiting_for_expense_desc)
async def process_new_description(message: types.Message, state: FSMContext):
    new_desc = message.text.strip()
    data = await state.get_data()
    exp_id = data.get("exp_id")
    cat_id = data.get("cat_id")
    user_id = message.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE expenses SET description = $1 WHERE id = $2 AND user_id = $3", new_desc, exp_id, user_id)

    await state.clear()
    buttons = [
        [InlineKeyboardButton(text="🧾 К карточке расхода", callback_data=f"exp_{exp_id}_{cat_id}")],
        [InlineKeyboardButton(text="📈 Аналитика этого товара", callback_data=f"anl_{exp_id}_{cat_id}_ov")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"✅ Название товара сохранено: **«{new_desc}»**!", reply_markup=keyboard, parse_mode="Markdown")

@dp.message(StateFilter(None), F.text)
async def handle_expense_input(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not match:
        await message.answer("Напишите сумму расхода (например, `420 шампунь` или `300`), чтобы выбрать категорию.", parse_mode="Markdown")
        return

    amount_str = match.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return

    raw_desc = text.replace(match.group(0), "", 1).strip()
    clean_desc = re.sub(r'^\s*(руб|рубл[ейя]|р\.?|rub)\s*', '', raw_desc, flags=re.IGNORECASE).strip()
    clean_desc = re.sub(r'\s*(руб|рубл[ейя]|р\.?|rub)\s*$', '', clean_desc, flags=re.IGNORECASE).strip()

    PENDING_EXPENSES[user_id] = {
        "amount": amount,
        "description": clean_desc
    }

    await ensure_default_categories(user_id)
    categories = await get_user_categories(user_id)
    keyboard = build_categories_add_keyboard(categories, amount)

    desc_hint = f" (*{clean_desc}*)" if clean_desc else ""
    await message.answer(f"Куда записать **{amount:.2f} руб.**{desc_hint}?", reply_markup=keyboard, parse_mode="Markdown")


# ---------------- CALLBACKS ----------------

@dp.callback_query(F.data.startswith("add_"))
async def callback_add_expense(callback: types.CallbackQuery):
    _, cat_id, amount_fallback = callback.data.split("_")
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    cached = PENDING_EXPENSES.pop(user_id, None)
    if cached:
        amount = cached["amount"]
        description = cached["description"]
    else:
        amount = float(amount_fallback)
        description = ""

    pool = await get_pool()
    async with pool.acquire() as conn:
        category_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)

    await add_expense(user_id, category_name or "📦 Другое", amount, description)
    desc_text = f" (*{description}*)" if description else ""
    await callback.message.edit_text(f"✅ Записано: **{amount:.2f} руб.**{desc_text} в категорию **{category_name}**", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "b_cats")
async def callback_back_to_cats(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    categories = await get_user_categories(callback.from_user.id)
    keyboard = build_categories_view_keyboard(categories)
    await callback.message.edit_text("📂 **Ваши категории:**", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("vcat_"))
async def callback_view_category(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    cat_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        category_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)
        rows = await conn.fetch(
            "SELECT id, amount, description, created_at FROM expenses WHERE user_id = $1 AND category_name = $2 ORDER BY id DESC LIMIT 15",
            user_id, category_name
        )

    buttons = []
    for r in rows:
        exp_id = r["id"]
        amt = float(r["amount"])
        desc = r["description"]
        dt = r["created_at"]
        date_short = format_short_date(dt)
        desc_label = f" — {desc[:10]}" if desc else ""
        btn_text = f"{amt:.2f} руб.{desc_label} ({date_short})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"exp_{exp_id}_{cat_id}")])

    buttons.append([
        InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rencat_{cat_id}"),
        InlineKeyboardButton(text="🗑️ Удалить категорию", callback_data=f"askdelcat_{cat_id}")
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад ко всем категориям", callback_data="b_cats")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"📂 Категория: **{category_name}** ({len(rows)} записей)\n*Нажмите на расход для изменения, названия товара или аналитики:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("rencat_"))
async def callback_rename_cat(callback: types.CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[1])
    await state.update_data(cat_id=cat_id)
    await state.set_state(Form.waiting_for_new_cat_name)

    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"vcat_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Введите новое название категории:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("askdelcat_"))
async def callback_ask_del_cat(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        cat_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)

    text = f"⚠️ **Удалить категорию «{cat_name}»?**\nВсе записанные расходы внутри нее также будут удалены!"
    buttons = [
        [InlineKeyboardButton(text="🗑️ Да, удалить всё", callback_data=f"confdelcat_{cat_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"vcat_{cat_id}")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("confdelcat_"))
async def callback_confirm_del_cat(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        cat_name = await conn.fetchval("SELECT name FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)
        if cat_name:
            await conn.execute("DELETE FROM categories WHERE id = $1 AND user_id = $2", cat_id, user_id)
            await conn.execute("DELETE FROM expenses WHERE category_name = $1 AND user_id = $2", cat_name, user_id)

    buttons = [[InlineKeyboardButton(text="◀️ Ко всем категориям", callback_data="b_cats")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(f"🗑️ Категория **«{cat_name}»** удалена.", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("exp_"))
async def callback_expense_details(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    _, exp_id, cat_id = callback.data.split("_")
    exp_id = int(exp_id)
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT category_name, amount, description, created_at FROM expenses WHERE id = $1 AND user_id = $2", exp_id, user_id)

    if not row:
        await callback.message.edit_text("Расход не найден.")
        return

    cat_name = row["category_name"]
    amount = float(row["amount"])
    desc = row["description"]
    created_at = row["created_at"]
    date_formatted = format_datetime(created_at)
    desc_text = f"• Товар/услуга: **{desc}**\n" if desc else "• Товар/услуга: *не указано*\n"

    text = f"🧾 **Детали расхода:**\n\n• Категория: **{cat_name}**\n• Сумма: **{amount:.2f} руб.**\n{desc_text}• Дата: **{date_formatted}**\n\nВыберите действие:"

    buttons = [
        [
            InlineKeyboardButton(text="✏️ Изменить сумму", callback_data=f"edit_{exp_id}_{cat_id}"),
            InlineKeyboardButton(text="🏷 Задать название", callback_data=f"tag_{exp_id}_{cat_id}")
        ],
        [
            InlineKeyboardButton(text="📈 Аналитика этого товара", callback_data=f"anl_{exp_id}_{cat_id}_ov"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"del_{exp_id}_{cat_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"vcat_{cat_id}")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("tag_"))
async def callback_tag_expense(callback: types.CallbackQuery, state: FSMContext):
    _, exp_id, cat_id = callback.data.split("_")
    await state.update_data(exp_id=int(exp_id), cat_id=int(cat_id))
    await state.set_state(Form.waiting_for_expense_desc)

    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"exp_{exp_id}_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Введите название товара или услуги (например, `шампунь` или `корм`):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_"))
async def callback_delete_expense(callback: types.CallbackQuery):
    _, exp_id, cat_id = callback.data.split("_")
    exp_id = int(exp_id)
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM expenses WHERE id = $1 AND user_id = $2", exp_id, user_id)

    buttons = [[InlineKeyboardButton(text="◀️ Вернуться к расходам", callback_data=f"vcat_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("🗑️ **Расход удален!**", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_"))
async def callback_edit_expense(callback: types.CallbackQuery, state: FSMContext):
    _, exp_id, cat_id = callback.data.split("_")
    await state.update_data(exp_id=int(exp_id), cat_id=int(cat_id))
    await state.set_state(Form.waiting_for_new_amount)

    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"exp_{exp_id}_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("Введите новую сумму для этого расхода:", reply_markup=keyboard)
    await callback.answer()

async def get_matching_expenses(user_id: int, exp_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT category_name, amount, description FROM expenses WHERE id = $1 AND user_id = $2", exp_id, user_id)
        if not target:
            return None, []

        cat_name = target["category_name"]
        amount = float(target["amount"])
        desc = target["description"]

        if desc and len(desc.strip()) > 0:
            clean_word = desc.strip()
            query = "SELECT id, amount, description, created_at FROM expenses WHERE user_id = $1 AND category_name = $2 AND description ILIKE $3 ORDER BY created_at DESC"
            rows = await conn.fetch(query, user_id, cat_name, f"%{clean_word}%")
        else:
            query = "SELECT id, amount, description, created_at FROM expenses WHERE user_id = $1 AND category_name = $2 AND amount = $3 ORDER BY created_at DESC"
            rows = await conn.fetch(query, user_id, cat_name, amount)

        return (cat_name, amount, desc), rows

@dp.callback_query(F.data.startswith("anl_"))
async def callback_expense_analytics(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    exp_id = int(parts[1])
    cat_id = int(parts[2])
    view_type = parts[3]
    user_id = callback.from_user.id

    target, matching_rows = await get_matching_expenses(user_id, exp_id)
    if not target:
        await callback.message.edit_text("Расход не найден.")
        await callback.answer()
        return

    cat_name, target_amount, target_desc = target
    has_tag = bool(target_desc and len(target_desc.strip()) > 0)
    item_title = f"«{target_desc}»" if has_tag else f"{target_amount:.2f} руб."

    now = datetime.datetime.utcnow()
    parsed_items = []
    for r in matching_rows:
        dt = r["created_at"]
        amt = float(r["amount"])
        desc = r["description"]
        parsed_items.append((dt, amt, dt, desc))

    def count_in_days(days: int):
        filtered = [x for x in parsed_items if (now - x[0]).total_seconds() <= days * 86400]
        return len(filtered), sum(x[1] for x in filtered)

    if view_type == "ov":
        w_cnt, w_sum = count_in_days(7)
        m1_cnt, m1_sum = count_in_days(30)
        m2_cnt, m2_sum = count_in_days(60)
        m3_cnt, m3_sum = count_in_days(90)
        y1_cnt, y1_sum = count_in_days(365)
        all_cnt = len(parsed_items)
        all_sum = sum(x[1] for x in parsed_items)

        all_amounts = [x[1] for x in parsed_items]
        avg_price = (all_sum / all_cnt) if all_cnt > 0 else 0
        min_p = min(all_amounts) if all_amounts else 0
        max_p = max(all_amounts) if all_amounts else 0
        price_spread = f"• Разброс цен: **от {min_p:.2f} до {max_p:.2f} руб.**\n" if min_p != max_p else ""

        monthly_stats = {}
        for dt, amt, _, _ in parsed_items:
            ym = dt.strftime("%Y-%m")
            if ym not in monthly_stats:
                monthly_stats[ym] = [0, 0.0]
            monthly_stats[ym][0] += 1
            monthly_stats[ym][1] += amt

        months_report = []
        for ym in sorted(monthly_stats.keys(), reverse=True)[:6]:
            m_title = get_month_title(ym)
            cnt, sm = monthly_stats[ym]
            months_report.append(f"• **{m_title}**: {cnt} раз(а) — {sm:.2f} руб.")

        months_text = "\n".join(months_report) if months_report else "• Пока нет данных"

        avg_text = ""
        if len(parsed_items) >= 2:
            oldest_dt = min(x[0] for x in parsed_items)
            newest_dt = max(x[0] for x in parsed_items)
            span_days = (newest_dt - oldest_dt).days
            if span_days > 0:
                avg_interval = span_days / (len(parsed_items) - 1)
                avg_text = f"💡 *В среднем покупаете раз в ~{round(avg_interval)} дн.*\n"

        tip_text = ""
        if not has_tag:
            tip_text = "\n⚠️ *У этой траты не задано название товара.*\nАнализ построен по точной сумме. Нажмите «🏷 Задать название» в карточке, чтобы бот объединил её с покупками по другим ценам!\n"

        text = f"📈 **Аналитика товара: {item_title}**\n📂 Категория: **{cat_name}**\n\n💰 **Общие показатели:**\n• Всего куплено: **{all_cnt} раз(а)**\n• Общая сумма: **{all_sum:.2f} руб.**\n• Средняя цена: **{avg_price:.2f} руб.**\n{price_spread}{avg_text}{tip_text}\n⏱ **Частота по периодам:**\n• 7 дней (неделя): **{w_cnt}** раз — {w_sum:.2f} руб.\n• 30 дней (1 мес.): **{m1_cnt}** раз — {m1_sum:.2f} руб.\n• 60 дней (2 мес.): **{m2_cnt}** раз — {m2_sum:.2f} руб.\n• 90 дней (3 мес.): **{m3_cnt}** раз — {m3_sum:.2f} руб.\n• 1 год (365 дн.): **{y1_cnt}** раз — {y1_sum:.2f} руб.\n\n📅 **По месяцам:**\n{months_text}\n\n👇 *Нажмите на период, чтобы посмотреть список чеков:* "

        buttons = [
            [
                InlineKeyboardButton(text="7 дн.", callback_data=f"anl_{exp_id}_{cat_id}_7"),
                InlineKeyboardButton(text="30 дн.", callback_data=f"anl_{exp_id}_{cat_id}_30"),
                InlineKeyboardButton(text="60 дн.", callback_data=f"anl_{exp_id}_{cat_id}_60")
            ],
            [
                InlineKeyboardButton(text="90 дн.", callback_data=f"anl_{exp_id}_{cat_id}_90"),
                InlineKeyboardButton(text="1 год", callback_data=f"anl_{exp_id}_{cat_id}_365"),
                InlineKeyboardButton(text="♾ Всё", callback_data=f"anl_{exp_id}_{cat_id}_all")
            ],
            [InlineKeyboardButton(text="◀️ Назад к расходу", callback_data=f"exp_{exp_id}_{cat_id}")]
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()
        return

    days_labels = {
        "7": "7 дней (неделя)",
        "30": "30 дней (1 месяц)",
        "60": "60 дней (2 месяца)",
        "90": "90 дней (3 месяца)",
        "365": "1 год (365 дней)",
        "all": "Всё время"
    }
    period_name = days_labels.get(view_type, view_type)

    if view_type == "all":
        selected_items = parsed_items
    else:
        limit_days = int(view_type)
        selected_items = [x for x in parsed_items if (now - x[0]).total_seconds() <= limit_days * 86400]

    count_period = len(selected_items)
    sum_period = sum(x[1] for x in selected_items)

    dates_lines = []
    for idx, (dt, amt, _, d_text) in enumerate(selected_items[:12], 1):
        f_dt = format_datetime(dt)
        note = f" ({d_text})" if d_text else ""
        dates_lines.append(f"{idx}. {f_dt} — **{amt:.2f} руб.**{note}")

    dates_block = "\n".join(dates_lines) if dates_lines else "Покупок за этот период не было."
    if len(selected_items) > 12:
        dates_block += f"\n*...и еще {len(selected_items) - 12} покупок*"

    detail_text = f"🔍 **Детально за период: {period_name}**\n\n📌 Товар: **{item_title}**\n📂 Категория: **{cat_name}**\n\n• Количество покупок: **{count_period}**\n• Общая сумма: **{sum_period:.2f} руб.**\n\n📜 **История покупок:**\n{dates_block}"

    buttons = [
        [
            InlineKeyboardButton(text="7 дн.", callback_data=f"anl_{exp_id}_{cat_id}_7"),
            InlineKeyboardButton(text="30 дн.", callback_data=f"anl_{exp_id}_{cat_id}_30"),
            InlineKeyboardButton(text="60 дн.", callback_data=f"anl_{exp_id}_{cat_id}_60")
        ],
        [
            InlineKeyboardButton(text="90 дн.", callback_data=f"anl_{exp_id}_{cat_id}_90"),
            InlineKeyboardButton(text="1 год", callback_data=f"anl_{exp_id}_{cat_id}_365"),
            InlineKeyboardButton(text="♾ Всё", callback_data=f"anl_{exp_id}_{cat_id}_all")
        ],
        [InlineKeyboardButton(text="📊 К общему обзору", callback_data=f"anl_{exp_id}_{cat_id}_ov")],
        [InlineKeyboardButton(text="◀️ К расходу", callback_data=f"exp_{exp_id}_{cat_id}")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(detail_text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "cancel")
async def callback_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()

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
    print(">>> Бот запущен на Render с базой PostgreSQL! <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
