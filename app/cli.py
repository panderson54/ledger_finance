import logging

import click
from flask.cli import with_appcontext
from flask_migrate import stamp, upgrade
from sqlalchemy import inspect

from app import db

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


def register(app):
    app.cli.add_command(init_db_command)
