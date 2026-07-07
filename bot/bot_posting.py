import os
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from bot.published_news_tracker import check_duplicate, add_published_news
from bot.categories import category_emoji, category_label, CATEGORY_ORDER
from bot.digest_buffer import add_to_digest, load_pending, clear_pending, pending_count

# Загрузка ключей
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Нужно добавить в .env ваш chat_id

# Список разрешенных пользователей (Telegram ID)
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", ADMIN_CHAT_ID or "").split(",") if x.strip()]

# Константы Telegram
MAX_MESSAGE_LENGTH = 4096

# --- Конфигурация дайджеста / маршрутизации по важности ---
# Новости с importance >= URGENT_THRESHOLD публикуются сразу отдельным постом,
# остальные копятся в буфер и уходят одним дайджестом по расписанию.
URGENT_THRESHOLD = int(os.getenv("URGENT_THRESHOLD", "8"))
# Часы публикации дайджеста (по локальному времени сервера), напр. "9,15,21"
DIGEST_HOURS = os.getenv("DIGEST_HOURS", "9,15,21")
# Тихие часы: срочное всё равно выходит, дайджест в это время не публикуется
QUIET_START = int(os.getenv("QUIET_START", "0"))
QUIET_END = int(os.getenv("QUIET_END", "8"))


def in_quiet_hours(now=None):
    """True, если сейчас тихие часы (интервал может пересекать полночь)."""
    now = now or datetime.now()
    h = now.hour
    if QUIET_START == QUIET_END:
        return False
    if QUIET_START < QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END

# Определяем корневую директорию проекта
PROJECT_ROOT = Path(__file__).parent.parent
RESULT_NEWS_FILE = PROJECT_ROOT / "result_news.json"
REJECTED_NEWS_FILE = PROJECT_ROOT / "rejected_news.json"
SETTINGS_FILE = PROJECT_ROOT / "settings.json"

# Загружаем новости
def load_news():
    """Загружает обработанные новости из result_news.json"""
    try:
        if not RESULT_NEWS_FILE.exists():
            print(f"ℹ️  Файл {RESULT_NEWS_FILE} не существует, возвращаем пустой список")
            return []
        with open(RESULT_NEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Ошибка загрузки {RESULT_NEWS_FILE}: {e}")
        return []

def load_rejected_news():
    """Загружает отклоненные новости из rejected_news.json"""
    try:
        if not REJECTED_NEWS_FILE.exists():
            return []
        with open(REJECTED_NEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Ошибка загрузки {REJECTED_NEWS_FILE}: {e}")
        return []

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"mode": "manual"}

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

def escape_markdown(text):
    """
    Экранирует специальные символы Markdown для Telegram.
    Оставляет только намеренно используемые символы форматирования.
    """
    # Символы, которые нужно экранировать в MarkdownV2
    # Но мы используем обычный Markdown, поэтому экранируем только проблемные
    # символы, которые могут создать незакрытые entities
    
    # Заменяем одиночные * и _ на экранированные версии
    # но сохраняем парные для форматирования
    result = text
    
    # Временно заменяем наши намеренные маркеры
    result = result.replace('📰 *', '📰 __BOLD_START__')
    result = result.replace('*\n', '__BOLD_END__\n')
    
    # Экранируем все остальные звездочки и подчеркивания
    result = result.replace('*', '\\*')
    result = result.replace('_', '\\_')
    
    # Возвращаем наши маркеры
    result = result.replace('__BOLD_START__', '*')
    result = result.replace('__BOLD_END__', '*')
    
    # Дополнительно экранируем другие проблемные символы
    result = result.replace('[', '\\[')
    result = result.replace(']', '\\]')
    result = result.replace('(', '\\(')
    result = result.replace(')', '\\)')
    
    return result

def build_news_body(news_item):
    """
    Собирает тело карточки новости: эмодзи-рубрика + жирный заголовок,
    короткие буллиты, хэштеги. Без ссылки на источник (её добавляет format_news_text).
    Поддерживает и новый формат (bullets), и старый (description).
    """
    title = news_item.get('title', '').replace('`', '')
    emoji = category_emoji(news_item.get('category'))

    bullets = news_item.get('bullets')
    if isinstance(bullets, list) and bullets:
        body = "\n".join(f"▪️ {str(b).replace('`', '')}" for b in bullets)
    else:
        # Обратная совместимость со старым форматом
        body = news_item.get('description', '').replace('`', '')

    parts = [f"{emoji} *{title}*", "", body]

    hashtags = news_item.get('hashtags')
    if isinstance(hashtags, list) and hashtags:
        parts.append("")
        parts.append(" ".join(hashtags[:4]))

    return "\n".join(parts)


def format_news_text(news_item, max_length=MAX_MESSAGE_LENGTH):
    """
    Форматирует новость для Telegram с автоматической обрезкой при необходимости
    """
    body = build_news_body(news_item)
    text = f"{body}\n\n🔗 [Ссылка на источник]({news_item['link']})"

    # Проверяем длину и обрезаем при необходимости
    if len(text) <= max_length:
        return text

    # Резервируем место для сообщения об обрезке
    truncate_suffix = "\n\n... _(текст обрезан из-за ограничений Telegram)_"
    available_length = max_length - len(truncate_suffix)

    # Обрезаем текст
    truncated = text[:available_length]

    # Пытаемся обрезать по последнему полному предложению или слову
    last_period = truncated.rfind('.')
    last_space = truncated.rfind(' ')

    if last_period > available_length * 0.8:  # Если точка близко к концу
        truncated = truncated[:last_period + 1]
    elif last_space > available_length * 0.9:  # Если пробел близко к концу
        truncated = truncated[:last_space]

    return truncated + truncate_suffix

async def send_news_to_admin(application: Application):
    """Автоматически отправляет новости админу при запуске бота"""
    news = load_news()

    if not ADMIN_CHAT_ID:
        print("⚠️  ADMIN_CHAT_ID не установлен в .env")
        return

    application.bot_data["news"] = news
    application.bot_data["index"] = 0

    await send_next_news_to_admin(application)



def load_current_cycle_news(max_age_hours=3):
    """
    Возвращает обработанные новости только из текущего цикла
    (по метке processed_at), отсекая остатки прошлых циклов.
    """
    all_news = load_news()
    current_time = time.time()
    max_age_seconds = max_age_hours * 60 * 60

    news = []
    old_news_count = 0
    for item in all_news:
        age_seconds = current_time - item.get("processed_at", 0)
        if age_seconds <= max_age_seconds:
            news.append(item)
        else:
            old_news_count += 1

    if old_news_count > 0:
        print(f"🗑️  Отфильтровано {old_news_count} новостей из предыдущих циклов")
    return news


async def schedule_auto_posting(application: Application):
    """
    Авто-режим: маршрутизирует новости текущего цикла по важности.
      • importance >= URGENT_THRESHOLD  → публикуем сразу отдельным постом;
      • остальное                        → копим в буфер дайджеста.
    Публикацией дайджеста занимается publish_digest по расписанию (3 слота в день).
    """
    news = load_current_cycle_news()
    rejected = load_rejected_news()

    if not news:
        if ADMIN_CHAT_ID:
            await application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"ℹ️ Нет новостей из текущего цикла.\n"
                    f"🚫 Отклонено AI: {len(rejected)}\n"
                    f"🗂 В буфере дайджеста: {pending_count()}"
                )
            )
        return

    urgent = [n for n in news if n.get("importance", 0) >= URGENT_THRESHOLD]
    routine = [n for n in news if n.get("importance", 0) < URGENT_THRESHOLD]

    # Срочные — сразу в канал (обходят тихие часы и дайджест)
    published_urgent = []
    for item in urgent:
        try:
            await publish_news(application.bot, item)
            published_urgent.append(item)
        except Exception as e:
            print(f"⚠️ Ошибка публикации срочной новости: {e}")

    # Рутина — в буфер дайджеста
    added = add_to_digest(routine)

    # Отчёт админу
    if ADMIN_CHAT_ID:
        urgent_summary = "\n".join(f"🚨 {i['title'][:40]}..." for i in published_urgent) or "—"
        report = (
            f"🤖 *Авто-режим: маршрутизация*\n\n"
            f"✅ Обработано из цикла: {len(news)}\n"
            f"🚨 Срочных опубликовано сразу: {len(published_urgent)}\n"
            f"🗂 Добавлено в дайджест: {added}\n"
            f"📦 Всего в буфере дайджеста: {pending_count()}\n"
            f"🚫 Отклонено AI: {len(rejected)}\n\n"
            f"*Срочные:*\n{urgent_summary}"
        )
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=report,
            parse_mode="Markdown"
        )


def build_digest_messages(items, header=None):
    """
    Собирает дайджест из буфера: группировка по рубрикам, каждый пункт —
    одна короткая строка-заголовок со ссылкой на источник.
    Возвращает список сообщений (разбитых по лимиту Telegram).
    """
    # Группируем по рубрикам
    by_cat = {}
    for it in items:
        cat = it.get("category", "other")
        by_cat.setdefault(cat, []).append(it)

    blocks = []
    for cat in CATEGORY_ORDER:
        cat_items = by_cat.get(cat)
        if not cat_items:
            continue
        lines = [f"{category_emoji(cat)} *{category_label(cat)}*"]
        for it in cat_items:
            title = it.get("title", "").replace("`", "").replace("[", "(").replace("]", ")")
            link = it.get("link", "")
            if link:
                lines.append(f"▪️ [{title}]({link})")
            else:
                lines.append(f"▪️ {title}")
        blocks.append("\n".join(lines))

    if not blocks:
        return []

    # Собираем блоки в сообщения с учётом лимита длины (рубрики через пустую строку)
    messages = []
    current = header.strip() if header else ""
    for block in blocks:
        candidate = (current + "\n\n" + block) if current else block
        if len(candidate) > MAX_MESSAGE_LENGTH and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


async def publish_digest(bot):
    """
    Публикует накопленный дайджест в канал одним (или несколькими) сообщениями
    и очищает буфер. Дубли отсекаются по истории публикаций.
    """
    items = load_pending()
    if not items:
        print("🗂 Буфер дайджеста пуст — публиковать нечего")
        return

    # Отсекаем то, что уже выходило (например, срочным постом или в прошлом дайджесте)
    fresh = []
    skipped_dup = 0
    for it in items:
        dup = check_duplicate(
            title=it.get("title", ""),
            text=it.get("description", ""),
            similarity_threshold=0.85,
        )
        if dup["is_duplicate"]:
            skipped_dup += 1
        else:
            fresh.append(it)

    if not fresh:
        print(f"🗂 Все {len(items)} новостей из буфера — дубли, дайджест не публикуем")
        clear_pending()
        return

    now = datetime.now()
    header = f"🗞 *Дайджест новостей Испании*\n_{now.strftime('%d.%m, %H:%M')} · {len(fresh)} новостей_"
    messages = build_digest_messages(fresh, header=header)

    for msg in messages:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # Помечаем как опубликованные и чистим буфер
    for it in fresh:
        add_published_news(
            title=it.get("title", ""),
            text=it.get("description", ""),
            url=it.get("link", ""),
        )
    clear_pending()

    print(f"✅ Дайджест опубликован: {len(fresh)} новостей ({len(messages)} сообщ.), дублей пропущено: {skipped_dup}")

    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🗞 Дайджест опубликован\n"
                    f"✅ Новостей: {len(fresh)}\n"
                    f"🗑 Дублей пропущено: {skipped_dup}\n"
                    f"✉️ Сообщений: {len(messages)}"
                ),
            )
        except Exception as e:
            print(f"⚠️ Не удалось отправить отчёт админу: {e}")


async def publish_digest_job(bot):
    """Обёртка для планировщика: уважает тихие часы."""
    if in_quiet_hours():
        print("🌙 Тихие часы — дайджест отложен до следующего слота")
        return
    await publish_digest(bot)


async def send_next_news_to_admin(application: Application):
    """Отправляет следующую новость админу"""
    news = application.bot_data.get("news", [])
    idx = application.bot_data.get("index", 0)

    if not news:
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="ℹ️ Нет новостей для проверки."
        )
        return

    if idx >= len(news):
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="✅ Все новости просмотрены!"
        )

        return

    n = news[idx]
    text = format_news_text(n)

    keyboard = [
        [
            InlineKeyboardButton("Опубликовать сейчас", callback_data="0"),
            InlineKeyboardButton("Через 30 мин", callback_data="30"),
        ],
        [
            InlineKeyboardButton("1 час", callback_data="60"),
            InlineKeyboardButton("2 часа", callback_data="120"),
            InlineKeyboardButton("3 часа", callback_data="180"),
        ],
        [
            InlineKeyboardButton("⏭️ Пропустить", callback_data="skip"),
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await application.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=False
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start для ручного запуска"""
    user_id = update.effective_user.id

    # Проверяем, есть ли пользователь в списке разрешенных
    if user_id not in ALLOWED_USERS:
        print(f"⚠️  Попытка доступа от неразрешенного пользователя: {user_id}")
        return

    settings = load_settings()
    current_mode = settings.get("mode", "manual")
    
    keyboard = [
        [
            InlineKeyboardButton(f"{'✅ ' if current_mode == 'manual' else ''}Ручной режим", callback_data="mode_manual"),
            InlineKeyboardButton(f"{'✅ ' if current_mode == 'auto' else ''}Автоматический", callback_data="mode_auto"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🤖 Бот управления новостями\nТекущий режим: *{current_mode}*", 
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    if current_mode == "manual":
        await send_news_to_admin(context.application)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # Проверяем, есть ли пользователь в списке разрешенных
    if user_id not in ALLOWED_USERS:
        await query.answer("❌ У вас нет доступа к этому боту", show_alert=True)
        print(f"⚠️  Попытка взаимодействия от неразрешенного пользователя: {user_id}")
        return

    await query.answer()

    news = context.application.bot_data.get("news", [])
    idx = context.application.bot_data.get("index", 0)
    
    # Обработка смены режима
    if query.data.startswith("mode_"):
        new_mode = query.data.split("_")[1]
        settings = load_settings()
        settings["mode"] = new_mode
        save_settings(settings)
        
        keyboard = [
            [
                InlineKeyboardButton(f"{'✅ ' if new_mode == 'manual' else ''}Ручной режим", callback_data="mode_manual"),
                InlineKeyboardButton(f"{'✅ ' if new_mode == 'auto' else ''}Автоматический", callback_data="mode_auto"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🤖 Бот управления новостями\nТекущий режим: *{new_mode}*\n\n✅ Режим изменен!",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    n = news[idx]

    if query.data == "skip":
        await query.edit_message_text(f"⏭️ Пропущено: {n['title']}")
    else:
        delay_minutes = int(query.data)

        if delay_minutes == 0:
            # Публикуем сразу
            await publish_news(context.bot, n)
            await query.edit_message_text(f"✅ Опубликовано: {n['title']}")
        else:
            # Планируем отправку в фоне
            await query.edit_message_text(f"✅ Запланировано: {n['title']} (через {delay_minutes} мин)")
            asyncio.create_task(schedule_post(context, n, delay_minutes))

    context.application.bot_data["index"] = idx + 1
    await asyncio.sleep(1)
    await send_next_news_to_admin(context.application)

async def publish_news(bot, news_item):
    """Публикует новость в канал"""
    # Проверяем на дубликат перед публикацией
    duplicate_check = check_duplicate(
        title=news_item.get('title', ''),
        text=news_item.get('description', ''),
        similarity_threshold=0.85
    )
    
    if duplicate_check['is_duplicate']:
        match = duplicate_check['match']
        similarity = duplicate_check['similarity_score']
        matched_by = duplicate_check['matched_by']
        
        print(f"\n⚠️  ДУБЛИКАТ ОБНАРУЖЕН!")
        print(f"   Новая новость: {news_item.get('title', '')[:60]}...")
        print(f"   Похожа на: {match.get('title', '')[:60]}...")
        print(f"   Схожесть: {similarity*100:.1f}% (по {matched_by})")
        print(f"   Опубликована: {match.get('published_at', '')}")
        print(f"   🚫 Публикация отменена\n")
        
        # Уведомляем админа о пропуске дубликата
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🚫 *Дубликат пропущен*\n\n"
                        f"📰 {news_item.get('title', '')[:100]}\n\n"
                        f"Похожа на новость от {match.get('published_at', '')[:10]}\n"
                        f"Схожесть: {similarity*100:.0f}%"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Ошибка отправки уведомления админу: {e}")
        return
    
    # Публикуем новость
    text = format_news_text(news_item)

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=False
    )
    
    # Добавляем в историю опубликованных новостей
    add_published_news(
        title=news_item.get('title', ''),
        text=news_item.get('description', ''),
        url=news_item.get('link', '')
    )
    print(f"✅ Новость опубликована и добавлена в историю: {news_item.get('title', '')[:60]}...")

async def schedule_post(context, news_item, delay_minutes):
    """Планирует отложенную публикацию"""
    await asyncio.sleep(delay_minutes * 60)
    await publish_news(context.bot, news_item)

async def post_init(application: Application):
    """Автоматически запускается после инициализации бота"""

    print("✅ Бот инициализирован")
    # При старте ничего не делаем, ждем команды или шедулера
    # await send_news_to_admin(application)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()