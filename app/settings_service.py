"""Read access to AppSetting values shared by routes and read-only services."""
import json

from app.models import AppSetting

ALLOCATION_TARGET_KEY = 'allocation_targets'
ALLOCATION_TARGET_DEFAULTS = {'domestic': 70.0, 'international': 15.0, 'bonds': 10.0, 'cash': 5.0}


def get_setting(key: str, default: str = '') -> str:
    """Return the setting's value, or default if absent or empty."""
    row = AppSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else default


def get_float_setting(key: str, default: float) -> float:
    raw = get_setting(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def get_allocation_targets() -> dict:
    """Return {asset_class: target_pct}, falling back to defaults."""
    raw = get_setting(ALLOCATION_TARGET_KEY)
    if not raw:
        return dict(ALLOCATION_TARGET_DEFAULTS)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return dict(ALLOCATION_TARGET_DEFAULTS)
