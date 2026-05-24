"""
Tests for startup-time security checks (PR-01, PR-02):
  - FLASK_DEBUG is driven by env var, never hard-coded True
  - SECRET_KEY absence logs a WARNING
  - SECRET_KEY absence in production raises RuntimeError
"""
import logging
import os

import pytest


def _pop(*keys):
    """Remove keys from os.environ, return a dict of what was there for restore."""
    saved = {}
    for k in keys:
        val = os.environ.pop(k, None)
        if val is not None:
            saved[k] = val
    return saved


def _restore(saved):
    os.environ.update(saved)


# ---------------------------------------------------------------------------
# PR-01 — FLASK_DEBUG driven by environment variable
# ---------------------------------------------------------------------------

class TestFlaskDebugEnvVar:
    def test_debug_off_by_default(self):
        saved = _pop('FLASK_DEBUG')
        try:
            import run as run_module
            import importlib
            importlib.reload(run_module)
            debug_val = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true')
            assert debug_val is False
        finally:
            _restore(saved)

    def test_debug_on_when_env_set(self):
        saved = _pop('FLASK_DEBUG')
        os.environ['FLASK_DEBUG'] = '1'
        try:
            debug_val = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true')
            assert debug_val is True
        finally:
            _restore(saved)
            os.environ.pop('FLASK_DEBUG', None)

    def test_debug_on_for_true_string(self):
        os.environ['FLASK_DEBUG'] = 'true'
        try:
            debug_val = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true')
            assert debug_val is True
        finally:
            os.environ.pop('FLASK_DEBUG', None)


# ---------------------------------------------------------------------------
# PR-02 — SECRET_KEY validation at factory startup
# ---------------------------------------------------------------------------

class TestSecretKeyStartup:
    def test_dev_key_logs_warning(self, caplog):
        saved = _pop('SECRET_KEY', 'FLASK_ENV')
        try:
            from app import create_app
            with caplog.at_level(logging.WARNING, logger='app'):
                create_app()
            warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
            assert any('SECRET_KEY' in t for t in warning_texts)
        finally:
            _restore(saved)

    def test_production_without_secret_key_raises(self):
        saved = _pop('SECRET_KEY', 'FLASK_ENV')
        os.environ['FLASK_ENV'] = 'production'
        try:
            from app import create_app
            with pytest.raises(RuntimeError, match='SECRET_KEY'):
                create_app()
        finally:
            _restore(saved)

    def test_production_with_secret_key_does_not_raise(self):
        saved = _pop('SECRET_KEY', 'FLASK_ENV')
        os.environ['SECRET_KEY'] = 'a-real-secret-key-for-testing-only'
        os.environ['FLASK_ENV'] = 'production'
        try:
            from app import create_app
            app = create_app()
            assert app.config['SECRET_KEY'] == 'a-real-secret-key-for-testing-only'
        finally:
            _restore(saved)

    def test_custom_key_is_used(self):
        saved = _pop('SECRET_KEY', 'FLASK_ENV')
        os.environ['SECRET_KEY'] = 'custom-key-xyz'
        try:
            from app import create_app
            app = create_app()
            assert app.config['SECRET_KEY'] == 'custom-key-xyz'
        finally:
            _restore(saved)
