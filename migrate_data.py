import os
import sys
import csv
import asyncio
import asyncpg
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = []
        for arg in args:
            if isinstance(arg, str):
                safe_args.append(arg.encode(sys.stdout.encoding or 'ascii', errors='replace').decode(sys.stdout.encoding or 'ascii'))
            else:
                safe_args.append(arg)
        try:
            print(*safe_args, **kwargs)
        except Exception:
            # Absolute fallback
            print(*(str(a).encode('ascii', errors='replace').decode() for a in args), **kwargs)

def parse_date(date_str):
    if not date_str or date_str.lower() == 'null' or date_str == '':
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        try:
            return datetime.strptime(date_str.split()[0], "%Y-%m-%d").date()
        except Exception:
            return None

def parse_datetime(dt_str):
    if not dt_str or dt_str.lower() == 'null' or dt_str == '':
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            # try parsing iso format
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None

def parse_bool(bool_str):
    if not bool_str:
        return False
    return bool_str.lower() in ('true', 't', '1', 'yes')

def parse_int(int_str, default=0):
    if not int_str or int_str.lower() == 'null' or int_str == '':
        return default
    try:
        return int(int_str)
    except Exception:
        return default

async def migrate():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        safe_print("DATABASE_URL is not set!")
        sys.exit(1)

    backup_dir = r"C:\Users\Шурик\Desktop\Для ботов\неон БД"
    personal_tasks_path = r"C:\Users\Шурик\Desktop\Для ботов\tasks_rows.csv"

    safe_print("Connecting to Supabase...")
    conn = await asyncpg.connect(db_url)

    safe_print("Cleaning database tables before migration...")
    await conn.execute("TRUNCATE TABLE completions, task_instances, task_templates, rewards, reward_purchases, personal_tasks, shopping_items, users, houses CASCADE;")

    # 1. Migrate houses.csv (only house_id = 81)
    safe_print("\n--- Migrating Houses ---")
    houses_file = os.path.join(backup_dir, "houses.csv")
    house_exists = False
    with open(houses_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if parse_int(row.get("id")) == 81:
                safe_print(f"Found active house: {row.get('name')} (ID 81)")
                await conn.execute(
                    """INSERT INTO houses (id, name, join_code, timezone, last_summary_date) 
                       VALUES ($1, $2, $3, $4, $5)""",
                    81,
                    row.get("name"),
                    row.get("join_code"),
                    row.get("timezone", "Europe/Moscow"),
                    parse_date(row.get("last_summary_date"))
                )
                house_exists = True
                break
    
    if not house_exists:
        safe_print("Active house 81 not found in houses.csv! Creating a fallback...")
        await conn.execute(
            """INSERT INTO houses (id, name, join_code, timezone) 
               VALUES ($1, $2, $3, $4)""",
            81, "Уютное гнездышко ☕️", "KW4ISW", "Europe/Moscow"
        )

    # 2. Migrate users.csv (only users belonging to house_id = 81)
    safe_print("\n--- Migrating Users ---")
    users_file = os.path.join(backup_dir, "users.csv")
    valid_user_ids = set()
    user_tg_to_id = {}
    with open(users_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            house_id = parse_int(row.get("house_id"))
            if house_id == 81:
                uid = parse_int(row.get("id"))
                tg_id = parse_int(row.get("telegram_id"))
                valid_user_ids.add(uid)
                user_tg_to_id[tg_id] = uid
                safe_print(f"Migrating user: {row.get('display_name')} (ID: {uid}, TG: {tg_id})")
                await conn.execute(
                    """INSERT INTO users (id, telegram_id, username, full_name, display_name, house_id, is_house_owner, points, last_today_message_id) 
                       VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8)""",
                    uid,
                    tg_id,
                    row.get("username"),
                    row.get("full_name"),
                    row.get("display_name"),
                    81,
                    parse_bool(row.get("is_house_owner")),
                    parse_int(row.get("last_today_message_id"))
                )

    # 3. Migrate task_templates.csv (only templates belonging to house_id = 81)
    safe_print("\n--- Migrating Task Templates ---")
    templates_file = os.path.join(backup_dir, "task_templates.csv")
    valid_template_ids = set()
    with open(templates_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            house_id = parse_int(row.get("house_id"))
            if house_id == 81:
                tid = parse_int(row.get("id"))
                valid_template_ids.add(tid)
                await conn.execute(
                    """INSERT INTO task_templates (id, house_id, title, points, periodicity, period_days, weekday, month_day, start_date, deleted) 
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                    tid,
                    81,
                    row.get("title"),
                    parse_int(row.get("points"), 1),
                    row.get("periodicity"),
                    parse_int(row.get("period_days")) if row.get("period_days") else None,
                    parse_int(row.get("weekday")) if row.get("weekday") else None,
                    parse_int(row.get("month_day")) if row.get("month_day") else None,
                    parse_date(row.get("start_date")),
                    parse_bool(row.get("deleted"))
                )
    safe_print(f"Migrated {len(valid_template_ids)} task templates.")

    # 4. Migrate task_instances.csv (only instances belonging to valid template ids of house 81)
    safe_print("\n--- Migrating Task Instances ---")
    instances_file = os.path.join(backup_dir, "task_instances.csv")
    valid_instance_ids = set()
    instance_count = 0
    with open(instances_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = parse_int(row.get("template_id"))
            if tid in valid_template_ids:
                inst_id = parse_int(row.get("id"))
                valid_instance_ids.add(inst_id)
                done_by = parse_int(row.get("done_by_user_id"))
                if done_by not in valid_user_ids:
                    done_by = None
                
                await conn.execute(
                    """INSERT INTO task_instances (id, template_id, date, status, priority, done_by_user_id, done_at) 
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    inst_id,
                    tid,
                    parse_date(row.get("date")),
                    row.get("status", "free"),
                    parse_int(row.get("priority")),
                    done_by,
                    parse_datetime(row.get("done_at"))
                )
                instance_count += 1
    safe_print(f"Migrated {instance_count} task instances.")

    # 5. Migrate completions.csv (only completions for valid user ids and instance ids)
    safe_print("\n--- Migrating Completions ---")
    completions_file = os.path.join(backup_dir, "completions.csv")
    completion_count = 0
    user_points = {uid: 0 for uid in valid_user_ids}
    
    with open(completions_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = parse_int(row.get("user_id"))
            inst_id = parse_int(row.get("task_instance_id"))
            if uid in valid_user_ids and inst_id in valid_instance_ids:
                pts = parse_int(row.get("points"))
                user_points[uid] += pts
                
                await conn.execute(
                    """INSERT INTO completions (id, user_id, task_instance_id, points, created_at) 
                       VALUES ($1, $2, $3, $4, $5)""",
                    parse_int(row.get("id")),
                    uid,
                    inst_id,
                    pts,
                    parse_datetime(row.get("created_at"))
                )
                completion_count += 1
    safe_print(f"Migrated {completion_count} completions.")

    # Update users' points balance based on sum of completions
    for uid, pts in user_points.items():
        safe_print(f"User ID {uid} total completion points: {pts}")
        await conn.execute("UPDATE users SET points = $1 WHERE id = $2", pts, uid)

    # 6. Migrate rewards.csv (only rewards of house_id = 81)
    safe_print("\n--- Migrating Rewards ---")
    rewards_file = os.path.join(backup_dir, "rewards.csv")
    reward_count = 0
    with open(rewards_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            house_id = parse_int(row.get("house_id"))
            if house_id == 81:
                await conn.execute(
                    """INSERT INTO rewards (id, house_id, title, price) 
                       VALUES ($1, $2, $3, $4)""",
                    parse_int(row.get("id")),
                    81,
                    row.get("title"),
                    parse_int(row.get("price"))
                )
                reward_count += 1
    safe_print(f"Migrated {reward_count} rewards.")

    # 7. Migrate personal tasks (tasks_rows.csv)
    # Since these are personal tasks for "Шурик", his telegram_id is 680630275.
    # His user ID in houses/users table is 1.
    safe_print("\n--- Migrating Personal Tasks ---")
    shurik_id = user_tg_to_id.get(680630275, 1)
    personal_task_count = 0
    
    with open(personal_tasks_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # We don't import deleted tasks
            if parse_bool(row.get("is_deleted")):
                continue
                
            await conn.execute(
                """INSERT INTO personal_tasks (user_id, text, date_execution, is_completed, category, recurrence, is_deleted) 
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                shurik_id,
                row.get("text"),
                parse_date(row.get("date_execution")),
                parse_bool(row.get("is_completed")),
                row.get("category", "inbox"),
                row.get("recurrence") if row.get("recurrence") else None,
                False
            )
            personal_task_count += 1
    safe_print(f"Migrated {personal_task_count} personal tasks for User ID {shurik_id} (Шурик).")

    # 8. Reset serial sequences to avoid conflicts on new inserts
    safe_print("\n--- Resetting Sequences ---")
    sequences = [
        ("houses", "id"),
        ("users", "id"),
        ("personal_tasks", "id"),
        ("shopping_items", "id"),
        ("task_templates", "id"),
        ("task_instances", "id"),
        ("completions", "id"),
        ("rewards", "id"),
        ("reward_purchases", "id"),
        ("pending_actions", "id")
    ]
    for table, col in sequences:
        val = await conn.fetchval(f"SELECT MAX({col}) FROM {table}")
        if val is not None:
            await conn.execute(f"SELECT setval(pg_get_serial_sequence($1, $2), $3)", table, col, val)
            safe_print(f"Reset sequence for table '{table}' to {val}")

    await conn.close()
    safe_print("\nMigration completed successfully!")

if __name__ == "__main__":
    asyncio.run(migrate())
