import logging
from datetime import datetime, timedelta
from aiogram import types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import sqlalchemy as sa
from sqlalchemy import select, and_

from db.models import AsyncSessionLocal, User, Reward, RewardPurchase, TaskTemplate, Completion, TaskInstance, House, PersonalTask
from bot.parser import clean_task_text
from bot.handlers.base import bot, dp, ACTIVE_HOUSE_ID, logger, AddRewardState, get_house_today_date, get_period_label


async def get_activity_count_30_days(session) -> int:
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    
    # 1. Chores
    chores_count = await session.scalar(
        select(sa.func.count(Completion.id))
        .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
        .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
        .where(
            and_(
                TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                Completion.created_at >= thirty_days_ago
            )
        )
    ) or 0
    
    # 2. Personal tasks
    users_result = await session.execute(select(User.id).where(User.house_id == ACTIVE_HOUSE_ID))
    house_user_ids = [u[0] for u in users_result.all()]
    
    personal_count = await session.scalar(
        select(sa.func.count(PersonalTask.id))
        .where(
            and_(
                PersonalTask.user_id.in_(house_user_ids),
                PersonalTask.is_completed == True,
                PersonalTask.completed_at >= thirty_days_ago
            )
        )
    ) or 0
    
    return chores_count + personal_count


async def get_adjusted_price(session, base_price: int) -> int:
    c_30 = await get_activity_count_30_days(session)
    multiplier = 0.5 + (c_30 / 100.0)
    multiplier = max(0.5, min(multiplier, 5.0))
    return max(1, int(round(base_price * multiplier)))



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
        text += f"🦸\u200d♂️ {usr.display_name}: {usr.points or 0} 🍪 (+{weekly} 🍪)\n"

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


@dp.callback_query(F.data == "stats_view")
async def handle_stats_view(call: types.CallbackQuery, db_user: User = None):
    await render_shop_and_purchases(call.message, db_user, is_callback=True)


@dp.callback_query(F.data.startswith("stat_arch:"))
async def handle_stat_arch(call: types.CallbackQuery, db_user: User = None):
    page = int(call.data.split(":")[1])
    from zoneinfo import ZoneInfo
    from datetime import timezone as dt_timezone
    from collections import defaultdict

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

        # Fetch in-progress chores
        ip_result = await session.execute(
            select(TaskInstance, User, TaskTemplate)
            .join(User, TaskInstance.done_by_user_id == User.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskInstance.status == "in_progress"
                )
            )
        )
        ip_chores = ip_result.all()

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
    grouped = defaultdict(list)

    for comp, usr, tmpl in chore_comps:
        utc_dt = comp.created_at.replace(tzinfo=dt_timezone.utc)
        local_dt = utc_dt.astimezone(tz)
        local_date = local_dt.date()
        pts_val = str(comp.points)
        u_name = usr.display_name or usr.username or "?"
        grouped[local_date].append({
            "name": tmpl.title,
            "points": pts_val,
            "user": u_name,
            "time": local_dt.strftime("%H:%M"),
            "is_personal": False,
            "is_in_progress": False,
            "comp_id": comp.id,
            "sort_dt": local_dt.replace(tzinfo=None)
        })

    for inst, usr, tmpl in ip_chores:
        local_date = inst.date
        pts_val = "2-8" if tmpl.title == "Готовка" else str(tmpl.points)
        u_name = usr.display_name or usr.username or "?"
        grouped[local_date].append({
            "name": tmpl.title,
            "points": pts_val,
            "user": u_name,
            "time": "",
            "is_personal": False,
            "is_in_progress": True,
            "inst_id": inst.id,
            "sort_dt": datetime.combine(inst.date, datetime.min.time())
        })

    for pt in pt_comps:
        u_name = user_name_map.get(pt.user_id, "?")
        clean = clean_task_text(pt.text)
        local_date = pt.date_execution
        
        time_str = ""
        sort_dt = datetime.combine(pt.date_execution, datetime.min.time())
        if pt.completed_at:
            utc_dt = pt.completed_at.replace(tzinfo=dt_timezone.utc)
            local_dt = utc_dt.astimezone(tz)
            time_str = local_dt.strftime("%H:%M")
            sort_dt = local_dt.replace(tzinfo=None)
            
        grouped[local_date].append({
            "name": clean,
            "points": "0",
            "user": u_name,
            "time": time_str,
            "is_personal": True,
            "is_in_progress": False,
            "id": pt.id,
            "sort_dt": sort_dt
        })

    unique_dates = sorted(list(grouped.keys()), reverse=True)
    total_days = len(unique_dates)

    if total_days == 0:
        await call.answer("Архив пуст!", show_alert=False)
        return

    if page < 0:
        page = 0
    if page >= total_days:
        page = total_days - 1

    current_date = unique_dates[page]
    day_entries = grouped[current_date]
    day_entries.sort(key=lambda x: x["sort_dt"], reverse=True)

    text = f"📅 *Дата:* {current_date.strftime('%d.%m.%Y')}"
    builder = InlineKeyboardBuilder()
    nav = []
    if page < total_days - 1:
        nav.append(InlineKeyboardButton(text="🟢 ⏪", callback_data=f"stat_arch:{page+1}"))
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏩ 🟢", callback_data=f"stat_arch:{page-1}"))
    if nav:
        builder.row(*nav)

    for e in day_entries:
        if e.get("is_personal"):
            left_text = e['name']
            callback = f"rollback_pt:{e['id']}:{page}"
        else:
            if e.get("is_in_progress"):
                left_text = f"⏳ {e['points']}🍪 {e['name']}"
                callback = f"unclaim_chore_inst:{e['inst_id']}:{page}"
            else:
                left_text = f"{e['points']}🍪 {e['name']}"
                callback = f"rollback_chore:{e['comp_id']}:{page}"
            
        if e['time']:
            right_text = f"{e['user']} {e['time']}"
        else:
            right_text = f"{e['user']}"
            if e.get("is_in_progress"):
                right_text += " (в работе)"
            
        builder.row(
            InlineKeyboardButton(text=left_text, callback_data=callback),
            InlineKeyboardButton(text=right_text, callback_data=callback)
        )

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("rollback_pt:"))
async def handle_rollback_pt(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    pt_id = int(parts[1])
    page = int(parts[2])
    async with AsyncSessionLocal() as session:
        pt = await session.get(PersonalTask, pt_id)
        if pt:
            pt.is_completed = False
            pt.completed_at = None
            today = await get_house_today_date(session)
            pt.date_execution = today
            await session.commit()
            await call.answer(f"🔄 Восстановлено: {clean_task_text(pt.text)}", show_alert=False)
        else:
            await call.answer("⚠️ Задача не найдена!", show_alert=False)
            
    await handle_stat_arch(call, db_user)


@dp.callback_query(F.data.startswith("rollback_chore:"))
async def handle_rollback_chore(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    comp_id = int(parts[1])
    page = int(parts[2])
    async with AsyncSessionLocal() as session:
        comp = await session.get(Completion, comp_id)
        if comp:
            user = await session.get(User, comp.user_id)
            inst = await session.get(TaskInstance, comp.task_instance_id)
            tmpl = await session.get(TaskTemplate, inst.template_id) if inst else None
            
            # Revert points
            if user:
                user.points = max(0, (user.points or 0) - comp.points)
                
            # Revert chore status to in_progress assigned to the user
            if inst:
                inst.status = "in_progress"
                inst.done_by_user_id = comp.user_id
                inst.done_at = None
                
            await session.delete(comp)
            await session.commit()
            
            title = tmpl.title if tmpl else "Домашнее дело"
            await call.answer(f"🔄 Восстановлено в Мои дела: {title}. Списано {comp.points} 🍪", show_alert=False)
        else:
            await call.answer("⚠️ Выполнение не найдено!", show_alert=False)
            
    await handle_stat_arch(call, db_user)



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
            adj = await get_adjusted_price(session, r.price)
            text += f"• *{r.title}* — `{adj} 🍪` (базовая: `{r.price}`)\n"
            builder.button(text=f"❌ {r.title}", callback_data=f"del_reward:{r.id}")
        text += "\n_Нажмите на кнопку с наградой, чтобы удалить её._"
    else:
        text += "Наград пока нет."

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить награду", callback_data="add_reward_start")
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
            adj = await get_adjusted_price(session, r.price)
            builder.row(InlineKeyboardButton(text=f"{r.title} ({adj}🍪)", callback_data=f"buy_reward:{r.id}"))
    else:
        text += "\nНаград пока нет."

    builder.row(
        InlineKeyboardButton(text="⚙️ Управление наградами", callback_data="rewards_settings"),
        InlineKeyboardButton(text="📜 Мои покупки", callback_data="rewards_purchases:0")
    )

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
        adj_price = await get_adjusted_price(session, reward.price)
        if (user.points or 0) < adj_price:
            await call.answer("⚠️ Недостаточно баллов для покупки!", show_alert=False)
            return
        
        user.points -= adj_price
        purchase = RewardPurchase(
            user_id=db_user.id,
            reward_title=reward.title,
            price=adj_price,
            status="purchased"
        )
        session.add(purchase)
        await session.commit()
        await call.answer(f"🎉 Куплено: {reward.title}! Списано {adj_price} 🍪", show_alert=False)
        
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
        nav.append(InlineKeyboardButton(text="🟡 ⏪", callback_data=f"rewards_purchases:{page-1}"))
    if len(rows) == 5:
        nav.append(InlineKeyboardButton(text="⏩ 🟡", callback_data=f"rewards_purchases:{page+1}"))
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
        reply_markup=None,
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
        reply_markup=None,
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


