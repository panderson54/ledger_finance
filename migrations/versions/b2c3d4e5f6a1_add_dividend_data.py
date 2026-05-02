"""add dividend_data table

Revision ID: b2c3d4e5f6a1
Revises: f1e2d3c4b5a6
Create Date: 2026-05-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a1'
down_revision = 'f1e2d3c4b5a6'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'dividend_data' not in sa.inspect(bind).get_table_names():
        op.create_table(
            'dividend_data',
            sa.Column('id',                 sa.Integer(),      nullable=False),
            sa.Column('ticker',             sa.String(20),     nullable=False),
            sa.Column('annual_yield',       sa.Numeric(8, 6),  nullable=True),
            sa.Column('dividend_per_share', sa.Numeric(10, 4), nullable=True),
            sa.Column('frequency',          sa.String(20),     nullable=True),
            sa.Column('payer_type',         sa.String(30),     nullable=True),
            sa.Column('is_dividend_payer',  sa.Boolean(),      nullable=False, server_default='1'),
            sa.Column('tax_treatment',      sa.String(20),     nullable=True),
            sa.Column('source_notes',       sa.Text(),         nullable=True),
            sa.Column('last_fetched_at',    sa.DateTime(),     nullable=True),
            sa.Column('created_at',         sa.DateTime(),     nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('ticker'),
        )


def downgrade():
    op.drop_table('dividend_data')
