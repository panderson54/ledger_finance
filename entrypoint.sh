#!/bin/sh
set -e

flask db upgrade

exec gunicorn --bind 0.0.0.0:5001 --workers 2 run:app
