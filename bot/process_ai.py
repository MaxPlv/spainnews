import os
import json
import time
import re
from difflib import SequenceMatcher
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from transformers import pipeline, AutoTokenizer, AutoModelForSeq2SeqLM
import torch

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

# Настройки
DUPLICATE_THRESHOLD = 0.8
RUSSIAN_TEXT_THRESHOLD = 0.8
MAX_TELEGRAM_LENGTH = 4000
INPUT_FILE = "news_raw.json"
OUTPUT_FILE = "result_news.json"
IMAGES_DIR = "processed_images"

os.makedirs(IMAGES_DIR, exist_ok=True)

# Глобальные переменные для моделей
translation_pipe = None
summarization_pipe = None
device = None

def init_models():
    """Инициализация моделей Hugging Face"""
    global translation_pipe, summarization_pipe, device

    print("🤖 Инициализация моделей Hugging Face...")

    # Определяем устройство (CPU для railway.app)
    device = 0 if torch.cuda.is_available() else -1
    device_name = "GPU" if device == 0 else "CPU"
    print(f"   📱 Используется устройство: {device_name}")

    try:
        # Модель для перевода (Helsinki-NLP opus-mt)
        # Маленькая и эффективная модель для испанский -> английский
        print("   📥 Загрузка модели перевода (es->en)...")
        translation_model_name = "Helsinki-NLP/opus-mt-es-en"
        translation_pipe = pipeline(
            "translation",
            model=translation_model_name,
            device=device,
            max_length=512
        )

        # Модель для перевода английский -> русский
        print("   📥 Загрузка модели перевода (en->ru)...")
        translation_en_ru_name = "Helsinki-NLP/opus-mt-en-ru"
        translation_en_ru_pipe = pipeline(
            "translation",
            model=translation_en_ru_name,
            device=device,
            max_length=512
        )

        # Модель для суммаризации (на английском, компактная)
        print("   📥 Загрузка модели суммаризации...")
        summarization_model_name = "facebook/bart-large-cnn"
        summarization_pipe = pipeline(
            "summarization",
            model=summarization_model_name,
            device=device
        )

        print("   ✅ Модели успешно загружены")
        return translation_pipe, translation_en_ru_pipe, summarization_pipe

    except Exception as e:
        print(f"   ❌ Ошибка загрузки моделей: {e}")
        raise

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

        return article_text[:8000] if article_text else ""

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

def translate_and_summarize(text, is_title=False, translation_es_en_pipe=None,
                           translation_en_ru_pipe=None, summarization_pipe=None,
                           max_retries=3):
    """
    Переводит и суммаризирует текст с помощью HuggingFace Transformers.
    Схема: Испанский -> Английский -> Суммаризация -> Русский
    """
    for attempt in range(max_retries):
        try:
            if is_title:
                # Для заголовка просто переводим напрямую
                print(f"   🔄 Перевод заголовка (es->en->ru)...")

                # Шаг 1: Испанский -> Английский
                en_result = translation_es_en_pipe(text, max_length=100)
                en_text = en_result[0]['translation_text']

                time.sleep(0.5)

                # Шаг 2: Английский -> Русский
                ru_result = translation_en_ru_pipe(en_text, max_length=100)
                translated_text = ru_result[0]['translation_text']

                return translated_text.strip()
            else:
                # Для текста: переводим, суммаризируем, переводим на русский
                print(f"   🔄 Перевод текста (es->en)...")

                # Разбиваем текст на части, если он слишком длинный
                chunks = split_text_into_chunks(text, max_length=400)
                en_chunks = []

                for i, chunk in enumerate(chunks):
                    if i > 0:
                        time.sleep(0.5)  # Небольшая задержка между запросами
                    result = translation_es_en_pipe(chunk, max_length=512)
                    en_chunks.append(result[0]['translation_text'])

                en_text = " ".join(en_chunks)

                # Ограничиваем длину для суммаризации
                en_text = en_text[:3000]

                print(f"   📝 Суммаризация текста...")
                time.sleep(0.5)

                # Суммаризация на английском
                summary = summarization_pipe(
                    en_text,
                    max_length=300,
                    min_length=100,
                    do_sample=False
                )
                summarized_text = summary[0]['summary_text']

                print(f"   🔄 Перевод суммаризации (en->ru)...")
                time.sleep(0.5)

                # Переводим суммаризацию на русский по частям
                summary_chunks = split_text_into_chunks(summarized_text, max_length=400)
                ru_chunks = []

                for i, chunk in enumerate(summary_chunks):
                    if i > 0:
                        time.sleep(0.5)
                    result = translation_en_ru_pipe(chunk, max_length=512)
                    ru_chunks.append(result[0]['translation_text'])

                final_text = " ".join(ru_chunks)

                # Добавляем хэштеги (извлекаем ключевые слова)
                hashtags = generate_hashtags(text)
                final_text = f"{final_text}\n\n{hashtags}"

                return final_text.strip()

        except Exception as e:
            error_msg = str(e)
            print(f"   ⚠️  Ошибка при попытке {attempt + 1}/{max_retries}: {error_msg[:100]}")

            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"   ⏳ Повторная попытка через {wait_time} секунд...")
                time.sleep(wait_time)
            else:
                raise Exception(f"Не удалось обработать текст после {max_retries} попыток")

    return "Не удалось обработать текст"

def generate_hashtags(text):
    """Генерирует хэштеги на основе ключевых слов"""
    # Простой подход: берём наиболее частые существительные
    keywords = ['España', 'Испания', 'Valencia', 'Madrid', 'Barcelona',
                'Gobierno', 'Economía', 'Política', 'Sociedad']

    found_tags = []
    text_lower = text.lower()

    if 'españa' in text_lower or 'spanish' in text_lower:
        found_tags.append('#Испания')
    if 'valencia' in text_lower:
        found_tags.append('#Валенсия')
    if 'madrid' in text_lower:
        found_tags.append('#Мадрид')
    if 'gobierno' in text_lower or 'política' in text_lower or 'government' in text_lower:
        found_tags.append('#Политика')
    if 'economía' in text_lower or 'economy' in text_lower:
        found_tags.append('#Экономика')

    # Если меньше 3 хэштегов, добавляем дефолтные
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

    # Инициализируем модели
    translation_es_en, translation_en_ru, summarization = init_models()

    print(f"\n📂 Загрузка новостей из {INPUT_FILE}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        news_items = json.load(f)

    print(f"✅ Загружено {len(news_items)} новостей")

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
            rewritten_title = translate_and_summarize(
                title,
                is_title=True,
                translation_es_en_pipe=translation_es_en,
                translation_en_ru_pipe=translation_en_ru,
                summarization_pipe=summarization
            )

            time.sleep(2)

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
            rewritten_text = translate_and_summarize(
                text_to_process,
                translation_es_en_pipe=translation_es_en,
                translation_en_ru_pipe=translation_en_ru,
                summarization_pipe=summarization
            )

            time.sleep(2)

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
