# -*- coding: utf-8 -*-
"""Подключение к моделям по API. Чат помощниц — второй этап."""
from .config import PRESETS, PublicConfig, clean_config, public_config
from .providers import check_connection, list_models
