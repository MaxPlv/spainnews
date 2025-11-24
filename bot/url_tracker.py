import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict


class URLTracker:
    """
    Класс для отслеживания URL новостей и предотвращения дубликатов.
    Автоматически очищает записи старше 24 часов.
    """
    
    def __init__(self, storage_file: str = "news_urls.json"):
        """
        Инициализация трекера URL
        
        Args:
            storage_file: Путь к JSON файлу для хранения URL (относительно корня проекта)
        """
        # Определяем путь к файлу относительно корня проекта
        project_root = Path(__file__).parent.parent
        self.storage_path = project_root / storage_file
        
        # Создаём файл если не существует
        if not self.storage_path.exists():
            self._save_urls([])
    
    def _load_urls(self) -> List[Dict[str, str]]:
        """Загружает URL из файла"""
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
    
    def _save_urls(self, urls: List[Dict[str, str]]) -> None:
        """Сохраняет URL в файл"""
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(urls, f, ensure_ascii=False, indent=2)
    
    def cleanup_old_urls(self, max_age_hours: int = 24) -> int:
        """
        Удаляет URL старше указанного времени
        
        Args:
            max_age_hours: Максимальный возраст записи в часах (по умолчанию 24)
            
        Returns:
            Количество удалённых записей
        """
        urls = self._load_urls()
        now = datetime.now(timezone.utc)
        
        # Фильтруем только те URL, которые моложе max_age_hours
        old_count = len(urls)
        fresh_urls = []
        
        for url_entry in urls:
            try:
                added_at = datetime.fromisoformat(url_entry['added_at'])
                age = now - added_at
                
                if age < timedelta(hours=max_age_hours):
                    fresh_urls.append(url_entry)
            except (KeyError, ValueError):
                # Пропускаем некорректные записи
                continue
        
        # Сохраняем только свежие URL
        self._save_urls(fresh_urls)
        removed_count = old_count - len(fresh_urls)
        
        return removed_count
    
    def is_duplicate(self, url: str) -> bool:
        """
        Проверяет, существует ли URL в базе
        
        Args:
            url: URL для проверки
            
        Returns:
            True если URL уже существует, False если новый
        """
        urls = self._load_urls()
        existing_urls = {entry['url'] for entry in urls}
        return url in existing_urls
    
    def add_url(self, url: str) -> bool:
        """
        Добавляет URL в базу с текущей датой
        
        Args:
            url: URL для добавления
            
        Returns:
            True если URL успешно добавлен, False если уже существует
        """
        if self.is_duplicate(url):
            return False
        
        urls = self._load_urls()
        urls.append({
            'url': url,
            'added_at': datetime.now(timezone.utc).isoformat()
        })
        self._save_urls(urls)
        
        return True
    
    def add_urls_batch(self, url_list: List[str]) -> int:
        """
        Добавляет несколько URL одновременно (эффективнее чем по одному)
        
        Args:
            url_list: Список URL для добавления
            
        Returns:
            Количество успешно добавленных URL
        """
        urls = self._load_urls()
        existing_urls = {entry['url'] for entry in urls}
        
        added_count = 0
        now = datetime.now(timezone.utc).isoformat()
        
        for url in url_list:
            if url not in existing_urls:
                urls.append({
                    'url': url,
                    'added_at': now
                })
                existing_urls.add(url)  # Обновляем set для следующих проверок
                added_count += 1
        
        self._save_urls(urls)
        return added_count
    
    def get_stats(self) -> Dict[str, int]:
        """
        Возвращает статистику по хранилищу URL
        
        Returns:
            Словарь с количеством записей
        """
        urls = self._load_urls()
        return {
            'total_urls': len(urls)
        }
