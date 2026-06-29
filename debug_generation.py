import os
import dotenv
import asyncio
from sqlalchemy import select, and_

dotenv.load_dotenv()
os.environ["BOT_TOKEN"] = "123456:abcdef"

from db.models import AsyncSessionLocal, TaskTemplate, TaskInstance, House
from bot.handlers.base import get_house_today_date

async def debug_gen():
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        weekday = today.weekday()
        day = today.day
        month = today.month
        house_id = 81

        print(f"DEBUG: today={today}, weekday={weekday}, day={day}, month={month}")

        house = await session.get(House, house_id)
        if not house:
            print("DEBUG: House not found!")
            return

        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == house_id,
                    TaskTemplate.deleted == False
                )
            )
        )
        templates = result.scalars().all()

        for tmpl in templates:
            print(f"\n--- Checking template: {tmpl.title} (ID: {tmpl.id}) ---")
            if tmpl.start_date and today < tmpl.start_date:
                print(f"  SKIPPED: today < start_date ({today} < {tmpl.start_date})")
                continue

            p = tmpl.periodicity
            should_create = False
            if p == "daily":
                should_create = True
                print("  p == 'daily' -> should_create = True")
            elif p == "weekly":
                target_wd = tmpl.weekday if tmpl.weekday is not None else 0
                should_create = (weekday == target_wd)
                print(f"  p == 'weekly' -> target_wd={target_wd}, weekday={weekday} -> should_create = {should_create}")
            elif p == "twice_weekly":
                should_create = (weekday in (0, 3))
                print(f"  p == 'twice_weekly' -> weekday={weekday} in (0, 3) -> should_create = {should_create}")
            elif p == "monthly":
                target_md = tmpl.month_day if tmpl.month_day is not None else 1
                should_create = (day == target_md)
                print(f"  p == 'monthly' -> target_md={target_md}, day={day} -> should_create = {should_create}")
            elif p == "twice_monthly":
                should_create = (day in (5, 20))
                print(f"  p == 'twice_monthly' -> day={day} in (5, 20) -> should_create = {should_create}")
            elif p == "quarterly":
                target_md = tmpl.month_day if tmpl.month_day is not None else 1
                if day == target_md:
                    pos = ((month - 1) % 3) + 1
                    target_q = tmpl.weekday if tmpl.weekday is not None else 1
                    should_create = (pos == target_q)
                    print(f"  p == 'quarterly' -> pos={pos}, target_q={target_q} -> should_create = {should_create}")
                else:
                    print(f"  p == 'quarterly' -> day={day} != target_md={target_md} -> should_create = False")
            elif p == "every_x_days":
                from bot.handlers.base import get_template_next_date_val
                last_done_date, nd = await get_template_next_date_val(session, tmpl, today)
                should_create = (nd <= today)
                print(f"  p == 'every_x_days' -> nd={nd} <= today={today} -> should_create = {should_create}")
            elif p == "once":
                from bot.handlers.base import get_template_next_date_val
                last_done_date, nd = await get_template_next_date_val(session, tmpl, today)
                should_create = (nd <= today)
                print(f"  p == 'once' -> nd={nd} <= today={today} -> should_create = {should_create}")

            if should_create:
                exists = await session.scalar(
                    select(TaskInstance).where(
                        and_(
                            TaskInstance.template_id == tmpl.id,
                            TaskInstance.date == today
                        )
                    )
                )
                print(f"  exists in DB for today: {exists}")
                if not exists:
                    print("  WOULD CREATE instance for today!")
                else:
                    print(f"  Instance already exists: ID={exists.id}, status={exists.status}")

asyncio.run(debug_gen())
