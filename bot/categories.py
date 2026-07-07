"""
Общая таксономия рубрик новостей.
Используется на всех этапах пайплдайна: process_ai (генерация/валидация category),
bot_posting (рендер эмодзи-рубрики и заголовков дайджеста).
"""

# Эмодзи для каждой рубрики (показывается перед заголовком новости)
CATEGORY_EMOJI = {
    "politics": "🏛",
    "economy": "💰",
    "sport": "⚽",
    "society": "👥",
    "incidents": "🚨",
    "culture": "🎭",
    "tourism": "🏖",
    "other": "📌",
}

# Человекочитаемые названия рубрик (для заголовков секций дайджеста)
CATEGORY_LABEL_RU = {
    "politics": "Политика",
    "economy": "Экономика",
    "sport": "Спорт",
    "society": "Общество",
    "incidents": "Происшествия",
    "culture": "Культура",
    "tourism": "Туризм и жизнь",
    "other": "Разное",
}

# Порядок вывода рубрик в дайджесте (важное сверху)
CATEGORY_ORDER = [
    "incidents", "politics", "economy", "society", "sport", "tourism", "culture", "other",
]

DEFAULT_CATEGORY = "other"
CATEGORIES = list(CATEGORY_EMOJI.keys())


def normalize_category(value: str) -> str:
    """Приводит значение категории к одному из допустимых, иначе — 'other'."""
    if not value:
        return DEFAULT_CATEGORY
    v = str(value).strip().lower()
    return v if v in CATEGORY_EMOJI else DEFAULT_CATEGORY


def category_emoji(value: str) -> str:
    """Эмодзи рубрики (с безопасным фолбэком)."""
    return CATEGORY_EMOJI.get(normalize_category(value), CATEGORY_EMOJI[DEFAULT_CATEGORY])


def category_label(value: str) -> str:
    """Русское название рубрики (с безопасным фолбэком)."""
    return CATEGORY_LABEL_RU.get(normalize_category(value), CATEGORY_LABEL_RU[DEFAULT_CATEGORY])
