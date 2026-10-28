import sqlite3
from traceback import print_exc

DB_PATH = "./data/vinted_notifications.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_or_update_sqlite_db(db_path):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Using the sql script
        with open(db_path, "r", encoding="utf-8") as sql_file:
            sql_script = sql_file.read()
            cursor.executescript(sql_script)

        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def ensure_schema():
    """Idempotent schema upgrades that don't warrant a version migration."""
    conn = None
    try:
        conn = get_db_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(queries)")]
        if "min_stars" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN min_stars REAL")
        if "min_reviews" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN min_reviews INTEGER")
        if "max_price" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN max_price REAL")
        # pending_scan: the query was just added or edited and should be scraped right away
        # rather than at the next scheduled cycle. push: send Web Push notifications for it.
        if "pending_scan" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN pending_scan INTEGER NOT NULL DEFAULT 0")
        if "push" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN push INTEGER NOT NULL DEFAULT 0")
        item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        for col, col_type in (
            ("seller_id", "INTEGER"),
            ("seller_login", "TEXT"),
            ("seller_stars", "REAL"),
            ("seller_reviews", "INTEGER"),
            ("checked_at", "INTEGER"),  # when availability was last verified
            ("available", "INTEGER"),  # 1 on sale, 0 seen as sold, NULL not known
        ):
            if col not in item_cols:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {col_type}")
        # Accounts and per-user data. user_id 0 is the implicit owner while no account exists.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions (user_id);
            -- listings the account deleted (or that sold): remembered so a later scan does not announce them again
            CREATE TABLE IF NOT EXISTS removed_items (
                item NUMERIC NOT NULL,
                user_id INTEGER NOT NULL,
                removed_at INTEGER NOT NULL,
                PRIMARY KEY (item, user_id)
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, key)
            );
            """
        )
        for table in ("queries", "items", "allowlist"):
            table_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "user_id" not in table_cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"
                )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_user ON queries (user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_user ON items (user_id, item)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_query ON items (query_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_timestamp ON items (timestamp)")
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_filters(query_id):
    """(min_stars, min_reviews, max_price) set on a query; each is None if not set."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT min_stars, min_reviews, max_price FROM queries WHERE id=?",
            (query_id,),
        ).fetchone()
        if not row:
            return None, None, None
        return row[0] or None, row[1] or None, row[2] or None
    except Exception:
        print_exc()
        return None, None, None
    finally:
        if conn:
            conn.close()


def is_item_in_db_by_id(id, user_id):
    """Has this user already been given this listing?"""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT (SELECT COUNT() FROM items WHERE item=? AND user_id=?) "
            "+ (SELECT COUNT() FROM removed_items WHERE item=? AND user_id=?)",
            (id, user_id, id, user_id),
        ).fetchone()
        return bool(row[0])
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_last_timestamp(query_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT last_item FROM queries WHERE id=?", (query_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def update_last_timestamp(query_id, timestamp):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def add_item_to_db(
    id,
    title,
    query_id,
    price,
    timestamp,
    photo_url,
    currency="EUR",
    seller_id=None,
    seller_login=None,
    seller_stars=None,
    seller_reviews=None,
    user_id=0,
):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Insert into db the id and the query_id related to the item
        cursor.execute(
            "INSERT INTO items (item, title, price, currency, timestamp, photo_url, query_id, "
            "seller_id, seller_login, seller_stars, seller_reviews, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                title,
                price,
                currency,
                timestamp,
                photo_url,
                query_id,
                seller_id,
                seller_login,
                seller_stars,
                seller_reviews,
                user_id,
            ),
        )
        # Update the last item for the query
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_queries(user_id=None):
    """
    Queries as (id, url, last_item, name, min_stars, min_reviews, max_price, user_id).
    user_id=None returns every user's queries (used by the scraper).
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        sql = "SELECT id, query, last_item, query_name, min_stars, min_reviews, max_price, user_id FROM queries"
        if user_id is None:
            return conn.execute(sql + " ORDER BY id").fetchall()
        return conn.execute(sql + " WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def get_query_owner(query_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT user_id FROM queries WHERE id=?", (query_id,)).fetchone()
        return row[0] if row else None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_queries_overview(user_id):
    """A user's queries with their filters plus collected-item count and average price."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        return conn.execute(
            "SELECT q.id, q.query, q.query_name, q.last_item, q.min_stars, q.min_reviews, q.max_price, "
            "COUNT(i.item), AVG(i.price), q.push FROM queries q LEFT JOIN items i ON i.query_id = q.id "
            "WHERE q.user_id = ? GROUP BY q.id ORDER BY q.id",
            (user_id,),
        ).fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def is_query_in_db(processed_query, user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COUNT() FROM queries WHERE query = ? AND user_id = ?",
            (processed_query, user_id),
        ).fetchone()
        return bool(row[0])
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_query_to_db(
    query, name=None, min_stars=None, min_reviews=None, max_price=None, user_id=0, push=False
):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO queries (query, last_item, query_name, min_stars, min_reviews, max_price, user_id, push, pending_scan) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 1)",
            (query, name or None, min_stars, min_reviews, max_price, user_id, int(bool(push))),
        )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_id_by_rowid(rowid, user_id):
    """Id of the user's Nth query (as numbered in the Telegram query list)."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY ROWID) rn FROM queries WHERE user_id=?) t WHERE rn=?",
            (user_id, int(rowid)),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def remove_query_from_db(query_number, user_id):
    """Delete one of the user's queries and its items. Returns True if it existed."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM items WHERE query_id=? AND user_id=?", (query_number, user_id))
        deleted = conn.execute(
            "DELETE FROM queries WHERE id=? AND user_id=?", (query_number, user_id)
        ).rowcount
        conn.commit()
        return deleted > 0
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def remove_all_queries_from_db(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM items WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM queries WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def update_query_in_db(
    query_id, query, name, min_stars=None, min_reviews=None, max_price=None, user_id=0, push=None
):
    """
    Update an existing query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new query URL
        name (str, optional): The new name for the query
        min_stars (float, optional): Minimum seller stars (0-5)
        min_reviews (int, optional): Minimum number of seller reviews
        max_price (float, optional): Maximum item price
        push (bool, optional): Web Push notifications on/off; None keeps the current choice

    A changed address starts over (the catalogue is recorded again without notifying) and
    every edit asks for an immediate scan.

    Returns:
        bool: True if the query was updated successfully, False otherwise
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT query FROM queries WHERE id=? AND user_id=?", (query_id, user_id)
        ).fetchone()
        if row is None:
            return False
        reset = ", last_item=NULL" if row[0] != query else ""
        push_sql, push_args = ("", ())
        if push is not None:
            push_sql, push_args = ", push=?", (int(bool(push)),)
        cursor.execute(
            "UPDATE queries SET query=?, query_name=?, min_stars=?, min_reviews=?, max_price=?, "
            "pending_scan=1" + reset + push_sql + " WHERE id=? AND user_id=?",
            (query, name, min_stars, min_reviews, max_price, *push_args, query_id, user_id),
        )
        conn.commit()
        return True
    except Exception:
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def pop_pending_scans():
    """Queries waiting for an immediate scan as (id, url); the flag is cleared as they are taken."""
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT id, query FROM queries WHERE pending_scan=1 ORDER BY id").fetchall()
        if rows:
            conn.executemany("UPDATE queries SET pending_scan=0 WHERE id=?", [(r[0],) for r in rows])
            conn.commit()
        return rows
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def get_query_push(query_id):
    """Should new items of this query also be sent as push notifications?"""
    rows = _fetchall("SELECT push FROM queries WHERE id=?", (query_id,))
    return bool(rows and rows[0]["push"])


def add_push_subscription(user_id, endpoint, p256dh, auth, user_agent=""):
    import time

    # The same browser re-subscribing (or another account signing in on it) replaces the row
    _execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, "
        "p256dh=excluded.p256dh, auth=excluded.auth, user_agent=excluded.user_agent",
        (user_id, endpoint, p256dh, auth, (user_agent or "")[:300], int(time.time())),
    )


def get_push_subscriptions(user_id):
    rows = _fetchall(
        "SELECT id, endpoint, p256dh, auth, user_agent, created_at FROM push_subscriptions WHERE user_id=? ORDER BY id",
        (user_id,),
    )
    return [dict(r) for r in rows]


def remove_push_subscription(endpoint, user_id=None):
    if user_id is None:
        _execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    else:
        _execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (endpoint, user_id))


def add_to_allowlist(country, user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO allowlist (country, user_id) VALUES (?, ?)", (country, user_id))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_from_allowlist(country, user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM allowlist WHERE country=? AND user_id=?", (country, user_id))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_allowlist(user_id):
    """The user's allowed countries, or 0 when the allowlist is empty (everything allowed)."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT country FROM allowlist WHERE user_id=?", (user_id,)).fetchall()
        countries = [row[0] for row in rows]
        return countries or 0
    finally:
        if conn:
            conn.close()


def clear_allowlist(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM allowlist WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_parameter(key):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM parameters WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def set_parameter(key, value):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE parameters SET value=? WHERE key=?", (value, key))
        if cursor.rowcount == 0:
            # Parameter not present yet (e.g. added after the DB was created)
            cursor.execute(
                "INSERT INTO parameters (key, value) VALUES (?, ?)", (key, value)
            )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_all_parameters():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM parameters")
        return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()


# A listing counts as a deal when it is at least 20% under the average price of what the same
# query has collected so far, and only once there is enough history to make that meaningful.
DEAL_RATIO = 0.8
DEAL_MIN_HISTORY = 10

ITEM_SORTS = {
    "newest": "i.timestamp DESC",
    "oldest": "i.timestamp ASC",
    "price_asc": "i.price ASC",
    "price_desc": "i.price DESC",
    "stars_desc": "i.seller_stars DESC",
    "reviews_desc": "i.seller_reviews DESC",
}


def _escape_like(value):
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clear_items(user_id, query_id=None):
    """
    Delete stored items, either all of them or only those of one query.

    The affected queries also get their "last item" watermark reset, so the next scrape
    is treated as a first run (existing listings are recorded silently instead of
    flooding the notification channels).

    Returns:
        int: number of deleted items.
    """
    conn = None
    try:
        conn = get_db_connection()
        if query_id:
            deleted = conn.execute(
                "DELETE FROM items WHERE query_id=? AND user_id=?", (query_id, user_id)
            ).rowcount
            conn.execute(
                "UPDATE queries SET last_item=NULL WHERE id=? AND user_id=?", (query_id, user_id)
            )
        else:
            deleted = conn.execute("DELETE FROM items WHERE user_id=?", (user_id,)).rowcount
            conn.execute("UPDATE queries SET last_item=NULL WHERE user_id=?", (user_id,))
        conn.commit()
        return deleted
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def search_items(
    user_id,
    limit=50,
    query_id=None,
    text=None,
    seller=None,
    min_price=None,
    max_price=None,
    min_stars=None,
    min_reviews=None,
    since=None,
    sort="newest",
    offset=0,
    deals_only=False,
):
    """
    Search stored items with optional filters.

    Returns:
        tuple: (rows, total_matching) where each row is
        (item, title, price, currency, timestamp, query_url, photo_url, query_name,
         seller_login, seller_stars, seller_reviews, query_avg_price, query_history_size)
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        where, params = ["i.user_id = ?"], [user_id]
        if query_id:
            where.append("i.query_id = ?")
            params.append(query_id)
        if text:
            where.append("i.title LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(text)}%")
        if seller:
            where.append("i.seller_login LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(seller)}%")
        if min_price is not None:
            where.append("i.price >= ?")
            params.append(min_price)
        if max_price is not None:
            where.append("i.price <= ?")
            params.append(max_price)
        if min_stars is not None:
            where.append("i.seller_stars >= ?")
            params.append(min_stars)
        if min_reviews is not None:
            where.append("i.seller_reviews >= ?")
            params.append(min_reviews)
        if since is not None:
            where.append("i.timestamp >= ?")
            params.append(since)
        if deals_only:
            where.append("s.n >= ? AND i.price <= s.avg_price * ?")
            params.extend([DEAL_MIN_HISTORY, DEAL_RATIO])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = ITEM_SORTS.get(sort, ITEM_SORTS["newest"])
        base = (
            "FROM items i JOIN queries q ON i.query_id = q.id "
            "JOIN (SELECT query_id, AVG(price) AS avg_price, COUNT(*) AS n FROM items "
            "GROUP BY query_id) s ON s.query_id = i.query_id" + where_sql
        )
        total = conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        rows = conn.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, "
            "i.photo_url, q.query_name, i.seller_login, i.seller_stars, i.seller_reviews, "
            "s.avg_price, s.n "
            + base
            + f" ORDER BY {order_sql}, i.timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return rows, total
    except Exception:
        print_exc()
        return [], 0
    finally:
        if conn:
            conn.close()


def get_total_items_count(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        return conn.execute("SELECT COUNT(*) FROM items WHERE user_id=?", (user_id,)).fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_total_queries_count(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        return conn.execute("SELECT COUNT(*) FROM queries WHERE user_id=?", (user_id,)).fetchone()[0]
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_items_per_day(user_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        total_items, first, last = conn.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM items WHERE user_id=?", (user_id,)
        ).fetchone()
        if not total_items:
            return 0
        import datetime

        days = (
            datetime.datetime.fromtimestamp(last).date()
            - datetime.datetime.fromtimestamp(first).date()
        ).days + 1
        return round(total_items / max(1, days), 1)
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------------------
# Accounts and per-user settings
# ---------------------------------------------------------------------------------------

# Settings every user owns. When a user has not set one, the server-wide parameter of the
# same name (edited by an administrator) is the default.
USER_SETTING_KEYS = (
    "language",
    "timezone",
    "items_autorefresh",
    "new_items_notice",
    "banwords",
    "message_template",
    "telegram_enabled",
    "telegram_token",
    "telegram_chat_id",
    "rss_enabled",
    "feed_token",
)


def _fetchall(sql, params=()):
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    except Exception:
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def _execute(sql, params=()):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.lastrowid, cursor.rowcount
    except Exception:
        print_exc()
        return None, 0
    finally:
        if conn:
            conn.close()


def count_users():
    rows = _fetchall("SELECT COUNT(*) AS n FROM users")
    return rows[0]["n"] if rows else 0


def get_user(user_id):
    rows = _fetchall("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(rows[0]) if rows else None


def get_user_by_name(username):
    rows = _fetchall("SELECT * FROM users WHERE username=?", (username,))
    return dict(rows[0]) if rows else None


def list_users():
    """Users with how much data each owns."""
    rows = _fetchall(
        "SELECT u.*, "
        "(SELECT COUNT(*) FROM queries q WHERE q.user_id = u.id) AS query_count, "
        "(SELECT COUNT(*) FROM items i WHERE i.user_id = u.id) AS item_count "
        "FROM users u ORDER BY u.id"
    )
    return [dict(row) for row in rows]


def _base_url(query_url):
    from urllib.parse import urlparse

    parsed = urlparse(query_url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


def get_item_for_user(item_id, user_id):
    """One collected item of an account (by Vinted's listing id), or None."""
    rows = _fetchall(
        "SELECT i.item, i.title, i.price, i.currency, i.timestamp, i.photo_url, i.seller_login, i.seller_stars, "
        "i.seller_reviews, i.checked_at, i.available, q.query_name, q.query "
        "FROM items i LEFT JOIN queries q ON q.id = i.query_id "
        "WHERE i.item=? AND i.user_id=? ORDER BY i.timestamp DESC LIMIT 1",
        (item_id, user_id),
    )
    if not rows:
        return None
    row = dict(rows[0])
    row["query_name"] = row.get("query_name") or ""
    row["base_url"] = _base_url(row.pop("query", None))
    row["url"] = f"{row['base_url']}/items/{int(row['item'])}" if row["base_url"] else ""
    return row


def delete_item(item_id, user_id):
    """Remove one item from an account's collection. It is remembered so it is never announced again."""
    import time

    conn = None
    try:
        conn = get_db_connection()
        deleted = conn.execute("DELETE FROM items WHERE item=? AND user_id=?", (item_id, user_id)).rowcount
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO removed_items (item, user_id, removed_at) VALUES (?, ?, ?)",
            (item_id, user_id, now),
        )
        conn.execute("DELETE FROM removed_items WHERE removed_at < ?", (now - 90 * 86400,))
        conn.commit()
        return deleted
    except Exception:
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def mark_item_checked(item_id, user_id, available):
    """Note that an item's availability was just verified (available: 1, 0 = seen sold, None = unknown)."""
    import time

    conn = None
    try:
        conn = get_db_connection()
        if available is None:
            conn.execute("UPDATE items SET checked_at=? WHERE item=? AND user_id=?", (int(time.time()), item_id, user_id))
        else:
            conn.execute(
                "UPDATE items SET checked_at=?, available=? WHERE item=? AND user_id=?",
                (int(time.time()), available, item_id, user_id),
            )
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def items_due_for_check(user_id, checked_before, limit):
    """The account's items never checked, or last checked before `checked_before`, oldest first."""
    rows = _fetchall(
        "SELECT i.item, i.available, i.checked_at, q.query FROM items i LEFT JOIN queries q ON q.id = i.query_id "
        "WHERE i.user_id=? AND (i.checked_at IS NULL OR i.checked_at < ?) "
        "ORDER BY i.checked_at IS NOT NULL, i.checked_at ASC LIMIT ?",
        (user_id, checked_before, limit),
    )
    out = []
    for r in rows:
        base = _base_url(r["query"])
        if base:
            out.append({"item": r["item"], "available": r["available"], "checked_at": r["checked_at"], "base_url": base})
    return out


def users_with_items():
    """Accounts that have collected items (the ones whose listings are verified in the background)."""
    rows = _fetchall("SELECT DISTINCT user_id FROM items")
    return [r["user_id"] for r in rows]


def get_recent_items(user_id, limit=5):
    """The newest items collected for an account, with the query they came from."""
    rows = _fetchall(
        "SELECT i.title, i.price, i.currency, i.timestamp, i.seller_login, q.query_name, q.query "
        "FROM items i LEFT JOIN queries q ON q.id = i.query_id WHERE i.user_id=? "
        "ORDER BY i.timestamp DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


def create_user(username, password_hash, role="user"):
    """Returns the new user's id, or None if the name is taken."""
    import time

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, int(time.time())),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None
    except Exception:
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def update_user(user_id, **fields):
    """Update password_hash, role or disabled."""
    allowed = {"password_hash", "role", "disabled"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE users SET {assignments} WHERE id=?", (*fields.values(), user_id))


def count_admins(exclude_id=None):
    rows = _fetchall(
        "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND id != ?",
        (exclude_id or -1,),
    )
    return rows[0]["n"] if rows else 0


def delete_user(user_id):
    """Remove an account together with its queries, items, allowlist and settings."""
    conn = None
    try:
        conn = get_db_connection()
        for table in ("items", "queries", "allowlist", "user_settings", "push_subscriptions"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def adopt_unowned_data(user_id):
    """
    Give everything created before accounts existed (owner 0) to `user_id`: queries, items,
    allowlist and the settings that were being used.
    """
    conn = None
    try:
        conn = get_db_connection()
        for table in ("queries", "items", "allowlist", "user_settings"):
            conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id=0", (user_id,))
        conn.commit()
    except Exception:
        print_exc()
    finally:
        if conn:
            conn.close()


def get_user_setting(user_id, key, default=None):
    """A user's setting, falling back to the server-wide parameter and then `default`."""
    rows = _fetchall("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key))
    if rows and rows[0]["value"] is not None:
        return rows[0]["value"]
    value = get_parameter(key)
    return value if value is not None else default


def set_user_setting(user_id, key, value):
    _execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, key, value),
    )


def get_user_settings(user_id):
    """All per-user settings with defaults applied."""
    return {key: get_user_setting(user_id, key) for key in USER_SETTING_KEYS}


def active_user_ids():
    """Ids whose queries and notifications are processed: enabled accounts, or 0 before any exist."""
    if count_users() == 0:
        return [0]
    return [row["id"] for row in _fetchall("SELECT id FROM users WHERE disabled=0 ORDER BY id")]


def users_with_setting(key, value="True"):
    """Active users for which a (defaulted) setting equals `value`."""
    return [uid for uid in active_user_ids() if get_user_setting(uid, key) == value]


def get_feed_token(user_id):
    """The user's private RSS feed token, created on first use."""
    import secrets

    token = get_user_setting(user_id, "feed_token")
    if not token:
        token = secrets.token_urlsafe(24)
        set_user_setting(user_id, "feed_token", token)
    return token


def get_user_id_by_feed_token(token):
    if not token:
        return None
    rows = _fetchall(
        "SELECT user_id FROM user_settings WHERE key='feed_token' AND value=?", (token,)
    )
    if not rows:
        return None
    user_id = rows[0]["user_id"]
    if user_id in active_user_ids():
        return user_id
    return None
