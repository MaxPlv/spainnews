import os
import json
import time
import re
from difflib import SequenceMatcher
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# Загрузка .env файла
def load_env_file():
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

# Загружаем переменные окружения
load_env_file()

# Hugging Face API Token (бесплатный)
HF_API_TOKEN = os.getenv("HUGGING_FACE_API_TOKEN")

# Настройки
DUPLICATE_THRESHOLD = 0.8
RUSSIAN_TEXT_THRESHOLD = 0.8
MAX_TELEGRAM_LENGTH = 4000
INPUT_FILE = "news_raw.json"
OUTPUT_FILE = "result_news.json"
IMAGES_DIR = "processed_images"

os.makedirs(IMAGES_DIR, exist_ok=True)

# Hugging Face API endpoints
HF_API_BASE = "https://api-inference.huggingface.co/models/"
TRANSLATION_ES_EN_MODEL = "Helsinki-NLP/opus-mt-es-en"
TRANSLATION_EN_RU_MODEL = "Helsinki-NLP/opus-mt-en-ru"
SUMMARIZATION_MODEL = "facebook/bart-large-cnn"

def query_huggingface_api(model_name, payload, max_retries=3):
    """Отправляет запрос к Hugging Face Inference API"""
    api_url = f"{HF_API_BASE}{model_name}"
    headers = {}

    if HF_API_TOKEN:
        headers["Authorization"] = f"Bearer {HF_API_TOKEN}"

    for attempt in range(max_retries):
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=30)

            if response.status_code == 503:
                # Модель загружается
                print(f"   ⏳ Модель загружается, ожидание {10 * (attempt + 1)} сек...")
                time.sleep(10 * (attempt + 1))
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            print(f"   ⚠️  Ошибка API (попытка {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                raise

    return None

def is_duplicate(title, seen_titles):
    """Проверяет, является ли заголовок дубликатом"""
    for seen in seen_titles:
        if SequenceMatcher(None, title.lower(), seen.lower()).ratio() > DUPLICATE_THRESHOLD:
            return True
    return False

def is_russian_text(text, threshold=RUSSIAN_TEXT_THRESHOLD):
    """Проверяет, что текст содержит минимум threshold% русских символов"""
    if not text or not text.strip():
        return False

    russian_chars = set('абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ')
    letters = [char for char in text if char.isalpha()]

    if not letters:
        return False

    russian_count = sum(1 for char in letters if char in russian_chars)
    russian_ratio = russian_count / len(letters)

    return russian_ratio >= threshold

def has_hashtags(text):
    """Проверяет наличие хэштегов в тексте"""
    if not text or not text.strip():
        return False

    hashtags = re.findall(r'#\w+', text)
    return len(hashtags) >= 2

def is_telegram_compatible(title, description, link):
    """Проверяет совместимость с лимитами Telegram"""
    formatted_text = f"📰 *{title}*\n\n{description}\n\n🔗 [Ссылка на источник]({link})"
    return len(formatted_text) <= MAX_TELEGRAM_LENGTH

def fetch_article_content(url):
    """Загружает содержимое статьи по ссылке"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
            element.decompose()

        article_text = ""

        article = soup.find('article')
        if article:
            paragraphs = article.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

        if not article_text:
            content_divs = soup.find_all(['div'], class_=lambda x: x and any(
                word in str(x).lower() for word in ['article', 'content', 'story', 'post']
            ))
            for div in content_divs:
                paragraphs = div.find_all('p')
                text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
                if len(text) > len(article_text):
                    article_text = text

        if not article_text or len(article_text) < 200:
            paragraphs = soup.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

        return article_text[:5000] if article_text else ""

    except Exception as e:
        print(f"   ⚠️  Ошибка загрузки статьи: {e}")
        return ""

def split_text_into_chunks(text, max_length=400):
    """Разбивает текст на части по предложениям"""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= max_length:
            current_chunk += sentence + " "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + " "

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks

def translate_es_to_en(text):
    """Переводит с испанского на английский"""
    print(f"   🔄 Перевод es→en...")
    result = query_huggingface_api(
        TRANSLATION_ES_EN_MODEL,
        {"inputs": text}
    )

    if result and isinstance(result, list) and len(result) > 0:
        return result[0].get('translation_text', '')
    return ""

def translate_en_to_ru(text):
    """Переводит с английского на русский"""
    print(f"   🔄 Перевод en→ru...")

    # Разбиваем на части, если текст длинный
    chunks = split_text_into_chunks(text, max_length=400)
    translated_chunks = []

    for chunk in chunks:
        result = query_huggingface_api(
            TRANSLATION_EN_RU_MODEL,
            {"inputs": chunk}
        )

        if result and isinstance(result, list) and len(result) > 0:
            translated_chunks.append(result[0].get('translation_text', ''))
        time.sleep(1)

    return " ".join(translated_chunks)

def summarize_text(text):
    """Суммаризирует текст на английском"""
    print(f"   📝 Суммаризация...")

    # Ограничиваем длину для API
    text = text[:2000]

    result = query_huggingface_api(
        SUMMARIZATION_MODEL,
        {
            "inputs": text,
            "parameters": {
                "max_length": 300,
                "min_length": 100,
                "do_sample": False
            }
        }
    )

    if result and isinstance(result, list) and len(result) > 0:
        return result[0].get('summary_text', '')
    return ""

def translate_and_summarize(text, is_title=False):
    """
    Переводит и суммаризирует текст через Hugging Face API.
    Схема: Испанский -> Английский -> Суммаризация -> Русский
    """
    try:
        if is_title:
            # Для заголовка просто переводим
            en_text = translate_es_to_en(text)
            time.sleep(1)

            ru_text = translate_en_to_ru(en_text)
            return ru_text.strip()
        else:
            # Для текста: переводим, суммаризируем, переводим на русский
            chunks = split_text_into_chunks(text, max_length=400)
            en_chunks = []

            for i, chunk in enumerate(chunks[:10]):  # Максимум 10 частей
                if i > 0:
                    time.sleep(1)
                en_text = translate_es_to_en(chunk)
                if en_text:
                    en_chunks.append(en_text)

            en_full_text = " ".join(en_chunks)
            time.sleep(2)

            # Суммаризация
            summary = summarize_text(en_full_text)
            if not summary:
                # Если суммаризация не удалась, используем начало текста
                summary = en_full_text[:500]

            time.sleep(2)

            # Перевод на русский
            ru_text = translate_en_to_ru(summary)

            # Добавляем хэштеги
            hashtags = generate_hashtags(text)
            final_text = f"{ru_text}\n\n{hashtags}"

            return final_text.strip()

    except Exception as e:
        print(f"   ❌ Ошибка обработки текста: {e}")
        raise

def generate_hashtags(text):
    """Генерирует хэштеги на основе ключевых слов"""
    found_tags = []
    text_lower = text.lower()

    if 'españa' in text_lower or 'spanish' in text_lower:
        found_tags.append('#Испания')
    if 'valencia' in text_lower:
        found_tags.append('#Валенсия')
    if 'madrid' in text_lower:
        found_tags.append('#Мадрид')
    if 'barcelona' in text_lower:
        found_tags.append('#Барселона')
    if 'gobierno' in text_lower or 'política' in text_lower or 'government' in text_lower:
        found_tags.append('#Политика')
    if 'economía' in text_lower or 'economy' in text_lower:
        found_tags.append('#Экономика')

    # Дефолтные теги
    if len(found_tags) < 3:
        default_tags = ['#ЖизньВИспании', '#НовостиИспании', '#España']
        for tag in default_tags:
            if tag not in found_tags:
                found_tags.append(tag)
            if len(found_tags) >= 3:
                break

    return ' '.join(found_tags[:4])

def main():
    input_path = Path(__file__).parent.parent / INPUT_FILE
    output_path = Path(__file__).parent.parent / OUTPUT_FILE

    if not input_path.exists():
        print(f"❌ Файл {INPUT_FILE} не найден. Сначала запустите fetch_news.py")
        return

    print(f"📂 Загрузка новостей из {INPUT_FILE}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        news_items = json.load(f)

    print(f"✅ Загружено {len(news_items)} новостей")

    if HF_API_TOKEN:
        print(f"🔑 Используется Hugging Face API Token")
    else:
        print(f"⚠️  Hugging Face API Token не найден, используется публичное API (могут быть ограничения)")

    processed_news = []
    seen_titles = []

    for idx, news in enumerate(news_items, 1):
        title = news.get("title", "")
        description = news.get("description", "")

        print(f"\n[{idx}/{len(news_items)}] Обработка: {title[:50]}...")

        if is_duplicate(title, seen_titles):
            print("   ⚠️  Дубликат, пропускаем")
            continue

        seen_titles.append(title)

        try:
            print(f"   🤖 Обработка заголовка...")
            rewritten_title = translate_and_summarize(title, is_title=True)
            time.sleep(3)

            link = news.get("link", "")
            print(f"   🔗 Загрузка полного текста статьи...")
            article_content = fetch_article_content(link)

            if article_content:
                text_to_process = f"{title}. {article_content}"
                print(f"   📄 Загружено {len(article_content)} символов")
            else:
                text_to_process = f"{title}. {description}"
                print(f"   ⚠️  Используем description")

            print(f"   🤖 Обработка текста...")
            rewritten_text = translate_and_summarize(text_to_process)
            time.sleep(3)

            if not rewritten_title or not rewritten_title.strip():
                print(f"   ⚠️  Пустой заголовок, пропускаем")
                continue

            if not rewritten_text or not rewritten_text.strip():
                print(f"   ⚠️  Пустой текст, пропускаем")
                continue

            if not is_russian_text(rewritten_title):
                print(f"   ⚠️  Заголовок не на русском, пропускаем")
                continue

            if not is_russian_text(rewritten_text):
                print(f"   ⚠️  Текст не на русском, пропускаем")
                continue

            if not has_hashtags(rewritten_text):
                print(f"   ⚠️  Нет хэштегов, пропускаем")
                continue

            if not is_telegram_compatible(rewritten_title, rewritten_text, link):
                print(f"   ⚠️  Превышен лимит Telegram, пропускаем")
                continue

            print(f"   ✅ Обработано успешно")

            processed_news.append({
                "title": rewritten_title,
                "link": news.get("link", ""),
                "description": rewritten_text,
                "published": news.get("published", ""),
                "author": news.get("author", ""),
                "categories": news.get("categories", []),
                "image": news.get("image")
            })
        except Exception as e:
            print(f"   ❌ Ошибка обработки: {e}")
            continue

    print(f"\n💾 Сохранение обработанных новостей в {OUTPUT_FILE}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(processed_news, f, ensure_ascii=False, indent=2)

    print(f"✅ Успешно обработано и сохранено {len(processed_news)} новостей")

if __name__ == "__main__":
    main()
