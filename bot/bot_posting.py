import os
import json
import asyncio
import random
import re
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

# Режим публикации (manual или auto)
PUBLICATION_MODE = "manual"  # По умолчанию ручной режим

# Загружаем новости
def load_news():
    with open("result_news.json", "r", encoding="utf-8") as f:
        return json.load(f)

def validate_news_item(news_item):
    """
    Проверяет пригодность новости для публикации.

    Возвращает словарь:
    {
        'is_valid': bool,
        'reason': str,
        'cleaned_description': str (без тега)
    }
    """
    description = news_item.get("description", "")

    # Проверка 1: Наличие тега пригодности
    if "[NOT_SUITABLE]" in description:
        return {
            'is_valid': False,
            'reason': 'Новость не соответствует тематике канала',
            'cleaned_description': description
        }

    if "[SUITABLE]" not in description:
        return {
            'is_valid': False,
            'reason': 'Отсутствует тег пригодности',
            'cleaned_description': description
        }

    # Удаляем тег из описания
    cleaned_description = re.sub(r'\[SUITABLE\]\s*', '', description, flags=re.IGNORECASE).strip()

    # Проверка 2: Наличие хэштегов в конце
    if not re.search(r'#\w+', cleaned_description):
        return {
            'is_valid': False,
            'reason': 'Отсутствуют хэштеги (возможно, неполная обработка)',
            'cleaned_description': cleaned_description
        }

    # Проверка 3: Минимальная длина текста (не менее 100 символов без хэштегов)
    text_without_hashtags = re.sub(r'#\w+', '', cleaned_description).strip()
    if len(text_without_hashtags) < 100:
        return {
            'is_valid': False,
            'reason': 'Слишком короткий текст (возможно, неполная обработка)',
            'cleaned_description': cleaned_description
        }

    # Проверка 4: Текст заканчивается не на середине предложения
    # Проверяем, что после последнего хэштега нет незавершённого текста
    last_sentence_chars = cleaned_description.rstrip()[-50:]  # Последние 50 символов
    if not any(last_sentence_chars.endswith(char) for char in ['.', '!', '?', '#']):
        # Если не заканчивается на знак препинания или хэштег
        words = last_sentence_chars.split()
        if len(words) > 3:  # Если есть несколько слов без завершения
            return {
                'is_valid': False,
                'reason': 'Текст обрезан посередине предложения',
                'cleaned_description': cleaned_description
            }

    # Проверка 5: Текст на русском языке (более 80% кириллицы)
    cyrillic_chars = len(re.findall(r'[а-яА-ЯёЁ]', text_without_hashtags))
    total_letters = len(re.findall(r'[a-zA-Zа-яА-ЯёЁ]', text_without_hashtags))
    if total_letters > 0 and cyrillic_chars / total_letters < 0.8:
        return {
            'is_valid': False,
            'reason': 'Текст не переведен на русский язык',
            'cleaned_description': cleaned_description
        }

    return {
        'is_valid': True,
        'reason': 'Новость прошла все проверки',
        'cleaned_description': cleaned_description
    }

def truncate_text_for_telegram(text, max_length=MAX_MESSAGE_LENGTH):
    """
    Обрезает текст до допустимой длины для Telegram,
    сохраняя форматирование Markdown и добавляя уведомление об обрезке
    """
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

async def process_news_automatically(application: Application):
    """
    Обрабатывает и публикует новости автоматически.
    Отправляет админу статус каждой новости.
    """
    news = load_news()

    if not ADMIN_CHAT_ID:
        print("⚠️  ADMIN_CHAT_ID не установлен в .env")
        return

    valid_news = []
    rejected_news = []

    # Анализируем все новости
    for idx, news_item in enumerate(news, 1):
        validation = validate_news_item(news_item)

        if validation['is_valid']:
            # Новость прошла валидацию
            news_item['cleaned_description'] = validation['cleaned_description']
            valid_news.append(news_item)
        else:
            # Новость отклонена
            rejected_news.append({
                'news': news_item,
                'reason': validation['reason']
            })

    # Отправляем админу сводку
    summary = f"📊 **Автоматическая обработка новостей**\n\n"
    summary += f"✅ Принято к публикации: {len(valid_news)}\n"
    summary += f"❌ Отклонено: {len(rejected_news)}\n\n"

    if rejected_news:
        summary += "**Отклонённые новости:**\n"
        for item in rejected_news[:5]:  # Показываем первые 5
            title = item['news']['title'][:50]
            summary += f"• {title}... - _{item['reason']}_\n"
        if len(rejected_news) > 5:
            summary += f"... и ещё {len(rejected_news) - 5}\n"

    await application.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=summary,
        parse_mode="Markdown"
    )

    # Публикуем валидные новости с задержкой
    if valid_news:
        # Первую новость публикуем сразу
        first_news = valid_news[0]
        await publish_news_auto(application.bot, first_news, 0)
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ **Опубликовано:** {first_news['title'][:80]}...",
            parse_mode="Markdown"
        )

        # Остальные новости публикуем с задержкой 5-10 минут
        cumulative_delay = 0
        for idx, news_item in enumerate(valid_news[1:], 1):
            # Добавляем случайную задержку от 5 до 10 минут к накопительной
            delay_increment = random.randint(5, 10)
            cumulative_delay += delay_increment

            await application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⏰ **Запланировано:** {news_item['title'][:80]}...\n_Опубликуется через {cumulative_delay} минут_",
                parse_mode="Markdown"
            )

            # Планируем публикацию
            asyncio.create_task(publish_news_auto(application.bot, news_item, cumulative_delay))
    else:
        await application.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="⚠️ Нет новостей, подходящих для автоматической публикации",
            parse_mode="Markdown"
        )

async def send_news_to_admin(application: Application):
    """Автоматически отправляет новости админу при запуске бота"""
    global PUBLICATION_MODE

    # Проверяем режим публикации
    mode = application.bot_data.get("publication_mode", "manual")

    if mode == "auto":
        # Автоматический режим
        await process_news_automatically(application)
    else:
        # Ручной режим
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
    text = f"📰 *{n['title']}*\n\n{n['description']}\n\n🔗 [Ссылка на источник]({n['link']})"

    # Проверяем и обрезаем текст, если он слишком длинный
    text = truncate_text_for_telegram(text)

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
    """Команда /start - показывает главное меню"""
    user_id = update.effective_user.id

    # Проверяем, есть ли пользователь в списке разрешенных
    if user_id not in ALLOWED_USERS:
        print(f"⚠️  Попытка доступа от неразрешенного пользователя: {user_id}")
        return

    # Показываем меню выбора режима
    keyboard = [
        [
            InlineKeyboardButton("📝 Ручной режим", callback_data="mode_manual"),
            InlineKeyboardButton("🤖 Автоматический режим", callback_data="mode_auto")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    current_mode = context.application.bot_data.get("publication_mode", "manual")
    mode_text = "ручной 📝" if current_mode == "manual" else "автоматический 🤖"

    await update.message.reply_text(
        f"🤖 **Бот управления новостями**\n\n"
        f"Текущий режим: **{mode_text}**\n\n"
        f"**Ручной режим** - вы просматриваете каждую новость и решаете, публиковать или пропустить\n\n"
        f"**Автоматический режим** - новости проверяются автоматически и публикуются с задержкой 5-10 минут\n\n"
        f"Выберите режим публикации:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # Проверяем, есть ли пользователь в списке разрешенных
    if user_id not in ALLOWED_USERS:
        await query.answer("❌ У вас нет доступа к этому боту", show_alert=True)
        print(f"⚠️  Попытка взаимодействия от неразрешенного пользователя: {user_id}")
        return

    await query.answer()

    # Обработка выбора режима публикации
    if query.data == "mode_manual":
        context.application.bot_data["publication_mode"] = "manual"
        await query.edit_message_text("✅ Выбран ручной режим публикации.\n\n📥 Загружаю новости...")
        await send_news_to_admin(context.application)
        return

    if query.data == "mode_auto":
        context.application.bot_data["publication_mode"] = "auto"
        await query.edit_message_text("✅ Выбран автоматический режим публикации.\n\n🤖 Начинаю обработку...")
        await send_news_to_admin(context.application)
        return

    # Обработка кнопок в ручном режиме
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
    """Публикует новость в канал (ручной режим)"""
    text = f"📰 *{news_item['title']}*\n\n{news_item['description']}\n\n🔗 [Ссылка на источник]({news_item['link']})"

    # Проверяем и обрезаем текст, если он слишком длинный
    text = truncate_text_for_telegram(text)

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=False
    )

async def publish_news_auto(bot, news_item, delay_minutes):
    """Публикует новость в канал (автоматический режим) с задержкой"""
    if delay_minutes > 0:
        await asyncio.sleep(delay_minutes * 60)

    # Используем очищенное описание
    description = news_item.get('cleaned_description', news_item.get('description', ''))
    text = f"📰 *{news_item['title']}*\n\n{description}\n\n🔗 [Ссылка на источник]({news_item['link']})"

    # Проверяем и обрезаем текст, если он слишком длинный
    text = truncate_text_for_telegram(text)

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )

        # Уведомляем админа об успешной публикации
        if delay_minutes > 0:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ **Опубликовано:** {news_item['title'][:80]}...",
                parse_mode="Markdown"
            )
    except Exception as e:
        # Уведомляем админа об ошибке
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ **Ошибка публикации:** {news_item['title'][:60]}...\n_Причина: {str(e)}_",
            parse_mode="Markdown"
        )

async def schedule_post(context, news_item, delay_minutes):
    """Планирует отложенную публикацию"""
    await asyncio.sleep(delay_minutes * 60)
    await publish_news(context.bot, news_item)

async def post_init(application: Application):
    """Автоматически запускается после инициализации бота"""
    print("✅ Бот инициализирован")
    print("💡 Используйте /start для выбора режима публикации")
    # Не запускаем автоматически - ждем команды от пользователя

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()