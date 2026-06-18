import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "jobs.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица спарсенных заказов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS parsed_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            external_id TEXT UNIQUE,
            title TEXT,
            url TEXT,
            description TEXT,
            price TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            matched INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new'
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных SQLite успешно инициализирована.")

def register_user(chat_id, username, first_name):
    normalized_username = None
    if username:
        normalized_username = username.lstrip("@").lower()
        
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (chat_id, username, first_name, last_seen)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = CURRENT_TIMESTAMP
    """, (chat_id, normalized_username, first_name))
    conn.commit()
    conn.close()
    logger.info(f"Пользователь {chat_id} (@{normalized_username}) зарегистрирован в БД.")

def get_chat_ids_for_allowed_users(allowed_users_raw):
    """
    Разрешает список разрешенных пользователей (ID или юзернеймы) в числовые chat_id.
    """
    if not allowed_users_raw:
        return []
        
    allowed_items = [item.strip() for item in allowed_users_raw.split(",") if item.strip()]
    allowed_ids = []
    allowed_usernames = []
    
    for item in allowed_items:
        if item.isdigit():
            allowed_ids.append(int(item))
        else:
            username = item.lstrip("@").lower()
            if username:
                allowed_usernames.append(username)
                
    resolved_ids = set(allowed_ids)
    
    if allowed_usernames:
        conn = get_connection()
        cursor = conn.cursor()
        # Поиск chat_id по юзернеймам
        placeholders = ",".join("?" for _ in allowed_usernames)
        cursor.execute(f"SELECT chat_id FROM users WHERE username IN ({placeholders})", allowed_usernames)
        for row in cursor.fetchall():
            resolved_ids.add(row["chat_id"])
        conn.close()
        
    return list(resolved_ids)

def insert_jobs(jobs):
    """
    Вставляет список вакансий/заказов.
    Каждая вакансия в списке должна быть словарем с ключами:
    source, external_id, title, url, description, price, matched, status
    """
    if not jobs:
        return 0
        
    conn = get_connection()
    cursor = conn.cursor()
    inserted_count = 0
    
    for job in jobs:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO parsed_jobs (source, external_id, title, url, description, price, matched, status)
                VALUES (:source, :external_id, :title, :url, :description, :price, :matched, :status)
            """, job)
            if cursor.rowcount > 0:
                inserted_count += 1
        except Exception as e:
            logger.error(f"Ошибка при вставке заказа {job.get('external_id')}: {e}")
            
    conn.commit()
    conn.close()
    return inserted_count

def get_new_matched_jobs(limit=25):
    """
    Возвращает список новых подходящих под фильтр вакансий.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, source, external_id, title, url, description, price, created_at, matched, status
        FROM parsed_jobs
        WHERE matched = 1 AND status = "new"
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_jobs_status(external_ids, new_status):
    """
    Пакетно обновляет статус для списка вакансий по их external_id.
    """
    if not external_ids:
        return
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in external_ids)
    params = [new_status] + list(external_ids)
    cursor.execute(f"""
        UPDATE parsed_jobs
        SET status = ?
        WHERE external_id IN ({placeholders})
    """, params)
    conn.commit()
    conn.close()

def cleanup_old_jobs(days=14):
    """
    Удаляет старые вакансии из БД, чтобы она не разрасталась.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM parsed_jobs WHERE created_at < datetime('now', '-{days} days')")
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted_count > 0:
        logger.info(f"Удалено {deleted_count} старых вакансий (старше {days} дней).")
