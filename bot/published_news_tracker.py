"""
Модуль для отслеживания опубликованных новостей и предотвращения дубликатов
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher


PROJECT_ROOT = Path(__file__).parent.parent
PUBLISHED_NEWS_FILE = PROJECT_ROOT / "published_news.json"
HISTORY_DAYS = 14  # Хранить историю за последние 14 дней


def similarity(text1: str, text2: str) -> float:
    """
    Вычисляет схожесть двух текстов (от 0.0 до 1.0)
    """
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()


def load_published_news() -> list:
    """
    Загружает историю опубликованных новостей
    """
    if not PUBLISHED_NEWS_FILE.exists():
        return []
    
    try:
        with open(PUBLISHED_NEWS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка при загрузке published_news.json: {e}")
        return []


def save_published_news(news_list: list):
    """
    Сохраняет историю опубликованных новостей
    """
    try:
        with open(PUBLISHED_NEWS_FILE, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при сохранении published_news.json: {e}")


def cleanup_old_entries(news_list: list) -> list:
    """
    Удаляет записи старше HISTORY_DAYS дней
    
    Args:
        news_list: Список опубликованных новостей
        
    Returns:
        Отфильтрованный список
    """
    cutoff_date = datetime.now() - timedelta(days=HISTORY_DAYS)
    
    filtered = []
    for news in news_list:
        try:
            published_at = datetime.fromisoformat(news.get('published_at', ''))
            if published_at >= cutoff_date:
                filtered.append(news)
        except (ValueError, TypeError):
            # Если дата невалидна, пропускаем запись
            continue
    
    removed_count = len(news_list) - len(filtered)
    if removed_count > 0:
        print(f"🧹 Удалено {removed_count} старых записей (>{HISTORY_DAYS} дней)")
    
    return filtered


def add_published_news(title: str, text: str, url: str = ""):
    """
    Добавляет новость в историю опубликованных
    
    Args:
        title: Заголовок новости
        text: Полный текст новости
        url: URL новости (опционально)
    """
    news_list = load_published_news()
    
    # Очищаем старые записи перед добавлением новой
    news_list = cleanup_old_entries(news_list)
    
    # Добавляем новую запись
    news_entry = {
        "title": title,
        "text": text,
        "url": url,
        "published_at": datetime.now().isoformat()
    }
    
    news_list.append(news_entry)
    save_published_news(news_list)
    print(f"✅ Новость добавлена в историю публикаций (всего: {len(news_list)})")


def check_duplicate(title: str, text: str, similarity_threshold: float = 0.85) -> dict:
    """
    Проверяет, является ли новость дубликатом уже опубликованной
    
    Args:
        title: Заголовок проверяемой новости
        text: Текст проверяемой новости
        similarity_threshold: Порог схожести (0.0-1.0)
        
    Returns:
        dict с результатами проверки:
        - is_duplicate: bool
        - match: dict с информацией о найденном дубликате (если есть)
        - similarity_score: float схожесть с найденным дубликатом
    """
    news_list = load_published_news()
    
    # Очищаем старые записи
    news_list = cleanup_old_entries(news_list)
    
    for published_news in news_list:
        # Сравниваем заголовки
        title_sim = similarity(title, published_news.get('title', ''))
        
        # Сравниваем тексты
        text_sim = similarity(text, published_news.get('text', ''))
        
        # Берём максимальную схожесть
        max_sim = max(title_sim, text_sim)
        
        if max_sim >= similarity_threshold:
            return {
                "is_duplicate": True,
                "match": published_news,
                "similarity_score": max_sim,
                "matched_by": "title" if title_sim > text_sim else "text"
            }
    
    return {
        "is_duplicate": False,
        "match": None,
        "similarity_score": 0.0
    }


def get_stats() -> dict:
    """
    Возвращает статистику по опубликованным новостям
    """
    news_list = load_published_news()
    news_list = cleanup_old_entries(news_list)
    
    if not news_list:
        return {
            "total": 0,
            "oldest_date": None,
            "newest_date": None
        }
    
    dates = []
    for news in news_list:
        try:
            dates.append(datetime.fromisoformat(news.get('published_at', '')))
        except (ValueError, TypeError):
            continue
    
    return {
        "total": len(news_list),
        "oldest_date": min(dates).isoformat() if dates else None,
        "newest_date": max(dates).isoformat() if dates else None,
        "history_days": HISTORY_DAYS
    }
