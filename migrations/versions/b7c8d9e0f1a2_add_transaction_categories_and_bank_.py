"""add transaction_categories and bank_transactions tables

Revision ID: b7c8d9e0f1a2
Revises: 545c7df15ab7
Create Date: 2026-08-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import table, column


# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = '545c7df15ab7'
branch_labels = None
depends_on = None


# Starter categories drawn from real Schwab Bank / Wealthfront Cash statement
# samples used to design this feature — the user manages this list afterward.
SEED_CATEGORIES = [
    # title, description, kind, display_order
    ('Paycheck/Salary', 'Regular payroll deposits from an employer.', 'income', 0),
    ('Interest Income', 'Interest paid by the bank on account balances.', 'income', 1),
    ('Other Income', 'Deposits from external sources not covered by another income category.', 'income', 2),
    ('Utilities', 'Recurring home utility bills: electric, gas, internet, cell phone, water, garbage.', 'expense', 10),
    ('Mortgage/Rent', 'Monthly mortgage or rent payment.', 'expense', 11),
    ('Chase Card', 'Payments to a Chase credit card.', 'expense', 12),
    ('Amex Card', 'Payments to an American Express credit card.', 'expense', 13),
    ('Insurance', 'Insurance premiums (auto, pet, life, etc.), excluding health premiums withheld from pay.', 'expense', 14),
    ('Groceries', 'Grocery store and supermarket purchases.', 'expense', 15),
    ('Uncategorized', 'Fallback for transactions that could not be confidently categorized (e.g. checks with no payee detail).', 'expense', 99),
    ('Internal Transfer', 'Money moved between the user\'s own accounts — never counted as income or expense.', 'transfer', 20),
]


def upgrade():
    op.create_table(
        'transaction_categories',
        sa.Column('id',            sa.Integer(),   nullable=False),
        sa.Column('title',         sa.String(100), nullable=False),
        sa.Column('description',   sa.Text(),      nullable=True),
        sa.Column('kind',          sa.String(20),  nullable=False),
        sa.Column('is_active',     sa.Boolean(),   nullable=False, server_default='1'),
        sa.Column('display_order', sa.Integer(),   nullable=False, server_default='0'),
        sa.Column('created_at',    sa.DateTime(),  nullable=True),
        sa.Column('updated_at',    sa.DateTime(),  nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title', name='uq_transaction_categories_title'),
    )

    op.create_table(
        'bank_transactions',
        sa.Column('id',               sa.Integer(),      nullable=False),
        sa.Column('account_id',       sa.Integer(),      nullable=False),
        sa.Column('transaction_date', sa.Date(),         nullable=False),
        sa.Column('month_date',       sa.Date(),         nullable=False),
        sa.Column('description',      sa.Text(),         nullable=False),
        sa.Column('amount',           sa.Numeric(12, 2), nullable=False),
        sa.Column('direction',        sa.String(10),     nullable=False),
        sa.Column('category_id',      sa.Integer(),      nullable=False),
        sa.Column('created_at',       sa.DateTime(),     nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['category_id'], ['transaction_categories.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_bank_transactions_account_month', 'bank_transactions', ['account_id', 'month_date'],
    )

    categories_table = table(
        'transaction_categories',
        column('title', sa.String),
        column('description', sa.Text),
        column('kind', sa.String),
        column('display_order', sa.Integer),
        column('is_active', sa.Boolean),
    )
    op.bulk_insert(categories_table, [
        {'title': title, 'description': description, 'kind': kind, 'display_order': order, 'is_active': True}
        for title, description, kind, order in SEED_CATEGORIES
    ])


def downgrade():
    op.drop_index('ix_bank_transactions_account_month', table_name='bank_transactions')
    op.drop_table('bank_transactions')
    op.drop_table('transaction_categories')
