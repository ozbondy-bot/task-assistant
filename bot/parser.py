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



def clean_task_text(text: str) -> str:
    """Remove status icons from task text."""
    for marker in STATUS_ICONS:
        text = text.replace(marker, "")
    return text.strip()
