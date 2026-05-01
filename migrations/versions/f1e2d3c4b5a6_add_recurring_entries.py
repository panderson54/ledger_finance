"""add recurring_entries table

Revision ID: f1e2d3c4b5a6
Revises: c334d36ceb9e
Create Date: 2026-04-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1e2d3c4b5a6'
down_revision = 'c334d36ceb9e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recurring_entries',
        sa.Column('id',            sa.Integer(),      nullable=False),
        sa.Column('account_name',  sa.String(100),    nullable=False),
        sa.Column('amount',        sa.Numeric(10, 2), nullable=False),
        sa.Column('entry_type',    sa.String(20),     nullable=False),
        sa.Column('notes',         sa.Text(),         nullable=True),
        sa.Column('is_active',     sa.Boolean(),      nullable=False, server_default='1'),
        sa.Column('display_order', sa.Integer(),      nullable=False, server_default='0'),
        sa.Column('created_at',    sa.DateTime(),     nullable=True),
        sa.Column('updated_at',    sa.DateTime(),     nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('recurring_entries')
