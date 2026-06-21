import os
from datetime import datetime, timezone as dt_timezone
from sqlalchemy import Column, Integer, String, Date, DateTime, Boolean, ForeignKey, Text, BigInteger
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Use asyncpg for async operations
if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Remove pgbouncer param (not compatible with asyncpg)
    ASYNC_DATABASE_URL = ASYNC_DATABASE_URL.replace("?pgbouncer=true", "").replace("&pgbouncer=true", "")
else:
    ASYNC_DATABASE_URL = DATABASE_URL

async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=10
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False
)
Base = declarative_base()


class House(Base):
    __tablename__ = "houses"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    join_code = Column(String, unique=True, index=True, nullable=False)
    timezone = Column(String, nullable=False, default="Europe/Moscow")
    last_summary_date = Column(Date, nullable=True)

    users = relationship("User", back_populates="house")
    templates = relationship("TaskTemplate", back_populates="house")
    rewards = relationship("Reward", back_populates="house")
    shopping_items = relationship("ShoppingItem", back_populates="house")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    house_id = Column(Integer, ForeignKey("houses.id"), nullable=True)
    is_house_owner = Column(Boolean, default=False)
    points = Column(Integer, default=0, nullable=False)
    last_today_message_id = Column(Integer, nullable=True)

    house = relationship("House", back_populates="users")
    personal_tasks = relationship("PersonalTask", back_populates="user")
    completions = relationship("Completion", back_populates="user")
    reward_purchases = relationship("RewardPurchase", back_populates="user")
    claimed_instances = relationship("TaskInstance", back_populates="done_by_user", foreign_keys="[TaskInstance.done_by_user_id]")
    shopping_items = relationship("ShoppingItem", back_populates="user")


class PersonalTask(Base):
    __tablename__ = "personal_tasks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(String, nullable=False)
    date_execution = Column(Date, nullable=False)
    is_completed = Column(Boolean, default=False, nullable=False)
    category = Column(String, default="inbox", nullable=False)
    recurrence = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="personal_tasks")


class ShoppingItem(Base):
    __tablename__ = "shopping_items"
    id = Column(Integer, primary_key=True)
    house_id = Column(Integer, ForeignKey("houses.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    item_name = Column(String, nullable=False)
    price = Column(Integer, default=0, nullable=False)
    priority = Column(String, default="normal", nullable=False)
    is_bought = Column(Boolean, default=False, nullable=False)
    bought_at = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)

    house = relationship("House", back_populates="shopping_items")
    user = relationship("User", back_populates="shopping_items")


class TaskTemplate(Base):
    __tablename__ = "task_templates"
    id = Column(Integer, primary_key=True)
    house_id = Column(Integer, ForeignKey("houses.id"), nullable=False)
    title = Column(String, nullable=False)
    points = Column(Integer, nullable=False, default=1)
    periodicity = Column(String, nullable=False)
    period_days = Column(Integer, nullable=True)
    weekday = Column(Integer, nullable=True)
    month_day = Column(Integer, nullable=True)
    start_date = Column(Date, nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)

    house = relationship("House", back_populates="templates")
    instances = relationship("TaskInstance", back_populates="template")


class TaskInstance(Base):
    __tablename__ = "task_instances"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("task_templates.id"), nullable=False)
    date = Column(Date, nullable=False)
    status = Column(String, nullable=False, default="free")  # 'free', 'in_progress', 'done'
    priority = Column(Integer, nullable=False, default=0)
    done_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    done_at = Column(DateTime, nullable=True)

    template = relationship("TaskTemplate", back_populates="instances")
    done_by_user = relationship("User", foreign_keys=[done_by_user_id], back_populates="claimed_instances")
    completions = relationship("Completion", back_populates="task_instance")


class Completion(Base):
    __tablename__ = "completions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_instance_id = Column(Integer, ForeignKey("task_instances.id"), nullable=False)
    points = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(dt_timezone.utc).replace(tzinfo=None))

    user = relationship("User", back_populates="completions")
    task_instance = relationship("TaskInstance", back_populates="completions")


class Reward(Base):
    __tablename__ = "rewards"
    id = Column(Integer, primary_key=True)
    house_id = Column(Integer, ForeignKey("houses.id"), nullable=False)
    title = Column(String, nullable=False)
    price = Column(Integer, nullable=False)

    house = relationship("House", back_populates="rewards")


class RewardPurchase(Base):
    __tablename__ = "reward_purchases"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reward_title = Column(String, nullable=False)
    price = Column(Integer, nullable=False)
    status = Column(String, default="purchased")
    created_at = Column(DateTime, default=lambda: datetime.now(dt_timezone.utc).replace(tzinfo=None))
    used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="reward_purchases")


class PendingAction(Base):
    __tablename__ = "pending_actions"
    id = Column(Integer, primary_key=True)
    house_id = Column(Integer, ForeignKey("houses.id"), nullable=False)
    initiator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action_type = Column(String, nullable=False)
    data_payload = Column(Text, nullable=False)
