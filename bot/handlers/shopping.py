import logging
from datetime import datetime
from aiogram import types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from sqlalchemy import select, and_

from db.models import AsyncSessionLocal, User, ShoppingItem, PersonalTask, RewardPurchase, TaskTemplate
from bot.parser import clean_task_text, parse_input
from bot.handlers.base import bot, dp, ACTIVE_HOUSE_ID, logger, EditShop, get_house_today_date, render_today, get_partner_user, create_calendar_keyboard_custom, format_calendar_header


# ── Shopping ──────────────────────────────────────────────────────────────────
async def render_shop(message: types.Message, db_user: User, is_callback=False):
    async with AsyncSessionLocal() as session:
        # Fetch active shopping items
        result_items = await session.execute(
            select(ShoppingItem).where(
                and_(
                    ShoppingItem.house_id == ACTIVE_HOUSE_ID,
                    ShoppingItem.is_bought == False,
                    ShoppingItem.is_deleted == False,
                )
            ).order_by(ShoppingItem.priority.desc(), ShoppingItem.id.asc())
        )
        items = result_items.scalars().all()
        
        # Fetch active reward purchases for this house
        house_users_result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )
        house_users = house_users_result.scalars().all()
        house_user_ids = [u.id for u in house_users]
        user_name_map = {u.id: (u.display_name or u.username or "?") for u in house_users}
        
        result_purchases = await session.execute(
            select(RewardPurchase).where(
                and_(
                    RewardPurchase.user_id.in_(house_user_ids),
                    RewardPurchase.status.in_(["purchased", "pending_use"])
                )
            ).order_by(RewardPurchase.created_at.asc())
        )
        purchases = result_purchases.scalars().all()

    import random
    def get_emoji(item_id):
        from bot.parser import FOOD_EMOJIS
        state = random.getstate()
        random.seed(item_id)
        e = random.choice(FOOD_EMOJIS)
        random.setstate(state)
        return e

    text = "\u3164"

    builder = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    builder.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="📊 Stat", callback_data="stats_view")
    )
    
    # Row 2 (Sub-tabs)
    builder.row(
        InlineKeyboardButton(text="🛍 Магазин", callback_data="rewards_shop_view"),
        InlineKeyboardButton(text="⚡🛒 Покупки⚡", callback_data="noop"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
    )
    
    # Row 3 (Info headers)
    if items or purchases:
        total = sum(i.price for i in items)
        builder.row(
            InlineKeyboardButton(text=f"🛒 Покупки — {total} ₽", callback_data="noop")
        )
        builder.row(
            InlineKeyboardButton(text="👉 Нажми на товар, чтобы вычеркнуть:", callback_data="noop")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🍏 Список покупок пуст!", callback_data="noop")
        )

    # Grocery items list
    for item in items:
        prefix = "🔴 " if item.priority == "high" else ""
        price_str = f"{item.price}₽ " if item.price > 0 else ""
        emoji = get_emoji(item.id)
        builder.row(InlineKeyboardButton(text=f"{price_str}{emoji} {prefix}{item.item_name}", callback_data=f"done_shop:{item.id}"))
        
    # Reward purchases list
    for purchase in purchases:
        buyer_name = user_name_map.get(purchase.user_id, "?")
        if purchase.status == "pending_use":
            builder.row(InlineKeyboardButton(text=f"⏳ 🎁 {purchase.reward_title} ({buyer_name})", callback_data="noop"))
        else:
            builder.row(InlineKeyboardButton(text=f"🎁 {purchase.reward_title} ({buyer_name})", callback_data=f"fulfill_rew:{purchase.id}"))
            
    # Bottom actions row
    if items or purchases:
        builder.row(
            InlineKeyboardButton(text="✏️ Изм.", callback_data="s_edit"),
            InlineKeyboardButton(text="❌ Удал.", callback_data="s_del"),
            InlineKeyboardButton(text="📜 Архив", callback_data="s_arch:0"),
        )
    else:
        builder.row(InlineKeyboardButton(text="📜 Архив покупок", callback_data="s_arch:0"))

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("fulfill_rew:"))
async def handle_fulfill_reward(call: types.CallbackQuery, db_user: User = None):
    purchase_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        purchase = await session.get(RewardPurchase, purchase_id)
        if not purchase:
            await call.answer("⚠️ Покупка не найдена!", show_alert=False)
            return
            
        if purchase.user_id == db_user.id:
            await call.answer("Эту награду должен выполнить твой партнёр! 😉", show_alert=False)
            return
            
        buyer = await session.get(User, purchase.user_id)
        if not buyer:
            await call.answer("⚠️ Покупатель не найден!", show_alert=False)
            return
            
        purchase.status = "pending_use"
        await session.commit()
        
        # Send confirmation request to the buyer
        partner_name = db_user.display_name or db_user.username or "?"
        confirm_text = (
            f"🔔 *{partner_name}* хочет погасить твою награду: *{purchase.reward_title}*.\n"
            "Подтверждаешь, что она была исполнена?"
        )
        confirm_kb = InlineKeyboardBuilder()
        confirm_kb.row(
            InlineKeyboardButton(text="✅ Да, исполнено", callback_data=f"conf_rew:{purchase.id}"),
            InlineKeyboardButton(text="❌ Нет, не сделано", callback_data=f"rej_rew:{purchase.id}")
        )
        
        try:
            await call.bot.send_message(
                chat_id=buyer.telegram_id,
                text=confirm_text,
                reply_markup=confirm_kb.as_markup(),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send confirmation message to buyer: {e}")
            
    await call.answer("Запрос на подтверждение отправлен партнёру! ⏳", show_alert=False)
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data.startswith("conf_rew:"))
async def handle_confirm_reward(call: types.CallbackQuery, db_user: User = None):
    purchase_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        purchase = await session.get(RewardPurchase, purchase_id)
        if not purchase:
            await call.answer("⚠️ Покупка не найдена!", show_alert=False)
            return
            
        if purchase.user_id != db_user.id:
            await call.answer("Только покупатель может подтвердить выполнение!", show_alert=False)
            return
            
        purchase.status = "used"
        purchase.used_at = datetime.utcnow()
        
        partner = await get_partner_user(session, db_user.id)
        await session.commit()
        
        buyer_name = db_user.display_name or db_user.username or "?"
        await call.message.edit_text(f"✅ Награда *{purchase.reward_title}* успешно погашена!", parse_mode="Markdown")
        
        if partner:
            try:
                await call.bot.send_message(
                    chat_id=partner.telegram_id,
                    text=f"✅ *{buyer_name}* подтвердил(а) выполнение награды *'{purchase.reward_title}'*!",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify partner: {e}")
                
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data.startswith("rej_rew:"))
async def handle_reject_reward(call: types.CallbackQuery, db_user: User = None):
    purchase_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        purchase = await session.get(RewardPurchase, purchase_id)
        if not purchase:
            await call.answer("⚠️ Покупка не найдена!", show_alert=False)
            return
            
        if purchase.user_id != db_user.id:
            await call.answer("Только покупатель может отклонить выполнение!", show_alert=False)
            return
            
        purchase.status = "purchased"
        
        partner = await get_partner_user(session, db_user.id)
        await session.commit()
        
        buyer_name = db_user.display_name or db_user.username or "?"
        await call.message.edit_text(f"❌ Выполнение награды *{purchase.reward_title}* отклонено.", parse_mode="Markdown")
        
        if partner:
            try:
                await call.bot.send_message(
                    chat_id=partner.telegram_id,
                    text=f"❌ *{buyer_name}* отклонил(а) выполнение награды *'{purchase.reward_title}'*. Она возвращена в список покупок.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify partner: {e}")
                
    await render_shop(call.message, db_user, True)


@dp.callback_query(F.data == "shop_view_items", StateFilter("*"))
async def shop_view_items_handler(call: types.CallbackQuery, state: FSMContext = None, db_user: User = None):
    if state:
        await state.clear()
    await render_shop(call.message, db_user, is_callback=True)


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
        # 1. Fetch bought shopping items
        result_items = await session.execute(
            select(ShoppingItem).where(
                and_(ShoppingItem.house_id == ACTIVE_HOUSE_ID, ShoppingItem.is_bought == True, ShoppingItem.is_deleted == False)
            )
        )
        items = result_items.scalars().all()

        # 2. Fetch all users in the house to map user IDs to names
        users_result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )
        users = users_result.scalars().all()
        house_user_ids = [u.id for u in users]
        user_name_map = {u.id: (u.display_name or u.username or "?") for u in users}

        # 3. Fetch used reward purchases
        result_purchases = await session.execute(
            select(RewardPurchase).where(
                and_(
                    RewardPurchase.user_id.in_(house_user_ids),
                    RewardPurchase.status == "used"
                )
            )
        )
        purchases = result_purchases.scalars().all()

    # Combine and sort by date desc
    archive_list = []
    for i in items:
        dt = i.bought_at or datetime.min
        archive_list.append({
            "type": "shopping_item",
            "id": i.id,
            "name": i.item_name,
            "price": i.price,
            "date": dt,
        })
    for p in purchases:
        dt = p.used_at or p.created_at or datetime.min
        buyer_name = user_name_map.get(p.user_id, "?")
        archive_list.append({
            "type": "reward_purchase",
            "id": p.id,
            "name": f"🎁 Награда: {p.reward_title} ({buyer_name})",
            "price": p.price,
            "date": dt,
        })

    archive_list.sort(key=lambda x: x["date"], reverse=True)

    # Paginate (10 per page)
    offset = page * 10
    limit = 10
    page_items = archive_list[offset:offset+limit]

    if not archive_list and page == 0:
        await call.answer("Архив покупок пуст!", show_alert=False)
        return

    text = "📜 *Архив покупок и наград*\n👉 _Тапни на покупку, чтобы вернуть её в список:_\n\n"
    b = InlineKeyboardBuilder()
    
    # Row 1 (Main Tabs)
    b.row(
        InlineKeyboardButton(text="🏠 Home", callback_data="home_view"),
        InlineKeyboardButton(text="📋 My", callback_data="my_page:0"),
        InlineKeyboardButton(text="⚡📊 Stat⚡", callback_data="noop")
    )
    
    # Row 2 (Sub-tabs)
    b.row(
        InlineKeyboardButton(text="🛍 Магазин", callback_data="rewards_shop_view"),
        InlineKeyboardButton(text="⚡🛒 Покупки⚡", callback_data="noop"),
        InlineKeyboardButton(text="📜 Архив", callback_data="stat_arch:0")
    )
    
    for entry in page_items:
        if entry["type"] == "shopping_item":
            price_str = f"({entry['price']}₽)" if entry["price"] > 0 else ""
            b.row(InlineKeyboardButton(text=f"✅ {entry['name']} {price_str}", callback_data=f"restore_shop:{entry['id']}:{page}"))
        else:
            price_str = f"({entry['price']}🍪)"
            b.row(InlineKeyboardButton(text=f"{entry['name']} {price_str}", callback_data="noop"))
            
    # Bottom pagination (3-button layout)
    nav = []
    # Left arrow
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"s_arch:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    # Middle button
    async with AsyncSessionLocal() as session:
        today_d = await get_house_today_date(session)
    def get_ru_weekday_abbr_local(d) -> str:
        abbrs = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        return abbrs[d.weekday()]
    date_lbl = f"{today_d.strftime('%d.%m')} ({get_ru_weekday_abbr_local(today_d)})"
    nav.append(InlineKeyboardButton(text=date_lbl, callback_data="noop"))
    
    # Right arrow
    if offset + limit < len(archive_list):
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"s_arch:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    b.row(*nav)
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



# ── Text/Voice input ──────────────────────────────────────────────────────────
@dp.message(StateFilter(None), F.text)
async def handle_any_text(message: types.Message, db_user: User = None, text_override: str = None):
    # Ignore command starts and main menu buttons
    text = message.text.strip()
    menu_buttons = {
        "🏠 Home", "🏠 Домашние дела",
        "📋 My", "👤 My", "👤 Мои дела",
        "📊 Stat", "🛍 Магазин и Покупки"
    }
    if text.startswith('/') or text in menu_buttons:
        return
    await message.answer(
        "⚠️ Добавление покупок и задач обычным текстом отключено.\n\n"
        "• Чтобы добавить покупку: перейдите в раздел *📊 Stat* → кнопка *Покупки* → кнопка *[ Добавить ]*.\n"
        "• Чтобы добавить личную задачу: перейдите в раздел *📋 My* → кнопка *[ Добавить ]*.",
        parse_mode="Markdown"
    )


@dp.message(StateFilter(None), F.voice)
async def handle_voice(message: types.Message, db_user: User = None):
    await message.answer(
        "⚠️ Голосовой ввод отключен. Пожалуйста, используйте кнопки в меню для добавления задач и покупок."
    )


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
    await call.message.edit_text("Выберите задачу для удаления:", reply_markup=builder.as_markup())


# Plan custom calendar navigation & shift execution callbacks
@dp.callback_query(F.data.startswith("rc_months_plan:"))
async def handle_rc_months_plan(call: types.CallbackQuery, db_user: User = None):
    t_id = int(call.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(t_id, today.year, today.month, today, "plan")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("cal_nav_plan:"))
async def handle_cal_nav_plan(call: types.CallbackQuery, db_user: User = None):
    parts = call.data.split(":")
    t_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
    markup = create_calendar_keyboard_custom(t_id, year, month, today, "plan")
    header = format_calendar_header(today)
    await call.message.edit_text(header, reply_markup=markup, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shift_plan:"))
async def handle_shift_plan(call: types.CallbackQuery, db_user: User = None):
    from bot.handlers.tasks import render_plans
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
    from bot.handlers.tasks import mov_p_select
    t_id = int(call.data.split(":")[1])
    call.data = f"mov_p:{t_id}"
    await mov_p_select(call)


@dp.callback_query(F.data.startswith("set_dt:"))
async def exe_set_dt(call: types.CallbackQuery, db_user: User = None):
    from bot.handlers.tasks import render_plans
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
