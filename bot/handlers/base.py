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
            await call.message.edit_text(f"✅ Вы одобрили добавление задачи «{tmpl.title}» ({tmpl.points}✨)!")
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
                
            old_title = tmpl.title
            tmpl.title = payload.get("title", tmpl.title)
            tmpl.points = payload.get("points", tmpl.points)
            tmpl.periodicity = payload.get("periodicity", tmpl.periodicity)
            tmpl.period_days = payload.get("period_days", tmpl.period_days)
            if "start_date" in payload and payload["start_date"]:
                try:
                    tmpl.start_date = datetime.strptime(payload["start_date"], "%Y-%m-%d").date()
                except Exception:
                    pass
                
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

        try:
            from api.routes import manager
            import asyncio
            asyncio.create_task(manager.broadcast_refresh(ACTIVE_HOUSE_ID))
        except Exception as e:
            logger.error(f"Failed to broadcast websocket refresh from bot approve: {e}")


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

        try:
            from api.routes import manager
            import asyncio
            asyncio.create_task(manager.broadcast_refresh(ACTIVE_HOUSE_ID))
        except Exception as e:
            logger.error(f"Failed to broadcast websocket refresh from bot reject: {e}")


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
        text = (
            "🌅 *Доброе утро!*\n\n"
            "Новый день начался, задачи уже ждут в приложении. Выполняй дела и отдыхай! ☕️\n\n"
            "👉 Откройте Mini App, чтобы посмотреть список задач!"
        )
    else:
        text = "🌅 *Доброе утро!*\n\nНа сегодня свободных домашних дел нет. Отдыхаем! ☕️"
        
    app_url = MINI_APP_URL
    if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
        app_url = app_url.rstrip("/") + "/app"
    if app_url:
        app_url = app_url.rstrip("/") + "/?v=28"
        
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
        text += f"🦸 *{u_name}* заработал *{total_earned}* искр ✨\n"
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
    if app_url:
        app_url = app_url.rstrip("/") + "/?v=28"
        
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
    import time
    sent_events = set()
    last_ping_time = 0
    
    while True:
        try:
            # Self-ping keep-alive to prevent Render sleeping
            current_time = time.time()
            if current_time - last_ping_time >= 300:  # 5 minutes
                last_ping_time = current_time
                external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("MINI_APP_URL")
                if external_url:
                    ping_url = external_url.rstrip("/") + "/health"
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as client:
                            async with client.get(ping_url, timeout=10) as resp:
                                logger.info(f"Self-ping keep-alive to {ping_url} returned status: {resp.status}")
                    except Exception as ping_err:
                        logger.warning(f"Self-ping keep-alive failed: {ping_err}")

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


_last_generation_check = None
_house_timezone_cache = {}

async def get_house_today_date(session: AsyncSession) -> date:
    from zoneinfo import ZoneInfo
    global _house_timezone_cache
    tz_str = _house_timezone_cache.get(ACTIVE_HOUSE_ID)
    if not tz_str:
        house = await session.get(House, ACTIVE_HOUSE_ID)
        tz_str = house.timezone if (house and house.timezone) else "Europe/Moscow"
        _house_timezone_cache[ACTIVE_HOUSE_ID] = tz_str
    return datetime.now(ZoneInfo(tz_str)).date()


async def generate_daily_chores_if_needed(session, house_id: int):
    """Generate calendar week's task instances from templates. Uses atomic UPDATE to prevent race conditions."""
    from sqlalchemy import update
    global _last_generation_check
    today = await get_house_today_date(session)
    
    # 1. In-memory check to bypass DB hits if already checked today
    if _last_generation_check == (house_id, today):
        return

    house = await session.get(House, house_id)
    if not house:
        return

    # Atomic claim: only one concurrent request can proceed to generate.
    result = await session.execute(
        update(House)
        .where(and_(House.id == house_id, House.last_summary_date != today))
        .values(last_summary_date=today)
        .returning(House.id)
    )
    await session.flush()  # flush immediately so the lock takes effect
    claimed = result.fetchone()
    if not claimed:
        _last_generation_check = (house_id, today)
        return

    _last_generation_check = (house_id, today)

    # 2. Rollover old uncompleted household task instances
    old_instances = (await session.execute(
        select(TaskInstance).where(and_(
            TaskInstance.date < today,
            TaskInstance.status.in_(["free", "in_progress"])
        ))
    )).scalars().all()

    for inst in old_instances:
        tmpl = await session.get(TaskTemplate, inst.template_id)
        if not tmpl:
            await session.delete(inst)
            continue

        is_daily = (tmpl.periodicity == "daily") or (tmpl.periodicity == "every_x_days" and tmpl.period_days == 1)
        if is_daily:
            # yesterday's daily task is skipped (status set to skipped, which excludes it from target points)
            inst.status = "skipped"
        else:
            # check if a copy exists today
            exists_today = await session.scalar(
                select(TaskInstance).where(and_(
                    TaskInstance.template_id == tmpl.id,
                    TaskInstance.date == today
                ))
            )
            if not exists_today:
                # rollover to today
                diff_days = (today - inst.date).days
                if diff_days > 0:
                    old_date = inst.date
                    inst.date = today
                    if inst.status == "in_progress":
                        # return to general list
                        inst.status = "free"
                        inst.done_by_user_id = None
                        inst.done_at = None
                    
                    # Also shift all future uncompleted instances of this template
                    await session.execute(
                        update(TaskInstance)
                        .where(and_(
                            TaskInstance.template_id == tmpl.id,
                            TaskInstance.date > old_date,
                            TaskInstance.status.in_(["free", "in_progress"])
                        ))
                        .values(date=TaskInstance.date + timedelta(days=diff_days))
                    )
            else:
                # delete yesterday's duplicate copy
                await session.delete(inst)

    await session.flush()

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

    # 3. Pre-generate tasks for the entire current calendar week (Monday to Sunday)
    monday_date = today - timedelta(days=today.weekday())
    sunday_date = monday_date + timedelta(days=6)

    week_already_generated = False
    if house.last_summary_date:
        last_monday = house.last_summary_date - timedelta(days=house.last_summary_date.weekday())
        if last_monday == monday_date:
            week_already_generated = True

    templates = (await session.execute(
        select(TaskTemplate).where(
            and_(
                TaskTemplate.house_id == house_id,
                TaskTemplate.deleted == False
            )
        )
    )).scalars().all()

    for tmpl in templates:
        if week_already_generated:
            # Check if this template already has any instances generated for this week (including done, skipped or shifted)
            has_instances = await session.scalar(
                select(TaskInstance.id)
                .where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.date.between(monday_date, sunday_date)
                    )
                )
                .limit(1)
            )
            if has_instances:
                continue

        p = tmpl.periodicity
        days = tmpl.period_days or 1
        
        scheduled_dates = []
        
        if p == "daily" or (p == "every_x_days" and days == 1):
            for d_idx in range(7):
                scheduled_dates.append(monday_date + timedelta(days=d_idx))
        elif p == "weekly":
            weekday = tmpl.weekday if tmpl.weekday is not None else 0
            scheduled_dates.append(monday_date + timedelta(days=weekday))
        elif p == "twice_weekly":
            scheduled_dates.append(monday_date + timedelta(days=0))
            scheduled_dates.append(monday_date + timedelta(days=3))
        elif p == "monthly":
            target_day = tmpl.month_day if tmpl.month_day is not None else 1
            for d_idx in range(7):
                curr_d = monday_date + timedelta(days=d_idx)
                if curr_d.day == target_day:
                    scheduled_dates.append(curr_d)
        elif p == "twice_monthly":
            for d_idx in range(7):
                curr_d = monday_date + timedelta(days=d_idx)
                if curr_d.day in (5, 20):
                    scheduled_dates.append(curr_d)
        elif p == "quarterly":
            target_day = tmpl.month_day if tmpl.month_day is not None else 1
            target_q_month = tmpl.weekday if tmpl.weekday is not None else 1
            for d_idx in range(7):
                curr_d = monday_date + timedelta(days=d_idx)
                if curr_d.day == target_day:
                    pos = ((curr_d.month - 1) % 3) + 1
                    if pos == target_q_month:
                        scheduled_dates.append(curr_d)
        elif p == "every_x_days":
            anchor = tmpl.start_date or monday_date
            if anchor < monday_date:
                diff = (monday_date - anchor).days
                k = (diff + days - 1) // days
                first_occ = anchor + timedelta(days=k * days)
            else:
                first_occ = anchor
                
            curr_occ = first_occ
            while curr_occ <= sunday_date:
                if curr_occ >= monday_date:
                    scheduled_dates.append(curr_occ)
                curr_occ += timedelta(days=days)
        elif p == "once":
            anchor = tmpl.start_date or monday_date
            if monday_date <= anchor <= sunday_date:
                scheduled_dates.append(anchor)

        # Insert instances for scheduled dates if they do not exist
        for s_date in scheduled_dates:
            if tmpl.start_date and s_date < tmpl.start_date:
                continue
            
            exists = await session.scalar(
                select(TaskInstance.id).where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.date == s_date
                    )
                )
            )
            if not exists:
                inst = TaskInstance(
                    template_id=tmpl.id,
                    date=s_date,
                    status="free",
                    priority=0
                )
                session.add(inst)

    house.last_summary_date = today
    await session.commit()
    logger.info(f"Generated calendar week chores for house {house_id}")


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
    if app_url:
        app_url = app_url.rstrip("/") + "/?v=28"
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


@dp.message(Command("raschet"))
async def cmd_raschet(message: types.Message):
    from bot.handlers.base import get_house_today_date, calculate_weekly_target_points
    import zoneinfo
    from datetime import date, datetime, timedelta, timezone
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        monday_date = today - timedelta(days=today.weekday())
        
        total_weekly_target_points, templates_detail = await calculate_weekly_target_points(session, ACTIVE_HOUSE_ID, today)
        
        members = (await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )).scalars().all()
        
        sorted_members = sorted(members, key=lambda x: x.id)
        num_members = len(sorted_members) or 1
        
        # Calculate current weekly earned points for each member
        msk_tz = zoneinfo.ZoneInfo("Europe/Moscow")
        start_msk = datetime.combine(monday_date, datetime.min.time()).replace(tzinfo=msk_tz)
        start_utc = start_msk.astimezone(timezone.utc).replace(tzinfo=None)
        end_sunday = monday_date + timedelta(days=6)
        end_msk = datetime.combine(end_sunday, datetime.max.time()).replace(tzinfo=msk_tz)
        end_utc = end_msk.astimezone(timezone.utc).replace(tzinfo=None)
        
        members_data = []
        target_info = []
        for index, m in enumerate(sorted_members):
            earned = await session.scalar(
                select(func.sum(Completion.points))
                .where(and_(
                    Completion.user_id == m.id,
                    Completion.created_at >= start_utc,
                    Completion.created_at <= end_utc
                ))
            ) or 0
            
            # Split target 2/3 for participant 1, 1/3 for participant 2
            if len(sorted_members) >= 2:
                member_target = int(total_weekly_target_points * 2 / 3) if index == 0 else int(total_weekly_target_points * 1 / 3)
            else:
                member_target = total_weekly_target_points
            
            if member_target < 1:
                member_target = 1
                
            members_data.append(f"• <b>{m.display_name or m.username}</b>: {earned}/{member_target} ✨")
            target_info.append(f"<b>{m.display_name or m.username}</b>: {member_target} ✨")
            
        # Format list of planned chores
        chores_list = []
        for t in templates_detail:
            chores_list.append(f"• <b>{t['title']}</b> ({t['occurrences']} раз/нед) — +{t['total']} ✨")
            
        text = (
            "📊 <b>Расчет целей на неделю:</b>\n\n"
            f"Сумма очков всех задач дома: <b>{total_weekly_target_points}</b> ✨\n"
            f"Цели участников: " + ", ".join(target_info) + "\n\n"
            "<b>Текущий прогресс:</b>\n"
            + "\n".join(members_data) + "\n\n"
            "<b>Запланированные задачи на неделю:</b>\n"
            + "\n".join(chores_list)
        )
        await message.answer(text, parse_mode="HTML")


@dp.message(Command("logs"))
async def cmd_logs(message: types.Message):
    from bot.handlers.base import ALLOWED_TELEGRAM_IDS
    if message.from_user.id not in ALLOWED_TELEGRAM_IDS:
        await message.answer("Access denied.")
        return
        
    async with AsyncSessionLocal() as session:
        try:
            from sqlalchemy import text
            logs_res = await session.execute(text("""
                SELECT path, method, duration_ms, created_at 
                FROM request_logs 
                ORDER BY id DESC 
                LIMIT 15
            """))
            recent_logs = logs_res.all()
            
            avg_res = await session.execute(text("SELECT AVG(duration_ms), COUNT(*) FROM request_logs"))
            avg_row = avg_res.fetchone()
            avg_duration = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0
            total_logs_count = int(avg_row[1]) if avg_row and avg_row[1] is not None else 0
            
            text_out = "📊 <b>Логи времени ответа (база/сервер):</b>\n\n"
            text_out += f"Всего записано запросов: <b>{total_logs_count}</b>\n"
            text_out += f"Среднее время ответа: <b>{avg_duration:.1f} мс</b>\n\n"
            text_out += "<b>Последние 15 запросов:</b>\n"
            
            for row in recent_logs:
                time_str = row[3].strftime("%H:%M:%S") if row[3] else ""
                text_out += f"• <code>[{time_str}]</code> <b>{row[1]}</b> {row[0]} — <b>{row[2]} мс</b>\n"
                
            await message.answer(text_out, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"⚠️ Ошибка при чтении логов из базы: {e}")


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    from bot.handlers.base import ALLOWED_TELEGRAM_IDS
    if message.from_user.id not in ALLOWED_TELEGRAM_IDS:
        await message.answer("Access denied.")
        return
        
    async with AsyncSessionLocal() as session:
        try:
            from sqlalchemy import update
            from datetime import date, timedelta
            from db.models import House
            import bot.handlers.base
            
            yesterday = date.today() - timedelta(days=1)
            await session.execute(
                update(House)
                .where(House.id == 81)
                .values(last_summary_date=yesterday)
            )
            await session.commit()
            
            bot.handlers.base._last_generation_check = None
            
            await message.answer(f"✅ <b>Сброс выполнен успешно!</b>\n\n<code>last_summary_date</code> для дома 81 установлен на {yesterday}. При следующем обновлении Mini App задачи на всю неделю сгенерируются заново.", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"⚠️ Ошибка сброса: {e}")



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
        
        def get_clamped_date(y, m, day):
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            return date(y, m, min(day, last_day))

        candidate = get_clamped_date(d.year, d.month, target_d)
        if candidate >= search_date:
            return candidate
        # Next month
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        return get_clamped_date(y, m, target_d)
    elif p == "twice_monthly":
        # 5 and 20
        d = search_date
        if d.day <= 5:
            return date(d.year, d.month, 5)
        elif d.day <= 20:
            return date(d.year, d.month, 20)
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
        
        def get_clamped_date(y, m, day):
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            return date(y, m, min(day, last_day))

        # Check current month and future months in the current year
        for m_offset in range(12):
            m = ((d.month - 1 + m_offset) % 12) + 1
            y = d.year + ((d.month - 1 + m_offset) // 12)
            pos = ((m - 1) % 3) + 1
            if pos == target_q_month:
                candidate = get_clamped_date(y, m, target_d)
                if candidate >= search_date:
                    return candidate
    elif p == "every_x_days":
        days = t.period_days or 1
        anchor = t.start_date or search_date
        if search_date <= anchor:
            return anchor
        diff = (search_date - anchor).days
        k = (diff + days - 1) // days
        return anchor + timedelta(days=k * days)
    elif p == "once":
        anchor = t.start_date or search_date
        if search_date <= anchor:
            return anchor
        return date(2099, 12, 31)
        
    return search_date


def find_scheduled_date_before_or_on(t: TaskTemplate, search_date: date) -> date:
    p = t.periodicity
    if p == "daily":
        return search_date
    elif p == "weekly":
        target_w = t.weekday if t.weekday is not None else 0
        days_behind = search_date.weekday() - target_w
        if days_behind < 0:
            days_behind += 7
        return search_date - timedelta(days=days_behind)
    elif p == "twice_weekly":
        w = search_date.weekday()
        if w == 0:
            return search_date
        elif w < 3:
            return search_date - timedelta(days=w)
        elif w == 3:
            return search_date
        else:
            return search_date - timedelta(days=w - 3)
    elif p == "monthly":
        target_d = t.month_day if t.month_day is not None else 1
        d = search_date
        
        def get_clamped_date(y, m, day):
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            return date(y, m, min(day, last_day))

        candidate = get_clamped_date(d.year, d.month, target_d)
        if candidate <= search_date:
            return candidate
        # Previous month
        m = d.month - 1
        y = d.year
        if m < 1:
            m = 12
            y -= 1
        return get_clamped_date(y, m, target_d)
    elif p == "twice_monthly":
        d = search_date
        if d.day >= 20:
            return date(d.year, d.month, 20)
        elif d.day >= 5:
            return date(d.year, d.month, 5)
        else:
            m = d.month - 1
            y = d.year
            if m < 1:
                m = 12
                y -= 1
            return date(y, m, 20)
    elif p == "quarterly":
        target_d = t.month_day if t.month_day is not None else 1
        target_q_month = t.weekday if t.weekday is not None else 1
        d = search_date
        
        def get_clamped_date(y, m, day):
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            return date(y, m, min(day, last_day))

        # Check current month and past months
        for m_offset in range(12):
            m = d.month - m_offset
            y = d.year
            while m < 1:
                m += 12
                y -= 1
            pos = ((m - 1) % 3) + 1
            if pos == target_q_month:
                candidate = get_clamped_date(y, m, target_d)
                if candidate <= search_date:
                    return candidate
    elif p == "every_x_days":
        days = t.period_days or 1
        anchor = t.start_date or search_date
        if search_date < anchor:
            return anchor
        diff = (search_date - anchor).days
        k = diff // days
        return anchor + timedelta(days=k * days)
    elif p == "once":
        anchor = t.start_date or search_date
        if search_date >= anchor:
            return anchor
        return anchor
        
    return search_date


def get_template_next_date(t: TaskTemplate, last_done_date: date, active_inst_date: date, today_date: date) -> date:
    """Compute the next scheduled execution date for a task template."""
    p = t.periodicity
    if p == "once":
        if last_done_date:
            return date(2099, 12, 31)
        return t.start_date or today_date

    ref_date = active_inst_date or last_done_date or t.start_date or (today_date - timedelta(days=1))
    
    # For find_scheduled_date_on_or_after, we search starting from ref_date + 1 day
    search_start = ref_date + timedelta(days=1)
    
    # If the task is every_x_days, it shifts relative to ref_date
    if p == "every_x_days":
        days = t.period_days or 1
        return ref_date + timedelta(days=days)
        
    return find_scheduled_date_on_or_after(t, search_start)



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
    
    real_today = await get_house_today_date(session)
    
    if today_date > real_today:
        # Assume any active task instances scheduled before today_date (and on or after real_today)
        # will be completed on their scheduled date.
        max_active_before = await session.scalar(
            select(func.max(TaskInstance.date))
            .where(and_(
                TaskInstance.template_id == t.id,
                TaskInstance.status.in_(["free", "in_progress"]),
                TaskInstance.date < today_date,
                TaskInstance.date >= real_today
            ))
        )
        if max_active_before:
            last_handled = max(last_handled or date.min, max_active_before)
            
        # Only look at active instances on or after today_date
        active_inst_result = await session.execute(
            select(func.min(TaskInstance.date))
            .where(
                and_(
                    TaskInstance.template_id == t.id,
                    TaskInstance.status.in_(["free", "in_progress"]),
                    TaskInstance.date >= today_date
                )
            )
        )
    else:
        # Standard query
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

    # Compute next execution date based on true last handled and active dates
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


async def calculate_weekly_target_points(session: AsyncSession, house_id: int, today: date) -> tuple[int, list[dict]]:
    from bot.handlers.base import get_template_next_date, get_house_today_date
    from datetime import timedelta
    
    real_today = await get_house_today_date(session)
    monday_date = today - timedelta(days=today.weekday())
    sunday_date = monday_date + timedelta(days=6)
    
    # Bulk pre-fetch all handled/done dates strictly before this week to avoid target changes on completion
    last_done_result = await session.execute(
        select(TaskInstance.template_id, func.max(TaskInstance.date))
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(and_(TaskTemplate.house_id == house_id, TaskInstance.status == "done", TaskInstance.date < monday_date))
        .group_by(TaskInstance.template_id)
    )
    last_done_map = {row[0]: row[1] for row in last_done_result.all()}
    
    last_handled_result = await session.execute(
        select(TaskInstance.template_id, func.max(TaskInstance.date))
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(and_(TaskTemplate.house_id == house_id, TaskInstance.status.in_(["done", "skipped"]), TaskInstance.date < monday_date))
        .group_by(TaskInstance.template_id)
    )
    last_handled_map = {row[0]: row[1] for row in last_handled_result.all()}
    
    active_inst_result = await session.execute(
        select(TaskInstance.template_id, func.min(TaskInstance.date))
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(and_(TaskTemplate.house_id == house_id, TaskInstance.status.in_(["free", "in_progress"]), TaskInstance.date < monday_date))
        .group_by(TaskInstance.template_id)
    )
    active_inst_map = {row[0]: row[1] for row in active_inst_result.all()}
    
    house = await session.get(House, house_id)
    
    today_inst_result = await session.execute(
        select(TaskInstance.template_id, func.count(TaskInstance.id))
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(and_(TaskTemplate.house_id == house_id, TaskInstance.date == monday_date))
        .group_by(TaskInstance.template_id)
    )
    today_inst_map = {row[0]: row[1] for row in today_inst_result.all()}
    
    def get_next_date_local(tmpl, base_date):
        l_done = last_done_map.get(tmpl.id)
        l_handled = last_handled_map.get(tmpl.id)
        act_inst = active_inst_map.get(tmpl.id)
        next_occ = get_template_next_date(tmpl, l_handled, act_inst, base_date)
        
        if tmpl.periodicity == "every_x_days" and next_occ < base_date:
            days = tmpl.period_days or 1
            diff = (base_date - next_occ).days
            k = (diff + days - 1) // days
            next_occ += timedelta(days=k * days)
            
        return next_occ
        
    templates = (await session.execute(
        select(TaskTemplate).where(
            and_(
                TaskTemplate.house_id == house_id,
                TaskTemplate.deleted == False
            )
        )
    )).scalars().all()
    # Bulk query all task instances for this week, including completions relationship
    from sqlalchemy.orm import selectinload
    week_insts_result = await session.execute(
        select(TaskInstance)
        .options(selectinload(TaskInstance.completions))
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(and_(
            TaskTemplate.house_id == house_id,
            TaskInstance.date >= monday_date,
            TaskInstance.date <= sunday_date
        ))
    )
    week_instances = week_insts_result.scalars().all()
    
    # Map: template_id -> date -> list of TaskInstance
    inst_by_tmpl_and_date = {}
    for inst in week_instances:
        inst_by_tmpl_and_date.setdefault(inst.template_id, {}).setdefault(inst.date, []).append(inst)
    
    total_weekly_target_points = 0
    templates_detail = []
    
    for tmpl in templates:
        tmpl_points_sum = 0
        occurrences = 0
        p = tmpl.periodicity
        
        next_occ = get_next_date_local(tmpl, monday_date)
        tmpl_insts_by_date = inst_by_tmpl_and_date.get(tmpl.id, {})
        
        done_dates = []
        planned_dates = []
        
        for d_idx in range(7):
            curr_d = monday_date + timedelta(days=d_idx)
            if tmpl.start_date and curr_d < tmpl.start_date:
                continue
                
            # If the period is every_x_days or once, track the schedule pointer
            is_next_occ_day = (curr_d == next_occ)
            
            # Check if there are instances in DB for this day
            day_insts = tmpl_insts_by_date.get(curr_d, [])
            if day_insts:
                # Count how many are active/done
                for inst in day_insts:
                    if inst.status == "done":
                        occurrences += 1
                        done_dates.append(curr_d.strftime("%d.%m"))
                        actual_pts = sum(c.points for c in inst.completions) if inst.completions else tmpl.points
                        tmpl_points_sum += actual_pts
                    elif inst.status in ["free", "in_progress"]:
                        # Only count uncompleted tasks if the date is today or in the future
                        if curr_d >= real_today:
                            occurrences += 1
                            planned_dates.append(curr_d.strftime("%d.%m"))
                            tmpl_points_sum += tmpl.points
                # Still advance next_occ if today was the scheduled date
                if is_next_occ_day:
                    if p == "every_x_days":
                        next_occ += timedelta(days=tmpl.period_days or 1)
                    elif p == "once":
                        next_occ = date(2099, 12, 31)
            else:
                # No instances in DB. Determine if we should simulate
                gen_done = (house.last_summary_date >= curr_d) if (house and house.last_summary_date) else False
                should_run = False
                if curr_d > real_today or (curr_d == real_today and not gen_done):
                    if p == "daily":
                        should_run = True
                    elif p == "weekly":
                        should_run = (curr_d.weekday() == (tmpl.weekday if tmpl.weekday is not None else 0))
                    elif p == "twice_weekly":
                        should_run = (curr_d.weekday() in (0, 3))
                    elif p == "monthly":
                        should_run = (curr_d.day == (tmpl.month_day if tmpl.month_day is not None else 1))
                    elif p == "twice_monthly":
                        should_run = (curr_d.day in (5, 20))
                    elif p == "quarterly":
                        if curr_d.day == (tmpl.month_day if tmpl.month_day is not None else 1):
                            pos = ((curr_d.month - 1) % 3) + 1
                            if pos == (tmpl.weekday if tmpl.weekday is not None else 1):
                                should_run = True
                    elif p == "every_x_days":
                        if is_next_occ_day:
                            should_run = True
                    elif p == "once":
                        if is_next_occ_day:
                            should_run = True
                            
                    if should_run:
                        occurrences += 1
                        planned_dates.append(curr_d.strftime("%d.%m"))
                        tmpl_points_sum += tmpl.points
                        
                # Always advance next_occ for schedule tracking even if we didn't simulate the day
                if is_next_occ_day:
                    if p == "every_x_days":
                        next_occ += timedelta(days=tmpl.period_days or 1)
                    elif p == "once":
                        next_occ = date(2099, 12, 31)
                        
        if occurrences > 0:
            total_weekly_target_points += tmpl_points_sum
            templates_detail.append({
                "title": tmpl.title,
                "periodicity": tmpl.periodicity,
                "points": tmpl.points,
                "occurrences": occurrences,
                "total": tmpl_points_sum,
                "done_dates": done_dates,
                "planned_dates": planned_dates
            })
            
    return total_weekly_target_points, templates_detail


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
    if app_url:
        app_url = app_url.rstrip("/") + "/?v=28"
        
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



