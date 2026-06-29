import logging
import calendar
from datetime import datetime, timedelta, date
from aiogram import types, F
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, and_

from db.models import AsyncSessionLocal, User, PersonalTask, TaskTemplate, TaskInstance
from bot.parser import get_recurrence_delta, clean_task_text
from bot.handlers.base import (
    bot, dp, ACTIVE_HOUSE_ID, logger, get_house_today_date, render_today,
    AddPersonalTaskState, format_calendar_header, create_calendar_keyboard_custom
)



@dp.message(F.text.in_({"📋 My", "👤 My", "👤 Мои дела"}))
async def today_handler(m: types.Message, db_user: User = None):
    await render_today(m, db_user, page=0)


@dp.callback_query(F.data.startswith("my_page:"))
async def handle_my_page(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    await render_today(call.message, db_user, is_callback=True, page=page)


@dp.callback_query(F.data == "t_cancel")
async def t_cancel(call: types.CallbackQuery, db_user: User = None):
    await render_today(call.message, db_user, True, page=0)


@dp.callback_query(F.data.startswith("my_add"))
async def handle_my_add(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 0
    await state.clear()
    await state.set_state(AddPersonalTaskState.waiting_for_text)
    await state.update_data(page=page)
    
    await call.message.edit_text(
        "✏️ <b>Новая личная задача</b>:\n\n"
        "Введите текст задачи (например: Купить хлеб).\n"
        "Если задача срочная, напишите слово <b>«срочно»</b>.",
        reply_markup=None,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("my_add_cancel"))
async def handle_my_add_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 0
    await state.clear()
    await render_today(call.message, db_user, is_callback=True, page=page)


@dp.message(StateFilter(AddPersonalTaskState.waiting_for_text))
async def handle_my_add_text(message: types.Message, state: FSMContext, db_user: User = None):
    text = message.text.strip()
    if not text:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
        
    state_data = await state.get_data()
    page = state_data.get("page", 0)
    
    import re
    is_urgent = "срочно" in text.lower()
    clean_text = re.sub(r'срочно', '', text, flags=re.IGNORECASE).strip().capitalize()
    
    db_text = f"🔴 {clean_text}" if is_urgent else clean_text
    
    await state.update_data(text=db_text)
    await state.set_state(AddPersonalTaskState.waiting_for_date)
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    d_today = today
    d_tomorrow = today + timedelta(days=1)

    builder = InlineKeyboardBuilder()
    # Nav Row 1
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text=f"{d_today.strftime('%d.%m')} ({days_ru[d_today.weekday()]})", callback_data="addpt_date:today"),
        InlineKeyboardButton(text=f"{d_tomorrow.strftime('%d.%m')} ({days_ru[d_tomorrow.weekday()]})", callback_data="addpt_date:tomorrow"),
        InlineKeyboardButton(text="Другая дата", callback_data="addpt_date:calendar")
    )

    
    await message.answer(
        f"📅 <b>Выберите дату выполнения</b> для задачи «{db_text}»:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@dp.callback_query(StateFilter(AddPersonalTaskState.waiting_for_date), F.data.startswith("addpt_date:"))
async def handle_addpt_date_btn(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    choice = call.data.split(":")[1]
    state_data = await state.get_data()
    page = state_data.get("page", 0)
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    if choice == "today":
        await state.update_data(date=today.isoformat())
        await ask_for_recurrence(call.message, state)
    elif choice == "tomorrow":
        tomorrow = today + timedelta(days=1)
        await state.update_data(date=tomorrow.isoformat())
        await ask_for_recurrence(call.message, state)
    elif choice == "calendar":
        markup = create_calendar_keyboard_custom(0, today.year, today.month, today, "addpt")
        header = format_calendar_header(today)
        await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(StateFilter(AddPersonalTaskState.waiting_for_date), F.data.startswith("cal_nav_addpt:"))
async def handle_cal_nav_addpt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    year = int(parts[2])
    month = int(parts[3])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(0, year, month, today, "addpt")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(StateFilter(AddPersonalTaskState.waiting_for_date), F.data.startswith("shift_addpt:"))
async def handle_shift_addpt(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    parts = call.data.split(":")
    date_str = parts[2]
    await state.update_data(date=date_str)
    await ask_for_recurrence(call.message, state)


async def ask_for_recurrence(message: types.Message, state: FSMContext):
    await state.set_state(AddPersonalTaskState.waiting_for_recurrence)
    state_data = await state.get_data()
    page = state_data.get("page", 0)
    
    builder = InlineKeyboardBuilder()
    # Nav Row 1
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="Единоразово", callback_data="addpt_period:once"),
        InlineKeyboardButton(text="Каждые X дней", callback_data="addpt_period:every_x_days")
    )

    
    try:
        await message.edit_text(
            "🔁 <b>Выберите периодичность задачи:</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception:
        await message.answer(
            "🔁 <b>Выберите периодичность задачи:</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )


@dp.callback_query(StateFilter(AddPersonalTaskState.waiting_for_recurrence), F.data.startswith("addpt_period:"))
async def handle_addpt_period(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    period = call.data.split(":")[1]
    state_data = await state.get_data()
    page = state_data.get("page", 0)
    
    if period == "once":
        text = state_data.get("text")
        date_str = state_data.get("date")
        exec_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        
        async with AsyncSessionLocal() as session:
            task = PersonalTask(
                user_id=db_user.id,
                text=text,
                date_execution=exec_date,
                category="inbox",
                recurrence=None,
                is_completed=False,
                is_deleted=False
            )
            session.add(task)
            await session.commit()
            
        await state.clear()
        await call.answer(f"✅ Добавил личную задачу: {text}", show_alert=False)
        await render_today(call.message, db_user, is_callback=True, page=page)
    else:
        await state.set_state(AddPersonalTaskState.waiting_for_recurrence_days)
        await call.message.edit_text(
            "Укажите число дней, с каким интервалом повторять задачу (например, 5):",
            reply_markup=None
        )


@dp.message(StateFilter(AddPersonalTaskState.waiting_for_recurrence_days))
async def handle_addpt_recurrence_days(message: types.Message, state: FSMContext, db_user: User = None):
    text_input = message.text.strip()
    try:
        days = int(text_input)
        if days <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Нужно положительное число дней! Попробуйте еще раз:")
        return
        
    state_data = await state.get_data()
    page = state_data.get("page", 0)
    text = state_data.get("text")
    date_str = state_data.get("date")
    exec_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        task = PersonalTask(
            user_id=db_user.id,
            text=text,
            date_execution=exec_date,
            category="inbox",
            recurrence=f"every_x_days:{days}",
            is_completed=False,
            is_deleted=False
        )
        session.add(task)
        await session.commit()
        
    await state.clear()
    await message.answer(f"✅ Добавил повторяющуюся задачу: *{text}* (каждые {days} дн.)", parse_mode="Markdown")
    await render_today(message, db_user, is_callback=False, page=page)



# Reschedule select menu
@dp.callback_query(F.data.startswith("my_shift_select"))
async def handle_my_shift_select(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 0
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.date_execution, PersonalTask.id)
        )
        personal_tasks_all = result.scalars().all()

        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress"
                )
            )
            .order_by(TaskInstance.date, TaskInstance.id)
        )
        my_chores_all = chores_result.all()

    all_dates = set()
    for pt in personal_tasks_all:
        all_dates.add(pt.date_execution)
    for inst, tmpl in my_chores_all:
        all_dates.add(inst.date)

    future_dates = sorted([d for d in all_dates if d > today])
    total_pages = 1 + len(future_dates)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    if page == 0:
        personal_tasks = [pt for pt in personal_tasks_all if pt.date_execution <= today]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date <= today]
    else:
        target_date = future_dates[page - 1]
        personal_tasks = [pt for pt in personal_tasks_all if pt.date_execution == target_date]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date == target_date]

    if not personal_tasks and not my_chores:
        await call.answer("Нет активных задач для переноса!", show_alert=False)
        return

    builder = InlineKeyboardBuilder()
    for t in personal_tasks:
        clean = clean_task_text(t.text)
        is_urgent = "🔴" in t.text or "срочно" in t.text.lower()
        prefix = "🔴 " if is_urgent else "🟡 " if t.date_execution < today else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}{clean}", callback_data=f"shift_pt_menu:{t.id}:{page}"))
        
    for inst, tmpl in my_chores:
        prefix = "🟡 " if inst.date < today else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}🏠 {tmpl.title}", callback_data=f"shift_chore_menu:{inst.id}:{page}"))

    await call.message.edit_text("🔄 Выберите задачу для переноса:", reply_markup=builder.as_markup())


# Reschedule choice menu (tomorrow/day after/calendar)
@dp.callback_query(F.data.startswith("shift_pt_menu:"))
async def handle_shift_pt_menu(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    
    nav_builder = InlineKeyboardBuilder()
    nav_builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    nav_builder.row(
        InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift_pt:{page}:{t_id}:{d1.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift_pt:{page}:{t_id}:{d2.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_pt:{t_id}:{page}")
    )
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=nav_builder.as_markup())


@dp.callback_query(F.data.startswith("shift_chore_menu:"))
async def handle_shift_chore_menu(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    
    nav_builder = InlineKeyboardBuilder()
    nav_builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    nav_builder.row(
        InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift_chore:{page}:{inst_id}:{d1.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift_chore:{page}:{inst_id}:{d2.strftime('%Y-%m-%d')}"),
        InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_chore:{inst_id}:{page}")
    )
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=nav_builder.as_markup())


# Calendar navigation & shift execution callbacks
@dp.callback_query(F.data.startswith("rc_months_pt:"))
async def handle_rc_months_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(t_id, today.year, today.month, today, f"pt:{page}")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_pt:"))
async def handle_cal_nav_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    if len(parts) >= 5:
        page = int(parts[1])
        t_id = int(parts[2])
        year = int(parts[3])
        month = int(parts[4])
    else:
        page = 0
        t_id = int(parts[1])
        year = int(parts[2])
        month = int(parts[3])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(t_id, year, month, today, f"pt:{page}")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_pt:"))
async def handle_shift_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    if len(parts) >= 4:
        page = int(parts[1])
        t_id = int(parts[2])
        date_str = parts[3]
    else:
        page = 0
        t_id = int(parts[1])
        date_str = parts[2]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            task.date_execution = new_date
            await session.commit()
            clean_text = clean_task_text(task.text)
            await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}!", show_alert=False)
            await call.message.answer(f"🔄 Задача '{clean_text}' перенесена на {new_date.strftime('%d.%m.%Y')}!")
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            
    await render_today(call.message, db_user, is_callback=False, page=page)


@dp.callback_query(F.data.startswith("rc_months_chore:"))
async def handle_rc_months_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(inst_id, today.year, today.month, today, f"chore:{page}")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_chore:"))
async def handle_cal_nav_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    if len(parts) >= 5:
        page = int(parts[1])
        inst_id = int(parts[2])
        year = int(parts[3])
        month = int(parts[4])
    else:
        page = 0
        inst_id = int(parts[1])
        year = int(parts[2])
        month = int(parts[3])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(inst_id, year, month, today, f"chore:{page}")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_chore:"))
async def handle_shift_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    if len(parts) >= 4:
        page = int(parts[1])
        inst_id = int(parts[2])
        date_str = parts[3]
    else:
        page = 0
        inst_id = int(parts[1])
        date_str = parts[2]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst:
            tmpl = await session.get(TaskTemplate, inst.template_id)
            inst.status = "shifted"
            
            exists = await session.scalar(
                select(TaskInstance).where(
                    and_(
                        TaskInstance.template_id == inst.template_id,
                        TaskInstance.date == new_date,
                        TaskInstance.status.in_(["free", "shifted"])
                    )
                )
            )
            if not exists:
                new_inst = TaskInstance(
                    template_id=inst.template_id,
                    date=new_date,
                    status="free",
                    priority=0
                )
                session.add(new_inst)
                
            await session.commit()
            title = tmpl.title if tmpl else "Домашнее дело"
            await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}!", show_alert=False)
            await call.message.answer(f"🔄 Задача '{title}' перенесена на {new_date.strftime('%d.%m.%Y')}!")
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            
    await render_today(call.message, db_user, is_callback=False, page=page)


# Delete select menu & callbacks
@dp.callback_query(F.data.startswith("my_delete_select"))
async def handle_my_delete_select(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 0
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.date_execution, PersonalTask.id)
        )
        personal_tasks_all = result.scalars().all()

        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress"
                )
            )
            .order_by(TaskInstance.date, TaskInstance.id)
        )
        my_chores_all = chores_result.all()

    all_dates = set()
    for pt in personal_tasks_all:
        all_dates.add(pt.date_execution)
    for inst, tmpl in my_chores_all:
        all_dates.add(inst.date)

    future_dates = sorted([d for d in all_dates if d > today])
    total_pages = 1 + len(future_dates)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    if page == 0:
        personal_tasks = [pt for pt in personal_tasks_all if pt.date_execution <= today]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date <= today]
    else:
        target_date = future_dates[page - 1]
        personal_tasks = [pt for pt in personal_tasks_all if pt.date_execution == target_date]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date == target_date]

    if not personal_tasks and not my_chores:
        await call.answer("Нет активных задач для удаления!", show_alert=False)
        return

    builder = InlineKeyboardBuilder()
    for t in personal_tasks:
        clean = clean_task_text(t.text)
        is_urgent = "🔴" in t.text or "срочно" in t.text.lower()
        prefix = "🔴 " if is_urgent else "🟡 " if t.date_execution < today else ""
        builder.row(InlineKeyboardButton(text=f"🗑 {prefix}{clean}", callback_data=f"del_pt:{t.id}:{page}"))
        
    for inst, tmpl in my_chores:
        prefix = "🟡 " if inst.date < today else ""
        builder.row(InlineKeyboardButton(text=f"🗑 {prefix}🏠 {tmpl.title}", callback_data=f"del_chore_inst:{inst.id}:{page}"))

    await call.message.edit_text("🗑 Выберите задачу для удаления:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("del_pt:"))
async def handle_del_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            clean_text = clean_task_text(task.text)
            await session.delete(task)
            await session.commit()
            await call.answer("🗑 Удалено!", show_alert=False)
            await call.message.answer(f"🗑 Личная задача '{clean_text}' полностью удалена!")
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            
    await render_today(call.message, db_user, is_callback=False, page=page)


@dp.callback_query(F.data.startswith("del_chore_inst:"))
async def handle_del_chore_inst(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst:
            tmpl = await session.get(TaskTemplate, inst.template_id)
            inst.status = "skipped"
            await session.commit()
            title = tmpl.title if tmpl else "Домашнее дело"
            await call.answer("🗑 Копия удалена!", show_alert=False)
            await call.message.answer(f"🗑 Копия домашнего дела '{title}' удалена на сегодня!")
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            
    await render_today(call.message, db_user, is_callback=False, page=page)


# ── Done task ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("done_task:"))
async def handle_done_task(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            task.is_completed = True
            task.completed_at = datetime.utcnow()
            if task.recurrence:
                delta = get_recurrence_delta(task.recurrence)
                clean = clean_task_text(task.text)
                new_task = PersonalTask(
                    user_id=task.user_id,
                    text=clean,
                    date_execution=await get_house_today_date(session) + delta,
                    category="inbox",
                    recurrence=task.recurrence,
                    is_completed=False,
                    is_deleted=False,
                )
                session.add(new_task)
            await session.commit()
    await render_today(call.message, db_user, is_callback=True, page=page)



# ── Plans ──────────────────────────────────────────────────────────────────────
async def render_plans(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution > today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.date_execution)
        )
        tasks = result.scalars().all()

    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    if tasks:
        text = "📅 *Предстоящие планы*\n\n"
        cur_date = None
        for t in tasks:
            ds = t.date_execution.strftime('%d.%m')
            dname = days_ru[t.date_execution.weekday()]
            if ds != cur_date:
                text += f"📌 _{ds} ({dname})_\n"
                cur_date = ds
            rec = " 🔁" if t.recurrence else ""
            text += f"• {t.text}{rec}\n"
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="➡️ Перенос", callback_data="p_menu_move"),
            InlineKeyboardButton(text="❌ Удалить", callback_data="p_menu_del"),
        )
    else:
        text = "📅 *Будущих планов пока нет.*"
        builder = InlineKeyboardBuilder()

    markup = builder.as_markup() if tasks else None
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text == "📅 Планы")
async def plans_handler(m: types.Message, db_user: User = None):
    await render_plans(m, db_user)


@dp.callback_query(F.data == "p_cancel")
async def p_cancel(call: types.CallbackQuery, db_user: User = None):
    await render_plans(call.message, db_user, True)


@dp.callback_query(F.data == "p_menu_move")
async def p_move_menu(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == db_user.id, PersonalTask.date_execution > today,
                     PersonalTask.is_completed == False, PersonalTask.is_deleted == False)
            ).order_by(PersonalTask.date_execution)
        )
        tasks = result.scalars().all()
    b = InlineKeyboardBuilder()
    cur = None
    for t in tasks:
        ds = t.date_execution.strftime('%d.%m')
        if ds != cur:
            b.row(InlineKeyboardButton(text=f"📅 {ds}", callback_data="ignore"))
            cur = ds
        b.row(InlineKeyboardButton(text=t.text, callback_data=f"mov_p:{t.id}"))
    await call.message.edit_text("Выбери задачу для переноса:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("mov_p:"))
async def mov_p_select(call: types.CallbackQuery):
    t_id = int(call.data.split(":")[1])
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"set_dt:pm:{t_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"set_dt:pm:{t_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_plan:{t_id}")
        ]
    ]
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "p_menu_del")
async def p_del_menu(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == db_user.id, PersonalTask.date_execution > today,
                     PersonalTask.is_completed == False, PersonalTask.is_deleted == False)
            ).order_by(PersonalTask.date_execution)
        )
        tasks = result.scalars().all()
    b = InlineKeyboardBuilder()
    for t in tasks:
        b.row(InlineKeyboardButton(text=f"❌ {t.text}", callback_data=f"del_p:{t.id}"))
    await call.message.edit_text("Выбери задачу для удаления:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("del_p:"))
async def exe_del_p(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            await session.delete(task)
            await session.commit()
    await call.answer("🗑 Удалено", show_alert=False)
    await render_plans(call.message, db_user, True)


# ── Archive tasks ─────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("t_arch:"))
async def t_archive(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == db_user.id, PersonalTask.is_completed == True, PersonalTask.is_deleted == False)
            ).order_by(PersonalTask.date_execution.desc(), PersonalTask.id.desc()).offset(page * 10).limit(10)
        )
        tasks = result.scalars().all()

    if not tasks and page == 0:
        await call.answer("Архив пуст!", show_alert=False)
        return

    text = "📜 *Архив задач*\n👉 _Тапни, чтобы вернуть на сегодня:_\n\n"
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"t_arch:{page-1}"))
    if len(tasks) == 10:
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"t_arch:{page+1}"))
    if nav:
        b.row(*nav)
        
    for t in tasks:
        clean = clean_task_text(t.text)
        ds = t.date_execution.strftime('%d.%m')
        b.row(InlineKeyboardButton(text=f"[{ds}] {clean}", callback_data=f"restore_t:{t.id}"))
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("restore_t:"))
async def restore_task(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        old = await session.get(PersonalTask, t_id)
        if old:
            clean = clean_task_text(old.text)
            new_task = PersonalTask(
                user_id=db_user.id,
                text=clean,
                date_execution=today,
                category="inbox",
                is_completed=False,
                is_deleted=False,
            )
            session.add(new_task)
            await session.commit()
    await call.answer("🔄 Задача восстановлена!", show_alert=False)
    await render_today(call.message, db_user, True)


