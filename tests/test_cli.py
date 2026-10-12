import sqlite3

import pytest

from app import create_app

HEAD_REVISION = '545c7df15ab7'


@pytest.fixture
def file_db_app(tmp_path, monkeypatch):
    db_path = tmp_path / 'finance.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{db_path}')
    return create_app(), db_path


def _tables(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _revision(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]


def test_empty_database_gets_full_schema_stamped_at_head(file_db_app):
    app, db_path = file_db_app
    result = app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    assert {'accounts', 'account_snapshots', 'holdings', 'dividend_data'} <= _tables(db_path)
    assert _revision(db_path) == HEAD_REVISION


def test_recovers_from_failed_first_upgrade(file_db_app):
    app, db_path = file_db_app
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
    result = app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    assert 'accounts' in _tables(db_path)
    assert _revision(db_path) == HEAD_REVISION


def test_existing_database_is_upgraded_not_recreated(file_db_app):
    app, db_path = file_db_app
    runner = app.test_cli_runner()
    runner.invoke(args=['init-db'])
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('keep', 'me')")
    result = runner.invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    assert 'up to date' in result.output
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM app_settings WHERE key='keep'").fetchone() == ('me',)
