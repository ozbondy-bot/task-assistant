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


async def get_adjusted_price(session, base_days: int) -> int:
    from datetime import datetime, timedelta
    from db.models import Completion, User
    from sqlalchemy import select, func, and_
    
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    
    # Sum of all completed task points in the last 30 days
    total_points = await session.scalar(
        select(func.sum(Completion.points))
        .join(User, Completion.user_id == User.id)
        .where(
            and_(
                User.house_id == ACTIVE_HOUSE_ID,
                Completion.created_at >= thirty_days_ago
            )
        )
    ) or 0
    
    # Count of active house users
    num_users = await session.scalar(
        select(func.count(User.id)).where(User.house_id == ACTIVE_HOUSE_ID)
    ) or 1
    if num_users == 0:
        num_users = 1
        
    # Calculate points per user per day over 30 days (default floor 15.0)
    points_per_day = total_points / (30.0 * num_users)
    points_per_day = max(15.0, points_per_day)
    
    price_cookies = base_days * points_per_day
    return max(1, int(round(price_cookies)))


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
    
    # Top Tab Row
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
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
    parts = call.data.split(":")
    page = int(parts[1])
    
    # 1. Fetch completions for the house
    async with AsyncSessionLocal() as session:
        # Get all user IDs in the house
        users_result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )
        users = users_result.scalars().all()
        house_user_ids = [u.id for u in users]
        user_name_map = {u.id: (u.display_name or u.username or "?") for u in users}
        
        # Get all completions for these users
        comps_result = await session.execute(
            select(Completion).where(Completion.user_id.in_(house_user_ids))
        )
        completions = comps_result.scalars().all()
        
        # Get task instances and templates to get titles
        inst_ids = [c.task_instance_id for c in completions]
        insts_result = await session.execute(
            select(TaskInstance).where(TaskInstance.id.in_(inst_ids)) if inst_ids else select(TaskInstance).where(False)
        )
        insts = insts_result.scalars().all()
        inst_map = {i.id: i for i in insts}
        
        tmpl_ids = [i.template_id for i in insts]
        tmpls_result = await session.execute(
            select(TaskTemplate).where(TaskTemplate.id.in_(tmpl_ids)) if tmpl_ids else select(TaskTemplate).where(False)
        )
        tmpls = tmpls_result.scalars().all()
        tmpl_map = {t.id: t for t in tmpls}
        
        # Also fetch personal tasks completions
        pt_result = await session.execute(
            select(PersonalTask).where(
                and_(PersonalTask.user_id.in_(house_user_ids), PersonalTask.is_completed == True, PersonalTask.is_deleted == False)
            )
        )
        personal_completed = pt_result.scalars().all()

    # Group all entries by date
    from collections import defaultdict
    grouped = defaultdict(list)
    from datetime import date, datetime
    
    for c in completions:
        dt = c.created_at
        local_date = dt.date()
        inst = inst_map.get(c.task_instance_id)
        title = "Чора"
        if inst:
            tmpl = tmpl_map.get(inst.template_id)
            if tmpl:
                title = tmpl.title
        user_name = user_name_map.get(c.user_id, "?")
        points = c.points
        grouped[local_date].append({
            "is_personal": False,
            "id": c.id,
            "title": title,
            "user_name": user_name,
            "points": points,
            "sort_dt": dt
        })
        
    for pt in personal_completed:
        dt = pt.completed_at or pt.date_execution
        # convert to datetime if date
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt_datetime = datetime.combine(dt, datetime.min.time())
        else:
            dt_datetime = dt
        local_date = dt_datetime.date()
        user_name = user_name_map.get(pt.user_id, "?")
        grouped[local_date].append({
            "is_personal": True,
            "id": pt.id,
            "title": pt.text,
            "user_name": user_name,
            "points": 0,
            "sort_dt": dt_datetime
        })

    unique_dates = sorted(list(grouped.keys()), reverse=True)
    total_days = len(unique_dates)
    
    if total_days == 0:
        await call.answer("Архив выполненных задач пуст!", show_alert=False)
        return
        
    if page < 0:
        page = 0
    if page >= total_days:
        page = total_days - 1
        
    current_date = unique_dates[page]
    day_entries = grouped[current_date]
    day_entries.sort(key=lambda x: x["sort_dt"], reverse=True)

    text = "📋 <b>Выполненные задачи вашего дома</b>\n👉 <i>Для возврата задачи нажмите на неё:</i>"
    
    def get_ru_weekday_abbr(d) -> str:
        abbrs = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        return abbrs[d.weekday()]

    builder = InlineKeyboardBuilder()
    
    for e in day_entries:
        if e.get("is_personal"):
            clean = clean_task_text(e["title"])
            left_text = f"👤 {clean}"
            right_text = f"{e['user_name']}"
            callback = f"rollback_pt:{e['id']}:{page}"
        else:
            left_text = f"🏠 {e['title']}"
            right_text = f"{e['user_name']} ({e['points']}🍪)"
            callback = f"rollback_chore:{e['id']}:{page}"
            
        builder.row(
            InlineKeyboardButton(text=left_text, callback_data="noop"),
            InlineKeyboardButton(text=right_text, callback_data=callback)
        )

    # Pagination row at the bottom (3-button layout)
    nav = []
    # Left arrow
    if page < total_days - 1:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"stat_arch:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    # Middle label
    date_lbl = f"{current_date.strftime('%d.%m')} ({get_ru_weekday_abbr(current_date)})"
    nav.append(InlineKeyboardButton(text=date_lbl, callback_data="noop"))
    
    # Right arrow
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"stat_arch:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    builder.row(*nav)

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


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
            text += f"• *{r.title}* — `{adj} 🍪` (цена: `{r.price}` дн.)\n"
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
    # Left arrow
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"rewards_purchases:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    # Middle label
    import datetime
    today_d = datetime.date.today()
    def get_ru_weekday_abbr_local(d) -> str:
        abbrs = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        return abbrs[d.weekday()]
    date_lbl = f"{today_d.strftime('%d.%m')} ({get_ru_weekday_abbr_local(today_d)})"
    nav.append(InlineKeyboardButton(text=date_lbl, callback_data="noop"))
    
    # Right arrow
    if len(rows) == 5:
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"rewards_purchases:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    builder.row(*nav)
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
        f"Установлено название: *{title}*\n\nЗа сколько в среднем дней можно заработать на эту награду? (Введите количество дней, например: 3):",
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
        await message.answer("Пожалуйста, введите целое положительное число (например: 3):")
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
    await message.answer(f"✅ Награда успешно добавлена: *{title}* (цена: *{price}* дн.)", parse_mode="Markdown")
    await render_rewards_settings(message, db_user, is_callback=False)
