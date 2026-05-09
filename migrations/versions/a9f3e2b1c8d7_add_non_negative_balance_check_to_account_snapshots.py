"""add non_negative_balance check to account_snapshots

Revision ID: a9f3e2b1c8d7
Revises: b2c3d4e5f6a1
Create Date: 2026-05-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9f3e2b1c8d7'
down_revision = 'b2c3d4e5f6a1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('account_snapshots', schema=None) as batch_op:
        batch_op.create_check_constraint('non_negative_balance', 'balance >= 0')


def downgrade():
    with op.batch_alter_table('account_snapshots', schema=None) as batch_op:
        batch_op.drop_constraint('non_negative_balance', type_='check')
