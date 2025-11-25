#!/usr/bin/env python3
# process_ai.py — улучшенная версия с кэшем, retry, fallback и одним запросом на статью

import os
import json
import time
import re
import random
from difflib import SequenceMatcher
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# --- Google GenAI SDK Imports ---
from google import genai
from google.genai import types
# --------------------------------

# Загрузка .env файла (если нужен)
def load_env_file():
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_env_file()

# Загрузка ключа
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise Exception("Установите переменную GEMINI_API_KEY")

# Настройки (можно подправить)
DUPLICATE_THRESHOLD = 0.8
RUSSIAN_TEXT_THRESHOLD = 0.8
MAX_TELEGRAM_LENGTH = 4000
INPUT_FILE = "news_raw.json"

OUTPUT_FILE = "result_news.json"
REJECTED_FILE = "rejected_news.json"
IMAGES_DIR = "processed_images"
CACHE_FILE = "gemini_cache.json"

# Rate limiting / delays
GLOBAL_DELAY = float(os.getenv("GLOBAL_DELAY", "12"))  # сек между вызовами к Gemini
BASE_RETRY_DELAY = 5  # базовая задержка для экспоненциального backoff
MAX_RETRIES = 5

# Модели в порядке приоритета (fallback-ready)
MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash-001"
]

# Подготовка директорий
os.makedirs(IMAGES_DIR, exist_ok=True)

# --- Утилиты ---
def is_duplicate(title, seen_titles):
    for seen in seen_titles:
        if SequenceMatcher(None, title.lower(), seen.lower()).ratio() > DUPLICATE_THRESHOLD:
            return True
    return False

def is_russian_text(text, threshold=RUSSIAN_TEXT_THRESHOLD):
    if not text or not text.strip():
        return False
    russian_chars = set('абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ')
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    russian_count = sum(1 for c in letters if c in russian_chars)
    return (russian_count / len(letters)) >= threshold

def has_hashtags(text):
    if not text or not text.strip():
        return False
    hashtags = re.findall(r'#\w+', text)
    return len(hashtags) >= 2

def is_telegram_compatible(title, description, link):
    formatted_text = f"📰 *{title}*\n\n{description}\n\n🔗 [Ссылка на источник]({link})"
    return len(formatted_text) <= MAX_TELEGRAM_LENGTH

def fetch_article_content(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')
        for el in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
            el.decompose()
        article_text = ""
        article = soup.find('article')
        if article:
            ps = article.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in ps if p.get_text().strip()])
        if not article_text:
            content_divs = soup.find_all(['div'], class_=lambda x: x and any(word in str(x).lower() for word in ['article','content','story','post']))
            for div in content_divs:
                ps = div.find_all('p')
                t = ' '.join([p.get_text().strip() for p in ps if p.get_text().strip()])
                if len(t) > len(article_text):
                    article_text = t
        if not article_text or len(article_text) < 200:
            ps = soup.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in ps if p.get_text().strip()])
        return article_text[:10000] if article_text else ""
    except Exception as e:
        print(f"   ⚠️ Ошибка загрузки статьи: {e}")
        return ""

def clean_ai_response(text):
    phrases_to_remove = [
        "Вот краткая выжимка:", "Краткая выжимка:", "Вот краткое изложение:", "Краткое изложение:",
        "Вот перевод:", "Перевод:", "Вот заголовок:", "Заголовок:", "Вот пересказ:", "Пересказ:",
        "Вот новость:", "Новость:"
    ]
    result = text.strip()
    for p in phrases_to_remove:
        if result.startswith(p):
            result = result[len(p):].strip()
    if (result.startswith('"') and result.endswith('"')) or (result.startswith("'") and result.endswith("'")):
        result = result[1:-1].strip()
    return result

# --- Кэш для ответов модели ---
def load_cache():
    try:
        if Path(CACHE_FILE).exists():
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                print(f"   ⚠️ Кэш файл повреждён, создаём новый")
    except Exception as e:
        print(f"   ⚠️ Ошибка чтения кэша: {e}")
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"   ⚠️ Не удалось сохранить кэш: {e}")

# --- Инициализация глобального клиента ---
client = genai.Client(api_key=API_KEY)

def parse_json_from_text(text):
    """
    Пытаемся извлечь JSON-объект из текста.
    Если не получается, возвращаем None.
    """
    try:
        # Попытаемся найти {...} блок
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            candidate = match.group(0)
            # чистим непечатаемые символы
            candidate = candidate.strip()
            return json.loads(candidate)
        # Если нет JSON в тексте — попробуем разобрать простым способом
        return None
    except Exception:
        return None

def gemini_request_single_json(article_text, max_retries=MAX_RETRIES, base_delay=BASE_RETRY_DELAY):
    """
    Делает один (логический) запрос к Gemini, который должен вернуть JSON:
    { "title_ru": "...", "summary_ru": "...", "hashtags": ["#a","#b"] }
    Возвращает dict или выбрасывает Exception.
    """
    # Промпт: просим возвращать чистый JSON в кодовом блоке или без него.
    prompt = (
        "Ты редактор новостного Telegram-канала про жизнь в Испании.\n\n"
        "1) На основе следующего текста статьи создай КРАТКИЙ заголовок на русском (до 10 слов) — поле title_ru.\n"
        "2) Пиши в стиле Александра Невзорова, но на 60% от его стиля.\n"
        "3) Создай краткую выжимку на русском (5-6 предложений; разделяй на абзацы, если уместно; максимум 4000 символов) — поле summary_ru.\n"
        "4) Подбери 3-4 хэштега по содержанию (без пробелов, с #) — поле hashtags (массив строк).\n\n"
        "ВЕРНИ СТРОГО JSON ОБЪЕКТ С ПОЛЯМИ: title_ru, summary_ru, hashtags.\n"
        "Пример корректного ответа:\n"
        '{"title_ru":"...","summary_ru":"...","hashtags":["#madrid","#immigration"]}\n\n'
        "Текст статьи:\n\n" + article_text
    )

    cache = load_cache()
    cache_key = str(hash(prompt))
    if cache_key in cache:
        # Возвращаем закешированный ответ
        return cache[cache_key]

    last_error = None
    for attempt in range(max_retries):
        model = MODEL_FALLBACKS[min(attempt, len(MODEL_FALLBACKS) - 1)]
        try:
            if attempt > 0:
                print(f"   🔁 Retry {attempt}/{max_retries}, trying model={model}")
            # Генерация
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=2100,
                    response_mime_type="application/json"
                )
            )
            text = ""
            if hasattr(response, "text") and response.text:
                text = response.text
            else:
                raise Exception("No text in response from model")
            text = clean_ai_response(text)

            # Попробуем парсить JSON
            parsed = parse_json_from_text(text)
            if parsed and isinstance(parsed, dict):
                # Небольшая валидация
                title = parsed.get("title_ru", "").strip()
                summary = parsed.get("summary_ru", "").strip()
                hashtags = parsed.get("hashtags", [])
                if title and summary and isinstance(hashtags, list) and len(hashtags) >= 1:
                    result = {"title_ru": title, "summary_ru": summary, "hashtags": hashtags}
                    cache[cache_key] = result
                    save_cache(cache)
                    return result
                # если невалидно — попытаемся дальше (кастом)
            # Если не JSON — пытаемся извлечь заголовок/хэштеги вручную
            # Пробуем найти хэштеги в ответе
            found_tags = re.findall(r'#\w+', text)
            # Попытка извлечь первые 200 символов как заголовок-предположение
            guessed_title = None
            # Иногда модель возвращает "title: ..." в тексте
            m = re.search(r'(?:title[:\-]\s*)(.+)', text, re.IGNORECASE)
            if m:
                guessed_title = m.group(1).split('\n')[0].strip()
            if not guessed_title:
                # первая строка как заголовок
                first_line = text.splitlines()[0].strip() if text.splitlines() else ""
                if 3 <= len(first_line.split()) <= 12:
                    guessed_title = first_line
            guessed_summary = text
            result = {
                "title_ru": guessed_title or "",
                "summary_ru": guessed_summary or text,
                "hashtags": found_tags or []
            }
            # Сохраняем в кэш для последующих попыток
            cache[cache_key] = result
            save_cache(cache)

            # Валидация: если пустой title или summary — бросаем ошибку, чтобы ретраить/fallback
            if not result["title_ru"] or not result["summary_ru"]:
                raise Exception("Invalid/empty parsed response from model")

            return result

        except Exception as e:
            last_error = str(e)
            le = last_error.lower()
            print(f"   ⚠️  Gemini error (model={model}): {last_error[:140]}")

            # классифицируем ошибку
            overloaded = ("503" in le) or ("overload" in le) or ("unavailable" in le) or ("overloaded" in le)
            rate_limit = ("429" in le) or ("rate limit" in le) or ("quota" in le)
            timeout_err = ("timeout" in le) or ("timed out" in le)

            # exponential backoff + jitter
            if rate_limit:
                delay = 30 + attempt * 10 + random.uniform(0, 3)
                print(f"   🚫 Rate limit detected. Waiting {int(delay)}s before retry...")
                time.sleep(delay)
            elif overloaded:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
                print(f"   🧘 Model overloaded. Waiting {int(delay)}s before retry (will fallback to next model if persists)...")
                time.sleep(delay)
            elif timeout_err:
                delay = base_delay * (attempt + 1) + random.uniform(0, 2)
                print(f"   ⏱ Timeout. Waiting {int(delay)}s before retry...")
                time.sleep(delay)
            else:
                delay = base_delay * (attempt + 1) + random.uniform(0, 2)
                print(f"   ⏳ Other error. Waiting {int(delay)}s before retry...")
                time.sleep(delay)
            # перейдём к следующему повтору (включая смену модели согласно индексам)

    # Если все попытки исчерпаны
    raise Exception(f"Gemini failed after {max_retries} retries. Last error: {last_error}")

# --- Основная логика ---
def main():
    input_path = Path(__file__).parent.parent / INPUT_FILE
    output_path = Path(__file__).parent.parent / OUTPUT_FILE
    rejected_path = Path(__file__).parent.parent / REJECTED_FILE

    if not input_path.exists():
        print(f"❌ Файл {INPUT_FILE} не найден.")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        news_items = json.load(f)

    if not news_items or not isinstance(news_items, list):
        print(f"❌ Файл {INPUT_FILE} пуст или имеет неверный формат.")
        return

    print(f"📂 Загружено {len(news_items)} новостей")

    processed_news = []
    rejected_news = []
    seen_titles = []
    cache = load_cache()

    for idx, news in enumerate(news_items, start=1):
        title = news.get("title", "").strip()
        description = news.get("description", "").strip()
        link = news.get("link", "").strip()

        print(f"\n[{idx}/{len(news_items)}] {title[:80]}")

        if is_duplicate(title, seen_titles):
            print("   ⚠️ Дубликат, пропускаем")
            rejected_news.append({"title": title, "reason": "duplicate"})
            continue
        seen_titles.append(title)

        # Подготовка текста для модели (сначала пытаемся полную статью)
        article_content = ""
        if link:
            print("   🔗 Загружаем статью...")
            article_content = fetch_article_content(link)
            if article_content:
                print(f"   📄 Загружено {len(article_content)} символов")
            else:
                print("   ⚠️ Полный текст не получен, используем description")

        text_for_model = (title + ". " + (article_content or description or title))[:12000]

        # Минимальная задержка между вызовами к Gemini
        print(f"   💤 Ждём {GLOBAL_DELAY}s перед запросом к Gemini (глобальный rate limit)")
        time.sleep(GLOBAL_DELAY)

        try:
            ai_result = gemini_request_single_json(text_for_model)
        except Exception as e:
            print(f"   ❌ Проблема с Gemini: {e}")
            rejected_news.append({"title": title, "reason": f"gemini_error: {str(e)}"})
            continue

        # Формируем итоговые поля (сохраняем в прежнем формате)
        rewritten_title = ai_result.get("title_ru", "").strip()
        rewritten_text = ai_result.get("summary_ru", "").strip()
        hashtags = ai_result.get("hashtags", [])

        # Валидации как раньше
        if not rewritten_title:
            print("   ⚠️ Пустой заголовок от модели, пропускаем")
            rejected_news.append({"title": title, "reason": "empty_title"})
            continue
        if not rewritten_text:
            print("   ⚠️ Пустой summary от модели, пропускаем")
            rejected_news.append({"title": title, "reason": "empty_summary"})
            continue
        if not is_russian_text(rewritten_title):
            print("   ⚠️ Заголовок не на русском >=80%, пропускаем")
            rejected_news.append({"title": title, "reason": "not_russian_title"})
            continue
        if not is_russian_text(rewritten_text):
            print("   ⚠️ Текст не на русском >=80%, пропускаем")
            rejected_news.append({"title": title, "reason": "not_russian_text"})
            continue
        if not hashtags or len(hashtags) < 2:
            print("   ⚠️ Мало хэштегов, пропускаем")
            rejected_news.append({"title": title, "reason": "few_hashtags"})
            continue
        # Добавляем хэштеги в конец summary, если их нет
        if not re.search(r'#\w+', rewritten_text):
            rewritten_text = rewritten_text.rstrip() + "\n\n" + " ".join(hashtags[:4])

        if not is_telegram_compatible(rewritten_title, rewritten_text, link):
            print("   ⚠️ Превышает лимит Telegram, пропускаем")
            rejected_news.append({"title": title, "reason": "telegram_limit"})
            continue

        print(f"   ✅ ОК: {rewritten_title[:60]} / summary {len(rewritten_text)} chars / tags {len(hashtags)}")

        processed_news.append({
            "title": rewritten_title,
            "link": link,
            "description": rewritten_text,
            "published": news.get("published", ""),
            "author": news.get("author", ""),
            "categories": news.get("categories", []),
            "image": news.get("image")
        })

    # Сохраняем результат
    print(f"\n💾 Сохранение {len(processed_news)} обработанных новостей в {OUTPUT_FILE}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(processed_news, f, ensure_ascii=False, indent=2)
    
    # Сохраняем отклоненные
    print(f"💾 Сохранение {len(rejected_news)} отклоненных новостей в {REJECTED_FILE}...")
    with open(rejected_path, 'w', encoding='utf-8') as f:
        json.dump(rejected_news, f, ensure_ascii=False, indent=2)

    print("✅ Готово.")

if __name__ == "__main__":
    main()