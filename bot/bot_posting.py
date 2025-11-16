import os
import json
import asyncio
from datetime import datetime, timedelta
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

# Загружаем новости
def load_news():
    with open("result_news.json", "r", encoding="utf-8") as f:
        return json.load(f)

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

async def send_next_news_to_admin(application: Application):
    """Отправляет следующую новость админу"""
    news = application.bot_data.get("news", [])
    idx = application.bot_data.get("index", 0)

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

    await update.message.reply_text("🤖 Бот запущен и работает в автоматическом режиме!")
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
    print("✅ Бот инициализирован, отправка новостей...")
    await send_news_to_admin(application)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()