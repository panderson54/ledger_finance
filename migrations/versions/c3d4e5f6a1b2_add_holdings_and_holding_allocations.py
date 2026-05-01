"""add holdings and holding_allocations tables

Revision ID: c3d4e5f6a1b2
Revises: a1b2c3d4e5f6
Create Date: 2026-04-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a1b2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'holdings',
        sa.Column('id',           sa.Integer(),     nullable=False),
        sa.Column('account_id',   sa.Integer(),     nullable=False),
        sa.Column('ticker',       sa.String(20),    nullable=False),
        sa.Column('name',         sa.String(100),   nullable=True),
        sa.Column('shares',       sa.Numeric(18, 8), nullable=False),
        sa.Column('last_price',   sa.Numeric(12, 4), nullable=True),
        sa.Column('last_fetched', sa.DateTime(),    nullable=True),
        sa.Column('is_active',    sa.Boolean(),     nullable=True, server_default='1'),
        sa.Column('cap_class',    sa.String(10),    nullable=True),
        sa.Column('created_at',   sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'holding_allocations',
        sa.Column('id',          sa.Integer(),    nullable=False),
        sa.Column('holding_id',  sa.Integer(),    nullable=False),
        sa.Column('asset_class', sa.String(20),   nullable=False),
        sa.Column('percentage',  sa.Numeric(5, 2), nullable=False),
        sa.CheckConstraint('percentage >= 0 AND percentage <= 100', name='ha_valid_pct'),
        sa.ForeignKeyConstraint(['holding_id'], ['holdings.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('holding_allocations')
    op.drop_table('holdings')
