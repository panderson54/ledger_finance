"""
Flask application factory
"""
import logging
import os
import sqlite3
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from authlib.integrations.flask_client import OAuth
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Load environment variables
load_dotenv()

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

GOOGLE_DISCOVERY_URL = 'https://accounts.google.com/.well-known/openid-configuration'

logger = logging.getLogger(__name__)


@event.listens_for(Engine, "connect")
def _set_sqlite_wal(dbapi_conn, _):
    """Enable WAL mode for SQLite to support concurrent Gunicorn workers."""
    if isinstance(dbapi_conn, sqlite3.Connection):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")


def _configure_logging(base_dir):
    """Set up rotating file handlers for app and error logs."""
    log_dir = os.path.join(base_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s')

    app_handler = RotatingFileHandler(
        os.path.join(log_dir, 'app.log'), maxBytes=5 * 1024 * 1024, backupCount=3
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(fmt)

    error_handler = RotatingFileHandler(
        os.path.join(log_dir, 'error.log'), maxBytes=5 * 1024 * 1024, backupCount=3
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger()
    if not root.handlers:  # avoid duplicate handlers when create_app is called multiple times (tests)
        root.setLevel(logging.INFO)
        root.addHandler(app_handler)
        root.addHandler(error_handler)
        root.addHandler(stderr_handler)

    # Keep SQLAlchemy and Werkzeug noise down
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)


def _datefmt(dt, fmt='%b %-d, %Y'):
    """strftime wrapper that handles %-d (no-pad day) on Windows and Linux."""
    return dt.strftime(fmt.replace('%-d', '{_d_}')).replace('{_d_}', str(dt.day))


def _configure_auth(app, is_production):
    """Load Google OAuth + allowlist settings from the environment — the only place they can be set."""
    from app.auth_service import parse_allowed_emails

    app.config['GOOGLE_CLIENT_ID'] = os.getenv('GOOGLE_CLIENT_ID', '')
    app.config['GOOGLE_CLIENT_SECRET'] = os.getenv('GOOGLE_CLIENT_SECRET', '')
    app.config['GOOGLE_REDIRECT_URI'] = os.getenv('GOOGLE_REDIRECT_URI', '')
    app.config['ALLOWED_EMAILS'] = parse_allowed_emails(os.getenv('LEDGER_ALLOWED_EMAILS'))

    required = {  # env var -> config key it populates
        'GOOGLE_CLIENT_ID': 'GOOGLE_CLIENT_ID',
        'GOOGLE_CLIENT_SECRET': 'GOOGLE_CLIENT_SECRET',
        'LEDGER_ALLOWED_EMAILS': 'ALLOWED_EMAILS',
    }
    if is_production:
        # Behind a TLS-terminating proxy, url_for(_external=True) would build an
        # http:// callback that Google rejects, so production must spell it out.
        required['GOOGLE_REDIRECT_URI'] = 'GOOGLE_REDIRECT_URI'
    missing = ', '.join(env for env, key in required.items() if not app.config[key])
    if missing:
        if is_production:
            raise RuntimeError(f'Google sign-in is not configured; set {missing}.')
        logger.warning('Google sign-in is not fully configured (missing %s); nobody can log in.', missing)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        # Lax, not Strict: the session cookie must ride along on Google's
        # top-level redirect back to the callback or the OAuth state is lost.
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=is_production,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )

    # Per app rather than a module-level OAuth(): Authlib caches each client with
    # the config of the first app that created it.
    app.extensions['google_oauth'] = OAuth(app).register(
        'google',
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256'},
    )


def create_app(config_name='development'):
    """
    Application factory pattern
    """
    app = Flask(__name__)

    # Ensure data directory exists
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    default_db_url = f'sqlite:///{os.path.join(data_dir, "finance.db")}'

    # Configuration
    _DEV_KEY = 'dev-secret-key-change-this'
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', _DEV_KEY)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', default_db_url)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Configure logging
    _configure_logging(base_dir)

    is_production = os.getenv('FLASK_ENV') == 'production'
    if app.config['SECRET_KEY'] == _DEV_KEY:
        if is_production:
            raise RuntimeError(
                'SECRET_KEY must be set in production. '
                'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        logger.warning(
            'SECRET_KEY is not set — using insecure dev default. '
            'Add SECRET_KEY to your .env file before deploying.'
        )

    _configure_auth(app, is_production)

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    # Registered before CSRFProtect so the auth check is the first before_request hook.
    from app import access_control
    access_control.register(app)
    csrf.init_app(app)

    # Register Jinja2 filters
    app.jinja_env.filters['datefmt'] = _datefmt

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    # Log all non-static requests; warn on 4xx/5xx
    @app.after_request
    def log_request(response):
        if not request.path.startswith('/static'):
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            logging.getLogger('app.request').log(
                level, '%s %s %d', request.method, request.path, response.status_code
            )
        return response

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception('Unhandled exception on %s %s', request.method, request.path)
        return 'Internal Server Error', 500

    return app
