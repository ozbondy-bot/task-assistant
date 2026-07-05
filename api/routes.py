import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, and_, delete

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (
    AsyncSessionLocal, User, House, PersonalTask, ShoppingItem,
    TaskTemplate, TaskInstance, Completion, Reward, RewardPurchase,
    PendingAction
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
async def get_today_tasks(date: Optional[str] = None, user: User = Depends(get_current_user)):
    from bot.handlers.base import get_house_today_date
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        
        target_date = today
        if date:
            try:
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
            except Exception:
                pass
                
        if target_date == today:
            cond = and_(
                PersonalTask.user_id == user.id,
                PersonalTask.date_execution <= today,
                PersonalTask.is_completed == False,
                PersonalTask.is_deleted == False,
            )
        else:
            cond = and_(
                PersonalTask.user_id == user.id,
                PersonalTask.date_execution == target_date,
                PersonalTask.is_deleted == False,
            )
            
        result = await session.execute(
            select(PersonalTask).where(cond).order_by(PersonalTask.id)
        )
        tasks = result.scalars().all()
        
        # Also get in-progress household tasks (claimed by this user)
        # Note: claimed chores only show up on today's dashboard
        house_tasks = []
        if target_date == today:
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
    from bot.handlers.base import get_house_today_date, generate_daily_chores_if_needed
    async with AsyncSessionLocal() as session:
        # Auto-generate today's chores if needed
        await generate_daily_chores_if_needed(session, ACTIVE_HOUSE_ID)
        
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
        
        res_list = []
        for inst, tmpl in rows:
            # Query last completion date
            last_date = await session.scalar(
                select(Completion.created_at)
                .join(TaskInstance, Completion.task_instance_id == TaskInstance.id)
                .where(TaskInstance.template_id == tmpl.id)
                .order_by(Completion.created_at.desc())
                .limit(1)
            )
            # Find next scheduled instance date
            next_date = await session.scalar(
                select(TaskInstance.date)
                .where(
                    and_(
                        TaskInstance.template_id == tmpl.id,
                        TaskInstance.status == "free",
                        TaskInstance.date > today
                    )
                )
                .order_by(TaskInstance.date.asc())
                .limit(1)
            )
            res_list.append({
                "id": inst.id,
                "template_id": tmpl.id,
                "title": tmpl.title,
                "points": tmpl.points,
                "periodicity": tmpl.periodicity,
                "period_days": tmpl.period_days,
                "date": str(inst.date),
                "status": inst.status,
                "last_completed": last_date.isoformat() if last_date else None,
                "next_execution": next_date.isoformat() if next_date else None
            })
            
    return res_list


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


# -- Rewards / Shop calculation and adjustment --
async def get_shop_calculation_stats(session):
    from sqlalchemy import func
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
        
    points_per_day = total_points / (30.0 * num_users)
    return total_points, num_users, points_per_day

async def get_adjusted_price(session, base_days: int) -> int:
    total_points, num_users, points_per_day = await get_shop_calculation_stats(session)
    if points_per_day <= 0:
        return max(1, base_days)
    price_cookies = base_days * points_per_day
    return max(1, int(round(price_cookies)))


@app.get("/api/rewards")
async def get_rewards(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Reward).where(Reward.house_id == ACTIVE_HOUSE_ID).order_by(Reward.price)
        )
        rewards = result.scalars().all()
        db_user = await session.get(User, user.id)
        user_points = db_user.points or 0
        
        # Calculate adjusted price for each reward
        adjusted_rewards = []
        for r in rewards:
            adj = await get_adjusted_price(session, r.price)
            adjusted_rewards.append({
                "id": r.id,
                "title": r.title,
                "price": adj,
                "base_days": r.price
            })

    return {
        "user_points": user_points,
        "rewards": adjusted_rewards,
    }


@app.post("/api/rewards/{reward_id}/buy")
async def buy_reward(reward_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        reward = await session.get(Reward, reward_id)
        if not reward:
            raise HTTPException(status_code=404, detail="Reward not found")

        adj_price = await get_adjusted_price(session, reward.price)
        db_user = await session.get(User, user.id)
        if (db_user.points or 0) < adj_price:
            raise HTTPException(status_code=402, detail="Not enough points")

        db_user.points -= adj_price
        purchase = RewardPurchase(
            user_id=user.id,
            reward_title=reward.title,
            price=adj_price,
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
    from bot.handlers.base import get_house_today_date, get_template_next_date_val
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        result = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.deleted == False
                )
            )
        )
        templates = result.scalars().all()
        
        res_list = []
        for t in templates:
            last_done, nd = await get_template_next_date_val(session, t, today)
            res_list.append({
                "id": t.id,
                "title": t.title,
                "points": t.points,
                "periodicity": t.periodicity,
                "period_days": t.period_days,
                "start_date": str(t.start_date) if t.start_date else None,
                "last_completed": last_done.isoformat() if last_done else None,
                "next_execution": nd.isoformat() if nd else None
            })
    return res_list

class CreateTemplateRequest(BaseModel):
    title: str
    points: int = 1
    periodicity: str = "daily"
    period_days: Optional[int] = None
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
        from bot.handlers.base import get_house_today_date, get_partner_user, bot
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        import json
        
        if not start_date:
            start_date = await get_house_today_date(session)
            
        p_days = req.period_days
        if req.periodicity == "every_x_days" and not p_days:
            p_days = 30
            
        partner = await get_partner_user(session, user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=user.id,
                action_type="create_template",
                data_payload=json.dumps({
                    "title": clean_title,
                    "points": req.points,
                    "periodicity": req.periodicity,
                    "period_days": p_days
                })
            )
            session.add(pending)
            await session.commit()
            await session.refresh(pending)
            
            p_label = req.periodicity
            if req.periodicity == "daily":
                p_label = "каждый день"
            elif req.periodicity == "weekly":
                p_label = "раз в неделю"
            elif req.periodicity == "twice_weekly":
                p_label = "2 раза в неделю"
            elif req.periodicity == "monthly":
                p_label = "раз в месяц"
            elif req.periodicity == "twice_monthly":
                p_label = "2 раза в месяц"
            elif req.periodicity == "quarterly":
                p_label = "раз в квартал"
            elif req.periodicity == "once":
                p_label = "один раз"
            elif req.periodicity == "every_x_days":
                p_label = f"каждые {p_days} дней"
                
            partner_text = (
                f"🔔 *Согласование новой задачи!*\n\n"
                f"*{user.display_name or user.username or 'Партнёр'}* хочет добавить домашнюю задачу:\n"
                f"📋 *Название:* {clean_title}\n"
                f"🍪 *Награда:* {req.points} печенек\n"
                f"📅 *Цикл:* {p_label}\n\n"
                f"Одобряете добавление?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            return {"ok": True, "pending": True, "message": "⏳ Запрос отправлен на согласование партнёру!"}
        else:
            tmpl = TaskTemplate(
                house_id=ACTIVE_HOUSE_ID,
                title=clean_title,
                points=req.points,
                periodicity=req.periodicity,
                period_days=p_days,
                start_date=start_date,
                deleted=False
            )
            session.add(tmpl)
            await session.commit()
            await session.refresh(tmpl)
            return {"ok": True, "id": tmpl.id, "title": tmpl.title}

@app.put("/api/chores/templates/{template_id}")
async def update_chore_template(template_id: int, req: CreateTemplateRequest, user: User = Depends(get_current_user)):
    try:
        start_date = datetime.strptime(req.start_date, "%Y-%m-%d").date() if req.start_date else None
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid start_date format (YYYY-MM-DD)")
        
    p_days = req.period_days
    if req.periodicity == "every_x_days" and not p_days:
        p_days = 30

    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, template_id)
        if not tmpl or tmpl.house_id != ACTIVE_HOUSE_ID:
            raise HTTPException(status_code=404, detail="Template not found")
            
        tmpl.title = req.title
        tmpl.points = req.points
        tmpl.periodicity = req.periodicity
        tmpl.period_days = p_days
        if start_date:
            tmpl.start_date = start_date
            
        await session.commit()
    return {"ok": True}

@app.delete("/api/chores/templates/{template_id}")
async def delete_chore_template(template_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        from bot.handlers.base import get_partner_user, bot
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        import json
        
        tmpl = await session.get(TaskTemplate, template_id)
        if not tmpl or tmpl.house_id != ACTIVE_HOUSE_ID:
            raise HTTPException(status_code=404, detail="Template not found")
            
        partner = await get_partner_user(session, user.id)
        if partner:
            pending = PendingAction(
                house_id=ACTIVE_HOUSE_ID,
                initiator_id=user.id,
                action_type="delete_template",
                data_payload=json.dumps({
                    "template_id": template_id
                })
            )
            session.add(pending)
            await session.commit()
            await session.refresh(pending)
            
            partner_text = (
                f"🔔 *Согласование удаления задачи!*\n\n"
                f"*{user.display_name or user.username or 'Партнёр'}* хочет удалить домашнюю задачу:\n"
                f"🗑 *{tmpl.title}*\n\n"
                f"Одобряете удаление?"
            )
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_act:{pending.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_act:{pending.id}")
            )
            try:
                await bot.send_message(chat_id=partner.telegram_id, text=partner_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send approval message to partner: {e}")
            return {"ok": True, "pending": True, "message": "⏳ Запрос на удаление отправлен партнёру!"}
        else:
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
        if not inst:
            raise HTTPException(status_code=404, detail="Task not found")
            
        tmpl = await session.get(TaskTemplate, inst.template_id)
        if not tmpl or tmpl.house_id != user.house_id:
            raise HTTPException(status_code=403, detail="Access denied")
            
        # Must be claimed by user OR must be free
        if inst.done_by_user_id != user.id and inst.status != "free":
            raise HTTPException(status_code=403, detail="Access denied")
            
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
        if not inst:
            raise HTTPException(status_code=404, detail="Task not found")
            
        tmpl = await session.get(TaskTemplate, inst.template_id)
        if not tmpl or tmpl.house_id != user.house_id:
            raise HTTPException(status_code=403, detail="Access denied")
            
        if inst.done_by_user_id != user.id and inst.status != "free":
            raise HTTPException(status_code=403, detail="Access denied")
            
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

@app.post("/api/chores/templates/{template_id}/shift")
async def shift_chore_template(template_id: int, req: ShiftRequest, user: User = Depends(get_current_user)):
    try:
        new_date = datetime.strptime(req.new_date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format (YYYY-MM-DD)")
        
    async with AsyncSessionLocal() as session:
        tmpl = await session.get(TaskTemplate, template_id)
        if not tmpl or tmpl.house_id != user.house_id:
            raise HTTPException(status_code=404, detail="Template not found")
            
        tmpl.start_date = new_date
        
        # Update any active instances
        from sqlalchemy import update
        await session.execute(
            update(TaskInstance)
            .where(
                and_(
                    TaskInstance.template_id == tmpl.id,
                    TaskInstance.status.in_(["free", "in_progress", "shifted"])
                )
            )
            .values(date=new_date)
        )
        await session.commit()
    return {"ok": True}


# ── Nudge and Archives Endpoints ──────────────────────────────────────────────

NUDGE_PHRASES = [
    "Домовой жалуется на беспорядок! Тут плачет без внимания: <b>{task_title}</b> 🥺",
    "Печеньки 🍪 сами себя не заработают! Тебя ждет отличный контракт: <b>{task_title}</b>",
    "Кажется, кто-то очень хочет, чтобы эта задача решилась. Герой, твой выход: <b>{task_title}</b> 🦸‍♂️",
    "Министерство уюта напоминает! Открыта горячая вакансия на дело: <b>{task_title}</b> 🔥",
    "Освободилось немного времени? Идеальный момент, чтобы закрыть: <b>{task_title}</b> ✨"
]
nudge_cache = {}

@app.post("/api/house/tasks/{instance_id}/nudge")
async def nudge_house_task(instance_id: int, user: User = Depends(get_current_user)):
    from bot.handlers.base import get_house_today_date, bot
    import random
    
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        if nudge_cache.get(instance_id) == today:
            raise HTTPException(status_code=429, detail="Намек уже отправлен")
            
        inst = await session.get(TaskInstance, instance_id)
        if not inst:
            raise HTTPException(status_code=404, detail="Task not found")
            
        tmpl = await session.get(TaskTemplate, inst.template_id)
        if not tmpl or tmpl.house_id != user.house_id:
            raise HTTPException(status_code=403, detail="Access denied")
            
        phrase = random.choice(NUDGE_PHRASES).format(task_title=tmpl.title)
        nudge_cache[instance_id] = today
        
        result = await session.execute(
            select(User).where(
                and_(
                    User.house_id == user.house_id,
                    User.id != user.id
                )
            )
        )
        others = result.scalars().all()
        
        for other in others:
            try:
                await bot.send_message(
                    chat_id=other.telegram_id,
                    text=f"🔔 *Намек от {user.display_name or user.username}*\n\n{phrase}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to send nudge to {other.telegram_id}: {e}")
                
    return {"ok": True}

@app.get("/api/archive/chores")
async def get_chores_archive(date: Optional[str] = None, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        query = select(Completion, User, TaskInstance, TaskTemplate).join(
            User, Completion.user_id == User.id
        ).join(
            TaskInstance, Completion.task_instance_id == TaskInstance.id
        ).join(
            TaskTemplate, TaskInstance.template_id == TaskTemplate.id
        ).where(TaskTemplate.house_id == ACTIVE_HOUSE_ID)
        
        if date:
            from sqlalchemy import cast
            parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
            query = query.where(cast(Completion.created_at, Date) == parsed_date)
            
        query = query.order_by(Completion.created_at.desc())
        result = await session.execute(query)
        rows = result.all()
        
    return [
        {
            "id": comp.id,
            "user": usr.display_name or usr.username or "Участник",
            "title": tmpl.title,
            "points": comp.points or tmpl.points,
            "date": comp.created_at.isoformat(),
        }
        for comp, usr, inst, tmpl in rows
    ]

@app.get("/api/archive/tasks")
async def get_tasks_archive(date: Optional[str] = None, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        query = select(PersonalTask).where(and_(
            PersonalTask.user_id == user.id,
            PersonalTask.is_completed == True,
            PersonalTask.is_deleted == False
        ))
        if date:
            parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
            query = query.where(PersonalTask.date_execution == parsed_date)
            
        query = query.order_by(PersonalTask.date_execution.desc(), PersonalTask.id.desc())
        result = await session.execute(query)
        tasks = result.scalars().all()
    return [
        {
            "id": t.id,
            "text": t.text,
            "date": str(t.date_execution),
            "recurrence": t.recurrence,
        }
        for t in tasks
    ]

@app.post("/api/archive/tasks/{task_id}/restore")
async def restore_task_from_archive(task_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        from bot.handlers.base import get_house_today_date
        today = await get_house_today_date(session)
        old = await session.get(PersonalTask, task_id)
        if not old or old.user_id != user.id:
            raise HTTPException(status_code=404, detail="Task not found")
            
        old.is_completed = False
        old.date_execution = today
        await session.commit()
    return {"ok": True}

@app.post("/api/archive/chores/{completion_id}/restore")
async def restore_chore_from_archive(completion_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        comp = await session.get(Completion, completion_id)
        if not comp or comp.user_id != user.id:
            raise HTTPException(status_code=404, detail="Completion record not found")
            
        inst = await session.get(TaskInstance, comp.task_instance_id)
        if not inst:
            raise HTTPException(status_code=404, detail="Task instance not found")
            
        inst.status = "in_progress"
        inst.done_by_user_id = user.id
        inst.done_at = None
        
        await session.delete(comp)
        await session.commit()
    return {"ok": True}

@app.get("/api/archive/purchases")
async def get_purchases_archive(page: int = 0, limit: int = 10, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RewardPurchase, User)
            .join(User, RewardPurchase.user_id == User.id)
            .where(User.house_id == ACTIVE_HOUSE_ID)
            .order_by(RewardPurchase.created_at.desc())
            .offset(page * limit)
            .limit(limit)
        )
        rows = result.all()
    return [
        {
            "id": p.id,
            "user": usr.display_name or usr.username or "Участник",
            "reward_title": p.reward_title,
            "price": p.price,
            "date": p.created_at.isoformat(),
            "status": p.status
        }
        for p, usr in rows
    ]


@app.get("/api/archive/shopping")
async def get_shopping_archive(page: int = 0, limit: int = 10, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ShoppingItem, User)
            .join(User, ShoppingItem.user_id == User.id)
            .where(and_(
                ShoppingItem.house_id == ACTIVE_HOUSE_ID,
                ShoppingItem.is_bought == True,
                ShoppingItem.is_deleted == False
            ))
            .order_by(ShoppingItem.bought_at.desc(), ShoppingItem.id.desc())
            .offset(page * limit)
            .limit(limit)
        )
        rows = result.all()
    return [
        {
            "id": item.id,
            "item_name": item.item_name,
            "price": item.price,
            "user": usr.display_name or usr.username or "Участник",
            "date": item.bought_at.isoformat() if item.bought_at else "",
        }
        for item, usr in rows
    ]


@app.on_event("startup")
async def startup_db_cleanup():
    from bot.handlers.base import ALLOWED_TELEGRAM_IDS, ACTIVE_HOUSE_ID
    async with AsyncSessionLocal() as session:
        # 1. Clean unauthorized users
        result = await session.execute(select(User))
        users = result.scalars().all()
        for u in users:
            if u.telegram_id not in ALLOWED_TELEGRAM_IDS:
                logger.info(f"Removing unauthorized user {u.username} (id: {u.telegram_id}) from house {ACTIVE_HOUSE_ID}")
                await session.execute(delete(Completion).where(Completion.user_id == u.id))
                await session.execute(delete(RewardPurchase).where(RewardPurchase.user_id == u.id))
                await session.execute(delete(PersonalTask).where(PersonalTask.user_id == u.id))
                await session.delete(u)
        
        # 2. Fix cooking template (if title matches 'готовка' or 'готовить' case-insensitive and points is 0)
        tmpl_res = await session.execute(
            select(TaskTemplate).where(
                and_(
                    TaskTemplate.house_id == ACTIVE_HOUSE_ID,
                    TaskTemplate.points == 0
                )
            )
        )
        templates = tmpl_res.scalars().all()
        for t in templates:
            if "готов" in t.title.lower():
                logger.info(f"Updating points of task template '{t.title}' to 15 (was 0)")
                t.points = 15
                
        await session.commit()





class SpawnChoreRequest(BaseModel):
    template_id: int

@app.post("/api/house/tasks/spawn")
async def spawn_chore_instance(req: SpawnChoreRequest, user: User = Depends(get_current_user)):
    from bot.handlers.base import get_house_today_date
    async with AsyncSessionLocal() as session:
        today = await get_house_today_date(session)
        tmpl = await session.get(TaskTemplate, req.template_id)
        if not tmpl or tmpl.house_id != user.house_id:
            raise HTTPException(status_code=404, detail="Template not found")
            
        # Check if already has free/in_progress instance today
        exists = await session.scalar(
            select(TaskInstance).where(
                and_(
                    TaskInstance.template_id == tmpl.id,
                    TaskInstance.date == today,
                    TaskInstance.status.in_(["free", "in_progress"])
                )
            )
        )
        if not exists:
            inst = TaskInstance(
                template_id=tmpl.id,
                date=today,
                status="free",
                priority=0
            )
            session.add(inst)
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
