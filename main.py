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
    from bot.handlers import bot, dp
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


async def main():
    from bot.handlers import scheduler_loop
    # Run bot polling, FastAPI, and scheduler loop concurrently
    await asyncio.gather(
        run_bot(),
        run_api(),
        scheduler_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
