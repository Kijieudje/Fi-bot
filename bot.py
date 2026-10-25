import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("BOT_TOKEN", "8835340952:AAGKvYplGHylAdVfGzO-KVRpnWMR7KsvI-M")
DB_NAME = "finance.db"
BACKUP_HEADER = "📦 АВТОБЭКАП БАЗЫ (НЕ УДАЛЯТЬ)"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= БАЗА ДАННЫХ =================
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,   -- Уникальный ID пользователя ТГ
                r_type TEXT,       -- 'income' или 'expense'
                amount REAL,
                comment TEXT,
                breakdown TEXT,
                date_str TEXT,     -- YYYY-MM-DD
                month_str TEXT,    -- YYYY-MM
                time_str TEXT      -- HH:MM
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)
        conn.commit()

def add_record(user_id: int, r_type: str, amount: float, comment: str, breakdown: str):
    now = datetime.now()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO records (user_id, r_type, amount, comment, breakdown, date_str, month_str, time_str)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, r_type, amount, comment, breakdown,
            now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.strftime("%H:%M")
        ))
        conn.commit()

def delete_last_record(user_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, r_type, amount, comment FROM records WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM records WHERE id = ?", (row[0],))
            conn.commit()
        return row

# ================= БЭКАП ПРЯМО В ЧАТ =================
def export_backup_text() -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, r_type, amount, comment, breakdown, date_str, month_str, time_str FROM records ORDER BY id ASC")
        rows = cursor.fetchall()

    data_json = json.dumps(rows, ensure_ascii=False)
    return (
        f"<b>{BACKUP_HEADER}</b>\n"
        f"📅 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Всего записей: {len(rows)}\n"
        f"<i>Если база на хостинге слетит — бот подхватит этот бэкап автоматически.</i>\n\n"
        f"<pre>{data_json}</pre>"
    )

def import_backup_text(text: str):
    match = re.search(r"(\[\[.*\]\]|\[\])", text, re.DOTALL)
    if not match:
        raise ValueError("Не найден блок данных")
    
    rows = json.loads(match.group(1))
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM records")
        for r in rows:
            cursor.execute("""
                INSERT INTO records (user_id, r_type, amount, comment, breakdown, date_str, month_str, time_str)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
        conn.commit()

async def auto_restore_check(chat_id: int):
    """Проверяет, пуста ли база на хостинге, и если да — ищет закрепленное сообщение и восстанавливает из него данные"""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM records")
        count = cursor.fetchone()[0]
    
    if count > 0:
        return # База не пуста, всё в порядке

    try:
        chat = await bot.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and BACKUP_HEADER in chat.pinned_message.text:
            import_backup_text(chat.pinned_message.text)
            print("База успешно автовосстановлена из закрепа!")
    except Exception as e:
        print(f"Не удалось автовосстановить базу: {e}")

async def sync_chat_backup(chat_id: int):
    backup_html = export_backup_text()
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT val FROM meta WHERE key = 'pin_msg_id'")
        row = cursor.fetchone()
        pin_msg_id = int(row[0]) if row else None

    if pin_msg_id:
        try:
            await bot.edit_message_text(text=backup_html, chat_id=chat_id, message_id=pin_msg_id, parse_mode="HTML")
            return
        except Exception:
            pass

    sent = await bot.send_message(chat_id=chat_id, text=backup_html, parse_mode="HTML", disable_notification=True)
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
    except Exception:
        pass

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO meta (key, val) VALUES ('pin_msg_id', ?)", (str(sent.message_id),))
        conn.commit()

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def format_money(val: float) -> str:
    if val.is_integer():
        return f"{int(val):,}".replace(",", " ") + " ₽"
    return f"{val:,.2f}".replace(",", " ") + " ₽"

def parse_expression(text: str):
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
        [KeyboardButton(text="💬 Бэкап в чат")]
    ],
    resize_keyboard=True
)

# ================= ОБРАБОТЧИКИ МЕНЮ =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await auto_restore_check(message.chat.id)
    await message.answer(
        "👋 Привет! Я считаю ЗП и траты (у каждого пользователя свои данные).\n\n"
        "✨ Вводи суммы через плюс: <code>2500+3000+5000</code>",
        reply_markup=main_kb,
        parse_mode="HTML"
    )

@dp.message(F.text == "💬 Бэкап в чат")
async def btn_chat_backup(message: types.Message, state: FSMContext):
    await state.clear()
    await sync_chat_backup(message.chat.id)
    await message.answer("✅ <b>Бэкап в закрепе чата обновлен!</b>", parse_mode="HTML")

@dp.message(F.text == "➕ Записать ЗП")
async def btn_income(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_income)
    await message.answer("💵 <b>Введи сумму дохода (например: <code>2500+3000</code>):</b>", parse_mode="HTML")

@dp.message(F.text == "➖ Записать трату")
async def btn_expense(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_expense)
    await message.answer("🛒 <b>Введи сумму трат (например: <code>350+120 обед</code>):</b>", parse_mode="HTML")

@dp.message(F.text == "↩️ Отменить последнюю")
async def btn_undo(message: types.Message, state: FSMContext):
    await state.clear()
    await auto_restore_check(message.chat.id)
    deleted = delete_last_record(message.from_user.id)
    if deleted:
        _, r_type, amount, comment = deleted
        label = "Доход" if r_type == "income" else "Расход"
        await sync_chat_backup(message.chat.id)
        await message.answer(f"🗑 <b>Удалена последняя запись:</b>\n{label}: <b>{format_money(amount)}</b> ({comment})", parse_mode="HTML")
    else:
        await message.answer("У тебя нет записей для удаления.")

# ================= СВОДКИ =================
@dp.message(F.text == "📊 Сводка за день")
async def btn_day_summary(message: types.Message, state: FSMContext):
    await state.clear()
    await auto_restore_check(message.chat.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r_type, amount, comment, breakdown, time_str FROM records
            WHERE user_id = ? AND date_str = ? ORDER BY id ASC
        """, (message.from_user.id, today))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("За сегодня записей пока нет.")
        return

    income = sum(r[1] for r in rows if r[0] == "income")
    expense = sum(r[1] for r in rows if r[0] == "expense")
    net = income - expense

    details = [
        f"<code>{t}</code> {'🟢 +' if rt == 'income' else '🔴 -'}<b>{format_money(a)}</b>{f' ({b})' if b and ' + ' in b else ''} — <i>{c}</i>"
        for rt, a, c, b, t in rows
    ]
    await message.answer(
        f"📊 <b>Сводка за сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>\n\n"
        f"➕ Всего ЗП: <b>{format_money(income)}</b>\n"
        f"➖ Всего трат: <b>{format_money(expense)}</b>\n"
        f"💰 <b>Итог чистыми: {format_money(net)}</b>\n\n"
        f"<b>Детализация:</b>\n" + "\n".join(details),
        parse_mode="HTML"
    )

@dp.message(F.text == "📅 Сводка за месяц")
async def btn_month_summary(message: types.Message, state: FSMContext):
    await state.clear()
    await auto_restore_check(message.chat.id)
    current_month = datetime.now().strftime("%Y-%m")
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r_type, amount, comment, date_str FROM records
            WHERE user_id = ? AND month_str = ?
        """, (message.from_user.id, current_month))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("В этом месяце записей пока нет.")
        return

    income = sum(r[1] for r in rows if r[0] == "income")
    expense = sum(r[1] for r in rows if r[0] == "expense")
    work_days = len(set(r[3] for r in rows if r[0] == "income"))
    avg = income / work_days if work_days > 0 else 0

    await message.answer(
        f"📅 <b>Сводка за текущий месяц ({datetime.now().strftime('%m.%Y')}):</b>\n\n"
        f"➕ Заработано: <b>{format_money(income)}</b>\n"
        f"➖ Потрачено: <b>{format_money(expense)}</b>\n"
        f"🏦 <b>Чистый остаток: {format_money(income - expense)}</b>\n\n"
        f"💼 Дней с доходом: <b>{work_days}</b> (в среднем {format_money(avg)}/день)",
        parse_mode="HTML"
    )

@dp.message(F.text == "📈 Общая сводка")
async def btn_total_summary(message: types.Message, state: FSMContext):
    await state.clear()
    await auto_restore_check(message.chat.id)
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT r_type, amount, date_str, month_str FROM records WHERE user_id = ? ORDER BY month_str ASC", (message.from_user.id,))
        rows = cursor.fetchall()

    if not rows:
        await message.answer("У тебя пока нет записей.")
        return

    total_inc = sum(r[1] for r in rows if r[0] == "income")
    total_exp = sum(r[1] for r in rows if r[0] == "expense")
    work_days = len(set(r[2] for r in rows if r[0] == "income"))
    avg = total_inc / work_days if work_days > 0 else 0

    months = {}
    for rt, a, _, ms in rows:
        months.setdefault(ms, {"income": 0.0, "expense": 0.0})[rt] += a

    m_lines = [
        f"▫️ <b>{ms.split('-')[1]}.{ms.split('-')[0]}:</b> +{format_money(d['income'])} | -{format_money(d['expense'])} <i>(чистыми: {format_money(d['income'] - d['expense'])})</i>"
        for ms, d in months.items()
    ]

    await message.answer(
        f"📈 <b>Общая сводка за всё время:</b>\n\n"
        f"➕ Всего заработано: <b>{format_money(total_inc)}</b>\n"
        f"➖ Всего потрачено: <b>{format_money(total_exp)}</b>\n"
        f"💎 <b>Чистый итог: {format_money(total_inc - total_exp)}</b>\n\n"
        f"💼 Дней с доходом: <b>{work_days}</b> ({format_money(avg)}/день)\n\n"
        f"<b>По месяцам:</b>\n" + "\n".join(m_lines),
        parse_mode="HTML"
    )

# ================= ОБРАБОТКА ВВОДА СУММ =================
@dp.message(FinanceStates.waiting_for_income)
async def process_income(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record(message.from_user.id, "income", total, comment, breakdown)
        await sync_chat_backup(message.chat.id)
        
        calc = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(f"✅ <b>ЗП записана:</b> +{format_money(total)}{calc}\n📝 <i>{comment}</i>", parse_mode="HTML")
    except Exception:
        await message.answer("⚠️ Ошибка формата. Введи число или сумму через плюс: <code>2500+3000</code>", parse_mode="HTML")

@dp.message(FinanceStates.waiting_for_expense)
async def process_expense(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record(message.from_user.id, "expense", total, comment, breakdown)
        await sync_chat_backup(message.chat.id)
        
        calc = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(f"✅ <b>Трата записана:</b> -{format_money(total)}{calc}\n📝 <i>{comment}</i>", parse_mode="HTML")
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
