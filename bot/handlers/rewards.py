import logging
from datetime import datetime, timedelta
from aiogram import types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import sqlalchemy as sa
from sqlalchemy import select, and_, func

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


async def get_shop_calculation_stats(session):
    from datetime import datetime, timedelta
    from db.models import Completion, User
    from sqlalchemy import select, func, and_
    
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    
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
    
    num_users = await session.scalar(
        select(func.count(User.id)).where(User.house_id == ACTIVE_HOUSE_ID)
    ) or 1
    if num_users == 0:
        num_users = 1
        
    # Points per user per day over 30 days (completely dynamic, no floor)
    points_per_day = total_points / (30.0 * num_users)
    return total_points, num_users, points_per_day

async def get_adjusted_price(session, base_days: int) -> int:
    total_points, num_users, points_per_day = await get_shop_calculation_stats(session)
    if points_per_day <= 0:
        return max(1, base_days)  # fallback if no 30-day history
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
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="🛍 Магазин", callback_data="rewards_shop_view"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
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
async def handle_stat_arch(call: types.CallbackQuery, db_user: User = None, _page: int = None):
    if _page is not None:
        page = _page
    else:
        parts = call.data.split(":")
        page = int(parts[1])
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        target_date = today - timedelta(days=page)
        
        # 1. House chores completed on target_date
        chores_result = await session.execute(
            select(Completion, TaskInstance, TaskTemplate, User)
            .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
            .join(TaskTemplate, TaskInstance.template_id == TaskTemplate.id)
            .join(User, Completion.user_id == User.id)
            .where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    func.date(Completion.created_at) == target_date
                )
            )
        )
        chores_rows = chores_result.all()
        
        # 2. Personal tasks completed on target_date
        users_result = await session.execute(select(User.id).where(User.house_id == ACTIVE_HOUSE_ID))
        house_user_ids = [u[0] for u in users_result.all()]
        
        pts_result = await session.execute(
            select(PersonalTask, User)
            .join(User, PersonalTask.user_id == User.id)
            .where(
                and_(
                    PersonalTask.user_id.in_(house_user_ids),
                    PersonalTask.is_completed == True,
                    func.date(PersonalTask.completed_at) == target_date
                )
            )
        )
        pts_rows = pts_result.all()

    # Combine
    day_entries = []
    for comp, inst, tmpl, usr in chores_rows:
        day_entries.append({
            "type": "chore",
            "id": comp.id,
            "title": tmpl.title,
            "points": comp.points,
            "user": usr.display_name,
            "time": comp.created_at.strftime("%H:%M")
        })
    for pt, usr in pts_rows:
        day_entries.append({
            "type": "personal",
            "id": pt.id,
            "title": clean_task_text(pt.text),
            "points": 0,
            "user": usr.display_name,
            "time": pt.completed_at.strftime("%H:%M")
        })
        
    day_entries.sort(key=lambda x: x["time"], reverse=True)

    # Header date label
    date_lbl = target_date.strftime("%d.%m")
    # Get weekday
    def get_ru_weekday_abbr_local(d) -> str:
        abbrs = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        return abbrs[d.weekday()]
    weekday_lbl = get_ru_weekday_abbr_local(target_date)
    
    text = f"📜 *Архив выполненных задач* — {date_lbl} ({weekday_lbl}):\n\n"
    if day_entries:
        for e in day_entries:
            pts_str = f" (+{e['points']}🍪)" if e['points'] > 0 else ""
            text += f"• *[{e['time']}]* {e['user']}: {e['title']}{pts_str}\n"
    else:
        text += "В этот день никто ничего не выполнял."

    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="🛍 Магазин", callback_data="rewards_shop_view"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="⚡📜 Архив⚡", callback_data="noop")
    )
    
    for e in day_entries:
        if e["type"] == "chore":
            builder.row(InlineKeyboardButton(text=f"🔄 Отменить: {e['title']}", callback_data=f"rollback_chore:{e['id']}:{page}"))
        else:
            builder.row(InlineKeyboardButton(text=f"🔄 Отменить: {e['title']}", callback_data=f"rollback_task:{e['id']}:{page}"))

    # Bottom pagination (3-button layout)
    nav = []
    nav.append(InlineKeyboardButton(text="⏪", callback_data=f"stat_arch:{page+1}"))
    
    # Middle button shows date and weekday
    nav.append(InlineKeyboardButton(text=f"{date_lbl} ({weekday_lbl})", callback_data="noop"))
    
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"stat_arch:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    builder.row(*nav)
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("rollback_task:"))
async def handle_rollback_task(call: types.CallbackQuery, db_user: User = None):
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
            
    await handle_stat_arch(call, db_user, _page=page)


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
            
    await handle_stat_arch(call, db_user, _page=page)



# ── Rewards Shop (Магазин наград) ─────────────────────────────────────────────
async def render_rewards_settings(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()

    text = "⚙️ *Управление наградами:*\n\n"
    
    rewards_builder = InlineKeyboardBuilder()
    if rewards:
        for r in rewards:
            adj = await get_adjusted_price(session, r.price)
            text += f"• *{r.title}* — `{adj} 🍪` (цена: `{r.price}` дн.)\n"
            rewards_builder.button(text=f"❌ {r.title}", callback_data=f"del_reward:{r.id}")
        text += "\n_Нажмите на кнопку с наградой, чтобы удалить её._"
    else:
        text += "Наград пока нет."

    rewards_builder.adjust(1)
    
    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🛑 Магазин⚡", callback_data="noop"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
    )
    
    builder.attach(rewards_builder)
    
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
        
        total_points, num_users, points_per_day = await get_shop_calculation_stats(session)

    # Format stats line
    if points_per_day > 0:
        stat_line = (
            f"ℹ️ <b>Расчёт цен:</b> за 30 дней заработано <code>{total_points}🍪</code>, "
            f"участников: <code>{num_users}</code>, "
            f"в день на человека: <code>{points_per_day:.1f}🍪</code>"
        )
    else:
        stat_line = "ℹ️ Истории выполнения за 30 дней нет — цены в днях."

    text = (
        "🎁 <b>Магазин наград</b>\n\n"
        f"{stat_line}\n"
        "Формула: <code>Цена = Базовые дни × Среднее в день</code>\n\n"
        "👉 <b>Нажми на награду для покупки:</b>"
    )

    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🛑 Магазин⚡", callback_data="noop"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
    )
    
    # Row 3 (Rewards management — only visible in shop)
    builder.row(
        InlineKeyboardButton(text="⚙️ Управление наградами", callback_data="rewards_settings")
    )
    
    if rewards:
        for r in rewards:
            if points_per_day > 0:
                price_cookies = r.price * points_per_day
                adj = max(1, int(round(price_cookies)))
                builder.row(InlineKeyboardButton(
                    text=f"{r.title}  ({r.price} дн. = {adj}🍪)",
                    callback_data=f"buy_reward:{r.id}"
                ))
            else:
                builder.row(InlineKeyboardButton(
                    text=f"{r.title}  ({r.price} дн.)",
                    callback_data=f"buy_reward:{r.id}"
                ))
    else:
        text += "\nНаград пока нет."

    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


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
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="⚡🛍 Магазин⚡", callback_data="noop"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="shop_view_items"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
    )
    
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
