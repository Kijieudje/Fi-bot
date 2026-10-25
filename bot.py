import asyncio
import os
import re
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, ReplyKeyboardMarkup

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("BOT_TOKEN", "8835340952:AAGKvYplGHylAdVfGzO-KVRpnWMR7KsvI-M")
DB_NAME = "finance.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                r_type TEXT,       -- 'income' (доход) или 'expense' (расход)
                amount REAL,
                comment TEXT,
                breakdown TEXT,    -- исходная цепочка: '2500+3000+5000'
                date_str TEXT,     -- YYYY-MM-DD
                month_str TEXT,    -- YYYY-MM
                time_str TEXT      -- HH:MM
            )
        """)
        conn.commit()

def add_record(r_type: str, amount: float, comment: str, breakdown: str):
    now = datetime.now()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO records (user_id, r_type, amount, comment, breakdown, date_str, month_str, time_str)
            VALUES (0, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r_type,
            amount,
            comment,
            breakdown,
            now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m"),
            now.strftime("%H:%M")
        ))
        conn.commit()

def delete_last_record():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, r_type, amount, comment FROM records 
            ORDER BY id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM records WHERE id = ?", (row[0],))
            conn.commit()
        return row

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def format_money(val: float) -> str:
    """Форматирует число в денежный вид: 10 500 ₽"""
    if val.is_integer():
        return f"{int(val):,}".replace(",", " ") + " ₽"
    return f"{val:,.2f}".replace(",", " ") + " ₽"

def parse_expression(text: str):
    """
    Парсит строки вида:
    '2500+3000+5000'
    '2500 + 3000 + 5000 смена'
    '150.5 + 400 такси'
    """
    text = text.strip()
    match = re.match(r"^([\d\s\+\,\.]+)(.*)$", text)
    if not match:
        raise ValueError("Не удалось распознать сумму")
    
    expr_str = match.group(1).strip()
    comment = match.group(2).strip() or "Без комментария"
    
    clean_expr = expr_str.replace(",", ".")
    raw_parts = clean_expr.split("+")
    numbers = []
    for p in raw_parts:
        p_clean = p.strip()
        if p_clean:
            val = float(p_clean)
            if val <= 0:
                raise ValueError("Суммы должны быть больше нуля")
            numbers.append(val)
            
    if not numbers:
        raise ValueError("Числа не найдены")
        
    total = sum(numbers)
    breakdown_text = " + ".join(f"{int(n) if n.is_integer() else n:,}".replace(",", " ") for n in numbers)
    return total, comment, breakdown_text

# ================= КЛАВИАТУРА И СОСТОЯНИЯ =================
class FinanceStates(StatesGroup):
    waiting_for_income = State()
    waiting_for_expense = State()

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Записать ЗП"), KeyboardButton(text="➖ Записать трату")],
        [KeyboardButton(text="📊 Сводка за день"), KeyboardButton(text="📅 Сводка за месяц")],
        [KeyboardButton(text="📈 Общая сводка"), KeyboardButton(text="↩️ Отменить последнюю")],
        [KeyboardButton(text="💾 Бэкап базы")]
    ],
    resize_keyboard=True
)

# ================= ОБРАБОТЧИКИ МЕНЮ =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я считаю ЗП и траты.\n\n"
        "✨ Суммы можно вводить через плюс:\n"
        "<code>2500+3000+5000</code> — я сам всё сложу и сохраню итог.\n\n"
        "Выбери действие на кнопках внизу 👇",
        reply_markup=main_kb,
        parse_mode="HTML"
    )

@dp.message(F.text == "➕ Записать ЗП")
async def btn_income(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_income)
    await message.answer(
        "💵 <b>Введи сумму дохода:</b>\n"
        "Можно цепочкой через плюс и с комментом:\n"
        "👉 <code>2500+3000+5000</code>\n"
        "👉 <code>3000+1500 смена</code>",
        parse_mode="HTML"
    )

@dp.message(F.text == "➖ Записать трату")
async def btn_expense(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_expense)
    await message.answer(
        "🛒 <b>Введи сумму трат:</b>\n"
        "Можно списком через плюс:\n"
        "👉 <code>350+120+450</code>\n"
        "👉 <code>1200+500 продукты</code>",
        parse_mode="HTML"
    )

@dp.message(F.text == "↩️ Отменить последнюю")
async def btn_undo(message: types.Message, state: FSMContext):
    await state.clear()
    deleted = delete_last_record()
    if deleted:
        _, r_type, amount, comment = deleted
        label = "Доход" if r_type == "income" else "Расход"
        await message.answer(
            f"🗑 <b>Удалена последняя запись:</b>\n"
            f"{label}: <b>{format_money(amount)}</b> ({comment})",
            parse_mode="HTML"
        )
    else:
        await message.answer("Записей для удаления пока нет.")

# ================= БЭКАП И ВОССТАНОВЛЕНИЕ БАЗЫ =================
@dp.message(F.text == "💾 Бэкап базы")
async def btn_backup(message: types.Message, state: FSMContext):
    await state.clear()
    if not os.path.exists(DB_NAME):
        await message.answer("Файл базы данных еще не создан.")
        return

    file_date = datetime.now().strftime("%Y-%m-%d_%H-%M")
    backup_file = FSInputFile(DB_NAME, filename=f"finance_backup_{file_date}.db")
    
    await message.answer_document(
        document=backup_file,
        caption=(
            f"💾 <b>Резервная копия базы</b>\n"
            f"📅 Дата: {datetime.now().strftime('%d.%m.%Y в %H:%M')}\n\n"
            f"<i>Чтобы восстановить эту базу, просто отправь этот файл обратно в чат.</i>"
        ),
        parse_mode="HTML"
    )

@dp.message(F.document)
async def restore_from_file(message: types.Message, state: FSMContext):
    await state.clear()
    if not message.document.file_name.endswith(".db"):
        await message.answer("⚠️ Для загрузки базы отправь файл с расширением <code>.db</code>", parse_mode="HTML")
        return

    file_info = await bot.get_file(message.document.file_id)
    await bot.download_file(file_info.file_path, DB_NAME)
    init_db()
    await message.answer("✅ <b>База данных успешно загружена и подключена!</b>", parse_mode="HTML")

# ================= СВОДКИ =================
@dp.message(F.text == "📊 Сводка за день")
async def btn_day_summary(message: types.Message, state: FSMContext):
    await state.clear()
    today = datetime.now().strftime("%Y-%m-%d")
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r_type, amount, comment, breakdown, time_str FROM records
            WHERE date_str = ?
            ORDER BY id ASC
        """, (today,))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("За сегодня записей пока нет.")
        return

    income = sum(r[1] for r in rows if r[0] == "income")
    expense = sum(r[1] for r in rows if r[0] == "expense")
    net = income - expense

    details = []
    for r_type, amount, comment, breakdown, time_str in rows:
        icon = "🟢 +" if r_type == "income" else "🔴 -"
        calc_info = f" ({breakdown})" if breakdown and " + " in breakdown else ""
        details.append(f"<code>{time_str}</code> {icon}<b>{format_money(amount)}</b>{calc_info} — <i>{comment}</i>")

    text = (
        f"📊 <b>Сводка за сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>\n\n"
        f"➕ Всего ЗП: <b>{format_money(income)}</b>\n"
        f"➖ Всего трат: <b>{format_money(expense)}</b>\n"
        f"💰 <b>Итог чистыми: {format_money(net)}</b>\n\n"
        f"<b>Детализация за день:</b>\n" + "\n".join(details)
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📅 Сводка за месяц")
async def btn_month_summary(message: types.Message, state: FSMContext):
    await state.clear()
    current_month = datetime.now().strftime("%Y-%m")
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r_type, amount, comment, date_str FROM records
            WHERE month_str = ?
        """, (current_month,))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("В этом месяце записей пока нет.")
        return

    income = sum(r[1] for r in rows if r[0] == "income")
    expense = sum(r[1] for r in rows if r[0] == "expense")
    net = income - expense
    
    work_days = len(set(r[3] for r in rows if r[0] == "income"))
    avg_per_day = income / work_days if work_days > 0 else 0

    text = (
        f"📅 <b>Сводка за текущий месяц ({datetime.now().strftime('%m.%Y')}):</b>\n\n"
        f"➕ Заработано: <b>{format_money(income)}</b>\n"
        f"➖ Потрачено: <b>{format_money(expense)}</b>\n"
        f"🏦 <b>Чистый остаток: {format_money(net)}</b>\n\n"
        f"💼 Дней с доходом: <b>{work_days}</b>\n"
        f"⚡️ Средний доход в день: <b>{format_money(avg_per_day)}</b>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📈 Общая сводка")
async def btn_total_summary(message: types.Message, state: FSMContext):
    await state.clear()
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r_type, amount, date_str, month_str FROM records
            ORDER BY month_str ASC
        """)
        rows = cursor.fetchall()

    if not rows:
        await message.answer("База пока пуста — добавь первую запись!")
        return

    total_income = sum(r[1] for r in rows if r[0] == "income")
    total_expense = sum(r[1] for r in rows if r[0] == "expense")
    total_net = total_income - total_expense

    work_days = len(set(r[2] for r in rows if r[0] == "income"))
    avg_per_day = total_income / work_days if work_days > 0 else 0

    months_data = {}
    for r_type, amount, _, month_str in rows:
        if month_str not in months_data:
            months_data[month_str] = {"income": 0.0, "expense": 0.0}
        months_data[month_str][r_type] += amount

    month_lines = []
    for m_str, data in months_data.items():
        year, month = m_str.split("-")
        m_inc = data["income"]
        m_exp = data["expense"]
        m_net = m_inc - m_exp
        month_lines.append(
            f"▫️ <b>{month}.{year}:</b> +{format_money(m_inc)} | -{format_money(m_exp)} "
            f"<i>(чистыми: {format_money(m_net)})</i>"
        )

    text = (
        f"📈 <b>Общая сводка за всё время:</b>\n\n"
        f"➕ Всего заработано: <b>{format_money(total_income)}</b>\n"
        f"➖ Всего потрачено: <b>{format_money(total_expense)}</b>\n"
        f"💎 <b>Чистый итог: {format_money(total_net)}</b>\n\n"
        f"💼 Всего дней с доходом: <b>{work_days}</b>\n"
        f"⚡️ Средний заработок за день: <b>{format_money(avg_per_day)}</b>\n\n"
        f"<b>Разбивка по месяцам:</b>\n" + "\n".join(month_lines)
    )
    await message.answer(text, parse_mode="HTML")

# ================= ОБРАБОТКА ВВОДА СУММ =================
@dp.message(FinanceStates.waiting_for_income)
async def process_income(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record("income", total, comment, breakdown)
        
        calc_line = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(
            f"✅ <b>ЗП записана:</b> +{format_money(total)}{calc_line}\n"
            f"📝 <i>{comment}</i>\n\n"
            f"Можешь вводить следующую сумму или выбрать пункт меню.",
            parse_mode="HTML"
        )
    except Exception:
        await message.answer("⚠️ Ошибка формата. Введи число или сумму через плюс: <code>2500+3000+5000</code>", parse_mode="HTML")

@dp.message(FinanceStates.waiting_for_expense)
async def process_expense(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record("expense", total, comment, breakdown)
        
        calc_line = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(
            f"✅ <b>Трата записана:</b> -{format_money(total)}{calc_line}\n"
            f"📝 <i>{comment}</i>\n\n"
            f"Можешь вводить следующую трату или выбрать пункт меню.",
            parse_mode="HTML"
        )
    except Exception:
        await message.answer("⚠️ Ошибка формата. Введи число или сумму через плюс: <code>450+120 обед</code>", parse_mode="HTML")

@dp.message()
async def fallback_handler(message: types.Message):
    await message.answer("Выбери действие на кнопках внизу 👇", reply_markup=main_kb)

# ================= ЗАПУСК =================
async def main():
    init_db()
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
