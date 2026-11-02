"""HTTP API загрузки по ссылке для внешних клиентов.

Окружение настраивается здесь, а не в `__main__`: пакет импортируется раньше
`api.app` при любом способе запуска — `python -m api`, `uvicorn api.app:app`.
"""

import os

from utils.env_files import load_env_files

# Те же файлы, что у бота: иначе прямой запуск API не видит TELEGRAM_TOKEN,
# ADMIN_IDS, DATA_DIR и настройки API из `.secrets/.env`.
load_env_files()
# До первого импорта utils.logger: метка выбирает файл журнала процесса.
os.environ["NUVIO_PROCESS"] = "api"
