from flask import g, jsonify

from app.access_control import agent_token_required
from app.routes import main_bp


@main_bp.route('/agent/whoami')
@agent_token_required
def agent_whoami():
    """Lets an agent confirm its bearer token is accepted."""
    return jsonify({'agent': g.agent_name})
