import os
import logging
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import sqlite3
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler
from groq import Groq
from dotenv import load_dotenv
import tempfile
import re

# === Настройки ===
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YANDEX_GEOCODER_API_KEY = os.getenv("YANDEX_GEOCODER_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("❗ Установите TELEGRAM_TOKEN и GROQ_API_KEY в файле .env!")

DB_PATH = Path("meetings.db")

RU_MONTHS = {m: i for i, months in enumerate([
    ["январь", "января"], ["февраль", "февраля"], ["март", "марта"],
    ["апрель", "апреля"], ["май", "мая"], ["июнь", "июня"],
    ["июль", "июля"], ["август", "августа"], ["сентябрь", "сентября"],
    ["октябрь", "октября"], ["ноябрь", "ноября"], ["декабрь", "декабря"]
], 1) for m in months}

_groq_client = None
def get_groq_client():
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client

# === База данных ===
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                start_time TEXT NOT NULL,
                duration_minutes INTEGER DEFAULT 30,
                location TEXT
            )
        """)
        conn.commit()

init_db()

def create_meeting(user_id: int, summary: str, start_time: str, duration: int = 30, location: str = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO meetings (user_id, summary, start_time, duration_minutes, location) VALUES (?, ?, ?, ?, ?)",
            (user_id, summary, start_time, duration, location)
        )
        conn.commit()

def get_meetings(user_id: int, time_min: str = None, time_max: str = None, query: str = None):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        sql = "SELECT summary, start_time, duration_minutes, location FROM meetings WHERE user_id = ?"
        params = [user_id]
        if time_min:
            sql += " AND start_time >= ?"
            params.append(time_min)
        if time_max:
            sql += " AND start_time < ?"
            params.append(time_max)
        if query:
            sql += " AND (summary LIKE ? OR location LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])
        sql += " ORDER BY start_time"
        cur.execute(sql, params)
        return cur.fetchall()

def find_meeting_by_query(user_id: int, query: str):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT summary, location, start_time FROM meetings WHERE user_id = ? AND (summary LIKE ? OR location LIKE ?) ORDER BY start_time DESC LIMIT 1",
            (user_id, f"%{query}%", f"%{query}%")
        )
        row = cur.fetchone()
        if row:
            return {
                'summary': row[0],
                'location': row[1] or 'Адрес не указан',
                'start': datetime.fromisoformat(row[2])
            }
    return None

def update_meeting_location(user_id: int, summary: str, start_time: str, new_location: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE meetings SET location = ? WHERE user_id = ? AND summary = ? AND start_time = ?",
            (new_location, user_id, summary, start_time)
        )
        conn.commit()

def update_meeting_summary(user_id: int, old_query: str, new_summary: str):
    meetings = smart_get_meetings(user_id, query=old_query)
    if not meetings:
        return False, None
    if len(meetings) == 1:
        old_summary, start_time, _, _ = meetings[0]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE meetings SET summary = ? WHERE user_id = ? AND summary = ? AND start_time = ?",
                (new_summary, user_id, old_summary, start_time)
            )
            conn.commit()
        return True, None
    return False, meetings

# === Умный поиск по дате ===
def smart_get_meetings(user_id: int, query: str = None, time_min: str = None, time_max: str = None):
    if query:
        lower_query = query.lower()
        now = datetime.now(timezone.utc)
        target_date = None

        if "завтра" in lower_query:
            target_date = now + timedelta(days=1)
        elif "сегодня" in lower_query:
            target_date = now
        elif "послезавтра" in lower_query:
            target_date = now + timedelta(days=2)

        date_match = re.search(r'(\d{1,2})\s*(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)', lower_query)
        if date_match and not target_date:
            day = int(date_match.group(1))
            month_str = date_match.group(2)
            month = RU_MONTHS.get(month_str, now.month)
            year = now.year if month >= now.month else now.year + 1
            target_date = datetime(year, month, day, tzinfo=timezone.utc)

        num_date_match = re.search(r'(\d{1,2})\.(\d{1,2})', lower_query)
        if num_date_match and not target_date:
            day = int(num_date_match.group(1))
            month = int(num_date_match.group(2))
            year = now.year if month >= now.month else now.year + 1
            try:
                target_date = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass

        if target_date:
            time_min = target_date.strftime("%Y-%m-%dT00:00:00")
            time_max = (target_date + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
            query = re.sub(r'(завтра|сегодня|послезавтра|\d{1,2}\s*(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|\d{1,2}\.\d{1,2})', '', lower_query, count=1).strip()
            if not query:
                query = None

    return get_meetings(user_id, time_min, time_max, query)

# === Яндекс.Geocoder ===
async def geocode_address(address: str):
    if not YANDEX_GEOCODER_API_KEY:
        return None, None
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {
        "apikey": YANDEX_GEOCODER_API_KEY,
        "format": "json",
        "geocode": address,
        "results": 1
    }
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            feature = data["response"]["GeoObjectCollection"]["featureMember"]
            if not feature:
                return None, None
            coords = feature[0]["GeoObject"]["Point"]["pos"]
            lon, lat = coords.split()
            return float(lat), float(lon)
    except Exception as e:
        logging.error(f"Геокодинг ошибка: {e}")
        return None, None

# === Парсинг через Groq ===
def parse_intent(user_msg: str):
    today = datetime.now().strftime("%Y-%m-%d")
    system_prompt = f"""
Ты — ассистент руководителя. Сегодня {today}.
Верни ТОЛЬКО JSON.

ВАЖНО: в "summary" всегда включай дату и время, если они есть! Пример: "С Регина 8 ноября в 20:00 по адресу Уфа"

Действия:
- "create"
- "list"
- "route"
- "get_location"
- "update_location"
- "update_summary"

Примеры:
• "Встреча с Лейсан 10 ноября в 15:00" → {{"action":"create","summary":"С Лейсан 10 ноября в 15:00","datetime":"2025-11-10T15:00:00"}}
• "Измени встречу 8 ноября, добавь имя Регина" → {{"action":"update_summary","query":"8 ноября","new_summary":"С Регина 8 ноября в 20:00"}}
• "Добавь адрес Королева 30 к встрече 8 ноября" → {{"action":"update_location","query":"8 ноября","location":"Уфа, Королева 30"}}
• "Покажи встречи" → {{"action":"list"}}
"""
    try:
        resp = get_groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Сообщение: {user_msg}"}
            ],
            temperature=0.2,
            max_tokens=300,
            response_format={"type": "json_object"}
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        logging.error(f"Groq ошибка: {e}")
        return None

# === Маршруты ===
async def reply_with_route(update: Update, context: ContextTypes.DEFAULT_TYPE, event: dict):
    user = update.effective_user
    name = user.first_name or "Коллега"
    dest = event['location']
    summary = event['summary']

    if dest == 'Адрес не указан':
        await update.message.reply_text(f"У встречи «{summary}» адрес не указан. Добавьте: «Добавь адрес ... к встрече с ...»")
        return

    coords = await geocode_address(dest)
    if not coords or not coords[0]:
        link = f"https://yandex.ru/maps/?text={quote(dest)}&rtt=auto"
        await update.message.reply_text(f"📍 {dest}\n[🚗 Открыть в навигаторе]({link})", parse_mode="Markdown")
        return

    lat, lon = coords
    user_loc = context.user_data.get('last_location')
    if user_loc:
        ulat, ulon = user_loc
        link = f"https://yandex.ru/maps/?rtext={ulat},{ulon}~{lat},{lon}&rtt=auto"
        await update.message.reply_text(
            f"Готово, {name}! 🗺️\nВстреча «{summary}»\n📍 {dest}\n[🚀 Построить маршрут от вас]({link})",
            parse_mode="Markdown"
        )
    else:
        link = f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"
        await update.message.reply_text(
            f"Конечно, {name}! 🚗\nВстреча «{summary}»\n📍 {dest}\n[🚀 Открыть навигатор]({link})\n\n"
            f"💡 Отправьте геопозицию (скрепка → Геопозиция), и маршрут будет от вас!",
            parse_mode="Markdown"
        )

async def send_route_to_event(update: Update, context: ContextTypes.DEFAULT_TYPE, event: dict):
    user = update.effective_user
    name = user.first_name or "Коллега"
    dest = event['location']
    if dest == 'Адрес не указан':
        await update.message.reply_text(f"У встречи «{event['summary']}» не указано место.")
        return

    coords = await geocode_address(dest)
    if not coords or not coords[0]:
        link = f"https://yandex.ru/maps/?rtext=~{quote(dest)}&rtt=auto"
        await update.message.reply_text(f"Адрес: {dest}\n[🚗 Маршрут]({link})", parse_mode="Markdown")
        return

    lat, lon = coords
    user_loc = context.user_data.get('last_location')
    if user_loc:
        ulat, ulon = user_loc
        link = f"https://yandex.ru/maps/?rtext={ulat},{ulon}~{lat},{lon}&rtt=auto"
        await update.message.reply_text(
            f"Отлично, {name}! 🗺️\nДо «{event['summary']}»:\n📍 {dest}\n[🚀 Навигация]({link})",
            parse_mode="Markdown"
        )
    else:
        link = f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"
        await update.message.reply_text(
            f"Конечно, {name}! 🚗\nДо «{event['summary']}»:\n📍 {dest}\n[👉 Навигатор]({link})\n\n"
            f"💡 Отправьте геопозицию (📎 → Геопозиция), чтобы строить маршрут от вас!",
            parse_mode="Markdown"
        )

# === Обработчики ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "Коллега"
    user_id = user.id
    msg = update.message.text
    intent = parse_intent(msg)
    if not intent:
        await update.message.reply_text(f"Извините, {name}, не понял. Примеры:\n• «Лейсан завтра в 13:00»\n• «Измени встречу 8 ноября, добавь Регина»")
        return

    action = intent.get("action")
    try:
        if action == "create":
            summary = intent.get("summary") or "Встреча"
            dt = intent.get("datetime")
            dur = intent.get("duration_minutes", 30)
            loc = intent.get("location")
            if not dt:
                await update.message.reply_text(f"{name}, укажите время.")
                return
            create_meeting(user_id, summary, dt, dur, loc)
            start = datetime.fromisoformat(dt)
            reply = f"Принято, {name}! 🗓\n«{summary}» на {start.strftime('%d.%m в %H:%M')}"
            if loc:
                reply += f"\n📍 {loc}"
            await update.message.reply_text(reply)

        elif action == "list":
            date_filter = intent.get("date_filter")
            query = intent.get("query")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            time_min = now.strftime("%Y-%m-%dT%H:%M:%S")
            time_max = None
            human = "в ближайшее время"

            if date_filter:
                df = date_filter.lower()
                if df in ("этот месяц", "в этом месяце"):
                    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
                    end = datetime(now.year + (1 if now.month == 12 else 0), (now.month % 12) + 1, 1, tzinfo=timezone.utc)
                    time_min = start.strftime("%Y-%m-%dT%H:%M:%S")
                    time_max = end.strftime("%Y-%m-%dT%H:%M:%S")
                    human = "в этом месяце"
                elif df in RU_MONTHS:
                    month = RU_MONTHS[df]
                    year = now.year if month >= now.month else now.year + 1
                    start = datetime(year, month, 1, tzinfo=timezone.utc)
                    end = datetime(year + (1 if month == 12 else 0), (month % 12) + 1, 1, tzinfo=timezone.utc)
                    time_min = start.strftime("%Y-%m-%dT%H:%M:%S")
                    time_max = end.strftime("%Y-%m-%dT%H:%M:%S")
                    human = f"в {date_filter}"

            meetings = smart_get_meetings(user_id, query=query, time_min=time_min, time_max=time_max)
            if not meetings:
                await update.message.reply_text(f"{name}, в этот период встреч нет. ☕")
            else:
                reply = f"Расписание {human}, {name}:\n"
                for summary, start_time, _, _ in meetings:
                    start = datetime.fromisoformat(start_time)
                    reply += f"\n• {start.strftime('%d.%m %H:%M')} — {summary}"
                await update.message.reply_text(reply)

        elif action in ("route", "get_location"):
            query = intent.get("query") or intent.get("summary")
            if not query:
                await update.message.reply_text(f"{name}, уточните встречу.")
                return
            event = find_meeting_by_query(user_id, query)
            if not event:
                meetings = smart_get_meetings(user_id, query=query)
                if meetings:
                    summary, start_time, _, location = meetings[0]
                    event = {
                        'summary': summary,
                        'location': location or 'Адрес не указан',
                        'start': datetime.fromisoformat(start_time)
                    }
            if not event:
                await update.message.reply_text(f"Не нашёл встречи с «{query}».")
            else:
                if action == "get_location":
                    await reply_with_route(update, context, event)  # теперь с маршрутом!
                else:
                    await reply_with_route(update, context, event)

        elif action == "update_location":
            query = intent.get("query")
            loc = intent.get("location")
            if not query or not loc:
                await update.message.reply_text(f"{name}, уточните встречу и адрес.")
                return
            meetings = smart_get_meetings(user_id, query=query)
            if not meetings:
                await update.message.reply_text(f"Не нашёл встречу с «{query}».")
                return
            if len(meetings) == 1:
                summary, start_time, _, _ = meetings[0]
                update_meeting_location(user_id, summary, start_time, loc)
                await update.message.reply_text(f"✅ Адрес обновлён:\n📍 {loc}")
            else:
                reply = "Найдено несколько встреч:\n"
                for i, (s, st, _, _) in enumerate(meetings, 1):
                    start = datetime.fromisoformat(st)
                    reply += f"\n{i}. {start.strftime('%d.%m %H:%M')} — {s}"
                reply += "\n\nУточните точнее."
                await update.message.reply_text(reply)

        elif action == "update_summary":
            query = intent.get("query")
            new_summary = intent.get("new_summary")
            if not query or not new_summary:
                await update.message.reply_text(f"{name}, уточните какую встречу и новое название.")
                return
            success, meetings = update_meeting_summary(user_id, query, new_summary)
            if success:
                await update.message.reply_text(f"✅ Название встречи обновлено на «{new_summary}»")
            else:
                if meetings and len(meetings) > 1:
                    reply = "Найдено несколько встреч:\n"
                    for i, (s, st, _, _) in enumerate(meetings, 1):
                        start = datetime.fromisoformat(st)
                        reply += f"\n{i}. {start.strftime('%d.%m %H:%M')} — {s}"
                    reply += "\n\nУточните точнее."
                    await update.message.reply_text(reply)
                else:
                    await update.message.reply_text(f"Не нашёл встречу с «{query}».")

        elif action == "where":
            query = intent.get("query")
            if not query:
                await update.message.reply_text(f"{name}, уточните какую встречу.")
                return
            event = find_meeting_by_query(user_id, query)
            if not event:
                # Попробуем умный поиск
                meetings = smart_get_meetings(user_id, query=query)
                if not meetings:
                    await update.message.reply_text(f"Не нашёл встречу с «{query}».")
                    return
                if len(meetings) > 1:
                    reply = "Найдено несколько:\n"
                    for i, (s, st, _, loc) in enumerate(meetings, 1):
                        start = datetime.fromisoformat(st)
                        reply += f"\n{i}. {start.strftime('%d.%m %H:%M')} — {s}" + (f" ({loc})" if loc else "")
                    reply += "\n\nУточните точнее."
                    await update.message.reply_text(reply)
                    return
                # Берём первую
                summary, start_time, _, location = meetings[0]
                event = {
                    'summary': summary,
                    'location': location or 'Адрес не указан',
                    'start': datetime.fromisoformat(start_time)
                }
            await reply_with_route(update, context, event)

    except Exception as e:
        logging.error(f"Ошибка обработки: {e}")
        await update.message.reply_text(f"Ой, {name}, что-то пошло не так… 🙏")

# === Голос ===
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "Коллега"
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"🎙️ Распознаю, {name}…")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    temp_path = tempfile.gettempdir() + f"/voice_{update.message.message_id}.ogg"
    await file.download_to_drive(temp_path)

    try:
        with open(temp_path, "rb") as f:
            transcription = get_groq_client().audio.transcriptions.create(
                file=("voice.ogg", f, "audio/ogg"),
                model="whisper-large-v3",
                language="ru",
                response_format="text"
            )
        text = transcription.text.strip() if hasattr(transcription, 'text') else str(transcription).strip()

        if text:
            update.message.text = text
            await handle_text(update, context)
        else:
            await context.bot.send_message(chat_id, "😶 Ничего не услышал.")
    except Exception as e:
        logging.error(f"Ошибка голоса: {e}")
        await context.bot.send_message(chat_id, "❌ Не смог распознать голосовое.")
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.message.location
    context.user_data['last_location'] = (location.latitude, location.longitude)
    await update.message.reply_text("📍 Ваше местоположение сохранено! Теперь маршруты будут от вас.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📖 Инструкция", callback_data='show_help')],
        [InlineKeyboardButton("🚀 Создать встречу", callback_data='example_create')],
        [InlineKeyboardButton("🗺️ Где встреча?", callback_data='example_where')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👔 Привет! Я твой умный ассистент по встречам.\n"
        "Нажми кнопку ниже или просто напиши/скажи голосовым что нужно:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # убирает "часики" с кнопки

    help_text = """
📖 **Как со мной работать**

🔹 **Создать встречу**  
   • Регина 8 ноября в 20:00  
   • Лейсан завтра в 13:30 по адресу Уфа, Ленина 5  

🔹 **Где проходит встреча?** (сразу с маршрутом!)  
   • Где встреча с Региной?  
   • Адрес 8 ноября?  
   • Как добраться до Лейсан?  

🔹 **Изменить**  
   • Добавь адрес Королева 30 к встрече с Региной  
   • Измени встречу 8 ноября, добавь имя Регина  

🔹 **Список встреч**  
   • Покажи все встречи  
   • Что завтра? / Встречи в ноябре  

🔹 **Голосовые** — просто говори!  
🔹 Отправь геопозицию → маршруты от тебя 🚗
    """.strip()

    example_create = "Пример: «Регина завтра в 20:00 по адресу Королева 30»"
    example_where = "Пример: «Где встреча с Региной?»"

    if query.data == 'show_help':
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode="Markdown")

    elif query.data == 'example_create':
        await query.edit_message_text(example_create + "\n\nНажми ниже:", reply_markup=back_keyboard())

    elif query.data == 'example_where':
        await query.edit_message_text(example_where + "\n\nНажми ниже:", reply_markup=back_keyboard())

    elif query.data == 'back_to_menu':
        await query.edit_message_text(
            "👔 Привет! Я твой умный ассистент по встречам.\n"
            "Нажми кнопку ниже или просто напиши/скажи голосовым что нужно:",
            reply_markup=main_keyboard()
        )

# Вспомогательные функции
def main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📖 Инструкция", callback_data='show_help')],
        [InlineKeyboardButton("🚀 Создать встречу", callback_data='example_create')],
        [InlineKeyboardButton("🗺️ Где встреча?", callback_data='example_where')]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_keyboard():
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    return InlineKeyboardMarkup(keyboard)

# === Запуск ===
def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    print("Запускаю бота... Токен ок")
    
    app = Application.builder().token(TELEGRAM_TOKEN) \
        .http_version("1.1") \
        .get_updates_http_version("1.1") \
        .build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("✅ Бот запущен на SQLite! Ожидаю сообщения...")
    
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logging.error("Критическая ошибка polling:")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()