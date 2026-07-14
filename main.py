import asyncio
import logging
import os
from datetime import datetime
import json
import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# НАСТРОЙКИ БОТА
# Сначала пробуем взять из переменных окружения Render, затем из настроек
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "YOUR_CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) if os.environ.get("ADMIN_ID") else 0
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Yekaterinburg")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = None
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=BOT_TIMEZONE)

# Простая in-memory БД (Для сохранения постов на сегодня)
db = {
    "daily_theme": "Бросок в прыжке (Jump Shot)",
    "replied_comments": set(),
    "today_posts": [
        {"time_idx": 0, "type": "Техника", "text": "Идеальный джамп-шот начинается с ног. Баланс, согнутые колени и передача энергии снизу вверх — вот секрет стабильности. Запомните правило 'B.E.E.F': Balance (Баланс), Eyes (Глаза), Elbow (Локоть под 90°), Follow-through (Проводка)."},
        {"time_idx": 1, "type": "Упражнение", "text": "Встаньте прямо под кольцо. Уберите небросковую руку за спину. Совершите 10 чистых попаданий подряд бросковой рукой для фиксации 'гусиной шеи'."},
        {"time_idx": 2, "type": "Юмор/Мем", "text": "Когда ты идеально выстроил механику, выпрыгнул как Леброн, выпустил мяч с идеальным вращением... и попал в ребро щита. 🧱 Не расстраиваемся!"},
        {"time_idx": 3, "type": "Тактика", "text": "Если защитник опустил руки или дал вам больше 1 метра пространства — это сигнал к броску. Читайте расстояние!"},
        {"time_idx": 4, "type": "Цитата", "text": "«Я промазал более 9000 бросков... И именно поэтому я преуспел.» — Майкл Джордан."}
    ]
}

# ==================== ИИ ГЕНЕРАЦИЯ (GEMINI) ====================

async def ask_gemini(prompt: str, json_mode=False):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
        
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=30.0)
        res_data = response.json()
        return res_data['candidates'][0]['content']['parts'][0]['text']

async def generate_daily_content():
    """Генерирует 5 связанных постов на день"""
    logging.info("Генерация ежедневного баскетбольного контента...")
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        logging.warning("Gemini API Key не задан. Используем стандартную базу.")
        return
        
    try:
        # 1. Придумываем тему дня
        theme_prompt = "Ты баскетбольный тренер. Придумай ОДНУ узкую баскетбольную тему на сегодня (например: Спэйсинг, Кроссовер, Защита зоны, Пик-н-ролл). Верни только название темы (1-3 слова)."
        theme = await ask_gemini(theme_prompt)
        db["daily_theme"] = theme.strip()
        logging.info(f"Сгенерирована тема дня: {db['daily_theme']}")
        
        # 2. Генерируем 5 связанных постов
        posts_prompt = f"""Ты профессиональный баскетбольный тренер и автор популярного канала. Напиши 5 связанных постов для Telegram-канала на тему: '{db['daily_theme']}'.
Формат вывода СТРОГО JSON:
{{
  \"posts\": [
    {{\"time_idx\": 0, \"type\": \"Техника\", \"text\": \"Текст утреннего поста об основах и технике по теме (до 100 слов). Используй теги <b> и <i> для выделения важных слов.\"}},
    {{\"time_idx\": 1, \"type\": \"Упражнение\", \"text\": \"Текст дневного практического упражнения для тренировки на площадке или дома.\"}},
    {{\"time_idx\": 2, \"type\": \"Юмор/Мем\", \"text\": \"Шутка, забавная ситуация, ирония или жизненный мем про баскетбол в контексте этой темы.\"}},
    {{\"time_idx\": 3, \"type\": \"Тактика\", \"text\": \"Вечерний разбор тактики, применения в реальной игре 5х5 или 3х3.\"}},
    {{\"time_idx\": 4, \"type\": \"Цитата\", \"text\": \"Мотивирующая баскетбольная мысль или цитата известного игрока/тренера, связанная с темой на ночь.\"}}
  ]
}}"""
        raw_json = await ask_gemini(posts_prompt, json_mode=True)
        data = json.loads(raw_json)
        if "posts" in data and len(data["posts"]) == 5:
            db["today_posts"] = data["posts"]
            logging.info("✅ 5 связанных постов успешно сгенерированы через Gemini!")
        else:
            logging.error("Получен некорректный формат JSON")
    except Exception as e:
        logging.error(f"Ошибка при генерации контента через Gemini: {e}")

async def send_scheduled_post(time_idx: int):
    """Отправка поста в канал (0-4)"""
    posts = db.get("today_posts", [])
    if not posts:
        logging.warning("Очередь постов пуста. Попытка генерации на лету...")
        await generate_daily_content()
        posts = db.get("today_posts", [])
        
    if time_idx < len(posts):
        post = posts[time_idx]
        text = f"🏀 <b>Тема дня: {db['daily_theme']} | {post['type']}</b>\n\n{post['text']}"
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
            logging.info(f"Пост #{time_idx} успешно опубликован в канал {CHANNEL_ID}")
        except Exception as e:
            logging.error(f"Не удалось отправить post #{time_idx}: {e}")

# ==================== АДМИН ПАНЕЛЬ И КЛАВИАТУРА ====================

def get_admin_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🤖 Тема дня")
    builder.button(text="📋 Показать посты")
    builder.button(text="📢 Опубликовать сейчас")
    builder.button(text="✍️ Новый пост")
    builder.button(text="⚙️ Статус")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

class AdminPost(StatesGroup):
    waiting_for_content = State()

# Сначала обрабатываем все команды и кнопки админки, чтобы их не перехватывал auto_reply_comments!

@dp.message(Command("start"), F.chat.type == "private")
async def admin_start(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.answer(
            f"👋 <b>Привет! Я баскетбольный бот-автопостер!</b>\n\n"
            f"Я умею вести канал 24/7, выпуская 5 постов в день на баскетбольные темы с помощью Gemini 3.5-Flash, и автоматически отвечать подписчикам в комментариях.\n\n"
            f"⚠️ <b>Внимание:</b> Чтобы войти в админку, укажите ваш Telegram ID в переменных окружения Render или в сервисе службы:\n"
            f"🔑 <code>ADMIN_ID</code> = <code>{message.from_user.id}</code>\n\n"
            f"После этого перезапустите бота и отправьте /start еще раз!",
            parse_mode="HTML"
        )
        return
        
    await message.answer(
        "👋 <b>Добро пожаловать в панель управления!</b>\n\n"
        "Используйте кнопки меню внизу для управления ботом вручную:\n"
        "• <b>🤖 Тема дня</b> — сгенерировать новую тему и 5 постов\n"
        "• <b>📋 Показать посты</b> — посмотреть текущие посты в очереди\n"
        "• <b>📢 Опубликовать сейчас</b> — отправить очередной пост немедленно\n"
        "• <b>✍️ Новый пост</b> — опубликовать произвольное сообщение вручную\n"
        "• <b>⚙️ Статус</b> — проверить статус планировщика и настроек",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard()
    )

@dp.message(F.text == "🤖 Тема дня", F.chat.type == "private")
async def admin_gen_theme(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔄 Запуск генерации темы и 5 новых постов через Gemini 3.5-Flash...")
    await generate_daily_content()
    theme = db.get("daily_theme")
    posts = db.get("today_posts", [])
    await message.answer(f"✅ Готово!\n<b>Тема дня:</b> {theme}\n<b>Постов в очереди:</b> {len(posts)}", parse_mode="HTML")

@dp.message(F.text == "📋 Показать посты", F.chat.type == "private")
async def admin_show_posts(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    posts = db.get("today_posts", [])
    theme = db.get("daily_theme")
    if not posts:
        await message.answer("⚠️ Очередь постов пуста. Сгенерируйте новые с помощью кнопки '🤖 Тема дня'.")
        return
    
    text = f"📋 <b>Текущие посты (Тема: {theme}):</b>\n\n"
    for idx, p in enumerate(posts):
        text += f"📍 <b>Пост #{idx} [{p['type']}]:</b>\n{p['text'][:120]}...\n\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📢 Опубликовать сейчас", F.chat.type == "private")
async def admin_publish_now(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    posts = db.get("today_posts", [])
    if not posts:
        await message.answer("⚠️ Очередь пуста. Генерирую тему на лету...")
        await generate_daily_content()
        posts = db.get("today_posts", [])
        
    if posts:
        post = posts[0]
        text = f"🏀 <b>Тема дня: {db['daily_theme']} | {post['type']}</b>\n\n{post['text']}"
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
            await message.answer("✅ Пост успешно опубликован в канал!")
        except Exception as e:
            await message.answer(f"❌ Ошибка отправки: {e}")
    else:
        await message.answer("❌ Не удалось получить посты.")

@dp.message(Command("post"), F.chat.type == "private")
@dp.message(F.text == "✍️ Новый пост", F.chat.type == "private")
async def admin_new_post_init(message: types.Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    await message.answer("📝 Отправьте любое сообщение (текст, фото, видео) и я перешлю его в канал от своего имени:")
    await state.set_state(AdminPost.waiting_for_content)

@dp.message(AdminPost.waiting_for_content, F.chat.type == "private")
async def admin_new_post_process(message: types.Message, state: FSMContext):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        await message.copy_to(chat_id=CHANNEL_ID)
        await message.answer("✅ Сообщение успешно переслано в канал!")
    except Exception as e:
        await message.answer(f"❌ Ошибка пересылки: {e}")
    finally:
        await state.clear()

@dp.message(Command("status"), F.chat.type == "private")
@dp.message(F.text == "⚙️ Статус", F.chat.type == "private")
async def admin_status_show(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    
    jobs = scheduler.get_jobs()
    jobs_info = ""
    for j in jobs:
        jobs_info += f"• <code>{j.name}</code> (след. запуск: {j.next_run_time})\n"
    if not jobs_info:
        jobs_info = "<i>Нет активных задач в планировщике</i>"

    gemini_masked = "Не задан"
    if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_API_KEY":
        gemini_masked = f"{GEMINI_API_KEY[:5]}...{GEMINI_API_KEY[-4:]}"

    status_text = (
        "⚙️ <b>Баскетбольный Бот — Панель Статуса</b>\n\n"
        f"👤 <b>Ваш Telegram ID:</b> <code>{message.from_user.id}</code>\n"
        f"🤖 <b>Бот-Администратор (ADMIN_ID):</b> <code>{ADMIN_ID}</code>\n"
        f"📢 <b>Канал (CHANNEL_ID):</b> <code>{CHANNEL_ID}</code>\n"
        f"🧠 <b>Gemini API Ключ:</b> <code>{gemini_masked}</code>\n\n"
        f"<b>📋 Запланированные задачи:</b>\n{jobs_info}\n"
    )
    await message.answer(status_text, parse_mode="HTML", reply_markup=get_admin_keyboard())

# ==================== ОТВЕТЫ НА КОММЕНТАРИИ В ГРУППЕ ====================

@dp.message(F.chat.type.in_({"group", "supergroup"}), ~F.text.startswith("/"))
async def auto_reply_comments(message: types.Message, state: FSMContext):
    """Автоматический ответ на комментарии подписчиков в обсуждении группы"""
    if not message.reply_to_message:
        return
        
    is_reply_to_bot = (message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot.id)
    is_forwarded_from_channel = message.reply_to_message.is_automatic_forward
    
    if not (is_reply_to_bot or is_forwarded_from_channel):
        return
        
    if message.message_id in db["replied_comments"]:
        return
        
    logging.info(f"Получен комментарий от {message.from_user.full_name if message.from_user else 'Анонима'}: {message.text}")
    
    sys_prompt = "Ты — профессиональный баскетбольный тренер, автор этого канала. Ответь подписчику на его комментарий дружелюбно, экспертно и емко (3-15 слов, 1 баскетбольный эмодзи)."
    user_prompt = f"Контекст нашего поста: {message.reply_to_message.text or '[Медиафайл]'}\nКомментарий подписчика: {message.text}"
    
    try:
        reply_text = await ask_gemini(sys_prompt + "\n\n" + user_prompt)
        await message.reply(reply_text.strip(), parse_mode="HTML")
        db["replied_comments"].add(message.message_id)
    except Exception as e:
        logging.error(f"Ошибка автоответа на комментарий: {e}")

@dp.message(F.chat.type == "private")
async def private_fallback(message: types.Message):
    """Заглушка для личных сообщений не-администратора"""
    await message.answer(
        f"👋 Привет! Я баскетбольный бот-автопостер.\n"
        f"Я автономно веду канал и отвечаю подписчикам в комментариях.\n\n"
        f"👤 Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        f"<i>Чтобы войти в панель управления, пропишите ваш ID в переменную ADMIN_ID.</i>",
        parse_mode="HTML"
    )

# ==================== ЗАПУСК БОТА ====================

def setup_scheduler():
    # 1. Генерация постов раз в день ночью в 04:00
    scheduler.add_job(generate_daily_content, CronTrigger(hour=4, minute=0))
    
    # 2. Публикация 5 постов в день по расписанию
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=9, minute=0), args=[0])
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=12, minute=0), args=[1])
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=15, minute=0), args=[2])
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=18, minute=0), args=[3])
    scheduler.add_job(send_scheduled_post, CronTrigger(hour=21, minute=0), args=[4])
    
    scheduler.start()

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
    logging.info(f"Веб-сервер заглушки запущен на порту {{port}}")

async def main():
    global bot
    try:
        await start_web_server()
    except Exception as e:
        logging.error(f"Не удалось запустить веб-сервер: {{e}}")

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or ":" not in BOT_TOKEN:
        logging.error("❌❌❌ ОШИБКА НАСТРОЙКИ BOT_TOKEN! ❌❌❌")
        logging.error("Бот находится в режиме ожидания настроек. Веб-сервер запущен!")
        while True:
            await asyncio.sleep(3600)

    try:
        bot = Bot(token=BOT_TOKEN)
        setup_scheduler()
        logging.info("🏀 Баскетбольный планировщик успешно запущен!")
        logging.info("🤖 Начинаем опрос Telegram (Polling)...")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске бота: {{e}}")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
