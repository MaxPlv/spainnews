"""
Единая точка резолва путей для файлов состояния бота (JSON-хранилища).

По умолчанию state-файлы лежат в корне проекта — поведение не меняется
для локальной разработки. На Railway (и любом хостинге с эфемерной ФС)
нужно примонтировать persistent volume и указать DATA_DIR на него —
иначе история дублей/буфер дайджеста/кэш стираются при каждом деплое.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
