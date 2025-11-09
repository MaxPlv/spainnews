import os
import json
from difflib import SequenceMatcher
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# --- Google GenAI SDK Imports ---
from google import genai
from google.genai import types
# --------------------------------

# Загрузка .env файла
def load_env_file():
    # Ищем .env в родительской директории (корне проекта)
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    # Используем os.environ.setdefault для предотвращения перезаписи, если переменная уже установлена
                    os.environ.setdefault(key.strip(), value.strip())

# Загружаем переменные окружения
load_env_file()

# Загрузка ключа из переменной окружения
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    # NOTE: В отличие от HTTP-вызова, SDK попытается найти ключ,
    # но лучше убедиться, что он есть.
    raise Exception("Установите переменную GEMINI_API_KEY")

# Настройки
DUPLICATE_THRESHOLD = 0.8
INPUT_FILE = "news_raw.json"
OUTPUT_FILE = "result_news.json"
IMAGES_DIR = "processed_images"

os.makedirs(IMAGES_DIR, exist_ok=True)

def is_duplicate(title, seen_titles):
    """Проверяет, является ли заголовок дубликатом уже виденных."""
    for seen in seen_titles:
        if SequenceMatcher(None, title.lower(), seen.lower()).ratio() > DUPLICATE_THRESHOLD:
            return True
    return False

def fetch_article_content(url):
    """
    Загружает содержимое статьи по ссылке и извлекает текст.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Удаляем ненужные элементы
        for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
            element.decompose()

        # Ищем основной текст статьи (разные варианты для разных сайтов)
        article_text = ""

        # Попытка 1: article tag
        article = soup.find('article')
        if article:
            paragraphs = article.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

        # Попытка 2: основной контент по классам
        if not article_text:
            content_divs = soup.find_all(['div'], class_=lambda x: x and any(word in str(x).lower() for word in ['article', 'content', 'story', 'post']))
            for div in content_divs:
                paragraphs = div.find_all('p')
                text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
                if len(text) > len(article_text):
                    article_text = text

        # Попытка 3: все параграфы на странице
        if not article_text or len(article_text) < 200:
            paragraphs = soup.find_all('p')
            article_text = ' '.join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])

        return article_text[:10000] if article_text else ""  # Ограничиваем до 10000 символов

    except Exception as e:
        print(f"   ⚠️  Ошибка загрузки статьи: {e}")
        return ""

def clean_ai_response(text):
    """
    Удаляет вводные фразы и мусор из ответа ИИ.
    """
    # Список фраз, которые нужно удалить
    phrases_to_remove = [
        "Вот краткая выжимка:",
        "Краткая выжимка:",
        "Вот краткое изложение:",
        "Краткое изложение:",
        "Вот перевод:",
        "Перевод:",
        "Вот заголовок:",
        "Заголовок:",
        "Вот пересказ:",
        "Пересказ:",
        "Вот новость:",
        "Новость:",
    ]

    result = text.strip()

    # Удаляем фразы в начале текста
    for phrase in phrases_to_remove:
        if result.startswith(phrase):
            result = result[len(phrase):].strip()

    # Удаляем кавычки в начале и конце, если они есть
    if (result.startswith('"') and result.endswith('"')) or (result.startswith("'") and result.endswith("'")):
        result = result[1:-1].strip()

    return result

def rewrite_and_translate_with_gemini(text, is_title=False):
    """
    Пересказывает и переводит текст с помощью Gemini API, используя официальный SDK.
    """
    try:
        # Клиент автоматически найдет ключ из переменной окружения GEMINI_API_KEY
        client = genai.Client()
    except Exception as e:
        raise Exception(f"Не удалось инициализировать GenAI клиент. Проверьте установку SDK и ключ: {e}")

    if is_title:
        prompt = "Ты редактор новостного Telegram-канала про жизнь в Испании. Переведи заголовок новости на русский язык и немного переформулируй его, сохраняя суть. Заголовок должен быть кратким (до 10 слов). Верни только заголовок, без кавычек, без вводных фраз, без дополнительного текста. Заголовок: " + text
    else:
        prompt = (
            "Ты редактор новостного Telegram-канала про жизнь в Испании. "
            "Прочитай полный текст статьи и выполни следующие задачи:\n\n"
            "1. ОЦЕНКА ПРИГОДНОСТИ: Определи, подходит ли новость для публикации в канале про Испанию. Критерии:\n"
            "   - ✅ ПОДХОДИТ: новость ТОЛЬКО про Испанию или напрямую касается Испании (политика, экономика, культура, общество, происшествия, спорт в Испании)\n"
            "   - ❌ НЕ ПОДХОДИТ: международные темы без прямого отношения к Испании, явная или скрытая реклама, новости других стран\n\n"
            "2. ФОРМАТ ОТВЕТА: Верни текст строго в таком формате:\n"
            "[SUITABLE] - если новость подходит\n"
            "[NOT_SUITABLE] - если не подходит\n\n"
            "Затем через новую строку:\n"
            "Краткая выжимка на русском языке (5-7 предложений, максимум 3500 символов). "
            "Сохрани все ключевые факты, цифры и детали. Только нейтральный пересказ, без мнений и вводных фраз. "
            "Разбей текст на абзацы, если допустимо.\n\n"
            "ВАЖНО: Текст ОБЯЗАТЕЛЬНО должен быть ПОЛНОСТЬЮ ЗАВЕРШЁН. Последнее предложение должно заканчиваться точкой, восклицательным или вопросительным знаком. "
            "НЕ обрывай текст на полуслове или в середине предложения!\n\n"
            "В конце через пустую строку добавь 3-4 хэш-тэга на основе текста.\n\n"
            f"Текст статьи: {text}"
        )

    # Конфигурация генерации
    # Увеличиваем max_output_tokens для полной обработки статей
    # Telegram лимит 4096 символов, но с учетом форматирования используем ~3500
    config = types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=3500 if not is_title else 100
    )

    try:
        # Используем модель gemini-1.5-flash
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=config,
        )

        # SDK возвращает объект с полем .text, которое содержит сгенерированный текст.
        # Дополнительный парсинг JSON не требуется.
        if response.text:
            # Очищаем ответ от вводных фраз
            cleaned_text = clean_ai_response(response.text)
            return cleaned_text

        # Если ответ пуст
        return "Не удалось сгенерировать текст (ответ модели пуст)."

    except Exception as e:
        # Обработка ошибок SDK (например, API_KEY недействителен, лимиты и т.д.)
        print(f"Ошибка вызова Gemini API через SDK: {e}")
        raise

def main():
    # Получаем путь к news_raw.json
    input_path = Path(__file__).parent.parent / INPUT_FILE
    output_path = Path(__file__).parent.parent / OUTPUT_FILE

    if not input_path.exists():
        print(f"❌ Файл {INPUT_FILE} не найден. Сначала запустите fetch_news.py")
        return

    # Загружаем новости из news_raw.json
    print(f"📂 Загрузка новостей из {INPUT_FILE}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        news_items = json.load(f)

    print(f"✅ Загружено {len(news_items)} новостей")

    # Обрабатываем новости
    processed_news = []
    seen_titles = []

    for idx, news in enumerate(news_items, 1):
        title = news.get("title", "")
        description = news.get("description", "")

        print(f"\n[{idx}/{len(news_items)}] Обработка: {title[:50]}...")

        # Проверка на дубликат
        if is_duplicate(title, seen_titles):
            print("   ⚠️  Дубликат, пропускаем")
            continue

        seen_titles.append(title)

        # Обработка заголовка и текста через Gemini
        try:
            rewritten_title = rewrite_and_translate_with_gemini(title, is_title=True)

            # Загружаем полный текст статьи по ссылке
            link = news.get("link", "")
            print(f"   🔗 Загрузка полного текста статьи...")
            article_content = fetch_article_content(link)

            # Если удалось загрузить статью, используем её полный текст, иначе только title + description
            if article_content:
                text_to_process = f"{title}. {article_content}"
                print(f"   📄 Загружено {len(article_content)} символов")
            else:
                text_to_process = f"{title}. {description}"
                print(f"   ⚠️  Не удалось загрузить полный текст, используем description")

            rewritten_text = rewrite_and_translate_with_gemini(text_to_process)

            # Проверяем, что заголовок и описание не пустые и не содержат ошибки
            if not rewritten_title or not rewritten_title.strip() or "Не удалось сгенерировать текст" in rewritten_title:
                print(f"   ⚠️  Пустой или некорректный заголовок после обработки ИИ, пропускаем новость")
                continue

            if not rewritten_text or not rewritten_text.strip() or "Не удалось сгенерировать текст" in rewritten_text:
                print(f"   ⚠️  Пустое или некорректное описание после обработки ИИ, пропускаем новость")
                continue

            print(f"   ✅ Обработано: {rewritten_title[:50]}... / {rewritten_text[:50]}...")

            # Сохраняем обработанную новость
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

    # Сохраняем результат в result_news.json
    print(f"\n💾 Сохранение обработанных новостей в {OUTPUT_FILE}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(processed_news, f, ensure_ascii=False, indent=2)

    print(f"✅ Успешно обработано и сохранено {len(processed_news)} новостей в {OUTPUT_FILE}")

if __name__ == "__main__":
    main()