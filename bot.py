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
                user_id INTEGER,
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

def add_record(r_type: str, amount: float, comment: str, breakdown: str):
    now = datetime.now()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO records (user_id, r_type, amount, comment, breakdown, date_str, month_str, time_str)
            VALUES (0, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r_type, amount, comment, breakdown,
            now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.strftime("%H:%M")
        ))
        conn.commit()

def delete_last_record():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, r_type, amount, comment FROM records ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM records WHERE id = ?", (row[0],))
            conn.commit()
        return row

# ================= БЭКАП ПРЯМО В ЧАТ (И В ЗАКРЕП) =================
def export_backup_text() -> str:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT r_type, amount, comment, breakdown, date_str, month_str, time_str FROM records ORDER BY id ASC")
        rows = cursor.fetchall()

    # Компактный JSON со всеми записями
    data_json = json.dumps(rows, ensure_ascii=False)
    return (
        f"<b>{BACKUP_HEADER}</b>\n"
        f"📅 Обновлено: {datetime.now().strftime('%d.%m.%Y
