import asyncio
import logging
import os
from datetime import datetime
import json
import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# НАСТРОЙКИ БОТА
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")

# Парсим ID топиков группы (Forum Threads)
env_polls_id = os.environ.get("POLLS_THREAD_ID", "")
POLLS_THREAD_ID = int(env_polls_id) if env_polls_id and env_polls_id.isdigit() else None

env_tips_id = os.environ.get("TIPS_THREAD_ID", "")
TIPS_THREAD_ID = int(env_tips_id) if env_tips_id and env_tips_id.isdigit() else None

# Ключ Gemini API для генерации уникальных советов (Опционально)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = None
dp = Dispatcher()
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Yekaterinburg")
scheduler = AsyncIOScheduler(timezone=BOT_TIMEZONE)

# Встроенная база баскетбольных статей на случай отсутствия ключа AI
BASKETBALL_TIPS_DATABASE = [
    {
        "title": "🏀 СЕКРЕТ ИДЕАЛЬНОГО БРОСКА: МЕХАНИКА И ТОЧКИ КОНТРОЛЯ",
        "text": "Каждый великий снайпер знает: стабильный бросок — это чистая физика и мышечная память. Локоть должен быть строго сонаправлен кольцу под углом 90 градусов. Не заводите мяч далеко за голову. Важнейшая точка контроля — фиксация расслабленной кисти («гусиная шея») после релиза мяча до его касания кольца. Это придает правильное обратное вращение."
    },
    {
        "title": "💪 РАЗВИТИЕ МЫШЦ КОРА (БАЗА ДЛЯ ПРЫЖКА И СТАБИЛЬНОСТИ)",
        "text": "Мышцы кора (пресс, косые мышцы, поясница) — это мост передачи энергии от ног к рукам. Без сильного кора вы будете терять баланс при броске в прыжке и контакте в воздухе. Добавьте в тренировки статическую планку (3 подхода по 1.5 мин), боковые планки и динамические скручивания «книжка». Это защитит поясницу от травм."
    },
    {
        "title": "⚡️ ДРИБЛИНГ СО СМЕНОЙ ТЕМПА (HESITATION MOVE)",
        "text": "Самый эффективный кроссовер — это не самый быстрый, а тот, который меняет темп. Научитесь усыплять бдительность защитника: ведите мяч высоко и медленно, выпрямляя корпус (имитируя подготовку к броску или передаче), а затем резко взрывайтесь вниз с низким ведением. Это заставит защитника подняться на носки."
    },
    {
        "title": "🛡️ ЗАЩИТА ОДИН НА ОДИН: РАБОТА НОГ И КОРПУСА",
        "text": "При защите на периметре никогда не скрещивайте ноги — двигайтесь приставными шагами в низкой стойке. Смотрите сопернику в область пояса (ее невозможно сымитировать финтом в отличие от головы или мяча). Держите одну руку на уровне его глаз для помехи броску, а вторую опустите ниже для перехвата передач."
    }
]

tip_counter = 0

async def generate_or_get_tip():
    global tip_counter
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        tip = BASKETBALL_TIPS_DATABASE[tip_counter % len(BASKETBALL_TIPS_DATABASE)]
        tip_counter += 1
        return f"<b>{tip['title']}</b>\n\n{tip['text']}\n\n<i>Подумайте об этом! Стабильность создается в деталях.</i>"
    
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        headers = {"Content-Type": "application/json"}
        params = {"key": GEMINI_API_KEY}
        payload = {
            "contents": [{
                "parts": [{
                    "text": "Напиши профессиональную, полезную, мотивирующую статью о баскетболе (тактика, ОФП, дриблинг, бросок, защита) в формате HTML для Telegram. Используй теги <b>, <i>, <code>. Статья должна начинаться с яркого заголовка с эмодзи. Не используй теги <p> или <br>. Используй обычные переносы строк."
                }]
            }]
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, params=params, timeout=15.0)
            res_data = response.json()
            raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
            return raw_text
    except Exception as e:
        logging.error(f"Ошибка вызова Gemini: {e}")
        tip = BASKETBALL_TIPS_DATABASE[0]
        return f"<b>{tip['title']}</b>\n\n{tip['text']}"

# ФУНКЦИИ ОТПРАВКИ ОПРОСОВ И СТАТЕЙ

async def send_custom_poll(question, options, emoji):
    try:
        poll_options = list(options) + [emoji]
        await bot.send_poll(
            chat_id=CHAT_ID,
            question=question,
            options=poll_options,
            is_anonymous=False, # Видим, кто придет на тренировку!
            message_thread_id=POLLS_THREAD_ID
        )
        logging.info(f"Опрос отправлен: {question}")
    except Exception as e:
        logging.error(f"Ошибка отправки опроса: {e}")

async def send_basketball_tip():
    try:
        text = await generate_or_get_tip()
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            message_thread_id=TIPS_THREAD_ID
        )
        logging.info("Полезный совет отправлен!")
    except Exception as e:
        logging.error(f"Ошибка отправки совета: {e}")

# ОПРЕДЕЛЕНИЕ РАСПИСАНИЯ ОТПРАВКИ

# 1. После Понедельника (во Вторник в 09:00) отправляем опрос на Четверг (21:00)
async def post_poll_for_thursday():
    await send_custom_poll(
        question="Академическая\nЧт 21:00-22:30",
        options=["Приду", "Не приду", "Не знаю"],
        emoji="🔥"
    )

# 2. После Четверга (в Пятницу в 09:00) отправляем два опроса на Субботу (9:00 утро и 18:00 вечер)
async def post_polls_for_saturday():
    await send_custom_poll(
        question="Академическая\nСб 9:00-11:00",
        options=["Приду", "Не приду", "Не знаю"],
        emoji="☀️"
    )
    await asyncio.sleep(2)
    await send_custom_poll(
        question="Академическая\nСб 18:00-20:00",
        options=["Приду", "Не приду", "Не знаю"],
        emoji="😎"
    )

# 3. После Субботы (в Воскресенье в 09:00) отправляем опрос на Понедельник (21:00)
async def post_poll_for_monday():
    await send_custom_poll(
        question="Академическая\nПн 21:00-22:30",
        options=["Приду", "Не приду", "Не знаю"],
        emoji="🤯"
    )

# РЕГИСТРАЦИЯ КОМАНД

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Я специализированный баскетбольный бот-организатор для твоей группы.\n\n"
        "<b>Моё автоматическое расписание (Екатеринбург):</b>\n"
        "• <b>Вторник 09:00</b> — Опрос на Четверг (21:00)\n"
        "• <b>Пятница 09:00</b> — Опросы на Субботу Утро (9:00) и Вечер (18:00)\n"
        "• <b>Воскресенье 09:00</b> — Опрос на Понедельник (21:00)\n"
        "• <b>Раз в 2 дня в 12:00</b> — Публикация полезных статей в «Подумай об этом»!\n\n"
        "<b>Команды быстрого теста:</b>\n"
        "/poll_thursday — Отправить опрос на Четверг сейчас\n"
        "/poll_saturday — Отправить опросы на Субботу сейчас\n"
        "/poll_monday — Отправить опрос на Понедельник сейчас\n"
        "/send_tip — Сгенерировать и отправить статью сейчас",
        parse_mode="HTML"
    )

@dp.message(Command("poll_thursday"))
async def force_thursday(message: types.Message):
    await message.answer("🔄 Отправляю опрос на Четверг...")
    await post_poll_for_thursday()

@dp.message(Command("poll_saturday"))
async def force_saturday(message: types.Message):
    await message.answer("🔄 Отправляю опросы на Субботу...")
    await post_polls_for_saturday()

@dp.message(Command("poll_monday"))
async def force_monday(message: types.Message):
    await message.answer("🔄 Отправляю опрос на Понедельник...")
    await post_poll_for_monday()

@dp.message(Command("send_tip"))
async def force_tip(message: types.Message):
    await message.answer("🔄 Генерирую и отправляю баскетбольный совет...")
    await send_basketball_tip()

# ЗАПУСК ПЛАНИРОВЩИКА
def setup_scheduler():
    # 1. Опрос на Четверг: запускается каждый ВТОРНИК в 09:00 (Екатеринбург)
    scheduler.add_job(post_poll_for_thursday, CronTrigger(day_of_week="tue", hour=9, minute=0))
    
    # 2. Опросы на Субботу: запускаются каждую ПЯТНИЦУ в 09:00 (Екатеринбург)
    scheduler.add_job(post_polls_for_saturday, CronTrigger(day_of_week="fri", hour=9, minute=0))
    
    # 3. Опрос на Понедельник: запускается каждое ВОСКРЕСЕНЬЕ в 09:00 (Екатеринбург)
    scheduler.add_job(post_poll_for_monday, CronTrigger(day_of_week="sun", hour=9, minute=0))
    
    # 4. Баскетбольные статьи: отправляются ЧЕРЕЗ ДЕНЬ (каждые 2 дня) в 12:00 (Екатеринбург)
    scheduler.add_job(send_basketball_tip, CronTrigger(day="*/2", hour=12, minute=0))
    
    scheduler.start()

# Простой веб-сервер для прохождения проверки портов на Render
async def handle_ping(request):
    return web.Response(text="Basketball Coach Bot is running live 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 3000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер заглушки запущен на порту {port}")

async def main():
    global bot
    
    try:
        await start_web_server()
    except Exception as e:
        logging.error(f"Не удалось запустить веб-сервер: {e}")

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or ":" not in BOT_TOKEN:
        logging.error("❌❌❌ ОШИБКА НАСТРОЙКИ BOT_TOKEN! ❌❌❌")
        logging.error("Бот находится в режиме ожидания настроек. Веб-сервер запущен, деплой на Render успешный!")
        while True:
            await asyncio.sleep(3600)

    try:
        bot = Bot(token=BOT_TOKEN)
        setup_scheduler()
        logging.info("🏀 Баскетбольный планировщик Екатеринбурга успешно запущен!")
        logging.info("🤖 Начинаем опрос Telegram (Polling)...")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске бота: {e}")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
