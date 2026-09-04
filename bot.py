import asyncio
import datetime
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import aiosqlite
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

def parse_created_date(val):
    if val is None:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # Если таймштамп в миллисекундах или секундах
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.replace(".", "", 1).isdigit()):
        v = float(val)
        if v > 10_000_000_000:
            v /= 1000.0
        try:
            return datetime.datetime.utcfromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    # Если строка даты
    s = str(val).replace("T", " ").replace("Z", "").strip()
    if len(s) >= 19:
        return s[:19]
    elif len(s) == 10 and s.count("-") == 2:
        return s + " 12:00:00"
    return s


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
            query = """
                SELECT category_name, SUM(amount) 
                FROM expenses 
                WHERE user_id = ? AND strftime('%Y-%m', created_at) = ? 
                GROUP BY category_name 
                ORDER BY SUM(amount) DESC
            """
            params = (user_id, ym_period)
        else:
            query = """
                SELECT category_name, SUM(amount) 
                FROM expenses 
                WHERE user_id = ? 
                GROUP BY category_name 
                ORDER BY SUM(amount) DESC
            """
            params = (user_id,)

        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


# ---------------- ТОЧНЫЙ ПАРСИНГ ВАШЕЙ БАЗЫ ДАННЫХ ----------------
def parse_backup_sqlite(sqlite_path):
    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0].lower() for r in cursor.fetchall()]

    expenses = []

    # Проверяем точные таблицы из вашей базы
    if "category" in tables and "transaction" in tables:
        # 1. Считываем категории
        cursor.execute("PRAGMA table_info('category');")
        cat_cols = {c[1].lower(): c[1] for c in cursor.fetchall()}
        c_uid = cat_cols.get("uid") or "uid"
        c_title = cat_cols.get("title") or "title"
        c_type = cat_cols.get("type")

        cursor.execute(f"SELECT {c_uid}, {c_title}, {c_type if c_type else '0'} FROM category;")
        cat_rows = cursor.fetchall()

        # Автоматически определяем тип расходов (по типичным категориям трат)
        expense_type = None
        for uid, title, ctype in cat_rows:
            t_low = str(title).lower()
            if any(w in t_low for w in ["продукт", "еда", "транспорт", "кафе", "красот", "аптек", "дом", "связь", "авто", "одежд", "развлеч", "кошк", "food", "transport"]):
                expense_type = ctype
                break

        cat_map = {}
        for uid, title, ctype in cat_rows:
            if expense_type is not None:
                if ctype == expense_type:
                    cat_map[str(uid)] = str(title).strip()
            else:
                cat_map[str(uid)] = str(title).strip()

        # 2. Считываем транзакции
        cursor.execute("PRAGMA table_info('transaction');")
        tx_cols = {c[1].lower(): c[1] for c in cursor.fetchall()}

        amt_col = tx_cols.get("amountindefaultcurrency") or tx_cols.get("amountinaccountcurrency") or tx_cols.get("amountinrealcurrency") or "amountInDefaultCurrency"
        date_col = tx_cols.get("created") or "created"
        cat_col = next((tx_cols[c] for c in tx_cols if "cat" in c), "categoryUid")
        note_col = next((tx_cols[c] for c in tx_cols if c in ["note", "comment", "description", "memo"]), None)
        rem_col = next((tx_cols[c] for c in tx_cols if "remove" in c or "delete" in c), None)

        q_cols = [amt_col, date_col, cat_col if cat_col in tx_cols.values() else "NULL"]
        q_cols.append(note_col if note_col else "NULL")
        q_cols.append(rem_col if rem_col else "NULL")

        cursor.execute(f"SELECT {', '.join(q_cols)} FROM 'transaction';")
        for r in cursor.fetchall():
            raw_amt = r[0]
            raw_date = r[1]
            raw_cat = r[2]
            raw_note = r[3]
            is_removed = r[4]

            # Игнорируем удаленные записи
            if is_removed in [1, "1", True, "true"]:
                continue

            if raw_amt is None:
                continue

            try:
                amt = abs(float(raw_amt))
                if amt == 0:
                    continue
            except (ValueError, TypeError):
                continue

            cat_uid_str = str(raw_cat).strip() if raw_cat is not None else ""
            if cat_map:
                if cat_uid_str not in cat_map:
                    continue
                category_name = cat_map[cat_uid_str]
            else:
                category_name = "Другое"

            dt_str = parse_created_date(raw_date)
            desc = str(raw_note).strip() if raw_note else ""

            expenses.append((category_name, amt, desc, dt_str))

    conn.close()
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
        "👋 Привет! Я твой бот учета финансов с умной аналитикой товаров.\n\n"
        "💡 **Как пользоваться:**\n"
        "• **Внести расход:** напиши сумму с названием товара или без (например, `420 шампунь` или `300`).\n"
        "• **📊 Текущий месяц / 📅 Выбрать месяц:** статистика за текущий или любой прошлый месяц.\n"
        "• **📂 Мои категории:** детальный просмотр трат, редактирование и умная аналитика.\n\n"
        "📁 **Импорт:** отправьте в чат ваш файл архива (`.zip` или `.mmbackup`)."
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# Загрузка и импорт резервной копии
@dp.message(F.document)
async def handle_backup_document(message: types.Message):
    doc = message.document
    fname = doc.file_name.lower()

    if not (fname.endswith(".zip") or fname.endswith(".mmbackup") or fname.endswith(".sqlite") or fname.endswith(".db")):
        await message.answer("Пожалуйста, отправьте файл резервной копии (`.zip` или `.mmbackup`).")
        return

    status_msg = await message.answer("⏳ Распаковываю и переношу историю ваших трат...")
    user_id = message.from_user.id
    temp_dir = tempfile.mkdtemp()

    try:
        file_info = await bot.get_file(doc.file_id)
        local_zip_path = os.path.join(temp_dir, "backup.zip")
        await bot.download_file(file_info.file_path, local_zip_path)

        extracted_db = None
        if fname.endswith(".sqlite") or fname.endswith(".db"):
            extracted_db = local_zip_path
        else:
            try:
                with zipfile.ZipFile(local_zip_path, 'r') as z:
                    z.extractall(temp_dir)
                for root, dirs, files in os.walk(temp_dir):
                    for f in files:
                        if f.endswith(".sqlite") or f.endswith(".db"):
                            extracted_db = os.path.join(root, f)
                            break
                    if extracted_db:
                        break
            except Exception:
                pass

        if not extracted_db or not os.path.exists(extracted_db):
            await status_msg.edit_text("❌ В архиве не найден файл базы данных.")
            return

        expenses = parse_backup_sqlite(extracted_db)

        if not expenses:
            await status_msg.edit_text("❌ В базе не удалось найти расходы.")
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
            f"🎉 **Импорт успешно завершен!**\n\n"
            f"• Перенесено трат: **{len(expenses)}**\n"
            f"• Категорий: **{len(imported_cats)}**\n"
            f"• Общая сумма: **{total_sum:.2f} руб.**\n"
            f"• Период: **{date_range}**\n\n"
            f"Все данные распределены по месяцам и категориям! Нажмите **📊 Текущий месяц** или **📅 Выбрать месяц**."
        )
        await status_msg.edit_text(report_text, parse_mode="Markdown")

    except Exception as ex:
        logging.error(f"Import error: {ex}")
        await status_msg.edit_text(f"❌ Ошибка при импорте: {ex}")
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
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT strftime('%Y-%m', created_at) FROM expenses WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("У вас пока нет сохраненных расходов.")
        return

    buttons = []
    for (ym,) in rows:
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
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT strftime('%Y-%m', created_at) FROM expenses WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    buttons = []
    for (ym,) in rows:
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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, cat_name))
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Категория **«{cat_name}»** сохранена!", reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(Form.waiting_for_new_cat_name)
async def process_rename_category(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    data = await state.get_data()
    cat_id = data.get("cat_id")
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            old_name = row[0] if row else ""

        await db.execute("UPDATE categories SET name = ? WHERE id = ? AND user_id = ?", (new_name, cat_id, user_id))
        await db.execute("UPDATE expenses SET category_name = ? WHERE category_name = ? AND user_id = ?", (new_name, old_name, user_id))
        await db.commit()

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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE expenses SET amount = ? WHERE id = ? AND user_id = ?", (new_amount, exp_id, user_id))
        await db.commit()
        async with db.execute("SELECT category_name FROM expenses WHERE id = ?", (exp_id,)) as cursor:
            row = await cursor.fetchone()
            cat_name = row[0] if row else ""

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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE expenses SET description = ? WHERE id = ? AND user_id = ?", (new_desc, exp_id, user_id))
        await db.commit()

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

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            category_name = row[0] if row else ""

    await add_expense(user_id, category_name, amount, description)
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

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            cat_row = await cursor.fetchone()
            category_name = cat_row[0] if cat_row else ""

        async with db.execute(
            "SELECT id, amount, description, created_at FROM expenses WHERE user_id = ? AND category_name = ? ORDER BY id DESC LIMIT 15",
            (user_id, category_name)
        ) as cursor:
            expenses = await cursor.fetchall()

    buttons = []
    for exp_id, amount, desc, created_at in expenses:
        date_short = format_short_date(created_at)
        desc_label = f" — {desc[:10]}" if desc else ""
        btn_text = f"{amount:.2f} руб.{desc_label} ({date_short})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"exp_{exp_id}_{cat_id}")])

    buttons.append([
        InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rencat_{cat_id}"),
        InlineKeyboardButton(text="🗑️ Удалить категорию", callback_data=f"askdelcat_{cat_id}")
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад ко всем категориям", callback_data="b_cats")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"📂 Категория: **{category_name}** ({len(expenses)} записей)\n*Нажмите на расход для изменения, названия товара или аналитики:*",
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

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            cat_name = row[0] if row else ""

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

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT
