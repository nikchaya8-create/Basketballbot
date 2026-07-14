import asyncio
import logging
import os
import json
import httpx
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# НАСТРОЙКИ (Берутся из переменных окружения Render)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "YOUR_CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))  # Ваш личный ID для админки
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
TIMEZONE = "Asia/Yekaterinburg"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Состояния для Админки
class AdminPost(StatesGroup):
    waiting_for_content = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Простая in-memory БД (Для Render рекомендуется использовать PostgreSQL)
db = {
    "daily_theme": "Бросок",
    "replied_comments": set(),
    "today_posts": []
}

# ==================== ИИ ГЕНЕРАЦИЯ (GEMINI) ====================

async def ask_gemini(prompt: str, json_mode=False):
    # Исправлено: Используем корректное имя модели gemini-1.5-flash или gemini-2.0-flash
    model_name = "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
        
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=30.0)
        # Безопасная обработка ответа
        response.raise_for_status()
        res_data = response.json()
        
        if "candidates" not in res_data or not res_data["candidates"]:
            raise ValueError(f"API Error or invalid key. Full response: {res_data}")
            
        return res_data['candidates'][0]['content']['parts'][0]['text']

async def generate_daily_content():
    """Генерирует 5 связанных постов на день"""
    logging.info("Генерация ежедневного контента...")
    
    # 1. Придумываем тему дня
    theme_prompt = "Ты баскетбольный тренер. Придумай ОДНУ узкую тему на сегодня (например: Спэйсинг, Кроссовер, Защита зоны). Верни только 1-3 слова."
    try:
        theme = await ask_gemini(theme_prompt)
        db["daily_theme"] = theme.strip()
        logging.info(f"Тема дня: {db['daily_theme']}")
    except Exception as e:
        logging.error(f"Ошибка генерации темы: {e}")
        return
    
    # 2. Генерируем 5 постов (JSON)
    posts_prompt = f"""Напиши 5 связанных постов для баскетбольного Telegram-канала на тему: '{db['daily_theme']}'.
Формат вывода строго JSON:
{{
  "posts": [
    {{"time_idx": 0, "type": "Техника", "text": "Текст утреннего поста с тегами <b> и <i> (до 100 слов)"}},
    {{"time_idx": 1, "type": "Упражнение", "text": "Текст дневного упражнения"}},
    {{"time_idx": 2, "type": "Юмор/Мем", "text": "Шутка или жизненная ситуация про баскетбол"}},
    {{"time_idx": 3, "type": "Тактика", "text": "Вечерний разбор тактики"}},
    {{"time_idx": 4, "type": "Цитата", "text": "Мотивирующая цитата на ночь"}}
  ]
}}"""
    
    try:
        raw_json = await ask_gemini(posts_prompt, json_mode=True)
        data = json.loads(raw_json)
        db["today_posts"] = data.get("posts", [])
        logging.info("✅ 5 постов успешно сгенерированы!")
    except Exception as e:
        logging.error(f"Ошибка генерации постов: {e}")

# ==================== АВТО-ПОСТИНГ ====================

async def send_scheduled_post(time_idx: int):
    """Функция отправки конкретного поста (0-4)"""
    posts = db.get("today_posts", [])
    if time_idx < len(posts):
        post = posts[time_idx]
        text = f"🏀 <b>Тема дня: {db['daily_theme']} | {post['type']}</b>\n\n{post['text']}"
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
            logging.info(f"Отправлен пост #{time_idx}")
        except Exception as e:
            logging.error(f"Ошибка отправки поста: {e}")

# ==================== АДМИН ПАНЕЛЬ (Личка с ботом) ====================
# Исправлено: Команды админки вынесены ВЫШЕ общего обработчика комментариев,
# чтобы общий @dp.message() не перехватывал и не глушил их.

@dp.message(Command("start"), F.chat.type == "private")
async def admin_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("Извините, у вас нет доступа к админ-панели.")
    
    await message.answer(
        "👋 <b>Добро пожаловать в Админ Панель!</b>\n\n"
        "Вы можете отправить мне текст, фото или видео, и я перешлю это в канал от своего имени.\n"
        "Отправьте /post для создания ручного поста.", parse_mode="HTML"
    )

@dp.message(Command("post"), F.chat.id == ADMIN_ID)
async def admin_create_post(message: Message, state: FSMContext):
    await message.answer("Пришлите текст или фото+текст для канала:")
    await state.set_state(AdminPost.waiting_for_content)

@dp.message(AdminPost.waiting_for_content, F.chat.id == ADMIN_ID)
async def admin_publish_post(message: Message, state: FSMContext):
    try:
        # Копируем сообщение админа напрямую в канал
        await message.copy_to(chat_id=CHANNEL_ID)
        await message.answer("✅ Успешно опубликовано в канал!")
    except Exception as e:
        await message.answer(f"❌ Ошибка публикации: {e}")
    finally:
        await state.clear()

# ==================== ОТВЕТЫ НА КОММЕНТАРИИ ====================
# Исправлено: Общий обработчик сообщений теперь идет последним.
# Он отвечает только на комментарии в обсуждениях канала.

@dp.message()
async def auto_reply_comments(message: Message, state: FSMContext):
    """Автоматический ответ на комментарии подписчиков"""
    # Игнорируем личные сообщения (чтобы админка работала корректно)
    if message.chat.type == "private":
        return
        
    # Проверяем, что сообщение - это ответ на пост канала в группе-обсуждении
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return
    if message.reply_to_message.from_user.id != bot.id:
        return
        
    # Анти-дубль
    if message.message_id in db["replied_comments"]:
        return
        
    logging.info(f"Новый комментарий от {message.from_user.full_name}")
    
    # ИИ Генерация короткого ответа
    sys_prompt = "Ты автор баскетбольного канала. Ответь подписчику на комментарий коротко, экспертно и дружелюбно (3-10 слов, 1 эмодзи)."
    user_prompt = f"Контекст поста: {message.reply_to_message.text[:100]}...\nКомментарий: {message.text}"
    
    try:
        reply_text = await ask_gemini(sys_prompt + "\n\n" + user_prompt)
        await message.reply(reply_text.strip())
        db["replied_comments"].add(message.message_id)
    except Exception as e:
        logging.error(f"Ошибка ответа: {e}")

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================

async def handle_ping(request):
    """Легкий веб-сервер, чтобы Render не убивал процесс (Нужен внешний пинг каждые 10 мин)"""
    return web.Response(text="Basketball Auto-Channel AI is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 3000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")

# ==================== ЗАПУСК ====================

async def main():
    # Настраиваем расписание на 5 постов в день
    scheduler.add_job(generate_daily_content, CronTrigger(hour=4, minute=0)) # Генерация контента ночью
    
    # Публикация в заданное время
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=9, minute=0), args=[0])  # Техника
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=12, minute=0), args=[1]) # Упражнения
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=15, minute=0), args=[2]) # Мем
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=18, minute=0), args=[3]) # Тактика
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=21, minute=0), args=[4]) # Цитата
    scheduler.start()
    
    # Запуск заглушки веб-сервера
    await start_web_server()
    
    logging.info("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
