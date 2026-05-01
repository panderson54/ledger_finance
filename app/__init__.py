"""
Flask application factory
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()

logger = logging.getLogger(__name__)


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
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-this')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', default_db_url)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Configure logging
    _configure_logging(base_dir)

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)

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

    # Create database tables
    with app.app_context():
        db.create_all()

    return app
