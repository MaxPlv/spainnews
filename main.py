import asyncio
import subprocess
import sys
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut
from telegram import Update, BotCommand
import os
import logging

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Отключаем подробные логи от httpx (HTTP запросы к Telegram)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Загрузка переменных окружения
load_dotenv()

# Импортируем функции из бота
sys.path.append(str(Path(__file__).parent / "bot"))
from bot.bot_posting import send_news_to_admin, button_handler, start, schedule_auto_posting, load_settings, publish_digest_job, DIGEST_HOURS, LOCAL_TZ

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")  # Опционально для прокси

# Глобальная переменная для приложения
bot_app = None


async def error_handler(update, context):
    """Обработчик ошибок от бота"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Подавляем сетевые ошибки (они обрабатываются автоматически)
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Network error occurred: {context.error}. Will retry automatically.")
        return
    
    # Для других ошибок логируем подробную информацию
    logger.exception("Unexpected error:", exc_info=context.error)


async def run_news_pipeline():
    """Запускает полный цикл обработки новостей"""
    global bot_app

    print("\n" + "="*60)
    print(f"🚀 Запуск цикла обработки новостей")
    print("="*60 + "\n")

    try:
        # Шаг 1: Собираем новости
        print("📥 Шаг 1/3: Сбор новостей из RSS...")
        result = await asyncio.to_thread(subprocess.run, 
            [sys.executable, "bot/fetch_news.py"],
            capture_output=True,
            text=True,
            check=True
        )
        print(result.stdout)
        print("✅ Новости собраны\n")

        # Шаг 2: Обрабатываем через AI
        print("🤖 Шаг 2/3: Обработка новостей через Gemini AI...")
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "bot/process_ai.py"],
            capture_output=True,
            text=True,
            check=True
        )
        print(result.stdout)
        print("✅ Новости обработаны\n")

        # Шаг 3: Отправляем новости админу через работающего бота
        print("📬 Шаг 3/3: Отправка новостей в Telegram...")
        if bot_app:
            settings = load_settings()
            mode = settings.get("mode", "manual")
            
            if mode == "auto":
                print("🤖 Режим: АВТОМАТИЧЕСКИЙ")
                await schedule_auto_posting(bot_app)
            else:
                print("👤 Режим: РУЧНОЙ")
                await send_news_to_admin(bot_app)
                
            print("✅ Новости отправлены/запланированы\n")
        else:
            print("⚠️ Бот не запущен\n")

        print("="*60)
        print("✨ Цикл обработки новостей завершён успешно!")
        print("="*60 + "\n")

    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка при выполнении скрипта: {e}")
        print(f"Вывод: {e.stdout}")
        print(f"Ошибки: {e.stderr}")
    except Exception as e:
        print(f"❌ Непредвиденная ошибка: {e}")



async def post_init(application: Application):
    """Инициализация после запуска бота"""
    global bot_app
    bot_app = application

    print("✅ Бот инициализирован и готов к работе")
    
    # Устанавливаем меню команд бота
    commands = [
        BotCommand("start", "Запустить бота и посмотреть новости"),
    ]
    await application.bot.set_my_commands(commands)
    print("✅ Меню команд установлено")

    # Создаём и запускаем планировщик (явный часовой пояс, не полагаемся на tzlocal)
    scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)

    # Сбор + обработка новостей: каждые 2 часа (копит буфер дайджеста)
    scheduler.add_job(
        run_news_pipeline,
        trigger=CronTrigger(hour="*/2"),
        id="news_pipeline",
        name="Обработка новостей",
        replace_existing=True
    )

    # Публикация дайджеста: фиксированные слоты (по умолчанию 09/15/21)
    digest_hours = ",".join(h.strip() for h in DIGEST_HOURS.split(",") if h.strip())
    scheduler.add_job(
        publish_digest_job,
        trigger=CronTrigger(hour=digest_hours, minute=0),
        args=[application.bot],
        id="digest_publish",
        name="Публикация дайджеста",
        replace_existing=True
    )

    scheduler.start()
    print("✅ Планировщик запущен")
    print("⏰ Следующая обработка новостей:", scheduler.get_job("news_pipeline").next_run_time)
    print(f"🗞 Слоты дайджеста (часы): {digest_hours}")
    print("⏰ Следующий дайджест:", scheduler.get_job("digest_publish").next_run_time)
    print()

    # Сохраняем планировщик в bot_data
    application.bot_data["scheduler"] = scheduler

    # Запускаем первый раз сразу (опционально)
    # await run_news_pipeline()

async def post_shutdown(application: Application):
    """Завершение работы"""
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown()
        print("🛑 Планировщик остановлен")

def main():
    """Основная функция"""
    import sys
    # Отключаем буферизацию для immediate output
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    # Отметка версии сборки — сразу видно, какой коммит реально запущен
    commit = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "unknown"
    print(f"🏷  Версия сборки (commit): {commit[:7]}", flush=True)
    print("🌟 Запуск системы автоматической обработки новостей", flush=True)
    print("📅 Расписание: каждые 2 часа", flush=True)
    print("🤖 Бот будет работать постоянно и обрабатывать кнопки\n", flush=True)

    # Проверяем соединение с интернетом
    print("🔍 Проверка подключения к Telegram API...", flush=True)

    try:
        # Создаём приложение с обработчиками
        builder = Application.builder().token(BOT_TOKEN)

        # Если указан прокси, используем его
        if PROXY_URL:
            print(f"🔐 Используется прокси: {PROXY_URL}", flush=True)
            request = HTTPXRequest(
                proxy=PROXY_URL,
                connect_timeout=30.0,
                read_timeout=30.0,
                pool_timeout=30.0,
                connection_pool_size=8
            )
            builder = builder.request(request)
        else:
            # Увеличиваем таймауты и настраиваем пул соединений
            request = HTTPXRequest(
                connect_timeout=30.0,
                read_timeout=30.0,
                pool_timeout=30.0,
                connection_pool_size=8
            )
            builder = builder.request(request)

        app = builder.post_init(post_init).post_shutdown(post_shutdown).build()

        # Добавляем обработчики
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(button_handler))
        
        # Добавляем обработчик ошибок
        app.add_error_handler(error_handler)

        print("🚀 Запуск бота...\\n", flush=True)
        print("✅ Бот запущен и работает!", flush=True)
        print("📬 Новости будут приходить автоматически каждые 2 часа", flush=True)
        print("💡 Для немедленного тестирования раскомментируйте строку 103 в main.py", flush=True)
        print("🛑 Нажмите Ctrl+C для остановки\\n", flush=True)

        # Запускаем бота (retry-логика настроена через HTTPXRequest выше)
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )

    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.exception("Критическая ошибка запуска бота")
        print(f"\n❌ Ошибка запуска бота: {e}")
        print("\n💡 Возможные причины:")
        print("   1. Нет подключения к интернету")
        print("   2. Telegram API заблокирован в вашей сети")
        print("   3. Неверный токен бота")
        print("\n🔧 Решения:")
        print("   1. Проверьте интернет-соединение")
        print("   2. Попробуйте использовать VPN/прокси")
        print("   3. Добавьте в .env строку: PROXY_URL=socks5://user:pass@host:port")
        print("   4. Проверьте правильность TELEGRAM_TOKEN в .env")


if __name__ == "__main__":
    main()
