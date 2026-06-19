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
    
    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Parsed jobs table
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
    
    # User current leads table (for persisting FSM state of shown leads)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_current_leads (
            chat_id INTEGER,
            job_index INTEGER,
            external_id TEXT,
            PRIMARY KEY (chat_id, job_index)
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("SQLite database successfully initialized.")

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
    logger.info(f"User {chat_id} (@{normalized_username}) registered in database.")

def get_chat_ids_for_allowed_users(allowed_users_raw):
    """
    Resolves the list of allowed users (IDs or usernames) to numeric chat_ids.
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
        # Search chat_ids by usernames
        placeholders = ",".join("?" for _ in allowed_usernames)
        cursor.execute(f"SELECT chat_id FROM users WHERE username IN ({placeholders})", allowed_usernames)
        for row in cursor.fetchall():
            resolved_ids.add(row["chat_id"])
        conn.close()
        
    return list(resolved_ids)

def insert_jobs(jobs):
    """
    Inserts a list of vacancies/jobs.
    Each vacancy in the list must be a dictionary with keys:
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
            logger.error(f"Error inserting job {job.get('external_id')}: {e}")
            
    conn.commit()
    conn.close()
    return inserted_count

def get_new_matched_jobs(limit=25):
    """
    Returns a list of new vacancies matching the filter, balanced between sources (Kwork and FL.ru).
    If one source does not have enough jobs, the other source fills the remaining spots.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Target count per source (try to split 50/50)
    target_half = (limit + 1) // 2
    
    # 1. Fetch new matched jobs from Kwork
    cursor.execute("""
        SELECT id, source, external_id, title, url, description, price, created_at, matched, status
        FROM parsed_jobs
        WHERE matched = 1 AND status = 'new' AND source = 'kwork'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    kwork_jobs = [dict(row) for row in cursor.fetchall()]
    
    # 2. Fetch new matched jobs from FL.ru
    cursor.execute("""
        SELECT id, source, external_id, title, url, description, price, created_at, matched, status
        FROM parsed_jobs
        WHERE matched = 1 AND status = 'new' AND source = 'fl'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    fl_jobs = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    kwork_len = len(kwork_jobs)
    fl_len = len(fl_jobs)
    
    # Greedy balancing logic
    if kwork_len >= target_half and fl_len >= target_half:
        selected_kwork = kwork_jobs[:target_half]
        selected_fl = fl_jobs[:limit - target_half]
    elif kwork_len < target_half:
        selected_kwork = kwork_jobs
        selected_fl = fl_jobs[:limit - kwork_len]
    else:
        selected_fl = fl_jobs
        selected_kwork = kwork_jobs[:limit - fl_len]
        
    combined = selected_kwork + selected_fl
    # Sort the combined list by job ID in descending order so that the newest vacancies are first
    combined.sort(key=lambda x: x["id"], reverse=True)
    
    return combined[:limit]

def update_jobs_status(external_ids, new_status):
    """
    Batch updates status for a list of vacancies by their external_id.
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
    Deletes old vacancies from the DB to keep it from growing too large.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM parsed_jobs WHERE created_at < datetime('now', '-{days} days')")
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted_count > 0:
        logger.info(f"Deleted {deleted_count} old vacancies (older than {days} days).")

def get_training_data():
    """
    Returns data for training the ML model.
    Extracts vacancies with status 'approved' (class 1) and 'rejected' (class 0).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, description, status
        FROM parsed_jobs
        WHERE status IN ('approved', 'rejected')
    """)
    rows = cursor.fetchall()
    conn.close()
    
    X = []
    y = []
    for row in rows:
        title = row["title"] or ""
        desc = row["description"] or ""
        text = f"{title} {desc}".strip()
        label = 1 if row["status"] == "approved" else 0
        X.append(text)
        y.append(label)
        
    return X, y

def save_current_leads(chat_id, jobs):
    """
    Saves the list of current leads shown to a user, replacing any existing ones.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_current_leads WHERE chat_id = ?", (chat_id,))
        for idx, job in enumerate(jobs):
            cursor.execute("""
                INSERT OR REPLACE INTO user_current_leads (chat_id, job_index, external_id)
                VALUES (?, ?, ?)
            """, (chat_id, idx, job["external_id"]))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving current leads for {chat_id}: {e}")
    finally:
        conn.close()

def get_current_leads(chat_id):
    """
    Retrieves the current leads for a user from the database.
    If no current leads are found in the persistent FSM table,
    falls back to the last 25 jobs marked as 'shown'.
    """
    conn = get_connection()
    cursor = conn.cursor()
    jobs = []
    try:
        cursor.execute("""
            SELECT p.id, p.source, p.external_id, p.title, p.url, p.description, p.price, p.created_at, p.matched, p.status
            FROM parsed_jobs p
            JOIN user_current_leads u ON p.external_id = u.external_id
            WHERE u.chat_id = ?
            ORDER BY u.job_index ASC
        """, (chat_id,))
        rows = cursor.fetchall()
        jobs = [dict(row) for row in rows]
        
        # Fallback if no entries are found in user_current_leads (e.g. leads shown before the update)
        if not jobs:
            cursor.execute("""
                SELECT id, source, external_id, title, url, description, price, created_at, matched, status
                FROM parsed_jobs
                WHERE status = 'shown'
                ORDER BY id DESC
                LIMIT 25
            """)
            rows = cursor.fetchall()
            jobs = [dict(row) for row in rows]
            
            # Save the retrieved leads to user_current_leads to register their indices
            if jobs:
                cursor.execute("DELETE FROM user_current_leads WHERE chat_id = ?", (chat_id,))
                for idx, job in enumerate(jobs):
                    cursor.execute("""
                        INSERT OR REPLACE INTO user_current_leads (chat_id, job_index, external_id)
                        VALUES (?, ?, ?)
                    """, (chat_id, idx, job["external_id"]))
                conn.commit()
    except Exception as e:
        logger.error(f"Error getting current leads for {chat_id}: {e}")
    finally:
        conn.close()
    return jobs

def clear_current_leads(chat_id):
    """
    Clears the current leads for a user.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_current_leads WHERE chat_id = ?", (chat_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error clearing current leads for {chat_id}: {e}")
    finally:
        conn.close()
