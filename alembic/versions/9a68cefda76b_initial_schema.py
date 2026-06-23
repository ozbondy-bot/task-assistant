"""initial_schema

Revision ID: 9a68cefda76b
Revises: 
Create Date: 2026-06-23 19:02:29.945689

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a68cefda76b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. houses
    op.create_table(
        'houses',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('join_code', sa.String(length=50), nullable=False),
        sa.Column('timezone', sa.String(length=50), nullable=False, server_default='Europe/Moscow'),
        sa.Column('last_summary_date', sa.Date(), nullable=True)
    )
    op.create_index(op.f('ix_houses_join_code'), 'houses', ['join_code'], unique=True)

    # 2. users
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('full_name', sa.String(length=255), nullable=True),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('house_id', sa.Integer(), sa.ForeignKey('houses.id', ondelete='SET NULL'), nullable=True),
        sa.Column('is_house_owner', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('points', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_today_message_id', sa.Integer(), nullable=True)
    )
    op.create_index(op.f('ix_users_telegram_id'), 'users', ['telegram_id'], unique=True)

    # 3. personal_tasks
    op.create_table(
        'personal_tasks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('date_execution', sa.Date(), nullable=False),
        sa.Column('is_completed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('category', sa.String(length=50), server_default='inbox', nullable=False),
        sa.Column('recurrence', sa.String(length=50), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True)
    )

    # 4. shopping_items
    op.create_table(
        'shopping_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('house_id', sa.Integer(), sa.ForeignKey('houses.id', ondelete='SET NULL'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('item_name', sa.String(length=255), nullable=False),
        sa.Column('price', sa.Integer(), server_default='0', nullable=False),
        sa.Column('priority', sa.String(length=50), server_default='normal', nullable=False),
        sa.Column('is_bought', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('bought_at', sa.DateTime(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False)
    )

    # 5. task_templates
    op.create_table(
        'task_templates',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('house_id', sa.Integer(), sa.ForeignKey('houses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('points', sa.Integer(), server_default='1', nullable=False),
        sa.Column('periodicity', sa.String(length=50), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=True),
        sa.Column('weekday', sa.Integer(), nullable=True),
        sa.Column('month_day', sa.Integer(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('deleted', sa.Boolean(), server_default='false', nullable=False)
    )

    # 6. task_instances
    op.create_table(
        'task_instances',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('task_templates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='free', nullable=False),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('done_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('done_at', sa.DateTime(), nullable=True)
    )

    # 7. completions
    op.create_table(
        'completions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('task_instance_id', sa.Integer(), sa.ForeignKey('task_instances.id', ondelete='CASCADE'), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now())
    )

    # 8. rewards
    op.create_table(
        'rewards',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('house_id', sa.Integer(), sa.ForeignKey('houses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('price', sa.Integer(), nullable=False)
    )

    # 9. reward_purchases
    op.create_table(
        'reward_purchases',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reward_title', sa.String(length=255), nullable=False),
        sa.Column('price', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='purchased', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('used_at', sa.DateTime(), nullable=True)
    )

    # 10. pending_actions
    op.create_table(
        'pending_actions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('house_id', sa.Integer(), sa.ForeignKey('houses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('initiator_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action_type', sa.String(length=255), nullable=False),
        sa.Column('data_payload', sa.Text(), nullable=False)
    )


def downgrade() -> None:
    op.drop_table('pending_actions')
    op.drop_table('reward_purchases')
    op.drop_table('rewards')
    op.drop_table('completions')
    op.drop_table('task_instances')
    op.drop_table('task_templates')
    op.drop_table('shopping_items')
    op.drop_table('personal_tasks')
    op.drop_table('users')
    op.drop_table('houses')
