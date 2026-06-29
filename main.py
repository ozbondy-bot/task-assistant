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
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://example.com")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required!")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required!")


async def run_bot():
    from bot.handlers.base import bot
    from bot.handlers import dp
    logger.info("Starting bot polling...")
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
        logger.info("Bot descriptions set successfully.")
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
    """Strip leading emojis from all task template titles in DB."""
    from db.models import AsyncSessionLocal, TaskTemplate
    from sqlalchemy import select
    import re
    
    logger.info("Stripping emojis from template titles...")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TaskTemplate))
        templates = result.scalars().all()
        updated_count = 0
        emoji_pat = re.compile(
            r'^[\U0001f000-\U0001f9ff\U00002600-\U000027BF\ufe0f\u200d]+[\s]*',
            re.UNICODE
        )
        for tmpl in templates:
            stripped = emoji_pat.sub('', tmpl.title).strip()
            if stripped != tmpl.title:
                logger.info(f"Stripping emoji: '{tmpl.title}' -> '{stripped}'")
                tmpl.title = stripped
                updated_count += 1
        if updated_count > 0:
            await session.commit()
            logger.info(f"Stripped emojis from {updated_count} templates.")
        else:
            logger.info("No emoji stripping needed.")



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
    
    # Run database migration for chore emojis
    try:
        await migrate_template_emojis()
    except Exception as e:
        logger.error(f"Migration failed: {e}")

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
