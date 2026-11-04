"""WebUI аналитики.

Окружение настраивается здесь, а не в `__main__`: пакет импортируется раньше
`web.app` при любом способе запуска — `python -m web`, `uvicorn web.app:app`.
"""

from utils.env_files import load_env_files

# Те же файлы, что у бота: иначе прямой запуск не видит WEB_PASSWORD и
# WEB_SECRET_KEY из `.secrets/.env`, и вход в WebUI остаётся закрытым.
load_env_files()
