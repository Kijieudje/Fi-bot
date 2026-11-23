import asyncio
import html
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, ReplyKeyboardMarkup

# ================= НАСТРОЙКИ =================
# Токен берём ТОЛЬКО из переменной окружения BOT_TOKEN (в коде его хранить нельзя)
TOKEN = os.getenv("8835340952:AAGKvYplGHylAdVfGzO-KVRpnWMR7KsvI-M")
if not TOKEN:
    raise SystemExit("8835340952:AAGKvYplGHylAdVfGzO-KVRpnWMR7KsvI-M")

# Часовой пояс для дат и времени записей (на хостингах часто стоит UTC)
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Moscow"))

# Папка, где лежат личные базы: data/finance_<user_id>.db
DATA_DIR = os.getenv("DATA_DIR", "data")
# Старая общая база (после миграции будет переименована в finance.db.migrated)
OLD_DB = "finance.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()


def now_local() -> datetime:
    return datetime.now(TZ)


# ================= БАЗА ДАННЫХ (ОТДЕЛЬНЫЙ ФАЙЛ НА КАЖДОГО ПОЛЬЗОВАТЕЛЯ) =================
RECORD_COLUMNS = "r_type, amount, comment, breakdown, date_str, month_str, time_str"

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        r_type TEXT,       -- 'income' или 'expense'
        amount REAL,
        comment TEXT,
        breakdown TEXT,
        date_str TEXT,     -- YYYY-MM-DD
        month_str TEXT,    -- YYYY-MM
        time_str TEXT      -- HH:MM
    )
"""


def db_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"finance_{int(user_id)}.db")


def get_conn(user_id: int) -> sqlite3.Connection:
    """Открывает личную базу пользователя (создаёт файл и таблицу при первом обращении)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path(user_id))
    conn.execute(CREATE_TABLE_SQL)
    return conn


def fetch_all(user_id: int, sql: str, params: tuple = ()) -> list:
    with closing(get_conn(user_id)) as conn:
        return conn.execute(sql, params).fetchall()


def add_record(user_id: int, r_type: str, amount: float, comment: str, breakdown: str):
    now = now_local()
    with closing(get_conn(user_id)) as conn:
        conn.execute(
            f"INSERT INTO records ({RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                r_type, amount, comment, breakdown,
                now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.strftime("%H:%M"),
            ),
        )
        conn.commit()


def delete_last_record(user_id: int):
    with closing(get_conn(user_id)) as conn:
        row = conn.execute(
            "SELECT id, r_type, amount, comment FROM records ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            conn.execute("DELETE FROM records WHERE id = ?", (row[0],))
            conn.commit()
        return row


def migrate_old_db():
    """Разносит записи из старой общей finance.db по личным базам пользователей."""
    if not os.path.exists(OLD_DB):
        return
    try:
        with closing(sqlite3.connect(OLD_DB)) as old:
            user_ids = [
                r[0] for r in old.execute(
                    "SELECT DISTINCT user_id FROM records WHERE user_id IS NOT NULL"
                )
            ]
            for uid in user_ids:
                if os.path.exists(db_path(uid)):
                    continue  # личная база уже есть — не затираем
                rows = old.execute(
                    f"SELECT {RECORD_COLUMNS} FROM records WHERE user_id = ? ORDER BY id",
                    (uid,),
                ).fetchall()
                with closing(get_conn(uid)) as conn:
                    conn.executemany(
                        f"INSERT INTO records ({RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    conn.commit()
    except sqlite3.DatabaseError as e:
        print(f"Миграция старой базы пропущена: {e}")
        return
    os.replace(OLD_DB, OLD_DB + ".migrated")
    print("Старая общая база разнесена по личным и переименована в finance.db.migrated")


def restore_user_db(user_id: int, uploaded_path: str) -> int:
    """
    Восстанавливает ЛИЧНУЮ базу пользователя из присланного файла.
    Если в файле есть колонка user_id (старый общий формат), берутся только записи этого пользователя.
    Возвращает число восстановленных записей.
    """
    needed = {"r_type", "amount", "comment", "breakdown", "date_str", "month_str", "time_str"}
    with closing(sqlite3.connect(uploaded_path)) as src:
        cols = {r[1] for r in src.execute("PRAGMA table_info(records)")}
        if not needed.issubset(cols):
            raise ValueError("В файле нет подходящей таблицы records")
        sql = f"SELECT {RECORD_COLUMNS} FROM records"
        params: tuple = ()
        if "user_id" in cols:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        rows = src.execute(sql + " ORDER BY rowid", params).fetchall()

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = db_path(user_id) + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    with closing(sqlite3.connect(tmp_path)) as dst:
        dst.execute(CREATE_TABLE_SQL)
        dst.executemany(
            f"INSERT INTO records ({RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
        dst.commit()
    os.replace(tmp_path, db_path(user_id))
    return len(rows)


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def format_money(val: float) -> str:
    if float(val).is_integer():
        return f"{int(val):,}".replace(",", " ") + " ₽"
    return f"{val:,.2f}".replace(",", " ") + " ₽"


def parse_expression(text: str):
    text = text.strip()
    match = re.match(r"^([\d\s\+\,\.]+)(.*)$", text, re.DOTALL)
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
    breakdown_text = " + ".join(
        f"{int(n) if n.is_integer() else n:,}".replace(",", " ") for n in numbers
    )
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
        [KeyboardButton(text="💾 Бэкап базы")],
    ],
    resize_keyboard=True,
)


# ================= ОБРАБОТЧИКИ МЕНЮ =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    get_conn(message.from_user.id).close()  # заводим личную базу сразу при старте
    await message.answer(
        "👋 Привет! У каждого аккаунта своя независимая личная касса и своя база данных.\n\n"
        "✨ Суммы можно вводить через плюс: <code>2500+3000+5000</code>",
        reply_markup=main_kb,
        parse_mode="HTML",
    )


@dp.message(F.text == "➕ Записать ЗП")
async def btn_income(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_income)
    await message.answer(
        "💵 Введи сумму и комментарий. Например, <code>2500+3000 смена в Диане</code>.\n"
        "Категорию можно указать самому: <code>5000 Свои заказы: диван в Лобне</code>.\n"
        f"Категории: {', '.join(INCOME_PATTERNS)}, Прочее.", parse_mode="HTML"
    )


@dp.message(F.text == "➖ Записать трату")
async def btn_expense(message: types.Message, state: FSMContext):
    await state.set_state(FinanceStates.waiting_for_expense)
    await message.answer(
        "🛒 Введи сумму и комментарий, например <code>350+120 обед</code>.\n"
        "Чтобы выбрать категорию самому, начни комментарий с её названия: "
        "<code>450 Транспорт: домой</code>.\n"
        f"Категории: {', '.join(EXPENSE_PATTERNS)}, Прочее.", parse_mode="HTML"
    )


@dp.message(F.text == "↩️ Отменить последнюю")
async def btn_undo(message: types.Message, state: FSMContext):
    await state.clear()
    deleted = delete_last_record(message.from_user.id)
    if deleted:
        _, r_type, amount, comment = deleted
        label = "Доход" if r_type == "income" else "Расход"
        await message.answer(
            f"🗑 <b>Удалена последняя запись:</b>\n"
            f"{label}: <b>{format_money(amount)}</b> ({html.escape(comment)})",
            parse_mode="HTML",
        )
    else:
        await message.answer("У тебя нет записей для удаления.")


# ================= БЭКАП И ВОССТАНОВЛЕНИЕ (ТОЛЬКО ЛИЧНАЯ БАЗА) =================
@dp.message(F.text == "💾 Бэкап базы")
async def btn_backup(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not os.path.exists(db_path(user_id)):
        await message.answer("У тебя пока нет данных для бэкапа.")
        return
    backup_file = FSInputFile(db_path(user_id), filename=f"finance_{user_id}.db")
    await message.answer_document(
        document=backup_file, caption="💾 Твоя личная резервная копия базы"
    )


@dp.message(F.document)
async def restore_from_file(message: types.Message, state: FSMContext):
    await state.clear()
    name = message.document.file_name or ""
    if not name.lower().endswith(".db"):
        return

    user_id = message.from_user.id
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_upload = os.path.join(DATA_DIR, f"upload_{user_id}.tmp")
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, tmp_upload)
        count = restore_user_db(user_id, tmp_upload)
        await message.answer(
            f"✅ <b>Твоя база восстановлена!</b> Записей: <b>{count}</b>", parse_mode="HTML"
        )
    except (ValueError, sqlite3.DatabaseError):
        await message.answer("⚠️ Это не похоже на файл бэкапа этого бота. Базу не менял.")
    except Exception:
        await message.answer("⚠️ Не удалось загрузить файл. Базу не менял.")
    finally:
        if os.path.exists(tmp_upload):
            os.remove(tmp_upload)


# ================= СВОДКИ =================
# Названия категорий можно указывать в начале комментария, например «Еда: кофе».
EXPENSE_PATTERNS = {
    "Еда": r"еда|еду|продукт\w*|обед\w*|ужин\w*|завтрак\w*|перекус\w*|кофе|кафе|ресторан\w*|кфс|kfc|вкусвилл\w*|пят[её]рочк\w*",
    "Транспорт": r"транспорт\w*|такси|метро|проезд\w*|автобус\w*|электричк\w*|бензин\w*|парковк\w*",
    "Дом": r"дом|аренда|квартплат\w*|коммунал\w*|жкх",
    "Связь": r"связь|интернет\w*|мобильная связь|тариф\w*",
    "Работа": r"работа|химия|химию|расходник\w*|экстрактор\w*|оборудовани\w*",
    "Здоровье": r"здоровье|аптек\w*|лекарств\w*|врач\w*",
    "Развлечения": r"развлечени\w*|кино|клуб\w*|концерт\w*|боулинг",
}
INCOME_PATTERNS = {
    "Чаевые": r"чаевые|чаевых|чаи|чай",
    "Свои заказы": r"частн\w* заказ\w*|сво(?:й|и|его|ему|им|их) заказ\w*|собственн\w* заказ\w*|личн\w* заказ\w*|свой клиент|напрямую",
    "Смены": r"смен\w*|диан[аыеу]|зарплат\w*|зп|оклад\w*",
    "Продажи": r"продаж\w*|продал[аи]?",
}
CATEGORY_RULES = {
    kind: {name: re.compile(rf"\b(?:{words})\b", re.IGNORECASE) for name, words in patterns.items()}
    for kind, patterns in (("income", INCOME_PATTERNS), ("expense", EXPENSE_PATTERNS))
}


def record_category(kind, comment):
    rules = CATEGORY_RULES[kind]
    prefix, colon, _ = comment.partition(":")
    if colon:
        for name in (*rules, "Прочее", "Смешанные"):
            if prefix.strip().casefold() == name.casefold():
                return name
    matches = [name for name, pattern in rules.items() if pattern.search(comment)]
    if not matches:
        return "Прочее"
    return matches[0] if len(matches) == 1 else "Смешанные"


def summary_pages(lines):
    # Даже с эмодзи 2000 символов укладываются в одно сообщение.
    limit = 2000
    page = ""
    for line in lines:
        if len(page) + 1 + len(line) > limit and page.strip():
            yield page.rstrip("\n")
            page = ""
        while len(line) > limit:
            yield line[:limit]
            line = line[limit:]
        if line or page:
            page = f"{page}\n{line}" if page else line
    if page.strip():
        yield page.rstrip("\n")


def summary_lines(rows, title, period):
    groups = {"income": {}, "expense": {}}
    totals = {"income": 0.0, "expense": 0.0}
    work_days = set()
    months = {}
    for kind, amount, comment, breakdown, day, month, clock in rows:
        comment = (comment or "").strip() or "Без комментария"
        totals[kind] += amount
        if kind == "income" and day:
            work_days.add(day)
        if period == "all":
            month_totals = months.setdefault(month or "", {"income": 0.0, "expense": 0.0})
            month_totals[kind] += amount

        date_label = ".".join(reversed(day.split("-"))) if day else "Без даты"
        stamp = (clock or date_label) if period == "day" else date_label
        calc = f" ({breakdown})" if breakdown and " + " in breakdown else ""
        sign = "+" if kind == "income" else "−"
        entry = f"{stamp} · {sign}{format_money(amount)}{calc} — {comment}"
        category = record_category(kind, comment)
        groups[kind].setdefault(category, []).append((amount, entry))

    income, expense = totals["income"], totals["expense"]
    yield title
    yield f"➕ Заработано: {format_money(income)}"
    yield f"➖ Потрачено: {format_money(expense)}"
    yield f"💰 Остаток: {format_money(income - expense)}"
    if period != "day":
        days = len(work_days)
        avg = income / days if days else 0
        yield f"💼 Дней с доходом: {days} (в среднем {format_money(avg)}/день)"
    if months:
        yield ""
        yield "📆 По месяцам"
        for month, values in sorted(months.items()):
            label = ".".join(reversed(month.split("-"))) or "Без месяца"
            inc, exp = values["income"], values["expense"]
            yield f"{label} · +{format_money(inc)} | −{format_money(exp)} | остаток {format_money(inc - exp)}"

    for kind, heading in (("income", "💵 Доходы"), ("expense", "🛒 Траты")):
        categories = groups[kind]
        if categories:
            yield ""
            yield heading
            subtotals = {name: sum(amount for amount, _ in items) for name, items in categories.items()}
            for name in sorted(categories, key=subtotals.get, reverse=True):
                yield f"{name} — {format_money(subtotals[name])}"
                for _, entry in categories[name]:
                    yield entry


async def send_summary(message, state, period):
    await state.clear()
    now = now_local()
    filters = {
        "day": (" WHERE date_str = ?", (now.strftime("%Y-%m-%d"),), f"📊 Сводка за {now:%d.%m.%Y}", "За сегодня записей пока нет."),
        "month": (" WHERE month_str = ?", (now.strftime("%Y-%m"),), f"📅 Сводка за {now:%m.%Y}", "В этом месяце записей пока нет."),
        "all": ("", (), "📈 Общая сводка за всё время", "У тебя пока нет записей."),
    }
    where, params, title, empty = filters[period]
    rows = fetch_all(
        message.from_user.id,
        f"SELECT {RECORD_COLUMNS} FROM records{where} ORDER BY date_str, time_str, id",
        params,
    )
    if not rows:
        await message.answer(empty)
        return
    for page in summary_pages(summary_lines(rows, title, period)):
        while True:
            try:
                await message.answer(page)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)


@dp.message(F.text == "📊 Сводка за день")
async def btn_day_summary(message: types.Message, state: FSMContext):
    await send_summary(message, state, "day")


@dp.message(F.text == "📅 Сводка за месяц")
async def btn_month_summary(message: types.Message, state: FSMContext):
    await send_summary(message, state, "month")


@dp.message(F.text == "📈 Общая сводка")
async def btn_total_summary(message: types.Message, state: FSMContext):
    await send_summary(message, state, "all")


# ================= ОБРАБОТКА ВВОДА СУММ =================
@dp.message(FinanceStates.waiting_for_income)
async def process_income(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record(message.from_user.id, "income", total, comment, breakdown)

        calc = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(
            f"✅ <b>ЗП записана:</b> +{format_money(total)}{calc}\n📝 <i>{html.escape(comment)}</i>",
            parse_mode="HTML",
        )
    except Exception:
        await message.answer(
            "⚠️ Ошибка формата. Введи число или сумму через плюс: <code>2500+3000</code>",
            parse_mode="HTML",
        )


@dp.message(FinanceStates.waiting_for_expense)
async def process_expense(message: types.Message):
    try:
        total, comment, breakdown = parse_expression(message.text)
        add_record(message.from_user.id, "expense", total, comment, breakdown)

        calc = f" ({breakdown})" if "+" in message.text else ""
        await message.answer(
            f"✅ <b>Трата записана:</b> -{format_money(total)}{calc}\n📝 <i>{html.escape(comment)}</i>",
            parse_mode="HTML",
        )
    except Exception:
        await message.answer(
            "⚠️ Ошибка формата. Введи число или сумму через плюс: <code>450+120 обед</code>",
            parse_mode="HTML",
        )


@dp.message()
async def fallback_handler(message: types.Message):
    await message.answer("Выбери действие на кнопках внизу 👇", reply_markup=main_kb)


# ================= ЗАПУСК =================
async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    migrate_old_db()
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
