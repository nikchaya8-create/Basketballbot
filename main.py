import asyncio
import logging
import os
import random
import json
import httpx
from datetime import datetime, time
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ----------------- НАСТРОЙКИ БОТА И БАЗЫ ДАННЫХ -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

# URL базы данных PostgreSQL (используется на Render). 
# Скрипт автоматически упадет на локальную SQLite, если Postgres не настроен!
DATABASE_URL = os.environ.get("DATABASE_URL", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = None
dp = Dispatcher()
scheduler = AsyncIOScheduler()
db_pool = None # Пул подключений PostgreSQL

# Очередь фоллбека (на случай, если лимиты Gemini временно исчерпаны)
FALLBACK_THEMES = [
    "Бросок с отклонением (Fadeaway Shot)",
    "Кроссовер и смена темпа (Hesitation Dribble)",
    "Защита на дуге против снайперов",
    "Игра без мяча и бэкдор-резы (Backdoor Cuts)"
]

# ----------------- СЛОЙ ИНИЦИАЛИЗАЦИИ БАЗЫ ДАННЫХ -----------------
IS_POSTGRES = bool(DATABASE_URL)

async def init_database():
    """Создает необходимые таблицы в PostgreSQL или локальном SQLite"""
    global db_pool
    if IS_POSTGRES:
        try:
            import asyncpg
            db_pool = await asyncpg.create_pool(DATABASE_URL)
            async with db_pool.acquire() as conn:
                # Таблица тем дня
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS bflow_themes (
                        id SERIAL PRIMARY KEY,
                        theme VARCHAR(255) UNIQUE NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                # Таблица 4 постов на день
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS bflow_posts (
                        id SERIAL PRIMARY KEY,
                        theme_id INT,
                        time_of_day VARCHAR(50) NOT NULL,
                        title VARCHAR(500) NOT NULL,
                        post_text TEXT NOT NULL,
                        image_prompt TEXT,
                        posted BOOLEAN DEFAULT FALSE,
                        scheduled_date DATE DEFAULT CURRENT_DATE
                    );
                ''')
                # Таблица учета комментариев во избежание дублей ответов
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS bflow_replies (
                        comment_id BIGINT PRIMARY KEY,
                        replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
            logging.info("🐘 База данных PostgreSQL успешно инициализирована!")
        except Exception as e:
            logging.error(f"Не удалось подключить PostgreSQL: {e}. Переходим на SQLite.")
            setup_sqlite()
    else:
        setup_sqlite()

def setup_sqlite():
    """Настройка локальной базы данных SQLite для автономных тестов"""
    import sqlite3
    conn = sqlite3.connect("basketflow.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bflow_themes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            theme TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bflow_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            theme_id INTEGER,
            time_of_day TEXT NOT NULL,
            title TEXT NOT NULL,
            post_text TEXT NOT NULL,
            image_prompt TEXT,
            posted INTEGER DEFAULT 0,
            scheduled_date DATE DEFAULT (date('now'))
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bflow_replies (
            comment_id INTEGER PRIMARY KEY,
            replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    conn.close()
    logging.info("💾 Локальная база SQLite успешно инициализирована!")

# ----------------- ПОМОЩНИКИ РАБОТЫ С ДАННЫМИ (БД) -----------------

async def db_execute(query, *args):
    if db_pool: # Postgres
        async with db_pool.acquire() as conn:
            return await conn.execute(query, *args)
    else: # SQLite
        import sqlite3
        conn = sqlite3.connect("basketflow.db")
        cursor = conn.cursor()
        cursor.execute(query.replace("$1", "?").replace("$2", "?").replace("$3", "?").replace("$4", "?").replace("$5", "?").replace("$6", "?"), args)
        conn.commit()
        conn.close()

async def db_fetch(query, *args):
    if db_pool: # Postgres
        async with db_pool.acquire() as conn:
            return await conn.fetch(query, *args)
    else: # SQLite
        import sqlite3
        conn = sqlite3.connect("basketflow.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query.replace("$1", "?").replace("$2", "?").replace("$3", "?").replace("$4", "?").replace("$5", "?").replace("$6", "?"), args)
        rows = cursor.fetchall()
        conn.close()
        return rows

# ----------------- ИНТЕГРАЦИЯ С GEMINI API -----------------

async def query_gemini(system_prompt: str, user_prompt: str, json_mode=False) -> str:
    """Безопасный запрос к API Gemini с обработкой ошибок"""
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        raise ValueError("Отсутствует ключ GEMINI_API_KEY")
        
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    params = {"key": GEMINI_API_KEY}
    
    payload = {
        "contents": [{
            "parts": [{"text": user_prompt}]
        }],
        "generationConfig": {
            "temperature": 0.8,
        }
    }
    
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        
    headers = {"Content-Type": "application/json"}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, params=params, timeout=30.0)
        response.raise_for_status()
        res_data = response.json()
        return res_data['candidates'][0]['content']['parts'][0]['text']

# ----------------- БИЗНЕС-ЛОГИКА: ГЕНЕРАЦИЯ КОНТЕНТА -----------------

async def generate_and_save_daily_content():
    """Шаг 1: Получаем список старых тем. Шаг 2: Генерируем новую тему. Шаг 3: Пишем 4 связанных поста."""
    logging.info("⏳ Начинается генерация суточного контента BasketFlow AI...")
    
    try:
        # Получаем прошлые темы для проверки уникальности
        past_rows = await db_fetch("SELECT theme FROM bflow_themes ORDER BY id DESC LIMIT 50")
        past_themes = [r['theme'] for r in past_rows]
        past_themes_str = ", ".join(past_themes) if past_themes else "Нет предыдущих тем"
        
        # 1. Запрос уникальной темы
        theme_sys = "Ты — главный эксперт баскетбольного медиа. Придумай ОДНУ конкретную и глубокую тему о баскетболе на русском языке. Она должна отличаться от предыдущих."
        theme_usr = f"Предыдущие темы: {past_themes_str}. Верни только короткое название темы, без кавычек и точек."
        
        try:
            theme = await query_gemini(theme_sys, theme_usr)
            theme = theme.strip().replace('"', '')
        except Exception as e:
            logging.error(f"Ошибка получения темы от ИИ: {e}. Используем случайную тему из резерва.")
            theme = random.choice(FALLBACK_THEMES)
            
        logging.info(f"🎯 Выбрана уникальная тема дня: {theme}")
        
        # Сохраняем тему в БД
        await db_execute("INSERT INTO bflow_themes (theme) VALUES ($1) ON CONFLICT DO NOTHING", theme)
        theme_row = await db_fetch("SELECT id FROM bflow_themes WHERE theme = $1", theme)
        theme_id = theme_row[0]['id']
        
        # 2. Генерация 4 постов
        posts_sys = """Ты — копирайтер баскетбольного медиа 'BasketFlow AI'. Напиши 4 связанных поста на русском языке.
Посты должны идти последовательно: Утро (08:00) — теория и обучение, День (12:00) — упражнения/тренировка,
Вечер (18:00) — увлекательная история/мем/цитата, Ночь (21:00) — мини-совет или умный факт.

Верни строго JSON объект следующей структуры:
{
  "posts": [
    {
      "time": "08:00",
      "title": "🌅 Название поста",
      "text": "Текст на 100-150 слов с HTML-тегами (<b>, <i>, <code>). Закрывай теги правильно!",
      "imagePrompt": "Detailed English image prompt for Midjourney describing a basketball scene for this post"
    },
    ... (всего 4 поста)
  ]
}"""
        posts_usr = f"Напиши посты по теме: '{theme}'."
        
        raw_json = await query_gemini(posts_sys, posts_usr, json_mode=True)
        data = json.loads(raw_json)
        
        # Записываем посты в БД
        for i, p in enumerate(data.get("posts", [])):
            time_key = "morning" if i == 0 else "day" if i == 1 else "evening" if i == 2 else "night"
            await db_execute(
                "INSERT INTO bflow_posts (theme_id, time_of_day, title, post_text, image_prompt) VALUES ($1, $2, $3, $4, $5)",
                theme_id, time_key, p['title'], p['text'], p['imagePrompt']
            )
            
        logging.info("✅ Пакет из 4 постов успешно сгенерирован и записан в базу данных!")
        
    except Exception as e:
        logging.error(f"🚨 Критическая ошибка при генерации ежедневного пакета: {e}")

# ----------------- БИЗНЕС-ЛОГИКА: АВТОМАТИЧЕСКИЙ ПОСТИНГ -----------------

async def publish_scheduled_post(time_key: str):
    """Публикует пост из базы данных по ключу времени (morning, day, evening, night)"""
    logging.info(f"⏳ Попытка публикации поста для времени: {time_key}")
    try:
        # Находим сегодняшний непобликованный пост
        rows = await db_fetch(
            "SELECT id, title, post_text, image_prompt FROM bflow_posts WHERE time_of_day = $1 AND posted = FALSE ORDER BY id DESC LIMIT 1",
            time_key
        )
        
        if not rows:
            logging.warning(f"⚠️ Пост для {time_key} не найден в БД. Срочно генерируем на лету...")
            await generate_and_save_daily_content()
            rows = await db_fetch(
                "SELECT id, title, post_text, image_prompt FROM bflow_posts WHERE time_of_day = $1 AND posted = FALSE ORDER BY id DESC LIMIT 1",
                time_key
            )
            
        if rows:
            post = rows[0]
            # Очищаем неподдерживаемые Telegram HTML-теги
            clean_text = post['post_text'].replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n").replace("<p>", "").replace("</p>", "\n").replace("<div>", "").replace("</div>", "\n")
            message_text = f"<b>{post['title']}</b>\n\n{clean_text}"
            
            image_prompt = post['image_prompt']
            if image_prompt:
                try:
                    import urllib.parse
                    encoded_prompt = urllib.parse.quote(image_prompt)
                    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&private=true"
                    logging.info(f"🎨 Отправляем фото через Pollinations по промпту: {image_prompt}")
                    await bot.send_photo(
                        chat_id=CHAT_ID,
                        photo=image_url,
                        caption=message_text[:1024], # Лимит подписи Telegram — 1024 символа
                        parse_mode="HTML"
                    )
                except Exception as img_err:
                    logging.error(f"⚠️ Не удалось отправить фото, отправляем текст: {img_err}")
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=message_text,
                        parse_mode="HTML"
                    )
            else:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=message_text,
                    parse_mode="HTML"
                )
            
            # Помечаем пост как отправленный
            if db_pool:
                await db_execute("UPDATE bflow_posts SET posted = TRUE WHERE id = $1", post['id'])
            else:
                await db_execute("UPDATE bflow_posts SET posted = 1 WHERE id = $1", post['id'])
                
            logging.info(f"🏀 Пост {post['title']} успешно опубликован в Telegram!")
        else:
            logging.error(f"❌ Не удалось сгенерировать пост для публикации {time_key}")
            
    except Exception as e:
        logging.error(f"Ошибка публикации поста {time_key}: {e}")

# ----------------- БИЗНЕС-ЛОГИКА: ОТВЕТЫ НА КОММЕНТАРИИ -----------------

@dp.message()
async def handle_group_comments(message: types.Message):
    """Отслеживает комментарии под постами и отвечает короткими фразами (3-10 слов)"""
    # В Telegram комментарии к постам канала пересылаются в связанный чат группы как ответы (replies)
    if not message.reply_to_message:
        return # Нас интересуют только ответы на сообщения бота
        
    # Проверяем, что ответ адресован именно нашему боту
    if not message.reply_to_message.from_user or message.reply_to_message.from_user.id != bot.id:
        return
        
    comment_id = message.message_id
    
    # Защита от дубликатов (1 ответ на 1 комментарий)
    try:
        duplicate_check = await db_fetch("SELECT comment_id FROM bflow_replies WHERE comment_id = $1", comment_id)
        if duplicate_check:
            return # Уже ответили
    except Exception:
        pass # Игнорируем ошибки БД, перейдем на логику на лету
        
    logging.info(f"💬 Обнаружен комментарий от {message.from_user.full_name}: '{message.text}'")
    
    # Генерация умного ответа через ИИ
    reply_sys = (
        "Ты — опытный баскетбольный коуч, автор канала 'BasketFlow AI'. "
        "Ответь на комментарий подписчика под твоим постом. "
        "Твой ответ должен быть СТРОГО коротким: от 3 до 10 слов на русском языке. "
        "Тон — мотивирующий, экспертный, дружелюбный. Используй максимум один эмодзи 🏀."
    )
    reply_usr = f"Пост: '{message.reply_to_message.text[:150]}'\nКомментарий подписчика: '{message.text}'"
    
    try:
        ai_reply = await query_gemini(reply_sys, reply_usr)
        ai_reply = ai_reply.strip()
    except Exception as e:
        logging.error(f"Ошибка генерации ответа: {e}")
        ai_reply = random.choice([
            "Согласен, отличный вопрос! Тренируйся 💪",
            "Верно подмечено. Главное — дисциплина! 🏀",
            "Хорошее замечание, обрати на это внимание 👍",
            "Да, это частая ошибка. Работаем над этим!"
        ])
        
    # Отправляем ответ
    await message.reply(ai_reply)
    
    # Записываем в базу, чтобы не отвечать дважды
    try:
        await db_execute("INSERT INTO bflow_replies (comment_id) VALUES ($1)", comment_id)
    except Exception:
        pass

# ----------------- ПЛАНИРОВЩИК (CRON РАСПИСАНИЕ) -----------------

def setup_scheduler():
    # 1. Ежедневная генерация контента на день вперед (в 00:05 ночи)
    scheduler.add_job(generate_and_save_daily_content, CronTrigger(hour=0, minute=5))
    
    # 2. Утренний обучающий пост (в 08:00)
    scheduler.add_job(lambda: asyncio.create_task(publish_scheduled_post("morning")), CronTrigger(hour=8, minute=0))
    
    # 3. Дневной тренировочный пост (в 12:00)
    scheduler.add_job(lambda: asyncio.create_task(publish_scheduled_post("day")), CronTrigger(hour=12, minute=0))
    
    # 4. Вечерняя история / мем (в 18:00)
    scheduler.add_job(lambda: asyncio.create_task(publish_scheduled_post("evening")), CronTrigger(hour=18, minute=0))
    
    # 5. Ночной совет / факт (в 21:00)
    scheduler.add_job(lambda: asyncio.create_task(publish_scheduled_post("night")), CronTrigger(hour=21, minute=0))
    
    scheduler.start()
    logging.info("⏰ Планировщик автопостинга BasketFlow AI успешно активирован!")

# ----------------- ВЕБ-СЕРВЕР (ДЛЯ RENDER HEALTH CHECK) -----------------

async def handle_health(request):
    return web.Response(text="BasketFlow AI is live 24/7!", content_type="text/plain")

async def start_render_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Порт 3000 требуется для прохождения проверок Render
    port = int(os.environ.get("PORT", 3000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Сервер-заглушка для Render запущен на порту {port}")

# ----------------- ТОЧКА ВХОДА В ПРОГРАММУ -----------------

async def main():
    global bot
    
    # 1. Сначала запускаем веб-сервер, чтобы деплой на Render не падал
    await start_render_web_server()
    
    # 2. Инициализируем базу данных
    await init_database()
    
    # 3. Проверка учетных данных
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or ":" not in BOT_TOKEN:
        logging.error("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не задан в переменных окружения Render!")
        logging.error("Бот запущен в режиме ожидания настроек. Веб-сервер работает, деплой активен.")
        while True:
            await asyncio.sleep(3600)
            
    try:
        bot = Bot(token=BOT_TOKEN)
        
        # Запускаем авто-планировщик постов
        setup_scheduler()
        
        # Если база данных пуста — генерируем первый пакет постов прямо сейчас!
        initial_check = await db_fetch("SELECT id FROM bflow_posts LIMIT 1")
        if not initial_check:
            logging.info("🆕 База данных пуста. Запускаем первичную ИИ-генерацию постов...")
            await generate_and_save_daily_content()
            
        logging.info("🤖 Начинается фоновый опрос Telegram (Polling)...")
        await dp.start_polling(bot)
        
    except Exception as e:
        logging.error(f"❌ Сбой запуска Telegram Polling: {e}")
        # Спим бесконечно, чтобы процесс не перезапускался циклично
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
