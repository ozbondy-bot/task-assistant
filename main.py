import os
import asyncio
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))
MINI_APP_URL = os.getenv("MINI_APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://example.com"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required!")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required!")


async def migrate_emojis_in_background():
    logger.info("Starting background database records emoji migration...")
    try:
        from bot.parser import get_ai_emoji
        from db.models import AsyncSessionLocal, PersonalTask, TaskTemplate
        from sqlalchemy import select
        import re
        async with AsyncSessionLocal() as session:
            # 1. Update PersonalTask records
            tasks_res = await session.execute(select(PersonalTask))
            tasks = tasks_res.scalars().all()
            updated_any = False
            for t in tasks:
                has_emoji = re.match(r'^([\u2600-\u27BF\U0001f000-\U0001f9ff])', t.text)
                if not has_emoji and not t.text.startswith("🔴"):
                    emoji = await get_ai_emoji(t.text)
                    if emoji and emoji != "📝":
                        t.text = f"{emoji} {t.text}"
                        updated_any = True
            
            # 2. Update TaskTemplate records
            templates_res = await session.execute(select(TaskTemplate))
            templates = templates_res.scalars().all()
            for tmpl in templates:
                has_emoji = re.match(r'^([\u2600-\u27BF\U0001f000-\U0001f9ff])', tmpl.title)
                if not has_emoji:
                    emoji = await get_ai_emoji(tmpl.title)
                    if emoji and emoji != "📝":
                        tmpl.title = f"{emoji} {tmpl.title}"
                        updated_any = True
                        
            if updated_any:
                await session.commit()
                logger.info("Background database records successfully migrated with emojis.")
            else:
                logger.info("No database records needed emoji migration.")
    except Exception as e:
        logger.error(f"Failed to migrate database records with emojis in background: {e}")


async def run_bot():
    from bot.handlers.base import bot
    from bot.handlers import dp
    logger.info("Starting bot polling...")
    asyncio.create_task(migrate_emojis_in_background())
    try:
        await bot.set_my_description(
            "Привет! Я семейный помощник по дому и личным делам. 🏠✨\n\n"
            "Помогаю распределять домашние обязанности, начисляю печеньки за выполненные задачи, "
            "веду списки покупок и личные дела.\n\n"
            "Выполняйте дела, зарабатывайте печеньки 🍪 и обменивайте их на награды!"
        )
        await bot.set_my_short_description(
            "Помощник по домашним делам и личным задачам. Зарабатывайте печеньки и обменивайте их на награды! 🍪🏠"
        )
        await bot.delete_my_commands()
        
        from aiogram.types import WebAppInfo, MenuButtonWebApp
        app_url = MINI_APP_URL
        if app_url and not app_url.endswith("/app") and not app_url.endswith("/app/"):
            app_url = app_url.rstrip("/") + "/app"
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📱 Открыть App",
                    web_app=WebAppInfo(url=app_url)
                )
            )
            logger.info("Bot descriptions, commands, and webapp menu button set successfully.")
        except Exception as e:
            logger.error(f"Failed to set chat menu button: {e}")
    except Exception as e:
        logger.error(f"Failed to set bot descriptions: {e}")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


async def run_api():
    import uvicorn
    from api.routes import app
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    logger.info(f"Starting FastAPI on port {PORT}...")
    await server.serve()


def has_leading_emoji(text: str) -> bool:
    if not text:
        return False
    val = text.strip()
    if not val:
        return False
    first_char_ord = ord(val[0])
    return (0x2000 <= first_char_ord <= 0x32FF) or (0x1F000 <= first_char_ord <= 0x1FFFF)


async def migrate_template_emojis():
    """Clean up and remove emojis from existing task templates and personal tasks in DB."""
    from db.models import AsyncSessionLocal, TaskTemplate, PersonalTask
    from sqlalchemy import select
    import re
    
    logger.info("Cleaning up emojis from templates and personal tasks...")
    
    emoji_pattern = re.compile(
        r'[🀀-\U0001fbfb\U0001fa00-\U0001faff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b06\u2b55\u2b1b\u2b1c\u2934\u2935\u3297\u3299\ufe0f\u200d\u200e\u200f]+',
        re.UNICODE
    )
    
    async with AsyncSessionLocal() as session:
        # 1. Clean TaskTemplate titles
        result = await session.execute(select(TaskTemplate))
        templates = result.scalars().all()
        t_updated = 0
        for tmpl in templates:
            cleaned = emoji_pattern.sub('', tmpl.title)
            cleaned = re.compile(r'\s+').sub(' ', cleaned).strip()
            if cleaned != tmpl.title:
                logger.info(f"Stripping emoji template: '{tmpl.title}' -> '{cleaned}'")
                tmpl.title = cleaned
                t_updated += 1
                
        # 2. Clean PersonalTask texts (keep 🔴 if present)
        pt_result = await session.execute(select(PersonalTask))
        personal_tasks = pt_result.scalars().all()
        pt_updated = 0
        for pt in personal_tasks:
            has_urgent = pt.text.startswith("🔴")
            cleaned = emoji_pattern.sub('', pt.text)
            cleaned = re.compile(r'\s+').sub(' ', cleaned).strip()
            if has_urgent:
                cleaned = f"🔴 {cleaned}"
            if cleaned != pt.text:
                logger.info(f"Stripping emoji personal task: '{pt.text}' -> '{cleaned}'")
                pt.text = cleaned
                pt_updated += 1
                
        if t_updated > 0 or pt_updated > 0:
            await session.commit()
            logger.info(f"Successfully cleaned database: {t_updated} templates, {pt_updated} personal tasks.")
        else:
            logger.info("Database templates and tasks are already clean of emojis.")


async def migrate_reward_prices_to_days():
    from db.models import AsyncSessionLocal, Reward
    from sqlalchemy import select
    
    # Default rewards per TZ with correct base_days:
    # price = number of 'average days' a person needs to earn this reward
    # average day = total_cookies_30days / 30 / num_users
    DEFAULT_REWARDS = [
        # (partial_title_match, correct_base_days)
        ("фильм",           2),
        ("массаж спины",    3),
        ("массаж шеи",      3),
        ("ванна",           4),
        ("завтрак",         5),
        ("массаж всего",    6),
        ("ничего не делаю 1", 7),
        ("спа",             10),
        ("ничего не делаю 2", 14),
        ("ничего не делаю неделю", 30),
        ("ничего не делаю 7", 30),
    ]
    
    logger.info("Starting reward base_days migration (TZ defaults)...")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Reward))
        rewards = result.scalars().all()
        
        updated_count = 0
        for r in rewards:
            title_lower = r.title.lower()
            for keyword, correct_days in DEFAULT_REWARDS:
                if keyword in title_lower and r.price != correct_days:
                    logger.info(f"Fixing reward '{r.title}': {r.price} -> {correct_days} days")
                    r.price = correct_days
                    updated_count += 1
                    break
                    
        if updated_count > 0:
            await session.commit()
            logger.info(f"Fixed {updated_count} reward base_days to match TZ.")
        else:
            logger.info("Reward base_days already correct.")


async def main():
    from bot.handlers.base import scheduler_loop
    


    # Run database migration for reward prices
    try:
        await migrate_reward_prices_to_days()
    except Exception as e:
        logger.error(f"Reward price migration failed: {e}")

    # Run bot polling, FastAPI, and scheduler loop concurrently
    await asyncio.gather(
        run_bot(),
        run_api(),
        scheduler_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
