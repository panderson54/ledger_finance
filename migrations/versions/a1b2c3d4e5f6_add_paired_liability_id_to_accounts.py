"""add paired_liability_id to accounts

Revision ID: a1b2c3d4e5f6
Revises: 536c6bf133d4
Create Date: 2026-04-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '536c6bf133d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paired_liability_id', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('paired_liability_id')
