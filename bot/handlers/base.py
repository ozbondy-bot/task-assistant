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
import sqlalchemy as sa


import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import AsyncSessionLocal, User, House, PersonalTask, ShoppingItem, TaskTemplate, TaskInstance, Completion, Reward, RewardPurchase, PendingAction
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


async def get_partner_user(session, current_user_id: int) -> User:
    result = await session.execute(
        select(User).where(and_(User.house_id == ACTIVE_HOUSE_ID, User.id != current_user_id))
    )
    return result.scalar_one_or_none()


@dp.callback_query(F.data.startswith("approve_act:"))
async def handle_approve_action(call: types.CallbackQuery, db_user: User = None):
    action_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        pending = await session.get(PendingAction, action_id)
        if not pending:
            await call.answer("⚠️ Запрос не найден или уже обработан!", show_alert=False)
            try:
                await call.message.delete()
            except Exception:
                pass
            return
            
        import json
        payload = json.loads(pending.data_payload)
        initiator = await session.get(User, pending.initiator_id)
        
        if pending.action_type == "create_template":
            tmpl = TaskTemplate(
                house_id=ACTIVE_HOUSE_ID,
                title=payload["title"],
                points=payload["points"],
                periodicity=payload["periodicity"],
                period_days=payload.get("period_days"),
                deleted=False
            )
            session.add(tmpl)
            await session.flush()
            
            # Spawn instance for today as well
            inst = TaskInstance(
                template_id=tmpl.id,
                date=await get_house_today_date(session),
                status="free",
                priority=0
            )
            session.add(inst)
            await session.commit()
            
            await call.answer("✅ Задача успешно добавлена!", show_alert=False)
            await call.message.edit_text(f"✅ Вы одобрили добавление задачи «{tmpl.title}» ({tmpl.points}🍪)!")
            if initiator:
                try:
                    await bot.send_message(
                        chat_id=initiator.telegram_id,
                        text=f"🔔 Партнёр одобрил добавление задачи «{tmpl.title}»! Она добавлена в список."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify initiator: {e}")
                    
        elif pending.action_type == "edit_template":
            tmpl = await session.get(TaskTemplate, payload["template_id"])
            if not tmpl:
                await call.answer("⚠️ Шаблон не найден!", show_alert=False)
                await session.delete(pending)
                await session.commit()
                try:
                    await call.message.delete()
                except Exception:
                    pass
                return
                
            field = payload["field"]
            old_title = tmpl.title
            if field == "title":
                tmpl.title = payload["new_value"]
            elif field == "points":
                tmpl.points = payload["new_value"]
            elif field == "periodicity":
                tmpl.periodicity = payload["periodicity"]
                tmpl.period_days = payload["period_days"]
                
            await session.commit()
            
            await call.answer("✅ Изменения одобрены!", show_alert=False)
            await call.message.edit_text(f"✅ Вы одобрили изменение задачи «{old_title}»!")
            if initiator:
                try:
                    await bot.send_message(
                        chat_id=initiator.telegram_id,
                        text=f"🔔 Партнёр одобрил изменение задачи «{tmpl.title}»!"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify initiator: {e}")
                    
        await session.delete(pending)
        await session.commit()


@dp.callback_query(F.data.startswith("reject_act:"))
async def handle_reject_action(call: types.CallbackQuery, db_user: User = None):
    action_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        pending = await session.get(PendingAction, action_id)
        if not pending:
            await call.answer("⚠️ Запрос не найден или уже обработан!", show_alert=False)
            try:
                await call.message.delete()
            except Exception:
                pass
            return
            
        import json
        payload = json.loads(pending.data_payload)
        initiator = await session.get(User, pending.initiator_id)
        title = payload.get("title")
        if not title and "template_id" in payload:
            tmpl = await session.get(TaskTemplate, payload["template_id"])
            title = tmpl.title if tmpl else "задачи"
            
        await call.answer("❌ Запрос отклонен", show_alert=False)
        await call.message.edit_text(f"❌ Вы отклонили запрос по задаче «{title}».")
        if initiator:
            try:
                await bot.send_message(
                    chat_id=initiator.telegram_id,
                    text=f"🔔 Партнёр отклонил добавление/изменение задачи «{title}»!"
                )
            except Exception as e:
                logger.error(f"Failed to notify initiator: {e}")
                
        await session.delete(pending)
        await session.commit()


async def send_morning_message():
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        await generate_daily_chores_if_needed(session, ACTIVE_HOUSE_ID)
        result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(and_(
                TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                TaskInstance.date <= today,
                TaskInstance.status == "free",
                TaskTemplate.deleted == False
            ))
            .order_by(TaskTemplate.points.desc())
        )
        chores = result.all()
        users = (await session.execute(select(User).where(User.house_id == ACTIVE_HOUSE_ID))).scalars().all()
        
    if not users:
        return
        
    if chores:
        total_cookies = sum(tmpl.points for inst, tmpl in chores)
        text = (
            "🌅 *Доброе утро!*\n\n"
            f"🎯 Сегодня можно залутать *{total_cookies}* 🍪\n\n"
            "Список свободных задач на сегодня:\n"
        )
        for inst, tmpl in chores:
            pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
            text += f"• *{tmpl.title}* (`+{pts_str} 🍪`)\n"
        text += "\n👉 Зайдите в 🏠 *Home*, чтобы взять задачу в работу!"
    else:
        text = "🌅 *Доброе утро!*\n\nНа сегодня свободных домашних дел нет. Отдыхаем! ☕️"
        
    for u in users:
        try:
            await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send morning message to {u.telegram_id}: {e}")
 
 
async def send_14_reminder():
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        users = (await session.execute(select(User).where(User.house_id == ACTIVE_HOUSE_ID))).scalars().all()
        for u in users:
            res = await session.execute(
                select(TaskInstance).where(and_(
                    TaskInstance.done_by_user_id == u.id,
                    TaskInstance.date <= today,
                    TaskInstance.status.in_(["in_progress", "done"])
                ))
            )
            taken = res.scalars().all()
            if not taken:
                text = (
                    "🔔 *Напоминание!*\n\n"
                    "Ты ещё не взял ни одной домашней задачи на сегодня.\n"
                    "Загляни в вкладку 🏠 *Home* и выбери что-нибудь полезное! 🍪"
                )
                try:
                    await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to send 14:00 reminder to {u.telegram_id}: {e}")
 
 
async def send_17_reminder():
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(TaskInstance, TaskTemplate)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(and_(
                TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                TaskInstance.date <= today,
                TaskInstance.status == "free",
                TaskTemplate.deleted == False
            ))
        )
        free_chores = result.all()
        users = (await session.execute(select(User).where(User.house_id == ACTIVE_HOUSE_ID))).scalars().all()
        
    if free_chores and users:
        text = (
            "⚠️ *Внимание!*\n\n"
            "На сегодня ещё остались невыполненные домашние дела!\n"
            "Успейте залутать печеньки 🍪 во вкладке 🏠 *Home*!"
        )
        for u in users:
            try:
                await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send 17:00 reminder to {u.telegram_id}: {e}")


async def send_midnight_summary():
    from zoneinfo import ZoneInfo
    from datetime import timezone as dt_timezone
    async with AsyncSessionLocal() as session:
        house = await session.get(House, ACTIVE_HOUSE_ID)
        tz_str = house.timezone if house else "Europe/Moscow"
        
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)
    summary_date = (now - timedelta(days=1)).date()
    
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User).where(User.house_id == ACTIVE_HOUSE_ID))).scalars().all()
        user_name_map = {u.id: (u.display_name or u.username or "?") for u in users}
        
        result = await session.execute(
            select(Completion, User, TaskTemplate)
            .join(User, Completion.user_id == User.id)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(TaskTemplate.house_id == ACTIVE_HOUSE_ID)
        )
        all_comps = result.all()
        
        comps_today = []
        for comp, usr, tmpl in all_comps:
            utc_dt = comp.created_at.replace(tzinfo=dt_timezone.utc)
            local_dt = utc_dt.astimezone(tz)
            if local_dt.date() == summary_date:
                comps_today.append((comp, usr, tmpl))
                
        pt_result = await session.execute(
            select(PersonalTask).where(and_(
                PersonalTask.user_id.in_([u.id for u in users]),
                PersonalTask.is_completed == True,
                PersonalTask.is_deleted == False,
                PersonalTask.date_execution == summary_date
            ))
        )
        pt_comps = pt_result.scalars().all()
        
    user_chores = {u.id: [] for u in users}
    user_pts = {u.id: 0 for u in users}
    for comp, usr, tmpl in comps_today:
        user_chores[usr.id].append(tmpl.title)
        user_pts[usr.id] += comp.points
        
    user_pts_list = sorted(user_pts.items(), key=lambda x: x[1], reverse=True)
    
    text = (
        f"🌙 *Итоги дня ({summary_date.strftime('%d.%m.%Y')}):*\n\n"
    )
    
    for uid, total_earned in user_pts_list:
        u_name = user_name_map.get(uid, "?")
        text += f"🦸 *{u_name}* заработал *{total_earned}* 🍪\n"
        chores_list = user_chores[uid]
        pts_for_user = [pt for pt in pt_comps if pt.user_id == uid]
        
        if chores_list or pts_for_user:
            text += "Выполненные задачи:\n"
            for ch in chores_list:
                text += f"• {ch} 🏠\n"
            for pt in pts_for_user:
                clean = clean_task_text(pt.text)
                text += f"• {clean} 👤\n"
        else:
            text += "Задач не выполнял.\n"
        text += "\n"
        
    for u in users:
        try:
            await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send midnight summary to {u.telegram_id}: {e}")


async def scheduler_loop():
    from zoneinfo import ZoneInfo
    sent_events = set()
    
    while True:
        try:
            async with AsyncSessionLocal() as session:
                house = await session.get(House, ACTIVE_HOUSE_ID)
                tz_str = house.timezone if house else "Europe/Moscow"
                
            tz = ZoneInfo(tz_str)
            now = datetime.now(tz)
            today = now.date()
            hour = now.hour
            minute = now.minute
            
            if hour == 9 and minute == 0 and (today, "morning") not in sent_events:
                await send_morning_message()
                sent_events.add((today, "morning"))
                
            if hour == 14 and minute == 0 and (today, "reminder_14") not in sent_events:
                await send_14_reminder()
                sent_events.add((today, "reminder_14"))
                
            if hour == 17 and minute == 0 and (today, "reminder_17") not in sent_events:
                await send_17_reminder()
                sent_events.add((today, "reminder_17"))
                
            if hour == 0 and minute == 0 and (today, "midnight_summary") not in sent_events:
                await send_midnight_summary()
                sent_events.add((today, "midnight_summary"))
                
            sent_events = { (d, e) for (d, e) in sent_events if d >= today - timedelta(days=1) }
            
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}", exc_info=True)
            
        await asyncio.sleep(30)


async def get_house_today_date(session: AsyncSession) -> date:
    from zoneinfo import ZoneInfo
    house = await session.get(House, ACTIVE_HOUSE_ID)
    tz_str = house.timezone if (house and house.timezone) else "Europe/Moscow"
    return datetime.now(ZoneInfo(tz_str)).date()


async def generate_daily_chores_if_needed(session, house_id: int):
    """Generate today's task instances from templates and rollover uncompleted ones."""
    from sqlalchemy import update
    today = await get_house_today_date(session)
    weekday = today.weekday()
    day = today.day
    month = today.month

    house = await session.get(House, house_id)
    if not house:
        return

    # Rollover old uncompleted tasks is disabled (we keep their original date)
    # to naturally show them with their original date and yellow circles.

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
        elif p == "every_x_days":
            _, nd = await get_template_next_date_val(session, tmpl, today)
            if nd <= today:
                should_create = True
        elif p == "once":
            _, nd = await get_template_next_date_val(session, tmpl, today)
            if nd <= today:
                should_create = True

        if should_create:
            # Prevent duplication by checking if there is any active instance of this template
            exists = await session.scalar(
                select(TaskInstance).where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.status.in_(["free", "in_progress"])
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
    waiting_for_date = State()
    waiting_for_recurrence = State()
    waiting_for_recurrence_days = State()



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
    
    # Activate reply keyboard
    await message.answer("🤖 Добро пожаловать! Главное меню активировано.", reply_markup=get_main_keyboard())
    
    # Send onboarding page 1
    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Я — твой семейный помощник для управления делами и покупками. 🍪🏠\n\n"
        "Вот как устроен наш функционал:\n\n"
        "🏠 <b>Home (Домашние дела)</b>\n"
        "• Здесь собраны все общие дела по дому на сегодня.\n"
        "• Любой жилец может нажать на задачу, чтобы взять её в работу (она перейдет во вкладку 📋 My).\n"
        "• Внизу есть кнопки:\n"
        "  - <code>➕ Добавить</code> — чтобы внести новую задачу или добавить из базы.\n"
        "  - <code>⚙️ Настройки</code> — управление шаблонами и баллами задач.\n\n"
        "📋 <b>My (Мои дела)</b>\n"
        "• Твоя рабочая зона. Здесь находятся:\n"
        "  - Взятые тобой домашние дела (со смайликом 🏠).\n"
        "  - Твои личные задачи 👤.\n"
        "  - Список покупок 🛒.\n"
        "• Нажми на взятое домашнее дело или личную задачу здесь, чтобы отметить их как выполненные (за общие дела начисляются печеньки 🍪!).\n"
        "• 🟡 Желтый кружок означает просроченные дела с прошлых дней.\n"
        "• 🔴 Красный кружок — срочные задачи.\n"
        "• Кнопка управления:\n"
        "  - <code>Добавить</code> — создать новую личную задачу.\n"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Далее ➡️", callback_data="ob_page:2"))
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("ob_page:"))
async def handle_ob_page(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    name = db_user.display_name if db_user else call.from_user.first_name
    
    if page == 1:
        text = (
            f"👋 Привет, <b>{name}</b>!\n\n"
            "Я помогу навести порядок в домашних и личных делах. 🍪🏠\n\n"
            "Вот как всё устроено:\n\n"
            "🏠 <b>Home (Домашние дела)</b>\n"
            "• Это список общих домашних дел на сегодня.\n"
            "• Любой жилец может нажать на задачу и взять её себе. Она перенесётся во вкладку 📋 My.\n"
            "• Кнопка <code>➕ Добавить</code> позволяет быстро создать новое дело или выбрать задачу из готовой базы.\n"
        )
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="Далее ➡️", callback_data="ob_page:2"))
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        try:
            await call.message.bot.pin_chat_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            logger.error(f"Failed to pin onboarding page 1: {e}")
            
    elif page == 2:
        text = (
            "📋 <b>My (Мои дела)</b>\n"
            "• Твой личный рабочий список на день.\n"
            "• Сюда попадают:\n"
            "  - Взятые тобой домашние дела (со смайликом 🏠).\n"
            "  - Твои личные задачи 👤 (создаются по кнопке <code>➕ Добавить</code> прямо в этой вкладке).\n"
            "  - Список покупок 🛒.\n"
            "• Нажми на выполненное дело здесь, чтобы закрыть его и получить печеньки 🍪!\n"
            "• Просроченные задачи отмечены желтым кружком 🟡, а срочные — красным 🔴.\n"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="⬅️ Назад", callback_data="ob_page:1"),
            InlineKeyboardButton(text="Далее ➡️", callback_data="ob_page:3")
        )
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        try:
            await call.message.bot.pin_chat_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            logger.error(f"Failed to pin onboarding page 2: {e}")
            
    elif page == 3:
        text = (
            "📊 <b>Stat (Магазин и Покупки)</b>\n"
            "• Твой баланс печенек и статистика дома.\n"
            "• 🛍 <b>Магазин:</b> обменивай заработанные печеньки 🍪 на награды! Цены наград рассчитываются автоматически в днях, исходя из вашей активности за последние 30 дней.\n"
            "• 🛒 <b>Покупки:</b> общий список покупок (продукты, бытовая химия и т.д.).\n"
            "• 📜 <b>Архив:</b> история всех выполненных дел по дням.\n\n"
            "Давайте делать дом уютнее вместе! 🎉"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="⬅️ Назад", callback_data="ob_page:2"),
            InlineKeyboardButton(text="Перейти к задачам", callback_data="ob_finish")
        )
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        try:
            await call.message.bot.pin_chat_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            logger.error(f"Failed to pin onboarding page 3: {e}")


@dp.callback_query(F.data == "ob_finish")
async def handle_ob_finish(call: types.CallbackQuery, db_user: User = None):
    try:
        await call.message.bot.unpin_chat_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception as e:
        logger.error(f"Failed to unpin onboarding message: {e}")
        
    try:
        await call.message.delete()
    except Exception as e:
        logger.error(f"Failed to delete onboarding message: {e}")
        
    from bot.handlers.chores import render_household_chores
    await render_household_chores(call.message, db_user, is_callback=False)


@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")



# ── Render Today ───────────────────────────────────────────────────────────────
async def rollover_overdue_tasks(session: AsyncSession, user_id: int):
    """Move overdue uncompleted tasks to today (disabled date shifting to preserve original date)."""
    pass



async def render_today(message: types.Message, db_user: User, is_callback=False, page: int = 0):
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)

        await rollover_overdue_tasks(session, db_user.id)

        # 1. Fetch ALL active personal tasks
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

        # 2. Fetch ALL user's claimed chores in progress
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

    # 3. Find unique dates and upcoming recurring task dates
    all_dates = set()
    for pt in personal_tasks_all:
        all_dates.add(pt.date_execution)
        if pt.recurrence:
            delta = get_recurrence_delta(pt.recurrence)
            # Add next 4 occurrences (limit to 30 days ahead)
            for k in range(1, 5):
                occ_date = pt.date_execution + k * delta
                if occ_date <= today + timedelta(days=30):
                    all_dates.add(occ_date)
                    
    for inst, tmpl in my_chores_all:
        all_dates.add(inst.date)

    # Future dates (strictly > today) sorted ascending
    future_dates = sorted([d for d in all_dates if d > today])
    total_pages = 1 + len(future_dates)

    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    # Filter tasks for the selected page
    personal_tasks = []
    my_chores = []

    def is_pt_occurring_on(pt, target_d):
        if pt.date_execution == target_d:
            return True
        if pt.recurrence and target_d > pt.date_execution:
            delta = get_recurrence_delta(pt.recurrence)
            return (target_d - pt.date_execution).days % delta.days == 0
        return False

    def get_ru_weekday_abbr(d: date) -> str:
        abbrs = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        return abbrs[d.weekday()]

    if page == 0:
        personal_tasks = [pt for pt in personal_tasks_all if pt.date_execution <= today]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date <= today]
        text = "📋 <b>Мои дела на сегодня</b>:\n👉 <i>Нажми на дело для выполнения:</i>"
    else:
        target_date = future_dates[page - 1]
        personal_tasks = [pt for pt in personal_tasks_all if is_pt_occurring_on(pt, target_date)]
        my_chores = [(inst, tmpl) for inst, tmpl in my_chores_all if inst.date == target_date]
        text = "📋 <b>Мои дела</b>:\n👉 <i>Нажми на дело для выполнения:</i>"

    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    
    # Row 2 (Sub-tabs) — no separate Добавить row, it's in pagination

    # Personal tasks rendering    # Personal tasks rendering
    for t in personal_tasks:
        clean = clean_task_text(t.text)
        is_urgent = "🔴" in t.text
        
        t_date = target_date if page > 0 else t.date_execution
        date_str = t_date.strftime('%d.%m.')
        
        # Emoji: 🔴 for urgent, 🔁 for recurring, otherwise 👤
        emoji = "🔴" if is_urgent else ("🔁" if t.recurrence else "👤")
        
        # Circle if overdue/shifted (disabled as per request)
        circle = ""
        
        right_text = f"{date_str} {emoji} ℹ️"
            
        builder.row(
            InlineKeyboardButton(text=clean, callback_data=f"done_task:{t.id}:{page}"),
            InlineKeyboardButton(text=right_text, callback_data=f"pt_info:{t.id}:{page}")
        )

    # Chores rendering
    for inst, tmpl in my_chores:
        pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
        c_date = inst.date
        date_str = c_date.strftime('%d.%m.')
        
        # Circle if overdue (disabled as per request)
        circle = ""
        
        right_text = f"🏠 {date_str} {pts_str}🍪 ℹ️"
            
        builder.row(
            InlineKeyboardButton(text=tmpl.title, callback_data=f"done_chore_inst:{inst.id}:{page}"),
            InlineKeyboardButton(text=right_text, callback_data=f"my_chore_info:{inst.id}:{page}")
        )

    # Pagination row: LEFT=➕Добавить (page 0) or ⏪, MIDDLE=date, RIGHT=⏩ or empty
    target_d = future_dates[page - 1] if page > 0 else today
    date_lbl = f"{target_d.strftime('%d.%m')} ({get_ru_weekday_abbr(target_d)})"
    nav = []
    # Left: Добавить on page 0, back arrow on pages 1+
    if page == 0:
        nav.append(InlineKeyboardButton(text="➕ Добавить", callback_data=f"my_add:{page}"))
    else:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"my_page:{page-1}"))
    # Middle: always date + weekday
    nav.append(InlineKeyboardButton(text=date_lbl, callback_data="noop"))
    # Right: forward arrow if more pages, else empty
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"my_page:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
    builder.row(*nav)

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data.startswith("pt_info:"))
async def handle_pt_info(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    pt_id = int(parts[1])
    page = int(parts[2])
    
    async with AsyncSessionLocal() as session:
        pt = await session.get(PersonalTask, pt_id)
        if not pt:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            return
            
    clean = clean_task_text(pt.text)
    cycle_str = pt.recurrence or "нет"
    dt_str = pt.date_execution.strftime('%d.%m.%Y')
    
    text = (
        f"ℹ️ <b>Информация о задаче:</b>\n\n"
        f"📝 <b>Текст:</b> {clean}\n"
        f"📅 <b>Дата выполнения:</b> {dt_str}\n"
        f"🔁 <b>Цикл:</b> {cycle_str}"
    )
    
    builder = InlineKeyboardBuilder()
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="🗓 Сдвиг", callback_data=f"shift_pt_menu:{pt.id}:{page}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_pt:{pt.id}:{page}")
    )
    
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("my_chore_info:"))
async def handle_my_chore_info(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    inst_id = int(parts[1])
    page = int(parts[2])
    
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, inst_id)
        if not inst:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            return
        tmpl = await session.get(TaskTemplate, inst.template_id)
        if not tmpl:
            await call.answer("⚠️ Шаблон не найден!", show_alert=False)
            return
            
    clean = tmpl.title
    pts_str = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
    cycle_str = get_period_label(tmpl).capitalize()
    dt_str = inst.date.strftime('%d.%m.%Y')
    
    text = (
        f"ℹ️ <b>Информация о деле:</b>\n\n"
        f"🏠 <b>Название:</b> {clean}\n"
        f"🍪 <b>Награда:</b> {pts_str} печенек\n"
        f"📅 <b>Дата выполнения:</b> {dt_str}\n"
        f"🔁 <b>Цикл:</b> {cycle_str}"
    )
    
    builder = InlineKeyboardBuilder()
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    builder.row(
        InlineKeyboardButton(text="🗓 Сдвиг", callback_data=f"shift_chore_menu:{inst.id}:{page}"),
        InlineKeyboardButton(text="🔄 Вернуть", callback_data=f"unclaim_chore_inst:{inst.id}:{page}"),
        InlineKeyboardButton(text="🗑 Копию", callback_data=f"del_chore_inst:{inst.id}:{page}")
    )
    
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


def format_calendar_header(today_date: date) -> str:
    days_full_ru = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
    weekday_idx = (today_date.weekday() + 1) % 7
    return f"📌 *Выберите дату переноса*\n_({days_full_ru[weekday_idx]}, {today_date.strftime('%d.%m.%Y')})_\n\n"


def find_scheduled_date_on_or_after(t: TaskTemplate, search_date: date) -> date:
    p = t.periodicity
    if p == "daily":
        return search_date
    elif p == "weekly":
        target_w = t.weekday if t.weekday is not None else 0
        days_ahead = target_w - search_date.weekday()
        if days_ahead < 0:
            days_ahead += 7
        return search_date + timedelta(days=days_ahead)
    elif p == "twice_weekly":
        # 0 (Mon), 3 (Thu)
        w = search_date.weekday()
        if w <= 0:
            return search_date + timedelta(days=-w)
        elif w <= 3:
            return search_date + timedelta(days=3-w)
        else:
            return search_date + timedelta(days=7-w)
    elif p == "monthly":
        target_d = t.month_day if t.month_day is not None else 1
        d = search_date
        try:
            candidate = date(d.year, d.month, target_d)
        except ValueError:
            candidate = date(d.year, d.month, 28)
        if candidate >= search_date:
            return candidate
        # Next month
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        try:
            return date(y, m, target_d)
        except ValueError:
            return date(y, m, 28)
    elif p == "twice_monthly":
        # 5 and 20
        d = search_date.day
        if d <= 5:
            return date(search_date.year, search_date.month, 5)
        elif d <= 20:
            return date(search_date.year, search_date.month, 20)
        else:
            m = search_date.month + 1
            y = search_date.year
            if m > 12:
                m = 1
                y += 1
            return date(y, m, 5)
    elif p == "quarterly":
        target_d = t.month_day if t.month_day is not None else 1
        target_q_month = t.weekday if t.weekday is not None else 1
        d = search_date
        # Check current month and future months in the current year
        for m_offset in range(12):
            m = ((d.month - 1 + m_offset) % 12) + 1
            y = d.year + ((d.month - 1 + m_offset) // 12)
            pos = ((m - 1) % 3) + 1
            if pos == target_q_month:
                try:
                    candidate = date(y, m, target_d)
                except ValueError:
                    candidate = date(y, m, 28)
                if candidate >= search_date:
                    return candidate
    elif p == "every_x_days":
        # Handled in val calc, default fallback
        return search_date
    return search_date


def get_template_next_date(t: TaskTemplate, last_done_date: date, active_inst_date: date, today_date: date) -> date:
    """Compute the next scheduled execution date for a task template."""
    p = t.periodicity
    if p == "once":
        if last_done_date:
            if t.start_date and t.start_date > last_done_date:
                return t.start_date
            return date(2099, 12, 31)
        else:
            return t.start_date if t.start_date else today_date
        
    if active_inst_date:
        return max(active_inst_date, today_date)
        
    if t.start_date and (last_done_date is None or t.start_date > last_done_date):
        anchor = t.start_date
        search_start = max(anchor, today_date)
    else:
        anchor = last_done_date or t.start_date or (today_date - timedelta(days=30))
        search_start = max(anchor + timedelta(days=1), today_date)
    
    if p == "every_x_days":
        days = t.period_days or 1
        if t.start_date and (last_done_date is None or t.start_date > last_done_date):
            if t.start_date >= today_date:
                return t.start_date
            next_d = t.start_date
            while next_d < today_date:
                next_d += timedelta(days=days)
            return next_d
        elif last_done_date:
            next_d = last_done_date + timedelta(days=days)
            while next_d < today_date:
                next_d += timedelta(days=days)
            return next_d
        else:
            return today_date
            
    nd = find_scheduled_date_on_or_after(t, search_start)
    if t.start_date and nd < t.start_date:
        return t.start_date
    return nd



async def get_template_next_date_val(session: AsyncSession, t: TaskTemplate, today_date: date):
    # Find last done date (for display)
    last_done_result = await session.execute(
        select(sa.func.max(TaskInstance.date))
        .where(and_(TaskInstance.template_id == t.id, TaskInstance.status == "done"))
    )
    last_done = last_done_result.scalar()

    # Find last handled date (treating skipped as done for next date calculation)
    last_handled_result = await session.execute(
        select(sa.func.max(TaskInstance.date))
        .where(and_(TaskInstance.template_id == t.id, TaskInstance.status.in_(["done", "skipped"])))
    )
    last_handled = last_handled_result.scalar()
    
    # Find active today/future instance
    active_inst_result = await session.execute(
        select(sa.func.min(TaskInstance.date))
        .where(
            and_(
                TaskInstance.template_id == t.id,
                TaskInstance.status.in_(["free", "in_progress"])
            )
        )
    )
    active_inst_date = active_inst_result.scalar()

    # If daily generation for today has already run, and no instance exists today,
    # treat today as already handled so the next occurrence starts searching from tomorrow.
    house = await session.get(House, t.house_id) if t.house_id else None
    generation_done = (house.last_summary_date >= today_date) if (house and house.last_summary_date) else False
    if generation_done:
        today_inst_count = await session.scalar(
            select(sa.func.count(TaskInstance.id))
            .where(and_(TaskInstance.template_id == t.id, TaskInstance.date == today_date))
        )
        if today_inst_count == 0:
            last_handled = today_date
    
    nd = get_template_next_date(t, last_handled, active_inst_date, today_date)
    return last_done, nd


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


def create_calendar_keyboard_custom(target_id: int, year: int, month: int, today_date: date, callback_prefix: str) -> InlineKeyboardMarkup:
    kb = []
    # Add navigation bars at the top of the calendar
    if 'pt' in callback_prefix or 'chore' in callback_prefix:
        # My tab context
        kb.append([
            InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
            InlineKeyboardButton(text="⚡📋 My⚡", callback_data="noop"),
            InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
        ])
    else:
        # Home tab context (chores)
        kb.append([
            InlineKeyboardButton(text="⚡🏠 Home⚡", callback_data="noop"),
            InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
            InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
        ])
        kb.append([
            InlineKeyboardButton(text="➕ Добавить", callback_data="chores_add_menu"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="chores_settings")
        ])
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
        
    return InlineKeyboardMarkup(inline_keyboard=kb)



