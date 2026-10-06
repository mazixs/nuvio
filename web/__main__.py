"""Точка входа для `python -m web`."""

from web.app import run
from utils.db_worker import shutdown_db_worker

try:
    run()
finally:
    shutdown_db_worker()
