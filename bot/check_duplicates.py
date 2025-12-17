"""
Модуль для проверки дубликатов сообщений в канале
"""
import os
from collections import defaultdict
from difflib import SequenceMatcher
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

load_dotenv()

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")


def similarity(text1: str, text2: str) -> float:
    """
    Вычисляет схожесть двух текстов (от 0.0 до 1.0)
    """
    return SequenceMatcher(None, text1, text2).ratio()


def extract_text(message) -> str:
    """
    Извлекает текст из сообщения (включая caption для фото/видео)
    """
    if message.text:
        return message.text
    elif message.caption:
        return message.caption
    return ""


async def check_channel_duplicates(bot: Bot, limit: int = 100, similarity_threshold: float = 0.85):
    """
    Проверяет последние N сообщений в канале на наличие дубликатов
    
    Args:
        bot: Экземпляр Telegram Bot
        limit: Количество последних сообщений для проверки (по умолчанию 100)
        similarity_threshold: Порог схожести для определения дубликатов (0.0-1.0)
    
    Returns:
        Список групп дубликатов с информацией о сообщениях
    """
    try:
        if not CHANNEL_ID:
            return {
                "error": "CHANNEL_ID не установлен в переменных окружения (.env файл)"
            }
        
        # Получаем информацию о канале
        try:
            chat = await bot.get_chat(CHANNEL_ID)
        except TelegramError as e:
            return {
                "error": f"Не удалось получить доступ к каналу {CHANNEL_ID}. Убедитесь, что бот является администратором канала.\nОшибка: {e}"
            }
        
        # Получаем последние сообщения
        messages = []
        message_id = None
        
        # Telegram не предоставляет прямой метод для получения истории сообщений через Bot API
        # Нужно использовать альтернативный подход - получить последнее сообщение и идти назад
        # Но Bot API ограничен в этом. Вместо этого будем использовать обходной путь:
        
        # Попробуем получить обновления или используем другой подход
        # Для полноценной работы с историей сообщений потребуется использовать Telethon или Pyrogram
        
        return {
            "error": "Для получения истории сообщений канала требуется использовать MTProto клиент (Telethon/Pyrogram).\n\n"
                     "Bot API не предоставляет метод для получения истории сообщений канала.\n\n"
                     "Варианты решения:\n"
                     "1. Использовать Telethon (требует API_ID и API_HASH от my.telegram.org)\n"
                     "2. Использовать Pyrogram (аналогично)\n"
                     "3. Отслеживать дубликаты в реальном времени при публикации\n\n"
                     "Рекомендация: добавить проверку на дубликаты перед публикацией новости."
        }
        
    except Exception as e:
        return {
            "error": f"Произошла ошибка при проверке дубликатов: {e}"
        }


async def check_duplicates_with_telethon(limit: int = 100, similarity_threshold: float = 0.85):
    """
    Альтернативный метод проверки дубликатов с использованием Telethon
    Требует установки: pip install telethon
    И настройки API_ID, API_HASH в .env
    """
    try:
        from telethon import TelegramClient
        
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        bot_token = os.getenv("TELEGRAM_TOKEN")
        
        if not api_id or not api_hash:
            return {
                "error": "Для использования Telethon необходимо добавить в .env:\n"
                         "TELEGRAM_API_ID=your_api_id\n"
                         "TELEGRAM_API_HASH=your_api_hash\n\n"
                         "Получить можно на https://my.telegram.org"
            }
        
        if not bot_token:
            return {
                "error": "TELEGRAM_TOKEN не найден в .env файле"
            }
        
        # Создаём клиент и авторизуемся как бот
        client = TelegramClient('bot_session', int(api_id), api_hash)
        await client.start(bot_token=bot_token)
        
        # Получаем последние сообщения
        messages = []
        async for message in client.iter_messages(CHANNEL_ID, limit=limit):
            text = extract_text(message)
            if text:  # Только сообщения с текстом
                messages.append({
                    'id': message.id,
                    'text': text,
                    'date': message.date,
                    'link': f"https://t.me/{CHANNEL_ID.replace('@', '')}/{message.id}"
                })
        
        await client.disconnect()
        
        # Ищем дубликаты
        duplicates = defaultdict(list)
        checked = set()
        
        for i, msg1 in enumerate(messages):
            if i in checked:
                continue
                
            group = [msg1]
            for j, msg2 in enumerate(messages[i+1:], start=i+1):
                if j in checked:
                    continue
                    
                sim = similarity(msg1['text'], msg2['text'])
                if sim >= similarity_threshold:
                    group.append(msg2)
                    checked.add(j)
            
            if len(group) > 1:
                # Нашли группу дубликатов
                duplicates[msg1['id']] = group
                checked.add(i)
        
        return {
            "total_checked": len(messages),
            "duplicates_found": len(duplicates),
            "duplicate_groups": list(duplicates.values()),
            "threshold": similarity_threshold
        }
        
    except ImportError:
        return {
            "error": "Telethon не установлен. Установите: pip install telethon"
        }
    except Exception as e:
        return {
            "error": f"Ошибка при работе с Telethon: {e}"
        }


def format_duplicates_report(result: dict) -> str:
    """
    Форматирует отчёт о дубликатах для отправки в Telegram
    """
    if "error" in result:
        return f"❌ Ошибка:\n\n{result['error']}"
    
    if result.get("duplicates_found", 0) == 0:
        return (
            f"✅ Проверка завершена\n\n"
            f"📊 Проверено сообщений: {result['total_checked']}\n"
            f"🎯 Дубликатов не найдено\n"
            f"📏 Порог схожести: {result['threshold']*100:.0f}%"
        )
    
    report = (
        f"⚠️ Найдены дубликаты!\n\n"
        f"📊 Проверено сообщений: {result['total_checked']}\n"
        f"🔍 Найдено групп дубликатов: {result['duplicates_found']}\n"
        f"📏 Порог схожести: {result['threshold']*100:.0f}%\n\n"
    )
    
    for idx, group in enumerate(result['duplicate_groups'], 1):
        report += f"\n━━━ Группа #{idx} ({len(group)} сообщений) ━━━\n"
        
        for msg in group:
            preview = msg['text'][:100].replace('\n', ' ')
            if len(msg['text']) > 100:
                preview += "..."
            
            report += (
                f"\n📅 {msg['date'].strftime('%Y-%m-%d %H:%M')}\n"
                f"🔗 {msg['link']}\n"
                f"💬 {preview}\n"
            )
    
    return report
