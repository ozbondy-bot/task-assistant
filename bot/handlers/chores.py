import logging
import calendar
from datetime import datetime, timedelta, date
from aiogram import types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, Date

from db.models import AsyncSessionLocal, User, House, TaskTemplate, TaskInstance, Completion, PendingAction
from bot.handlers.base import (
    bot, dp, ACTIVE_HOUSE_ID, ALLOWED_TELEGRAM_IDS, logger,
    get_partner_user, get_house_today_date, generate_daily_chores_if_needed,
    render_today, get_main_keyboard, EditTemplateState, AddTemplateState,
        format_calendar_header, find_scheduled_date_on_or_after,
    get_template_next_date, get_template_next_date_val, get_period_label,
    create_calendar_keyboard_custom
)



# ── Household Chores (Домашние дела) ──────────────────────────────────────────
def period_label_ru(p: str) -> str:
    mapping = {
        "daily": "каждый день",
        "weekly": "раз в неделю",
        "twice_weekly": "2 раза в неделю",
        "monthly": "раз в месяц",
        "twice_monthly": "2 раза в месяц",
        "quarterly": "раз в квартал"
    }
    return mapping.get(p, p)


async def render_household_chores(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        await generate_daily_chores_if_needed(session, ACTIVE_HOUSE_ID)
        result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskInstance.date <= today,
                    TaskInstance.status == "free",
                    TaskTemplate.deleted == False
                )
            )
            .order_by(TaskTemplate.points.desc())
        )
        chores = result.all()

    total_cookies = sum(tmpl.points for inst, tmpl in chores)

    text = (
        "🌅 Привет!\n"
        f"🎯 Сегодня можно залутать {total_cookies} 🍪\n"
        "👉 Чтобы взять задачу в работу, нажми на её название 👇"
    )

    builder = InlineKeyboardBuilder()

    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )

    if chores:
        for inst, tmpl in chores:
            pts_prefix = "🟡 " if inst.date < today else ""
            # We hardcode Points to display 2-8🍪 for 'Готовка' to match the old bot
            pts_str = f"{pts_prefix}2-8🍪 ℹ️" if tmpl.title == "Готовка" else f"{pts_prefix}{tmpl.points}🍪 ℹ️"
            builder.row(
                InlineKeyboardButton(text=tmpl.title, callback_data=f"claim_chore:{inst.id}"),
                InlineKeyboardButton(text=pts_str, callback_data=f"tmpl_set:{tmpl.id}:today")
            )

    # Bottom buttons moved to Row 2
    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text.in_({"🏠 Home", "🏠 Домашние дела"}))
async def handle_household_chores_btn(message: types.Message, db_user: User = None):
    await render_household_chores(message, db_user)


@dp.callback_query(F.data == "home_view")
async def handle_home_view(call: types.CallbackQuery, state: FSMContext = None, db_user: User = None):
    if state:
        await state.clear()
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data == "chores_back")
async def handle_chores_back(call: types.CallbackQuery, db_user: User = None):
    await render_household_chores(call.message, db_user, is_callback=True)

@dp.callback_query(F.data == "chores_add_menu")
async def handle_chores_add_menu(call: types.CallbackQuery, db_user: User = None):
    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="⚡➕ Добавить⚡", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    
    # Row 3 (Add options — always persisted)
    builder.row(
        InlineKeyboardButton(text="Добавить из базы", callback_data="add_from_templates_list"),
        InlineKeyboardButton(text="Создать новую", callback_data="add_tmpl_start")
    )
    await call.message.edit_text(
        "➕ *Добавить задачу:*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "add_from_templates_list")
async def handle_add_from_templates_list(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        subq = select(TaskInstance.template_id).where(
            TaskInstance.status.in_(["free", "in_progress", "shifted"])
        )
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.deleted == False,
                    TaskTemplate.id.not_in(subq)
                )
            )
        )
        templates = result.scalars().all()

        # Compute next date for each and sort ascending (nearest first)
        tmpl_with_dates = []
        for t in templates:
            last_done_date, nd = await get_template_next_date_val(session, t, today)
            tmpl_with_dates.append((t, last_done_date, nd))
        
        # Exclude once templates that are already completed (nd.year >= 2099)
        tmpl_with_dates = [
            (t, ldd, nd) for (t, ldd, nd) in tmpl_with_dates
            if not (t.periodicity == "once" and nd and nd.year >= 2099)
        ]
        
        from datetime import date
        tmpl_with_dates.sort(key=lambda x: x[2] if x[2] is not None else date(2100, 12, 31))


        builder = InlineKeyboardBuilder()
        # Row 1 (Main Tabs)
        builder.row(
            InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
            InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
            InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
        )
        # Row 2 (Sub-tabs)
        builder.row(
            InlineKeyboardButton(text="⚡➕ Добавить⚡", callback_data="chores_add_menu"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
        )
        # Row 3 (Add options — Добавить из базы active)
        builder.row(
            InlineKeyboardButton(text="⚡Добавить из базы⚡", callback_data="noop"),
            InlineKeyboardButton(text="Создать новую", callback_data="add_tmpl_start")
        )
        if tmpl_with_dates:
            text = "📋 *Выберите задачу для добавления на сегодня:*"
            for t, last_done_date, nd in tmpl_with_dates:
                pts_str = "2-8" if t.title == "Готовка" else str(t.points)
                if nd and nd.year < 2099:
                    date_suffix = f" {nd.strftime('%d.%m.')}"
                else:
                    date_suffix = ""
                builder.row(
                    InlineKeyboardButton(text=t.title, callback_data=f"spawn_chore:{t.id}"),
                    InlineKeyboardButton(text=f"{pts_str}🍪{date_suffix}", callback_data=f"spawn_chore:{t.id}")
                )
        else:
            text = "⚠️ Шаблонов дел пока нет!"

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("spawn_chore:"))
async def handle_spawn_chore(call: types.CallbackQuery, db_user: User = None):
    tmpl_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        tmpl = await session.get(TaskTemplate, tmpl_id)
        if tmpl:
            inst = TaskInstance(
                template_id=tmpl.id,
                date=today,
                status="free",
                priority=0
            )
            session.add(inst)
            await session.commit()
            await call.answer(f"✅ Добавлено на сегодня: {tmpl.title}", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
    await render_household_chores(call.message, db_user, is_callback=True)


NUDGE_PHRASES = [
    "Домовой жалуется на беспорядок! Тут плачет без внимания: <b>{task_title}</b> 🥺",
    "Печеньки 🍪 сами себя не заработают! Тебя ждет отличный контракт: <b>{task_title}</b>",
    "Кажется, кто-то очень хочет, чтобы эта задача решилась. Герой, твой выход: <b>{task_title}</b> 🦸‍♂️",
    "Министерство уюта напоминает! Открыта горячая вакансия на дело: <b>{task_title}</b> 🔥",
    "Освободилось немного времени? Идеальный момент, чтобы закрыть: <b>{task_title}</b> ✨"
]
nudge_cache = {}


def create_calendar_keyboard(tid: int, year: int, month: int, today_date: date) -> InlineKeyboardMarkup:
    kb = []
    month_names_ru = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    kb.append([InlineKeyboardButton(text=f"🗓 {month_names_ru[month-1]} {year}", callback_data="noop")])
    
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    
    kb.append([
        InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"cal_nav:{tid}:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text="След. ➡️", callback_data=f"cal_nav:{tid}:{next_y}:{next_m}")
    ])
    
    weeks_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    kb.append([InlineKeyboardButton(text=w, callback_data="noop") for w in weeks_ru])
    
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    
    for week in month_days:
        row = []
        for day in week:
            if day == 0: 
                row.append(InlineKeyboardButton(text=" ", callback_data="noop"))
            else:
                btn_text = f"[{day}]" if year == today_date.year and month == today_date.month and day == today_date.day else str(day)
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"shift:once:{tid}:{year}-{month:02d}-{day:02d}"))
        kb.append(row)
        
    return InlineKeyboardMarkup(inline_keyboard=kb)




async def redirect_to_template_settings(message: types.Message, tid: int, src: str, db_user: User, is_callback=True):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        tmpl = await session.get(TaskTemplate, tid)
        if not tmpl:
            if src == "today_list":
                await render_today(message, db_user, is_callback=is_callback)
            else:
                await render_chores_settings(message, db_user, is_callback=is_callback)
            return
            
        last_done_date, nd = await get_template_next_date_val(session, tmpl, today)
        last_done_str = last_done_date.strftime("%d.%m.%Y") if last_done_date else "никогда"
        next_done_str = nd.strftime("%d.%m.%Y") if nd.year < 2099 else "никогда"

    period_lbl = get_period_label(tmpl).capitalize()
    pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)

    back_target = "chores_settings"
    if src == "today_list":
        back_target = f"tmpl_set:{tmpl.id}:today"
    elif src.startswith("chores_arch_"):
        page_val = src.replace("chores_arch_", "")
        back_target = f"chores_arch:{page_val}"

    text = (
        f"⚙️ <b>Настройки:</b> {tmpl.title}\n"
        f"Награда: {pts_str}🍪 | {period_lbl}\n"
        f"📅 last: {last_done_str}\n"
        f"🔮 next: {next_done_str}"
    )

    builder = InlineKeyboardBuilder()
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚡⚙️ Настройки⚡", callback_data="chores_settings")
    )
    builder.row(
        InlineKeyboardButton(text="Имя", callback_data=f"te_f:title:{tmpl.id}:{src}"),
        InlineKeyboardButton(text="Цикл", callback_data=f"te_f:period:{tmpl.id}:{src}"),
        InlineKeyboardButton(text="🍪", callback_data=f"te_f:points:{tmpl.id}:{src}"),
    )
    builder.row(
        InlineKeyboardButton(text="📅 Сдвиг", callback_data=f"te_shift_start:{tmpl.id}:{src}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"te_del_confirm:{tmpl.id}:{src}")
    )
    if is_callback:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("tmpl_set:"))
async def handle_tmpl_set(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tmpl_id = int(parts[1])
    src = parts[2]
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        tmpl = await session.get(TaskTemplate, tmpl_id)
        if not tmpl:
            await call.answer("Шаблон не найден", show_alert=False)
            return
            
        last_done_date, nd = await get_template_next_date_val(session, tmpl, today)
        last_done_str = last_done_date.strftime("%d.%m.%Y") if last_done_date else "никогда"
        next_done_str = nd.strftime("%d.%m.%Y") if nd.year < 2099 else "нет"

    period_lbl = get_period_label(tmpl).capitalize()
    pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)

    if src == "today" or src.startswith("chores_arch_"):
        if src == "today":
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TaskInstance).where(
                        and_(
                            TaskInstance.template_id == tmpl.id,
                            TaskInstance.date <= today,
                            TaskInstance.status == "free"
                        )
                    )
                )
                inst = result.scalars().first()
                if not inst:
                    result = await session.execute(
                        select(TaskInstance).where(
                            and_(
                                TaskInstance.template_id == tmpl.id,
                                TaskInstance.status.in_(["free", "shifted", "in_progress"])
                            )
                        ).order_by(TaskInstance.id.desc())
                    )
                    inst = result.scalars().first()
                    
                if not inst:
                    await call.answer("Копия задачи не найдена", show_alert=False)
                    return
                    
                inst_id = inst.id

            builder = InlineKeyboardBuilder()
            # Row 1 (Main Tabs)
            builder.row(
                InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
                InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
            )
            # Row 2 (Sub-tabs)
            builder.row(
                InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
            )
            builder.row(
                InlineKeyboardButton(text="🔔 Намек", callback_data=f"nudge:{inst_id}"),
                InlineKeyboardButton(text="📅 Сдвиг", callback_data=f"resched_menu:{inst_id}"),
                InlineKeyboardButton(text="🗑 Копию", callback_data=f"del_inst:{inst_id}")
            )

        else:
            builder = InlineKeyboardBuilder()

        text = (
            f"📋{tmpl.title} {pts_str}🍪\n"
            f"⏱️{period_lbl}\n"
            f"📅last: {last_done_str}\n"
            f"🔮next : {next_done_str}"
        )
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=None)
    else:
        await redirect_to_template_settings(call.message, tmpl.id, src, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("te_cancel:"))
async def handle_te_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    parts = call.data.split(":")
    tid = int(parts[1])
    src = parts[2]
    await state.clear()
    await call.answer("Отменено", show_alert=False)
    await redirect_to_template_settings(call.message, tid, src, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("te_f:title:"))
async def handle_te_title_start(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    tid = int(parts[2])
    src = parts[3]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_title)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚡⚙️ Настройки⚡", callback_data="chores_settings")
    )
    sent_msg = await call.message.edit_text("Введите новое название для шаблона:", reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=sent_msg.message_id)


@dp.message(StateFilter(EditTemplateState.waiting_for_title))
async def handle_te_title_input(message: types.Message, state: FSMContext, db_user: User = None):
    title = message.text.strip()
    if not title:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    last_msg_id = data.get("last_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    import json
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if not tmpl:
            await state.clear()
            return
            
        partner = await get_partner_user(session, db_user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=db_user.id,
                action_type="edit_template",
                data_payload=json.dumps({
                    "template_id": tid,
                    "field": "title",
                    "new_value": title
                })
            )
            session.add(pending)
            await session.commit()
            
            partner_text = (
                "🔔 *Согласование изменения задачи!*\\n\\n"
                f"Жилец *{db_user.display_name or db_user.username or 'Партнёр'}* хочет изменить название домашней задачи:\\n"
                f"❌ *Было:* {tmpl.title}\\n"
                f"✅ *Станет:* {title}\\n\\n"
                "Одобряете изменение?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            await state.clear()
            builder_nav = InlineKeyboardBuilder()
            builder_nav.row(
                InlineKeyboardButton(text="Home", callback_data="home_view"),
                InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
            )
            if last_msg_id:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=last_msg_id,
                    text=f"⏳ Запрос на переименование задачи в «{title}» отправлен партнёру.",
                    reply_markup=builder_nav.as_markup()
                )
        else:
            tmpl.title = title
            await session.commit()
            await state.clear()
            if last_msg_id:
                message.message_id = last_msg_id
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=True)
            else:
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("te_f:points:"))
async def handle_te_points_start(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    tid = int(parts[2])
    src = parts[3]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_points)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    sent_msg = await call.message.edit_text("Введите новое количество баллов (печенек):", reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=sent_msg.message_id)


@dp.message(StateFilter(EditTemplateState.waiting_for_points))
async def handle_te_points_input(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        pts = int(message.text.strip())
        if pts < 0:
            raise ValueError()
    except ValueError:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    last_msg_id = data.get("last_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    import json
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if not tmpl:
            await state.clear()
            return
            
        partner = await get_partner_user(session, db_user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=db_user.id,
                action_type="edit_template",
                data_payload=json.dumps({
                    "template_id": tid,
                    "field": "points",
                    "new_value": pts
                })
            )
            session.add(pending)
            await session.commit()
            
            partner_text = (
                "🔔 *Согласование изменения задачи!*\\n\\n"
                f"Жилец *{db_user.display_name or db_user.username or 'Партнёр'}* хочет изменить количество печенек:\\n"
                f"❌ *Было:* {tmpl.points}\\n"
                f"✅ *Станет:* {pts}\\n\\n"
                "Одобряете изменение?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            await state.clear()
            builder_nav = InlineKeyboardBuilder()
            builder_nav.row(
                InlineKeyboardButton(text="Home", callback_data="home_view"),
                InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
            )
            if last_msg_id:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=last_msg_id,
                    text=f"⏳ Запрос на изменение печенек задачи отправлен партнёру.",
                    reply_markup=builder_nav.as_markup()
                )
        else:
            tmpl.points = pts
            await session.commit()
            await state.clear()
            if last_msg_id:
                message.message_id = last_msg_id
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=True)
            else:
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("te_f:period:"))
async def handle_te_period_start(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    tid = int(parts[2])
    src = parts[3]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_periodicity)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    builder.row(
        InlineKeyboardButton(text="1 Раз", callback_data="te_period_sel:once"),
        InlineKeyboardButton(text="Каждый день", callback_data="te_period_sel:daily"),
        InlineKeyboardButton(text="Каждые X дней", callback_data="te_period_sel:every_x_days")
    )
    sent_msg = await call.message.edit_text("Выберите периодичность для шаблона:", reply_markup=builder.as_markup())
    await state.update_data(last_msg_id=sent_msg.message_id)


@dp.callback_query(StateFilter(EditTemplateState.waiting_for_periodicity), F.data.startswith("te_period_sel:"))
async def handle_te_period_selected(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    p = call.data.split(":")[1]
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    
    import json
    if p in ["once", "daily"]:
        async with AsyncSessionLocal() as session:
            tmpl = await session.get(TaskTemplate, tid)
            if not tmpl:
                await call.answer("Шаблон не найден")
                await state.clear()
                return
            partner = await get_partner_user(session, db_user.id)
            if partner:
                pending = PendingAction(
                    house_id=ACTIVE_HOUSE_ID,
                    initiator_id=db_user.id,
                    action_type="edit_template",
                    data_payload=json.dumps({
                        "template_id": tid,
                        "field": "periodicity",
                        "periodicity": p,
                        "period_days": 0 if p == "once" else 1
                    })
                )
                session.add(pending)
                await session.commit()
                
                partner_text = (
                    "🔔 *Согласование изменения задачи!*\\n\\n"
                    f"Жилец *{db_user.display_name or db_user.username or 'Партнёр'}* хочет изменить цикл задачи.\\n\\n"
                    "Одобряете изменение?"
                )
                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
                )
                try:
                    await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to send approval message to partner: {e}")
                await state.clear()
                builder_nav = InlineKeyboardBuilder()
                builder_nav.row(
                    InlineKeyboardButton(text="Home", callback_data="home_view"),
                    InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                    InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
                )
                await call.message.edit_text(f"⏳ Запрос на изменение цикла задачи отправлен партнёру.", reply_markup=builder_nav.as_markup())
            else:
                tmpl.periodicity = p
                tmpl.period_days = 0 if p == "once" else 1
                await session.commit()
                await call.answer("✅ Периодичность обновлена!", show_alert=False)
                await state.clear()
                await redirect_to_template_settings(call.message, tid, src, db_user, is_callback=True)
    else:
        await state.set_state(EditTemplateState.waiting_for_period_days)
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="Home", callback_data="home_view"),
            InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
            InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
        )
        builder.row(
            InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
        )
        sent_msg = await call.message.edit_text("Укажите число дней, с каким интервалом повторять задачу (например, 5):", reply_markup=builder.as_markup())
        await state.update_data(last_msg_id=sent_msg.message_id)


@dp.message(StateFilter(EditTemplateState.waiting_for_period_days))
async def handle_te_period_days_input(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError()
    except ValueError:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    last_msg_id = data.get("last_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    import json
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if not tmpl:
            await state.clear()
            return
            
        partner = await get_partner_user(session, db_user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=db_user.id,
                action_type="edit_template",
                data_payload=json.dumps({
                    "template_id": tid,
                    "field": "periodicity",
                    "periodicity": "every_x_days",
                    "period_days": days
                })
            )
            session.add(pending)
            await session.commit()
            
            partner_text = (
                "🔔 *Согласование изменения задачи!*\\n\\n"
                f"Жилец *{db_user.display_name or db_user.username or 'Партнёр'}* хочет изменить цикл задачи на каждые {days} дней.\\n\\n"
                "Одобряете изменение?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            await state.clear()
            builder_nav = InlineKeyboardBuilder()
            builder_nav.row(
                InlineKeyboardButton(text="Home", callback_data="home_view"),
                InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
            )
            if last_msg_id:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=last_msg_id,
                    text=f"⏳ Запрос на изменение печенек задачи отправлен партнёру.",
                    reply_markup=builder_nav.as_markup()
                )
        else:
            tmpl.periodicity = "every_x_days"
            tmpl.period_days = days
            await session.commit()
            await state.clear()
            if last_msg_id:
                message.message_id = last_msg_id
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=True)
            else:
                await redirect_to_template_settings(message, tid, src, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("te_del:"))
async def handle_te_del(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tid = int(parts[1])
    src = parts[2]
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if tmpl:
            tmpl.deleted = True
            await session.commit()
            await call.answer("🗑 Шаблон удален!", show_alert=False)
        else:
            await call.answer("Шаблон не найден", show_alert=False)
            
    if src == "today_list":
        await render_today(call.message, db_user, is_callback=True)
    else:
        await render_chores_settings(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("te_del_confirm:"))
async def handle_te_del_confirm(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tid = int(parts[1])
    src = parts[2]
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        name = tmpl.title if tmpl else "задача"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"te_del:{tid}:{src}")
    )
    await call.message.edit_text(f"Точно удалить «{name}»?", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("nudge:"))
async def handle_nudge(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        if nudge_cache.get(inst_id) == today:
            await call.answer("Тише-тише, намек уже отправлен. Ждем реакции! 🤫", show_alert=False)
            return
            
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("Задача не найдена", show_alert=False)
            return
            
        tmpl = await session.get(TaskTemplate, inst.template_id)
        import random
        phrase = random.choice(NUDGE_PHRASES).format(task_title=tmpl.title)
        nudge_cache[inst_id] = today
        
        result = await session.execute(
            select(User).where(
                and_(
                    User.house_id == db_user.house_id,
                    User.id != db_user.id
                )
            )
        )
        others = result.scalars().all()
        
        for other in others:
            try:
                await bot.send_message(
                    chat_id=other.telegram_id,
                    text=f"🔔 *Намек от {db_user.display_name}*\n\n{phrase}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to send nudge to {other.telegram_id}: {e}")
                
    await call.answer("Намек успешно отправлен! 🔔", show_alert=False)
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("resched_menu:"))
async def handle_resched_menu(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        inst = await session.get(TaskInstance, inst_id)
        tmpl_id = inst.template_id if inst else 0
        
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift:once:{inst_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift:once:{inst_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months:{inst_id}")
        ]
    ]
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("shift:once:"))
async def handle_shift_once(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[2])
    date_str = parts[3]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("Ошибка: задача не найдена.", show_alert=False)
            return
            
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
        
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("del_inst:"))
async def handle_del_inst(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
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
            
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("rc_months:"))
async def handle_rc_months(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard(inst_id, today.year, today.month, today)
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav:"))
async def handle_cal_nav(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard(inst_id, year, month, today)
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data == "chores_leaderboard")
async def handle_chores_leaderboard(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        leaderboard_result = await session.execute(
            select(User)
            .where(User.house_id == ACTIVE_HOUSE_ID)
            .order_by(User.points.desc())
        )
        leaderboard = leaderboard_result.scalars().all()

    text = "🏆 *Рейтинг участников:*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, usr in enumerate(leaderboard):
        medal = medals[idx] if idx < len(medals) else "👤"
        text += f"{medal} {usr.display_name} — `{usr.points or 0} 🍪`\n"

    await call.message.edit_text(text, reply_markup=None, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("claim_chore:"))
async def handle_claim_chore(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst:
            if inst.status == "free":
                inst.status = "in_progress"
                inst.done_by_user_id = db_user.id
                await session.commit()
                await call.answer("🏠 Задача взята в работу! Она добавлена в «Мои дела».", show_alert=False)
            else:
                await call.answer("⚠️ Кто-то уже взял эту задачу!", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
    await render_household_chores(call.message, db_user, is_callback=True)


async def render_chores_settings(message: types.Message, db_user: User = None, is_callback=False):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.deleted == False
                )
            ).order_by(TaskTemplate.id)
        )
        templates = result.scalars().all()

        builder = InlineKeyboardBuilder()
        
        # Row 1 (Main Tabs)
        builder.row(
            InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="home_view"),
            InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
            InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
        )
        
        # Row 2 (Sub-tabs)
        builder.row(
            InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
            InlineKeyboardButton(text="⚡⚙️ Настройки⚡", callback_data="chores_settings")
        )
        
        if templates:
            temp_data = []
            for t in templates:
                last_done_date, nd = await get_template_next_date_val(session, t, today)
                temp_data.append((t, last_done_date, nd))
            
            from datetime import date
            temp_data.sort(key=lambda x: x[2] if x[2] is not None else date(2100, 12, 31))

            for t, last_done_date, nd in temp_data:
                pts_str = "2-8" if t.title == "Готовка" else str(t.points)
                if nd and nd.year < 2099:
                    date_suffix = f" {nd.strftime('%d.%m.')}"
                else:
                    date_suffix = ""
                
                builder.row(
                    InlineKeyboardButton(text=t.title, callback_data=f"tmpl_set:{t.id}:settings"),
                    InlineKeyboardButton(text=f"⚙️{pts_str}🍪{date_suffix}", callback_data=f"tmpl_set:{t.id}:settings")
                )

    text = "🛠 <b>Список задач дома:</b>" if templates else "Задач пока нет."

    if is_callback:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "chores_settings")
async def handle_chores_settings(call: types.CallbackQuery, db_user: User = None):
    await render_chores_settings(call.message, db_user, is_callback=True)


# Obsolete del_tmpl callback query handler removed (handled by te_del)


@dp.callback_query(F.data.startswith("chores_arch:"))
async def handle_chores_archive(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Completion, User, TaskTemplate)
            .join(User, Completion.user_id == User.id)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(TaskTemplate.house_id == ACTIVE_HOUSE_ID)
            .order_by(Completion.created_at.desc())
        )
        all_completions = result.all()
        
        house = await session.get(House, ACTIVE_HOUSE_ID)
        tz_str = house.timezone or "Europe/Moscow"
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_str)
        from datetime import timezone as dt_timezone
        
        from collections import defaultdict
        grouped = defaultdict(list)
        for comp, usr, tmpl in all_completions:
            utc_dt = comp.created_at.replace(tzinfo=dt_timezone.utc)
            local_dt = utc_dt.astimezone(tz)
            local_date = local_dt.date()
            grouped[local_date].append((comp, usr, tmpl, local_dt))
            
        sorted_dates = sorted(grouped.keys(), reverse=True)
        
        if not sorted_dates:
            text = "📜 *История выполненных домашних дел:*\n\nИстория пуста!"
            await call.message.edit_text(text, reply_markup=None, parse_mode="Markdown")
            return
            
        if page < 0:
            page = 0
        if page >= len(sorted_dates):
            page = len(sorted_dates) - 1
            
        target_date = sorted_dates[page]
        completions_for_day = grouped[target_date]
        
        text = f"📅 *{target_date.strftime('%d.%m.%Y')}*\n\n"
        
        builder = InlineKeyboardBuilder()
        nav = []
        if page < len(sorted_dates) - 1:
            nav.append(InlineKeyboardButton(text="⏪", callback_data=f"chores_arch:{page+1}"))
        if page > 0:
            nav.append(InlineKeyboardButton(text="⏩", callback_data=f"chores_arch:{page-1}"))
        if nav:
            builder.row(*nav)
            
        for comp, usr, tmpl, local_dt in completions_for_day:
            u_name = usr.display_name or usr.username or "Кто-то"
            time_str = local_dt.strftime("%H:%M")
            pts_val = "2-8" if tmpl.title == "Готовка" else str(comp.points)
            
            text += f"• {time_str} — {u_name} выполнил *{tmpl.title}* (+{pts_val}🍪)\n"
            
            pts_str = f"{pts_val}🍪 ℹ️"
            builder.row(
                InlineKeyboardButton(text=tmpl.title, callback_data="noop"),
                InlineKeyboardButton(text=pts_str, callback_data=f"tmpl_set:{tmpl.id}:chores_arch_{page}")
            )
            
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# Add template FSM flow
@dp.callback_query(F.data == "add_tmpl_start")
async def handle_add_tmpl_start(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddTemplateState.waiting_for_title)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    builder.row(
        InlineKeyboardButton(text="Добавить из базы", callback_data="add_from_templates_list"),
        InlineKeyboardButton(text="⚡Создать новую⚡", callback_data="noop")
    )
    sent_msg = await call.message.edit_text(
        "Пиши название задачи 📝:",
        reply_markup=builder.as_markup(),
        parse_mode=None
    )
    await state.update_data(last_msg_id=sent_msg.message_id)


@dp.callback_query(F.data == "add_tmpl_cancel")
async def handle_add_tmpl_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    await state.clear()
    await call.answer("Отменено", show_alert=False)
    await render_chores_settings(call.message, db_user, is_callback=True)


@dp.message(StateFilter(AddTemplateState.waiting_for_title))
async def handle_add_tmpl_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    await state.update_data(title=title)
    await state.set_state(AddTemplateState.waiting_for_points)
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    builder.row(
        InlineKeyboardButton(text="Добавить из базы", callback_data="add_from_templates_list"),
        InlineKeyboardButton(text="⚡Создать новую⚡", callback_data="noop")
    )
    
    if last_msg_id:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=last_msg_id,
            text="Сколько печенек 🍪 за нее дадим?",
            reply_markup=builder.as_markup()
        )
    else:
        sent_msg = await message.answer(
            "Сколько печенек 🍪 за нее дадим?",
            reply_markup=builder.as_markup(),
            parse_mode=None
        )
        await state.update_data(last_msg_id=sent_msg.message_id)


@dp.message(StateFilter(AddTemplateState.waiting_for_points))
async def handle_add_tmpl_points(message: types.Message, state: FSMContext):
    try:
        pts = int(message.text.strip())
        if pts <= 0:
            raise ValueError()
    except ValueError:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    await state.update_data(points=pts)
    await state.set_state(AddTemplateState.waiting_for_periodicity)
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
    )
    builder.row(
        InlineKeyboardButton(text="Добавить из базы", callback_data="add_from_templates_list"),
        InlineKeyboardButton(text="⚡Создать новую⚡", callback_data="noop")
    )
    builder.row(
        InlineKeyboardButton(text="Единоразово", callback_data="set_tmpl_period:once"),
        InlineKeyboardButton(text="Каждые X дней", callback_data="set_tmpl_period:every_x_days")
    )
    
    if last_msg_id:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=last_msg_id,
            text="Отлично! Как часто это делаем? 📅",
            reply_markup=builder.as_markup()
        )
    else:
        sent_msg = await message.answer(
            "Отлично! Как часто это делаем? 📅",
            reply_markup=builder.as_markup(),
            parse_mode=None
        )
        await state.update_data(last_msg_id=sent_msg.message_id)


@dp.callback_query(StateFilter(AddTemplateState.waiting_for_periodicity), F.data.startswith("set_tmpl_period:"))
async def handle_add_tmpl_periodicity(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    periodicity = call.data.split(":")[1]
    data = await state.get_data()
    title = data["title"]
    pts = data["points"]
    
    if periodicity == "every_x_days":
        await state.set_state(AddTemplateState.waiting_for_period_days)
        await call.message.edit_text(
            "Укажите число дней, с каким интервалом повторять задачу (например, 5):",
            reply_markup=None
        )
        return
        
    import json
    async with AsyncSessionLocal() as session:
        partner = await get_partner_user(session, db_user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=db_user.id,
                action_type="create_template",
                data_payload=json.dumps({
                    "title": title,
                    "points": pts,
                    "periodicity": periodicity,
                    "period_days": 1 if periodicity == "daily" else None
                })
            )
            session.add(pending)
            await session.commit()
            
            p_desc = "каждый день" if periodicity == "daily" else "1 раз"
            partner_text = (
                f"🔔 *Согласование новой задачи!*\n\n"
                f"Жилец *{db_user.display_name or db_user.username or 'Партнёр'}* хочет добавить домашнюю задачу:\n"
                f"📋 *Название:* {title}\n"
                f"🍪 *Награда:* {pts} печенек\n"
                f"📅 *Цикл:* {p_desc}\n\n"
                f"Одобряете добавление?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            await state.clear()
            await call.answer("⏳ Отправлено на согласование партнёру", show_alert=False)
            builder_nav = InlineKeyboardBuilder()
            builder_nav.row(
                InlineKeyboardButton(text="Home", callback_data="home_view"),
                InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
                InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
            )
            await call.message.edit_text(
                f"⏳ Задача «{title}» отправлена на согласование партнёру.",
                reply_markup=builder_nav.as_markup()
            )
        else:
            tmpl = TaskTemplate(
                house_id=ACTIVE_HOUSE_ID,
                title=title,
                points=pts,
                periodicity=periodicity,
                period_days=1 if periodicity == "daily" else None,
                deleted=False
            )
            session.add(tmpl)
            await session.flush()
            
            inst = TaskInstance(
                template_id=tmpl.id,
                date=await get_house_today_date(session),
                status="free",
                priority=0
            )
            session.add(inst)
            await session.commit()
            
            await state.clear()
            await call.answer("✅ Шаблон успешно добавлен!", show_alert=False)
            await render_chores_settings(call.message, db_user, is_callback=True)


@dp.message(StateFilter(AddTemplateState.waiting_for_period_days))
async def handle_add_tmpl_period_days(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError()
    except ValueError:
        try:
            await message.delete()
        except Exception:
            pass
        return
        
    data = await state.get_data()
    title = data["title"]
    points = data["points"]
    last_msg_id = data.get("last_msg_id")
    
    async with AsyncSessionLocal() as session:
        tmpl = TaskTemplate(
            house_id=ACTIVE_HOUSE_ID,
            title=title,
            points=points,
            periodicity="every_x_days",
            period_days=days,
            start_date=await get_house_today_date(session),
            is_active=True,
            deleted=False
        )
        session.add(tmpl)
        await session.flush()
        
        inst = TaskInstance(
            template_id=tmpl.id,
            date=await get_house_today_date(session),
            status="free",
            priority=0
        )
        session.add(inst)
        await session.commit()
        
    try:
        await message.delete()
    except Exception:
        pass
        
    await state.clear()
    if last_msg_id:
        message.message_id = last_msg_id
        await render_chores_settings(message, db_user, is_callback=True)
    else:
        await render_chores_settings(message, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("done_chore_inst:"))
async def handle_done_chore_inst(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst and inst.done_by_user_id == db_user.id:
            tmpl = await session.get(TaskTemplate, inst.template_id)
            
            # --- Cooking Time Check ---
            if tmpl and tmpl.title == "Готовка":
                builder = InlineKeyboardBuilder()
                builder.row(InlineKeyboardButton(text="⏳ До 30 мин (3 🍪)", callback_data=f"cook_time:{inst.id}:3:{page}"))
                builder.row(InlineKeyboardButton(text="⏳ 30 - 60 мин (5 🍪)", callback_data=f"cook_time:{inst.id}:5:{page}"))
                builder.row(InlineKeyboardButton(text="⏳ 60 - 90 мин (8 🍪)", callback_data=f"cook_time:{inst.id}:8:{page}"))
                builder.row(InlineKeyboardButton(text="⏳ Более 90 мин (10 🍪)", callback_data=f"cook_time:{inst.id}:10:{page}"))

                
                await call.message.edit_text(
                    "🍳 *Готовка*\nСколько активного времени вы потратили на нее?",
                    reply_markup=builder.as_markup(),
                    parse_mode="Markdown"
                )
                await call.answer()
                return
            
            inst.status = "done"
            inst.done_at = datetime.utcnow()
            
            # Award points to user
            user = await session.get(User, db_user.id)
            pts = tmpl.points if tmpl else 1
            user.points = (user.points or 0) + pts
            
            # Record completion
            comp = Completion(
                user_id=db_user.id,
                task_instance_id=inst.id,
                points=pts
            )
            session.add(comp)
            await session.commit()
            await call.answer(f"✅ Выполнено! Начислено +{pts} 🍪", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!", show_alert=False)
    await render_today(call.message, db_user, is_callback=True, page=page)


@dp.callback_query(F.data.startswith("cook_time:"))
async def handle_cook_time(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    pts = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst and inst.done_by_user_id == db_user.id:
            inst.status = "done"
            inst.done_at = datetime.utcnow()
            
            # Award points
            user = await session.get(User, db_user.id)
            user.points = (user.points or 0) + pts
            
            # Record completion
            comp = Completion(
                user_id=db_user.id,
                task_instance_id=inst.id,
                points=pts
            )
            session.add(comp)
            await session.commit()
            await call.answer(f"✅ Выполнено! Начислено +{pts} 🍪", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!", show_alert=False)
    await render_today(call.message, db_user, is_callback=True, page=page)


@dp.callback_query(F.data.startswith("unclaim_chore_inst:"))
async def handle_unclaim_chore_inst(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst and inst.done_by_user_id == db_user.id:
            inst.status = "free"
            inst.done_by_user_id = None
            await session.commit()
            await call.answer("↪ Задача возвращена в общий пул свободных дел.", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!", show_alert=False)
    await render_today(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("te_shift_start:"))
async def handle_te_shift_start(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tmpl_id = int(parts[1])
    src = parts[2]
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    markup = create_calendar_keyboard_custom(tmpl_id, today.year, today.month, today, "tmpl_start")
    header = format_calendar_header(today) + "\n*(выберите дату начала/отсчета для задачи)*"
    keyboard = markup.inline_keyboard
    await call.message.edit_text(header, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_tmpl_start:"))
async def handle_cal_nav_tmpl_start(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tmpl_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
    markup = create_calendar_keyboard_custom(tmpl_id, year, month, today, "tmpl_start")
    header = format_calendar_header(today) + "\n*(выберите дату начала/отсчета для задачи)*"
    keyboard = markup.inline_keyboard
    await call.message.edit_text(header, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_tmpl_start:"))
async def handle_shift_tmpl_start(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tmpl_id = int(parts[1])
    date_str = parts[2]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    from sqlalchemy import update
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tmpl_id)
        if tmpl:
            tmpl.start_date = new_date
            # Move any active instances of this task template to the new date
            await session.execute(
                update(TaskInstance)
                .where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.status.in_(["free", "in_progress", "shifted"])
                    )
                )
                .values(date=new_date)
            )
            await session.commit()
            await call.answer(f"✅ Дата отсчета перенесена на {new_date.strftime('%d.%m')}!", show_alert=False)
            
    await redirect_to_template_settings(call.message, tmpl_id, "settings", db_user, is_callback=True)



# Obsolete Move/Delete handlers removed (fully replaced by my_shift_select / my_delete_select workflows)

