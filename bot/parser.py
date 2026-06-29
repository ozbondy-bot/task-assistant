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

async def get_ai_emoji(text: str) -> str:
    text_lower = text.lower()
    
    # 1. Try Gemini API
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
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
            import aiohttp
            import re
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        emoji_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        emoji_text = emoji_text.replace(" ", "").replace("\n", "").replace("\r", "")
                        match = re.match(r'^([\u2600-\u27BF\U0001f000-\U0001f9ff])', emoji_text)
                        if match:
                            return match.group(1)
        except Exception as e:
            logger.error(f"Error fetching AI emoji from Gemini: {e}")
            
    # 2. Local fallback rules (detailed substring checks)
    rules = [
        (["таблет", "лекарств", "витамин", "аптек"], "💊"),
        (["пылесос"], "🧹"),
        (["посудомой", "посуд", "тарелк", "чашк", "ложек", "вилк"], "🍽"),
        (["стир", "бель", "одеял", "наматрас", "постир", "сложить", "одежд"], "🧺"),
        (["глаж", "гладить", "утюг"], "👔"),
        (["зеркал", "окно", "окна", "стекл"], "✨"),
        (["пыль"], "💨"),
        (["поилк", "асю"], "🐈"),
        (["цвет", "полив", "растен", "цветок"], "🌱"),
        (["готовк", "готовит", "обед", "ужин", "завтр", "еда", "кухн"], "🍳"),
        (["духовк", "плит"], "🍳"),
        (["шкаф", "комод", "тумб"], "🗄"),
        (["пол ", "полы"], "🧹"),
        (["мусор", "пакет"], "🗑"),
        (["разбор", "разобрать", "вещ", "коробк", "порядок"], "📦"),
        (["стол"], "🧼"),
        (["мыть", "чистк", "раковин", "кош", "корм", "кот", "животн", "ванн", "туалет", "душ", "сантех"], "🧼"),
    ]
    
    for kw_list, emoji in rules:
        for kw in kw_list:
            if kw in text_lower:
                return emoji
        
    return "🧹" # Default fallback chore emoji


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
