import os
import logging
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import KeyboardButton, InlineKeyboardButton, WebAppInfo
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

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


class AddRewardState(StatesGroup):
    waiting_for_title = State()
    waiting_for_price = State()




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

    if chores:
        text = (
            "🌅 *Доброе утро! Твои задачи на сегодня:*\n\n"
            "🎯 *План на сегодня*\n"
            f"(_Можно залутать {total_cookies} 🍪_)\n\n"
            "👉 Чтобы взять задачу в работу, нажми на её название 👇"
        )
    else:
        text = (
            "🌅 *Доброе утро! Твои задачи на сегодня:*\n\n"
            "🎯 *План на сегодня*\n"
            "(_Можно залутать 0 🍪_)\n\n"
            "🎉 *Все домашние дела на сегодня разобраны!*"
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
        InlineKeyboardButton(text="➕", callback_data="chores_add_menu"),
        InlineKeyboardButton(text="⚙️", callback_data="chores_settings"),
        InlineKeyboardButton(text="📁", callback_data="chores_arch:0")
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
        InlineKeyboardButton(text="📋", callback_data="add_from_templates_list"),
        InlineKeyboardButton(text="➕", callback_data="add_tmpl_start")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="chores_back"))
    await call.message.edit_text(
        "➕ *Добавить задачу:*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "add_from_templates_list")
async def handle_add_from_templates_list(call: types.CallbackQuery, db_user: User = None):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.deleted == False
                )
            ).order_by(TaskTemplate.title)
        )
        templates = result.scalars().all()

    text = "📋 *Выберите задачу из списка дел для добавления на сегодня:*"
    builder = InlineKeyboardBuilder()
    if templates:
        for t in templates:
            builder.button(
                text=f"{t.title} ({t.points}🍪)",
                callback_data=f"spawn_chore:{t.id}"
            )
    else:
        text = "⚠️ Шаблонов дел пока нет!"

    builder.adjust(1)
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
            await call.answer(f"✅ Добавлено на сегодня: {tmpl.title}")
        else:
            await call.answer("⚠️ Задача не найдена!")
    await render_household_chores(call.message, db_user, is_callback=True)


NUDGE_PHRASES = [
    "Домовой жалуется на беспорядок! Тут плачет без внимания: <b>{task_title}</b> 🥺",
    "Печеньки 🍪 сами себя не заработают! Тебя ждет отличный контракт: <b>{task_title}</b>",
    "Кажется, кто-то очень хочет, чтобы эта задача решилась. Герой, твой выход: <b>{task_title}</b> 🦸‍♂️",
    "Министерство уюта напоминает! Открыта горячая вакансия на дело: <b>{task_title}</b> 🔥",
    "Освободилось немного времени? Идеальный момент, чтобы закрыть: <b>{task_title}</b> ✨"
]
nudge_cache = {}

@dp.callback_query(F.data.startswith("tmpl_set:") & F.data.endswith(":today"))
async def handle_tmpl_set_today(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    tmpl_id = int(parts[1])
    today = datetime.now().date()
    
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tmpl_id)
        if not tmpl:
            await call.answer("Шаблон не найден", show_alert=True)
            return
        
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
            await call.answer("Копия задачи на сегодня не найдена", show_alert=True)
            return

        last_comp = await session.execute(
            select(Completion.created_at)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .where(TaskInstance.template_id == tmpl.id)
            .order_by(Completion.created_at.desc())
            .limit(1)
        )
        last_done_dt = last_comp.scalar()
        last_done_str = last_done_dt.strftime("%d.%m.%Y") if last_done_dt else "никогда"
        next_done_str = today.strftime("%d.%m.%Y")

    period_lbl = period_label_ru(tmpl.periodicity)
    pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)

    text = (
        f"ℹ️ *Информация:*\n\n"
        f"📋 *{tmpl.title}*\n"
        f"Награда: {pts_str}🍪 | {period_lbl}\n"
        f"📅 Последнее выполнение: *{last_done_str}*\n"
        f"🔮 Следующее выполнение: *{next_done_str}*"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔔 Намек", callback_data=f"nudge:{inst.id}"),
        InlineKeyboardButton(text="📅 Сдвиг", callback_data=f"resched_menu:{inst.id}"),
        InlineKeyboardButton(text="🗑 Копию", callback_data=f"del_inst:{inst.id}")
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"tmpl_set:{tmpl.id}:today_list"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="chores_back")
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("nudge:"))
async def handle_nudge(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    
    if nudge_cache.get(inst_id) == today:
        await call.answer("Тише-тише, намек уже отправлен. Ждем реакции! 🤫", show_alert=True)
        return
        
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("Задача не найдена")
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
                
    await call.answer("Намек успешно отправлен! 🔔", show_alert=True)
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
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{d1.strftime('%d.%m')} ({days_ru[d1.weekday()]})",
            callback_data=f"shift:once:{inst_id}:{d1.strftime('%Y-%m-%d')}"
        ),
        InlineKeyboardButton(
            text=f"{d2.strftime('%d.%m')} ({days_ru[d2.weekday()]})",
            callback_data=f"shift:once:{inst_id}:{d2.strftime('%Y-%m-%d')}"
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data=f"tmpl_set:{tmpl_id}:today")
    )
    await call.message.edit_text("На какой день перенести задачу?", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("shift:once:"))
async def handle_shift_once(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[2])
    date_str = parts[3]
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("Ошибка: задача не найдена.", show_alert=True)
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
        await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}!", show_alert=True)
        
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("del_inst:"))
async def handle_del_inst(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst:
            inst.status = "skipped"
            await session.commit()
            await call.answer("🗑 Копия задачи убрана на сегодня!", show_alert=True)
        else:
            await call.answer("⚠️ Задача не найдена!")
            
    await render_household_chores(call.message, db_user, is_callback=True)


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
                await call.answer("🏠 Задача взята в работу! Она добавлена в «Мои дела».")
            else:
                await call.answer("⚠️ Кто-то уже взял эту задачу!")
        else:
            await call.answer("⚠️ Задача не найдена!")
    await render_household_chores(call.message, db_user, is_callback=True)


@dp.callback_query(F.data == "chores_settings")
async def handle_chores_settings(call: types.CallbackQuery, db_user: User = None):
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

    text = "⚙️ *Настройка шаблонов домашних дел:*\n\n"
    builder = InlineKeyboardBuilder()
    if templates:
        for t in templates:
            period_lbl = period_label_ru(t.periodicity)
            text += f"• *{t.title}* ({t.points}🍪, {period_lbl})\n"
            builder.button(text=f"❌ {t.title}", callback_data=f"del_tmpl:{t.id}")
        text += "\n_Нажмите на кнопку с шаблоном, чтобы удалить его._"
    else:
        text += "Шаблонов пока нет. Добавьте первый!"

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить шаблон", callback_data="add_tmpl_start"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="chores_back")
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("del_tmpl:"))
async def handle_del_tmpl(call: types.CallbackQuery, db_user: User = None):
    tmpl_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, tmpl_id)
        if tmpl:
            tmpl.deleted = True
            await session.commit()
            await call.answer("🗑 Шаблон удален!")
        else:
            await call.answer("⚠️ Шаблон не найден!")
    await handle_chores_settings(call, db_user)


@dp.callback_query(F.data.startswith("chores_arch:"))
async def handle_chores_archive(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Completion, User, TaskTemplate)
            .join(User, Completion.user_id == User.id)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .order_by(Completion.created_at.desc())
            .offset(page * 5)
            .limit(5)
        )
        rows = result.all()

    text = "📜 *История выполненных домашних дел:*\n\n"
    if rows:
        for comp, usr, tmpl in rows:
            dt_str = comp.created_at.strftime("%d.%m %H:%M")
            text += f"• *{dt_str}* — {usr.display_name} выполнил *{tmpl.title}* (`+{comp.points} 🍪`)\n"
    else:
        text += "История пуста!"

    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"chores_arch:{page-1}"))
    if len(rows) == 5:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"chores_arch:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="chores_back"))
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# Add template FSM flow
@dp.callback_query(F.data == "add_tmpl_start")
async def handle_add_tmpl_start(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddTemplateState.waiting_for_title)
    await call.message.edit_text(
        "✏️ *Добавление нового шаблона дела*\n\nВведите название домашнего дела (например: Помыть холодильник):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel")
        ]]),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "add_tmpl_cancel")
async def handle_add_tmpl_cancel(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    await state.clear()
    await call.answer("Отменено")
    await handle_chores_settings(call, db_user)


@dp.message(AddTemplateState.waiting_for_title)
async def handle_add_tmpl_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуйте еще раз:")
        return
    await state.update_data(title=title)
    await state.set_state(AddTemplateState.waiting_for_points)
    await message.answer(
        f"Установлено название: *{title}*\n\nСколько баллов (🍪) давать за выполнение? (Введите число, например: 3):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel")
        ]]),
        parse_mode="Markdown"
    )


@dp.message(AddTemplateState.waiting_for_points)
async def handle_add_tmpl_points(message: types.Message, state: FSMContext):
    try:
        pts = int(message.text.strip())
        if pts <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("Пожалуйста, введите целое положительное число (например: 3):")
        return
    
    await state.update_data(points=pts)
    await state.set_state(AddTemplateState.waiting_for_periodicity)
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Каждый день", callback_data="set_tmpl_period:daily"),
        InlineKeyboardButton(text="Раз в неделю", callback_data="set_tmpl_period:weekly")
    )
    builder.row(
        InlineKeyboardButton(text="2 раза в неделю", callback_data="set_tmpl_period:twice_weekly"),
        InlineKeyboardButton(text="Раз в месяц", callback_data="set_tmpl_period:monthly")
    )
    builder.row(
        InlineKeyboardButton(text="2 раза в месяц", callback_data="set_tmpl_period:twice_monthly"),
        InlineKeyboardButton(text="Раз в квартал", callback_data="set_tmpl_period:quarterly")
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="add_tmpl_cancel"))
    builder.adjust(2)
    
    await message.answer(
        f"Установлено баллов: *{pts}* 🍪\n\nВыберите периодичность выполнения дела:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(AddTemplateState.waiting_for_periodicity, F.data.startswith("set_tmpl_period:"))
async def handle_add_tmpl_periodicity(call: types.CallbackQuery, state: FSMContext, db_user: User = None):
    periodicity = call.data.split(":")[1]
    data = await state.get_data()
    title = data["title"]
    pts = data["points"]
    
    async with AsyncSessionLocal() as session:
        tmpl = TaskTemplate(
            house_id=ACTIVE_HOUSE_ID,
            title=title,
            points=pts,
            periodicity=periodicity,
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
    await call.answer("✅ Шаблон успешно добавлен!")
    await handle_chores_settings(call, db_user)


# ── Render Today ───────────────────────────────────────────────────────────────
async def rollover_overdue_tasks(session: AsyncSession, user_id: int):
    """Move overdue uncompleted tasks to today."""
    from sqlalchemy import update, text
    today = datetime.now().date()
    result = await session.execute(
        select(PersonalTask).where(
            and_(
                PersonalTask.user_id == user_id,
                PersonalTask.date_execution < today,
                PersonalTask.is_completed == False,
                PersonalTask.is_deleted == False,
            )
        )
    )
    tasks = result.scalars().all()
    for task in tasks:
        clean = clean_task_text(task.text)
        task.text = clean
        task.date_execution = today
    if tasks:
        await session.commit()


async def ensure_daily_routines(session: AsyncSession, user_id: int):
    pass


async def render_today(message: types.Message, db_user: User, is_callback=False):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        await rollover_overdue_tasks(session, db_user.id)

        # 1. Fetch personal tasks
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution == today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.id)
        )
        personal_tasks = result.scalars().all()

        # 2. Fetch user's claimed chores
        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress",
                    TaskInstance.date == today
                )
            )
            .order_by(TaskInstance.id)
        )
        my_chores = chores_result.all()

    text = "👤 *Твои дела на сегодня:*\n\n"
    builder = InlineKeyboardBuilder()

    # Personal tasks rendering
    text += "*👤 Личные задачи:*\n"
    if personal_tasks:
        for t in personal_tasks:
            clean = clean_task_text(t.text)
            rec_icon = " 🔁" if t.recurrence else ""
            text += f"• {clean}{rec_icon}\n"
            builder.button(text=clean, callback_data=f"done_task:{t.id}")
    else:
        text += "_Нет личных задач_\n"
    text += "\n"

    # Household chores rendering
    text += "*🏠 В работе из домашних:*\n"
    if my_chores:
        for inst, tmpl in my_chores:
            pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
            text += f"• {tmpl.title} (`+{pts_str} 🍪`)\n"
            builder.button(
                text=f"🏠 {tmpl.title} (+{pts_str}🍪)",
                callback_data=f"done_chore_inst:{inst.id}"
            )
    else:
        text += "_Нет взятых домашних дел_\n"

    # Adjust buttons layout (1 per row)
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить", callback_data="my_edit_menu"),
        InlineKeyboardButton(text="🗄️ Архив", callback_data="t_arch:0"),
    )

    markup = builder.as_markup()
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


@dp.callback_query(F.data == "my_edit_menu")
async def handle_my_edit_menu(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        # Fetch active personal tasks for today
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == db_user.id,
                    PersonalTask.date_execution == today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.id)
        )
        personal_tasks = result.scalars().all()

        # Fetch claimed chores in progress for today
        chores_result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskInstance.done_by_user_id == db_user.id,
                    TaskInstance.status == "in_progress",
                    TaskInstance.date == today
                )
            ).order_by(TaskInstance.id)
        )
        my_chores = chores_result.all()

    if not personal_tasks and not my_chores:
        await call.answer("⚠️ Нет активных задач для редактирования!")
        return

    text = "✏️ *Выберите задачу для редактирования:*"
    builder = InlineKeyboardBuilder()

    for t in personal_tasks:
        clean = clean_task_text(t.text)
        builder.button(text=f"👤 {clean}", callback_data=f"edit_task_opts:{t.id}")

    for inst, tmpl in my_chores:
        builder.button(text=f"🏠 {tmpl.title}", callback_data=f"edit_chore_opts:{inst.id}")

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="t_cancel")
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("edit_task_opts:"))
async def handle_edit_task_opts(call: types.CallbackQuery, db_user: User = None):
    task_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, task_id)
        if not task:
            await call.answer("⚠️ Задача не найдена!")
            await handle_my_edit_menu(call, db_user)
            return
        clean = clean_task_text(task.text)

    text = f"⚙️ *Редактирование задачи:*\n\n*{clean}*"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 Перенести", callback_data=f"mov_t:{task_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_t:{task_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="my_edit_menu")
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("edit_chore_opts:"))
async def handle_edit_chore_opts(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("⚠️ Домашнее дело не найдено!")
            await handle_my_edit_menu(call, db_user)
            return
        tmpl = await session.get(TaskTemplate, inst.template_id)
        title = tmpl.title if tmpl else "Без названия"

    text = f"⚙️ *Редактирование домашнего дела:*\n\n*{title}*"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить копию", callback_data=f"del_inst_from_my:{inst_id}"),
        InlineKeyboardButton(text="↩ Вернуть в пул", callback_data=f"unclaim_chore_inst:{inst_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="my_edit_menu")
    )
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("del_inst_from_my:"))
async def handle_del_inst_from_my(call: types.CallbackQuery, db_user: User = None):
    inst_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if inst and inst.done_by_user_id == db_user.id:
            inst.status = "skipped"
            await session.commit()
            await call.answer("🗑 Копия домашнего дела убрана!", show_alert=True)
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!")
    await render_today(call.message, db_user, is_callback=True)


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
            await call.answer(f"✅ Выполнено! Начислено +{pts} 🍪")
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!")
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
            await call.answer("↪ Задача возвращена в общий пул свободных дел.")
        else:
            await call.answer("⚠️ Задача не найдена или не назначена на вас!")
    await render_today(call.message, db_user, is_callback=True)


# ── Move / Delete tasks ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "t_menu_move")
async def t_move_menu(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == db_user.id, PersonalTask.date_execution == today,
                     PersonalTask.is_completed == False, PersonalTask.is_deleted == False)
            )
        )
        tasks = result.scalars().all()
    b = InlineKeyboardBuilder()
    for t in tasks:
        b.button(text=t.text, callback_data=f"mov_t:{t.id}")
    b.button(text="⬅️ Назад", callback_data="t_cancel")
    b.adjust(1)
    await call.message.edit_text("Выбери задачу для переноса:", reply_markup=b.as_markup())


@dp.callback_query(F.data == "t_menu_del")
async def t_del_menu(call: types.CallbackQuery, db_user: User = None):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == db_user.id, PersonalTask.date_execution == today,
                     PersonalTask.is_completed == False, PersonalTask.is_deleted == False)
            )
        )
        tasks = result.scalars().all()
    b = InlineKeyboardBuilder()
    for t in tasks:
        b.button(text=f"❌ {t.text}", callback_data=f"del_t:{t.id}")
    b.button(text="⬅️ Назад", callback_data="t_cancel")
    b.adjust(1)
    await call.message.edit_text("Выбери задачу для удаления:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("mov_t:"))
async def mov_t_select(call: types.CallbackQuery):
    t_id = int(call.data.split(":")[1])
    today = datetime.now().date()
    d1 = today + timedelta(days=1)
    d2 = today + timedelta(days=2)
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} {days_ru[d1.weekday()]}", callback_data=f"set_dt:tm:{t_id}:{d1}"),
        InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} {days_ru[d2.weekday()]}", callback_data=f"set_dt:tm:{t_id}:{d2}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="t_cancel"),
    )
    await call.message.edit_text("Выбери дату для переноса:", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("set_dt:"))
async def exe_set_dt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    prefix = parts[1]
    t_id = int(parts[2])
    new_date = datetime.strptime(parts[3], "%Y-%m-%d").date()
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            clean = clean_task_text(task.text)
            task.text = clean
            task.date_execution = new_date
            await session.commit()
    await call.answer(f"✅ Перенесено на {new_date.strftime('%d.%m')}")
    if prefix == "tm":
        await render_today(call.message, db_user, True)
    else:
        await render_plans(call.message, db_user, True)


@dp.callback_query(F.data.startswith("del_t:"))
async def exe_del_t(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        if task:
            clean = clean_task_text(task.text)
            await session.delete(task)
            await session.commit()
    await call.answer("🗑 Удалено")
    await render_today(call.message, db_user, True)


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
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, t_id)
        t_date = task.date_execution
    d1 = t_date + timedelta(days=1)
    d2 = t_date + timedelta(days=2)
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"{d1.strftime('%d.%m')} {days_ru[d1.weekday()]}", callback_data=f"set_dt:pm:{t_id}:{d1}"),
        InlineKeyboardButton(text=f"{d2.strftime('%d.%m')} {days_ru[d2.weekday()]}", callback_data=f"set_dt:pm:{t_id}:{d2}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="p_cancel"),
    )
    await call.message.edit_text("Выбери новую дату:", reply_markup=b.as_markup())


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
    await call.answer("🗑 Удалено")
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
        await call.answer("Архив пуст!")
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
    await call.answer("🔄 Задача восстановлена!")
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
    await call.answer("🗑 Удалено")
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
        await call.answer("Архив покупок пуст!")
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
    await call.answer("🔄 Возвращено в список!")
    await render_shop(call.message, db_user, True)


# ── Shop and Purchases (Магазин и Покупки) ────────────────────────────────────
async def render_shop_and_purchases(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, db_user.id)
        points = user.points or 0

        leaderboard_result = await session.execute(
            select(User)
            .where(User.house_id == ACTIVE_HOUSE_ID)
            .order_by(User.points.desc())
        )
        leaderboard = leaderboard_result.scalars().all()

        shopping_result = await session.execute(
            select(ShoppingItem)
            .where(
                and_(
                    ShoppingItem.house_id == ACTIVE_HOUSE_ID,
                    ShoppingItem.is_bought == False,
                    ShoppingItem.is_deleted == False
                )
            )
            .order_by(ShoppingItem.priority.desc(), ShoppingItem.id.asc())
        )
        shopping_items = shopping_result.scalars().all()

    text = "🛍 *Магазин и Покупки*\n\n"
    text += f"✨ *Твой баланс:* `{points} 🍪`\n\n"

    text += "🏆 *Рейтинг участников:*\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, usr in enumerate(leaderboard):
        medal = medals[idx] if idx < len(medals) else "👤"
        text += f"{medal} {usr.display_name} — `{usr.points or 0} 🍪`\n"
    text += "\n"

    text += "🛒 *Список покупок:*\n"
    if shopping_items:
        for item in shopping_items[:5]:
            prefix = "🔴 " if item.priority == "high" else ""
            price_str = f" ({item.price}₽)" if item.price > 0 else ""
            text += f"• {prefix}{item.item_name}{price_str}\n"
        if len(shopping_items) > 5:
            text += f"_...и еще {len(shopping_items) - 5} товаров_\n"
    else:
        text += "_Список покупок пуст_\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛒 Перейти в покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="🎁 Магазин наград", callback_data="rewards_shop_view")
    )

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text.in_({"📊 Stat", "🛍 Магазин и Покупки"}))
async def handle_shop_and_purchases_btn(message: types.Message, db_user: User = None):
    await render_shop_and_purchases(message, db_user)


@dp.callback_query(F.data == "shop_and_purchases_back")
async def handle_shop_and_purchases_back(call: types.CallbackQuery, db_user: User = None):
    await render_shop_and_purchases(call.message, db_user, is_callback=True)


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
        user = await session.get(User, db_user.id)
        points = user.points or 0

        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()

    text = "🎁 *Магазин наград*\n\n"
    text += f"✨ *Твой баланс:* `{points} 🍪`\n"
    text += "Выбери награду для покупки:\n\n"

    builder = InlineKeyboardBuilder()
    if rewards:
        for r in rewards:
            text += f"• *{r.title}* — `{r.price} 🍪`\n"
            builder.button(text=f"Купить: {r.title} ({r.price}🍪)", callback_data=f"buy_reward:{r.id}")
    else:
        text += "_Награды пока не добавлены._"

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="⚙️ Управление наградами", callback_data="rewards_settings"),
        InlineKeyboardButton(text="📜 Мои покупки", callback_data="rewards_purchases:0")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="shop_and_purchases_back"))

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data == "rewards_back")
async def handle_rewards_back(call: types.CallbackQuery, db_user: User = None):
    await handle_rewards_shop_view(call, db_user)


@dp.callback_query(F.data.startswith("buy_reward:"))
async def handle_buy_reward(call: types.CallbackQuery, db_user: User = None):
    reward_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if not reward:
            await call.answer("⚠️ Награда не найдена!")
            return
        
        user = await session.get(User, db_user.id)
        if (user.points or 0) < reward.price:
            await call.answer("⚠️ Недостаточно баллов для покупки!")
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
        await call.answer(f"🎉 Куплено: {reward.title}! Списано {reward.price} 🍪")
        
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
            await call.answer("🗑 Награда удалена!")
        else:
            await call.answer("⚠️ Награда не найдена!")
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
    await call.answer("Отменено")
    await render_rewards_settings(call.message, db_user, is_callback=True)


@dp.message(AddRewardState.waiting_for_title)
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


@dp.message(AddRewardState.waiting_for_price)
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
            task = PersonalTask(
                user_id=db_user.id,
                text=clean_text,
                date_execution=date_exec,
                category="inbox",
                recurrence=recurrence,
                is_completed=False,
                is_deleted=False,
            )
            session.add(task)
            await session.commit()
            today = datetime.now().date()
            date_str = "Сегодня" if date_exec == today else date_exec.strftime('%d.%m')
            rec_text = " 🔁 (Повторяющаяся)" if recurrence else ""
            await message.answer(
                f"✅ Добавил: *{clean_text}*\n🗓 {date_str}{rec_text}",
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
    await call.answer()
