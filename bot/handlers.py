import os
import logging
import asyncio
import calendar
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, Date

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import AsyncSessionLocal, User, House, PersonalTask, ShoppingItem, TaskTemplate, TaskInstance, Completion, Reward, RewardPurchase
from bot.parser import parse_input, get_recurrence_delta, clean_task_text

logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://example.com")

# Hardcoded house and users for auto-join
ACTIVE_HOUSE_ID = 81
ALLOWED_TELEGRAM_IDS = {
    680630275: ("Шурик", True),   # (display_name, is_owner)
    399998303: ("Биба", False),
}

bot = Bot(token=TOKEN)
dp = Dispatcher()


async def generate_daily_chores_if_needed(session, house_id: int):
    """Generate today's task instances from templates and rollover uncompleted ones."""
    from sqlalchemy import update
    today = datetime.now().date()
    weekday = today.weekday()
    day = today.day
    month = today.month

    house = await session.get(House, house_id)
    if not house:
        return

    if house.last_summary_date == today:
        return

    result = await session.execute(
        select(TaskTemplate).where(
            and_(
                TaskTemplate.house_id == house_id,
                TaskTemplate.deleted == False
            )
        )
    )
    templates = result.scalars().all()

    for tmpl in templates:
        if tmpl.start_date and today < tmpl.start_date:
            continue

        p = tmpl.periodicity
        should_create = False
        if p == "daily":
            should_create = True
        elif p == "weekly":
            should_create = (weekday == (tmpl.weekday if tmpl.weekday is not None else 0))
        elif p == "twice_weekly":
            should_create = (weekday in (0, 3))
        elif p == "monthly":
            should_create = (day == (tmpl.month_day if tmpl.month_day is not None else 1))
        elif p == "twice_monthly":
            should_create = (day in (5, 20))
        elif p == "quarterly":
            if day == (tmpl.month_day if tmpl.month_day is not None else 1):
                pos = ((month - 1) % 3) + 1
                if pos == (tmpl.weekday if tmpl.weekday is not None else 1):
                    should_create = True

        if should_create:
            exists = await session.scalar(
                select(TaskInstance).where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.date == today
                    )
                )
            )
            if not exists:
                inst = TaskInstance(
                    template_id=tmpl.id,
                    date=today,
                    status="free",
                    priority=0
                )
                session.add(inst)

    # Rollover old uncompleted tasks
    await session.execute(
        update(TaskInstance)
        .where(
            and_(
                TaskInstance.date < today,
                TaskInstance.status != "done"
            )
        )
        .values(date=today)
    )

    house.last_summary_date = today
    await session.commit()
    logger.info(f"Generated daily chores for house {house_id}")


# ── FSM States ────────────────────────────────────────────────────────────────
class EditShop(StatesGroup):
    waiting_for_input = State()
    item_id = State()


class AddTemplateState(StatesGroup):
    waiting_for_title = State()
    waiting_for_points = State()
    waiting_for_periodicity = State()
    waiting_for_period_days = State()


class AddRewardState(StatesGroup):
    waiting_for_title = State()
    waiting_for_price = State()


class AddPersonalTaskState(StatesGroup):
    waiting_for_text = State()


class EditTemplateState(StatesGroup):
    waiting_for_title = State()
    waiting_for_points = State()
    waiting_for_periodicity = State()
    waiting_for_period_days = State()




# ── Middleware: auto-register users ──────────────────────────────────────────
class AutoRegisterMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from_user = None
        if isinstance(event, types.Message):
            from_user = event.from_user
        elif isinstance(event, types.CallbackQuery):
            from_user = event.from_user

        if from_user:
            tg_id = from_user.id
            async with AsyncSessionLocal() as session:
                user = await session.scalar(select(User).where(User.telegram_id == tg_id))
                if not user:
                    user_info = ALLOWED_TELEGRAM_IDS.get(tg_id)
                    display_name = user_info[0] if user_info else (from_user.first_name or str(tg_id))
                    is_owner = user_info[1] if user_info else False
                    user = User(
                        telegram_id=tg_id,
                        username=from_user.username,
                        full_name=from_user.full_name,
                        display_name=display_name,
                        house_id=ACTIVE_HOUSE_ID,
                        is_house_owner=is_owner,
                        points=0,
                    )
                    session.add(user)
                    await session.commit()
                    logger.info(f"Auto-registered user {tg_id} as {display_name}")
                data["db_user"] = user

        return await handler(event, data)


dp.message.middleware(AutoRegisterMiddleware())
dp.callback_query.middleware(AutoRegisterMiddleware())


# ── Keyboards ─────────────────────────────────────────────────────────────────
def get_main_keyboard() -> types.ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🏠 Home"),
        KeyboardButton(text="📋 My"),
        KeyboardButton(text="📊 Stat"),
    )
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


# ── /start ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, db_user: User = None):
    name = db_user.display_name if db_user else message.from_user.first_name
    text = (
        f"👋 Привет, *{name}*!\n\n"
        "Это твой личный помощник по домашним и личным делам.\n\n"
        "🏠 *Home* — свободные обязанности по дому\n"
        "📋 *My* — твои личные задачи + взятые домашние дела\n"
        "📊 *Stat* — покупки, награды, лидерборд и статистика печенек\n\n"
        "Просто напиши мне, что нужно сделать, и я всё запомню!\n"
        "_Например: «купить молоко 150» или «позвонить врачу завтра»_"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


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
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        await generate_daily_chores_if_needed(session, ACTIVE_HOUSE_ID)
        result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskInstance.date == today,
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

    if chores:
        for inst, tmpl in chores:
            # We hardcode Points to display 2-8🍪 for 'Готовка' to match the old bot
            pts_str = "2-8🍪 ℹ️" if tmpl.title == "Готовка" else f"{tmpl.points}🍪 ℹ️"
            builder.row(
                InlineKeyboardButton(text=tmpl.title, callback_data=f"claim_chore:{inst.id}"),
                InlineKeyboardButton(text=pts_str, callback_data=f"tmpl_set:{tmpl.id}:today")
            )

    builder.row(
        InlineKeyboardButton(text="Добавить", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="Настройки", callback_data="chores_settings"),
    )

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text.in_({"🏠 Home", "🏠 Домашние дела"}))
async def handle_household_chores_btn(message: types.Message, db_user: User = None):
    await render_household_chores(message, db_user)


@dp.callback_query(F.data == "chores_back")
async def handle_chores_back(call: types.CallbackQuery, db_user: User = None):
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data == "chores_add_menu")
async def handle_chores_add_menu(call: types.CallbackQuery, db_user: User = None):
    builder = InlineKeyboardBuilder()
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
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.deleted == False
                )
            )
        )
        templates = result.scalars().all()

        # Compute next date for each and sort ascending (nearest first)
        tmpl_with_dates = []
        for t in templates:
            last_done_date, nd = await get_template_next_date_val(session, t, today)
            tmpl_with_dates.append((t, last_done_date, nd))
        tmpl_with_dates.sort(key=lambda x: x[2])

        builder = InlineKeyboardBuilder()
        if tmpl_with_dates:
            text = "📋 *Выберите задачу для добавления на сегодня:*"
            for t, last_done_date, nd in tmpl_with_dates:
                pts_str = "2-8" if t.title == "Готовка" else str(t.points)
                if t.periodicity == "once":
                    col2_text = f"{pts_str}🍪 Единоразовая"
                else:
                    nd_str = nd.strftime("%d.%m")
                    period_lbl = get_period_label(t)
                    col2_text = f"{pts_str}🍪 {period_lbl} → {nd_str}"
                builder.row(
                    InlineKeyboardButton(text=t.title, callback_data=f"spawn_chore:{t.id}"),
                    InlineKeyboardButton(text=col2_text, callback_data="noop")
                )
        else:
            text = "⚠️ Шаблонов дел пока нет!"

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="chores_add_menu"))
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("spawn_chore:"))
async def handle_spawn_chore(call: types.CallbackQuery, db_user: User = None):
    tmpl_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
        
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"resched_menu:{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def create_calendar_keyboard_custom(target_id: int, year: int, month: int, today_date: date, callback_prefix: str) -> InlineKeyboardMarkup:
    kb = []
    month_names_ru = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    kb.append([InlineKeyboardButton(text=f"🗓 {month_names_ru[month-1]} {year}", callback_data="noop")])
    
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    
    kb.append([
        InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"cal_nav_{callback_prefix}:{target_id}:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text="След. ➡️", callback_data=f"cal_nav_{callback_prefix}:{target_id}:{next_y}:{next_m}")
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
                row.append(InlineKeyboardButton(text=btn_text, callback_data=f"shift_{callback_prefix}:{target_id}:{year}-{month:02d}-{day:02d}"))
        kb.append(row)
        
    back_cb = f"shift_{callback_prefix}_menu:{target_id}"
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def format_calendar_header(today_date: date) -> str:
    days_full_ru = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
    weekday_idx = (today_date.weekday() + 1) % 7
    return f"📌 *Выберите дату переноса*\n_({days_full_ru[weekday_idx]}, {today_date.strftime('%d.%m.%Y')})_\n\n"


def find_scheduled_date_on_or_after(t: TaskTemplate, search_date: date) -> date:
    curr = search_date
    p = t.periodicity
    if p == "weekly":
        target_wd = t.weekday if t.weekday is not None else 0
        for _ in range(365):
            if curr.weekday() == target_wd:
                return curr
            curr += timedelta(days=1)
    elif p == "twice_weekly":
        for _ in range(365):
            if curr.weekday() in (0, 3):
                return curr
            curr += timedelta(days=1)
    elif p == "monthly":
        target_md = t.month_day if t.month_day is not None else 1
        for _ in range(365):
            if curr.day == target_md:
                return curr
            curr += timedelta(days=1)
    elif p == "twice_monthly":
        for _ in range(365):
            if curr.day in (5, 20):
                return curr
            curr += timedelta(days=1)
    elif p == "quarterly":
        target_qm = t.weekday if t.weekday is not None else 1
        target_md = t.month_day if t.month_day is not None else 1
        for _ in range(365 * 2):
            if curr.day == target_md:
                pos = ((curr.month - 1) % 3) + 1
                if pos == target_qm:
                    return curr
            curr += timedelta(days=1)
    return curr


def get_template_next_date(t: TaskTemplate, last_done_date: date, active_inst_date: date, today_date: date) -> date:
    if active_inst_date:
        return max(active_inst_date, today_date)
        
    ref_start = t.start_date or today_date
    
    if t.periodicity == "once":
        if last_done_date:
            return date(2099, 12, 31)
        else:
            return max(ref_start, today_date)
            
    if t.periodicity == "daily":
        if last_done_date:
            if last_done_date >= today_date:
                return today_date + timedelta(days=1)
            else:
                return today_date
        else:
            return max(ref_start, today_date)
            
    if t.periodicity == "every_x_days":
        p_days = t.period_days or 1
        if last_done_date:
            next_date = last_done_date + timedelta(days=p_days)
            return max(next_date, today_date)
        else:
            return max(ref_start, today_date)
            
    # For weekly, twice_weekly, monthly, twice_monthly, quarterly:
    search_start = last_done_date if last_done_date else ref_start
    search_start = max(search_start, ref_start)
    
    S = find_scheduled_date_on_or_after(t, search_start)
    
    if last_done_date:
        threshold = 4
        if t.periodicity == "twice_weekly":
            threshold = 2
        elif t.periodicity == "monthly":
            threshold = 15
        elif t.periodicity == "twice_monthly":
            threshold = 6
        elif t.periodicity == "quarterly":
            threshold = 45
            
        if S >= last_done_date and (S - last_done_date).days < threshold:
            S = find_scheduled_date_on_or_after(t, S + timedelta(days=1))
            
    return max(S, today_date)


async def get_template_next_date_val(session: AsyncSession, t: TaskTemplate, today_date: date):
    # 1. last done date
    last_comp = await session.execute(
        select(Completion.created_at)
        .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
        .where(TaskInstance.template_id == t.id)
        .order_by(Completion.created_at.desc())
        .limit(1)
    )
    last_done_dt = last_comp.scalar()
    last_done_date = last_done_dt.date() if last_done_dt else None

    # 2. active inst date
    active_inst_date = await session.scalar(
        select(TaskInstance.date).where(
            and_(
                TaskInstance.template_id == t.id,
                TaskInstance.status.in_(["free", "shifted", "in_progress"])
            )
        ).order_by(TaskInstance.date.desc()).limit(1)
    )

    nd = get_template_next_date(t, last_done_date, active_inst_date, today_date)
    return last_done_date, nd


def get_period_label(tmpl: TaskTemplate) -> str:
    p = tmpl.periodicity
    if p in ("every_x_days", "everyxdays"):
        days = tmpl.period_days or 1
        if days % 10 == 1 and days % 100 != 11:
            return f"каждый {days} день"
        elif days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
            return f"каждые {days} дня"
        else:
            return f"каждые {days} дней"
    
    mapping = {
        "daily": "каждый день",
        "weekly": "раз в неделю",
        "twice_weekly": "2 раза в неделю",
        "monthly": "раз в месяц",
        "twice_monthly": "2 раза в месяц",
        "quarterly": "раз в квартал",
        "once": "один раз"
    }
    return mapping.get(p, p)


async def redirect_to_template_settings(message: types.Message, tid: int, src: str, db_user: User, is_callback=True):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
        f"📅 Последнее выполнение: {last_done_str}\n"
        f"🔮 Следующее выполнение: {next_done_str}"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Имя", callback_data=f"te_f:title:{tmpl.id}:{src}"),
        InlineKeyboardButton(text="Цикл", callback_data=f"te_f:period:{tmpl.id}:{src}"),
        InlineKeyboardButton(text="🍪 Печеньки", callback_data=f"te_f:points:{tmpl.id}:{src}"),
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
    today = datetime.now().date()
    
    async with AsyncSessionLocal() as session:
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
                            TaskInstance.date == today,
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
            builder.row(
                InlineKeyboardButton(text="🔔 Намек", callback_data=f"nudge:{inst_id}"),
                InlineKeyboardButton(text="📅 Сдвиг", callback_data=f"resched_menu:{inst_id}"),
                InlineKeyboardButton(text="🗑 Копию", callback_data=f"del_inst:{inst_id}")
            )
            builder.row(
                InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"tmpl_set:{tmpl.id}:today_list"),
                InlineKeyboardButton(text="🔙 Назад", callback_data="chores_back")
            )
        else:
            page_val = src.replace("chores_arch_", "")
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="🔙 Назад", callback_data=f"chores_arch:{page_val}")
            )

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
    tid = int(parts[3])
    src = parts[4]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_title)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"te_cancel:{tid}:{src}"))
    await call.message.edit_text("Введите новое название для шаблона:", reply_markup=builder.as_markup())


@dp.message(StateFilter(EditTemplateState.waiting_for_title))
async def handle_te_title_input(message: types.Message, state: FSMContext, db_user: User = None):
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if tmpl:
            tmpl.title = title
            await session.commit()
            await message.answer(f"✅ Название успешно обновлено на: *{title}*", parse_mode="Markdown")
            
    await state.clear()
    await redirect_to_template_settings(message, tid, src, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("te_f:points:"))
async def handle_te_points_start(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    tid = int(parts[3])
    src = parts[4]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_points)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"te_cancel:{tid}:{src}"))
    await call.message.edit_text("Введите новое количество баллов (печенек):", reply_markup=builder.as_markup())


@dp.message(StateFilter(EditTemplateState.waiting_for_points))
async def handle_te_points_input(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        pts = int(message.text.strip())
        if pts < 0:
            raise ValueError()
    except ValueError:
        await message.answer("Пожалуйста, введите неотрицательное целое число:")
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if tmpl:
            tmpl.points = pts
            await session.commit()
            await message.answer(f"✅ Баллы успешно обновлены на: *{pts}*", parse_mode="Markdown")
            
    await state.clear()
    await redirect_to_template_settings(message, tid, src, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("te_f:period:"))
async def handle_te_period_start(call: types.CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    tid = int(parts[3])
    src = parts[4]
    await state.update_data(edit_tid=tid, edit_src=src)
    await state.set_state(EditTemplateState.waiting_for_periodicity)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="1 Раз", callback_data="te_period_sel:once"),
        InlineKeyboardButton(text="Каждый день", callback_data="te_period_sel:daily"),
        InlineKeyboardButton(text="Каждые X дней", callback_data="te_period_sel:every_x_days")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"te_cancel:{tid}:{src}")
    )
    await call.message.edit_text("Выберите периодичность для шаблона:", reply_markup=builder.as_markup())


@dp.callback_query(StateFilter(EditTemplateState.waiting_for_periodicity), F.data.startswith("te_period_sel:"))
async def handle_te_period_selected(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    p = call.data.split(":")[1]
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    
    if p in ["once", "daily"]:
        async with AsyncSessionLocal() as session:
            tmpl = await session.get(TaskTemplate, tid)
            if tmpl:
                tmpl.periodicity = p
                tmpl.period_days = 0 if p == "once" else 1
                await session.commit()
                await call.answer("✅ Периодичность обновлена!", show_alert=False)
        await state.clear()
        await redirect_to_template_settings(call.message, tid, src, db_user, is_callback=True)
    else:
        await state.set_state(EditTemplateState.waiting_for_period_days)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"te_cancel:{tid}:{src}"))
        await call.message.edit_text("Укажите число дней, с каким интервалом повторять задачу (например, 5):", reply_markup=builder.as_markup())


@dp.message(StateFilter(EditTemplateState.waiting_for_period_days))
async def handle_te_period_days_input(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Пожалуйста, введите целое положительное число:")
        return
        
    data = await state.get_data()
    tid = data["edit_tid"]
    src = data["edit_src"]
    
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tid)
        if tmpl:
            tmpl.periodicity = "every_x_days"
            tmpl.period_days = days
            await session.commit()
            await message.answer(f"✅ Периодичность обновлена: каждые {days} дней!")
            
    await state.clear()
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
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"te_del:{tid}:{src}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"tmpl_set:{tid}:{src}")
    )
    await call.message.edit_text(f"Точно удалить «{name}»?", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("nudge:"))
async def handle_nudge(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    
    if nudge_cache.get(inst_id) == today:
        await call.answer("Тише-тише, намек уже отправлен. Ждем реакции! 🤫", show_alert=False)
        return
        
    async with AsyncSessionLocal() as session:
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
    today = datetime.now().date()
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        tmpl_id = inst.template_id if inst else 0
        
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift:once:{inst_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift:once:{inst_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months:{inst_id}")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data=f"tmpl_set:{tmpl_id}:today")
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
    today = datetime.now().date()
    markup = create_calendar_keyboard(inst_id, today.year, today.month, today)
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav:"))
async def handle_cal_nav(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    today = datetime.now().date()
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

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="chores_back"))
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


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
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
        if templates:
            for t in templates:
                last_done_date, nd = await get_template_next_date_val(session, t, today)
                pts_str = "2-8" if t.title == "Готовка" else str(t.points)
                period_lbl = get_period_label(t)
                last_str = last_done_date.strftime("%d.%m") if last_done_date else "никогда"
                next_str = nd.strftime("%d.%m") if nd.year < 2099 else "нет"
                gear_text = f"⚙️ {pts_str}🍪 {period_lbl} /{next_str}"
                builder.row(
                    InlineKeyboardButton(text=t.title, callback_data="noop"),
                    InlineKeyboardButton(text=gear_text, callback_data=f"tmpl_set:{t.id}:settings")
                )

    text = "🛠 <b>Список задач дома:</b>" if templates else "Задач пока нет."
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="add_tmpl_start"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="settings_del_menu"),
    )

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
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="chores_back"))
            await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            return
            
        if page < 0:
            page = 0
        if page >= len(sorted_dates):
            page = len(sorted_dates) - 1
            
        target_date = sorted_dates[page]
        completions_for_day = grouped[target_date]
        
        text = f"📅 *{target_date.strftime('%d.%m.%Y')}*\n\n"
        
        builder = InlineKeyboardBuilder()
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
            
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"chores_arch:{page-1}"))
        if page < len(sorted_dates) - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"chores_arch:{page+1}"))
        if nav:
            builder.row(*nav)
        
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="chores_back"))
            
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# Add template FSM flow
@dp.callback_query(F.data == "add_tmpl_start")
async def handle_add_tmpl_start(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddTemplateState.waiting_for_title)
    await call.message.edit_text(
        "Пиши название задачи 📝:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel")
        ]]),
        parse_mode=None
    )


@dp.callback_query(F.data == "add_tmpl_cancel")
async def handle_add_tmpl_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    await state.clear()
    await call.answer("Отменено", show_alert=False)
    await render_chores_settings(call.message, db_user, is_callback=True)


@dp.message(StateFilter(AddTemplateState.waiting_for_title))
async def handle_add_tmpl_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
    await state.update_data(title=title)
    await state.set_state(AddTemplateState.waiting_for_points)
    await message.answer(
        "Сколько печенек 🍪 за нее дадим?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel")
        ]]),
        parse_mode=None
    )


@dp.message(StateFilter(AddTemplateState.waiting_for_points))
async def handle_add_tmpl_points(message: types.Message, state: FSMContext):
    try:
        pts = int(message.text.strip())
        if pts <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Нужно число! Попробуйте еще раз:")
        return
    
    await state.update_data(points=pts)
    await state.set_state(AddTemplateState.waiting_for_periodicity)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Единоразово", callback_data="set_tmpl_period:once"),
        InlineKeyboardButton(text="Каждые X дней", callback_data="set_tmpl_period:every_x_days")
    )
    
    await message.answer(
        "Отлично! Как часто это делаем? 📅",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(StateFilter(AddTemplateState.waiting_for_periodicity), F.data.startswith("set_tmpl_period:"))
async def handle_add_tmpl_periodicity(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    periodicity = call.data.split(":")[1]
    data = await state.get_data()
    title = data["title"]
    pts = data["points"]
    
    if periodicity == "every_x_days":
        await state.set_state(AddTemplateState.waiting_for_period_days)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel"))
        await call.message.edit_text(
            "Укажите число дней, с каким интервалом повторять задачу (например, 5):",
            reply_markup=builder.as_markup()
        )
        return
        
    async with AsyncSessionLocal() as session:
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
        
        # Spawn instance for today as well
        inst = TaskInstance(
            template_id=tmpl.id,
            date=datetime.now().date(),
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
        p_days = int(message.text.strip())
        if p_days <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число дней!")
        return
        
    data = await state.get_data()
    title = data["title"]
    pts = data["points"]
    
    async with AsyncSessionLocal() as session:
        tmpl = TaskTemplate(
            house_id=ACTIVE_HOUSE_ID,
            title=title,
            points=pts,
            periodicity="every_x_days",
            period_days=p_days,
            deleted=False
        )
        session.add(tmpl)
        await session.flush()
        
        # Spawn instance for today as well
        inst = TaskInstance(
            template_id=tmpl.id,
            date=datetime.now().date(),
            status="free",
            priority=0
        )
        session.add(inst)
        await session.commit()
        
    await state.clear()
    await message.answer(f"✅ Шаблон успешно добавлен: *{title}*!")
    await render_chores_settings(message, db_user, is_callback=False)


# ── Render Today ───────────────────────────────────────────────────────────────
async def rollover_overdue_tasks(session: AsyncSession, user_id: int):
    """Move overdue uncompleted tasks to today (disabled date shifting to preserve original date)."""
    pass



async def render_today(message: types.Message, db_user: User, is_callback=False):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        await rollover_overdue_tasks(session, db_user.id)

        # 1. Fetch personal tasks (execution date <= today)
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution <= today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.id)
        )
        personal_tasks = result.scalars().all()

        # 2. Fetch user's claimed chores (chores date <= today)
        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress",
                    TaskInstance.date <= today
                )
            )
            .order_by(TaskInstance.id)
        )
        my_chores = chores_result.all()

    text = "👤 *Твои дела на сегодня:*\n\n"
    keyboard = []

    # Personal tasks rendering
    text += "*👤 Личные задачи:*\n"
    if personal_tasks:
        for t in personal_tasks:
            clean = clean_task_text(t.text)
            is_urgent = "🔴" in t.text
            prefix = "🔴 " if is_urgent else "🟡 " if t.date_execution < today else ""
            display_text = f"{prefix}{clean}"
            rec_icon = " 🔁" if t.recurrence else ""
            text += f"• {display_text}{rec_icon}\n"
            keyboard.append([InlineKeyboardButton(text=display_text, callback_data=f"done_task:{t.id}")])
    else:
        text += "_Нет личных задач_\n"
    text += "\n"

    # Household chores rendering
    text += "*🏠 В работе из домашних:*\n"
    if my_chores:
        for inst, tmpl in my_chores:
            pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
            prefix = "🟡 " if inst.date < today else ""
            text += f"• {prefix}{tmpl.title} (`+{pts_str} 🍪`)\n"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🏠 {prefix}{tmpl.title} (+{pts_str}🍪)",
                    callback_data=f"done_chore_inst:{inst.id}"
                )
            ])
    else:
        text += "_Нет взятых домашних дел_\n"

    # Add bottom toolbar as a single horizontal row
    keyboard.append([
        InlineKeyboardButton(text="Добавить", callback_data="my_add"),
        InlineKeyboardButton(text="Сдвиг", callback_data="my_shift_select"),
        InlineKeyboardButton(text="Удалить", callback_data="my_delete_select"),
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text.in_({"📋 My", "👤 My", "👤 Мои дела"}))
async def today_handler(m: types.Message, db_user: User = None):
    await render_today(m, db_user)


@dp.callback_query(F.data == "t_cancel")
async def t_cancel(call: types.CallbackQuery, db_user: User = None):
    await render_today(call.message, db_user, True)


# Personal task addition FSM callbacks
@dp.callback_query(F.data == "my_add")
async def handle_my_add(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddPersonalTaskState.waiting_for_text)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="my_add_cancel"))
    await call.message.edit_text(
        "Пиши название личной задачи 📝:",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "my_add_cancel")
async def handle_my_add_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    await state.clear()
    await call.answer("Отменено", show_alert=False)
    await render_today(call.message, db_user, is_callback=True)


@dp.message(StateFilter(AddPersonalTaskState.waiting_for_text))
async def handle_my_add_text(message: types.Message, state: FSMContext, db_user: User = None):
    text = message.text.strip()
    if not text:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
        
    import re
    is_urgent = "срочно" in text.lower()
    clean_text = re.sub(r'срочно', '', text, flags=re.IGNORECASE).strip().capitalize()
    
    if is_urgent:
        db_text = f"🔴 {clean_text}"
    else:
        db_text = clean_text
        
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        task = PersonalTask(
            user_id=db_user.id,
            text=db_text,
            date_execution=today,
            category="inbox",
            is_completed=False,
            is_deleted=False
        )
        session.add(task)
        await session.commit()
        
    await state.clear()
    await message.answer(f"✅ Добавил личную задачу: *{clean_text}*", parse_mode="Markdown")
    await render_today(message, db_user, is_callback=False)


# Reschedule select menu
@dp.callback_query(F.data == "my_shift_select")
async def handle_my_shift_select(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution <= today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.id)
        )
        personal_tasks = result.scalars().all()

        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress",
                    TaskInstance.date <= today
                )
            )
            .order_by(TaskInstance.id)
        )
        my_chores = chores_result.all()

    if not personal_tasks and not my_chores:
        await call.answer("Нет активных задач для переноса!", show_alert=False)
        return

    builder = InlineKeyboardBuilder()
    for t in personal_tasks:
        clean = clean_task_text(t.text)
        is_urgent = "🔴" in t.text or "срочно" in t.text.lower()
        prefix = "🔴 " if is_urgent else "🟡 " if t.date_execution < today else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}{clean}", callback_data=f"shift_pt_menu:{t.id}"))
        
    for inst, tmpl in my_chores:
        prefix = "🟡 " if inst.date < today else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}🏠 {tmpl.title}", callback_data=f"shift_chore_menu:{inst.id}"))

    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="t_cancel"))
    await call.message.edit_text("🔄 Выберите задачу для переноса:", reply_markup=builder.as_markup())


# Reschedule choice menu (tomorrow/day after/calendar)
@dp.callback_query(F.data.startswith("shift_pt_menu:"))
async def handle_shift_pt_menu(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift_pt:{t_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift_pt:{t_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_pt:{t_id}")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="my_shift_select")
        ]
    ]
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("shift_chore_menu:"))
async def handle_shift_chore_menu(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"shift_chore:{inst_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"shift_chore:{inst_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_chore:{inst_id}")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="my_shift_select")
        ]
    ]
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# Calendar navigation & shift execution callbacks
@dp.callback_query(F.data.startswith("rc_months_pt:"))
async def handle_rc_months_pt(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(t_id, today.year, today.month, today, "pt")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_pt:"))
async def handle_cal_nav_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(t_id, year, month, today, "pt")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_pt:"))
async def handle_shift_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
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
            
    await render_today(call.message, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("rc_months_chore:"))
async def handle_rc_months_chore(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(inst_id, today.year, today.month, today, "chore")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_chore:"))
async def handle_cal_nav_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(inst_id, year, month, today, "chore")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_chore:"))
async def handle_shift_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
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
            
    await render_today(call.message, db_user, is_callback=False)


# Delete select menu & callbacks
@dp.callback_query(F.data == "my_delete_select")
async def handle_my_delete_select(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        personal_tasks = (await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution <= today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False
                )
            ).order_by(PersonalTask.id)
        )).scalars().all()
        
        my_chores = (await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress",
                    TaskInstance.date <= today
                )
            )
            .order_by(TaskInstance.id)
        )).all()

    if not personal_tasks and not my_chores:
        await call.answer("Нет активных задач для удаления!", show_alert=False)
        return

    builder = InlineKeyboardBuilder()
    for t in personal_tasks:
        clean = clean_task_text(t.text)
        is_urgent = "🔴" in t.text or "срочно" in t.text.lower()
        prefix = "🔴 " if is_urgent else "🟡 " if t.date_execution < today else ""
        builder.row(InlineKeyboardButton(text=f"🗑 {prefix}{clean}", callback_data=f"del_pt:{t.id}"))
        
    for inst, tmpl in my_chores:
        prefix = "🟡 " if inst.date < today else ""
        builder.row(InlineKeyboardButton(text=f"🗑 {prefix}🏠 {tmpl.title}", callback_data=f"del_chore_inst:{inst.id}"))

    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="t_cancel"))
    await call.message.edit_text("🗑 Выберите задачу для удаления:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("del_pt:"))
async def handle_del_pt(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
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
            
    await render_today(call.message, db_user, is_callback=False)


@dp.callback_query(F.data.startswith("del_chore_inst:"))
async def handle_del_chore_inst(call: types.CallbackQuery, db_user: User = None):
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
            
    await render_today(call.message, db_user, is_callback=False)


# ── Done task ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("done_task:"))
async def handle_done_task(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            task.is_completed = True
            if task.recurrence:
                delta = get_recurrence_delta(task.recurrence)
                clean = clean_task_text(task.text)
                new_task = PersonalTask(
                    user_id=task.user_id,
                    text=clean,
                    date_execution=datetime.now().date() + delta,
                    category="inbox",
                    recurrence=task.recurrence,
                    is_completed=False,
                    is_deleted=False,
                )
                session.add(new_task)
            await session.commit()
    await render_today(call.message, db_user, True)


@dp.callback_query(F.data.startswith("done_chore_inst:"))
async def handle_done_chore_inst(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst and inst.done_by_user_id == db_user.id:
            tmpl = await session.get(TaskTemplate, inst.template_id)
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
    await render_today(call.message, db_user, is_callback=True)


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


# Obsolete Move/Delete handlers removed (fully replaced by my_shift_select / my_delete_select workflows)


# ── Plans ──────────────────────────────────────────────────────────────────────
async def render_plans(message: types.Message, db_user: User, is_callback=False):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="p_cancel"))
    await call.message.edit_text("Выбери задачу для переноса:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("mov_p:"))
async def mov_p_select(call: types.CallbackQuery):
    t_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    
    keyboard = [
        [
            InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})", callback_data=f"set_dt:pm:{t_id}:{d1.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})", callback_data=f"set_dt:pm:{t_id}:{d2.strftime('%Y-%m-%d')}"),
            InlineKeyboardButton(text="Другая дата", callback_data=f"rc_months_plan:{t_id}")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="p_menu_move")
        ]
    ]
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "p_menu_del")
async def p_del_menu(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
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
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="p_cancel"))
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
    for t in tasks:
        clean = clean_task_text(t.text)
        ds = t.date_execution.strftime('%d.%m')
        b.button(text=f"[{ds}] {clean}", callback_data=f"restore_t:{t.id}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"t_arch:{page-1}"))
    if len(tasks) == 10:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"t_arch:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="t_cancel"))
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("restore_t:"))
async def restore_task(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        old = await session.get(PersonalTask, t_id)
        if old:
            clean = clean_task_text(old.text)
            new_task = PersonalTask(
                user_id=db_user.id,
                text=clean,
                date_execution=datetime.now().date(),
                category="inbox",
                is_completed=False,
                is_deleted=False,
            )
            session.add(new_task)
            await session.commit()
    await call.answer("🔄 Задача восстановлена!", show_alert=False)
    await render_today(call.message, db_user, True)


# ── Shopping ──────────────────────────────────────────────────────────────────
async def render_shop(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ShoppingItem).where(
                and_(
                    ShoppingItem.house_id == ACTIVE_HOUSE_ID,
                    ShoppingItem.is_bought == False,
                    ShoppingItem.is_deleted == False,
                )
            ).order_by(ShoppingItem.priority.desc(), ShoppingItem.id.asc())
        )
        items = result.scalars().all()

    import random
    def get_emoji(item_id):
        from bot.parser import FOOD_EMOJIS
        state = random.getstate()
        random.seed(item_id)
        e = random.choice(FOOD_EMOJIS)
        random.setstate(state)
        return e

    if items:
        total = sum(i.price for i in items)
        text = f"🛒 *Покупки — {total} ₽*\n👉 _Тапни на товар для вычеркивания:_"
        builder = InlineKeyboardBuilder()
        for item in items:
            prefix = "🔴 " if item.priority == "high" else ""
            price_str = f"{item.price}₽ " if item.price > 0 else ""
            emoji = get_emoji(item.id)
            builder.button(text=f"{price_str}{emoji} {prefix}{item.item_name}", callback_data=f"done_shop:{item.id}")
        builder.adjust(2)
        builder.row(
            InlineKeyboardButton(text="✏️ Изм.", callback_data="s_edit"),
            InlineKeyboardButton(text="❌ Удал.", callback_data="s_del"),
            InlineKeyboardButton(text="📜 Архив", callback_data="s_arch:0"),
        )
    else:
        text = "🍏 *Список покупок пуст!*"
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📜 Архив покупок", callback_data="s_arch:0"))

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="shop_and_purchases_back"))

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data == "shop_view_items")
async def shop_view_items_handler(call: types.CallbackQuery, db_user: User = None):
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data == "s_cancel")
async def s_cancel(call: types.CallbackQuery, db_user: User = None):
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data.startswith("done_shop:"))
async def done_shop(call: types.CallbackQuery, db_user: User = None):
    s_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        item = await session.get(ShoppingItem, s_id)
        if item:
            item.is_bought = True
            item.bought_at = datetime.utcnow()
            await session.commit()
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data == "s_del")
async def s_del_menu(call: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ShoppingItem).where(
                and_(ShoppingItem.house_id == ACTIVE_HOUSE_ID, ShoppingItem.is_bought == False, ShoppingItem.is_deleted == False)
            )
        )
        items = result.scalars().all()
    b = InlineKeyboardBuilder()
    for i in items:
        b.button(text=f"❌ {i.item_name}", callback_data=f"del_shop:{i.id}")
    b.button(text="⬅️ Назад", callback_data="s_cancel")
    b.adjust(2)
    await call.message.edit_text("Выбери товар для удаления:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("del_shop:"))
async def del_shop(call: types.CallbackQuery, db_user: User = None):
    s_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        item = await session.get(ShoppingItem, s_id)
        if item:
            await session.delete(item)
            await session.commit()
    await call.answer("🗑 Удалено", show_alert=False)
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data == "s_edit")
async def s_edit_menu(call: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ShoppingItem).where(
                and_(ShoppingItem.house_id == ACTIVE_HOUSE_ID, ShoppingItem.is_bought == False, ShoppingItem.is_deleted == False)
            )
        )
        items = result.scalars().all()
    b = InlineKeyboardBuilder()
    for i in items:
        b.button(text=f"✏️ {i.item_name}", callback_data=f"ed_shop:{i.id}")
    b.button(text="⬅️ Назад", callback_data="s_cancel")
    b.adjust(2)
    await call.message.edit_text("Выбери товар для редактирования:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("ed_shop:"))
async def ed_shop_start(call: types.CallbackQuery, state: FSMContext):
    s_id = int(call.data.split(":")[1])
    await state.update_data(item_id=s_id)
    await state.set_state(EditShop.waiting_for_input)
    await call.message.edit_text("Отправь новое название и/или цену (например: `Протеин 3200`):")


@dp.message(EditShop.waiting_for_input)
async def ed_shop_process(message: types.Message, state: FSMContext, db_user: User = None):
    data = await state.get_data()
    _, clean_text, _, price, _, _ = parse_input(message.text)
    async with AsyncSessionLocal() as session:
        item = await session.get(ShoppingItem, data["item_id"])
        if item:
            item.item_name = clean_text
            item.price = price
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Обновлено: *{clean_text}*", parse_mode="Markdown")
    await render_shop(message, db_user)


@dp.callback_query(F.data.startswith("s_arch:"))
async def s_archive(call: types.CallbackQuery):
    page = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ShoppingItem).where(
                and_(ShoppingItem.house_id == ACTIVE_HOUSE_ID, ShoppingItem.is_bought == True, ShoppingItem.is_deleted == False)
            ).order_by(ShoppingItem.id.desc()).offset(page * 10).limit(10)
        )
        items = result.scalars().all()

    if not items and page == 0:
        await call.answer("Архив покупок пуст!", show_alert=False)
        return

    text = "📜 *Архив покупок*\n👉 _Тапни, чтобы вернуть в список:_\n\n"
    b = InlineKeyboardBuilder()
    for i in items:
        price_str = f"({i.price}₽)" if i.price > 0 else ""
        b.button(text=f"✅ {i.item_name} {price_str}", callback_data=f"restore_shop:{i.id}:{page}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"s_arch:{page-1}"))
    if len(items) == 10:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"s_arch:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="s_cancel"))
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("restore_shop:"))
async def restore_shop(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    s_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        old = await session.get(ShoppingItem, s_id)
        if old:
            new_item = ShoppingItem(
                house_id=ACTIVE_HOUSE_ID,
                user_id=db_user.id,
                item_name=old.item_name,
                price=old.price,
                priority=old.priority,
                is_bought=False,
                is_deleted=False,
            )
            session.add(new_item)
            await session.commit()
    await call.answer("🔄 Возвращено в список!", show_alert=False)
    await render_shop(call.message, db_user, True)


# ── Shop and Purchases (Магазин и Покупки) ────────────────────────────────────
async def render_shop_and_purchases(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        leaderboard_result = await session.execute(
            select(User)
            .where(User.house_id == ACTIVE_HOUSE_ID)
            .order_by(User.points.desc())
        )
        leaderboard = leaderboard_result.scalars().all()

        week_ago = datetime.utcnow() - timedelta(days=7)
        weekly_comps_result = await session.execute(
            select(Completion).where(Completion.created_at >= week_ago)
        )
        weekly_comps = weekly_comps_result.scalars().all()
        weekly_map = {}
        for c in weekly_comps:
            weekly_map[c.user_id] = weekly_map.get(c.user_id, 0) + (c.points or 0)

    text = "🏆 Баланс героев:\n"
    for usr in leaderboard:
        weekly = weekly_map.get(usr.id, 0)
        text += f"🦸\u200d♂️ {usr.display_name}: {usr.points or 0} 🍪 (За неделю: +{weekly} 🍪)\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Магазин", callback_data="rewards_shop_view"),
        InlineKeyboardButton(text="Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="Архив", callback_data="stat_arch:0"),
    )

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@dp.message(F.text.in_({"📊 Stat", "🛍 Магазин и Покупки"}))
async def handle_shop_and_purchases_btn(message: types.Message, db_user: User = None):
    await render_shop_and_purchases(message, db_user)


@dp.callback_query(F.data == "shop_and_purchases_back")
async def handle_shop_and_purchases_back(call: types.CallbackQuery, db_user: User = None):
    await render_shop_and_purchases(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("stat_arch:"))
async def handle_stat_arch(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    page_size = 10
    from zoneinfo import ZoneInfo
    from datetime import timezone as dt_timezone

    async with AsyncSessionLocal() as session:
        chore_result = await session.execute(
            select(Completion, User, TaskTemplate)
            .join(User, Completion.user_id == User.id)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(TaskTemplate.house_id == ACTIVE_HOUSE_ID)
            .order_by(Completion.created_at.desc())
        )
        chore_comps = chore_result.all()

        house_users_result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )
        house_users = house_users_result.scalars().all()
        house_user_ids = [u.id for u in house_users]
        user_name_map = {u.id: (u.display_name or u.username or "?") for u in house_users}

        pt_result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id.in_(house_user_ids),
                    PersonalTask.is_completed == True,
                    PersonalTask.is_deleted == False
                )
            ).order_by(PersonalTask.date_execution.desc())
        )
        pt_comps = pt_result.scalars().all()

        house = await session.get(House, ACTIVE_HOUSE_ID)
        tz_str = house.timezone or "Europe/Moscow" if house else "Europe/Moscow"

    tz = ZoneInfo(tz_str)
    entries = []

    for comp, usr, tmpl in chore_comps:
        utc_dt = comp.created_at.replace(tzinfo=dt_timezone.utc)
        local_dt = utc_dt.astimezone(tz)
        pts_val = "2-8" if tmpl.title == "Готовка" else str(comp.points)
        u_name = usr.display_name or usr.username or "?"
        entries.append({
            "sort_key": local_dt.replace(tzinfo=None),
            "name": tmpl.title,
            "info": f"{local_dt.strftime('%d.%m %H:%M')} {u_name} +{pts_val}🍪",
            "cb2": f"tmpl_set:{tmpl.id}:chores_arch_0"
        })

    for pt in pt_comps:
        u_name = user_name_map.get(pt.user_id, "?")
        clean = clean_task_text(pt.text)
        sort_dt = datetime.combine(pt.date_execution, datetime.min.time())
        entries.append({
            "sort_key": sort_dt,
            "name": clean,
            "info": f"{pt.date_execution.strftime('%d.%m')} {u_name} ✅",
            "cb2": "noop"
        })

    entries.sort(key=lambda x: x["sort_key"], reverse=True)

    total = len(entries)
    start = page * page_size
    end = start + page_size
    page_entries = entries[start:end]

    if not page_entries and page == 0:
        await call.answer("Архив пуст!", show_alert=False)
        return

    text = "📜 *Архив выполненных задач:*"
    builder = InlineKeyboardBuilder()
    for e in page_entries:
        builder.row(
            InlineKeyboardButton(text=e["name"], callback_data="noop"),
            InlineKeyboardButton(text=e["info"], callback_data=e["cb2"])
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"stat_arch:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"stat_arch:{page+1}"))
    if nav:
        builder.row(*nav)

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")



# ── Rewards Shop (Магазин наград) ─────────────────────────────────────────────
async def render_rewards_settings(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()

    text = "⚙️ *Управление наградами:*\n\n"
    builder = InlineKeyboardBuilder()
    if rewards:
        for r in rewards:
            text += f"• *{r.title}* — `{r.price} 🍪`\n"
            builder.button(text=f"❌ {r.title}", callback_data=f"del_reward:{r.id}")
        text += "\n_Нажмите на кнопку с наградой, чтобы удалить её._"
    else:
        text += "Наград пока нет."

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить награду", callback_data="add_reward_start"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="rewards_back")
    )
    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data == "rewards_shop_view")
async def handle_rewards_shop_view(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()

    text = "🎁 Магазин наград\nДля покупки нажми на выбранную награду:"

    builder = InlineKeyboardBuilder()
    if rewards:
        for r in rewards:
            builder.row(InlineKeyboardButton(text=f"{r.title} ({r.price}🍪)", callback_data=f"buy_reward:{r.id}"))
    else:
        text += "\nНаград пока нет."

    builder.row(InlineKeyboardButton(text="⚙️ Управление наградами", callback_data="rewards_settings"))

    await call.message.edit_text(text, reply_markup=builder.as_markup())


@dp.callback_query(F.data == "rewards_back")
async def handle_rewards_back(call: types.CallbackQuery, db_user: User = None):
    await handle_rewards_shop_view(call, db_user)


@dp.callback_query(F.data.startswith("buy_reward:"))
async def handle_buy_reward(call: types.CallbackQuery, db_user: User = None):
    reward_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if not reward:
            await call.answer("⚠️ Награда не найдена!", show_alert=False)
            return
        
        user = await session.get(User, db_user.id)
        if (user.points or 0) < reward.price:
            await call.answer("⚠️ Недостаточно баллов для покупки!", show_alert=False)
            return
        
        user.points -= reward.price
        purchase = RewardPurchase(
            user_id=db_user.id,
            reward_title=reward.title,
            price=reward.price,
            status="purchased"
        )
        session.add(purchase)
        await session.commit()
        await call.answer(f"🎉 Куплено: {reward.title}! Списано {reward.price} 🍪", show_alert=False)
        
    await handle_rewards_shop_view(call, db_user)


@dp.callback_query(F.data == "rewards_settings")
async def handle_rewards_settings_btn(call: types.CallbackQuery, db_user: User = None):
    await render_rewards_settings(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("del_reward:"))
async def handle_del_reward(call: types.CallbackQuery, db_user: User = None):
    reward_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if reward:
            await session.delete(reward)
            await session.commit()
            await call.answer("🗑 Награда удалена!", show_alert=False)
        else:
            await call.answer("⚠️ Награда не найдена!", show_alert=False)
    await render_rewards_settings(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("rewards_purchases:"))
async def handle_rewards_purchases(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RewardPurchase, User)
            .join(User, RewardPurchase.user_id == User.id)
            .where(User.house_id == ACTIVE_HOUSE_ID)
            .order_by(RewardPurchase.created_at.desc())
            .offset(page * 5)
            .limit(5)
        )
        rows = result.all()

    text = "📜 *История купленных наград:*\n\n"
    if rows:
        for purchase, usr in rows:
            dt_str = purchase.created_at.strftime("%d.%m %H:%M")
            text += f"• *{dt_str}* — {usr.display_name} купил *{purchase.reward_title}* (`-{purchase.price} 🍪`)\n"
    else:
        text += "Покупок пока не было."

    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"rewards_purchases:{page-1}"))
    if len(rows) == 5:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"rewards_purchases:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rewards_back"))
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# Add reward FSM flow
@dp.callback_query(F.data == "add_reward_start")
async def handle_add_reward_start(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddRewardState.waiting_for_title)
    await call.message.edit_text(
        "✏️ *Добавление новой награды*\n\nВведите название награды (например: Пицца за счет дома):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_reward_cancel")
        ]]),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "add_reward_cancel")
async def handle_add_reward_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    await state.clear()
    await call.answer("Отменено", show_alert=False)
    await render_rewards_settings(call.message, db_user, is_callback=True)


@dp.message(StateFilter(AddRewardState.waiting_for_title))
async def handle_add_reward_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
    await state.update_data(title=title)
    await state.set_state(AddRewardState.waiting_for_price)
    await message.answer(
        f"Установлено название: *{title}*\n\nСколько баллов (🍪) должна стоить эта награда? (Введите число, например: 50):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_reward_cancel")
        ]]),
        parse_mode="Markdown"
    )


@dp.message(StateFilter(AddRewardState.waiting_for_price))
async def handle_add_reward_price(message: types.Message, state: FSMContext, db_user: User = None):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Пожалуйста, введите целое положительное число (например: 50):")
        return
    
    data = await state.get_data()
    title = data["title"]
    
    async with AsyncSessionLocal() as session:
        reward = Reward(
            house_id=ACTIVE_HOUSE_ID,
            title=title,
            price=price
        )
        session.add(reward)
        await session.commit()
        
    await state.clear()
    await message.answer(f"✅ Награда успешно добавлена: *{title}* за *{price}* 🍪")
    await render_rewards_settings(message, db_user, is_callback=False)


# ── Text/Voice input ──────────────────────────────────────────────────────────
@dp.message(StateFilter(None), F.text)
async def handle_any_text(message: types.Message, db_user: User = None, text_override: str = None):
    input_text = text_override or message.text
    msg_type, clean_text, date_exec, price, priority, recurrence = parse_input(input_text)

    async with AsyncSessionLocal() as session:
        if msg_type == "purchase":
            item = ShoppingItem(
                house_id=ACTIVE_HOUSE_ID,
                user_id=db_user.id if db_user else None,
                item_name=clean_text,
                price=price,
                priority=priority,
                is_bought=False,
                is_deleted=False,
            )
            session.add(item)
            await session.commit()
            await message.answer(f"🛒 Записал в покупки: *{clean_text}*", parse_mode="Markdown")
        else:
            await message.answer(
                "⚠️ Личные задачи можно добавить только по кнопке в разделе 📋 My.\nЧтобы записать покупку, введи: купить [что-то] [очки]",
                parse_mode="Markdown"
            )


@dp.message(StateFilter(None), F.voice)
async def handle_voice(message: types.Message, db_user: User = None):
    processing_msg = await message.answer("🎙 Слушаю...")
    try:
        import speech_recognition as sr
        from pydub import AudioSegment

        file = await bot.get_file(message.voice.file_id)
        ogg_path = f"temp_{message.from_user.id}.ogg"
        wav_path = f"temp_{message.from_user.id}.wav"
        await bot.download_file(file.file_path, destination=ogg_path)

        audio = AudioSegment.from_file(ogg_path, format="ogg")
        audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            recognized = recognizer.recognize_google(audio_data, language="ru-RU")

        import os as _os
        _os.remove(ogg_path)
        _os.remove(wav_path)

        await processing_msg.edit_text(f"🗣 *Распознано:* {recognized}", parse_mode="Markdown")
        await handle_any_text(message, db_user=db_user, text_override=recognized)

    except ImportError:
        await processing_msg.edit_text("⚠️ Библиотеки для распознавания голоса не установлены.")
    except Exception as e:
        logger.error(f"Voice recognition error: {e}")
        await processing_msg.edit_text("⚠️ Голос не распознан. Попробуй ещё раз.")


@dp.callback_query(F.data == "ignore")
async def noop(call: types.CallbackQuery):
    await call.answer(show_alert=False)


@dp.callback_query(F.data == "noop")
async def handle_noop(call: types.CallbackQuery):
    await call.answer(show_alert=False)


@dp.callback_query(F.data == "settings_del_menu")
async def handle_settings_del_menu(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
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
    for t in templates:
        builder.row(InlineKeyboardButton(text=f"🗑 {t.title}", callback_data=f"te_del:{t.id}:settings"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="chores_settings"))
    await call.message.edit_text("Выберите задачу для удаления:", reply_markup=builder.as_markup())


# Plan custom calendar navigation & shift execution callbacks
@dp.callback_query(F.data.startswith("rc_months_plan:"))
async def handle_rc_months_plan(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(t_id, today.year, today.month, today, "plan")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_plan:"))
async def handle_cal_nav_plan(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    today = datetime.now().date()
    markup = create_calendar_keyboard_custom(t_id, year, month, today, "plan")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_plan:"))
async def handle_shift_plan(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    date_str = parts[2]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            clean_text = clean_task_text(task.text)
            task.date_execution = new_date
            await session.commit()
            await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}!", show_alert=False)
            await call.message.answer(f"🔄 Задача '{clean_text}' перенесена на {new_date.strftime('%d.%m.%Y')}!")
            
    await render_plans(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("shift_plan_menu:"))
async def handle_shift_plan_menu(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    call.data = f"mov_p:{t_id}"
    await mov_p_select(call)


@dp.callback_query(F.data.startswith("set_dt:"))
async def exe_set_dt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    prefix = parts[1]
    t_id = int(parts[2])
    date_str = parts[3]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            clean = clean_task_text(task.text)
            task.date_execution = new_date
            await session.commit()
            await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}!", show_alert=False)
            await call.message.answer(f"🔄 Задача '{clean}' перенесена на {new_date.strftime('%d.%m.%Y')}!")
    if prefix == "tm":
        await render_today(call.message, db_user, True)
    else:
        await render_plans(call.message, db_user, True)
