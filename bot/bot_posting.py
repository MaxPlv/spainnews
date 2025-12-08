import os
import json
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Загрузка ключей
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Нужно добавить в .env ваш chat_id

# Список разрешенных пользователей (Telegram ID)
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", ADMIN_CHAT_ID or "").split(",") if x.strip()]

# Константы Telegram
MAX_MESSAGE_LENGTH = 4096

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

def format_news_text(news_item, max_length=MAX_MESSAGE_LENGTH):
    """
    Форматирует новость для Telegram с автоматической обрезкой при необходимости
    """
    text = f"📰 *{news_item['title']}*\n\n{news_item['description']}\n\n🔗 [Ссылка на источник]({news_item['link']})"
    
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



async def schedule_auto_posting(application: Application):
    """Планирует автоматическую публикацию новостей"""
    all_news = load_news()
    rejected = load_rejected_news()
    
    # Фильтруем новости по времени обработки - только из текущего цикла
    # Цикл каждые 2 часа, берем новости не старше 2.5 часов для запаса
    current_time = time.time()
    max_age_seconds = 2.5 * 60 * 60  # 2.5 часа
    
    news = []
    old_news_count = 0
    for item in all_news:
        processed_at = item.get("processed_at", 0)
        age_seconds = current_time - processed_at
        
        if age_seconds <= max_age_seconds:
            news.append(item)
        else:
            old_news_count += 1
            print(f"⏰ Пропущена старая новость (возраст: {age_seconds/3600:.1f}ч): {item.get('title', '')[:50]}...")
    
    if old_news_count > 0:
        print(f"🗑️  Отфильтровано {old_news_count} новостей из предыдущих циклов")
    
    if not news:
        if ADMIN_CHAT_ID:
            await application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"ℹ️ Нет новостей для публикации из текущего цикла.\n🚫 Отклонено AI: {len(rejected)}\n⏰ Старых новостей: {old_news_count}"
            )
        return

    # Формула: 2 часа / (количество новостей + 2)
    # 2 часа = 120 минут
    interval_minutes = 120 / (len(news) + 2)
    
    scheduler = application.bot_data.get("scheduler")
    if not scheduler:
        print("⚠️ Scheduler not found in bot_data")
        return

    # Очищаем старые задачи публикации (если есть)
    for job in scheduler.get_jobs():
        if job.id.startswith("auto_post_"):
            job.remove()

    scheduled_info = []
    now = datetime.now()
    
    for i, item in enumerate(news):
        run_date = now + timedelta(minutes=interval_minutes * (i + 1))
        job_id = f"auto_post_{i}"
        
        scheduler.add_job(
            publish_news,
            'date',
            run_date=run_date,
            args=[application.bot, item],
            id=job_id
        )
        scheduled_info.append(f"{i+1}. {item['title'][:30]}... в {run_date.strftime('%H:%M')}")

    # Отчет админу
    if ADMIN_CHAT_ID:
        rejected_summary = "\n".join([f"- {r['title'][:30]}... ({r['reason']})" for r in rejected[:5]])
        if len(rejected) > 5:
            rejected_summary += f"\n... и еще {len(rejected) - 5}"
            
        report = (
            f"🤖 *Автоматический режим*\n\n"
            f"✅ Одобрено: {len(news)}\n"
            f"🚫 Отклонено: {len(rejected)}\n\n"
            f"📅 *Расписание публикации:*\n" + "\n".join(scheduled_info) + "\n\n"
            f"🗑 *Причины отклонения (топ-5):*\n{rejected_summary if rejected else 'Нет отклоненных новостей'}"
        )
        
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=report,
            parse_mode="Markdown"
        )

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
    text = format_news_text(news_item)

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=False
    )

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