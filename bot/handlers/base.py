import os
import logging
import asyncio
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, Date, func


import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import AsyncSessionLocal, User, House, PersonalTask, ShoppingItem, TaskTemplate, TaskInstance, Completion, Reward, RewardPurchase, PendingAction
from bot.parser import clean_task_text

logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://example.com"

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
                    
        elif pending.action_type == "delete_template":
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
                
            tmpl.deleted = True
            title = tmpl.title
            await session.commit()
            
            await call.answer("✅ Удаление одобрено!", show_alert=False)
            await call.message.edit_text(f"✅ Вы одобрили удаление задачи «{title}»!")
            if initiator:
                try:
                    await bot.send_message(
                        chat_id=initiator.telegram_id,
                        text=f"🔔 Партнёр одобрил удаление задачи «{title}»!"
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
                msg_text = f"🔔 Партнёр отклонил добавление/изменение задачи «{title}»!"
                if pending.action_type == "delete_template":
                    msg_text = f"🔔 Партнёр отклонил удаление задачи «{title}»!"
                await bot.send_message(
                    chat_id=initiator.telegram_id,
                    text=msg_text
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
        text += "\n👉 Откройте Mini App, чтобы взять задачу в работу!"
    else:
        text = "🌅 *Доброе утро!*\n\nНа сегодня свободных домашних дел нет. Отдыхаем! ☕️"
        
    app_url = MINI_APP_URL
    if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
        app_url = app_url.rstrip("/") + "/app"
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 Открыть App",
            web_app=WebAppInfo(url=app_url)
        )
    )
    markup = builder.as_markup()
        
    for u in users:
        try:
            await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            logger.error(f"Failed to send morning message to {u.telegram_id}: {e}")
 
 
# send_14_reminder and send_17_reminder have been disabled as per optimization settings


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
        
    app_url = MINI_APP_URL
    if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
        app_url = app_url.rstrip("/") + "/app"
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 Открыть App",
            web_app=WebAppInfo(url=app_url)
        )
    )
    markup = builder.as_markup()
        
    for u in users:
        try:
            await bot.send_message(chat_id=u.telegram_id, text=text, parse_mode="Markdown", reply_markup=markup)
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
    """Generate today's task instances from templates. Uses atomic UPDATE to prevent race conditions."""
    from sqlalchemy import update
    today = await get_house_today_date(session)
    weekday = today.weekday()
    day = today.day
    month = today.month

    house = await session.get(House, house_id)
    if not house:
        return

    # Atomic claim: only one concurrent request can proceed to generate.
    # UPDATE returns rows only if last_summary_date != today, preventing race conditions.
    result = await session.execute(
        update(House)
        .where(and_(House.id == house_id, House.last_summary_date != today))
        .values(last_summary_date=today)
        .returning(House.id)
    )
    await session.flush()  # flush immediately so the lock takes effect
    claimed = result.fetchone()
    if not claimed:
        # Another request already claimed generation for today — skip
        return

    # Rollover old uncompleted household task instances to today
    await session.execute(
        update(TaskInstance)
        .where(and_(
            TaskInstance.date < today,
            TaskInstance.status.in_(["free", "in_progress"])
        ))
        .values(date=today)
    )

    # Rollover old uncompleted personal tasks to today
    await session.execute(
        update(PersonalTask)
        .where(and_(
            PersonalTask.date_execution < today,
            PersonalTask.is_completed == False,
            PersonalTask.is_deleted == False
        ))
        .values(date_execution=today)
    )

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
            # Final safety check: no instance already exists for today
            exists = await session.scalar(
                select(TaskInstance.id).where(
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

    house.last_summary_date = today
    await session.commit()
    logger.info(f"Generated daily chores for house {house_id}")


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
                    if tg_id not in ALLOWED_TELEGRAM_IDS:
                        logger.warning(f"Registration request from unauthorized user ID: {tg_id}")
                        return await handler(event, data)
                    user_info = ALLOWED_TELEGRAM_IDS.get(tg_id)
                    display_name = user_info[0]
                    is_owner = user_info[1]
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





# ── /start ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, db_user: User = None):
    # Set chat menu button for Web App dynamically
    import os
    from aiogram.types import WebAppInfo, MenuButtonWebApp
    app_url = os.getenv("MINI_APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://example.com"
    if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
        app_url = app_url.rstrip("/") + "/app"
    try:
        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="📱 Открыть App",
                web_app=WebAppInfo(url=app_url)
            )
        )
    except Exception as e:
        logger.error(f"Failed to set chat menu button: {e}")

    # Remove reply keyboard quietly (optional, disabled to prevent flash)
    
    text = (
        "👋 Привет!\n\n"
        "Все функции управления домашними делами, личными задачами, покупками и наградами теперь доступны только в <b>Mini App</b>! 📱\n\n"
        "В чате бота вы будете получать только:\n"
        "• Утренний список дел (в 9:00) 🌅\n"
        "• Итоги дня (в полночь) 🌙\n"
        "• Запросы на одобрение изменений и удалений от вашего партнера 🔔\n\n"
        "Нажмите на кнопку ниже, чтобы открыть приложение и приступить к работе!"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 Открыть App",
            web_app=WebAppInfo(url=app_url)
        )
    )
    sent_msg = await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")





@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")



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
        select(func.max(TaskInstance.date))
        .where(and_(TaskInstance.template_id == t.id, TaskInstance.status == "done"))
    )
    last_done = last_done_result.scalar()

    # Find last handled date (treating skipped as done for next date calculation)
    last_handled_result = await session.execute(
        select(func.max(TaskInstance.date))
        .where(and_(TaskInstance.template_id == t.id, TaskInstance.status.in_(["done", "skipped"])))
    )
    last_handled = last_handled_result.scalar()
    
    # Find active today/future instance
    active_inst_result = await session.execute(
        select(func.min(TaskInstance.date))
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
            select(func.count(TaskInstance.id))
            .where(and_(TaskInstance.template_id == t.id, TaskInstance.date == today_date))
        )
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





# ── Catch-All Handlers to Redirect to Mini App ─────────────────────────────────

@dp.callback_query()
async def catch_all_callbacks(call: types.CallbackQuery):
    await call.answer(
        "⚠️ Данное действие доступно только в Mini App!\nПожалуйста, используйте кнопку «Открыть App» внизу.",
        show_alert=True
    )


@dp.message()
async def catch_all_messages(message: types.Message):
    import os
    from aiogram.types import WebAppInfo
    app_url = os.getenv("MINI_APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://example.com"
    if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
        app_url = app_url.rstrip("/") + "/app"
        
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 Открыть App",
            web_app=WebAppInfo(url=app_url)
        )
    )
    await message.answer(
        "🤖 Все функции управления домашними делами, личными задачами, покупками и наградами доступны только в <b>Mini App</b>!\n\n"
        "Пожалуйста, откройте приложение с помощью кнопки ниже.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )



