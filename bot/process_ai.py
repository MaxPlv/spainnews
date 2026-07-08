#!/usr/bin/env python3
# process_ai.py — улучшенная версия с кэшем, retry, fallback и одним запросом на статью

import os
import json
import time
import re
import random
import hashlib
import traceback
from difflib import SequenceMatcher
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# --- Google GenAI SDK Imports ---
from google import genai
from google.genai import types
# --------------------------------

# Общая таксономия рубрик (bot/categories.py)
from categories import CATEGORIES, normalize_category
from paths import DATA_DIR

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

# Формат карточки новости ("фаст-фуд")
MIN_BULLETS = 2          # минимум пунктов в новости
MAX_BULLETS = 3          # максимум пунктов (остальное отбрасываем)
MAX_BULLET_WORDS = 18    # мягкий ориентир длины пункта для промпта

# Экономия токенов Gemini: в модель отдаём заголовок + первые абзацы статьи.
# Новости построены по "перевёрнутой пирамиде" — суть в начале, поэтому для
# сводки из 2-3 буллитов ~4000 символов достаточно. Можно поднять через env.
MAX_MODEL_INPUT_CHARS = int(os.getenv("MAX_MODEL_INPUT_CHARS", "4000"))
MAX_ARTICLE_FETCH_CHARS = 6000  # верхняя граница парсинга статьи (с запасом над входом)
# Версия промпта — часть ключа кэша, чтобы при смене промпта старые ответы не переиспользовались.
# v3: добавлены поля spain_focus / israel_related — старый кэш обязательно инвалидируем,
# иначе закешированные ответы без этих полей молча обойдут новый фильтр темы.
PROMPT_VERSION = "v3-focus-israel"

INPUT_FILE = DATA_DIR / "news_raw.json"
OUTPUT_FILE = DATA_DIR / "result_news.json"
REJECTED_FILE = DATA_DIR / "rejected_news.json"
IMAGES_DIR = DATA_DIR / "processed_images"
CACHE_FILE = DATA_DIR / "gemini_cache.json"

# Rate limiting / delays
GLOBAL_DELAY = float(os.getenv("GLOBAL_DELAY", "12"))  # сек между вызовами к Gemini
BASE_RETRY_DELAY = 5  # базовая задержка для экспоненциального backoff
MAX_RETRIES = 5

# Модели в порядке приоритета (fallback-ready)
# Используем только существующие модели
MODEL_FALLBACKS = [
    "gemini-2.5-flash-lite",   # Ультра-быстрая и бюджетная модель (отлично для квот)
    "gemini-2.5-flash",        # Быстрая, актуальная и рекомендуемая модель
    "gemini-2.5-pro",          # Самая мощная модель для сложных задач (второй приоритет)
    "gemini-2.0-flash",        # Предыдущая стабильная версия (как запасной вариант)
    "gemini-1.0-pro"           # Самая стабильная, предыдущая версия Pro (резерв)
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
        return article_text[:MAX_ARTICLE_FETCH_CHARS] if article_text else ""
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
        # Сначала пытаемся распарсить текст целиком
        cleaned = text.strip()
        return json.loads(cleaned)
    except:
        pass
    
    try:
        # Попытаемся найти {...} блок
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if match:
            candidate = match.group(0)
            # чистим непечатаемые символы
            candidate = candidate.strip()
            return json.loads(candidate)
        # Если нет JSON в тексте — попробуем разобрать простым способом
        return None
    except Exception as e:
        print(f"   ⚠️ JSON parsing error: {e}")
        return None

def _coerce_bullets(value):
    """Приводит поле bullets к списку непустых строк."""
    if isinstance(value, list):
        items = [str(b).strip() for b in value if str(b).strip()]
    elif isinstance(value, str) and value.strip():
        # Иногда модель возвращает пункты одной строкой через перенос/маркеры
        items = [ln.strip(" -•*\t") for ln in value.splitlines() if ln.strip(" -•*\t")]
    else:
        items = []
    return items[:MAX_BULLETS]


def _coerce_importance(value):
    """Приводит importance к целому 1..10 (по умолчанию 5)."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, n))


def _coerce_bool(value, default):
    """Приводит значение к bool; при отсутствии/невалидности — безопасный дефолт."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "да"):
            return True
        if v in ("false", "0", "no", "нет"):
            return False
    return default


def gemini_request_single_json(article_text, max_retries=MAX_RETRIES, base_delay=BASE_RETRY_DELAY):
    """
    Делает один (логический) запрос к Gemini, который должен вернуть JSON:
    { "title_ru": "...", "bullets": ["...","..."], "importance": 7,
      "category": "politics", "hashtags": ["#a","#b"] }
    Возвращает dict или выбрасывает Exception.
    """
    # Промпт: формат "фаст-фуд" — цепляющий заголовок + 2-3 коротких пункта.
    prompt = (
        "Ты редактор новостного Telegram-канала про жизнь в Испании.\n"
        "Формат канала — быстрый и лёгкий: короткие карточки, которые читаются за пару секунд.\n\n"
        "На основе следующего текста статьи верни СТРОГО JSON-объект с полями:\n"
        f"1) title_ru — цепляющий заголовок-суть на русском, до 8 слов, без кликбейта и без точки в конце.\n"
        f"2) bullets — массив из {MIN_BULLETS}-{MAX_BULLETS} очень коротких пунктов на русском "
        f"(каждый до {MAX_BULLET_WORDS} слов): что случилось, ключевая деталь или цифра, последствие. "
        "Только факты, без вводных слов и без воды.\n"
        "3) importance — целое число от 1 до 10: насколько новость важна и срочна для широкой аудитории "
        "(10 = экстренное: катастрофа, теракт, отставка правительства, крупная авария; 1 = проходная заметка).\n"
        "4) category — РОВНО одна из строк: " + ", ".join(CATEGORIES) + ".\n"
        "5) hashtags — массив из 3-4 хэштегов по содержанию (без пробелов, с #).\n"
        "6) spain_focus — true/false: Испания является ГЛАВНОЙ темой статьи (место действия, кто\n"
        "   действует, кого касается), а не просто мельком упомянута (например, статья о ЧМ по футболу,\n"
        "   где просто упомянут клуб или город испанского футболиста, — это НЕ spain_focus, верни false).\n"
        "7) israel_related — true/false: статья каким-либо образом связана с Израилем, Палестиной,\n"
        "   Газой, Западным берегом, ХАМАС, Хезболлой или израильско-палестинским конфликтом\n"
        "   (даже если Израиль упомянут только частично или как одна из сторон события).\n\n"
        "Пиши живо и по-человечески, но кратко. НЕ упоминай стиль письма и не добавляй комментариев о нём.\n"
        "ВЕРНИ ТОЛЬКО JSON. Пример корректного ответа:\n"
        '{"title_ru":"...","bullets":["...","..."],"importance":6,"category":"politics",'
        '"hashtags":["#madrid","#gobierno"],"spain_focus":true,"israel_related":false}\n\n'
        "Текст статьи:\n\n" + article_text
    )

    cache = load_cache()
    # Стабильный ключ по содержимому статьи (+версия промпта) — работает между запусками,
    # в отличие от рандомизированного hash(). Экономит повторные вызовы для тех же новостей.
    cache_key = hashlib.sha256((PROMPT_VERSION + "\n" + article_text).encode("utf-8")).hexdigest()
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
                    temperature=0.4,
                    max_output_tokens=800,
                    response_mime_type="application/json"
                )
            )
            text = ""
            if hasattr(response, "text") and response.text:
                text = response.text
            else:
                raise Exception("No text in response from model")

            print(f"   📨 Gemini response length: {len(text)} chars")
            print(f"   📝 First 300 chars of raw response: {text[:300]}")

            text = clean_ai_response(text)

            # Парсим JSON (для нового формата ручной фолбэк непрактичен — при неудаче ретраим/fallback модели)
            parsed = parse_json_from_text(text)
            print(f"   🔍 JSON parsing result: {type(parsed)} {'(dict)' if isinstance(parsed, dict) else '(failed)'}")
            if not (parsed and isinstance(parsed, dict)):
                raise Exception("Model did not return a valid JSON object")

            title = str(parsed.get("title_ru", "")).strip()
            bullets = _coerce_bullets(parsed.get("bullets"))
            hashtags = parsed.get("hashtags", [])
            if isinstance(hashtags, str):
                hashtags = re.findall(r'#\w+', hashtags)
            importance = _coerce_importance(parsed.get("importance"))
            category = normalize_category(parsed.get("category"))
            # Дефолты выбраны в сторону базовой частоты: большинство статей после
            # RSS-фильтра действительно про Испанию и не про Израиль.
            spain_focus = _coerce_bool(parsed.get("spain_focus"), default=True)
            israel_related = _coerce_bool(parsed.get("israel_related"), default=False)

            if not title or len(bullets) < MIN_BULLETS or not isinstance(hashtags, list):
                raise Exception(
                    f"Incomplete response: title={bool(title)}, bullets={len(bullets)}, "
                    f"hashtags={type(hashtags).__name__}"
                )

            result = {
                "title_ru": title,
                "bullets": bullets,
                "importance": importance,
                "category": category,
                "hashtags": hashtags,
                "spain_focus": spain_focus,
                "israel_related": israel_related,
            }
            cache[cache_key] = result
            save_cache(cache)
            return result

        except Exception as e:
            last_error = str(e)
            le = last_error.lower()
            print(f"   ⚠️  Gemini error (model={model}): {last_error[:300]}")
            print(f"   📋 Error traceback:")
            traceback.print_exc()

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
    input_path = INPUT_FILE
    output_path = OUTPUT_FILE
    rejected_path = REJECTED_FILE

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
    seen_titles = []  # исходные заголовки из RSS
    seen_processed_titles = []  # переписанные AI заголовки (кэш ответов ведётся внутри gemini_request_single_json)

    for idx, news in enumerate(news_items, start=1):
        try:
            title = news.get("title", "").strip()
            description = news.get("description", "").strip()
            link = news.get("link", "").strip()

            print(f"\n{'='*70}")
            print(f"[{idx}/{len(news_items)}] {title[:80]}")
            print(f"{'='*70}")

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

            text_for_model = (title + ". " + (article_content or description or title))[:MAX_MODEL_INPUT_CHARS]

            # Минимальная задержка между вызовами к Gemini
            print(f"   💤 Ждём {GLOBAL_DELAY}s перед запросом к Gemini (глобальный rate limit)")
            time.sleep(GLOBAL_DELAY)

            print(f"   🤖 Отправляем запрос к Gemini...")
            try:
                ai_result = gemini_request_single_json(text_for_model)
                print(f"   ✨ Получен ответ от Gemini: title_ru={bool(ai_result.get('title_ru'))}, bullets={len(ai_result.get('bullets', []))}, importance={ai_result.get('importance')}, category={ai_result.get('category')}, hashtags_count={len(ai_result.get('hashtags', []))}")
            except Exception as e:
                print(f"   ❌ Проблема с Gemini: {e}")
                print(f"   📋 Full traceback:")
                traceback.print_exc()
                rejected_news.append({"title": title, "reason": f"gemini_error: {str(e)}"})
                continue

            # Семантическая проверка темы (AI понимает контекст лучше keyword-фильтра
            # на этапе fetch_news.py — например, ловит статьи, где Испания упомянута
            # только мельком, а не является главной темой)
            if not ai_result.get("spain_focus", True):
                print("   ⚠️ AI определил: не про Испанию по существу, пропускаем")
                rejected_news.append({"title": title, "reason": "not_spain_focus"})
                continue
            if ai_result.get("israel_related", False):
                print("   ⚠️ AI определил: связано с Израилем, пропускаем (политика канала)")
                rejected_news.append({"title": title, "reason": "israel_related_excluded"})
                continue

            # Формируем итоговые поля нового формата (заголовок + буллиты)
            rewritten_title = ai_result.get("title_ru", "").strip()
            bullets = ai_result.get("bullets", [])
            hashtags = ai_result.get("hashtags", [])
            importance = _coerce_importance(ai_result.get("importance"))
            category = normalize_category(ai_result.get("category"))

            def _clean_md(s):
                """Убирает символы, ломающие Telegram Markdown (backtick, непарные * и _)."""
                s = s.replace('`', '')
                s = re.sub(r'(?<!\*)\*(?!\*)', '', s)
                s = re.sub(r'(?<!_)_(?!_)', '', s)
                return s.strip()

            rewritten_title = _clean_md(rewritten_title)
            bullets = [_clean_md(b) for b in bullets if _clean_md(b)]

            print(f"   📝 AI результат: title='{rewritten_title[:50]}...', bullets={len(bullets)}, importance={importance}, category={category}, hashtags={hashtags}")

            # Текст для валидаций на русский язык / дубли / лимит длины (буллиты одной строкой)
            bullets_text = " ".join(bullets)

            # Валидации
            print(f"   🔍 Начинаем валидацию результатов...")
            if not rewritten_title:
                print("   ⚠️ Пустой заголовок от модели, пропускаем")
                rejected_news.append({"title": title, "reason": "empty_title"})
                continue
            if len(bullets) < MIN_BULLETS:
                print(f"   ⚠️ Мало пунктов ({len(bullets)}), пропускаем")
                rejected_news.append({"title": title, "reason": "few_bullets"})
                continue
            if not is_russian_text(rewritten_title):
                print(f"   ⚠️ Заголовок не на русском >=80%, пропускаем (title: '{rewritten_title[:50]}')")
                rejected_news.append({"title": title, "reason": "not_russian_title"})
                continue
            if not is_russian_text(bullets_text):
                print(f"   ⚠️ Пункты не на русском >=80%, пропускаем")
                rejected_news.append({"title": title, "reason": "not_russian_text"})
                continue
            if not hashtags or len(hashtags) < 2:
                print(f"   ⚠️ Мало хэштегов ({len(hashtags)}), пропускаем")
                rejected_news.append({"title": title, "reason": "few_hashtags"})
                continue
            # Проверка на дубликат среди переписанных заголовков (одно событие из разных источников)
            if is_duplicate(rewritten_title, seen_processed_titles):
                print(f"   ⚠️ Дубликат переписанного заголовка (одно событие из разных источников), пропускаем")
                rejected_news.append({"title": title, "reason": "duplicate_processed"})
                continue
            seen_processed_titles.append(rewritten_title)

            # description — плоский текст из буллитов (для трекера дублей и обратной совместимости)
            description = "\n".join(f"• {b}" for b in bullets)

            if not is_telegram_compatible(rewritten_title, description, link):
                print(f"   ⚠️ Превышает лимит Telegram (length={len(description)}), пропускаем")
                rejected_news.append({"title": title, "reason": "telegram_limit"})
                continue

            print(f"   ✅ ОК: {rewritten_title[:60]} / {len(bullets)} пунктов / importance {importance} / {category} / tags {len(hashtags)}")

            processed_news.append({
                "title": rewritten_title,
                "bullets": bullets,
                "importance": importance,
                "category": category,
                "hashtags": hashtags,
                "link": link,
                "description": description,
                "published": news.get("published", ""),
                "author": news.get("author", ""),
                "categories": news.get("categories", []),
                "image": news.get("image"),
                "processed_at": time.time()  # Временная метка обработки
            })
            
        except Exception as e:
            print(f"\n   💥 КРИТИЧЕСКАЯ ОШИБКА при обработке новости [{idx}/{len(news_items)}]")
            print(f"   ❌ Ошибка: {e}")
            print(f"   📋 Full traceback:")
            traceback.print_exc()
            rejected_news.append({"title": news.get("title", "Unknown"), "reason": f"processing_error: {str(e)}"})
            continue

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