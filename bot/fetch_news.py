import feedparser
import json
from dateutil import parser as dateparser
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# RSS источники
RSS_FEEDS = [
    "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
    "https://e00-elmundo.uecdn.es/elmundo/rss/espana.xml"
    # можно добавить ещё
]

def extract_image(entry):
    """Извлекает первую подходящую картинку из разных полей RSS item"""
    # 1️⃣ media:thumbnail
    if "media_thumbnail" in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get("url")

    # 2️⃣ media:content (если это видео, то thumbnail внутри)
    if "media_content" in entry and len(entry.media_content) > 0:
        for media in entry.media_content:
            if "medium" in media and media["medium"] == "image" and "url" in media:
                return media["url"]
            if "url" in media and "jpg" in media["url"]:
                return media["url"]

    # 3️⃣ enclosure (классический RSS)
    if "enclosures" in entry and len(entry.enclosures) > 0:
        return entry.enclosures[0].get("url")

    # 4️⃣ внутри контента ищем <img>
    if "content" in entry and len(entry.content) > 0:
        html = entry.content[0].value
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]

    # 5️⃣ в description
    if "description" in entry:
        soup = BeautifulSoup(entry.description, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]

    return None


def fetch_recent_news(max_age_hours=2):
    now = datetime.now(timezone.utc)
    news_items = []

    for feed_url in RSS_FEEDS:
        print(f"🔹 Fetching: {feed_url}")
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            # получаем дату
            pub_date = None
            if hasattr(entry, "published"):
                try:
                    pub_date = dateparser.parse(entry.published)
                except Exception:
                    pass
            elif hasattr(entry, "updated"):
                try:
                    pub_date = dateparser.parse(entry.updated)
                except Exception:
                    pass

            # если даты нет — пропускаем
            if not pub_date:
                continue

            # фильтрация по времени
            if (now - pub_date) > timedelta(hours=max_age_hours):
                continue

            news = {
                "title": entry.title.strip() if hasattr(entry, "title") else "",
                "link": entry.link if hasattr(entry, "link") else "",
                "description": entry.get("description", "").strip(),
                "published": pub_date.isoformat(),
                "author": entry.get("author", ""),
                "categories": entry.get("tags", []),
                "image": extract_image(entry),
            }
            news_items.append(news)

    return news_items


def remove_duplicates(new_news, existing_news):
    """Удаляет дубликаты из новых новостей, проверяя по ссылке на оригинал"""
    # Создаём set из ссылок существующих новостей
    existing_links = {item['link'] for item in existing_news if 'link' in item}

    # Фильтруем новые новости
    unique_news = []
    duplicates_count = 0

    for news in new_news:
        if news['link'] not in existing_links:
            unique_news.append(news)
        else:
            duplicates_count += 1

    return unique_news, duplicates_count


if __name__ == "__main__":
    # Загружаем существующие новости из файла
    existing_news = []
    try:
        with open("news_raw.json", "r", encoding="utf-8") as f:
            existing_news = json.load(f)
        print(f"📂 Загружено {len(existing_news)} существующих новостей из news_raw.json")
    except FileNotFoundError:
        print("📂 Файл news_raw.json не найден, создаём новый")
    except json.JSONDecodeError:
        print("⚠️  Файл news_raw.json повреждён, создаём новый")

    # Получаем новые новости
    news = fetch_recent_news()
    print(f"\n📰 Получено {len(news)} свежих новостей из RSS\n")

    # Удаляем дубликаты
    unique_news, duplicates = remove_duplicates(news, existing_news)

    if duplicates > 0:
        print(f"🗑️  Удалено {duplicates} дубликатов")

    print(f"✨ Уникальных новостей: {len(unique_news)}\n")

    # Выводим информацию о новостях
    for n in unique_news:
        print(f"🧩 {n['title']}")
        print(f"   🕒 {n['published']}")
        print(f"   🔗 {n['link']}")
        print(f"   ✍️  {n['author']}")
        print(f"   🖼️  {n['image']}")
        print(f"   🗂️  {[tag['term'] for tag in n['categories']] if n['categories'] else []}")
        print()

    # Сохраняем только уникальные новости
    with open("news_raw.json", "w", encoding="utf-8") as f:
        json.dump(unique_news, f, ensure_ascii=False, indent=2)

    print("✅ Уникальные новости сохранены в news_raw.json — теперь запусти process_ai.py")