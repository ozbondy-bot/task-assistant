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
    from db.models import AsyncSessionLocal, TaskTemplate
    from bot.parser import get_ai_emoji
    from sqlalchemy import select
    
    logger.info("Starting template emoji migration...")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TaskTemplate))
        templates = result.scalars().all()
        
        updated_count = 0
        for tmpl in templates:
            # Check if title already starts with an emoji
            if not has_leading_emoji(tmpl.title):
                emoji = await get_ai_emoji(tmpl.title)
                if emoji:
                    tmpl.title = f"{emoji} {tmpl.title}"
                    updated_count += 1
                    logger.info(f"Prepend emoji {emoji} to chore: {tmpl.title}")
                    
        if updated_count > 0:
            await session.commit()
            logger.info(f"Successfully migrated {updated_count} template emojis.")
        else:
            logger.info("No templates needed emoji migration.")


async def migrate_reward_prices_to_days():
    from db.models import AsyncSessionLocal, Reward
    from sqlalchemy import select
    
    logger.info("Starting reward price to days migration...")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Reward))
        rewards = result.scalars().all()
        
        updated_count = 0
        for r in rewards:
            if r.price > 14:
                old_price = r.price
                new_price = max(1, int(round(old_price / 15.0)))
                r.price = new_price
                updated_count += 1
                logger.info(f"Converted reward '{r.title}' price from {old_price} cookies to {new_price} days.")
                
        if updated_count > 0:
            await session.commit()
            logger.info(f"Successfully converted {updated_count} reward prices to days.")
        else:
            logger.info("No rewards needed price migration.")


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
