import feedparser
import json
from dateutil import parser as dateparser
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from url_tracker import URLTracker
from paths import DATA_DIR

NEWS_RAW_FILE = DATA_DIR / "news_raw.json"

# RSS источники
RSS_FEEDS = [
    "https://e00-elmundo.uecdn.es/elmundo/rss/espana.xml",
    "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"
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


def is_spain_related(text):
    """Проверяет, связана ли новость с Испанией"""
    text_lower = text.lower()
    
    # Длинные ключевые слова (можно искать как подстроку)
    spain_keywords = [
         # страны/регионы/автономные сообщества
        "españ", "españa", "madrid", 'valéncia', "barcelona", "valencia", "sevilla", "zaragoza", "bilbao",
        "andalucía", "cataluña", "galicia", "pais vasco", "país vasco", "comunidad valenciana",
        "castilla", "castilla y león", "castilla-la mancha", "la mancha", "navarra", "murcia", 
        "asturias", "cantabria", "aragón", "extremadura", "la rioja", "rioja",
        "baleares", "islas baleares", "canarias", "islas canarias",
        
        # 100 городов Испании
        "málaga", "palma", "las palmas", "alicante", "córdoba", "valladolid", "vigo", "gijón",
        "hospitalet", "vitoria", "coruña", "granada", "elche", "oviedo", "badalona", "cartagena",
        "terrassa", "jerez", "sabadell", "móstoles", "santa cruz", "pamplona", "almería", "fuenlabrada",
        "leganés", "getafe", "alcalá", "burgos", "salamanca", "san sebastián", "logroño", "badajoz",
        "albacete", "santander", "castellón", "alcorcón", "tarragona", "león", "cádiz", "huelva",
        "marbella", "mataró", "jaén", "parla", "algeciras", "torrejón", "ourense", "reus",
        "telde", "barakaldo", "lugo", "san fernando", "girona", "cáceres", "lorca", "coslada",
        "talavera", "el ejido", "torrevieja", "ávila", "palencia", "guadalajara", "melilla", "ceuta",
        "pontevedra", "manresa", "roquetas", "linares", "alcobendas", "zamora", "segovia", "cuenca",
        "torremolinos", "ronda", "benidorm", "aranjuez", "estepona", "alcoy", "gandía", "chiclana",
        "ferrol", "vélez", "elda", "toledo", "ponferrada", "puerto real", "benalmádena", "orihuela",
        "sagunto", "utrera", "torrent", "la línea", "motril", "paterna", "puertollano", "cornellá",
        "molina", "pozuelo", "rubí", "villanueva", "rivas",

        # органы власти
        "gobierno", "ayuntamiento", "comunidad autónoma",
        "policía nacional", "guardia civil", "tribunal supremo",
        "audiencia nacional", "seguridad social", "hacienda",

        # испанские компании
        "bbva", "santander", "caixabank", "iberdrola", "repsol", "endesa",

        # известные люди Испании (политики, королевская семья, бизнес, спорт, культура)
        # Политики
        "sánchez", "ayuso", "feijóo", "abascal", "yolanda díaz", "iglesias", "montero",
        "García-Page", "moreno bonilla", "mazón", "aragonés", "urkullu", "revilla",
        "García albiol", "edmundo bal", "espinosa de los monteros", "macarena olona",
        "rufián", "puigdemont", "junqueras", "arrimadas", "borrell", "calviño",
        "escrivá", "marlaska", "robles", "ribera", "bolaños", "alegría",
        "albares", "planas", "cuerpo", "urtasun", "montero", "belarra",
        
        # Королевская семья
        "felipe vi", "letizia", "leonor", "sofía", "juan carlos", "cristina de borbón",
        "iñaki urdangarin", "froilán",
        
        # Судьи и прокуроры
        "garcía castellón", "peinado", "marchena", "lesmes", "dolores delgado",
        "álvaro garcía", "garcía ortiz", "conde-pumpido",
        
        # Бизнесмены
        "amancio ortega", "sandra ortega", "botín", "Ana botín", "isak andic",
        "florentino pérez", "francisco gonzález", "juan roig", "del pino",
        "miguel fluxá", "entrecanales", "alierta", "álvarez",
        
        # Футболисты и тренеры
        "ancelotti", "xavi", "vinicius", "benzema", "modric", "kroos", "bellingham",
        "mbappé", "lewandowski", "ter stegen", "pedri", "gavi", "morata", "rodrigo",
        "rodri", "carvajal", "nacho", "yamal", "ansu fati", "ferran torres",
        "griezmann", "koke", "oblak", "courtois", "valverde", "camavinga",
        
        # Другие спортсмены
        "alcaraz", "nadal", "gasol", "lorenzo", "sainz", "alonso", "márquez",
        "mir", "belmonte", "craviotto",
        
        # Журналисты и медиа
        "ana rosa", "susanna griso", "carlos herrera", "alsina", "ferreras",
        "ana pastor", "évole", "buenafuente", "broncano", "pablo motos",
        "sandra golpe", "matías prats", "iker jiménez", "carmen porter",
        
        # Культура и искусство
        "almodóvar", "penélope cruz", "javier bardem", "antonio banderas",
        "rosalía", "alejandro sanz", "pablo alborán", "bisbal",
    ]
    
    # Короткие аббревиатуры (нужна проверка границ слов)
    import re
    short_abbrev = ["psoe", "pp", "vox", "sumar", "podemos", "erc", "junts"]
    
    # Проверяем длинные ключевые слова
    if any(keyword in text_lower for keyword in spain_keywords):
        return True
    
    # Проверяем короткие аббревиатуры с границами слов
    for abbrev in short_abbrev:
        # \b - граница слова (word boundary)
        if re.search(r'\b' + re.escape(abbrev) + r'\b', text_lower):
            return True
    
    return False


def is_not_advertisement(text):
    """Проверяет, что новость не является рекламой (улучшенная версия)"""
    text_lower = text.lower()
    
    # Сильные рекламные маркеры (если есть хотя бы один - точно реклама)
    strong_ad_keywords = [
        'comprar ahora', 'compra ahora', 'cómpralo',
        'haz clic aquí', 'pincha aquí',
        'solicita', 'solicitud gratuita',
        'llama ahora', 'contacta ahora',
        'visita nuestra tienda',
        'añadir al carrito',
        'pagar ahora',
    ]
    
    # Слабые рекламные маркеры (нужно несколько)
    weak_ad_keywords = [
        'oferta', 'descuento', 'promoción', 'rebaja',
        'precio especial', 'precio reducido',
        'ahorra', '% de descuento', 'gratis',
        'patrocinado', 'publicidad', 'anuncio',
        'suscríbete', 'suscripción', 'prueba gratis',
        'hasta agotar', 'por tiempo limitado',
        'liquidación', 'outlet', 'chollazo',
    ]
    
    # Если есть сильный маркер - это реклама
    if any(keyword in text_lower for keyword in strong_ad_keywords):
        return False
    
    # Считаем слабые маркеры
    weak_count = sum(1 for keyword in weak_ad_keywords if keyword in text_lower)
    
    # Если больше 2 слабых маркеров - скорее всего реклама
    return weak_count < 3


def is_valid_news(news_item):
    """Проверяет, является ли новость валидной (про Испанию и не реклама)"""
    title = news_item.get('title', '')
    description = news_item.get('description', '')
    
    # Объединяем заголовок и описание для проверки
    full_text = f"{title} {description}"
    
    # Проверяем оба условия
    spain_related = is_spain_related(full_text)
    not_ad = is_not_advertisement(full_text)
    
    return spain_related and not_ad


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





if __name__ == "__main__":
    # Инициализируем трекер URL
    url_tracker = URLTracker()
    
    # Очищаем старые URL (старше 24 часов)
    removed_count = url_tracker.cleanup_old_urls()
    if removed_count > 0:
        print(f"🧹 Очищено {removed_count} старых URL из базы (>24ч)\n")
    
    # Получаем новые новости
    news = fetch_recent_news()
    print(f"\n📰 Получено {len(news)} свежих новостей из RSS\n")
    
    # Проверяем на дубликаты через URL трекер
    unique_news = []
    duplicates_count = 0
    new_urls_to_track = []
    
    for news_item in news:
        url = news_item.get('link', '')
        if url and url_tracker.is_duplicate(url):
            duplicates_count += 1
        else:
            unique_news.append(news_item)
            # Сразу добавляем URL в список для отслеживания
            if url:
                new_urls_to_track.append(url)
    
    # Немедленно сохраняем URL в трекер, чтобы избежать дубликатов при параллельных запусках
    if new_urls_to_track:
        added_urls = url_tracker.add_urls_batch(new_urls_to_track)
        print(f"💾 Добавлено {added_urls} новых URL в базу отслеживания (защита от дубликатов)")
    
    if duplicates_count > 0:
        print(f"🗑️  Отклонено {duplicates_count} дубликатов (URL уже обработаны)")
    
    print(f"✨ Уникальных новостей: {len(unique_news)}\n")

    # Фильтруем новости: оставляем только про Испанию и не рекламные
    filtered_news = []
    rejected_count = 0
    rejected_reasons = {
        'not_spain': 0,
        'advertisement': 0,
        'both': 0
    }

    for news_item in unique_news:
        spain_related = is_spain_related(f"{news_item['title']} {news_item['description']}")
        not_ad = is_not_advertisement(f"{news_item['title']} {news_item['description']}")
        
        if spain_related and not_ad:
            filtered_news.append(news_item)
        else:
            rejected_count += 1
            # Определяем причину отклонения
            if not spain_related and not not_ad:
                rejected_reasons['both'] += 1
                reason = "не про Испанию + реклама"
            elif not spain_related:
                rejected_reasons['not_spain'] += 1
                reason = "не про Испанию"
            else:
                rejected_reasons['advertisement'] += 1
                reason = "реклама"
            
            print(f"❌ Отклонено ({reason}): {news_item['title'][:60]}...")

    print(f"\n🚫 Отклонено {rejected_count} новостей:")
    print(f"   📍 Не про Испанию: {rejected_reasons['not_spain']}")
    print(f"   🛒 Реклама: {rejected_reasons['advertisement']}")
    print(f"   ⚠️  Оба критерия: {rejected_reasons['both']}")
    print(f"✅ Прошло проверку: {len(filtered_news)} новостей\n")
    
    # Выводим информацию о новостях
    for n in filtered_news:
        print(f"🧩 {n['title']}")
        print(f"   🕒 {n['published']}")
        print(f"   🔗 {n['link']}")
        print(f"   ✍️  {n['author']}")
        print(f"   🖼️  {n['image']}")
        print(f"   🗂️  {[tag['term'] for tag in n['categories']] if n['categories'] else []}")
        print()

    # Сохраняем отфильтрованные новости (перезапись файла)
    with open(NEWS_RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(filtered_news, f, ensure_ascii=False, indent=2)

    print(f"✅ Сохранено {len(filtered_news)} новостей в {NEWS_RAW_FILE}")
    
    # Финальная статистика
    stats = url_tracker.get_stats()
    print(f"📊 Всего URL в базе: {stats['total_urls']}")