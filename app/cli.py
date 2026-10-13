import logging

import click
from flask.cli import with_appcontext
from flask_migrate import stamp, upgrade
from sqlalchemy import inspect

from app import db, mcp_token_service

logger = logging.getLogger(__name__)


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Create the schema on an empty database, otherwise apply pending migrations."""
    # The first migration alters tables that predate Alembic, so the migration
    # chain can't build a database from nothing; the models are the full schema.
    # Checks for `accounts` rather than any table: a failed first upgrade leaves
    # an empty alembic_version table behind.
    if 'accounts' not in inspect(db.engine).get_table_names():
        db.create_all()
        stamp()
        logger.info('Created new database schema and stamped it at the latest migration')
        click.echo('Created database schema.')
    else:
        upgrade()
        click.echo('Database is up to date.')


@click.command('mcp-token')
@click.argument('name')
def mcp_token_command(name):
    """Generate an agent bearer token for NAME (e.g. muse) and its LEDGER_MCP_TOKENS entry."""
    name = name.strip().lower()
    if not mcp_token_service.is_valid_client_name(name):
        raise click.BadParameter('use lowercase letters, digits, "-" or "_" (max 64).', param_hint='NAME')
    token = mcp_token_service.generate_token()
    click.echo(f'Bearer token for {name} — give this to the agent; it is not stored anywhere:')
    click.echo(f'  {token}')
    click.echo('')
    click.echo('Add this entry to LEDGER_MCP_TOKENS (comma-separate multiple agents), then restart:')
    click.echo(f'  {name}:{mcp_token_service.hash_token(token)}')


def register(app):
    app.cli.add_command(init_db_command)
    app.cli.add_command(mcp_token_command)
