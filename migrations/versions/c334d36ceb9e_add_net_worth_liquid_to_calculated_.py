"""add net_worth_liquid to calculated_metrics

Revision ID: c334d36ceb9e
Revises: ea70ad5d7726
Create Date: 2026-04-18 12:21:50.996839

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c334d36ceb9e'
down_revision = 'ea70ad5d7726'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('calculated_metrics', schema=None) as batch_op:
        batch_op.add_column(sa.Column('net_worth_liquid', sa.Numeric(precision=12, scale=2), nullable=True))


def downgrade():
    with op.batch_alter_table('calculated_metrics', schema=None) as batch_op:
        batch_op.drop_column('net_worth_liquid')
