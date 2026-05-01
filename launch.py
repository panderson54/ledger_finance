"""Launcher that injects venv site-packages for environments where the venv is misconfigured."""
import sys
import os

# Inject the venv site-packages into sys.path
venv_site = os.path.join(os.path.dirname(__file__), 'venv', 'Lib', 'site-packages')
if venv_site not in sys.path:
    sys.path.insert(0, venv_site)

# Now run the app
from app import create_app
app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
