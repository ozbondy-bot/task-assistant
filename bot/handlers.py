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

from db.models import AsyncSessionLocal, User, House, PersonalTask, ShoppingItem
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


# ── FSM States ────────────────────────────────────────────────────────────────
class EditShop(StatesGroup):
    waiting_for_input = State()
    item_id = State()


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
def get_main_keyboard(mini_app_url: str) -> types.ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="📋 Сегодня"),
        KeyboardButton(text="📅 Планы"),
    )
    builder.row(
        KeyboardButton(text="🛒 Покупки"),
        KeyboardButton(
            text="🏠 Открыть приложение",
            web_app=WebAppInfo(url=mini_app_url)
        ),
    )
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


# ── /start ─────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message, db_user: User = None):
    name = db_user.display_name if db_user else message.from_user.first_name
    text = (
        f"👋 Привет, *{name}*!\n\n"
        "Это твой личный помощник по домашним и личным делам.\n\n"
        "📋 *Сегодня* — задачи на сегодня\n"
        "📅 *Планы* — задачи на будущее\n"
        "🛒 *Покупки* — список покупок\n"
        "🏠 *Открыть приложение* — полный интерфейс\n\n"
        "Просто напиши мне что нужно сделать, и я всё запомню!\n"
        "_Например: «купить молоко 150» или «позвонить врачу завтра»_"
    )
    await message.answer(text, reply_markup=get_main_keyboard(MINI_APP_URL), parse_mode="Markdown")


@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


# ── Render Today ───────────────────────────────────────────────────────────────
async def rollover_overdue_tasks(session: AsyncSession, user_id: int):
    """Move overdue uncompleted tasks to today with yellow marker."""
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
        task.text = f"🟡 {clean}"
        task.date_execution = today
    if tasks:
        await session.commit()


async def ensure_daily_routines(session: AsyncSession, user_id: int):
    """Add daily routines if not present for today."""
    today = datetime.now().date()
    routines = [
        "💧 Выпить 1 стакан теплой воды натощак",
        "🤸‍♂️ 5 минут зарядки",
        "🚶‍♂️ 30 минут прогулки",
        "🐈 Поиграть с Бусей",
        "📚 Изучение слов",
    ]
    for r in routines:
        exists = await session.scalar(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == user_id,
                    PersonalTask.date_execution == today,
                    PersonalTask.text.like(f"%{r}%"),
                    PersonalTask.is_deleted == False,
                )
            )
        )
        if not exists:
            task = PersonalTask(
                user_id=user_id,
                text=f"🟢 {r}",
                date_execution=today,
                category="routine",
                is_completed=False,
                is_deleted=False,
            )
            session.add(task)
    await session.commit()


async def render_today(message: types.Message, db_user: User, is_callback=False):
    today = datetime.now().date()
    async with AsyncSessionLocal() as session:
        await rollover_overdue_tasks(session, db_user.id)
        await ensure_daily_routines(session, db_user.id)

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
        tasks = result.scalars().all()

    if tasks:
        text = "📋 *Задачи на сегодня*\n👉 _Тапни для выполнения:_"
        builder = InlineKeyboardBuilder()
        for t in tasks:
            rec_icon = " 🔁" if t.recurrence else ""
            builder.button(text=f"{t.text}{rec_icon}", callback_data=f"done_task:{t.id}")
        builder.adjust(1)
        builder.row(
            InlineKeyboardButton(text="➡️ Перенос", callback_data="t_menu_move"),
            InlineKeyboardButton(text="❌ Удалить", callback_data="t_menu_del"),
            InlineKeyboardButton(text="📜 Архив", callback_data="t_arch:0"),
        )
    else:
        text = "🎉 *На сегодня задач нет! Всё выполнено.*"
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="📜 Архив задач", callback_data="t_arch:0"))

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text == "📋 Сегодня")
async def today_handler(m: types.Message, db_user: User = None):
    await render_today(m, db_user)


@dp.callback_query(F.data == "t_cancel")
async def t_cancel(call: types.CallbackQuery, db_user: User = None):
    await render_today(call.message, db_user, True)


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
                    text=f"🟢 {clean}",
                    date_execution=datetime.now().date() + delta,
                    category="inbox",
                    recurrence=task.recurrence,
                    is_completed=False,
                    is_deleted=False,
                )
                session.add(new_task)
            await session.commit()
    await render_today(call.message, db_user, True)


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
            task.text = f"🟢 {clean}"
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
        b.button(text=f"✅ [{ds}] {clean}", callback_data=f"restore_t:{t.id}")
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
                text=f"🟢 {clean}",
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

    markup = builder.as_markup()
    if is_callback:
        await message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@dp.message(F.text == "🛒 Покупки")
async def shop_handler(m: types.Message, db_user: User = None):
    await render_shop(m, db_user)


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
