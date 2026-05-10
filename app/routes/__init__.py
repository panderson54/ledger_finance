from flask import Blueprint

main_bp = Blueprint('main', __name__)

# Import sub-modules AFTER defining main_bp (they import main_bp from here)
from app.routes import helpers, dashboard, accounts, snapshots, spending, holdings, allocation, projections, income, import_export, settings, visualizations  # noqa: F401, E402
