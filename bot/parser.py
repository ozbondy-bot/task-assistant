import re
from datetime import datetime, timedelta


FOOD_EMOJIS = [
    '🍏', '🍎', '🍐', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🫐',
    '🍒', '🍑', '🥭', '🍍', '🥥', '🥝', '🍅', '🥑', '🥦', '🧄',
    '🧅', '🍞', '🥐', '🥨', '🧀', '🍖', '🍗', '🥩', '🥓', '🍔',
    '🍕', '🌮', '🥚', '🥗', '🍿', '🥫', '🍱', '☕', '🍵', '🥤',
]

DAYS_OF_WEEK_RU = {
    "понедельник": 0, "понедельникам": 0,
    "вторник": 1, "вторникам": 1,
    "среду": 2, "средам": 2, "среда": 2,
    "четверг": 3, "четвергам": 3,
    "пятницу": 4, "пятницам": 4, "пятница": 4,
    "субботу": 5, "субботам": 5, "суббота": 5,
    "воскресенье": 6, "воскресеньям": 6,
}

STATUS_ICONS = ['🟢', '🟡', '🔴', '❗️', '⚡', '⬜', '💧', '🤸‍♂️', '🚶‍♂️', '🐈', '📚', '✅']


def parse_input(text: str):
    """
    Parse a text command and return:
    (msg_type, clean_text, target_date, price, priority, recurrence)
    
    msg_type: 'task' | 'purchase'
    """
    text_lower = text.lower().strip()
    target_date = datetime.now().date()
    recurrence = None
    is_purchase = False
    price = 0
    priority = "normal"

    # Check if it's a purchase (defaults to True now)
    is_purchase = True
    if text_lower.startswith("купить "):
        text = text[7:].strip()
        text_lower = text.lower()
        
    words = text.split()
    if words and words[-1].isdigit():
        price = int(words[-1])
        text = " ".join(words[:-1])
        text_lower = text.lower()
    if "срочно" in text_lower:
        priority = "high"
        text = re.sub(r'срочно', '', text, flags=re.IGNORECASE).strip()
        text_lower = text.lower()

    # Parse recurrence keywords
    if "каждый день" in text_lower or "ежедневно" in text_lower:
        recurrence = 'daily'
        text = re.sub(r'каждый день|ежедневно', '', text, flags=re.IGNORECASE).strip()
    elif "раз в 2 недели" in text_lower or "каждые 2 недели" in text_lower:
        recurrence = 'biweekly'
        text = re.sub(r'раз в 2 недели|каждые 2 недели', '', text, flags=re.IGNORECASE).strip()
    elif "каждую неделю" in text_lower or "раз в неделю" in text_lower:
        recurrence = 'weekly'
        text = re.sub(r'каждую неделю|раз в неделю', '', text, flags=re.IGNORECASE).strip()
    elif "каждый месяц" in text_lower or "раз в месяц" in text_lower:
        recurrence = 'monthly'
        text = re.sub(r'каждый месяц|раз в месяц', '', text, flags=re.IGNORECASE).strip()

    text_lower = text.lower().strip()

    # Parse day of week
    for day_str, day_num in DAYS_OF_WEEK_RU.items():
        if f"каждый {day_str}" in text_lower or f"каждую {day_str}" in text_lower or f"по {day_str}" in text_lower:
            recurrence = 'weekly'
            today = datetime.now().date()
            days_ahead = day_num - today.weekday()
            if days_ahead < 0:
                days_ahead += 7
            target_date = today + timedelta(days=days_ahead)
            text = re.sub(rf'(каждый|каждую|по)\s+{day_str}', '', text, flags=re.IGNORECASE).strip()
            text_lower = text.lower().strip()
            break
        elif f"в {day_str}" in text_lower or f"во {day_str}" in text_lower or day_str in text_lower:
            today = datetime.now().date()
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target_date = today + timedelta(days=days_ahead)
            text = re.sub(rf'(в|во)?\s*{day_str}', '', text, flags=re.IGNORECASE).strip()
            text_lower = text.lower().strip()
            break

    # Parse relative dates
    if "послезавтра" in text_lower:
        target_date = datetime.now().date() + timedelta(days=2)
        text = re.sub(r'послезавтра', '', text, flags=re.IGNORECASE).strip()
    elif "завтра" in text_lower:
        target_date = datetime.now().date() + timedelta(days=1)
        text = re.sub(r'завтра', '', text, flags=re.IGNORECASE).strip()

    # Clean prepositions and status icons
    words = text.split()
    prepositions = {"в", "во", "на", "по", "до", "к", "с", "об", "обо", "под", "перед"}
    clean_words = [w for w in words if w.lower().strip(",.?!") not in prepositions]
    text = " ".join(clean_words).strip()

    for marker in STATUS_ICONS:
        text = text.replace(marker, "")
    text = text.strip()

    if is_purchase:
        return "purchase", text, target_date, price, priority, recurrence

    if "срочно" in text.lower():
        text = re.sub(r'срочно', '', text, flags=re.IGNORECASE).strip().capitalize()
        text = f"🔴 {text}"
    else:
        text = text

    return "task", text, target_date, 0, "normal", recurrence


def get_recurrence_delta(recurrence: str) -> timedelta:
    if not recurrence:
        return timedelta(days=1)
    if recurrence.startswith("every_x_days:") or recurrence.startswith("everyxdays:"):
        try:
            days = int(recurrence.split(":")[1])
            return timedelta(days=days)
        except Exception:
            return timedelta(days=1)
    if recurrence == 'daily':
        return timedelta(days=1)
    elif recurrence == 'weekly':
        return timedelta(days=7)
    elif recurrence == 'biweekly':
        return timedelta(days=14)
    elif recurrence == 'monthly':
        return timedelta(days=30)
    return timedelta(days=1)



import aiohttp
import os
import json
import logging

logger = logging.getLogger(__name__)

_working_model = "gemini-1.5-flash"

async def get_ai_emoji(text: str) -> str:
    global _working_model
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "📝"
        
    models_to_try = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-1.5-pro", "gemini-pro", "gemini-2.0-flash-exp"]
    if _working_model in models_to_try:
        models_to_try.remove(_working_model)
    models_to_try.insert(0, _working_model)
    
    for model in models_to_try:
        base_endpoint = os.getenv("GEMINI_API_ENDPOINT", "https://generativelanguage.googleapis.com")
        url = f"{base_endpoint.rstrip('/')}/v1/models/{model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        prompt = (
            f"Тебе дана задача: '{text}'. "
            "Верни ровно один наиболее подходящий эмодзи для этой задачи. "
            "Не пиши никаких объяснений, никаких других слов, только один символ эмодзи."
        )
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }]
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        emoji_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        emoji_text = emoji_text.replace(" ", "").replace("\n", "").replace("\r", "").replace("`", "").replace('"', '').replace("'", "")
                        if emoji_text:
                            _working_model = model
                            return emoji_text
                    elif resp.status in (400, 401, 403):
                        logger.error(f"Gemini API auth/bad-request error for model {model}: Status {resp.status}")
                        return "📝"
                    elif resp.status == 404:
                        continue
                    else:
                        resp_text = await resp.text()
                        logger.error(f"Gemini API error for model {model}: Status {resp.status}, Response: {resp_text}")
        except Exception as e:
            logger.error(f"Error fetching AI emoji from Gemini for model {model}: {e}")
            
    return "📝" # Default fallback chore emoji


def clean_task_text(text: str) -> str:
    """Remove status icons from task text (keeping urgency indicator if present)."""
    is_urgent = text.startswith("🔴")
    
    # Strip any status icons first
    clean_text = text
    for marker in STATUS_ICONS:
        clean_text = clean_text.replace(marker, "")
    clean_text = clean_text.strip()

    if is_urgent:
        clean_text = f"🔴 {clean_text}"
        
    return clean_text


def extract_emoji(text: str) -> str:
    words = text.split()
    for w in words:
        if w == "🔴":
            continue
        if w and ord(w[0]) > 0x2000:
            return w
        break
    return None
