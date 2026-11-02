"""Точка входа для `python -m api`."""

from api.app import run
from utils.db_worker import shutdown_db_worker

try:
    run()
finally:
    shutdown_db_worker()
