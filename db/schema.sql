-- Drop existing tables to start clean
DROP TABLE IF EXISTS pending_actions CASCADE;
DROP TABLE IF EXISTS reward_purchases CASCADE;
DROP TABLE IF EXISTS rewards CASCADE;
DROP TABLE IF EXISTS completions CASCADE;
DROP TABLE IF EXISTS task_instances CASCADE;
DROP TABLE IF EXISTS task_templates CASCADE;
DROP TABLE IF EXISTS shopping_items CASCADE;
DROP TABLE IF EXISTS personal_tasks CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS houses CASCADE;

-- Create tables
CREATE TABLE houses (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255),
    join_code VARCHAR(50) UNIQUE NOT NULL,
    timezone VARCHAR(50) NOT NULL DEFAULT 'Europe/Moscow',
    last_summary_date DATE
);

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(255),
    full_name VARCHAR(255),
    display_name VARCHAR(255),
    house_id INTEGER REFERENCES houses(id) ON DELETE SET NULL,
    is_house_owner BOOLEAN DEFAULT FALSE,
    points INTEGER DEFAULT 0 NOT NULL,
    last_today_message_id INTEGER
);

CREATE TABLE personal_tasks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    date_execution DATE NOT NULL,
    is_completed BOOLEAN DEFAULT FALSE NOT NULL,
    category VARCHAR(50) DEFAULT 'inbox' NOT NULL,
    recurrence VARCHAR(50),
    is_deleted BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE TABLE shopping_items (
    id SERIAL PRIMARY KEY,
    house_id INTEGER REFERENCES houses(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    item_name VARCHAR(255) NOT NULL,
    price INTEGER DEFAULT 0 NOT NULL,
    priority VARCHAR(50) DEFAULT 'normal' NOT NULL,
    is_bought BOOLEAN DEFAULT FALSE NOT NULL,
    bought_at TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE TABLE task_templates (
    id SERIAL PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    points INTEGER DEFAULT 1 NOT NULL,
    periodicity VARCHAR(50) NOT NULL,
    period_days INTEGER,
    weekday INTEGER,
    month_day INTEGER,
    start_date DATE,
    deleted BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE TABLE task_instances (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES task_templates(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    status VARCHAR(50) DEFAULT 'free' NOT NULL,
    priority INTEGER DEFAULT 0 NOT NULL,
    done_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    done_at TIMESTAMP
);

CREATE TABLE completions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_instance_id INTEGER NOT NULL REFERENCES task_instances(id) ON DELETE CASCADE,
    points INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rewards (
    id SERIAL PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    price INTEGER NOT NULL
);

CREATE TABLE reward_purchases (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_title VARCHAR(255) NOT NULL,
    price INTEGER NOT NULL,
    status VARCHAR(50) DEFAULT 'purchased' NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP
);

CREATE TABLE pending_actions (
    id SERIAL PRIMARY KEY,
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    initiator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type VARCHAR(255) NOT NULL,
    data_payload TEXT NOT NULL
);
