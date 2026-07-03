import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, and_

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (
    AsyncSessionLocal, User, House, PersonalTask, ShoppingItem,
    TaskTemplate, TaskInstance, Completion, Reward, RewardPurchase
)
from api.auth import validate_telegram_init_data

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ACTIVE_HOUSE_ID = 81

app = FastAPI(title="Task Assistant API", docs_url=None, redoc_url=None)


# ── Auth dependency ───────────────────────────────────────────────────────────
async def get_current_user(x_init_data: str = Header(None)) -> User:
    if not x_init_data:
        raise HTTPException(status_code=401, detail="Missing auth header")

    tg_user = validate_telegram_init_data(x_init_data, BOT_TOKEN)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid auth data")

    tg_id = tg_user.get("id")
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == tg_id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found. Please start the bot first.")
        return user


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


# ── API Routes ────────────────────────────────────────────────────────────────

# -- Personal tasks --
@app.get("/api/tasks/today")
async def get_today_tasks(user: User = Depends(get_current_user)):
    from bot.handlers.base import get_house_today_date
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(PersonalTask).where(
                and_(
                    PersonalTask.user_id == user.id,
                    PersonalTask.date_execution == today,
                    PersonalTask.is_completed == False,
                    PersonalTask.is_deleted == False,
                )
            ).order_by(PersonalTask.id)
        )
        tasks = result.scalars().all()
        
        # Also get in-progress household tasks (claimed by this user)
        house_result = await session.execute(
            select(TaskInstance, TaskTemplate).join(
                TaskTemplate, TaskInstance.template_id == TaskTemplate.id
            ).where(
                and_(
                    TaskInstance.done_by_user_id == user.id,
                    TaskInstance.status == "in_progress",
                )
            )
        )
        house_tasks = house_result.all()

    return {
        "personal": [
            {
                "id": t.id,
                "text": t.text,
                "date": str(t.date_execution),
                "recurrence": t.recurrence,
                "category": t.category,
                "type": "personal",
            }
            for t in tasks
        ],
        "household": [
            {
                "id": inst.id,
                "text": tmpl.title,
                "points": tmpl.points,
                "template_id": tmpl.id,
                "type": "household",
                "status": "in_progress",
            }
            for inst, tmpl in house_tasks
        ],
    }


class AddTaskRequest(BaseModel):
    text: str
    date: Optional[str] = None
    recurrence: Optional[str] = None
    category: str = "inbox"


@app.post("/api/tasks")
async def add_task(req: AddTaskRequest, user: User = Depends(get_current_user)):
    from bot.parser import parse_input
    _, clean_text, date_exec, _, _, recurrence = parse_input(req.text)
    if req.date:
        try:
            date_exec = datetime.strptime(req.date, "%Y-%m-%d").date()
        except Exception:
            pass
    if req.recurrence:
        recurrence = req.recurrence

    from bot.parser import get_ai_emoji
    emoji = await get_ai_emoji(clean_text)
    if emoji:
        clean_text = f"{emoji} {clean_text}"

    async with AsyncSessionLocal() as session:
        task = PersonalTask(
            user_id=user.id,
            text=clean_text,
            date_execution=date_exec,
            category=req.category,
            recurrence=recurrence,
            is_completed=False,
            is_deleted=False,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return {"id": task.id, "text": task.text, "date": str(task.date_execution)}


@app.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int, user: User = Depends(get_current_user)):
    from bot.parser import get_recurrence_delta, clean_task_text
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, task_id)
        if not task or task.user_id != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        task.is_completed = True
        if task.recurrence:
            delta = get_recurrence_delta(task.recurrence)
            from bot.handlers.base import get_house_today_date
            today_date = await get_house_today_date(session)
            new_task = PersonalTask(
                user_id=user.id,
                text=f"🟢 {clean_task_text(task.text)}",
                date_execution=today_date + delta,
                category="inbox",
                recurrence=task.recurrence,
                is_completed=False,
                is_deleted=False,
            )
            session.add(new_task)
        await session.commit()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, task_id)
        if not task or task.user_id != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
        await session.delete(task)
        await session.commit()
    return {"ok": True}


# -- Household tasks --
@app.get("/api/house/tasks")
async def get_house_tasks(user: User = Depends(get_current_user)):
    from bot.handlers.base import get_house_today_date
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(TaskInstance, TaskTemplate).join(
                TaskTemplate, TaskInstance.template_id == TaskTemplate.id
            ).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskInstance.date <= today,
                    TaskInstance.status == "free",
                    TaskTemplate.deleted == False,
                )
            ).order_by(TaskTemplate.points.desc())
        )
        rows = result.all()

    return [
        {
            "id": inst.id,
            "template_id": tmpl.id,
            "title": tmpl.title,
            "points": tmpl.points,
            "periodicity": tmpl.periodicity,
            "status": inst.status,
        }
        for inst, tmpl in rows
    ]


@app.post("/api/house/tasks/{instance_id}/claim")
async def claim_house_task(instance_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst:
            raise HTTPException(status_code=404, detail="Task not found")
        if inst.status != "free":
            raise HTTPException(status_code=409, detail="Task already claimed")
        inst.status = "in_progress"
        inst.done_by_user_id = user.id
        await session.commit()
    return {"ok": True, "status": "in_progress"}


@app.post("/api/house/tasks/{instance_id}/done")
async def complete_house_task(instance_id: int, points: Optional[int] = None, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst or inst.done_by_user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        
        tmpl = await session.get(TaskTemplate, inst.template_id)
        inst.status = "done"
        inst.done_at = datetime.utcnow()

        # Award points
        db_user = await session.get(User, user.id)
        if points is not None:
            pts = points
        else:
            if tmpl and tmpl.title == "Готовка":
                pts = 5  # default cooking points
            else:
                pts = tmpl.points if tmpl else 1
        db_user.points = (db_user.points or 0) + pts

        # Record completion
        comp = Completion(
            user_id=user.id,
            task_instance_id=instance_id,
            points=pts,
        )
        session.add(comp)
        await session.commit()

    return {"ok": True, "points_earned": pts}


@app.post("/api/house/tasks/{instance_id}/unclaim")
async def unclaim_house_task(instance_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst or inst.done_by_user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        inst.status = "free"
        inst.done_by_user_id = None
        await session.commit()
    return {"ok": True}


# -- House info & members --
@app.get("/api/house/members")
async def get_house_members(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID)
        )
        members = result.scalars().all()
    return [
        {
            "id": m.id,
            "display_name": m.display_name or m.full_name or "Участник",
            "points": m.points or 0,
            "is_owner": m.is_house_owner,
            "is_me": m.id == user.id,
        }
        for m in members
    ]


# -- Shopping --
@app.get("/api/shopping")
async def get_shopping(user: User = Depends(get_current_user)):
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
    return [
        {"id": i.id, "item_name": i.item_name, "price": i.price, "priority": i.priority}
        for i in items
    ]


class AddShoppingRequest(BaseModel):
    item_name: str
    price: int = 0
    priority: str = "normal"


@app.post("/api/shopping")
async def add_shopping(req: AddShoppingRequest, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        item = ShoppingItem(
            house_id=ACTIVE_HOUSE_ID,
            user_id=user.id,
            item_name=req.item_name,
            price=req.price,
            priority=req.priority,
            is_bought=False,
            is_deleted=False,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return {"id": item.id, "item_name": item.item_name, "price": item.price}


@app.post("/api/shopping/{item_id}/bought")
async def mark_bought(item_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        item = await session.get(ShoppingItem, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        item.is_bought = True
        item.bought_at = datetime.utcnow()
        await session.commit()
    return {"ok": True}


@app.delete("/api/shopping/{item_id}")
async def delete_shopping(item_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        item = await session.get(ShoppingItem, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        await session.delete(item)
        await session.commit()
    return {"ok": True}


# -- Rewards / Shop --
@app.get("/api/rewards")
async def get_rewards(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()
        db_user = await session.get(User, user.id)
        user_points = db_user.points or 0

    return {
        "user_points": user_points,
        "rewards": [{"id": r.id, "title": r.title, "price": r.price} for r in rewards],
    }


@app.post("/api/rewards/{reward_id}/buy")
async def buy_reward(reward_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if not reward:
            raise HTTPException(status_code=404, detail="Reward not found")

        db_user = await session.get(User, user.id)
        if (db_user.points or 0) < reward.price:
            raise HTTPException(status_code=402, detail="Not enough points")

        db_user.points -= reward.price
        purchase = RewardPurchase(
            user_id=user.id,
            reward_title=reward.title,
            price=reward.price,
            status="purchased",
        )
        session.add(purchase)
        await session.commit()

    return {"ok": True, "remaining_points": db_user.points}


# -- Stats --
@app.get("/api/stats")
async def get_stats(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        db_user = await session.get(User, user.id)
        total_personal = await session.scalar(
            select(PersonalTask).where(
                and_(PersonalTask.user_id == user.id, PersonalTask.is_completed == True)
            )
        )
        total_house = await session.scalar(
            select(Completion).where(Completion.user_id == user.id)
        )
        result = await session.execute(
            select(User).where(User.house_id == ACTIVE_HOUSE_ID).order_by(User.points.desc())
        )
        leaderboard = result.scalars().all()

    return {
        "my_points": db_user.points or 0,
        "leaderboard": [
            {
                "display_name": m.display_name or m.full_name or "?",
                "points": m.points or 0,
                "is_me": m.id == user.id,
            }
            for m in leaderboard
        ],
    }


# -- Chores templates --
@app.get("/api/chores/templates")
async def get_chores_templates(user: User = Depends(get_current_user)):
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
    return [
        {
            "id": t.id,
            "title": t.title,
            "points": t.points,
            "periodicity": t.periodicity,
            "start_date": str(t.start_date) if t.start_date else None,
        }
        for t in templates
    ]

class CreateTemplateRequest(BaseModel):
    title: str
    points: int = 1
    periodicity: str = "daily"
    start_date: Optional[str] = None

@app.post("/api/chores/templates")
async def create_chore_template(req: CreateTemplateRequest, user: User = Depends(get_current_user)):
    start_date = None
    if req.start_date:
        try:
            start_date = datetime.strptime(req.start_date, "%Y-%m-%d").date()
        except Exception:
            pass
            
    from bot.parser import get_ai_emoji
    clean_title = req.title
    emoji = await get_ai_emoji(clean_title)
    if emoji:
        clean_title = f"{emoji} {clean_title}"
        
    async with AsyncSessionLocal() as session:
        from bot.handlers.base import get_house_today_date
        if not start_date:
            start_date = await get_house_today_date(session)
            
        tmpl = TaskTemplate(
            house_id=ACTIVE_HOUSE_ID,
            title=clean_title,
            points=req.points,
            periodicity=req.periodicity,
            start_date=start_date,
            deleted=False
        )
        session.add(tmpl)
        await session.commit()
        await session.refresh(tmpl)
    return {"ok": True, "id": tmpl.id, "title": tmpl.title}

@app.delete("/api/chores/templates/{template_id}")
async def delete_chore_template(template_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, template_id)
        if not tmpl or tmpl.house_id != ACTIVE_HOUSE_ID:
            raise HTTPException(status_code=404, detail="Template not found")
        tmpl.deleted = True
        await session.commit()
    return {"ok": True}


# -- Rewards templates --
class CreateRewardRequest(BaseModel):
    title: str
    price: int

@app.post("/api/rewards")
async def create_reward(req: CreateRewardRequest, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        reward = Reward(
            house_id=ACTIVE_HOUSE_ID,
            title=req.title,
            price=req.price
        )
        session.add(reward)
        await session.commit()
        await session.refresh(reward)
    return {"ok": True, "id": reward.id, "title": reward.title}

@app.delete("/api/rewards/{reward_id}")
async def delete_reward(reward_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if not reward or reward.house_id != ACTIVE_HOUSE_ID:
            raise HTTPException(status_code=404, detail="Reward not found")
        await session.delete(reward)
        await session.commit()
    return {"ok": True}


# -- House settings & Generate --
@app.get("/api/house/settings")
async def get_house_settings(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        house = await session.get(House, ACTIVE_HOUSE_ID)
        if not house:
            raise HTTPException(status_code=404, detail="House not found")
        return {
            "name": house.name or "Уютное гнездышко",
            "timezone": house.timezone,
            "join_code": house.join_code
        }

class UpdateHouseSettingsRequest(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None

@app.post("/api/house/settings")
async def update_house_settings(req: UpdateHouseSettingsRequest, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        house = await session.get(House, ACTIVE_HOUSE_ID)
        if not house:
            raise HTTPException(status_code=404, detail="House not found")
        if req.name is not None:
            house.name = req.name
        if req.timezone is not None:
            house.timezone = req.timezone
        await session.commit()
    return {"ok": True}

@app.post("/api/house/generate")
async def force_generate_chores(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        from bot.handlers.base import generate_daily_chores_if_needed
        await generate_daily_chores_if_needed(session, ACTIVE_HOUSE_ID)
        await session.commit()
    return {"ok": True}


# -- Task actions (unclaim, skip, shift) --
@app.post("/api/house/tasks/{instance_id}/unclaim")
async def unclaim_chore(instance_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst or inst.done_by_user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        inst.status = "free"
        inst.done_by_user_id = None
        await session.commit()
    return {"ok": True}

@app.post("/api/house/tasks/{instance_id}/skip")
async def skip_chore(instance_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst or inst.done_by_user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        inst.status = "skipped"
        await session.commit()
    return {"ok": True}

class ShiftRequest(BaseModel):
    new_date: str

@app.post("/api/house/tasks/{instance_id}/shift")
async def shift_chore_instance(instance_id: int, req: ShiftRequest, user: User = Depends(get_current_user)):
    try:
        new_date = datetime.strptime(req.new_date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format (YYYY-MM-DD)")
        
    async with AsyncSessionLocal() as session:
        inst = await session.get(TaskInstance, instance_id)
        if not inst or inst.done_by_user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
            
        inst.status = "shifted"
        exists = await session.scalar(
            select(TaskInstance).where(
                and_(
                    TaskInstance.template_id == inst.template_id,
                    TaskInstance.date == new_date,
                    TaskInstance.status.in_(["free", "shifted"])
                )
            )
        )
        if not exists:
            new_inst = TaskInstance(
                template_id=inst.template_id,
                date=new_date,
                status="free",
                priority=0
            )
            session.add(new_inst)
        await session.commit()
    return {"ok": True}

@app.post("/api/tasks/{task_id}/shift")
async def shift_personal_task(task_id: int, req: ShiftRequest, user: User = Depends(get_current_user)):
    try:
        new_date = datetime.strptime(req.new_date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format (YYYY-MM-DD)")
        
    async with AsyncSessionLocal() as session:
        task = await session.get(PersonalTask, task_id)
        if not task or task.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your task")
        task.date_execution = new_date
        await session.commit()
    return {"ok": True}


# ── Static files for Mini App ─────────────────────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/app", StaticFiles(directory=frontend_dir, html=True), name="frontend")


@app.get("/")
@app.head("/")
async def root():
    index = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Task Assistant API", "docs": "/health"}
