import os
import dotenv
import asyncio
from sqlalchemy import select, and_

dotenv.load_dotenv()
os.environ["BOT_TOKEN"] = "123456:abcdef"

from db.models import AsyncSessionLocal, TaskTemplate, TaskInstance, House
from bot.handlers.base import get_house_today_date, get_template_next_date_val

async def test():
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        print(f"Today is: {today}")
        
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == 81,
                    TaskTemplate.deleted == False
                )
            )
        )
        templates = result.scalars().all()
        
        print("\n--- Diagnostic details for all active templates ---")
        for t in templates:
            last_done_date, nd = await get_template_next_date_val(session, t, today)
            print(f"Template: {t.title} | Periodicity: {t.periodicity} | Weekday: {t.weekday} | Month day: {t.month_day} | Start date: {t.start_date} | Calculated next date: {nd}")

asyncio.run(test())
