"""normalize ira hsa roth_ira category values

Revision ID: 536c6bf133d4
Revises: d314a7d176e6
Create Date: 2026-04-12 21:50:41.671721

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '536c6bf133d4'
down_revision = 'd314a7d176e6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE accounts SET category = 'ira'      WHERE category = 'IRA'")
    op.execute("UPDATE accounts SET category = 'roth_ira' WHERE category = 'Roth IRA'")
    op.execute("UPDATE accounts SET category = 'hsa'      WHERE category = 'HSA'")


def downgrade():
    op.execute("UPDATE accounts SET category = 'IRA'      WHERE category = 'ira'")
    op.execute("UPDATE accounts SET category = 'Roth IRA' WHERE category = 'roth_ira'")
    op.execute("UPDATE accounts SET category = 'HSA'      WHERE category = 'hsa'")
