"""add ticker_classifications table

Revision ID: e5f6a1b2c3d4
Revises: c3d4e5f6a1b2
Create Date: 2026-04-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a1b2c3d4'
down_revision = 'c3d4e5f6a1b2'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'ticker_classifications' not in sa.inspect(bind).get_table_names():
        op.create_table(
            'ticker_classifications',
            sa.Column('id',              sa.Integer(),  nullable=False),
            sa.Column('ticker',          sa.String(20), nullable=False),
            sa.Column('asset_class',     sa.String(20), nullable=False),
            sa.Column('market_cap_tilt', sa.String(10), nullable=True),
            sa.Column('sector_weights',  sa.Text(),     nullable=False),
            sa.Column('source',          sa.String(20), nullable=True, server_default='claude'),
            sa.Column('classified_at',   sa.DateTime(), nullable=True),
            sa.Column('created_at',      sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('ticker', name='uq_ticker_classification'),
        )


def downgrade():
    op.drop_table('ticker_classifications')
