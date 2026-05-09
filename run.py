"""
Application entry point
"""
import os
from app import create_app, db
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric

app = create_app()


@app.shell_context_processor
def make_shell_context():
    """
    Make database models available in Flask shell
    """
    return {
        'db': db,
        'Account': Account,
        'AccountSnapshot': AccountSnapshot,
        'SpendingEntry': SpendingEntry,
        'CalculatedMetric': CalculatedMetric
    }


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true')
    app.run(debug=debug, host='0.0.0.0', port=int(os.getenv('PORT', 5001)))
