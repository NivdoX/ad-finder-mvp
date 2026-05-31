import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import psycopg2
import stripe
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import resend
from flask import (
    abort,
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from openai import OpenAI
from werkzeug.security import check_password_hash, generate_password_hash

from services.ad_relevance import AdRelevanceFilter
from services.meta_ads import MetaAdsService, MetaAdsServiceError
from seo_brands import BRAND_PAGES, get_brand_by_slug, get_related_brands

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "").strip()

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip() or "https://getrunningads.com"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        send_default_pii=True,
    )

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_BASIC_PRICE_ID = os.getenv("STRIPE_BASIC_PRICE_ID", "").strip()
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "").strip()
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "").strip()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower() or "contact@nivdox.com"

FREE_PREVIEW_COUNT = 2
CACHE_TTL_HOURS = 48
FRESH_SEARCH_ESTIMATED_COST = 0.03
CACHED_SEARCH_ESTIMATED_COST = 0.0
RATE_LIMIT_PER_MINUTE = 12
ABUSE_BLOCK_AFTER = 18
COST_PER_SEARCH_ALERT_THRESHOLD = 0.08
CACHE_RATE_ALERT_THRESHOLD = 40.0
COST_SPIKE_THRESHOLD_PERCENT = 50.0

PLAN_LIMITS = {
    "basic": {"monthly": 40, "daily": 5},
    "pro": {"monthly": 90, "daily": 15},
}

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def absolute_url(path: str = "/") -> str:
    clean_base = APP_BASE_URL.rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    if clean_path == "/":
        return f"{clean_base}/"
    return f"{clean_base}{clean_path}"


def utcnow():
    return datetime.now(timezone.utc)


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("Missing DATABASE_URL")
    return psycopg2.connect(DATABASE_URL)


def ensure_schema():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT,
                        stripe_customer_id TEXT UNIQUE,
                        stripe_subscription_id TEXT UNIQUE,
                        subscription_status TEXT DEFAULT 'inactive',
                        is_paid BOOLEAN DEFAULT FALSE,
                        plan TEXT DEFAULT 'free',
                        role TEXT DEFAULT 'user',
                        full_access BOOLEAN DEFAULT FALSE,
                        reset_token TEXT,
                        reset_token_expires TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMPTZ")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS searches (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        anon_id TEXT,
                        email TEXT,
                        search_query TEXT NOT NULL,
                        query_normalized TEXT NOT NULL,
                        plan TEXT NOT NULL,
                        is_cached BOOLEAN DEFAULT FALSE,
                        estimated_cost NUMERIC(10,4) DEFAULT 0,
                        result_count INTEGER DEFAULT 0,
                        counts_toward_limit BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cached_searches (
                        id SERIAL PRIMARY KEY,
                        query_normalized TEXT UNIQUE NOT NULL,
                        query_original TEXT NOT NULL,
                        results_json TEXT NOT NULL,
                        result_count INTEGER DEFAULT 0,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS feedback (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        anon_id TEXT,
                        email TEXT,
                        search_query TEXT,
                        feedback_type TEXT NOT NULL,
                        optional_text TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS request_logs (
                        id SERIAL PRIMARY KEY,
                        identifier TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alerts (
                        id SERIAL PRIMARY KEY,
                        alert_key TEXT UNIQUE NOT NULL,
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'inactive';")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_paid BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free';")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user';")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_access BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS anon_id TEXT;")
                cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS counts_toward_limit BOOLEAN DEFAULT TRUE;")

                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_searches_user_created_at
                    ON searches(user_id, created_at);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_searches_anon_created_at
                    ON searches(anon_id, created_at);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_request_logs_identifier_created_at
                    ON request_logs(identifier, created_at);
                    """
                )

                cur.execute(
                    """
                    INSERT INTO users (email, role, full_access, is_paid, plan, subscription_status)
                    VALUES (%s, 'admin', TRUE, TRUE, 'admin', 'active')
                    ON CONFLICT (email)
                    DO UPDATE SET
                        role = 'admin',
                        full_access = TRUE,
                        is_paid = TRUE,
                        plan = 'admin',
                        subscription_status = 'active',
                        updated_at = NOW();
                    """,
                    (ADMIN_EMAIL,),
                )
    finally:
        conn.close()


def get_meta_ads_service():
    return MetaAdsService(
        apify_token=APIFY_TOKEN,
        apify_actor_id=APIFY_ACTOR_ID,
    )


def get_ad_relevance_filter():
    return AdRelevanceFilter(
        api_key=OPENAI_API_KEY,
        model=OPENAI_MODEL,
    )


def get_openai_client():
    if not OPENAI_API_KEY:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def normalize_query(value: str) -> str:
    return " ".join((value or "").lower().strip().split())


def ensure_anon_id():
    if "anon_id" not in session:
        session["anon_id"] = f"anon_{secrets.token_hex(12)}"
    return session["anon_id"]


def get_user_by_email(email: str):
    if not email:
        return None
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, stripe_customer_id, stripe_subscription_id,
                           subscription_status, is_paid, plan, role, full_access
                    FROM users
                    WHERE email = %s
                    """,
                    (email.lower().strip(),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "email": row[1],
                    "password_hash": row[2],
                    "stripe_customer_id": row[3],
                    "stripe_subscription_id": row[4],
                    "subscription_status": row[5] or "inactive",
                    "is_paid": bool(row[6]),
                    "plan": row[7] or "free",
                    "role": row[8] or "user",
                    "full_access": bool(row[9]),
                }
    finally:
        conn.close()

def send_reset_email(email: str, reset_link: str):
    if not RESEND_API_KEY:
        return

    resend.api_key = RESEND_API_KEY

    resend.Emails.send({
        "from": "RunningAds <contact@nivdox.com>",
        "to": [email],
        "subject": "Reset your RunningAds password",
        "html": f"""
        <div style="font-family:Arial,sans-serif;padding:20px;">
            <h2>Reset your password</h2>
            <p>Click the button below to reset your password.</p>
            <p>
                <a href="{reset_link}" style="background:#22c55e;color:white;padding:12px 20px;text-decoration:none;border-radius:8px;">
                    Reset password
                </a>
            </p>
            <p>If you did not request this, you can ignore this email.</p>
        </div>
        """
    })
def save_reset_token(user_id: int, token: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET reset_token = %s,
                        reset_token_expires = NOW() + INTERVAL '1 hour'
                    WHERE id = %s
                    """,
                    (token, user_id),
                )
    finally:
        conn.close()


def get_user_by_reset_token(token: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email
                    FROM users
                    WHERE reset_token = %s
                    AND reset_token_expires > NOW()
                    """,
                    (token,),
                )

                row = cur.fetchone()

                if not row:
                    return None

                return {
                    "id": row[0],
                    "email": row[1],
                }
    finally:
        conn.close()
def get_user_by_id(user_id: int):
    if not user_id:
        return None
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, stripe_customer_id, stripe_subscription_id,
                           subscription_status, is_paid, plan, role, full_access
                    FROM users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "email": row[1],
                    "password_hash": row[2],
                    "stripe_customer_id": row[3],
                    "stripe_subscription_id": row[4],
                    "subscription_status": row[5] or "inactive",
                    "is_paid": bool(row[6]),
                    "plan": row[7] or "free",
                    "role": row[8] or "user",
                    "full_access": bool(row[9]),
                }
    finally:
        conn.close()


def create_free_user(email: str, password: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash, plan, subscription_status, is_paid, role, full_access)
                    VALUES (%s, %s, 'free', 'inactive', FALSE, 'user', FALSE)
                    RETURNING id
                    """,
                    (email.lower().strip(), generate_password_hash(password)),
                )
                new_id = cur.fetchone()[0]
                return new_id
    finally:
        conn.close()


def set_password_for_existing_user(user_id: int, password: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET password_hash = %s,
                    reset_token = NULL,
                    reset_token_expires = NULL,
                    updated_at = NOW()
                    WHERE id = %s
                    """,
                    (generate_password_hash(password), user_id),
                )
    finally:
        conn.close()


def log_user_in(user):
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]


def log_user_out():
    session.pop("user_id", None)
    session.pop("user_email", None)


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def is_admin(user) -> bool:
    return bool(user and (user["full_access"] or user["role"] == "admin"))


def plan_limits(plan: str):
    if plan in PLAN_LIMITS:
        return PLAN_LIMITS[plan]
    return {"monthly": None, "daily": None}


def get_current_month_range():
    now = utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return month_start, next_month


def get_today_range():
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)
    return day_start, next_day


def get_usage_counts_for_user(user_id: int):
    month_start, next_month = get_current_month_range()
    day_start, next_day = get_today_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM searches
                    WHERE user_id = %s
                    AND counts_toward_limit = TRUE
                    AND created_at >= %s
                    AND created_at < %s
                    """,
                    (user_id, month_start, next_month),
                )
                monthly_count = int(cur.fetchone()[0])

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM searches
                    WHERE user_id = %s
                    AND counts_toward_limit = TRUE
                    AND created_at >= %s
                    AND created_at < %s
                    """,
                    (user_id, day_start, next_day),
                )
                daily_count = int(cur.fetchone()[0])

                return {
                    "monthly_count": monthly_count,
                    "daily_count": daily_count,
                }
    finally:
        conn.close()


def get_total_free_searches_for_anon(anon_id: str) -> int:
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM searches
                    WHERE anon_id = %s
                    AND counts_toward_limit = TRUE
                    """,
                    (anon_id,),
                )
                return int(cur.fetchone()[0])
    finally:
        conn.close()


def get_zero_result_freebies_today(user_id: int) -> int:
    if not user_id:
        return 0
    day_start, next_day = get_today_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM searches
                    WHERE user_id = %s
                    AND result_count = 0
                    AND counts_toward_limit = FALSE
                    AND created_at >= %s
                    AND created_at < %s
                    """,
                    (user_id, day_start, next_day),
                )
                return int(cur.fetchone()[0] or 0)
    finally:
        conn.close()


def get_usage_message(user):
    if not user:
        return None
    if is_admin(user):
        return None
    if not user["is_paid"] or user["plan"] not in PLAN_LIMITS:
        return None

    counts = get_usage_counts_for_user(user["id"])
    limits = plan_limits(user["plan"])
    monthly_limit = limits["monthly"]
    if not monthly_limit:
        return None

    pct = (counts["monthly_count"] / monthly_limit) * 100 if monthly_limit else 0
    if pct >= 80:
        return "You’ve used 80% of your searches this month"
    return None


def can_user_search(user):
    if not user:
        return {"allowed": False, "reason": "login"}
    if is_admin(user):
        return {"allowed": True, "reason": None}
    if not user["is_paid"] or user["plan"] not in PLAN_LIMITS:
        return {"allowed": False, "reason": "upgrade"}

    limits = plan_limits(user["plan"])
    counts = get_usage_counts_for_user(user["id"])

    if counts["monthly_count"] >= limits["monthly"]:
        return {
            "allowed": False,
            "reason": "monthly_limit",
            "message": "You’ve reached your monthly limit. Upgrade to continue finding winning ads.",
        }

    if counts["daily_count"] >= limits["daily"]:
        if user["plan"] == "pro":
            message = "You’ve reached today’s Pro limit. Your searches reset tomorrow."
        elif user["plan"] == "basic":
            message = "You’ve reached your daily Basic limit. Upgrade to Pro to continue finding winning ads."
        else:
            message = "You’ve reached your daily limit."

        return {
            "allowed": False,
            "reason": "daily_limit",
            "message": message,
        }

    return {"allowed": True, "reason": None}


def record_search(
    user_id,
    anon_id,
    email,
    search_query,
    plan,
    is_cached,
    estimated_cost,
    result_count,
    counts_toward_limit=True,
):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO searches (
                        user_id, anon_id, email, search_query, query_normalized, plan,
                        is_cached, estimated_cost, result_count, counts_toward_limit
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        anon_id,
                        email,
                        search_query,
                        normalize_query(search_query),
                        plan,
                        is_cached,
                        estimated_cost,
                        result_count,
                        counts_toward_limit,
                    ),
                )
    finally:
        conn.close()


def get_cached_results(normalized_query: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT results_json
                    FROM cached_searches
                    WHERE query_normalized = %s
                    AND expires_at > NOW()
                    """,
                    (normalized_query,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return json.loads(row[0])
    finally:
        conn.close()


def save_cached_results(query_original: str, normalized_query: str, results: list):
    expires_at = utcnow() + timedelta(hours=CACHE_TTL_HOURS)
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cached_searches (
                        query_normalized, query_original, results_json, result_count, expires_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (query_normalized)
                    DO UPDATE SET
                        query_original = EXCLUDED.query_original,
                        results_json = EXCLUDED.results_json,
                        result_count = EXCLUDED.result_count,
                        created_at = NOW(),
                        expires_at = EXCLUDED.expires_at
                    """,
                    (
                        normalized_query,
                        query_original,
                        json.dumps(results),
                        len(results),
                        expires_at,
                    ),
                )
    finally:
        conn.close()


def create_alert(alert_key: str, alert_type: str, severity: str, message: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alerts (alert_key, alert_type, severity, message)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alert_key) DO NOTHING
                    """,
                    (alert_key, alert_type, severity, message),
                )
    finally:
        conn.close()


def get_monitoring_stats():
    day_start, next_day = get_today_range()
    month_start, next_month = get_current_month_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total_searches,
                        COALESCE(SUM(estimated_cost), 0),
                        AVG(CASE WHEN is_cached THEN 1 ELSE 0 END)::float
                    FROM searches
                    WHERE created_at >= %s AND created_at < %s
                    """,
                    (day_start, next_day),
                )
                day_row = cur.fetchone()
                day_searches = int(day_row[0] or 0)
                day_cost = float(day_row[1] or 0)
                day_cache_rate = float(day_row[2] or 0) * 100 if day_row[2] is not None else 0.0
                day_cps = (day_cost / day_searches) if day_searches else 0.0

                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total_searches,
                        COALESCE(SUM(estimated_cost), 0),
                        AVG(CASE WHEN is_cached THEN 1 ELSE 0 END)::float
                    FROM searches
                    WHERE created_at >= %s AND created_at < %s
                    """,
                    (month_start, next_month),
                )
                month_row = cur.fetchone()
                month_searches = int(month_row[0] or 0)
                month_cost = float(month_row[1] or 0)
                month_cache_rate = float(month_row[2] or 0) * 100 if month_row[2] is not None else 0.0
                month_cps = (month_cost / month_searches) if month_searches else 0.0

                yesterday_start = day_start - timedelta(days=1)
                cur.execute(
                    """
                    SELECT COALESCE(SUM(estimated_cost), 0)
                    FROM searches
                    WHERE created_at >= %s AND created_at < %s
                    """,
                    (yesterday_start, day_start),
                )
                yesterday_cost = float(cur.fetchone()[0] or 0)

                cur.execute(
                    """
                    SELECT COALESCE(email, anon_id, 'unknown') AS actor,
                           COUNT(*) AS total_searches
                    FROM searches
                    WHERE created_at >= %s AND created_at < %s
                    GROUP BY actor
                    ORDER BY total_searches DESC
                    LIMIT 10
                    """,
                    (month_start, next_month),
                )
                top_users = cur.fetchall()

                return {
                    "day_searches": day_searches,
                    "day_cost": day_cost,
                    "day_cache_rate": day_cache_rate,
                    "day_cps": day_cps,
                    "month_searches": month_searches,
                    "month_cost": month_cost,
                    "month_cache_rate": month_cache_rate,
                    "month_cps": month_cps,
                    "yesterday_cost": yesterday_cost,
                    "top_users": top_users,
                }
    finally:
        conn.close()


def evaluate_alerts():
    stats = get_monitoring_stats()
    today_key = utcnow().strftime("%Y-%m-%d")

    if stats["day_searches"] > 0 and stats["day_cps"] > COST_PER_SEARCH_ALERT_THRESHOLD:
        create_alert(
            f"cps:{today_key}",
            "cost_per_search",
            "warning",
            f"Cost per search is ${stats['day_cps']:.2f}, above ${COST_PER_SEARCH_ALERT_THRESHOLD:.2f}.",
        )

    if stats["day_searches"] > 0 and stats["day_cache_rate"] < CACHE_RATE_ALERT_THRESHOLD:
        create_alert(
            f"cache:{today_key}",
            "cache_rate",
            "warning",
            f"Cache rate is {stats['day_cache_rate']:.1f}%, below {CACHE_RATE_ALERT_THRESHOLD:.0f}%.",
        )

    if stats["yesterday_cost"] > 0:
        increase_pct = ((stats["day_cost"] - stats["yesterday_cost"]) / stats["yesterday_cost"]) * 100
        if increase_pct > COST_SPIKE_THRESHOLD_PERCENT:
            create_alert(
                f"spike:{today_key}",
                "cost_spike",
                "warning",
                f"Total cost is up {increase_pct:.1f}% day over day.",
            )


def create_daily_cap_alert(user):
    if not user:
        return
    today_key = utcnow().strftime("%Y-%m-%d")
    create_alert(
        f"dailycap:{user['id']}:{today_key}",
        "daily_cap_attempt",
        "info",
        f"{user['email']} tried to search after hitting the daily cap.",
    )


def check_rate_limit(identifier: str, endpoint: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO request_logs (identifier, endpoint)
                    VALUES (%s, %s)
                    """,
                    (identifier, endpoint),
                )

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM request_logs
                    WHERE identifier = %s
                    AND created_at >= NOW() - INTERVAL '1 minute'
                    """,
                    (identifier,),
                )
                count = int(cur.fetchone()[0] or 0)

                if count > RATE_LIMIT_PER_MINUTE:
                    time.sleep(1)
                if count > ABUSE_BLOCK_AFTER:
                    return False
                return True
    finally:
        conn.close()


def get_request_identifier(user):
    if user:
        return f"user:{user['id']}"
    anon_id = ensure_anon_id()
    return f"anon:{anon_id}"


def login_required(view_func):
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    wrapped.__name__ = view_func.__name__
    return wrapped


def admin_required(view_func):
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not is_admin(user):
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)

    wrapped.__name__ = view_func.__name__
    return wrapped


@app.context_processor
def inject_globals():
    return {
        "current_user": get_current_user(),
        "year_now": utcnow().year,
    }


def get_plan_from_price_id(price_id: str):
    if price_id == STRIPE_BASIC_PRICE_ID:
        return "basic"
    if price_id == STRIPE_PRO_PRICE_ID:
        return "pro"
    return "free"


@app.route("/", methods=["GET", "POST"])
def index():
    ensure_anon_id()
    user = get_current_user()
    brand = (request.args.get("brand") or "").strip() if request.method == "GET" else ""
    error = None
    searched = False
    blocked = False
    preview_results = []
    shown_count = 0
    usage_message = get_usage_message(user)
    blocked_message = None

    if request.method == "POST":
        brand = (request.form.get("brand") or "").strip()
        identifier = get_request_identifier(user)

        if not check_rate_limit(identifier, "search"):
            error = "Too many requests. Please wait a moment and try again."
            return render_template(
                "index.html",
                brand=brand,
                error=error,
                searched=False,
                blocked=False,
                preview_results=[],
                shown_count=0,
                usage_message=usage_message,
                blocked_message=None,
            )

        if not brand:
            error = "Please enter a brand name."
            return render_template(
                "index.html",
                brand=brand,
                error=error,
                searched=False,
                blocked=False,
                preview_results=[],
                shown_count=0,
                usage_message=usage_message,
                blocked_message=None,
            )

        if user:
            access = can_user_search(user)
            if not access["allowed"]:
                blocked = True
                if access["reason"] == "daily_limit":
                    create_daily_cap_alert(user)
                blocked_message = access.get("message") or "Upgrade to continue finding winning ads."
                return render_template(
                    "index.html",
                    brand=brand,
                    error=None,
                    searched=False,
                    blocked=blocked,
                    preview_results=[],
                    shown_count=0,
                    usage_message=get_usage_message(user),
                    blocked_message=blocked_message,
                )
        else:
            blocked = True
            blocked_message = "Create an account to search competitor ads."

            return render_template(
                "index.html",
                brand=brand,
                error=None,
                searched=False,
                blocked=blocked,
                preview_results=[],
                shown_count=0,
                usage_message=None,
                blocked_message=blocked_message,
            )

        searched = True
        normalized = normalize_query(brand)

        try:
            cached = get_cached_results(normalized)
            is_cached = cached is not None

            if is_cached:
                ads = cached
                estimated_cost = CACHED_SEARCH_ESTIMATED_COST
            else:
                service = get_meta_ads_service()
                ads = service.search_ads(
                    brand=brand,
                    country="NO",
                    max_results=50,
                )

                relevance_filter = get_ad_relevance_filter()
                ads = relevance_filter.filter_ads(
                    search_brand=brand,
                    ads=ads,
                )

                save_cached_results(brand, normalized, ads)
                estimated_cost = FRESH_SEARCH_ESTIMATED_COST

            if user and can_user_search(user)["allowed"]:
                preview_results = ads
                plan_name = user["plan"]
                user_id = user["id"]
                anon_id = None
                email = user["email"]
            else:
                preview_results = ads[:FREE_PREVIEW_COUNT]
                plan_name = "free"
                user_id = None
                anon_id = session["anon_id"]
                email = None

            shown_count = len(preview_results)

            counts_toward_limit = True
            zero_result_freebie = False

            if user_id and len(ads) == 0:
                free_zero_used = get_zero_result_freebies_today(user_id)
                if free_zero_used < 1:
                    counts_toward_limit = False
                    zero_result_freebie = True

            record_search(
                user_id=user_id,
                anon_id=anon_id,
                email=email,
                search_query=brand,
                plan=plan_name,
                is_cached=is_cached,
                estimated_cost=estimated_cost,
                result_count=len(ads),
                counts_toward_limit=counts_toward_limit,
            )

            if zero_result_freebie:
                blocked_message = "No ads found. This search was not counted."

            evaluate_alerts()
            usage_message = get_usage_message(get_current_user())

        except MetaAdsServiceError as exc:
            error = str(exc)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            error = "Something went wrong while searching for ads."

    return render_template(
        "index.html",
        brand=brand,
        error=error,
        searched=searched,
        blocked=blocked,
        preview_results=preview_results,
        shown_count=shown_count,
        usage_message=usage_message,
        blocked_message=blocked_message,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    next_url = request.args.get("next") or url_for("account")
    error = None

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        next_url = request.form.get("next") or url_for("account")
        identifier = f"register:{request.remote_addr or 'unknown'}"

        if not check_rate_limit(identifier, "register"):
            error = "Too many attempts. Please wait a moment and try again."
        elif not email or not password:
            error = "Please fill in all fields."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            existing = get_user_by_email(email)
            if existing and existing["password_hash"]:
                error = "An account with this email already exists. Please log in."
            elif existing and not existing["password_hash"]:
                set_password_for_existing_user(existing["id"], password)
                updated = get_user_by_email(email)
                log_user_in(updated)
                return redirect(next_url)
            else:
                create_free_user(email, password)
                user = get_user_by_email(email)
                log_user_in(user)
                return redirect(next_url)

    return render_template("register.html", error=error, next_url=next_url)

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = get_user_by_email(email)

        if user:
            token = secrets.token_urlsafe(32)
            reset_link = f"{APP_BASE_URL}/reset-password/{token}"
            save_reset_token(user["id"], token)
            send_reset_email(email, reset_link)

        message = "Check your inbox for a password reset link."

    return render_template("forgot_password.html", message=message)
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    error = None
    message = None

    user = get_user_by_reset_token(token)

    if not user:
        error = "Invalid or expired reset link."

    elif request.method == "POST":
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if len(password) < 8:
            error = "Password must be at least 8 characters."

        elif password != confirm_password:
            error = "Passwords do not match."

        else:
            set_password_for_existing_user(user["id"], password)
            return redirect(url_for("login"))

    return render_template(
        "reset_password.html",
        error=error,
        message=message,
    )
@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or url_for("account")
    error = None

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        next_url = request.form.get("next") or url_for("account")
        identifier = f"login:{request.remote_addr or 'unknown'}"

        if not check_rate_limit(identifier, "login"):
            error = "Too many attempts. Please wait a moment and try again."
        else:
            user = get_user_by_email(email)
            if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
                error = "Invalid email or password."
            else:
                log_user_in(user)
                return redirect(next_url)

    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout")
def logout():
    log_user_out()
    return redirect(url_for("index"))


@app.route("/pricing")
def pricing():
    user = get_current_user()
    usage_message = get_usage_message(user)
    return render_template("pricing.html", usage_message=usage_message)


@app.route("/create-checkout/<plan>", methods=["POST"])
@login_required
def create_checkout(plan):
    user = get_current_user()

    if not user:
        return redirect(url_for("login"))

    if user["is_paid"]:
        flash("You already have an active subscription. Manage your subscription from your account.")
        return redirect(url_for("account"))

    if plan not in ("basic", "pro"):
        return redirect(url_for("pricing"))

    price_id = STRIPE_BASIC_PRICE_ID if plan == "basic" else STRIPE_PRO_PRICE_ID

    if not price_id:
        flash("Missing Stripe price configuration.")
        return redirect(url_for("pricing"))

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user["email"],
            allow_promotion_codes=True,
            success_url=f"{APP_BASE_URL}/account?checkout=success",
            cancel_url=f"{APP_BASE_URL}/pricing?checkout=cancelled",
            automatic_tax={"enabled": True},
            metadata={
                "user_id": str(user["id"]),
                "plan": plan,
            },
        )
        return redirect(checkout_session.url, code=303)

    except Exception as exc:
        flash(f"Checkout error: {str(exc)}")
        return redirect(url_for("pricing"))


@app.route("/billing-portal", methods=["POST"])
@login_required
def billing_portal():
    user = get_current_user()
    if not user or not user["stripe_customer_id"]:
        flash("No Stripe customer found yet.")
        return redirect(url_for("account"))

    try:
        portal = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=f"{APP_BASE_URL}/account",
        )
        return redirect(portal.url, code=303)
    except Exception as exc:
        flash(f"Billing portal error: {str(exc)}")
        return redirect(url_for("account"))


@app.route("/account")
@login_required
def account():
    user = get_current_user()
    usage_message = get_usage_message(user)
    usage = None
    limits = None

    if user and user["plan"] in PLAN_LIMITS:
        usage = get_usage_counts_for_user(user["id"])
        limits = plan_limits(user["plan"])

    return render_template(
        "account.html",
        user=user,
        usage=usage,
        limits=limits,
        usage_message=usage_message,
    )


@app.route("/feedback", methods=["POST"])
def feedback():
    ensure_anon_id()
    user = get_current_user()
    identifier = get_request_identifier(user)

    if not check_rate_limit(identifier, "feedback"):
        return redirect(url_for("index"))

    feedback_type = (request.form.get("feedback_type") or "").strip()
    optional_text = (request.form.get("optional_text") or "").strip()
    search_query = (request.form.get("search_query") or "").strip()

    if not feedback_type:
        return redirect(url_for("index"))

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feedback (user_id, anon_id, email, search_query, feedback_type, optional_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user["id"] if user else None,
                        None if user else session["anon_id"],
                        user["email"] if user else None,
                        search_query or None,
                        feedback_type,
                        optional_text or None,
                    ),
                )
    finally:
        conn.close()

    flash("Thanks, your feedback was saved.")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin():
    stats = get_monitoring_stats()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT created_at, alert_type, severity, message
                    FROM alerts
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                alerts = cur.fetchall()

                cur.execute(
                    """
                    SELECT created_at, email, search_query, feedback_type, optional_text
                    FROM feedback
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                feedback_rows = cur.fetchall()
    finally:
        conn.close()

    ai_summary = None
    if request.args.get("summary") == "1" and OPENAI_API_KEY:
        client = get_openai_client()
        if client:
            prompt = f"""
You are summarizing SaaS monitoring data for the founder.
Daily searches: {stats['day_searches']}
Daily cost: {stats['day_cost']:.2f}
Daily cost per search: {stats['day_cps']:.2f}
Daily cache rate: {stats['day_cache_rate']:.1f}%
Monthly searches: {stats['month_searches']}
Monthly cost: {stats['month_cost']:.2f}
Monthly cost per search: {stats['month_cps']:.2f}
Monthly cache rate: {stats['month_cache_rate']:.1f}%
Top users:
{stats['top_users']}
Give a short weekly-style summary:
1. what cost the most
2. which users seem heaviest
3. whether cost per search is increasing
4. one concrete recommendation
""".strip()
            try:
                response = client.responses.create(
                    model=OPENAI_MODEL,
                    input=prompt,
                )
                ai_summary = response.output_text
            except Exception as exc:
                ai_summary = f"AI summary error: {str(exc)}"

    return render_template(
        "admin.html",
        stats=stats,
        alerts=alerts,
        feedback_rows=feedback_rows,
        ai_summary=ai_summary,
    )


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Missing STRIPE_WEBHOOK_SECRET"}), 500

    try:
        stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
        raw_event = json.loads(payload.decode("utf-8"))
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as exc:
        print("Webhook error:", str(exc))
        return jsonify({"error": "Webhook error"}), 400

    event_type = raw_event["type"]
    data = raw_event["data"]["object"]

    if event_type == "checkout.session.completed":
        email = (
            data.get("customer_email")
            or (data.get("customer_details") or {}).get("email")
            or ""
        ).strip().lower()
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("subscription")
        plan = (data.get("metadata") or {}).get("plan") or "free"

        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users (
                            email, stripe_customer_id, stripe_subscription_id, subscription_status,
                            is_paid, plan, role, full_access, updated_at
                        )
                        VALUES (%s, %s, %s, 'active', TRUE, %s, 'user', FALSE, NOW())
                        ON CONFLICT (email)
                        DO UPDATE SET
                            stripe_customer_id = EXCLUDED.stripe_customer_id,
                            stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                            subscription_status = 'active',
                            is_paid = TRUE,
                            plan = EXCLUDED.plan,
                            updated_at = NOW();
                        """,
                        (email, stripe_customer_id, stripe_subscription_id, plan),
                    )
        finally:
            conn.close()

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        stripe_customer_id = data.get("customer")
        stripe_subscription_id = data.get("id")
        status = data.get("status") or "inactive"
        items = data.get("items", {}).get("data", [])
        price_id = None

        if items:
            price_id = (((items[0] or {}).get("price")) or {}).get("id")

        plan = get_plan_from_price_id(price_id)
        is_paid = status in ("active", "trialing")

        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET stripe_customer_id = COALESCE(%s, stripe_customer_id),
                            stripe_subscription_id = COALESCE(%s, stripe_subscription_id),
                            subscription_status = %s,
                            is_paid = %s,
                            plan = %s,
                            updated_at = NOW()
                        WHERE stripe_customer_id = %s
                           OR stripe_subscription_id = %s
                        """,
                        (
                            stripe_customer_id,
                            stripe_subscription_id,
                            status,
                            is_paid,
                            plan if is_paid else "free",
                            stripe_customer_id,
                            stripe_subscription_id,
                        ),
                    )
        finally:
            conn.close()

    elif event_type == "invoice.payment_failed":
        stripe_customer_id = data.get("customer")

        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET subscription_status = 'past_due',
                            is_paid = FALSE,
                            plan = 'free',
                            updated_at = NOW()
                        WHERE stripe_customer_id = %s
                        """,
                        (stripe_customer_id,),
                    )
        finally:
            conn.close()

    return jsonify({"received": True}), 200


ensure_schema()
@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/brand/<brand_slug>")
def brand_page(brand_slug):
    brand = get_brand_by_slug(brand_slug)
    if not brand:
        abort(404)

    return render_template(
        "brand.html",
        brand=brand,
        related_brands=get_related_brands(brand),
        canonical_url=absolute_url(f"/brand/{brand['slug']}"),
    )

@app.route("/robots.txt")
def robots_txt():
    return send_from_directory(".", "robots.txt")

@app.route("/sitemap.xml")
def sitemap_xml():
    pages = [
        {
            "loc": absolute_url("/"),
            "priority": "1.0",
            "changefreq": "weekly",
        },
        {
            "loc": absolute_url("/pricing"),
            "priority": "0.8",
            "changefreq": "weekly",
        },
        {
            "loc": absolute_url("/privacy"),
            "priority": "0.3",
            "changefreq": "yearly",
        },
        {
            "loc": absolute_url("/terms"),
            "priority": "0.3",
            "changefreq": "yearly",
        },
    ]

    for brand in BRAND_PAGES:
        pages.append(
            {
                "loc": absolute_url(f"/brand/{brand['slug']}"),
                "priority": "0.7",
                "changefreq": "weekly",
            }
        )

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    for page in pages:
        xml.append("  <url>")
        xml.append(f"    <loc>{escape(page['loc'])}</loc>")
        xml.append(f"    <changefreq>{page['changefreq']}</changefreq>")
        xml.append(f"    <priority>{page['priority']}</priority>")
        xml.append("  </url>")

    xml.append("</urlset>")

    return "\n".join(xml), 200, {"Content-Type": "application/xml"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

