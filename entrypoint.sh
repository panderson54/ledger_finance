#!/bin/sh
set -e

flask init-db

exec gunicorn --bind 0.0.0.0:5001 --workers 2 --timeout 300 run:app
