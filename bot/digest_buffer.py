"""
Буфер отложенных (рутинных) новостей для дайджеста.

Срочные новости публикуются сразу отдельными постами, а всё остальное
копится здесь между слотами публикации и уходит одним постом-дайджестом.
Хранилище — pending_digest.json в корне проекта.
"""
import json
from pathlib import Path
from difflib import SequenceMatcher

PROJECT_ROOT = Path(__file__).parent.parent
PENDING_FILE = PROJECT_ROOT / "pending_digest.json"

# Порог схожести заголовков для отсечения дублей внутри буфера
BUFFER_DUP_THRESHOLD = 0.8


def load_pending() -> list:
    """Загружает накопленные новости из буфера."""
    if not PENDING_FILE.exists():
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Ошибка загрузки {PENDING_FILE}: {e}")
        return []


def save_pending(items: list) -> None:
    """Сохраняет буфер на диск."""
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️  Ошибка сохранения {PENDING_FILE}: {e}")


def clear_pending() -> None:
    """Очищает буфер (после публикации дайджеста)."""
    save_pending([])


def _is_dup_title(title: str, existing_titles: list) -> bool:
    t = (title or "").lower()
    for seen in existing_titles:
        if SequenceMatcher(None, t, (seen or "").lower()).ratio() > BUFFER_DUP_THRESHOLD:
            return True
    return False


def add_to_digest(items: list) -> int:
    """
    Добавляет новости в буфер, отсекая дубли по заголовку внутри буфера.
    Возвращает количество реально добавленных.
    """
    pending = load_pending()
    existing_titles = [it.get("title", "") for it in pending]

    added = 0
    for item in items:
        title = item.get("title", "")
        if _is_dup_title(title, existing_titles):
            continue
        pending.append(item)
        existing_titles.append(title)
        added += 1

    if added:
        save_pending(pending)
    return added


def pending_count() -> int:
    return len(load_pending())
