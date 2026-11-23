    for kind, amount, comment, day in rows:
        comment = (comment or "").strip() or "Без комментария"
        date_label = f"{day[8:10]}.{day[5:7]}" if day else "Без даты"
        entry = f"{date_label} · {format_money(amount)} — {comment}"
        if kind == "income":
            salaries.append(entry)
        elif kind == "expense":
            category = expense_category(comment)
            expenses.setdefault(category, []).append((amount, entry))

    if salaries:
        lines.extend(["", "💵 Зарплата", *salaries])
    if expenses:
        lines.extend(["", "🛒 Траты по категориям"])
        totals = {name: sum(amount for amount, _ in items) for name, items in expenses.items()}
        for name in sorted(expenses, key=totals.get, reverse=True):
            lines.append(f"\n{name} — {format_money(totals[name])}")
            lines.extend(entry for _, entry in expenses[name])

    for page in monthly_pages(lines):
        await message.answer(page)


@dp.message(F.text == "📈 Общая сводка")
async def btn_total_summary(message: types.Message, state: FSMContext):
    await state.clear()

    rows = fetch_all(
        message.from_user.id,
        "SELECT r_type, amount, date_str, month_str FROM records ORDER BY month_str ASC",
    )

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

    m_lines = []
    for ms, d in months.items():
        year, month = ms.split("-")
        m_lines.append(
            f"▫️ <b>{month}.{year}:</b> +{format_money(d['income'])} | -{format_money(d['expense'])} "
            f"<i>(чистыми: {format_money(d['income'] - d['expense'])})</i>"
        )

    await message.answer(
        f"📈 <b>Общая сводка за всё время:</b>\n\n"
        f"➕ Всего заработано: <b>{format_money(total_inc)}</b>\n"
        f"➖ Всего потрачено: <b>{format_money(total_exp)}</b>\n"
        f"💎 <b>Чистый итог: {format_money(total_inc - total_exp)}</b>\n\n"
        f"💼 Дней с доходом: <b>{work_days}</b> ({format_money(avg)}/день)\n\n"
        f"<b>По месяцам:</b>\n" + "\n".join(m_lines),
        parse_mode="HTML",
    )


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
