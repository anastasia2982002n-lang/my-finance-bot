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
    waiting_for_category_name = State()   # Создание категории
    waiting_for_new_cat_name = State()    # Переименование категории
    waiting_for_new_amount = State()      # Редактирование суммы
    waiting_for_expense_desc = State()    # Задание названия товара/тега

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


# ---------------- УМНЫЙ ПАРСИНГ РЕЗЕРВНОЙ КОПИИ ----------------
def parse_backup_sqlite(sqlite_path):
    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0] for r in cursor.fetchall()]

    # 1. Категории
    cat_map = {}
    for tname in tables:
        if "CATEGORY" in tname.upper() and "SUB" not in tname.upper():
            cursor.execute(f"PRAGMA table_info('{tname}');")
            cols = {c[1].upper(): c[1] for c in cursor.fetchall()}
            uid_col = cols.get("UID") or cols.get("ID") or cols.get("_ID") or cols.get("Z_PK")
            name_col = cols.get("NAME") or cols.get("TITLE") or cols.get("CATEGORY_NAME")
            if uid_col and name_col:
                try:
                    cursor.execute(f"SELECT {uid_col}, {name_col} FROM '{tname}';")
                    for uid, name in cursor.fetchall():
                        if name:
                            cat_map[str(uid)] = str(name).strip()
                except Exception:
                    pass
            if cat_map:
                break

    # 2. Подкатегории (если есть в приложении)
    subcat_map = {}
    for tname in tables:
        if "SUB" in tname.upper() and "CAT" in tname.upper():
            cursor.execute(f"PRAGMA table_info('{tname}');")
            cols = {c[1].upper(): c[1] for c in cursor.fetchall()}
            uid_col = cols.get("UID") or cols.get("ID") or cols.get("_ID")
            name_col = cols.get("NAME") or cols.get("TITLE")
            if uid_col and name_col:
                try:
                    cursor.execute(f"SELECT {uid_col}, {name_col} FROM '{tname}';")
                    for uid, name in cursor.fetchall():
                        if name:
                            subcat_map[str(uid)] = str(name).strip()
                except Exception:
                    pass
            if subcat_map:
                break

    # 3. Расходы
    expenses = []
    for tname in tables:
        cursor.execute(f"PRAGMA table_info('{tname}');")
        cols = {c[1].upper(): c[1] for c in cursor.fetchall()}

        money_col = cols.get("MONEY") or cols.get("AMOUNT") or cols.get("PRICE")
        date_col = cols.get("DO_DATE") or cols.get("DATE") or cols.get("REG_DATE") or cols.get("CREATED_AT")
        cat_uid_col = cols.get("CATEGORY_UID") or cols.get("CATEGORY_ID") or cols.get("CAT_UID") or cols.get("CATEGORY")
        subcat_col = cols.get("SUBCATEGORY_UID") or cols.get("CATEGORY_SUB_UID") or cols.get("SUB_UID")
        pay_type_col = cols.get("PAY_TYPE") or cols.get("INOUT_TYPE") or cols.get("TYPE") or cols.get("INOUT")
        content_col = cols.get("CONTENT") or cols.get("MEMO") or cols.get("NOTE") or cols.get("DESCRIPTION")

        if money_col and date_col:
            q_cols = [
                money_col, 
                date_col,
                cat_uid_col if cat_uid_col else "NULL",
                content_col if content_col else "NULL",
                pay_type_col if pay_type_col else "NULL",
                subcat_col if subcat_col else "NULL"
            ]
            try:
                cursor.execute(f"SELECT {', '.join(q_cols)} FROM '{tname}';")
                rows = cursor.fetchall()
                for r in rows:
                    amt = r[0]
                    raw_date = r[1]
                    cat_val = r[2]
                    desc_val = r[3] if r[3] else ""
                    ptype = r[4]
                    subcat_val = r[5] if len(r) > 5 else None

                    # Пропускаем доходы и переводы
                    if ptype is not None and str(ptype).strip() in ['1', '2', 'INC', 'INCOME', 'TRANSFER']:
                        continue

                    try:
                        amt = abs(float(amt))
                        if amt == 0:
                            continue
                    except (ValueError, TypeError):
                        continue

                    d_str = str(raw_date).strip()
                    if len(d_str) == 10 and d_str.count("-") == 2:
                        d_str += " 12:00:00"
                    elif len(d_str) > 19:
                        d_str = d_str[:19]

                    category_name = "Другое"
                    if cat_val is not None:
                        s_cat = str(cat_val).strip()
                        if s_cat in cat_map:
                            category_name = cat_map[s_cat]
                        elif not s_cat.isdigit() and len(s_cat) > 1:
                            category_name = s_cat

                    # Формируем описание товара из подкатегории и заметки
                    desc_parts = []
                    if subcat_val is not None and str(subcat_val).strip() in subcat_map:
                        desc_parts.append(subcat_map[str(subcat_val).strip()])
                    if desc_val and str(desc_val).strip():
                        desc_parts.append(str(desc_val).strip())

                    desc = " — ".join(desc_parts)
                    expenses.append((category_name, amt, desc, d_str))

                if expenses:
                    break
            except Exception:
                continue

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
        "💡 **Как вносить траты:**\n"
        "• С названием товара: `420 шампунь` или `890 корм` ➔ выбери категорию.\n"
        "• Или просто сумму: `350` ➔ название товара можно задать в любой момент!\n\n"
        "📈 **Аналитика повторов:** объединяет траты по названию товара независимо от цены (даже если шампунь стоил сначала 350, а потом 420 руб.).\n\n"
        "📁 **Импорт истории:** отправьте сюда файл резервной копии (`.zip` или `.mmbackup`)."
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# Импорт бэкапа
@dp.message(F.document)
async def handle_backup_document(message: types.Message):
    doc = message.document
    fname = doc.file_name.lower()

    if not (fname.endswith(".zip") or fname.endswith(".mmbackup") or fname.endswith(".sqlite") or fname.endswith(".db")):
        await message.answer("Пожалуйста, отправьте файл резервной копии (с расширением `.zip` или `.mmbackup`).")
        return

    status_msg = await message.answer("⏳ Получил файл! Распаковываю и переношу историю...")
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
            await status_msg.edit_text("❌ В базе не найдено записей о расходах.")
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
            f"Все данные распределены по месяцам и категориям!"
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

# ПРИЕМ НАЗВАНИЯ ТОВАРА / ТЕГА
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
    await message.answer(f"✅ Название товара сохранено: **«{new_desc}»**!\nТеперь этот расход привязан к аналитике этого товара.", reply_markup=keyboard, parse_mode="Markdown")

# Ввод нового расхода
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
        async with db.execute("SELECT name FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            cat_name = row[0] if row else ""

        if cat_name:
            await db.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
            await db.execute("DELETE FROM expenses WHERE category_name = ? AND user_id = ?", (cat_name, user_id))
            await db.commit()

    buttons = [[InlineKeyboardButton(text="◀️ Ко всем категориям", callback_data="b_cats")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(f"🗑️ Категория **«{cat_name}»** удалена.", reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()

# Карточка расхода (с кнопкой "Задать название" и "Аналитика")
@dp.callback_query(F.data.startswith("exp_"))
async def callback_expense_details(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    _, exp_id, cat_id = callback.data.split("_")
    exp_id = int(exp_id)
    cat_id = int(cat_id)
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT category_name, amount, description, created_at FROM expenses WHERE id = ? AND user_id = ?", 
            (exp_id, user_id)
        ) as cursor:
            exp = await cursor.fetchone()

    if not exp:
        await callback.message.edit_text("Расход не найден.")
        return

    cat_name, amount, desc, created_at = exp
    date_formatted = format_datetime(created_at)
    desc_text = f"• Товар/услуга: **{desc}**\n" if desc else "• Товар/услуга: *не указано*\n"

    text = (
        f"🧾 **Детали расхода:**\n\n"
        f"• Категория: **{cat_name}**\n"
        f"• Сумма: **{amount:.2f} руб.**\n"
        f"{desc_text}"
        f"• Дата: **{date_formatted}**\n\n"
        f"Выберите действие:"
    )

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

# Нажатие на "Задать название / тег"
@dp.callback_query(F.data.startswith("tag_"))
async def callback_tag_expense(callback: types.CallbackQuery, state: FSMContext):
    _, exp_id, cat_id = callback.data.split("_")
    await state.update_data(exp_id=int(exp_id), cat_id=int(cat_id))
    await state.set_state(Form.waiting_for_expense_desc)

    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"exp_{exp_id}_{cat_id}")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        "Введите название товара или услуги для этого расхода (например, `шампунь` или `корм`):\n\n"
        "*Это позволит боту объединить этот расход с будущими и прошлыми покупками этого товара по любым ценам!*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

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


# ---------------- УМНАЯ АНАЛИТИКА ПО НАЗВАНИЮ ТОВАРА ----------------

async def get_matching_expenses(user_id: int, exp_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT category_name, amount, description FROM expenses WHERE id = ? AND user_id = ?", 
            (exp_id, user_id)
        ) as cursor:
            target = await cursor.fetchone()

        if not target:
            return None, []

        cat_name, amount, desc = target

        # Если у расхода есть название товара -> ищем по названию (НЕ по сумме!)
        if desc and len(desc.strip()) > 0:
            clean_word = desc.strip()
            query = """
                SELECT id, amount, description, created_at 
                FROM expenses 
                WHERE user_id = ? AND category_name = ? 
                  AND LOWER(description) LIKE LOWER(?)
                ORDER BY created_at DESC
            """
            params = (user_id, cat_name, f"%{clean_word}%")
        else:
            # Если названия нет -> ищем по точной сумме
            query = """
                SELECT id, amount, description, created_at 
                FROM expenses 
                WHERE user_id = ? AND category_name = ? AND amount = ?
                ORDER BY created_at DESC
            """
            params = (user_id, cat_name, amount)

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return target, rows

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
    for mid, m_amount, m_desc, m_created_at in matching_rows:
        try:
            dt = datetime.datetime.strptime(m_created_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            dt = now
        parsed_items.append((dt, m_amount, m_created_at, m_desc))

    def count_in_days(days: int):
        filtered = [x for x in parsed_items if (now - x[0]).total_seconds() <= days * 86400]
        return len(filtered), sum(x[1] for x in filtered)

    # ОБЩИЙ ОБЗОР
    if view_type == "ov":
        w_cnt, w_sum = count_in_days(7)
        m1_cnt, m1_sum = count_in_days(30)
        m2_cnt, m2_sum = count_in_days(60)
        m3_cnt, m3_sum = count_in_days(90)
        y1_cnt, y1_sum = count_in_days(365)
        all_cnt = len(parsed_items)
        all_sum = sum(x[1] for x in parsed_items)

        # Анализ цен товара (средний чек, разброс цен)
        all_amounts = [x[1] for x in parsed_items]
        avg_price = (all_sum / all_cnt) if all_cnt > 0 else 0
        min_p = min(all_amounts) if all_amounts else 0
        max_p = max(all_amounts) if all_amounts else 0

        if min_p != max_p:
            price_spread = f"• Разброс цен: **от {min_p:.2f} до {max_p:.2f} руб.**\n"
        else:
            price_spread = ""

        # Статистика по месяцам
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

        # Расчет средней периодичности
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
            tip_text = (
                "\n⚠️ *У этой траты не задано название товара.*\n"
                "Анализ построен по точной сумме. Нажмите «🏷 Задать название» в карточке, чтобы бот объединил её с покупками по другим ценам!\n"
            )

        text = (
            f"📈 **Аналитика товара: {item_title}**\n"
            f"📂 Категория: **{cat_name}**\n\n"
            f"💰 **Общие показатели:**\n"
            f"• Всего куплено: **{all_cnt} раз(а)**\n"
            f"• Общая сумма: **{all_sum:.2f} руб.**\n"
            f"• Средняя цена: **{avg_price:.2f} руб.**\n"
            f"{price_spread}"
            f"{avg_text}"
            f"{tip_text}\n"
            f"⏱ **Частота по периодам:**\n"
            f"• 7 дней (неделя): **{w_cnt}** раз — {w_sum:.2f} руб.\n"
            f"• 30 дней (1 мес.): **{m1_cnt}** раз — {m1_sum:.2f} руб.\n"
            f"• 60 дней (2 мес.): **{m2_cnt}** раз — {m2_sum:.2f} руб.\n"
            f"• 90 дней (3 мес.): **{m3_cnt}** раз — {m3_sum:.2f} руб.\n"
            f"• 1 год (365 дн.): **{y1_cnt}** раз — {y1_sum:.2f} руб.\n\n"
            f"📅 **По месяцам:**\n{months_text}\n\n"
            f"👇 *Нажмите на период, чтобы посмотреть список чеков:* "
        )

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

    # ДЕТАЛИЗАЦИЯ
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
    for idx, (_, amt, raw_dt, d_text) in enumerate(selected_items[:12], 1):
        f_dt = format_datetime(raw_dt)
        note = f" ({d_text})" if d_text else ""
        dates_lines.append(f"{idx}. {f_dt} — **{amt:.2f} руб.**{note}")

    dates_block = "\n".join(dates_lines) if dates_lines else "Покупок за этот период не было."
    if len(selected_items) > 12:
        dates_block += f"\n*...и еще {len(selected_items) - 12} покупок*"

    detail_text = (
        f"🔍 **Детально за период: {period_name}**\n\n"
        f"📌 Товар: **{item_title}**\n"
        f"📂 Категория: **{cat_name}**\n\n"
        f"• Количество покупок: **{count_period}**\n"
        f"• Общая сумма: **{sum_period:.2f} руб.**\n\n"
        f"📜 **История покупок:**\n{dates_block}"
    )

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
    print(">>> Бот запущен на Render! <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
