import gc
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import psycopg2
import requests
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
    Response,
    send_from_directory,
    session,
    url_for,
)
from openai import OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

from services.ad_relevance import AdRelevanceFilter
from services.meta_ads import (
    MetaAdsService,
    MetaAdsServiceError,
    MetaAdsPlatformLimitError,
    extract_best_image_url,
    normalize_image_url,
)
from seo_brands import (
    BRAND_PAGES,
    get_brand_by_slug as get_static_brand_by_slug,
    get_related_brands as get_static_related_brands,
)
from seo_candidate_seeds import (
    SEO_CANDIDATE_SEEDS,
    get_seed_brands_for_categories,
    get_seed_categories,
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "").strip()


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
CACHE_TTL_HOURS = 12
FRESH_SEARCH_ESTIMATED_COST = 0.03
CACHED_SEARCH_ESTIMATED_COST = 0.0
RATE_LIMIT_PER_MINUTE = 12
ABUSE_BLOCK_AFTER = 18
COST_PER_SEARCH_ALERT_THRESHOLD = 0.08
CACHE_RATE_ALERT_THRESHOLD = 40.0
COST_SPIKE_THRESHOLD_PERCENT = 50.0
PUBLIC_SEARCH_COUNTRY = "US"
SEO_BRAND_CACHE_TTL_HOURS = 72
SEO_BRAND_PREVIEW_COUNT = 3
SEO_BRAND_REFRESH_MAX_RESULTS = 12
SEO_BRAND_CACHE_REFRESH_MAX_RESULTS = 50
SEO_REFRESH_MAX_BRANDS_PER_CLICK = env_int("SEO_REFRESH_MAX_BRANDS_PER_CLICK", 3, minimum=1)
SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY = env_int("SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY", 10)
SEO_REFRESH_DISABLE_BULK = env_bool("SEO_REFRESH_DISABLE_BULK", False)
SEO_STALE_CACHE_REFRESH_LIMIT = SEO_REFRESH_MAX_BRANDS_PER_CLICK
SEO_REFRESH_RUN_TYPE_PATTERN = "seo_cache_refresh_%"
SEO_REFRESH_ADVISORY_LOCK_ID = 73190422
APIFY_USAGE_LIMIT_MESSAGE = "Apify usage limit reached. No further refreshes were attempted."
SEO_ENGINE_2_DRY_RUN_LIMIT = 25
SEO_ENGINE_2_RECENT_ACTIVITY_LIMIT = 12
SEO_ENGINE_2_MAINTENANCE_DEFAULT_LIMIT = 10
SEO_ENGINE_2_MAINTENANCE_MAX_LIMIT = 25
SEO_DURABLE_IMAGE_ABSOLUTE_MAX_BYTES = 1_000_000
SEO_DURABLE_IMAGE_MAX_BYTES = min(
    SEO_DURABLE_IMAGE_ABSOLUTE_MAX_BYTES,
    env_int("SEO_DURABLE_IMAGE_MAX_BYTES", 500_000, minimum=50_000),
)
SEO_DURABLE_IMAGE_MAX_BRANDS_PER_CLICK = 3
SEO_DURABLE_IMAGE_MAX_SOURCE_BYTES = 8_000_000
SEO_DURABLE_IMAGE_MAX_SOURCE_PIXELS = 12_000_000
SEO_DURABLE_IMAGE_MAX_DIMENSION = 900
SEO_DURABLE_IMAGE_MAX_REDIRECTS = 3
SEO_DURABLE_IMAGE_CONNECT_TIMEOUT_SECONDS = 5
SEO_DURABLE_IMAGE_READ_TIMEOUT_SECONDS = 12
SAMPLE_RESULTS_PREFERRED_BRAND_SLUGS = (
    "gymshark",
    "adidas",
    "athletic-greens",
    "ag1",
    "brevo",
    "kimp",
)
SAMPLE_RESULTS_BRAND_LIMIT = 3
SAMPLE_RESULTS_ADS_PER_BRAND = 2
SEO_CANDIDATE_TEST_COUNTRY = "US"
SEO_CANDIDATE_TEST_MAX_RESULTS = 25
SEO_CANDIDATE_MAX_QUERY_VARIANTS = 3

SEO_CANDIDATE_QUERY_ALIASES = {
    "mud-wtr": ["MUD\\WTR", "Mud Wtr", "MUDWTR"],
    "se-ranking": ["SE Ranking", "seranking"],
    "dollar-shave-club": ["Dollar Shave Club", "DollarShaveClub"],
    "dr-squatch": ["Dr. Squatch", "Dr Squatch"],
    "liquid-i-v": ["Liquid I.V.", "Liquid IV"],
    "ag1": ["AG1", "Athletic Greens"],
}

SEO_CACHE_DIAGNOSTIC_SLUGS = ("whoop", "native", "oura")

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
                    CREATE TABLE IF NOT EXISTS seo_brand_ad_cache (
                        id SERIAL PRIMARY KEY,
                        brand_slug TEXT UNIQUE NOT NULL,
                        brand_name TEXT NOT NULL,
                        search_query TEXT NOT NULL,
                        country TEXT NOT NULL DEFAULT 'NO',
                        ads_json TEXT NOT NULL DEFAULT '[]',
                        result_count INTEGER DEFAULT 0,
                        preview_count INTEGER DEFAULT 0,
                        fetched_at TIMESTAMPTZ,
                        expires_at TIMESTAMPTZ,
                        refresh_status TEXT DEFAULT 'pending',
                        last_error TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_brand_ad_snapshots (
                        id SERIAL PRIMARY KEY,
                        brand_slug TEXT UNIQUE NOT NULL,
                        brand_name TEXT NOT NULL,
                        source_query TEXT NOT NULL,
                        creative_angle TEXT,
                        ads_json TEXT NOT NULL DEFAULT '[]',
                        ad_count INTEGER DEFAULT 0,
                        image_count INTEGER DEFAULT 0,
                        quality_status TEXT DEFAULT 'quality_empty',
                        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_ad_images (
                        id SERIAL PRIMARY KEY,
                        brand_slug TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        source_url_hash TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        byte_size INTEGER NOT NULL,
                        original_byte_size INTEGER,
                        image_data BYTEA NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        CONSTRAINT seo_ad_images_byte_size_positive CHECK (byte_size > 0)
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_ad_image_sources (
                        id SERIAL PRIMARY KEY,
                        image_id INTEGER NOT NULL REFERENCES seo_ad_images(id) ON DELETE CASCADE,
                        brand_slug TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        source_url_hash TEXT NOT NULL,
                        original_byte_size INTEGER,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_brands (
                        id SERIAL PRIMARY KEY,
                        brand_name TEXT NOT NULL,
                        brand_slug TEXT UNIQUE NOT NULL,
                        search_query TEXT NOT NULL,
                        category TEXT,
                        focus TEXT,
                        audience TEXT,
                        creative_angle TEXT,
                        market_context TEXT,
                        headline TEXT,
                        meta_title TEXT,
                        meta_description TEXT,
                        summary TEXT,
                        is_published BOOLEAN DEFAULT TRUE,
                        source_candidate_id INTEGER,
                        published_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_brand_candidates (
                        id SERIAL PRIMARY KEY,
                        brand_name TEXT NOT NULL,
                        brand_slug TEXT UNIQUE NOT NULL,
                        category TEXT,
                        status TEXT DEFAULT 'not_tested',
                        result_count INTEGER DEFAULT 0,
                        preview_count INTEGER DEFAULT 0,
                        last_tested_at TIMESTAMPTZ,
                        last_success_at TIMESTAMPTZ,
                        last_error TEXT,
                        is_qualified BOOLEAN DEFAULT FALSE,
                        promoted_at TIMESTAMPTZ,
                        published_brand_id INTEGER,
                        quality_score INTEGER DEFAULT 0,
                        quality_status TEXT DEFAULT 'untested',
                        quality_signals_json TEXT,
                        source_type TEXT,
                        source_name TEXT,
                        review_notes TEXT,
                        rejected_at TIMESTAMPTZ,
                        auto_published_at TIMESTAMPTZ,
                        last_scored_at TIMESTAMPTZ,
                        notes TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_automation_settings (
                        id SERIAL PRIMARY KEY,
                        is_enabled BOOLEAN DEFAULT FALSE,
                        kill_switch_enabled BOOLEAN DEFAULT TRUE,
                        daily_test_limit INTEGER DEFAULT 25,
                        daily_apify_run_limit INTEGER DEFAULT 25,
                        max_results_per_test INTEGER DEFAULT 25,
                        auto_publish_threshold INTEGER DEFAULT 70,
                        review_threshold INTEGER DEFAULT 50,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seo_automation_runs (
                        id SERIAL PRIMARY KEY,
                        run_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TIMESTAMPTZ DEFAULT NOW(),
                        finished_at TIMESTAMPTZ,
                        candidates_tested INTEGER DEFAULT 0,
                        auto_published INTEGER DEFAULT 0,
                        sent_to_review INTEGER DEFAULT 0,
                        rejected INTEGER DEFAULT 0,
                        failed INTEGER DEFAULT 0,
                        apify_runs INTEGER DEFAULT 0,
                        brands_attempted INTEGER DEFAULT 0,
                        platform_limit_reached BOOLEAN DEFAULT FALSE,
                        error TEXT
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

                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS brand_name TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS search_query TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS country TEXT DEFAULT 'NO';")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS ads_json TEXT DEFAULT '[]';")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS result_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS preview_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS refresh_status TEXT DEFAULT 'pending';")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS last_error TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_brand_ad_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS brand_slug TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS brand_name TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS source_query TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS creative_angle TEXT;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS ads_json TEXT DEFAULT '[]';")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS ad_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS image_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS durable_image_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS quality_status TEXT DEFAULT 'quality_empty';")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_brand_ad_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS brand_name TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS brand_slug TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS search_query TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS category TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS focus TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS audience TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS creative_angle TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS market_context TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS headline TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS meta_title TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS meta_description TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS summary TEXT;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT TRUE;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS source_candidate_id INTEGER;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_brands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS brand_name TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS brand_slug TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS category TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'not_tested';")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS result_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS preview_count INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS last_tested_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS last_error TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS is_qualified BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS published_brand_id INTEGER;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS quality_status TEXT DEFAULT 'untested';")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS quality_signals_json TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS source_type TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS source_name TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS review_notes TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS auto_published_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS last_scored_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS notes TEXT;")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_brand_candidates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS kill_switch_enabled BOOLEAN DEFAULT TRUE;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS daily_test_limit INTEGER DEFAULT 25;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS daily_apify_run_limit INTEGER DEFAULT 25;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS max_results_per_test INTEGER DEFAULT 25;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS auto_publish_threshold INTEGER DEFAULT 70;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS review_threshold INTEGER DEFAULT 50;")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_automation_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS run_type TEXT;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS status TEXT;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS candidates_tested INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS auto_published INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS sent_to_review INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS rejected INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS failed INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS apify_runs INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS brands_attempted INTEGER DEFAULT 0;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS platform_limit_reached BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE seo_automation_runs ADD COLUMN IF NOT EXISTS error TEXT;")

                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS brand_slug TEXT;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS source_url TEXT;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS source_url_hash TEXT;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS content_hash TEXT;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS content_type TEXT;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS byte_size INTEGER;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS original_byte_size INTEGER;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS image_data BYTEA;")
                cur.execute("ALTER TABLE seo_ad_images ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS image_id INTEGER;")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS brand_slug TEXT;")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS source_url TEXT;")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS source_url_hash TEXT;")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS original_byte_size INTEGER;")
                cur.execute("ALTER TABLE seo_ad_image_sources ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")

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
                    CREATE INDEX IF NOT EXISTS idx_seo_brand_ad_cache_brand_slug
                    ON seo_brand_ad_cache(brand_slug);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_brand_ad_snapshots_brand_slug
                    ON seo_brand_ad_snapshots(brand_slug);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_ad_images_source_url_hash
                    ON seo_ad_images(source_url_hash);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_ad_images_content_hash
                    ON seo_ad_images(content_hash);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_seo_ad_images_brand_slug
                    ON seo_ad_images(brand_slug);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_ad_image_sources_source_url_hash
                    ON seo_ad_image_sources(source_url_hash);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_seo_ad_image_sources_image_id
                    ON seo_ad_image_sources(image_id);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_brands_brand_slug
                    ON seo_brands(brand_slug);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_seo_brands_published_category
                    ON seo_brands(is_published, category);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_brand_candidates_brand_slug
                    ON seo_brand_candidates(brand_slug);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_seo_brand_candidates_quality_status
                    ON seo_brand_candidates(quality_status, updated_at);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_seo_automation_runs_started_at
                    ON seo_automation_runs(started_at);
                    """
                )

                cur.execute(
                    """
                    INSERT INTO seo_automation_settings (
                        id, is_enabled, kill_switch_enabled, daily_test_limit,
                        daily_apify_run_limit, max_results_per_test,
                        auto_publish_threshold, review_threshold, updated_at
                    )
                    VALUES (1, TRUE, FALSE, 25, 25, 25, 70, 50, NOW())
                    ON CONFLICT (id) DO NOTHING;
                    """
                )

                for brand in BRAND_PAGES:
                    cur.execute(
                        """
                        INSERT INTO seo_brands (
                            brand_name, brand_slug, search_query, category, focus,
                            audience, creative_angle, market_context, headline,
                            meta_title, meta_description, summary, is_published,
                            published_at, updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            TRUE, COALESCE(%s, NOW()), NOW()
                        )
                        ON CONFLICT (brand_slug)
                        DO UPDATE SET
                            brand_name = EXCLUDED.brand_name,
                            search_query = EXCLUDED.search_query,
                            category = EXCLUDED.category,
                            focus = EXCLUDED.focus,
                            audience = EXCLUDED.audience,
                            creative_angle = EXCLUDED.creative_angle,
                            market_context = EXCLUDED.market_context,
                            headline = EXCLUDED.headline,
                            meta_title = EXCLUDED.meta_title,
                            meta_description = EXCLUDED.meta_description,
                            summary = EXCLUDED.summary,
                            is_published = TRUE,
                            published_at = COALESCE(seo_brands.published_at, NOW()),
                            updated_at = NOW()
                        """,
                        (
                            brand["name"],
                            brand["slug"],
                            brand["search_query"],
                            brand["category"],
                            brand["focus"],
                            brand["audience"],
                            brand["creative_angle"],
                            brand["market_context"],
                            brand["headline"],
                            brand["meta_title"],
                            brand["meta_description"],
                            brand["summary"],
                            None,
                        ),
                    )

                cur.execute(
                    """
                    SELECT c.brand_slug, c.brand_name, c.search_query, c.ads_json,
                           COALESCE(c.fetched_at, c.updated_at, NOW()), b.creative_angle
                    FROM seo_brand_ad_cache c
                    LEFT JOIN seo_brands b ON b.brand_slug = c.brand_slug
                    LEFT JOIN seo_brand_ad_snapshots s ON s.brand_slug = c.brand_slug
                    WHERE s.id IS NULL
                      AND COALESCE(NULLIF(BTRIM(c.ads_json), ''), '[]') <> '[]'
                    """
                )
                for cache_row in cur.fetchall():
                    try:
                        cached_ads = json.loads(cache_row[3] or "[]")
                    except (TypeError, ValueError):
                        cached_ads = []
                    snapshot_ads = build_seo_snapshot_ads(cache_row[1], cached_ads)
                    if not snapshot_ads:
                        continue
                    snapshot_image_count = count_snapshot_images(snapshot_ads)
                    snapshot_quality_status = get_seo_snapshot_quality_status(
                        len(snapshot_ads),
                        snapshot_image_count,
                    )
                    cur.execute(
                        """
                        INSERT INTO seo_brand_ad_snapshots (
                            brand_slug, brand_name, source_query, creative_angle,
                            ads_json, ad_count, image_count, quality_status,
                            captured_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (brand_slug) DO NOTHING
                        """,
                        (
                            cache_row[0],
                            cache_row[1],
                            cache_row[2] or cache_row[1],
                            cache_row[5],
                            json.dumps(snapshot_ads),
                            len(snapshot_ads),
                            snapshot_image_count,
                            snapshot_quality_status,
                            cache_row[4],
                        ),
                    )

                cur.execute(
                    """
                    SELECT id, ads_json, ad_count, image_count, quality_status
                    FROM seo_brand_ad_snapshots
                    """
                )
                for snapshot_row in cur.fetchall():
                    snapshot_ads = parse_ads_json(snapshot_row[1])
                    actual_ad_count = len(snapshot_ads)
                    actual_image_count = count_snapshot_images(snapshot_ads)
                    quality_status = get_seo_snapshot_quality_status(
                        actual_ad_count,
                        actual_image_count,
                    )
                    if (
                        actual_ad_count == int(snapshot_row[2] or 0)
                        and actual_image_count == int(snapshot_row[3] or 0)
                        and quality_status == (snapshot_row[4] or "quality_empty")
                    ):
                        continue
                    cur.execute(
                        """
                        UPDATE seo_brand_ad_snapshots
                        SET ad_count = %s,
                            image_count = %s,
                            quality_status = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            actual_ad_count,
                            actual_image_count,
                            quality_status,
                            snapshot_row[0],
                        ),
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


def build_public_search_cache_key(normalized_query: str, country: str) -> str:
    normalized_country = (country or "").strip().upper() or PUBLIC_SEARCH_COUNTRY
    return f"{normalized_country.lower()}:{normalized_query}"


def get_cached_results(normalized_query: str, country: str):
    cache_key = build_public_search_cache_key(normalized_query, country)
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
                    (cache_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return json.loads(row[0])
    finally:
        conn.close()


def save_cached_results(query_original: str, normalized_query: str, results: list, country: str):
    cache_key = build_public_search_cache_key(normalized_query, country)
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
                        cache_key,
                        query_original,
                        json.dumps(results),
                        len(results),
                        expires_at,
                    ),
                )
    finally:
        conn.close()


UNRESOLVED_TEMPLATE_PATTERN = re.compile(r"\{\{[^{}]*\}\}")


def title_from_slug(slug: str) -> str:
    return " ".join(part for part in (slug or "").replace("-", " ").split()).title()


def strip_unresolved_placeholders(value):
    if value is None:
        return ""
    text = str(value)
    text = UNRESOLVED_TEMPLATE_PATTERN.sub("", text)
    text = " ".join(text.split()).strip()
    return text


def safe_brand_display_name(brand_slug: str, brand_name: str = "") -> str:
    clean_name = strip_unresolved_placeholders(brand_name)
    return (clean_name or title_from_slug(brand_slug) or "Brand").upper()


SAFE_SEO_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


class SeoImagePreservationError(Exception):
    pass


def durable_seo_image_url(image_id) -> str:
    try:
        normalized_id = int(image_id)
    except (TypeError, ValueError):
        return ""
    return f"/seo-ad-image/{normalized_id}" if normalized_id > 0 else ""


def get_durable_image_id(ad) -> int | None:
    if not isinstance(ad, dict):
        return None
    try:
        image_id = int(ad.get("durable_image_id") or 0)
    except (TypeError, ValueError):
        return None
    return image_id if image_id > 0 else None


def get_external_seo_image_url(ad) -> str:
    if not isinstance(ad, dict):
        return ""
    source_url = normalize_image_url(ad.get("source_image_url"))
    if source_url:
        return source_url
    return extract_best_image_url(ad)


def get_seo_ad_display_image_url(ad) -> str:
    durable_url = durable_seo_image_url(get_durable_image_id(ad))
    return durable_url or get_external_seo_image_url(ad)


def count_durable_snapshot_images(ads) -> int:
    return sum(
        1
        for ad in (ads if isinstance(ads, list) else [])
        if get_durable_image_id(ad)
    )


def count_external_snapshot_images(ads) -> int:
    return sum(
        1
        for ad in (ads if isinstance(ads, list) else [])
        if get_external_seo_image_url(ad)
    )


def count_missing_durable_snapshot_images(ads) -> int:
    return sum(
        1
        for ad in (ads if isinstance(ads, list) else [])
        if not get_durable_image_id(ad) and get_external_seo_image_url(ad)
    )


def detect_safe_image_content_type(image_data: bytes) -> str:
    if len(image_data) < 32:
        return ""
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return ""


def validate_public_image_download_url(value: str) -> str:
    normalized_url = normalize_image_url(value)
    if not normalized_url:
        raise SeoImagePreservationError("Image URL is invalid or expired.")

    parsed = urlparse(normalized_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SeoImagePreservationError("Only HTTPS image URLs can be preserved.")
    if parsed.username or parsed.password:
        raise SeoImagePreservationError("Authenticated image URLs cannot be preserved.")
    try:
        image_port = parsed.port
    except ValueError as exc:
        raise SeoImagePreservationError("Image URL has an invalid port.") from exc
    if image_port not in (None, 443):
        raise SeoImagePreservationError("Image URL uses an unsupported port.")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SeoImagePreservationError("Image host could not be resolved.") from exc
    if not addresses:
        raise SeoImagePreservationError("Image host could not be resolved.")

    for address in addresses:
        try:
            resolved_ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise SeoImagePreservationError("Image host resolved to an invalid address.") from exc
        if not resolved_ip.is_global:
            raise SeoImagePreservationError("Image host is not publicly routable.")
    return normalized_url


def download_seo_image_source(source_url: str):
    current_url = source_url
    for redirect_count in range(SEO_DURABLE_IMAGE_MAX_REDIRECTS + 1):
        current_url = validate_public_image_download_url(current_url)
        try:
            response = requests.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=(
                    SEO_DURABLE_IMAGE_CONNECT_TIMEOUT_SECONDS,
                    SEO_DURABLE_IMAGE_READ_TIMEOUT_SECONDS,
                ),
                headers={"User-Agent": "RunningAds SEO image preservation/1.0"},
            )
        except requests.RequestException as exc:
            raise SeoImagePreservationError("Image download failed.") from exc

        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= SEO_DURABLE_IMAGE_MAX_REDIRECTS:
                    raise SeoImagePreservationError("Image download redirected too many times.")
                location = response.headers.get("Location")
                if not location:
                    raise SeoImagePreservationError("Image redirect was missing a destination.")
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                raise SeoImagePreservationError(f"Image download returned HTTP {response.status_code}.")

            declared_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if declared_type not in SAFE_SEO_IMAGE_CONTENT_TYPES:
                raise SeoImagePreservationError("Image response had an unsupported content type.")

            try:
                declared_size = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > SEO_DURABLE_IMAGE_MAX_SOURCE_BYTES:
                raise SeoImagePreservationError("Source image exceeds the safe optimization download limit.")

            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.extend(chunk)
                if len(chunks) > SEO_DURABLE_IMAGE_MAX_SOURCE_BYTES:
                    raise SeoImagePreservationError("Source image exceeds the safe optimization download limit.")
            image_data = bytes(chunks)
            detected_type = detect_safe_image_content_type(image_data)
            if not detected_type or detected_type != declared_type:
                raise SeoImagePreservationError("Image content did not match its declared type.")
            return image_data, detected_type, current_url
        finally:
            response.close()

    raise SeoImagePreservationError("Image download could not be completed.")


def flatten_seo_preview_image(image):
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba_image = image.convert("RGBA")
        background = Image.new("RGBA", rgba_image.size, (255, 255, 255, 255))
        background.alpha_composite(rgba_image)
        return background.convert("RGB")
    return image.convert("RGB")


def optimize_seo_image_preview(source_data: bytes):
    try:
        with Image.open(io.BytesIO(source_data)) as source_image:
            width, height = source_image.size
            if width <= 0 or height <= 0:
                raise SeoImagePreservationError("Source image dimensions were invalid.")
            if width * height > SEO_DURABLE_IMAGE_MAX_SOURCE_PIXELS:
                raise SeoImagePreservationError("Source image dimensions exceed the safe processing limit.")
            if getattr(source_image, "is_animated", False):
                source_image.seek(0)
            source_image.load()
            oriented_image = ImageOps.exif_transpose(source_image)
            rgb_image = flatten_seo_preview_image(oriented_image)
    except SeoImagePreservationError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise SeoImagePreservationError("Source image could not be decoded safely.") from exc

    dimension_steps = (SEO_DURABLE_IMAGE_MAX_DIMENSION, 720, 576, 460, 368)
    quality_steps = (82, 72, 62, 52, 45)
    for max_dimension in dimension_steps:
        preview_image = rgb_image.copy()
        preview_image.thumbnail(
            (
                min(max_dimension, SEO_DURABLE_IMAGE_MAX_DIMENSION),
                min(max_dimension, SEO_DURABLE_IMAGE_MAX_DIMENSION),
            ),
            Image.Resampling.LANCZOS,
        )
        for quality in quality_steps:
            output = io.BytesIO()
            preview_image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            optimized_data = output.getvalue()
            if len(optimized_data) <= SEO_DURABLE_IMAGE_MAX_BYTES:
                return optimized_data, "image/jpeg"

    raise SeoImagePreservationError(
        "Image could not be optimized below the durable preview size limit."
    )


def get_durable_seo_image_by_source(source_url: str):
    source_url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT image.id, image.content_type, image.byte_size,
                           COALESCE(source.original_byte_size, image.original_byte_size)
                    FROM seo_ad_image_sources source
                    JOIN seo_ad_images image ON image.id = source.image_id
                    WHERE source.source_url_hash = %s
                    """,
                    (source_url_hash,),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        """
                        SELECT id, content_type, byte_size, original_byte_size
                        FROM seo_ad_images
                        WHERE source_url_hash = %s
                        """,
                        (source_url_hash,),
                    )
                    row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": int(row[0]),
                    "content_type": row[1],
                    "byte_size": int(row[2] or 0),
                    "original_byte_size": int(row[3] or 0),
                }
    finally:
        conn.close()


def save_durable_seo_image(brand_slug: str, source_url: str):
    source_url = validate_public_image_download_url(source_url)
    existing = get_durable_seo_image_by_source(source_url)
    if existing:
        return existing

    source_data, _, _ = download_seo_image_source(source_url)
    original_byte_size = len(source_data)
    image_data, content_type = optimize_seo_image_preview(source_data)
    source_url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    content_hash = hashlib.sha256(image_data).hexdigest()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_ad_images (
                        brand_slug, source_url, source_url_hash, content_hash,
                        content_type, byte_size, original_byte_size, image_data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        brand_slug,
                        source_url,
                        source_url_hash,
                        content_hash,
                        content_type,
                        len(image_data),
                        original_byte_size,
                        psycopg2.Binary(image_data),
                    ),
                )
                inserted = cur.fetchone()
                if inserted:
                    image = {
                        "id": int(inserted[0]),
                        "content_type": content_type,
                        "byte_size": len(image_data),
                        "original_byte_size": original_byte_size,
                    }
                else:
                    cur.execute(
                        """
                        SELECT id, content_type, byte_size, original_byte_size
                        FROM seo_ad_images
                        WHERE source_url_hash = %s OR content_hash = %s
                        ORDER BY CASE WHEN source_url_hash = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (source_url_hash, content_hash, source_url_hash),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise SeoImagePreservationError("Durable image could not be stored.")
                    image = {
                        "id": int(row[0]),
                        "content_type": row[1],
                        "byte_size": int(row[2] or 0),
                        "original_byte_size": int(row[3] or 0),
                    }
                cur.execute(
                    """
                    INSERT INTO seo_ad_image_sources (
                        image_id, brand_slug, source_url, source_url_hash,
                        original_byte_size
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (source_url_hash) DO NOTHING
                    """,
                    (
                        image["id"],
                        brand_slug,
                        source_url,
                        source_url_hash,
                        original_byte_size,
                    ),
                )
                image["original_byte_size"] = original_byte_size
                return image
    finally:
        conn.close()


def preserve_durable_images_for_ads(brand_slug: str, ads):
    preserved_ads = []
    summary = {
        "attempted": 0,
        "saved": 0,
        "reused": 0,
        "already_durable": 0,
        "failed": 0,
        "errors": [],
    }
    for original_ad in (ads if isinstance(ads, list) else [])[:SEO_BRAND_PREVIEW_COUNT]:
        ad = dict(original_ad) if isinstance(original_ad, dict) else {}
        if get_durable_image_id(ad):
            summary["already_durable"] += 1
            preserved_ads.append(ad)
            continue

        source_url = get_external_seo_image_url(ad)
        if not source_url:
            preserved_ads.append(ad)
            continue

        summary["attempted"] += 1
        try:
            existing = get_durable_seo_image_by_source(source_url)
            image = existing or save_durable_seo_image(brand_slug, source_url)
            ad["source_image_url"] = source_url
            ad["durable_image_id"] = image["id"]
            ad["durable_content_type"] = image["content_type"]
            ad["durable_byte_size"] = image["byte_size"]
            ad["durable_original_byte_size"] = image.get("original_byte_size") or 0
            summary["reused" if existing else "saved"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(str(exc))
            print(f"SEO durable image preservation failed for {brand_slug}: {str(exc)}")
        preserved_ads.append(ad)
    return preserved_ads, summary


def prepare_seo_ad_preview(ad, fallback_brand_name: str, brand_slug: str):
    if not isinstance(ad, dict):
        ad = {}

    fallback_name = safe_brand_display_name(brand_slug, fallback_brand_name)
    page_name = strip_unresolved_placeholders(
        ad.get("page_name") or ad.get("advertiser_name") or ad.get("brand_name")
    )
    ad_text = strip_unresolved_placeholders(ad.get("ad_text"))
    headline = strip_unresolved_placeholders(ad.get("headline"))
    media_url = get_seo_ad_display_image_url(ad)
    snapshot_url = strip_unresolved_placeholders(ad.get("snapshot_url"))

    prepared = dict(ad)
    prepared["display_page_name"] = page_name or fallback_name
    prepared["display_ad_text"] = ad_text or headline
    prepared["media_url"] = media_url
    prepared["snapshot_url"] = snapshot_url
    return prepared


def select_seo_preview_ads(ads):
    normalized_ads = []
    for ad in ads if isinstance(ads, list) else []:
        if not isinstance(ad, dict):
            continue
        normalized = dict(ad)
        normalized["media_url"] = get_seo_ad_display_image_url(ad)
        normalized_ads.append(normalized)

    durable_ads = [ad for ad in normalized_ads if get_durable_image_id(ad)]
    external_ads = [
        ad for ad in normalized_ads
        if not get_durable_image_id(ad) and ad.get("media_url")
    ]
    text_only_ads = [ad for ad in normalized_ads if not ad.get("media_url")]
    return (durable_ads + external_ads + text_only_ads)[:SEO_BRAND_PREVIEW_COUNT]


SEO_SNAPSHOT_AD_FIELDS = (
    "ad_id",
    "page_name",
    "advertiser_name",
    "ad_text",
    "headline",
    "cta_text",
    "landing_page",
    "snapshot_url",
    "media_url",
    "source_image_url",
    "durable_image_id",
    "durable_content_type",
    "durable_byte_size",
    "durable_original_byte_size",
    "start_date_display",
    "days_running",
)


def parse_ads_json(value):
    try:
        ads = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return ads if isinstance(ads, list) else []


def build_seo_snapshot_ads(brand_name: str, ads):
    snapshot_ads = []
    for ad in select_seo_preview_ads(ads):
        compact = {}
        for field in SEO_SNAPSHOT_AD_FIELDS:
            value = ad.get(field)
            if field in {
                "days_running",
                "durable_image_id",
                "durable_byte_size",
                "durable_original_byte_size",
            }:
                try:
                    compact[field] = int(value) if value is not None else None
                except (TypeError, ValueError):
                    compact[field] = None
            else:
                compact[field] = strip_unresolved_placeholders(value)

        page_name = compact.get("page_name") or compact.get("advertiser_name")
        useful = any(
            compact.get(field)
            for field in ("media_url", "snapshot_url", "landing_page", "ad_text", "headline", "page_name")
        )
        if not useful:
            continue
        compact["page_name"] = page_name or strip_unresolved_placeholders(brand_name)
        compact["advertiser_name"] = compact.get("advertiser_name") or compact["page_name"]
        compact["ad_url"] = compact.get("snapshot_url") or compact.get("landing_page") or ""
        compact["title"] = compact.get("headline") or compact["page_name"]
        snapshot_ads.append(compact)
    return snapshot_ads


def count_snapshot_images(ads):
    return sum(
        1
        for ad in (ads if isinstance(ads, list) else [])
        if isinstance(ad, dict) and get_seo_ad_display_image_url(ad)
    )


def get_seo_snapshot_quality_status(ad_count: int, image_count: int, failed: bool = False):
    if failed:
        return "quality_failed"
    if int(image_count or 0) > 0:
        return "quality_good_images"
    if int(ad_count or 0) > 0:
        return "quality_ads_no_images"
    return "quality_empty"


def select_seo_brand_saved_data(cached, snapshot, now=None):
    now = now or utcnow()
    cached = cached or {}
    snapshot = snapshot or {}
    cache_ads = select_seo_preview_ads(cached.get("ads") or [])
    snapshot_ads = select_seo_preview_ads(snapshot.get("ads") or [])
    cache_image_count = count_snapshot_images(cache_ads)
    snapshot_image_count = count_snapshot_images(snapshot_ads)
    cache_durable_image_count = count_durable_snapshot_images(cache_ads)
    snapshot_durable_image_count = count_durable_snapshot_images(snapshot_ads)
    cache_is_fresh = bool(
        cache_ads
        and cached.get("refresh_status") == "success"
        and cached.get("expires_at")
        and cached["expires_at"] > now
    )
    snapshot_captured_at = snapshot.get("captured_at")
    snapshot_is_stale = bool(
        snapshot_ads
        and (
            not snapshot_captured_at
            or snapshot_captured_at < now - timedelta(hours=SEO_BRAND_CACHE_TTL_HOURS)
        )
    )

    if snapshot_durable_image_count > cache_durable_image_count:
        selected_ads = snapshot_ads
        data_source = "stale_snapshot" if snapshot_is_stale else "fallback_snapshot"
        source_captured_at = snapshot_captured_at
    elif cache_durable_image_count > snapshot_durable_image_count:
        selected_ads = cache_ads
        data_source = "fresh_cache" if cache_is_fresh else "stale_cache"
        source_captured_at = cached.get("fetched_at")
    elif (
        cache_is_fresh
        and cache_image_count > 0
        and cache_image_count >= snapshot_image_count
    ):
        selected_ads = cache_ads
        data_source = "fresh_cache"
        source_captured_at = cached.get("fetched_at")
    elif snapshot_image_count > 0:
        selected_ads = snapshot_ads
        data_source = "stale_snapshot" if snapshot_is_stale else "fallback_snapshot"
        source_captured_at = snapshot_captured_at
    elif cache_image_count > 0:
        selected_ads = cache_ads
        data_source = "fresh_cache" if cache_is_fresh else "stale_cache"
        source_captured_at = cached.get("fetched_at")
    elif snapshot_ads:
        selected_ads = snapshot_ads
        data_source = "stale_snapshot" if snapshot_is_stale else "fallback_snapshot"
        source_captured_at = snapshot_captured_at
    elif cache_ads:
        selected_ads = cache_ads
        data_source = "fresh_cache" if cache_is_fresh else "stale_cache"
        source_captured_at = cached.get("fetched_at")
    else:
        selected_ads = []
        data_source = "empty"
        source_captured_at = None

    preview_count = len(selected_ads)
    usable_image_count = count_snapshot_images(selected_ads)
    durable_image_count = count_durable_snapshot_images(selected_ads)
    external_image_count = count_external_snapshot_images(selected_ads)
    missing_durable_image_count = count_missing_durable_snapshot_images(selected_ads)
    failed = bool(not selected_ads and cached.get("refresh_status") == "failed")
    quality_status = get_seo_snapshot_quality_status(
        preview_count,
        usable_image_count,
        failed=failed,
    )
    snapshot_quality_status = get_seo_snapshot_quality_status(
        len(snapshot_ads),
        snapshot_image_count,
    )
    return {
        "ads": selected_ads,
        "data_source": data_source,
        "source_captured_at": source_captured_at,
        "preview_count": preview_count,
        "usable_image_count": usable_image_count,
        "durable_image_count": durable_image_count,
        "external_image_count": external_image_count,
        "missing_durable_image_count": missing_durable_image_count,
        "quality_status": quality_status,
        "cache_is_fresh": cache_is_fresh,
        "snapshot_is_stale": snapshot_is_stale,
        "snapshot_ad_count": len(snapshot_ads),
        "snapshot_image_count": snapshot_image_count,
        "snapshot_quality_status": snapshot_quality_status,
        "using_snapshot": data_source in ("fallback_snapshot", "stale_snapshot"),
        "using_saved_data": bool(selected_ads and data_source != "fresh_cache"),
        "needs_images": bool(preview_count > 0 and usable_image_count < preview_count),
        "seo_ready": bool(selected_ads and durable_image_count > 0),
        "durable_images_ready": bool(
            preview_count > 0 and durable_image_count >= preview_count
        ),
        "external_images_only": bool(durable_image_count == 0 and external_image_count > 0),
        "needs_durable_images": bool(
            durable_image_count > 0 and missing_durable_image_count > 0
        ),
        "stale_but_usable": bool(
            data_source in ("stale_snapshot", "stale_cache")
            and usable_image_count > 0
        ),
    }


def upsert_last_good_seo_snapshot(cur, brand, ads, captured_at):
    snapshot_ads = build_seo_snapshot_ads(brand.get("name") or "", ads)
    if not snapshot_ads:
        return False

    image_count = count_snapshot_images(snapshot_ads)
    durable_image_count = count_durable_snapshot_images(snapshot_ads)
    quality_status = get_seo_snapshot_quality_status(len(snapshot_ads), image_count)
    cur.execute(
        """
        SELECT ads_json
        FROM seo_brand_ad_snapshots
        WHERE brand_slug = %s
        FOR UPDATE
        """,
        (brand["slug"],),
    )
    existing_row = cur.fetchone()
    if existing_row:
        existing_ads = parse_ads_json(existing_row[0])
        existing_ad_count = len(existing_ads)
        existing_image_count = count_snapshot_images(existing_ads)
        existing_durable_image_count = count_durable_snapshot_images(existing_ads)
        existing_quality_status = get_seo_snapshot_quality_status(
            existing_ad_count,
            existing_image_count,
        )
        cur.execute(
            """
            UPDATE seo_brand_ad_snapshots
            SET ad_count = %s,
                image_count = %s,
                durable_image_count = %s,
                quality_status = %s
            WHERE brand_slug = %s
            """,
            (
                existing_ad_count,
                existing_image_count,
                existing_durable_image_count,
                existing_quality_status,
                brand["slug"],
            ),
        )
        if existing_durable_image_count > durable_image_count:
            return False
        if existing_durable_image_count == durable_image_count and existing_image_count > image_count:
            return False
        if (
            existing_durable_image_count == durable_image_count
            and existing_image_count == image_count
            and existing_ad_count > len(snapshot_ads)
        ):
            return False

    cur.execute(
        """
        INSERT INTO seo_brand_ad_snapshots (
            brand_slug, brand_name, source_query, creative_angle,
            ads_json, ad_count, image_count, durable_image_count, quality_status,
            captured_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (brand_slug)
        DO UPDATE SET
            brand_name = EXCLUDED.brand_name,
            source_query = EXCLUDED.source_query,
            creative_angle = EXCLUDED.creative_angle,
            ads_json = EXCLUDED.ads_json,
            ad_count = EXCLUDED.ad_count,
            image_count = EXCLUDED.image_count,
            durable_image_count = EXCLUDED.durable_image_count,
            quality_status = EXCLUDED.quality_status,
            captured_at = EXCLUDED.captured_at,
            updated_at = NOW()
        WHERE EXCLUDED.durable_image_count > COALESCE(seo_brand_ad_snapshots.durable_image_count, 0)
           OR (
                EXCLUDED.durable_image_count = COALESCE(seo_brand_ad_snapshots.durable_image_count, 0)
                AND EXCLUDED.image_count > COALESCE(seo_brand_ad_snapshots.image_count, 0)
           )
           OR (
                EXCLUDED.durable_image_count = COALESCE(seo_brand_ad_snapshots.durable_image_count, 0)
                AND EXCLUDED.image_count = COALESCE(seo_brand_ad_snapshots.image_count, 0)
                AND EXCLUDED.ad_count >= COALESCE(seo_brand_ad_snapshots.ad_count, 0)
           )
        """,
        (
            brand["slug"],
            brand["name"],
            brand.get("search_query") or brand["name"],
            brand.get("creative_angle") or "",
            json.dumps(snapshot_ads),
            len(snapshot_ads),
            image_count,
            durable_image_count,
            quality_status,
            captured_at,
        ),
    )
    return cur.rowcount > 0


def save_last_good_snapshot_from_search(brand, ads, source_query: str):
    if not brand or not ads:
        return False
    snapshot_brand = dict(brand)
    snapshot_brand["search_query"] = source_query or brand.get("search_query") or brand["name"]
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                return upsert_last_good_seo_snapshot(cur, snapshot_brand, ads, utcnow())
    finally:
        conn.close()


def get_cached_seo_brand_ads(brand_slug: str, fallback_brand_name: str = ""):
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ads_json, result_count, preview_count, fetched_at, expires_at,
                           refresh_status, last_error, brand_name
                    FROM seo_brand_ad_cache
                    WHERE brand_slug = %s
                    """,
                    (brand_slug,),
                )
                cache_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT ads_json, ad_count, image_count, captured_at,
                           source_query, brand_name, creative_angle
                    FROM seo_brand_ad_snapshots
                    WHERE brand_slug = %s
                    """,
                    (brand_slug,),
                )
                snapshot_row = cur.fetchone()

                cached = {
                    "ads": parse_ads_json(cache_row[0]) if cache_row else [],
                    "fetched_at": cache_row[3] if cache_row else None,
                    "expires_at": cache_row[4] if cache_row else None,
                    "refresh_status": cache_row[5] if cache_row else None,
                }
                snapshot = {
                    "ads": parse_ads_json(snapshot_row[0]) if snapshot_row else [],
                    "captured_at": snapshot_row[3] if snapshot_row else None,
                }
                page_state = select_seo_brand_saved_data(cached, snapshot)
                if page_state["using_snapshot"]:
                    brand_name = snapshot_row[5] or fallback_brand_name
                elif cache_row:
                    brand_name = cache_row[7] or fallback_brand_name
                else:
                    brand_name = fallback_brand_name

                preview_ads = [
                    prepare_seo_ad_preview(ad, brand_name, brand_slug)
                    for ad in page_state["ads"]
                ]
                return {
                    "ads": preview_ads,
                    "result_count": int(cache_row[1] or 0) if cache_row else 0,
                    "preview_count": len(preview_ads),
                    "usable_image_count": page_state["usable_image_count"],
                    "durable_image_count": page_state["durable_image_count"],
                    "external_image_count": page_state["external_image_count"],
                    "fetched_at": cache_row[3] if cache_row else None,
                    "expires_at": cache_row[4] if cache_row else None,
                    "refresh_status": cache_row[5] if cache_row else None,
                    "last_error": cache_row[6] if cache_row else None,
                    "data_source": page_state["data_source"],
                    "using_snapshot": page_state["using_snapshot"],
                    "using_saved_data": page_state["using_saved_data"],
                    "source_captured_at": page_state["source_captured_at"],
                    "snapshot_captured_at": snapshot_row[3] if snapshot_row else None,
                    "snapshot_ad_count": page_state["snapshot_ad_count"],
                    "snapshot_image_count": page_state["snapshot_image_count"],
                    "snapshot_source_query": snapshot_row[4] if snapshot_row else None,
                    "snapshot_is_stale": page_state["snapshot_is_stale"],
                    "quality_status": page_state["quality_status"],
                    "snapshot_quality_status": page_state["snapshot_quality_status"],
                    "seo_ready": page_state["seo_ready"],
                    "durable_images_ready": page_state["durable_images_ready"],
                    "external_images_only": page_state["external_images_only"],
                    "needs_durable_images": page_state["needs_durable_images"],
                    "needs_images": page_state["needs_images"],
                }
    except Exception as exc:
        print("SEO brand cache read error:", str(exc))
        return {
            "ads": [],
            "result_count": 0,
            "preview_count": 0,
            "usable_image_count": 0,
            "durable_image_count": 0,
            "external_image_count": 0,
            "fetched_at": None,
            "expires_at": None,
            "refresh_status": "error",
            "last_error": str(exc),
            "data_source": "empty",
            "using_snapshot": False,
            "using_saved_data": False,
            "source_captured_at": None,
            "snapshot_captured_at": None,
            "snapshot_ad_count": 0,
            "snapshot_image_count": 0,
            "snapshot_source_query": None,
            "snapshot_is_stale": False,
            "quality_status": "quality_failed",
            "snapshot_quality_status": "quality_empty",
            "seo_ready": False,
            "durable_images_ready": False,
            "external_images_only": False,
            "needs_durable_images": False,
            "needs_images": False,
        }
    finally:
        if conn:
            conn.close()


def get_durable_sample_result_brands(limit: int = SAMPLE_RESULTS_BRAND_LIMIT):
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_slug, brand_name, creative_angle, captured_at, ads_json
                    FROM seo_brand_ad_snapshots
                    WHERE durable_image_count > 0
                      AND quality_status = 'quality_good_images'
                    ORDER BY
                        COALESCE(array_position(%s::text[], brand_slug), 999),
                        captured_at DESC NULLS LAST,
                        brand_slug ASC
                    LIMIT 20
                    """,
                    (list(SAMPLE_RESULTS_PREFERRED_BRAND_SLUGS),),
                )
                snapshot_rows = cur.fetchall()

                image_ids = sorted({
                    image_id
                    for row in snapshot_rows
                    for ad in parse_ads_json(row[4])
                    for image_id in [get_durable_image_id(ad)]
                    if image_id
                })
                if not image_ids:
                    return []

                cur.execute(
                    """
                    SELECT id, content_type, SUBSTRING(image_data FROM 1 FOR 32)
                    FROM seo_ad_images
                    WHERE id = ANY(%s)
                      AND content_type = ANY(%s)
                      AND byte_size > 0
                      AND byte_size <= %s
                      AND image_data IS NOT NULL
                      AND OCTET_LENGTH(image_data) = byte_size
                      AND content_hash IS NOT NULL
                    """,
                    (
                        image_ids,
                        sorted(SAFE_SEO_IMAGE_CONTENT_TYPES),
                        SEO_DURABLE_IMAGE_ABSOLUTE_MAX_BYTES,
                    ),
                )
                valid_image_ids = {
                    int(row[0])
                    for row in cur.fetchall()
                    if detect_safe_image_content_type(bytes(row[2] or b"")) == row[1]
                }

        sample_brands = []
        for brand_slug, stored_name, stored_angle, captured_at, ads_json in snapshot_rows:
            saved_ads = []
            seen_image_ids = set()
            for ad in parse_ads_json(ads_json):
                image_id = get_durable_image_id(ad)
                if not image_id or image_id not in valid_image_ids or image_id in seen_image_ids:
                    continue

                prepared = prepare_seo_ad_preview(ad, stored_name, brand_slug)
                prepared["media_url"] = durable_seo_image_url(image_id)
                prepared["start_date"] = prepared.get("start_date_display") or "Not recorded"
                prepared["is_saved"] = True
                seen_image_ids.add(image_id)
                saved_ads.append(prepared)
                if len(saved_ads) >= SAMPLE_RESULTS_ADS_PER_BRAND:
                    break

            if not saved_ads:
                continue

            brand_name = strip_unresolved_placeholders(stored_name) or title_from_slug(brand_slug)
            creative_angle = strip_unresolved_placeholders(stored_angle) or "benefit-led messaging and offer positioning"
            for ad in saved_ads:
                ad["creative_angle"] = creative_angle
                ad["marketer_learning"] = (
                    "Compare the saved creative, message, and offer with other long-running ads "
                    "to identify patterns worth testing."
                )

            sample_brands.append({
                "slug": brand_slug,
                "name": brand_name,
                "usefulness": (
                    f"Study saved {brand_name} creatives to compare {creative_angle}."
                ),
                "captured_at": captured_at,
                "ads": saved_ads,
            })
            if len(sample_brands) >= max(1, int(limit or 1)):
                break
        return sample_brands
    except Exception as exc:
        print("Durable sample results read error:", str(exc))
        return []
    finally:
        if conn:
            conn.close()


def get_seo_brand_cache_by_slug():
    cache_by_slug = {}
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_slug, refresh_status, result_count, preview_count,
                           fetched_at, expires_at, last_error, country, updated_at,
                           ads_json
                    FROM seo_brand_ad_cache
                    """
                )
                for row in cur.fetchall():
                    cached_ads = parse_ads_json(row[9])
                    cache_by_slug[row[0]] = {
                        "refresh_status": row[1],
                        "result_count": row[2],
                        "preview_count": row[3],
                        "fetched_at": row[4],
                        "expires_at": row[5],
                        "last_error": row[6],
                        "country": row[7],
                        "updated_at": row[8],
                        "ad_count": len(cached_ads),
                        "image_count": count_snapshot_images(cached_ads),
                        "durable_image_count": count_durable_snapshot_images(cached_ads),
                        "external_image_count": count_external_snapshot_images(cached_ads),
                        "ads": cached_ads,
                    }
    finally:
        conn.close()
    return cache_by_slug


def get_seo_brand_snapshot_by_slug():
    snapshots = {}
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_slug, brand_name, source_query, creative_angle,
                           ad_count, image_count, durable_image_count, captured_at,
                           updated_at, ads_json, quality_status
                    FROM seo_brand_ad_snapshots
                    """
                )
                for row in cur.fetchall():
                    snapshot_ads = parse_ads_json(row[9])
                    snapshots[row[0]] = {
                        "brand_name": row[1],
                        "source_query": row[2],
                        "creative_angle": row[3],
                        "ad_count": len(snapshot_ads),
                        "image_count": count_snapshot_images(snapshot_ads),
                        "durable_image_count": count_durable_snapshot_images(snapshot_ads),
                        "external_image_count": count_external_snapshot_images(snapshot_ads),
                        "captured_at": row[7],
                        "updated_at": row[8],
                        "ads": snapshot_ads,
                        "stored_quality_status": row[10] or "quality_empty",
                        "quality_status": get_seo_snapshot_quality_status(
                            len(snapshot_ads),
                            count_snapshot_images(snapshot_ads),
                        ),
                    }
    finally:
        conn.close()
    return snapshots


def get_seo_page_data_source(cached, snapshot, now):
    return select_seo_brand_saved_data(cached, snapshot, now)["data_source"]


def get_seo_brand_cache_admin_rows():
    cache_by_slug = get_seo_brand_cache_by_slug()
    snapshots_by_slug = get_seo_brand_snapshot_by_slug()
    published_brands = get_published_seo_brands()
    rows = []
    summary = {
        "total_brands": len(published_brands),
        "with_previews": 0,
        "no_active_ads": 0,
        "failed": 0,
        "not_refreshed": 0,
        "with_snapshots": 0,
        "using_fallback": 0,
        "missing_snapshots": 0,
        "missing_snapshot_images": 0,
        "seo_ready": 0,
        "durable_images_ready": 0,
        "external_images_only": 0,
        "needs_durable_images": 0,
        "stale_but_usable": 0,
    }
    now = utcnow()

    for brand in published_brands:
        cached = cache_by_slug.get(brand["slug"], {})
        snapshot = snapshots_by_slug.get(brand["slug"], {})
        raw_status = cached.get("refresh_status")
        expires_at = cached.get("expires_at")
        is_expired = bool(expires_at and expires_at < now)
        page_state = select_seo_brand_saved_data(cached, snapshot, now)
        preview_count = page_state["preview_count"]
        data_source = page_state["data_source"]

        if int(snapshot.get("ad_count") or 0) > 0:
            summary["with_snapshots"] += 1
        else:
            summary["missing_snapshots"] += 1
        if page_state["needs_images"]:
            summary["missing_snapshot_images"] += 1
        if page_state["seo_ready"]:
            summary["seo_ready"] += 1
        if page_state["durable_images_ready"]:
            summary["durable_images_ready"] += 1
        if page_state["external_images_only"]:
            summary["external_images_only"] += 1
        if page_state["needs_durable_images"]:
            summary["needs_durable_images"] += 1
        if page_state["stale_but_usable"]:
            summary["stale_but_usable"] += 1
        if page_state["using_saved_data"]:
            summary["using_fallback"] += 1

        display_status, status_key = get_cache_status_display(raw_status, preview_count)

        if status_key == "failed":
            summary["failed"] += 1
        elif status_key == "not_refreshed":
            summary["not_refreshed"] += 1
        elif preview_count > 0:
            summary["with_previews"] += 1
        else:
            summary["no_active_ads"] += 1

        rows.append(
            {
                "name": brand["name"],
                "slug": brand["slug"],
                "refresh_status": raw_status,
                "display_status": display_status,
                "status_key": status_key,
                "is_expired": is_expired,
                "result_count": cached.get("result_count"),
                "preview_count": preview_count,
                "usable_image_count": page_state["usable_image_count"],
                "external_image_count": page_state["external_image_count"],
                "durable_image_count": page_state["durable_image_count"],
                "country": cached.get("country"),
                "fetched_at": cached.get("fetched_at"),
                "expires_at": expires_at,
                "updated_at": cached.get("updated_at"),
                "last_error": cached.get("last_error"),
                "data_source": data_source,
                "using_fallback": page_state["using_saved_data"],
                "quality_status": page_state["quality_status"],
                "seo_ready": page_state["seo_ready"],
                "durable_images_ready": page_state["durable_images_ready"],
                "external_images_only": page_state["external_images_only"],
                "needs_durable_images": page_state["needs_durable_images"],
                "needs_images": page_state["needs_images"],
                "stale_but_usable": page_state["stale_but_usable"],
                "snapshot_captured_at": snapshot.get("captured_at"),
                "snapshot_ad_count": int(snapshot.get("ad_count") or 0),
                "snapshot_image_count": int(snapshot.get("image_count") or 0),
                "snapshot_durable_image_count": int(snapshot.get("durable_image_count") or 0),
                "snapshot_source_query": snapshot.get("source_query"),
                "snapshot_is_stale": page_state["snapshot_is_stale"],
                "snapshot_quality_status": page_state["snapshot_quality_status"],
            }
        )

    return {
        "rows": rows,
        "summary": summary,
    }


def get_seo_market_audit_report():
    candidates = get_seo_brand_candidate_rows()
    published_brands = get_published_seo_brands()
    cache_by_slug = {}

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_slug, brand_name, country, result_count, preview_count,
                           refresh_status, fetched_at, expires_at, last_error, updated_at
                    FROM seo_brand_ad_cache
                    """
                )
                for row in cur.fetchall():
                    cache_by_slug[row[0]] = {
                        "brand_slug": row[0],
                        "brand_name": row[1],
                        "country": row[2],
                        "result_count": int(row[3] or 0),
                        "preview_count": int(row[4] or 0),
                        "refresh_status": row[5],
                        "fetched_at": row[6],
                        "expires_at": row[7],
                        "last_error": row[8],
                        "updated_at": row[9],
                    }
    finally:
        conn.close()

    candidates_by_slug = {row["brand_slug"]: row for row in candidates}
    published_by_slug = {brand["slug"]: brand for brand in published_brands}
    now = utcnow()

    def candidate_test_market(candidate):
        raw = candidate.get("quality_signals_json")
        if not raw:
            return None
        try:
            signals = json.loads(raw)
        except (TypeError, ValueError):
            return None
        diagnostics = signals.get("diagnostics") if isinstance(signals, dict) else None
        if not isinstance(diagnostics, dict):
            return None
        country = diagnostics.get("country")
        return str(country).upper() if country else None

    candidates_needing_retest = []
    for candidate in candidates:
        test_market = candidate_test_market(candidate)
        preview_count = int(candidate.get("preview_count") or 0)
        quality_status = candidate.get("quality_status") or "untested"
        needs_retest = (
            not candidate.get("last_tested_at")
            or quality_status in ("untested", "failed", "inconclusive", "rejected")
            or test_market != SEO_CANDIDATE_TEST_COUNTRY
        )
        if needs_retest:
            row = dict(candidate)
            row["test_market"] = test_market or "unknown"
            row["reason"] = "Not tested"
            if candidate.get("last_tested_at") and test_market != SEO_CANDIDATE_TEST_COUNTRY:
                row["reason"] = "Tested before/without US market diagnostics"
            elif quality_status in ("failed", "inconclusive", "rejected"):
                row["reason"] = get_quality_status_label(quality_status)
            elif preview_count == 0:
                row["reason"] = "No previews"
            candidates_needing_retest.append(row)

    brands_needing_refresh = []
    brands_with_previews = []
    published_empty_cache = []
    pre_us_cache_rows = []
    published_without_candidate = []

    for slug, brand in published_by_slug.items():
        cache = cache_by_slug.get(slug)
        candidate = candidates_by_slug.get(slug)
        if not candidate:
            published_without_candidate.append(
                {
                    "brand_name": brand["name"],
                    "brand_slug": slug,
                    "category": brand.get("category"),
                }
            )

        preview_count = int(cache.get("preview_count") or 0) if cache else 0
        country = (cache.get("country") or "unknown") if cache else "none"
        cache_row = {
            "brand_name": brand["name"],
            "brand_slug": slug,
            "preview_count": preview_count,
            "result_count": cache.get("result_count") if cache else 0,
            "refresh_status": cache.get("refresh_status") if cache else None,
            "country": country,
            "fetched_at": cache.get("fetched_at") if cache else None,
            "updated_at": cache.get("updated_at") if cache else None,
            "expires_at": cache.get("expires_at") if cache else None,
            "is_expired": bool(cache and cache.get("expires_at") and cache["expires_at"] < now),
            "last_error": cache.get("last_error") if cache else None,
        }

        if cache and str(country).upper() != SEO_CANDIDATE_TEST_COUNTRY:
            pre_us_cache_rows.append(cache_row)

        if preview_count > 0:
            brands_with_previews.append(cache_row)
        else:
            brands_needing_refresh.append(cache_row)
            published_empty_cache.append(cache_row)

    candidate_not_published = [
        {
            "brand_name": candidate["brand_name"],
            "brand_slug": candidate["brand_slug"],
            "status_label": candidate["status_label"],
            "quality_status_label": candidate["quality_status_label"],
            "quality_score": candidate["quality_score"],
            "preview_count": candidate.get("preview_count") or 0,
            "last_tested_at": candidate.get("last_tested_at"),
            "is_qualified": candidate.get("is_qualified"),
        }
        for slug, candidate in candidates_by_slug.items()
        if slug not in published_by_slug
    ]

    return {
        "summary": {
            "total_candidates": len(candidates),
            "total_published": len(published_brands),
            "total_cache_rows": len(cache_by_slug),
            "candidates_needing_retest": len(candidates_needing_retest),
            "published_needing_refresh": len(brands_needing_refresh),
            "published_with_previews": len(brands_with_previews),
            "candidate_not_published": len(candidate_not_published),
            "published_without_candidate": len(published_without_candidate),
            "pre_us_cache_rows": len(pre_us_cache_rows),
        },
        "candidates_needing_retest": candidates_needing_retest,
        "brands_needing_refresh": brands_needing_refresh,
        "brands_with_previews": brands_with_previews,
        "candidate_not_published": candidate_not_published,
        "published_without_candidate": published_without_candidate,
        "published_empty_cache": published_empty_cache,
        "pre_us_cache_rows": pre_us_cache_rows,
    }


def get_seo_engine_2_pipeline_map():
    """Human-readable map of the existing SEO engine for future admin/autopilot work."""
    return [
        {
            "step": "Discover",
            "routes": "/admin/seo-candidate-generator",
            "functions": "seo_candidate_generator, insert_seed_seo_brand_candidates",
            "templates": "templates/seo_candidate_generator.html",
        },
        {
            "step": "Qualify",
            "routes": "/admin/seo-brand-candidates, /admin/seo-brand-candidates/process-next",
            "functions": "test_seo_brand_candidate, process_one_seo_automation_candidate",
            "templates": "templates/seo_brand_candidates.html",
        },
        {
            "step": "Promote",
            "routes": "/admin/seo-brand-candidates/<id>/promote, /admin/seo-brand-candidates/bulk-promote",
            "functions": "promote_seo_brand_candidate, promote_seo_brand_candidate_route",
            "templates": "templates/seo_brand_candidates.html",
        },
        {
            "step": "Build",
            "routes": "/brand/<brand_slug>",
            "functions": "get_brand_by_slug, get_cached_seo_brand_ads, get_related_brands",
            "templates": "templates/brand.html",
        },
        {
            "step": "Validate",
            "routes": "/admin/seo-engine-2, /admin/seo-market-audit",
            "functions": "build_seo_engine_2_dry_run, get_seo_market_audit_report",
            "templates": "templates/seo_engine_2.html, templates/seo_market_audit.html",
        },
        {
            "step": "Publish",
            "routes": "/brand/<brand_slug>, /sitemap.xml",
            "functions": "get_published_seo_brands, sitemap_xml",
            "templates": "templates/brand.html",
        },
        {
            "step": "Maintain",
            "routes": "/admin/seo-brand-cache",
            "functions": "refresh_published_seo_brand_ads_cache, preserve_missing_durable_seo_images",
            "templates": "templates/seo_brand_cache_admin.html",
        },
    ]


def parse_quality_signals(value):
    if isinstance(value, dict):
        return value
    try:
        signals = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return signals if isinstance(signals, dict) else {}


def has_meaningful_brand_text(brand):
    text_fields = [
        brand.get("summary"),
        brand.get("focus"),
        brand.get("audience"),
        brand.get("creative_angle"),
        brand.get("market_context"),
        brand.get("meta_description"),
    ]
    useful_fields = [
        field
        for field in text_fields
        if isinstance(field, str) and len(field.strip()) >= 24
    ]
    return len(useful_fields) >= 4


def evaluate_seo_brand_quality_gates(brand, cached=None, snapshot=None, candidate=None, published_count: int = 0):
    cached = cached or {}
    snapshot = snapshot or {}
    candidate = candidate or {}
    now = utcnow()
    page_state = select_seo_brand_saved_data(cached, snapshot, now)
    slug = (brand.get("slug") or brand.get("brand_slug") or "").strip().lower()
    expected_slug = slugify_brand_name(brand.get("name") or brand.get("brand_name") or slug)
    candidate_preview_count = int(candidate.get("preview_count") or 0)
    quality_signals = parse_quality_signals(candidate.get("quality_signals_json"))
    candidate_image_count = int(quality_signals.get("image_count") or 0)
    candidate_brand_match = (quality_signals.get("brand_match") or "").lower()

    passed = []
    blocked = []

    def check(condition, pass_label, block_label):
        if condition:
            passed.append(pass_label)
        else:
            blocked.append(block_label)

    check(bool(brand.get("name") or brand.get("brand_name")), "Brand name exists", "Missing brand name")
    check(bool(slug and slug == expected_slug), "Clean slug", "Slug is missing or unclear")
    check(
        bool((brand.get("category") or "").strip() and (brand.get("focus") or "").strip()),
        "Commercial context exists",
        "Missing commercial category or focus",
    )
    check(
        bool(page_state["preview_count"] > 0 or candidate_preview_count > 0),
        "Ad preview signal exists",
        "No existing ad previews or candidate preview potential",
    )
    check(has_meaningful_brand_text(brand), "Template text is populated", "Generic or incomplete page text")
    check(
        bool(page_state["usable_image_count"] > 0 or candidate_image_count > 0),
        "Image signal exists",
        "No usable ad/product image signal",
    )
    check(
        not bool(cached.get("refresh_status") == "failed" and page_state["preview_count"] == 0),
        "No critical cache failure",
        "Critical cache failure with no saved previews",
    )
    check(True, "Clear CTA present in brand template", "Missing CTA")
    check(published_count > 1, "Internal links available", "No related brand links available yet")

    if candidate and candidate_brand_match:
        check(
            candidate_brand_match in {"likely", "strong"},
            f"Brand match is {candidate_brand_match}",
            f"Brand match is {candidate_brand_match}",
        )

    return {
        "seo_ready": not blocked,
        "passed": passed,
        "blocked": blocked,
        "page_state": page_state,
    }


def evaluate_seo_candidate_promotion_gates(candidate):
    signals = parse_quality_signals(candidate.get("quality_signals_json"))
    image_count = int(signals.get("image_count") or 0)
    brand_match = (signals.get("brand_match") or "").lower()
    score = int(candidate.get("quality_score") or 0)
    preview_count = int(candidate.get("preview_count") or 0)
    blocked = []

    if not candidate.get("is_qualified"):
        blocked.append("Candidate is not qualified")
    if candidate.get("published_brand_id"):
        blocked.append("Already published")
    if candidate.get("status") == "archived":
        blocked.append("Candidate is archived")
    if candidate.get("quality_status") in {"rejected", "failed", "inconclusive", "untested"}:
        blocked.append(f"Quality status is {get_quality_status_label(candidate.get('quality_status'))}")
    if preview_count <= 0:
        blocked.append("No preview ads found")
    if score < 70:
        blocked.append("Quality score is below 70")
    if image_count <= 0:
        blocked.append("No image signal from candidate test")
    if brand_match and brand_match not in {"likely", "strong"}:
        blocked.append(f"Brand match is {brand_match}")

    return {
        "ready": not blocked,
        "blocked": blocked,
        "signals": signals,
    }


def get_recent_seo_engine_activity(limit: int = SEO_ENGINE_2_RECENT_ACTIVITY_LIMIT):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_type, status, started_at, finished_at,
                           candidates_tested, auto_published, sent_to_review,
                           rejected, failed, apify_runs, brands_attempted,
                           platform_limit_reached, error
                    FROM seo_automation_runs
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = []
                for row in cur.fetchall():
                    rows.append(
                        {
                            "run_type": row[0],
                            "status": row[1],
                            "started_at": row[2],
                            "finished_at": row[3],
                            "candidates_tested": int(row[4] or 0),
                            "auto_published": int(row[5] or 0),
                            "sent_to_review": int(row[6] or 0),
                            "rejected": int(row[7] or 0),
                            "failed": int(row[8] or 0),
                            "apify_runs": int(row[9] or 0),
                            "brands_attempted": int(row[10] or 0),
                            "platform_limit_reached": bool(row[11]),
                            "error": row[12],
                        }
                    )
                return rows
    finally:
        conn.close()


def build_seo_engine_2_overview():
    candidates = get_seo_brand_candidate_rows(sort_key="alpha", view_key="all")
    published_brands = get_published_seo_brands()
    cache_by_slug = get_seo_brand_cache_by_slug()
    snapshots_by_slug = get_seo_brand_snapshot_by_slug()
    published_count = len(published_brands)
    now = utcnow()

    published_page_rows = []
    summary = {
        "total_candidates": len(candidates),
        "qualified_candidates": 0,
        "manual_review_candidates": 0,
        "rejected_candidates": 0,
        "published_brands": published_count,
        "published_with_images": 0,
        "published_missing_images": 0,
        "published_stale_failed_cache": 0,
        "published_needing_refresh": 0,
        "candidates_ready_for_promotion": 0,
        "candidates_blocked_from_promotion": 0,
    }

    candidates_by_slug = {candidate["brand_slug"]: candidate for candidate in candidates}
    candidate_rows = []
    for candidate in candidates:
        if candidate.get("status") == "archived":
            continue
        if candidate.get("is_qualified"):
            summary["qualified_candidates"] += 1
        if candidate.get("quality_status") == "needs_review" and not candidate.get("published_brand_id"):
            summary["manual_review_candidates"] += 1
        if candidate.get("quality_status") in {"rejected", "failed"}:
            summary["rejected_candidates"] += 1

        gates = evaluate_seo_candidate_promotion_gates(candidate)
        if gates["ready"]:
            summary["candidates_ready_for_promotion"] += 1
        elif not candidate.get("published_brand_id"):
            summary["candidates_blocked_from_promotion"] += 1
        row = dict(candidate)
        row["promotion_ready"] = gates["ready"]
        row["promotion_blockers"] = gates["blocked"]
        candidate_rows.append(row)

    for brand in published_brands:
        cached = cache_by_slug.get(brand["slug"], {})
        snapshot = snapshots_by_slug.get(brand["slug"], {})
        candidate = candidates_by_slug.get(brand["slug"], {})
        gate_result = evaluate_seo_brand_quality_gates(
            brand,
            cached=cached,
            snapshot=snapshot,
            candidate=candidate,
            published_count=published_count,
        )
        page_state = gate_result["page_state"]
        refresh_needed = is_stale_seo_cache_row(cached, now)
        cache_failed = cached.get("refresh_status") == "failed"

        if page_state["usable_image_count"] > 0:
            summary["published_with_images"] += 1
        else:
            summary["published_missing_images"] += 1
        if refresh_needed:
            summary["published_needing_refresh"] += 1
        if refresh_needed or cache_failed:
            summary["published_stale_failed_cache"] += 1

        published_page_rows.append(
            {
                "brand": brand,
                "cache": cached,
                "snapshot": snapshot,
                "candidate": candidate,
                "page_state": page_state,
                "quality_gates": gate_result,
                "refresh_needed": refresh_needed,
                "cache_failed": cache_failed,
            }
        )

    return {
        "summary": summary,
        "published_pages": published_page_rows,
        "candidates": candidate_rows,
        "activity": get_recent_seo_engine_activity(),
        "pipeline": get_seo_engine_2_pipeline_map(),
    }


def summarize_engine_row(row, reason):
    brand = row["brand"]
    page_state = row["page_state"]
    cache = row["cache"]
    return {
        "brand_name": brand["name"],
        "brand_slug": brand["slug"],
        "category": brand.get("category") or "",
        "reason": reason,
        "preview_count": page_state["preview_count"],
        "usable_image_count": page_state["usable_image_count"],
        "durable_image_count": page_state["durable_image_count"],
        "refresh_status": cache.get("refresh_status") or "not_refreshed",
        "updated_at": cache.get("updated_at"),
        "blocked": row["quality_gates"]["blocked"],
    }


def build_seo_engine_2_dry_run(overview=None):
    overview = overview or build_seo_engine_2_overview()
    candidates = overview["candidates"]
    published_pages = overview["published_pages"]

    missing_images = [
        summarize_engine_row(row, "Missing usable images")
        for row in published_pages
        if row["page_state"]["preview_count"] > 0 and row["page_state"]["usable_image_count"] <= 0
    ]
    missing_images.sort(key=lambda row: (row["updated_at"] or datetime.min.replace(tzinfo=timezone.utc), row["brand_name"]))

    needing_cache_repair = [
        summarize_engine_row(row, "Cache stale, empty, non-US, or failed")
        for row in published_pages
        if row["refresh_needed"] or row["cache_failed"]
    ]
    needing_cache_repair.sort(key=lambda row: (row["updated_at"] or datetime.min.replace(tzinfo=timezone.utc), row["brand_name"]))

    expired_image_risk = [
        summarize_engine_row(row, "External images should be preserved durably")
        for row in published_pages
        if row["page_state"]["needs_durable_images"] or row["page_state"]["external_images_only"]
    ]
    expired_image_risk.sort(key=lambda row: (row["updated_at"] or datetime.min.replace(tzinfo=timezone.utc), row["brand_name"]))

    ready_for_promotion = [
        candidate for candidate in candidates
        if candidate.get("promotion_ready")
    ]
    ready_for_promotion.sort(
        key=lambda row: (-(int(row.get("quality_score") or 0)), row.get("brand_name") or "")
    )

    manual_review = [
        candidate for candidate in candidates
        if (
            not candidate.get("published_brand_id")
            and candidate.get("quality_status") == "needs_review"
            and not candidate.get("promotion_ready")
        )
    ]
    manual_review.sort(
        key=lambda row: (-(int(row.get("quality_score") or 0)), row.get("brand_name") or "")
    )

    qualify_next = [
        candidate for candidate in candidates
        if (
            not candidate.get("published_brand_id")
            and candidate.get("status") != "archived"
            and candidate.get("quality_status") in {"untested", "failed", "inconclusive"}
        )
    ]
    qualify_next.sort(key=lambda row: (row.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), row.get("brand_name") or ""))

    reject_or_skip = [
        candidate for candidate in candidates
        if (
            not candidate.get("published_brand_id")
            and (
                candidate.get("quality_status") in {"rejected", "failed"}
                or candidate.get("status") == "archived"
            )
        )
    ]
    reject_or_skip.sort(key=lambda row: (row.get("brand_name") or "").lower())

    not_seo_ready = [
        summarize_engine_row(row, "Published page fails one or more quality gates")
        for row in published_pages
        if not row["quality_gates"]["seo_ready"]
    ]
    not_seo_ready.sort(key=lambda row: (len(row["blocked"]), row["brand_name"]), reverse=True)

    return {
        "generated_at": utcnow(),
        "mode": "dry_run_read_only",
        "mutates_records": False,
        "calls_paid_apis": False,
        "priority_order": [
            "Published pages missing images",
            "Published pages with broken/stale cache",
            "Published pages with expired or external image risk",
            "Qualified candidates ready for promotion",
            "New candidate generation",
        ],
        "published_missing_images": missing_images[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "published_needing_cache_repair": needing_cache_repair[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "published_with_expired_image_risk": expired_image_risk[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "candidates_ready_for_promotion": ready_for_promotion[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "candidates_to_qualify_next": qualify_next[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "candidates_for_manual_review": manual_review[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "candidates_to_reject_or_skip": reject_or_skip[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "published_pages_not_seo_ready": not_seo_ready[:SEO_ENGINE_2_DRY_RUN_LIMIT],
        "blocked_actions": [
            "No pages were published.",
            "No cache refresh ran.",
            "No image preservation ran.",
            "No candidates were tested.",
            "No Apify, OpenAI, Stripe, or external paid API calls were made.",
        ],
    }


def normalize_seo_engine_2_maintenance_limit(value=None):
    try:
        limit = int(value or SEO_ENGINE_2_MAINTENANCE_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = SEO_ENGINE_2_MAINTENANCE_DEFAULT_LIMIT
    return max(1, min(limit, SEO_ENGINE_2_MAINTENANCE_MAX_LIMIT))


def get_seo_engine_2_maintenance_limit_options():
    return [5, 10, 15, 25]


def get_seo_engine_2_maintenance_skip_reason(row):
    brand = row.get("brand") or {}
    candidate = row.get("candidate") or {}
    gates = row.get("quality_gates") or {}
    slug = (brand.get("slug") or "").strip().lower()

    if not slug:
        return "Missing brand slug"
    if not brand.get("name"):
        return "Missing brand name"
    if brand.get("is_published") is False:
        return "Brand is not published"
    if candidate.get("status") == "failed" or candidate.get("quality_status") in {"failed", "rejected"}:
        return "Linked candidate is failed or rejected"
    if any(
        blocker in set(gates.get("blocked") or [])
        for blocker in {
            "Missing brand name",
            "Slug is missing or unclear",
            "Missing commercial category or focus",
            "Generic or incomplete page text",
        }
    ):
        return "Critical metadata problem"
    return ""


def get_seo_engine_2_maintenance_reason(row):
    page_state = row.get("page_state") or {}
    if page_state.get("needs_images"):
        return "missing_images", "Published page is missing one or more ad images"
    if row.get("cache_failed") or row.get("refresh_needed"):
        return "cache_repair", "Published page has failed, stale, empty, non-US, or expired cache"
    if page_state.get("external_images_only") or page_state.get("needs_durable_images"):
        return "durable_image_preservation", "Published page has external or missing durable image storage"
    if page_state.get("stale_but_usable"):
        return "old_preview_refresh", "Published page uses old but usable previews"
    return "", ""


def build_seo_engine_2_maintenance_targets(overview, max_brands=None):
    max_brands = normalize_seo_engine_2_maintenance_limit(max_brands)
    priority = {
        "missing_images": 1,
        "cache_repair": 2,
        "durable_image_preservation": 3,
        "old_preview_refresh": 4,
    }
    skipped = []
    eligible = []
    published_pages = overview.get("published_pages") or []

    for row in published_pages:
        brand = row.get("brand") or {}
        skip_reason = get_seo_engine_2_maintenance_skip_reason(row)
        if skip_reason:
            skipped.append(
                {
                    "brand_name": brand.get("name") or "Unknown brand",
                    "brand_slug": brand.get("slug") or "",
                    "reason": skip_reason,
                }
            )
            continue

        reason_key, reason_label = get_seo_engine_2_maintenance_reason(row)
        if not reason_key:
            continue

        eligible.append(
            {
                "brand": brand,
                "cache": row.get("cache") or {},
                "snapshot": row.get("snapshot") or {},
                "page_state": row.get("page_state") or {},
                "reason_key": reason_key,
                "reason": reason_label,
                "priority": priority.get(reason_key, 99),
            }
        )

    eligible.sort(
        key=lambda item: (
            item["priority"],
            item["cache"].get("updated_at") or datetime.min.replace(tzinfo=timezone.utc),
            item["brand"].get("name") or "",
        )
    )
    selected = eligible[:max_brands]
    return {
        "brands_checked": len(published_pages),
        "eligible_count": len(eligible),
        "targets": selected,
        "skipped": skipped,
        "limit": max_brands,
    }


def build_seo_engine_2_refresh_target_data(target_selection):
    refresh_targets = [
        {
            "brand": target["brand"],
            "cache": target.get("cache") or {},
        }
        for target in target_selection["targets"]
        if target["reason_key"] in {"missing_images", "cache_repair", "old_preview_refresh"}
    ]
    return {
        "brands_checked": target_selection["brands_checked"],
        "eligible_count": len(refresh_targets),
        "estimated_brands_selected": len(refresh_targets),
        "targets": refresh_targets,
        "limit": target_selection["limit"],
    }


def preserve_durable_images_for_seo_engine_targets(target_selection):
    durable_targets = [
        target
        for target in target_selection["targets"]
        if target["reason_key"] == "durable_image_preservation"
    ]
    summary = {
        "brands_checked": target_selection["brands_checked"],
        "eligible_count": len(durable_targets),
        "brands_attempted": 0,
        "brands_updated": 0,
        "images_attempted": 0,
        "images_saved": 0,
        "images_reused": 0,
        "images_failed": 0,
        "errors": [],
        "limit": target_selection["limit"],
    }

    for target in durable_targets:
        brand = target["brand"]
        ads = (target.get("page_state") or {}).get("ads") or []
        if not ads:
            summary["errors"].append(f"{brand['name']}: no saved ads available for durable image preservation")
            continue
        summary["brands_attempted"] += 1
        try:
            preserved_ads, result = preserve_durable_images_for_ads(brand["slug"], ads)
            summary["images_attempted"] += result["attempted"]
            summary["images_saved"] += result["saved"]
            summary["images_reused"] += result["reused"]
            summary["images_failed"] += result["failed"]
            summary["errors"].extend(result["errors"])
            if result["saved"] or result["reused"]:
                if update_snapshot_durable_image_references(brand["slug"], preserved_ads):
                    summary["brands_updated"] += 1
        except Exception as exc:
            summary["images_failed"] += 1
            summary["errors"].append(f"{brand['name']}: {str(exc)}")

    return summary


def run_seo_engine_2_maintenance(
    max_brands=None,
    overview=None,
    refresh_executor=None,
    durable_executor=None,
    overview_builder=None,
    log_activity: bool = True,
):
    limit = normalize_seo_engine_2_maintenance_limit(max_brands)
    overview_builder = overview_builder or build_seo_engine_2_overview
    overview = overview or overview_builder()
    target_selection = build_seo_engine_2_maintenance_targets(overview, max_brands=limit)
    refresh_executor = refresh_executor or refresh_seo_brand_cache_targets
    durable_executor = durable_executor or preserve_durable_images_for_seo_engine_targets
    run_id = create_seo_automation_run("seo_engine_2_maintenance") if log_activity else None
    summary = {
        "started_at": utcnow(),
        "finished_at": None,
        "limit": limit,
        "brands_checked": target_selection["brands_checked"],
        "eligible_count": target_selection["eligible_count"],
        "brands_selected": len(target_selection["targets"]),
        "brands_repaired": 0,
        "cache_refresh_attempted": 0,
        "image_refresh_attempted": 0,
        "durable_image_attempted": 0,
        "durable_images_preserved": 0,
        "brands_skipped": len(target_selection["skipped"]),
        "skipped": target_selection["skipped"],
        "errors": [],
        "apify_runs": 0,
        "remaining_missing_images": 0,
        "remaining_stale_failed_cache": 0,
        "cache_summary": {},
        "durable_summary": {},
        "status": "completed",
    }

    try:
        refresh_target_data = build_seo_engine_2_refresh_target_data(target_selection)
        if refresh_target_data["targets"]:
            refresh_summary = refresh_executor(
                refresh_target_data,
                run_type="seo_engine_2_maintenance_cache_refresh",
            )
        else:
            refresh_summary = {
                "brands_attempted": 0,
                "brands_updated_with_previews": 0,
                "brands_preserved": 0,
                "brands_failed": 0,
                "timeouts": 0,
                "apify_runs": 0,
                "errors": [],
                "blocked_reason": "",
            }
        summary["cache_summary"] = refresh_summary
        summary["cache_refresh_attempted"] = int(refresh_summary.get("brands_attempted") or 0)
        selected_missing_image_targets = sum(
            1 for target in refresh_target_data["targets"]
            if target.get("brand", {}).get("slug") in {
                selected["brand"]["slug"]
                for selected in target_selection["targets"]
                if selected["reason_key"] == "missing_images"
            }
        )
        summary["image_refresh_attempted"] = min(
            selected_missing_image_targets,
            summary["cache_refresh_attempted"],
        )
        summary["apify_runs"] = int(refresh_summary.get("apify_runs") or 0)
        summary["errors"].extend(refresh_summary.get("errors") or [])
        if refresh_summary.get("blocked_reason"):
            summary["errors"].append(refresh_summary["blocked_reason"])

        durable_summary = durable_executor(target_selection)
        summary["durable_summary"] = durable_summary
        summary["durable_image_attempted"] = int(durable_summary.get("images_attempted") or 0)
        summary["durable_images_preserved"] = int(durable_summary.get("images_saved") or 0) + int(
            durable_summary.get("images_reused") or 0
        )
        summary["errors"].extend(durable_summary.get("errors") or [])
        summary["brands_repaired"] = (
            int(refresh_summary.get("brands_updated_with_previews") or 0)
            + int(refresh_summary.get("brands_preserved") or 0)
            + int(durable_summary.get("brands_updated") or 0)
        )

        refreshed_overview = overview_builder()
        refreshed_summary = refreshed_overview.get("summary") or {}
        summary["remaining_missing_images"] = int(refreshed_summary.get("published_missing_images") or 0)
        summary["remaining_stale_failed_cache"] = int(refreshed_summary.get("published_stale_failed_cache") or 0)
        if summary["errors"]:
            summary["status"] = "completed_with_errors"
    except Exception as exc:
        summary["status"] = "completed_with_errors"
        summary["errors"].append(str(exc))
    finally:
        summary["finished_at"] = utcnow()
        if run_id:
            error_message = ""
            if summary["errors"]:
                error_message = json.dumps(
                    {
                        "errors": summary["errors"][:5],
                        "remaining_missing_images": summary["remaining_missing_images"],
                        "remaining_stale_failed_cache": summary["remaining_stale_failed_cache"],
                    }
                )
            finish_seo_automation_run(
                run_id,
                summary["status"],
                {
                    "tested": 0,
                    "auto_published": 0,
                    "sent_to_review": 0,
                    "rejected": 0,
                    "failed": 1 if summary["status"] == "completed_with_errors" else 0,
                    "apify_runs": summary["apify_runs"],
                    "brands_attempted": summary["cache_refresh_attempted"] + int(
                        (summary.get("durable_summary") or {}).get("brands_attempted") or 0
                    ),
                },
                error=error_message,
            )
    return summary


def format_seo_engine_2_maintenance_summary(summary):
    message = (
        f"SEO maintenance repair complete. Checked {summary['brands_checked']} published page(s), "
        f"selected {summary['brands_selected']} of {summary['eligible_count']} eligible, "
        f"repaired or preserved {summary['brands_repaired']}, "
        f"cache refresh attempted {summary['cache_refresh_attempted']}, "
        f"image refresh attempted {summary['image_refresh_attempted']}, "
        f"durable images preserved {summary['durable_images_preserved']}, "
        f"skipped {summary['brands_skipped']}. "
        f"Remaining missing images: {summary['remaining_missing_images']}. "
        f"Remaining stale/failed cache: {summary['remaining_stale_failed_cache']}."
    )
    if summary["errors"]:
        message += f" Errors: {'; '.join(summary['errors'][:3])}"
        if len(summary["errors"]) > 3:
            message += f"; plus {len(summary['errors']) - 3} more."
    return message


def slugify_brand_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "candidate"


def fetch_filtered_seo_brand_ads(search_query: str, country: str = "NO", max_results: int | None = None):
    service = get_meta_ads_service()
    ads = service.search_ads(
        brand=search_query,
        country=country,
        max_results=max_results or SEO_BRAND_REFRESH_MAX_RESULTS,
    )

    relevance_filter = get_ad_relevance_filter()
    return relevance_filter.filter_ads(
        search_brand=search_query,
        ads=ads,
    )


def build_seo_candidate_query_variants(candidate):
    brand_name = (candidate.get("brand_name") or "").strip()
    brand_slug = (candidate.get("brand_slug") or slugify_brand_name(brand_name)).strip()
    variants = []

    def add_variant(value):
        value = re.sub(r"\s+", " ", (value or "").strip())
        if not value:
            return
        normalized = normalize_query(value)
        if normalized and normalized not in {normalize_query(item) for item in variants}:
            variants.append(value)

    add_variant(brand_name)
    add_variant(re.sub(r"[^A-Za-z0-9\s]+", " ", brand_name))
    add_variant(title_from_slug(brand_slug))

    for alias in SEO_CANDIDATE_QUERY_ALIASES.get(brand_slug, []):
        add_variant(alias)

    return variants[:SEO_CANDIDATE_MAX_QUERY_VARIANTS]


def get_candidate_rejection_reason(diagnostics, preview_count):
    if diagnostics.get("timed_out"):
        return "timeout"
    if preview_count > 0:
        return ""
    if diagnostics.get("raw_result_count", 0) <= 0:
        return "no_ads_found"
    if diagnostics.get("normalized_count", 0) <= 0:
        return "filtered_before_relevance"
    if diagnostics.get("relevance_filtered_count", 0) <= 0:
        return "filtered_by_relevance"
    return "no_preview_ads"


def fetch_candidate_validation_ads(
    candidate,
    max_results: int,
    max_variant_attempts: int,
    country: str = SEO_CANDIDATE_TEST_COUNTRY,
):
    service = get_meta_ads_service()
    relevance_filter = get_ad_relevance_filter()
    variants = build_seo_candidate_query_variants(candidate)[:max(1, max_variant_attempts)]
    diagnostics = {
        "country": country,
        "max_results": max_results,
        "query_variants": variants,
        "variant_results": [],
        "apify_runs": 0,
        "raw_result_count": 0,
        "normalized_count": 0,
        "relevance_filtered_count": 0,
        "final_preview_count": 0,
        "selected_query": None,
        "rejection_reason": "",
        "timed_out": False,
    }
    best_ads = []
    best_variant = None
    best_score = -1

    for variant in variants:
        diagnostics["apify_runs"] += 1
        try:
            search_result = service.search_ads_with_diagnostics(
                brand=variant,
                country=country,
                max_results=max_results,
                include_young_ads=True,
            )
        except MetaAdsServiceError as exc:
            exc.apify_runs = diagnostics["apify_runs"]
            raise
        normalized_ads = search_result["ads"]
        filtered_ads = relevance_filter.filter_ads(
            search_brand=candidate["brand_name"],
            ads=normalized_ads,
        )
        preview_count = min(len(filtered_ads), SEO_BRAND_PREVIEW_COUNT)
        variant_summary = {
            "query": variant,
            "raw_result_count": search_result["raw_result_count"],
            "normalized_count": search_result["normalized_count"],
            "relevance_filtered_count": len(filtered_ads),
            "final_preview_count": preview_count,
        }
        diagnostics["variant_results"].append(variant_summary)

        score = (
            preview_count * 1000
            + len(filtered_ads) * 100
            + search_result["normalized_count"] * 10
            + search_result["raw_result_count"]
        )
        if score > best_score:
            best_score = score
            best_ads = filtered_ads
            best_variant = variant_summary

        if preview_count > 0:
            break

    if best_variant:
        diagnostics.update(
            {
                "raw_result_count": best_variant["raw_result_count"],
                "normalized_count": best_variant["normalized_count"],
                "relevance_filtered_count": best_variant["relevance_filtered_count"],
                "final_preview_count": best_variant["final_preview_count"],
                "selected_query": best_variant["query"],
            }
        )

    diagnostics["rejection_reason"] = get_candidate_rejection_reason(
        diagnostics,
        diagnostics["final_preview_count"],
    )
    return best_ads, diagnostics


def get_seo_automation_settings():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, is_enabled, kill_switch_enabled, daily_test_limit,
                           daily_apify_run_limit, max_results_per_test,
                           auto_publish_threshold, review_threshold
                    FROM seo_automation_settings
                    WHERE id = 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "id": 1,
                        "is_enabled": True,
                        "kill_switch_enabled": False,
                        "daily_test_limit": 25,
                        "daily_apify_run_limit": 25,
                        "max_results_per_test": SEO_CANDIDATE_TEST_MAX_RESULTS,
                        "auto_publish_threshold": 70,
                        "review_threshold": 50,
                    }
                return {
                    "id": row[0],
                    "is_enabled": bool(row[1]),
                    "kill_switch_enabled": bool(row[2]),
                    "daily_test_limit": int(row[3] or 25),
                    "daily_apify_run_limit": int(row[4] or 25),
                    "max_results_per_test": max(
                        int(row[5] or SEO_CANDIDATE_TEST_MAX_RESULTS),
                        SEO_CANDIDATE_TEST_MAX_RESULTS,
                    ),
                    "auto_publish_threshold": int(row[6] or 70),
                    "review_threshold": int(row[7] or 50),
                }
    finally:
        conn.close()


def get_seo_automation_daily_usage():
    day_start, _ = get_today_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(candidates_tested), 0),
                        COALESCE(SUM(auto_published), 0),
                        COALESCE(SUM(sent_to_review), 0),
                        COALESCE(SUM(rejected), 0),
                        COALESCE(SUM(failed), 0),
                        COALESCE(SUM(apify_runs), 0)
                    FROM seo_automation_runs
                    WHERE started_at >= %s
                    """,
                    (day_start,),
                )
                row = cur.fetchone()
                tests = int(row[0] or 0)
                return {
                    "tests": tests,
                    "apify_runs": int(row[5] or tests),
                    "auto_published": int(row[1] or 0),
                    "sent_to_review": int(row[2] or 0),
                    "rejected": int(row[3] or 0),
                    "failed": int(row[4] or 0),
                }
    finally:
        conn.close()


def create_seo_automation_run(run_type: str = "run_next"):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_automation_runs (run_type, status, started_at)
                    VALUES (%s, 'running', NOW())
                    RETURNING id
                    """,
                    (run_type,),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def finish_seo_automation_run(run_id: int, status: str, counts: dict, error: str = ""):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE seo_automation_runs
                    SET status = %s,
                        finished_at = NOW(),
                        candidates_tested = %s,
                        auto_published = %s,
                        sent_to_review = %s,
                        rejected = %s,
                        failed = %s,
                        apify_runs = %s,
                        brands_attempted = %s,
                        platform_limit_reached = %s,
                        error = %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        counts.get("tested", 0),
                        counts.get("auto_published", 0),
                        counts.get("sent_to_review", 0),
                        counts.get("rejected", 0),
                        counts.get("failed", 0),
                        counts.get("apify_runs", counts.get("tested", 0)),
                        counts.get("brands_attempted", 0),
                        bool(counts.get("platform_limit_reached", False)),
                        error[:1000] if error else None,
                        run_id,
                    ),
                )
    finally:
        conn.close()


def get_seo_refresh_daily_usage():
    day_start, _ = get_today_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(apify_runs), 0),
                           COALESCE(SUM(brands_attempted), 0),
                           COALESCE(BOOL_OR(platform_limit_reached), FALSE)
                    FROM seo_automation_runs
                    WHERE started_at >= %s
                      AND run_type LIKE %s
                    """,
                    (day_start, SEO_REFRESH_RUN_TYPE_PATTERN),
                )
                today_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT error, COALESCE(finished_at, started_at)
                    FROM seo_automation_runs
                    WHERE run_type LIKE %s
                      AND platform_limit_reached = TRUE
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (SEO_REFRESH_RUN_TYPE_PATTERN,),
                )
                limit_row = cur.fetchone()
    finally:
        conn.close()

    apify_calls = int(today_row[0] or 0)
    remaining = max(0, SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY - apify_calls)
    return {
        "apify_calls_today": apify_calls,
        "brands_attempted_today": int(today_row[1] or 0),
        "daily_limit": SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY,
        "remaining_calls": remaining,
        "platform_limit_reached_today": bool(today_row[2]),
        "last_limit_error": limit_row[0] if limit_row else None,
        "last_limit_at": limit_row[1] if limit_row else None,
    }


def get_seo_refresh_safety_status():
    usage = get_seo_refresh_daily_usage()
    refresh_allowed = bool(
        usage["remaining_calls"] > 0
        and not usage["platform_limit_reached_today"]
    )
    return {
        **usage,
        "max_brands_per_click": SEO_REFRESH_MAX_BRANDS_PER_CLICK,
        "max_brands_this_click": min(
            SEO_REFRESH_MAX_BRANDS_PER_CLICK,
            usage["remaining_calls"],
        ),
        "bulk_disabled": SEO_REFRESH_DISABLE_BULK,
        "refresh_allowed": refresh_allowed,
        "bulk_refresh_allowed": bool(refresh_allowed and not SEO_REFRESH_DISABLE_BULK),
    }


def reserve_seo_refresh_run(run_type: str, requested_calls: int):
    requested_calls = max(1, int(requested_calls or 1))
    day_start, _ = get_today_range()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (SEO_REFRESH_ADVISORY_LOCK_ID,),
                )
                cur.execute(
                    """
                    SELECT COALESCE(SUM(apify_runs), 0),
                           COALESCE(BOOL_OR(platform_limit_reached), FALSE)
                    FROM seo_automation_runs
                    WHERE started_at >= %s
                      AND run_type LIKE %s
                    """,
                    (day_start, SEO_REFRESH_RUN_TYPE_PATTERN),
                )
                row = cur.fetchone()
                used_calls = int(row[0] or 0)
                if bool(row[1]):
                    return {
                        "run_id": None,
                        "reserved_calls": 0,
                        "remaining_calls": max(0, SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY - used_calls),
                        "blocked_reason": "apify_limit",
                    }

                remaining_calls = max(0, SEO_REFRESH_MAX_APIFY_CALLS_PER_DAY - used_calls)
                if remaining_calls <= 0:
                    return {
                        "run_id": None,
                        "reserved_calls": 0,
                        "remaining_calls": 0,
                        "blocked_reason": "daily_limit",
                    }

                reserved_calls = min(requested_calls, remaining_calls)
                cur.execute(
                    """
                    INSERT INTO seo_automation_runs (
                        run_type, status, started_at, apify_runs
                    )
                    VALUES (%s, 'reserved', NOW(), %s)
                    RETURNING id
                    """,
                    (run_type, reserved_calls),
                )
                return {
                    "run_id": cur.fetchone()[0],
                    "reserved_calls": reserved_calls,
                    "remaining_calls": remaining_calls,
                    "blocked_reason": "",
                }
    finally:
        conn.close()


def get_next_seo_automation_candidates(limit: int):
    if limit <= 0:
        return []

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, brand_name, brand_slug, category, status,
                           result_count, preview_count, last_tested_at,
                           last_success_at, last_error, is_qualified,
                           promoted_at, published_brand_id, notes,
                           created_at, updated_at, quality_score,
                           quality_status, quality_signals_json,
                           source_type, source_name, review_notes,
                           rejected_at, auto_published_at, last_scored_at
                    FROM seo_brand_candidates
                    WHERE published_brand_id IS NULL
                    AND COALESCE(status, 'not_tested') != 'archived'
                    AND COALESCE(quality_status, 'untested') IN ('untested', 'failed', 'inconclusive')
                    ORDER BY updated_at ASC, id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [seo_brand_candidate_row_to_dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_ad_days_running(ad):
    try:
        days = ad.get("days_running")
        if days is None:
            return None
        return int(days)
    except (TypeError, ValueError, AttributeError):
        return None


def calculate_brand_match_score(brand_name: str, ads: list):
    normalized_brand = normalize_query(brand_name)
    brand_tokens = [token for token in normalized_brand.split() if token]
    if not normalized_brand or not ads:
        return 0, "weak"

    best = 0
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        identity_text = normalize_query(
            " ".join(
                [
                    str(ad.get("page_name") or ""),
                    str(ad.get("advertiser_name") or ""),
                    str(ad.get("brand_name") or ""),
                ]
            )
        )
        broader_text = normalize_query(
            " ".join(
                [
                    identity_text,
                    str(ad.get("headline") or ""),
                    str(ad.get("ad_text") or ""),
                ]
            )
        )
        if normalized_brand and normalized_brand in identity_text:
            best = max(best, 20)
        elif brand_tokens and all(token in identity_text for token in brand_tokens):
            best = max(best, 16)
        elif normalized_brand and normalized_brand in broader_text:
            best = max(best, 12)
        elif brand_tokens and any(token in broader_text for token in brand_tokens):
            best = max(best, 8)

    if best >= 20:
        label = "strong"
    elif best >= 12:
        label = "likely"
    else:
        label = "weak"
    return best, label


def calculate_seo_candidate_quality(brand_name: str, ads: list):
    ads = ads if isinstance(ads, list) else []
    preview_ads = ads[:SEO_BRAND_PREVIEW_COUNT]
    preview_count = len(preview_ads)
    result_count = len(ads)

    if preview_count == 0:
        preview_points = 0
    elif preview_count == 1:
        preview_points = 15
    elif preview_count == 2:
        preview_points = 24
    else:
        preview_points = 30

    if result_count == 0:
        result_points = 0
    elif result_count <= 2:
        result_points = 5
    elif result_count <= 5:
        result_points = 10
    else:
        result_points = 15

    image_count = sum(1 for ad in preview_ads if strip_unresolved_placeholders(ad.get("media_url") if isinstance(ad, dict) else ""))
    if image_count == 0:
        image_points = 0
        image_label = "none"
    elif image_count >= max(1, preview_count - 1):
        image_points = 15
        image_label = "most"
    else:
        image_points = 8
        image_label = "some"

    text_count = 0
    for ad in preview_ads:
        if not isinstance(ad, dict):
            continue
        text = strip_unresolved_placeholders(ad.get("ad_text") or ad.get("headline"))
        if len(text) >= 20:
            text_count += 1
    if text_count == 0:
        text_points = 0
        text_label = "none"
    elif text_count >= max(1, preview_count - 1):
        text_points = 10
        text_label = "most"
    else:
        text_points = 5
        text_label = "some"

    brand_match_points, brand_match_label = calculate_brand_match_score(brand_name, preview_ads)

    max_days_running = 0
    for ad in preview_ads:
        days = get_ad_days_running(ad) if isinstance(ad, dict) else None
        if days is not None:
            max_days_running = max(max_days_running, days)

    if max_days_running >= 60:
        days_points = 10
    elif max_days_running >= 30:
        days_points = 8
    elif max_days_running >= 7:
        days_points = 5
    else:
        days_points = 0

    score = preview_points + result_points + image_points + text_points + brand_match_points + days_points
    signals = {
        "preview_count": preview_count,
        "result_count": result_count,
        "image_count": image_count,
        "image_availability": image_label,
        "ad_text_count": text_count,
        "ad_text_availability": text_label,
        "brand_match": brand_match_label,
        "brand_match_points": brand_match_points,
        "max_days_running": max_days_running or None,
        "points": {
            "preview_count": preview_points,
            "result_count": result_points,
            "image_availability": image_points,
            "ad_text_availability": text_points,
            "brand_match": brand_match_points,
            "days_running": days_points,
        },
    }
    return min(score, 100), signals


def build_seo_brand_defaults(brand_name: str, brand_slug: str, category: str = ""):
    name = (brand_name or title_from_slug(brand_slug) or "Brand").strip()
    slug = (brand_slug or slugify_brand_name(name)).strip()
    category = (category or "brand research").strip()
    focus = f"{category}, competitor ads, and performance marketing"
    audience = "marketers, founders, ecommerce operators, and growth teams researching active ads"
    creative_angle = "long-running creatives, offer positioning, messaging hooks, and ad formats"
    market_context = (
        f"a competitive {category} market where active ad examples can help reveal what brands keep live"
    )
    headline = f"Find long-running {name} ads"
    meta_title = f"{name} Ads | Find Long-Running Ads | RunningAds"
    meta_description = (
        f"Research active {name} ads across {focus}. See creative patterns and cached ad previews with RunningAds."
    )
    summary = (
        f"Use RunningAds to research active {name} ads across {focus}. "
        f"Look for {creative_angle}. Market context: {market_context}."
    )
    return {
        "name": name,
        "slug": slug,
        "search_query": name,
        "category": category,
        "focus": focus,
        "audience": audience,
        "creative_angle": creative_angle,
        "market_context": market_context,
        "headline": headline,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "summary": summary,
    }


def seo_brand_row_to_dict(row):
    defaults = build_seo_brand_defaults(row[1], row[2], category=row[4] or "")
    return {
        "id": row[0],
        "name": row[1] or defaults["name"],
        "slug": row[2] or defaults["slug"],
        "search_query": row[3] or defaults["search_query"],
        "category": row[4] or defaults["category"],
        "focus": row[5] or defaults["focus"],
        "audience": row[6] or defaults["audience"],
        "creative_angle": row[7] or defaults["creative_angle"],
        "market_context": row[8] or defaults["market_context"],
        "headline": row[9] or defaults["headline"],
        "meta_title": row[10] or defaults["meta_title"],
        "meta_description": row[11] or defaults["meta_description"],
        "summary": row[12] or defaults["summary"],
        "is_published": row[13],
        "source_candidate_id": row[14],
        "published_at": row[15],
    }


def get_published_seo_brands():
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM seo_brands")
                total_db_brands = int(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    SELECT id, brand_name, brand_slug, search_query, category,
                           focus, audience, creative_angle, market_context,
                           headline, meta_title, meta_description, summary,
                           is_published, source_candidate_id, published_at
                    FROM seo_brands
                    WHERE is_published = TRUE
                    ORDER BY brand_name ASC
                    """
                )
                brands = [seo_brand_row_to_dict(row) for row in cur.fetchall()]
                return brands if total_db_brands > 0 else BRAND_PAGES
    except Exception as exc:
        print("SEO brands DB read error:", str(exc))
        return BRAND_PAGES
    finally:
        if conn:
            conn.close()


def get_brand_by_slug(brand_slug: str):
    slug = (brand_slug or "").strip().lower()
    if not slug:
        return None

    conn = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, brand_name, brand_slug, search_query, category,
                           focus, audience, creative_angle, market_context,
                           headline, meta_title, meta_description, summary,
                           is_published, source_candidate_id, published_at
                    FROM seo_brands
                    WHERE brand_slug = %s
                    """,
                    (slug,),
                )
                row = cur.fetchone()
                if row:
                    if not row[13]:
                        return None
                    return seo_brand_row_to_dict(row)
    except Exception as exc:
        print("SEO brand lookup DB error:", str(exc))
    finally:
        if conn:
            conn.close()

    return get_static_brand_by_slug(slug)


def get_related_brands(brand, limit: int = 5):
    if not brand:
        return []

    try:
        brands = get_published_seo_brands()
        related = [
            item
            for item in brands
            if item["slug"] != brand["slug"] and item.get("category") == brand.get("category")
        ]

        if len(related) < limit:
            related.extend(
                item
                for item in brands
                if item["slug"] != brand["slug"] and item not in related
            )

        return related[:limit]
    except Exception as exc:
        print("Related SEO brands error:", str(exc))
        return get_static_related_brands(brand, limit=limit)


def is_static_seo_brand_slug(brand_slug: str):
    return get_static_brand_by_slug((brand_slug or "").strip().lower()) is not None


def unpublish_seo_brand(brand_slug: str):
    slug = (brand_slug or "").strip().lower()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE seo_brands
                    SET is_published = FALSE,
                        updated_at = NOW()
                    WHERE brand_slug = %s
                    RETURNING brand_name
                    """,
                    (slug,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]

                static_brand = get_static_brand_by_slug(slug)
                if not static_brand:
                    return None

                defaults = build_seo_brand_defaults(
                    static_brand["name"],
                    static_brand["slug"],
                    category=static_brand.get("category") or "",
                )
                cur.execute(
                    """
                    INSERT INTO seo_brands (
                        brand_name, brand_slug, search_query, category, focus,
                        audience, creative_angle, market_context, headline,
                        meta_title, meta_description, summary, is_published,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, NOW())
                    ON CONFLICT (brand_slug)
                    DO UPDATE SET
                        is_published = FALSE,
                        updated_at = NOW()
                    """,
                    (
                        static_brand["name"],
                        static_brand["slug"],
                        static_brand.get("search_query") or defaults["search_query"],
                        static_brand.get("category") or defaults["category"],
                        static_brand.get("focus") or defaults["focus"],
                        static_brand.get("audience") or defaults["audience"],
                        static_brand.get("creative_angle") or defaults["creative_angle"],
                        static_brand.get("market_context") or defaults["market_context"],
                        static_brand.get("headline") or defaults["headline"],
                        static_brand.get("meta_title") or defaults["meta_title"],
                        static_brand.get("meta_description") or defaults["meta_description"],
                        static_brand.get("summary") or defaults["summary"],
                    ),
                )
                return static_brand["name"]
    finally:
        conn.close()


def delete_seo_brand_cache(brand_slug: str):
    slug = (brand_slug or "").strip().lower()
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM seo_brand_ad_cache
                    WHERE brand_slug = %s
                    """,
                    (slug,),
                )
                return cur.rowcount
    finally:
        conn.close()


def delete_seo_brand(brand_slug: str):
    slug = (brand_slug or "").strip().lower()
    brand_name = None
    retained_tombstone = False
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_name
                    FROM seo_brands
                    WHERE brand_slug = %s
                    """,
                    (slug,),
                )
                row = cur.fetchone()
                if not row:
                    static_brand = get_static_brand_by_slug(slug)
                    if not static_brand:
                        return None, retained_tombstone

                    cur.execute(
                        """
                        DELETE FROM seo_brand_ad_cache
                        WHERE brand_slug = %s
                        """,
                        (slug,),
                    )
                    cur.execute(
                        """
                        DELETE FROM seo_brand_ad_snapshots
                        WHERE brand_slug = %s
                        """,
                        (slug,),
                    )
                    defaults = build_seo_brand_defaults(
                        static_brand["name"],
                        static_brand["slug"],
                        category=static_brand.get("category") or "",
                    )
                    cur.execute(
                        """
                        INSERT INTO seo_brands (
                            brand_name, brand_slug, search_query, category, focus,
                            audience, creative_angle, market_context, headline,
                            meta_title, meta_description, summary, is_published,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, NOW())
                        ON CONFLICT (brand_slug)
                        DO UPDATE SET
                            is_published = FALSE,
                            updated_at = NOW()
                        """,
                        (
                            static_brand["name"],
                            static_brand["slug"],
                            static_brand.get("search_query") or defaults["search_query"],
                            static_brand.get("category") or defaults["category"],
                            static_brand.get("focus") or defaults["focus"],
                            static_brand.get("audience") or defaults["audience"],
                            static_brand.get("creative_angle") or defaults["creative_angle"],
                            static_brand.get("market_context") or defaults["market_context"],
                            static_brand.get("headline") or defaults["headline"],
                            static_brand.get("meta_title") or defaults["meta_title"],
                            static_brand.get("meta_description") or defaults["meta_description"],
                            static_brand.get("summary") or defaults["summary"],
                        ),
                    )
                    return static_brand["name"], True

                brand_name = row[0]
                cur.execute(
                    """
                    DELETE FROM seo_brand_ad_cache
                    WHERE brand_slug = %s
                    """,
                    (slug,),
                )
                cur.execute(
                    """
                    DELETE FROM seo_brand_ad_snapshots
                    WHERE brand_slug = %s
                    """,
                    (slug,),
                )

                if is_static_seo_brand_slug(slug):
                    cur.execute(
                        """
                        UPDATE seo_brands
                        SET is_published = FALSE,
                            updated_at = NOW()
                        WHERE brand_slug = %s
                        """,
                        (slug,),
                    )
                    retained_tombstone = True
                else:
                    cur.execute(
                        """
                        DELETE FROM seo_brands
                        WHERE brand_slug = %s
                        """,
                        (slug,),
                    )

                return brand_name, retained_tombstone
    finally:
        conn.close()


def get_candidate_status_label(status: str):
    labels = {
        "qualified": "Qualified",
        "no_active_ads_found": "No active ads found",
        "inconclusive": "Search inconclusive",
        "failed": "Failed",
        "not_tested": "Not tested",
        "archived": "Archived",
    }
    return labels.get(status or "not_tested", "Not tested")


def get_quality_status_label(status: str):
    labels = {
        "untested": "Untested",
        "auto_published": "Auto-published",
        "needs_review": "Needs review",
        "inconclusive": "Search inconclusive",
        "rejected": "Rejected",
        "failed": "Failed",
    }
    return labels.get(status or "untested", "Untested")


def get_candidate_sort_options():
    return [
        {"key": "qualified_first", "label": "Qualified first"},
        {"key": "alpha", "label": "Alphabetical A to Z"},
        {"key": "published_first", "label": "Published first"},
        {"key": "unpublished_first", "label": "Unpublished first"},
        {"key": "inconclusive_first", "label": "Inconclusive first"},
        {"key": "needs_review_first", "label": "Needs review first"},
        {"key": "rejected_failed_first", "label": "Rejected/failed first"},
    ]


def get_candidate_view_options():
    return [
        {"key": "active", "label": "Active candidates"},
        {"key": "archived", "label": "Archived candidates"},
        {"key": "all", "label": "All candidates"},
    ]


def normalize_candidate_sort(value: str):
    allowed = {item["key"] for item in get_candidate_sort_options()}
    return value if value in allowed else "qualified_first"


def normalize_candidate_view(value: str):
    allowed = {item["key"] for item in get_candidate_view_options()}
    return value if value in allowed else "active"


def get_cache_status_display(raw_status, preview_count: int):
    if raw_status == "failed":
        return "Failed", "failed"
    if not raw_status:
        return "Not refreshed", "not_refreshed"
    if preview_count > 0:
        return "Preview available", "preview_available"
    return "No active ads found", "no_active_ads"


def seo_brand_candidate_row_to_dict(row):
    status = row[4] or "not_tested"
    quality_status = row[17] or "untested"
    return {
        "id": row[0],
        "brand_name": row[1],
        "brand_slug": row[2],
        "category": row[3],
        "status": status,
        "status_label": get_candidate_status_label(status),
        "status_key": status,
        "result_count": row[5],
        "preview_count": row[6],
        "last_tested_at": row[7],
        "last_success_at": row[8],
        "last_error": row[9],
        "is_qualified": row[10],
        "promoted_at": row[11],
        "published_brand_id": row[12],
        "notes": row[13],
        "created_at": row[14],
        "updated_at": row[15],
        "quality_score": int(row[16] or 0),
        "quality_status": quality_status,
        "quality_status_label": get_quality_status_label(quality_status),
        "quality_signals_json": row[18],
        "source_type": row[19],
        "source_name": row[20],
        "review_notes": row[21],
        "rejected_at": row[22],
        "auto_published_at": row[23],
        "last_scored_at": row[24],
    }


def apply_candidate_sort(rows, sort_key: str):
    sort_key = normalize_candidate_sort(sort_key)

    def name_key(row):
        return (row.get("brand_name") or "").lower()

    if sort_key == "alpha":
        return sorted(rows, key=name_key)
    if sort_key == "published_first":
        return sorted(rows, key=lambda row: (not bool(row.get("published_brand_id")), name_key(row)))
    if sort_key == "unpublished_first":
        return sorted(rows, key=lambda row: (bool(row.get("published_brand_id")), name_key(row)))
    if sort_key == "inconclusive_first":
        return sorted(
            rows,
            key=lambda row: (
                row.get("quality_status") != "inconclusive" and row.get("status") != "inconclusive",
                name_key(row),
            ),
        )
    if sort_key == "needs_review_first":
        return sorted(rows, key=lambda row: (row.get("quality_status") != "needs_review", name_key(row)))
    if sort_key == "rejected_failed_first":
        return sorted(
            rows,
            key=lambda row: (
                row.get("quality_status") not in ("rejected", "failed") and row.get("status") != "failed",
                name_key(row),
            ),
        )

    return sorted(rows, key=lambda row: (not bool(row.get("is_qualified")), name_key(row)))


def attach_cache_status_to_candidates(rows):
    cache_by_slug = get_seo_brand_cache_by_slug()
    enriched_rows = []
    for row in rows:
        candidate = dict(row)
        cached = cache_by_slug.get(candidate["brand_slug"], {})
        preview_count = int(cached.get("preview_count") or 0)
        raw_status = cached.get("refresh_status")
        display_status, status_key = get_cache_status_display(raw_status, preview_count)
        candidate.update(
            {
                "cache_refresh_status": raw_status,
                "cache_status_label": display_status,
                "cache_status_key": status_key,
                "cache_preview_count": preview_count if raw_status else None,
                "cache_country": cached.get("country"),
                "cache_updated_at": cached.get("updated_at"),
                "cache_fetched_at": cached.get("fetched_at"),
                "cache_expires_at": cached.get("expires_at"),
                "cache_last_error": cached.get("last_error"),
            }
        )
        enriched_rows.append(candidate)
    return enriched_rows


def filter_candidate_rows(rows, view_key: str):
    view_key = normalize_candidate_view(view_key)
    if view_key == "archived":
        return [row for row in rows if row.get("status") == "archived"]
    if view_key == "all":
        return rows
    return [row for row in rows if row.get("status") != "archived"]


def get_seo_brand_candidate_rows(sort_key: str = "qualified_first", view_key: str = "active"):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, brand_name, brand_slug, category, status,
                           result_count, preview_count, last_tested_at,
                           last_success_at, last_error, is_qualified,
                           promoted_at, published_brand_id, notes,
                           created_at, updated_at, quality_score,
                           quality_status, quality_signals_json,
                           source_type, source_name, review_notes,
                           rejected_at, auto_published_at, last_scored_at
                    FROM seo_brand_candidates
                    ORDER BY is_qualified DESC, updated_at DESC, brand_name ASC
                    """
                )
                rows = [seo_brand_candidate_row_to_dict(row) for row in cur.fetchall()]
                rows = attach_cache_status_to_candidates(rows)
                rows = filter_candidate_rows(rows, view_key)
                return apply_candidate_sort(rows, sort_key)
    finally:
        conn.close()


def get_seo_brand_candidate(candidate_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, brand_name, brand_slug, category, status,
                           result_count, preview_count, last_tested_at,
                           last_success_at, last_error, is_qualified,
                           promoted_at, published_brand_id, notes,
                           created_at, updated_at, quality_score,
                           quality_status, quality_signals_json,
                           source_type, source_name, review_notes,
                           rejected_at, auto_published_at, last_scored_at
                    FROM seo_brand_candidates
                    WHERE id = %s
                    """,
                    (candidate_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return seo_brand_candidate_row_to_dict(row)
    finally:
        conn.close()


def save_seo_brand_candidate(
    brand_name: str,
    category: str = "",
    notes: str = "",
    source_type: str = "",
    source_name: str = "",
):
    brand_slug = slugify_brand_name(brand_name)
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_brand_candidates (
                        brand_name, brand_slug, category, notes, source_type,
                        source_name, status, quality_status, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'not_tested', 'untested', NOW())
                    ON CONFLICT (brand_slug)
                    DO UPDATE SET
                        brand_name = EXCLUDED.brand_name,
                        category = EXCLUDED.category,
                        notes = EXCLUDED.notes,
                        source_type = EXCLUDED.source_type,
                        source_name = EXCLUDED.source_name,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        brand_name,
                        brand_slug,
                        category or None,
                        notes or None,
                        source_type or None,
                        source_name or None,
                    ),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def count_seo_brand_candidates():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM seo_brand_candidates")
                return int(cur.fetchone()[0] or 0)
    finally:
        conn.close()


def archive_seo_brand_candidate(candidate_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE seo_brand_candidates
                    SET status = 'archived',
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING brand_name
                    """,
                    (candidate_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


def delete_seo_brand_candidate(candidate_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM seo_brand_candidates
                    WHERE id = %s
                    RETURNING brand_name
                    """,
                    (candidate_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


def bulk_delete_seo_brand_candidates(candidate_ids: list[int]):
    clean_ids = []
    for candidate_id in candidate_ids:
        try:
            clean_ids.append(int(candidate_id))
        except (TypeError, ValueError):
            continue

    if not clean_ids:
        return 0

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM seo_brand_candidates
                    WHERE id = ANY(%s)
                    """,
                    (clean_ids,),
                )
                return cur.rowcount
    finally:
        conn.close()


def insert_seed_seo_brand_candidates(seed_rows):
    result = {
        "added": 0,
        "skipped": 0,
        "total": 0,
    }
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for seed in seed_rows:
                    brand_name = (seed.get("brand_name") or "").strip()
                    category = (seed.get("category") or "").strip()
                    brand_slug = slugify_brand_name(brand_name)
                    if not brand_name or not brand_slug:
                        result["skipped"] += 1
                        continue

                    cur.execute(
                        """
                        INSERT INTO seo_brand_candidates (
                            brand_name, brand_slug, category, notes,
                            source_type, source_name, status, quality_status,
                            created_at, updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s, 'seed', %s,
                            'not_tested', 'untested', NOW(), NOW()
                        )
                        ON CONFLICT (brand_slug) DO NOTHING
                        RETURNING id
                        """,
                        (
                            brand_name,
                            brand_slug,
                            category or None,
                            "Generated from curated SEO candidate seed list.",
                            category or "Seed",
                        ),
                    )
                    if cur.fetchone():
                        result["added"] += 1
                    else:
                        result["skipped"] += 1

                cur.execute("SELECT COUNT(*) FROM seo_brand_candidates")
                result["total"] = int(cur.fetchone()[0] or 0)
                return result
    finally:
        conn.close()


def update_seo_brand_candidate_test_result(
    candidate_id: int,
    status: str,
    result_count: int = 0,
    preview_count: int = 0,
    error_message: str = "",
):
    is_qualified = preview_count > 0
    last_success_at_sql = "NOW()" if is_qualified else "last_success_at"
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE seo_brand_candidates
                    SET status = %s,
                        result_count = %s,
                        preview_count = %s,
                        last_tested_at = NOW(),
                        last_success_at = {last_success_at_sql},
                        last_error = %s,
                        is_qualified = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        status,
                        result_count,
                        preview_count,
                        (error_message[:1000] if error_message else None),
                        is_qualified,
                        candidate_id,
                    ),
                )
    finally:
        conn.close()


def update_seo_brand_candidate_quality_result(
    candidate_id: int,
    status: str,
    result_count: int,
    preview_count: int,
    quality_score: int,
    quality_status: str,
    quality_signals: dict,
    error_message: str = "",
    auto_published: bool = False,
):
    is_qualified = preview_count > 0 and quality_status in ("auto_published", "needs_review")
    last_success_at_sql = "NOW()" if is_qualified else "last_success_at"
    rejected_at_sql = "NOW()" if quality_status == "rejected" else "rejected_at"
    auto_published_at_sql = "NOW()" if auto_published else "auto_published_at"
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE seo_brand_candidates
                    SET status = %s,
                        result_count = %s,
                        preview_count = %s,
                        last_tested_at = NOW(),
                        last_success_at = {last_success_at_sql},
                        last_error = %s,
                        is_qualified = %s,
                        quality_score = %s,
                        quality_status = %s,
                        quality_signals_json = %s,
                        last_scored_at = NOW(),
                        rejected_at = {rejected_at_sql},
                        auto_published_at = {auto_published_at_sql},
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        status,
                        result_count,
                        preview_count,
                        (error_message[:1000] if error_message else None),
                        is_qualified,
                        quality_score,
                        quality_status,
                        json.dumps(quality_signals),
                        candidate_id,
                    ),
                )
    finally:
        conn.close()


def update_seo_brand_candidate_quality_fields(
    candidate_id: int,
    quality_score: int,
    quality_status: str,
    quality_signals: dict,
):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE seo_brand_candidates
                    SET quality_score = %s,
                        quality_status = %s,
                        quality_signals_json = %s,
                        last_scored_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        quality_score,
                        quality_status,
                        json.dumps(quality_signals),
                        candidate_id,
                    ),
                )
    finally:
        conn.close()


def get_seo_candidate_quality_backfill_rows():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.brand_name, c.brand_slug, a.ads_json
                    FROM seo_brand_candidates c
                    JOIN seo_brand_ad_cache a ON a.brand_slug = c.brand_slug
                    WHERE COALESCE(c.quality_score, 0) = 0
                      AND COALESCE(c.preview_count, 0) > 0
                    ORDER BY c.updated_at DESC, c.brand_name ASC
                    """
                )
                return cur.fetchall()
    finally:
        conn.close()


def get_backfill_quality_status(preview_count: int):
    if preview_count <= 0:
        return "rejected"
    return "needs_review"


def backfill_seo_candidate_quality_scores_from_cache():
    rows = get_seo_candidate_quality_backfill_rows()
    result = {
        "checked": len(rows),
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    for candidate_id, brand_name, brand_slug, ads_json in rows:
        try:
            try:
                ads = json.loads(ads_json or "[]")
            except (TypeError, ValueError):
                ads = []

            if not isinstance(ads, list):
                ads = []

            preview_count = min(len(ads), SEO_BRAND_PREVIEW_COUNT)
            if preview_count <= 0:
                result["skipped"] += 1
                continue

            score, signals = calculate_seo_candidate_quality(brand_name, ads)
            quality_status = get_backfill_quality_status(preview_count)
            update_seo_brand_candidate_quality_fields(
                candidate_id,
                score,
                quality_status,
                signals,
            )
            result["updated"] += 1
        except Exception as exc:
            result["errors"].append(f"{brand_name or brand_slug}: {str(exc)}")

    return result


def promote_seo_brand_candidate(candidate):
    defaults = build_seo_brand_defaults(
        candidate["brand_name"],
        candidate["brand_slug"],
        category=candidate.get("category") or "",
    )
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_brands (
                        brand_name, brand_slug, search_query, category, focus,
                        audience, creative_angle, market_context, headline,
                        meta_title, meta_description, summary, is_published,
                        source_candidate_id, published_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        TRUE, %s, NOW(), NOW()
                    )
                    ON CONFLICT (brand_slug)
                    DO UPDATE SET
                        brand_name = COALESCE(seo_brands.brand_name, EXCLUDED.brand_name),
                        search_query = COALESCE(seo_brands.search_query, EXCLUDED.search_query),
                        category = COALESCE(seo_brands.category, EXCLUDED.category),
                        focus = COALESCE(seo_brands.focus, EXCLUDED.focus),
                        audience = COALESCE(seo_brands.audience, EXCLUDED.audience),
                        creative_angle = COALESCE(seo_brands.creative_angle, EXCLUDED.creative_angle),
                        market_context = COALESCE(seo_brands.market_context, EXCLUDED.market_context),
                        headline = COALESCE(seo_brands.headline, EXCLUDED.headline),
                        meta_title = COALESCE(seo_brands.meta_title, EXCLUDED.meta_title),
                        meta_description = COALESCE(seo_brands.meta_description, EXCLUDED.meta_description),
                        summary = COALESCE(seo_brands.summary, EXCLUDED.summary),
                        is_published = TRUE,
                        source_candidate_id = COALESCE(seo_brands.source_candidate_id, EXCLUDED.source_candidate_id),
                        published_at = COALESCE(seo_brands.published_at, NOW()),
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        defaults["name"],
                        defaults["slug"],
                        defaults["search_query"],
                        defaults["category"],
                        defaults["focus"],
                        defaults["audience"],
                        defaults["creative_angle"],
                        defaults["market_context"],
                        defaults["headline"],
                        defaults["meta_title"],
                        defaults["meta_description"],
                        defaults["summary"],
                        candidate["id"],
                    ),
                )
                published_brand_id = cur.fetchone()[0]
                cur.execute(
                    """
                    UPDATE seo_brand_candidates
                    SET promoted_at = COALESCE(promoted_at, NOW()),
                        published_brand_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (published_brand_id, candidate["id"]),
                )
                return published_brand_id
    finally:
        conn.close()


def save_seo_brand_ads_cache(brand, ads: list, country: str = "NO", result_count: int | None = None):
    ads = ads if isinstance(ads, list) else []
    stored_ads = select_seo_preview_ads(ads)
    try:
        stored_ads, _ = preserve_durable_images_for_ads(brand["slug"], stored_ads)
        stored_ads = select_seo_preview_ads(stored_ads)
    except Exception as exc:
        print(f"SEO durable image preservation skipped for {brand['slug']}: {str(exc)}")
    result_count = len(ads) if result_count is None else int(result_count or 0)
    preview_count = min(len(stored_ads), SEO_BRAND_PREVIEW_COUNT)
    fetched_at = utcnow()
    expires_at = fetched_at + timedelta(hours=SEO_BRAND_CACHE_TTL_HOURS)
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_brand_ad_cache (
                        brand_slug, brand_name, search_query, country, ads_json,
                        result_count, preview_count, fetched_at, expires_at,
                        refresh_status, last_error, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'success', NULL, NOW())
                    ON CONFLICT (brand_slug)
                    DO UPDATE SET
                        brand_name = EXCLUDED.brand_name,
                        search_query = EXCLUDED.search_query,
                        country = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.country
                            ELSE EXCLUDED.country
                        END,
                        ads_json = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.ads_json
                            ELSE EXCLUDED.ads_json
                        END,
                        result_count = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.result_count
                            ELSE EXCLUDED.result_count
                        END,
                        preview_count = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.preview_count
                            ELSE EXCLUDED.preview_count
                        END,
                        fetched_at = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.fetched_at
                            ELSE EXCLUDED.fetched_at
                        END,
                        expires_at = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN seo_brand_ad_cache.expires_at
                            ELSE EXCLUDED.expires_at
                        END,
                        refresh_status = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN 'no_results_preserved'
                            ELSE 'success'
                        END,
                        last_error = CASE
                            WHEN EXCLUDED.preview_count = 0
                                AND (
                                    COALESCE(seo_brand_ad_cache.preview_count, 0) > 0
                                    OR COALESCE(NULLIF(BTRIM(seo_brand_ad_cache.ads_json), ''), '[]') <> '[]'
                                )
                            THEN 'Refresh returned 0 preview ads; existing cached previews were preserved.'
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    """,
                    (
                        brand["slug"],
                        brand["name"],
                        brand["search_query"],
                        country,
                        json.dumps(stored_ads),
                        result_count,
                        preview_count,
                        fetched_at,
                        expires_at,
                    ),
                )
                upsert_last_good_seo_snapshot(cur, brand, stored_ads, fetched_at)
    finally:
        conn.close()


def save_seo_brand_cache_error(brand, error_message: str, country: str = "NO"):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seo_brand_ad_cache (
                        brand_slug, brand_name, search_query, country, ads_json,
                        result_count, preview_count, refresh_status, last_error, updated_at
                    )
                    VALUES (%s, %s, %s, %s, '[]', 0, 0, 'failed', %s, NOW())
                    ON CONFLICT (brand_slug)
                    DO UPDATE SET
                        brand_name = EXCLUDED.brand_name,
                        search_query = EXCLUDED.search_query,
                        country = EXCLUDED.country,
                        refresh_status = 'failed',
                        last_error = EXCLUDED.last_error,
                        updated_at = NOW()
                    """,
                    (
                        brand["slug"],
                        brand["name"],
                        brand["search_query"],
                        country,
                        error_message[:1000],
                    ),
                )
    finally:
        conn.close()


def refresh_seo_brand_ads_cache(brand, country: str = "NO"):
    ads = fetch_filtered_seo_brand_ads(brand["search_query"], country=country)
    save_seo_brand_ads_cache(brand, ads, country=country)
    return {
        "result_count": len(ads),
        "preview_count": min(len(ads), SEO_BRAND_PREVIEW_COUNT),
    }


def build_seo_brand_cache_candidate(brand):
    return {
        "brand_name": brand.get("search_query") or brand.get("name") or title_from_slug(brand.get("slug", "")),
        "brand_slug": brand.get("slug") or slugify_brand_name(brand.get("name", "")),
        "category": brand.get("category") or "",
    }


def get_existing_seo_cache_summary(brand_slug: str):
    slug = (brand_slug or "").strip().lower()
    cached = get_seo_brand_cache_by_slug().get(slug, {})
    snapshot = get_seo_brand_snapshot_by_slug().get(slug, {})
    ads_json_has_ads = False
    if cached:
        conn = get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ads_json
                        FROM seo_brand_ad_cache
                        WHERE brand_slug = %s
                        """,
                        ((brand_slug or "").strip().lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        try:
                            cached_ads = json.loads(row[0] or "[]")
                        except (TypeError, ValueError):
                            cached_ads = []
                        ads_json_has_ads = isinstance(cached_ads, list) and len(cached_ads) > 0
        finally:
            conn.close()

    return {
        "preview_count": int(cached.get("preview_count") or 0),
        "ads_json_has_ads": ads_json_has_ads,
        "snapshot_ad_count": int(snapshot.get("ad_count") or 0),
    }


def refresh_seo_brand_ads_cache_for_candidate(brand, candidate=None):
    candidate = candidate or build_seo_brand_cache_candidate(brand)
    ads, diagnostics = fetch_candidate_validation_ads(
        candidate,
        max_results=SEO_CANDIDATE_TEST_MAX_RESULTS,
        max_variant_attempts=SEO_CANDIDATE_MAX_QUERY_VARIANTS,
    )
    result_count = len(ads)
    preview_count = min(result_count, SEO_BRAND_PREVIEW_COUNT)
    save_seo_brand_ads_cache(
        brand,
        ads,
        country=SEO_CANDIDATE_TEST_COUNTRY,
        result_count=result_count,
    )
    return {
        "result_count": result_count,
        "preview_count": preview_count,
        "diagnostics": diagnostics,
    }


def refresh_published_seo_brand_ads_cache(
    brand,
    max_variant_attempts: int = SEO_CANDIDATE_MAX_QUERY_VARIANTS,
):
    candidate = build_seo_brand_cache_candidate(brand)
    existing_cache = get_existing_seo_cache_summary(brand["slug"])
    ads, diagnostics = fetch_candidate_validation_ads(
        candidate,
        max_results=SEO_BRAND_CACHE_REFRESH_MAX_RESULTS,
        max_variant_attempts=max_variant_attempts,
    )
    result_count = len(ads)
    preview_count = min(result_count, SEO_BRAND_PREVIEW_COUNT)
    preserved_existing_previews = (
        preview_count == 0
        and (
            existing_cache["preview_count"] > 0
            or existing_cache["ads_json_has_ads"]
            or existing_cache["snapshot_ad_count"] > 0
        )
    )
    save_seo_brand_ads_cache(
        brand,
        ads,
        country=SEO_CANDIDATE_TEST_COUNTRY,
        result_count=result_count,
    )
    return {
        "result_count": result_count,
        "preview_count": preview_count,
        "diagnostics": diagnostics,
        "preserved_existing_previews": preserved_existing_previews,
    }


def is_stale_seo_cache_row(cached, now):
    if not cached:
        return True

    preview_count = int(cached.get("preview_count") or 0)
    country = (cached.get("country") or "").upper()
    refresh_status = cached.get("refresh_status")
    fetched_at = cached.get("fetched_at")
    expires_at = cached.get("expires_at")
    updated_at = cached.get("updated_at")
    is_older_than_ttl = bool(fetched_at and fetched_at < now - timedelta(hours=SEO_BRAND_CACHE_TTL_HOURS))
    is_expired = bool(expires_at and expires_at < now)
    recently_attempted = bool(updated_at and updated_at >= now - timedelta(hours=SEO_BRAND_CACHE_TTL_HOURS))

    if preview_count == 0 or country != SEO_CANDIDATE_TEST_COUNTRY:
        return True

    if refresh_status == "no_results_preserved" and recently_attempted:
        return False

    return (
        is_older_than_ttl
        or is_expired
        or refresh_status != "success"
    )


def get_stale_seo_cache_refresh_targets(limit: int = SEO_STALE_CACHE_REFRESH_LIMIT):
    published_brands = get_published_seo_brands()
    cache_by_slug = get_seo_brand_cache_by_slug()
    now = utcnow()
    targets = []

    for brand in published_brands:
        cached = cache_by_slug.get(brand["slug"])
        if is_stale_seo_cache_row(cached, now):
            targets.append(
                {
                    "brand": brand,
                    "cache": cached or {},
                }
            )

    return {
        "brands_checked": len(published_brands),
        "eligible_count": len(targets),
        "targets": targets[:limit],
        "limit": limit,
    }


def get_selected_seo_cache_refresh_targets(brand_slugs, limit: int = SEO_STALE_CACHE_REFRESH_LIMIT):
    requested_slugs = []
    for value in brand_slugs or []:
        slug = (value or "").strip().lower()
        if slug and slug not in requested_slugs:
            requested_slugs.append(slug)
    published_by_slug = {brand["slug"]: brand for brand in get_published_seo_brands()}
    cache_by_slug = get_seo_brand_cache_by_slug()
    targets = [
        {
            "brand": published_by_slug[slug],
            "cache": cache_by_slug.get(slug, {}),
        }
        for slug in requested_slugs
        if slug in published_by_slug
    ]
    return {
        "brands_checked": len(requested_slugs),
        "eligible_count": len(targets),
        "targets": targets[:limit],
        "limit": limit,
    }


def get_missing_snapshot_refresh_targets(limit: int = SEO_STALE_CACHE_REFRESH_LIMIT):
    published_brands = get_published_seo_brands()
    snapshots_by_slug = get_seo_brand_snapshot_by_slug()
    cache_by_slug = get_seo_brand_cache_by_slug()
    targets = [
        {
            "brand": brand,
            "cache": cache_by_slug.get(brand["slug"], {}),
        }
        for brand in published_brands
        if int(snapshots_by_slug.get(brand["slug"], {}).get("ad_count") or 0) <= 0
    ]
    targets.sort(
        key=lambda target: target["cache"].get("updated_at")
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return {
        "brands_checked": len(published_brands),
        "eligible_count": len(targets),
        "targets": targets[:limit],
        "limit": limit,
    }


def get_missing_snapshot_image_refresh_targets(limit: int = SEO_STALE_CACHE_REFRESH_LIMIT):
    published_brands = get_published_seo_brands()
    snapshots_by_slug = get_seo_brand_snapshot_by_slug()
    cache_by_slug = get_seo_brand_cache_by_slug()
    targets = []

    for brand in published_brands:
        snapshot = snapshots_by_slug.get(brand["slug"], {})
        cached = cache_by_slug.get(brand["slug"], {})
        page_state = select_seo_brand_saved_data(cached, snapshot)
        snapshot_ad_count = int(snapshot.get("ad_count") or 0)
        snapshot_image_count = int(snapshot.get("image_count") or 0)
        snapshot_needs_images = bool(
            snapshot_ad_count > 0 and snapshot_image_count < snapshot_ad_count
        )
        if not page_state["needs_images"] and not snapshot_needs_images:
            continue
        targets.append(
            {
                "brand": brand,
                "cache": cached,
                "snapshot": snapshot,
            }
        )

    targets.sort(
        key=lambda target: target["snapshot"].get("captured_at")
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return {
        "brands_checked": len(published_brands),
        "eligible_count": len(targets),
        "targets": targets[:limit],
        "limit": limit,
    }


def get_missing_durable_image_targets(
    limit: int = SEO_DURABLE_IMAGE_MAX_BRANDS_PER_CLICK,
):
    published_brands = get_published_seo_brands()
    snapshots_by_slug = get_seo_brand_snapshot_by_slug()
    targets = []
    for brand in published_brands:
        snapshot = snapshots_by_slug.get(brand["slug"], {})
        ads = snapshot.get("ads") or []
        missing_durable_ads = [
            ad for ad in ads
            if (
                isinstance(ad, dict)
                and not get_durable_image_id(ad)
                and get_external_seo_image_url(ad)
            )
        ]
        if not missing_durable_ads:
            continue
        targets.append(
            {
                "brand": brand,
                "ads": ads,
                "captured_at": snapshot.get("captured_at"),
                "missing_count": len(missing_durable_ads),
            }
        )

    targets.sort(
        key=lambda target: target.get("captured_at")
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return {
        "brands_checked": len(published_brands),
        "eligible_count": len(targets),
        "targets": targets[:max(0, min(limit, SEO_DURABLE_IMAGE_MAX_BRANDS_PER_CLICK))],
        "limit": SEO_DURABLE_IMAGE_MAX_BRANDS_PER_CLICK,
    }


def update_snapshot_durable_image_references(brand_slug: str, ads) -> bool:
    snapshot_ads = select_seo_preview_ads(ads)
    ad_count = len(snapshot_ads)
    image_count = count_snapshot_images(snapshot_ads)
    durable_image_count = count_durable_snapshot_images(snapshot_ads)
    quality_status = get_seo_snapshot_quality_status(ad_count, image_count)
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE seo_brand_ad_snapshots
                    SET ads_json = %s,
                        ad_count = %s,
                        image_count = %s,
                        durable_image_count = %s,
                        quality_status = %s,
                        updated_at = NOW()
                    WHERE brand_slug = %s
                    """,
                    (
                        json.dumps(snapshot_ads),
                        ad_count,
                        image_count,
                        durable_image_count,
                        quality_status,
                        brand_slug,
                    ),
                )
                return cur.rowcount > 0
    finally:
        conn.close()


def preserve_missing_durable_seo_images():
    target_data = get_missing_durable_image_targets()
    summary = {
        "brands_checked": target_data["brands_checked"],
        "eligible_count": target_data["eligible_count"],
        "brands_attempted": 0,
        "brands_updated": 0,
        "images_attempted": 0,
        "images_saved": 0,
        "images_reused": 0,
        "images_failed": 0,
        "errors": [],
        "limit": SEO_DURABLE_IMAGE_MAX_BRANDS_PER_CLICK,
    }
    for target in target_data["targets"]:
        brand = target["brand"]
        summary["brands_attempted"] += 1
        preserved_ads, result = preserve_durable_images_for_ads(
            brand["slug"],
            target["ads"],
        )
        summary["images_attempted"] += result["attempted"]
        summary["images_saved"] += result["saved"]
        summary["images_reused"] += result["reused"]
        summary["images_failed"] += result["failed"]
        summary["errors"].extend(result["errors"])
        if result["saved"] or result["reused"]:
            if update_snapshot_durable_image_references(brand["slug"], preserved_ads):
                summary["brands_updated"] += 1
    return summary


def refresh_seo_brand_cache_targets(target_data, run_type: str):
    safety = get_seo_refresh_safety_status()
    targets = target_data["targets"][:SEO_REFRESH_MAX_BRANDS_PER_CLICK]
    summary = {
        "brands_checked": target_data["brands_checked"],
        "eligible_count": target_data["eligible_count"],
        "estimated_brands_selected": target_data["eligible_count"],
        "max_brands_attempted": min(
            len(targets),
            SEO_REFRESH_MAX_BRANDS_PER_CLICK,
            safety["remaining_calls"],
        ),
        "brands_attempted": 0,
        "brands_refreshed": 0,
        "brands_updated_with_previews": 0,
        "brands_preserved": 0,
        "brands_no_previews": 0,
        "brands_failed": 0,
        "timeouts": 0,
        "limit": target_data["limit"],
        "errors": [],
        "apify_runs": 0,
        "daily_apify_run_limit": safety["daily_limit"],
        "remaining_apify_runs": safety["remaining_calls"],
        "refresh_allowed": safety["refresh_allowed"],
        "bulk_disabled": safety["bulk_disabled"],
        "platform_limit_reached": False,
        "service_unavailable": False,
        "blocked_reason": "",
    }

    if safety["bulk_disabled"]:
        summary["blocked_reason"] = "Bulk SEO cache refresh is disabled."
        return summary
    if safety["platform_limit_reached_today"]:
        summary["blocked_reason"] = APIFY_USAGE_LIMIT_MESSAGE
        return summary
    if safety["remaining_calls"] <= 0:
        summary["blocked_reason"] = "SEO refresh daily safety limit reached. No refreshes were attempted."
        return summary
    if not targets:
        return summary

    requested_calls = len(targets) * SEO_CANDIDATE_MAX_QUERY_VARIANTS
    reservation = reserve_seo_refresh_run(run_type, requested_calls)
    if reservation["blocked_reason"]:
        summary["blocked_reason"] = (
            APIFY_USAGE_LIMIT_MESSAGE
            if reservation["blocked_reason"] == "apify_limit"
            else "SEO refresh daily safety limit reached. No refreshes were attempted."
        )
        summary["refresh_allowed"] = False
        summary["remaining_apify_runs"] = reservation["remaining_calls"]
        return summary

    run_id = reservation["run_id"]
    remaining_apify_runs = reservation["reserved_calls"]
    summary["max_brands_attempted"] = min(
        len(targets),
        SEO_REFRESH_MAX_BRANDS_PER_CLICK,
        remaining_apify_runs,
    )

    for target in targets[:summary["max_brands_attempted"]]:
        if remaining_apify_runs <= 0:
            break
        brand = target["brand"]
        cached = target["cache"]
        existing_preview_count = int(cached.get("preview_count") or 0)
        allowed_attempts = min(SEO_CANDIDATE_MAX_QUERY_VARIANTS, remaining_apify_runs)
        summary["brands_attempted"] += 1
        try:
            refresh_result = refresh_published_seo_brand_ads_cache(
                brand,
                max_variant_attempts=allowed_attempts,
            )
            apify_runs = max(1, int((refresh_result.get("diagnostics") or {}).get("apify_runs") or 0))
            summary["apify_runs"] += apify_runs
            remaining_apify_runs = max(0, remaining_apify_runs - apify_runs)
            summary["brands_refreshed"] += 1
            if int(refresh_result.get("preview_count") or 0) > 0:
                summary["brands_updated_with_previews"] += 1
            elif refresh_result.get("preserved_existing_previews") or existing_preview_count > 0:
                summary["brands_preserved"] += 1
            else:
                summary["brands_no_previews"] += 1
        except MetaAdsPlatformLimitError as exc:
            apify_runs = max(1, int(getattr(exc, "apify_runs", 1) or 1))
            summary["apify_runs"] += apify_runs
            remaining_apify_runs = max(0, remaining_apify_runs - apify_runs)
            summary["platform_limit_reached"] = True
            summary["refresh_allowed"] = False
            summary["errors"].append(APIFY_USAGE_LIMIT_MESSAGE)
            break
        except MetaAdsServiceError as exc:
            error_message = str(exc)
            apify_runs = max(1, int(getattr(exc, "apify_runs", 1) or 1))
            summary["apify_runs"] += apify_runs
            remaining_apify_runs = max(0, remaining_apify_runs - apify_runs)
            summary["service_unavailable"] = True
            summary["refresh_allowed"] = False
            if "timed out" in error_message.lower():
                summary["timeouts"] += 1
            summary["errors"].append(f"{brand['name']}: {error_message}")
            break
        except Exception as exc:
            error_message = str(exc)
            summary["apify_runs"] += allowed_attempts
            remaining_apify_runs = max(0, remaining_apify_runs - allowed_attempts)
            summary["brands_failed"] += 1
            summary["errors"].append(f"{brand['name']}: {error_message}")
            try:
                save_seo_brand_cache_error(brand, error_message, country=SEO_CANDIDATE_TEST_COUNTRY)
            except Exception as save_exc:
                print("SEO stale cache error save failed:", str(save_exc))

    summary["remaining_apify_runs"] = max(
        0,
        reservation["remaining_calls"] - summary["apify_runs"],
    )
    if summary["platform_limit_reached"]:
        run_status = "apify_limit_reached"
        run_error = APIFY_USAGE_LIMIT_MESSAGE
    elif summary["service_unavailable"]:
        run_status = "apify_unavailable"
        run_error = "; ".join(summary["errors"][:3])
    elif summary["brands_failed"]:
        run_status = "completed_with_errors"
        run_error = "; ".join(summary["errors"][:3])
    else:
        run_status = "completed"
        run_error = ""
    finish_seo_automation_run(
        run_id,
        run_status,
        {
            "tested": 0,
            "failed": summary["brands_failed"],
            "apify_runs": summary["apify_runs"],
            "brands_attempted": summary["brands_attempted"],
            "platform_limit_reached": summary["platform_limit_reached"],
        },
        run_error,
    )
    return summary


def refresh_stale_seo_brand_caches(limit: int = SEO_STALE_CACHE_REFRESH_LIMIT):
    return refresh_seo_brand_cache_targets(
        get_stale_seo_cache_refresh_targets(limit=limit),
        run_type="seo_cache_refresh_stale",
    )


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
                canonical_url=absolute_url("/"),
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
                canonical_url=absolute_url("/"),
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
                    canonical_url=absolute_url("/"),
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
                canonical_url=absolute_url("/"),
            )

        searched = True
        normalized = normalize_query(brand)
        search_country = PUBLIC_SEARCH_COUNTRY

        try:
            cached = get_cached_results(normalized, country=search_country)
            is_cached = cached is not None

            if is_cached:
                ads = cached
                estimated_cost = CACHED_SEARCH_ESTIMATED_COST
            else:
                service = get_meta_ads_service()
                ads = service.search_ads(
                    brand=brand,
                    country=search_country,
                    max_results=50,
                )

                relevance_filter = get_ad_relevance_filter()
                ads = relevance_filter.filter_ads(
                    search_brand=brand,
                    ads=ads,
                )

                save_cached_results(brand, normalized, ads, country=search_country)
                estimated_cost = FRESH_SEARCH_ESTIMATED_COST

            try:
                snapshot_brand = get_brand_by_slug(slugify_brand_name(brand))
                if snapshot_brand:
                    save_last_good_snapshot_from_search(snapshot_brand, ads, brand)
            except Exception as snapshot_exc:
                print("SEO snapshot save from search failed:", str(snapshot_exc))

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
        canonical_url=absolute_url("/"),
    )


@app.route("/sample-results")
def sample_results():
    fallback_brands = [
        {
            "name": "Manscaped",
            "usefulness": "See how a direct-to-consumer brand can test distinct hooks while keeping its core offer recognizable.",
            "ads": [
                {
                    "days_running": 84,
                    "start_date": "January 8, 2026",
                    "creative_angle": "Problem-solution framing for a familiar grooming frustration.",
                    "marketer_learning": "Lead with a specific pain point before introducing the product as the simple fix.",
                },
                {
                    "days_running": 57,
                    "start_date": "February 4, 2026",
                    "creative_angle": "Bundle value positioned as a complete routine.",
                    "marketer_learning": "Packaging complementary products together can make the offer feel easier to understand and compare.",
                },
            ],
        },
        {
            "name": "Gymshark",
            "usefulness": "Compare creative approaches that sell both product benefits and an aspirational fitness identity.",
            "ads": [
                {
                    "days_running": 73,
                    "start_date": "January 19, 2026",
                    "creative_angle": "Performance apparel shown through a training-focused message.",
                    "marketer_learning": "Connect functional product details to the activity and outcome the audience cares about.",
                },
                {
                    "days_running": 46,
                    "start_date": "February 15, 2026",
                    "creative_angle": "Social-proof-led community positioning.",
                    "marketer_learning": "A strong community signal can reinforce belonging without replacing a clear product benefit.",
                },
            ],
        },
        {
            "name": "Ridge",
            "usefulness": "Study how a compact product can be differentiated through benefits, objections, and offer structure.",
            "ads": [
                {
                    "days_running": 91,
                    "start_date": "January 1, 2026",
                    "creative_angle": "Before-and-after contrast with a bulky traditional wallet.",
                    "marketer_learning": "A visual contrast can communicate the main benefit faster than a long feature list.",
                },
                {
                    "days_running": 62,
                    "start_date": "January 30, 2026",
                    "creative_angle": "Risk reversal built around durability and warranty.",
                    "marketer_learning": "Answering the durability objection directly can make a premium price easier to justify.",
                },
            ],
        },
    ]

    sample_brands = get_durable_sample_result_brands()
    selected_names = {brand["name"].strip().lower() for brand in sample_brands}
    for fallback_brand in fallback_brands:
        if len(sample_brands) >= SAMPLE_RESULTS_BRAND_LIMIT:
            break
        if fallback_brand["name"].strip().lower() in selected_names:
            continue
        sample_brands.append(fallback_brand)
        selected_names.add(fallback_brand["name"].strip().lower())

    return render_template(
        "sample_results.html",
        sample_brands=sample_brands,
        seo_public_page=True,
        canonical_url=absolute_url("/sample-results"),
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
    return render_template(
        "pricing.html",
        usage_message=usage_message,
        canonical_url=absolute_url("/pricing"),
    )


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


@app.route("/admin/seo-brand-cache")
@admin_required
def seo_brand_cache_admin():
    cache_admin_data = get_seo_brand_cache_admin_rows()
    refresh_safety = get_seo_refresh_safety_status()
    refresh_estimates = {
        "stale": get_stale_seo_cache_refresh_targets()["eligible_count"],
        "missing_snapshots": get_missing_snapshot_refresh_targets()["eligible_count"],
        "missing_images": get_missing_snapshot_image_refresh_targets()["eligible_count"],
        "missing_durable_images": get_missing_durable_image_targets()["eligible_count"],
    }
    return render_template(
        "seo_brand_cache_admin.html",
        cache_rows=cache_admin_data["rows"],
        cache_summary=cache_admin_data["summary"],
        refresh_safety=refresh_safety,
        refresh_estimates=refresh_estimates,
    )


@app.route("/admin/seo-market-audit")
@admin_required
def seo_market_audit():
    report = get_seo_market_audit_report()
    return render_template(
        "seo_market_audit.html",
        report=report,
        expected_country=SEO_CANDIDATE_TEST_COUNTRY,
    )


@app.route("/admin/seo-engine-2", methods=["GET", "POST"])
@admin_required
def seo_engine_2_admin():
    overview = build_seo_engine_2_overview()
    dry_run_plan = None
    maintenance_summary = None
    if request.method == "POST":
        action = (request.form.get("action") or "dry_run").strip()
        if action == "maintenance_repair":
            maintenance_summary = run_seo_engine_2_maintenance(
                max_brands=request.form.get("max_brands"),
                overview=overview,
            )
            flash(format_seo_engine_2_maintenance_summary(maintenance_summary))
            overview = build_seo_engine_2_overview()
        else:
            dry_run_plan = build_seo_engine_2_dry_run(overview)
            flash("SEO Engine 2.0 dry run complete. No records were changed and no paid APIs were called.")

    return render_template(
        "seo_engine_2.html",
        overview=overview,
        dry_run_plan=dry_run_plan,
        maintenance_summary=maintenance_summary,
        maintenance_limit_options=get_seo_engine_2_maintenance_limit_options(),
        default_maintenance_limit=SEO_ENGINE_2_MAINTENANCE_DEFAULT_LIMIT,
    )


def isoformat_or_none(value):
    return value.isoformat() if value else None


@app.route("/admin/seo-cache-diagnostics")
@admin_required
def seo_cache_diagnostics():
    diagnostics = {
        "slugs": list(SEO_CACHE_DIAGNOSTIC_SLUGS),
        "cache": {},
        "brands": {},
    }
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand_slug, brand_name, search_query, country, ads_json,
                           result_count, preview_count, fetched_at, expires_at,
                           refresh_status, last_error, created_at, updated_at
                    FROM seo_brand_ad_cache
                    WHERE brand_slug = ANY(%s)
                    ORDER BY brand_slug
                    """,
                    (list(SEO_CACHE_DIAGNOSTIC_SLUGS),),
                )
                for row in cur.fetchall():
                    ads = []
                    try:
                        ads = json.loads(row[4] or "[]")
                    except (TypeError, ValueError):
                        ads = []
                    if not isinstance(ads, list):
                        ads = []

                    first_ads = ads[:SEO_BRAND_PREVIEW_COUNT]
                    diagnostics["cache"][row[0]] = {
                        "row_count": 1,
                        "brand_slug": row[0],
                        "brand_name": row[1],
                        "search_query": row[2],
                        "country": row[3],
                        "result_count": int(row[5] or 0),
                        "preview_count": int(row[6] or 0),
                        "ads_json_count": len(ads),
                        "first_3_ad_ids": [ad.get("ad_id") for ad in first_ads if isinstance(ad, dict)],
                        "first_3_snapshot_urls": [
                            ad.get("snapshot_url") for ad in first_ads if isinstance(ad, dict)
                        ],
                        "fetched_at": isoformat_or_none(row[7]),
                        "expires_at": isoformat_or_none(row[8]),
                        "refresh_status": row[9],
                        "last_error": row[10],
                        "created_at": isoformat_or_none(row[11]),
                        "updated_at": isoformat_or_none(row[12]),
                    }

                cur.execute(
                    """
                    SELECT brand_slug, brand_name, search_query, is_published,
                           published_at, updated_at
                    FROM seo_brands
                    WHERE brand_slug = ANY(%s)
                    ORDER BY brand_slug
                    """,
                    (list(SEO_CACHE_DIAGNOSTIC_SLUGS),),
                )
                for row in cur.fetchall():
                    diagnostics["brands"][row[0]] = {
                        "brand_slug": row[0],
                        "brand_name": row[1],
                        "search_query": row[2],
                        "is_published": bool(row[3]),
                        "published_at": isoformat_or_none(row[4]),
                        "updated_at": isoformat_or_none(row[5]),
                    }
    finally:
        conn.close()

    for slug in SEO_CACHE_DIAGNOSTIC_SLUGS:
        diagnostics["cache"].setdefault(
            slug,
            {
                "row_count": 0,
                "brand_slug": slug,
                "preview_count": 0,
                "ads_json_count": 0,
                "first_3_ad_ids": [],
                "first_3_snapshot_urls": [],
                "refresh_status": None,
            },
        )
        diagnostics["brands"].setdefault(
            slug,
            {
                "brand_slug": slug,
                "row_count": 0,
                "is_published": False,
            },
        )

    return jsonify(diagnostics)


@app.route("/admin/seo-cache-read-diagnostics")
@admin_required
def seo_cache_read_diagnostics():
    diagnostics = {}
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for slug in SEO_CACHE_DIAGNOSTIC_SLUGS:
                    raw_cache = {
                        "row_exists": False,
                        "preview_count": 0,
                        "ads_json_count": 0,
                        "refresh_status": None,
                        "last_error": None,
                    }
                    cur.execute(
                        """
                        SELECT ads_json, preview_count, refresh_status, last_error
                        FROM seo_brand_ad_cache
                        WHERE brand_slug = %s
                        """,
                        (slug,),
                    )
                    row = cur.fetchone()
                    if row:
                        ads = []
                        try:
                            ads = json.loads(row[0] or "[]")
                        except (TypeError, ValueError):
                            ads = []
                        if not isinstance(ads, list):
                            ads = []
                        raw_cache = {
                            "row_exists": True,
                            "preview_count": int(row[1] or 0),
                            "ads_json_count": len(ads),
                            "refresh_status": row[2],
                            "last_error": row[3],
                        }

                    brand = get_brand_by_slug(slug)
                    brand_summary = {
                        "brand_exists": bool(brand),
                        "brand_name": brand.get("name") if brand else None,
                        "brand_slug": brand.get("slug") if brand else None,
                        "is_published": bool(brand.get("is_published", True)) if brand else False,
                    }

                    helper_summary = {
                        "helper_ads_count": 0,
                        "helper_status": None,
                        "helper_error": None,
                        "first_3_helper_ad_ids": [],
                        "first_3_helper_snapshot_urls": [],
                        "first_3_helper_media_urls": [],
                    }
                    try:
                        if brand:
                            seo_ads_cache = get_cached_seo_brand_ads(
                                brand["slug"],
                                fallback_brand_name=brand["name"],
                            )
                            helper_ads = seo_ads_cache.get("ads") or []
                            first_ads = helper_ads[:SEO_BRAND_PREVIEW_COUNT]
                            helper_summary = {
                                "helper_ads_count": len(helper_ads),
                                "helper_status": seo_ads_cache.get("refresh_status"),
                                "helper_error": seo_ads_cache.get("last_error"),
                                "first_3_helper_ad_ids": [
                                    ad.get("ad_id") for ad in first_ads if isinstance(ad, dict)
                                ],
                                "first_3_helper_snapshot_urls": [
                                    ad.get("snapshot_url") for ad in first_ads if isinstance(ad, dict)
                                ],
                                "first_3_helper_media_urls": [
                                    ad.get("media_url") for ad in first_ads if isinstance(ad, dict)
                                ],
                            }
                    except Exception as exc:
                        helper_summary["helper_error"] = str(exc)

                    diagnostics[slug] = {
                        "slug_requested": slug,
                        "brand": brand_summary,
                        "raw_cache": raw_cache,
                        "public_helper": helper_summary,
                    }
    finally:
        conn.close()

    return jsonify(diagnostics)


@app.route("/admin/seo-brand-candidates", methods=["GET", "POST"])
@admin_required
def seo_brand_candidates():
    if request.method == "POST":
        brand_name = (request.form.get("brand_name") or "").strip()
        category = (request.form.get("category") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        source_type = (request.form.get("source_type") or "").strip()
        source_name = (request.form.get("source_name") or "").strip()

        if not brand_name:
            flash("Enter a brand name to add.")
        else:
            try:
                save_seo_brand_candidate(
                    brand_name,
                    category=category,
                    notes=notes,
                    source_type=source_type,
                    source_name=source_name,
                )
                flash(f"{brand_name} added as an SEO brand candidate.")
                return redirect(url_for("seo_brand_candidates"))
            except Exception as exc:
                flash(f"Could not save candidate: {str(exc)}")

    selected_sort = normalize_candidate_sort(request.args.get("sort") or "qualified_first")
    selected_view = normalize_candidate_view(request.args.get("view") or "active")
    candidate_rows = get_seo_brand_candidate_rows(sort_key=selected_sort, view_key=selected_view)
    automation_settings = get_seo_automation_settings()
    automation_usage = get_seo_automation_daily_usage()

    return render_template(
        "seo_brand_candidates.html",
        candidate_rows=candidate_rows,
        sort_options=get_candidate_sort_options(),
        selected_sort=selected_sort,
        view_options=get_candidate_view_options(),
        selected_view=selected_view,
        max_results=automation_settings["max_results_per_test"],
        automation_settings=automation_settings,
        automation_usage=automation_usage,
    )


@app.route("/admin/seo-candidate-generator", methods=["GET", "POST"])
@admin_required
def seo_candidate_generator():
    generation_options = [25, 50, 100]
    selected_categories = []
    generation_result = None

    if request.method == "POST":
        selected_categories = [
            category
            for category in request.form.getlist("categories")
            if category in SEO_CANDIDATE_SEEDS
        ]
        try:
            requested_count = int(request.form.get("generate_count") or 25)
        except (TypeError, ValueError):
            requested_count = 25

        if requested_count not in generation_options:
            requested_count = 25

        if not selected_categories:
            flash("Select at least one seed category.")
        else:
            seed_rows = get_seed_brands_for_categories(selected_categories)[:requested_count]
            generation_result = insert_seed_seo_brand_candidates(seed_rows)
            flash(
                f"Candidate generation complete. Added: {generation_result['added']}. "
                f"Skipped existing: {generation_result['skipped']}. "
                f"Total candidates in queue: {generation_result['total']}."
            )

    category_rows = [
        {
            "name": category,
            "count": len(SEO_CANDIDATE_SEEDS.get(category, [])),
            "brands": SEO_CANDIDATE_SEEDS.get(category, [])[:8],
        }
        for category in get_seed_categories()
    ]

    return render_template(
        "seo_candidate_generator.html",
        category_rows=category_rows,
        generation_options=generation_options,
        selected_categories=selected_categories,
        generation_result=generation_result,
        total_candidates=count_seo_brand_candidates(),
    )


@app.route("/admin/seo-brand-candidates/<int:candidate_id>/test", methods=["POST"])
@admin_required
def test_seo_brand_candidate(candidate_id):
    candidate = get_seo_brand_candidate(candidate_id)
    if not candidate:
        abort(404)

    try:
        ads, diagnostics = fetch_candidate_validation_ads(
            candidate,
            max_results=SEO_CANDIDATE_TEST_MAX_RESULTS,
            max_variant_attempts=SEO_CANDIDATE_MAX_QUERY_VARIANTS,
        )
        score, signals = calculate_seo_candidate_quality(candidate["brand_name"], ads)
        signals["diagnostics"] = diagnostics
        preview_count = min(len(ads), SEO_BRAND_PREVIEW_COUNT)
        status = "qualified" if preview_count > 0 else "inconclusive"
        quality_status = "needs_review" if preview_count > 0 else "inconclusive"
        update_seo_brand_candidate_quality_result(
            candidate_id,
            status=status,
            result_count=len(ads),
            preview_count=preview_count,
            quality_score=score,
            quality_status=quality_status,
            quality_signals=signals,
        )

        if preview_count > 0:
            flash(
                f"{candidate['brand_name']} qualified with {preview_count} preview ads "
                f"and a quality score of {score}."
            )
        else:
            flash(
                f"{candidate['brand_name']} search inconclusive: "
                f"{diagnostics['rejection_reason']}."
            )

    except MetaAdsServiceError as exc:
        error_message = str(exc)
        timed_out = "timed out" in error_message.lower()
        update_seo_brand_candidate_quality_result(
            candidate_id,
            status="failed",
            result_count=0,
            preview_count=0,
            quality_score=0,
            quality_status="failed",
            quality_signals={
                "error": error_message,
                "diagnostics": {
                    "country": SEO_CANDIDATE_TEST_COUNTRY,
                    "timed_out": timed_out,
                    "rejection_reason": "timeout" if timed_out else "search_error",
                },
            },
            error_message=error_message,
        )
        flash(f"{candidate['brand_name']} test failed: {error_message}")

    except Exception as exc:
        error_message = str(exc)
        print("SEO brand candidate test error:", error_message)
        update_seo_brand_candidate_quality_result(
            candidate_id,
            status="failed",
            result_count=0,
            preview_count=0,
            quality_score=0,
            quality_status="failed",
            quality_signals={
                "error": error_message,
                "diagnostics": {
                    "country": SEO_CANDIDATE_TEST_COUNTRY,
                    "timed_out": False,
                    "rejection_reason": "search_error",
                },
            },
            error_message=error_message,
        )
        flash(f"{candidate['brand_name']} test failed: {error_message}")

    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/backfill-quality-scores", methods=["POST"])
@admin_required
def backfill_seo_candidate_quality_scores():
    try:
        result = backfill_seo_candidate_quality_scores_from_cache()
        message = (
            f"Quality score backfill complete: {result['updated']} updated, "
            f"{result['skipped']} skipped, {len(result['errors'])} errors."
        )
        if result["errors"]:
            message += f" Details: {'; '.join(result['errors'][:3])}"
            if len(result["errors"]) > 3:
                message += f"; plus {len(result['errors']) - 3} more."
        flash(message)
    except Exception as exc:
        flash(f"Quality score backfill failed: {str(exc)}")

    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/<int:candidate_id>/promote", methods=["POST"])
@admin_required
def promote_seo_brand_candidate_route(candidate_id):
    candidate = get_seo_brand_candidate(candidate_id)
    if not candidate:
        abort(404)

    if not candidate.get("is_qualified") or candidate.get("quality_status") == "rejected":
        flash(f"{candidate['brand_name']} is not qualified yet.")
        return redirect(url_for("seo_brand_candidates"))

    if candidate.get("published_brand_id"):
        flash(f"{candidate['brand_name']} is already promoted.")
        return redirect(url_for("seo_brand_candidates"))

    try:
        promote_seo_brand_candidate(candidate)
    except Exception as exc:
        print("SEO brand promotion error:", str(exc))
        flash(f"{candidate['brand_name']} promotion failed: {str(exc)}")
        return redirect(url_for("seo_brand_candidates"))

    brand = get_brand_by_slug(candidate["brand_slug"]) or build_seo_brand_defaults(
        candidate["brand_name"],
        candidate["brand_slug"],
        category=candidate.get("category") or "",
    )

    try:
        refresh_result = refresh_seo_brand_ads_cache_for_candidate(brand, candidate)
        preview_count = refresh_result["preview_count"]
        result_count = refresh_result["result_count"]
        diagnostics = refresh_result.get("diagnostics") or {}
        if preview_count > 0:
            flash(
                f"{candidate['brand_name']} promoted successfully. "
                f"SEO cache refreshed with {preview_count} preview ads."
            )
        else:
            flash(
                f"{candidate['brand_name']} promoted successfully, "
                f"but candidate-style cache refresh found no preview ads from "
                f"{result_count} filtered results. Reason: "
                f"{diagnostics.get('rejection_reason') or 'no_preview_ads'}."
            )
    except MetaAdsServiceError as exc:
        error_message = str(exc)
        try:
            save_seo_brand_cache_error(brand, error_message, country=SEO_CANDIDATE_TEST_COUNTRY)
        except Exception as save_exc:
            print("SEO brand cache error save failed:", str(save_exc))
        flash(
            f"{candidate['brand_name']} promoted successfully, "
            f"but cache refresh failed: {error_message}"
        )
    except Exception as exc:
        error_message = str(exc)
        print("SEO brand promote cache refresh error:", error_message)
        try:
            save_seo_brand_cache_error(brand, error_message, country=SEO_CANDIDATE_TEST_COUNTRY)
        except Exception as save_exc:
            print("SEO brand cache error save failed:", str(save_exc))
        flash(
            f"{candidate['brand_name']} promoted successfully, "
            f"but cache refresh failed: {error_message}"
        )

    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/bulk-promote", methods=["POST"])
@admin_required
def bulk_promote_seo_brand_candidates():
    candidate_ids = []
    for raw_id in request.form.getlist("candidate_ids"):
        try:
            candidate_ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue

    if not candidate_ids:
        flash("Select at least one qualified candidate to promote.")
        return redirect(url_for("seo_brand_candidates"))

    promoted_count = 0
    skipped_count = 0
    errors = []

    for candidate_id in candidate_ids:
        candidate = get_seo_brand_candidate(candidate_id)
        if not candidate:
            skipped_count += 1
            continue

        if (
            not candidate.get("is_qualified")
            or candidate.get("published_brand_id")
            or candidate.get("quality_status") == "rejected"
        ):
            skipped_count += 1
            continue

        try:
            promote_seo_brand_candidate(candidate)
            promoted_count += 1
        except Exception as exc:
            errors.append(f"{candidate['brand_name']}: {str(exc)}")

    message = (
        f"Bulk promotion complete: {promoted_count} promoted, "
        f"{skipped_count} skipped, {len(errors)} errors."
    )
    if errors:
        message += f" Details: {'; '.join(errors[:3])}"
        if len(errors) > 3:
            message += f"; plus {len(errors) - 3} more."
    flash(message)

    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/<int:candidate_id>/archive", methods=["POST"])
@admin_required
def archive_seo_brand_candidate_route(candidate_id):
    candidate_name = archive_seo_brand_candidate(candidate_id)
    if candidate_name:
        flash(f"{candidate_name} archived. It is hidden from the default active candidate list.")
    else:
        flash("Candidate not found.")
    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/<int:candidate_id>/delete", methods=["POST"])
@admin_required
def delete_seo_brand_candidate_route(candidate_id):
    candidate_name = delete_seo_brand_candidate(candidate_id)
    if candidate_name:
        flash(f"{candidate_name} candidate deleted. Published SEO brands and cache were not changed.")
    else:
        flash("Candidate not found.")
    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/bulk-delete", methods=["POST"])
@admin_required
def bulk_delete_seo_brand_candidates_route():
    deleted_count = bulk_delete_seo_brand_candidates(request.form.getlist("delete_candidate_ids"))
    flash(
        f"Deleted {deleted_count} candidate record"
        f"{'' if deleted_count == 1 else 's'}. Published SEO brands and cache were not changed."
    )
    return redirect(url_for("seo_brand_candidates"))


def process_one_seo_automation_candidate(candidate, settings, max_variant_attempts: int):
    ads = None
    try:
        ads, diagnostics = fetch_candidate_validation_ads(
            candidate,
            max_results=settings["max_results_per_test"],
            max_variant_attempts=max_variant_attempts,
        )

        score, signals = calculate_seo_candidate_quality(candidate["brand_name"], ads)
        signals["diagnostics"] = diagnostics
        result_count = len(ads)
        preview_count = min(result_count, SEO_BRAND_PREVIEW_COUNT)

        if preview_count == 0:
            update_seo_brand_candidate_quality_result(
                candidate["id"],
                status="inconclusive",
                result_count=result_count,
                preview_count=preview_count,
                quality_score=score,
                quality_status="inconclusive",
                quality_signals=signals,
            )
            return "inconclusive", (
                f"{candidate['brand_name']} search inconclusive: "
                f"{diagnostics['rejection_reason']}. Quality score {score}."
            ), diagnostics

        if score >= settings["auto_publish_threshold"]:
            promote_seo_brand_candidate(candidate)
            brand = get_brand_by_slug(candidate["brand_slug"]) or build_seo_brand_defaults(
                candidate["brand_name"],
                candidate["brand_slug"],
                category=candidate.get("category") or "",
            )
            save_seo_brand_ads_cache(
                brand,
                ads,
                country=SEO_CANDIDATE_TEST_COUNTRY,
                result_count=result_count,
            )
            update_seo_brand_candidate_quality_result(
                candidate["id"],
                status="qualified",
                result_count=result_count,
                preview_count=preview_count,
                quality_score=score,
                quality_status="auto_published",
                quality_signals=signals,
                auto_published=True,
            )
            return "auto_published", (
                f"{candidate['brand_name']} auto-published with score {score} "
                f"and {preview_count} cached previews."
            ), diagnostics

        if score >= settings["review_threshold"]:
            update_seo_brand_candidate_quality_result(
                candidate["id"],
                status="qualified",
                result_count=result_count,
                preview_count=preview_count,
                quality_score=score,
                quality_status="needs_review",
                quality_signals=signals,
            )
            return "sent_to_review", (
                f"{candidate['brand_name']} sent to review with score {score} "
                f"and {preview_count} previews."
            ), diagnostics

        update_seo_brand_candidate_quality_result(
            candidate["id"],
            status="qualified",
            result_count=result_count,
            preview_count=preview_count,
            quality_score=score,
            quality_status="rejected",
            quality_signals=signals,
        )
        return "rejected", (
            f"{candidate['brand_name']} rejected with score {score} "
            f"and {preview_count} previews."
        ), diagnostics
    finally:
        if ads is not None:
            del ads
        gc.collect()


def process_next_seo_brand_candidate_response():
    settings = get_seo_automation_settings()
    usage = get_seo_automation_daily_usage()

    if settings["kill_switch_enabled"]:
        flash("SEO automation is stopped because the kill switch is enabled.")
        return redirect(url_for("seo_brand_candidates"))

    if not settings["is_enabled"]:
        flash("SEO automation is disabled.")
        return redirect(url_for("seo_brand_candidates"))

    remaining_tests = settings["daily_test_limit"] - usage["tests"]
    remaining_apify_runs = settings["daily_apify_run_limit"] - usage["apify_runs"]

    if min(remaining_tests, remaining_apify_runs) <= 0:
        flash("SEO automation daily limit reached. No candidates were tested.")
        return redirect(url_for("seo_brand_candidates"))

    candidates = get_next_seo_automation_candidates(1)
    if not candidates:
        flash("No untested or failed SEO candidates are ready for automation.")
        return redirect(url_for("seo_brand_candidates"))

    candidate = candidates[0]
    run_id = create_seo_automation_run("process_next")
    counts = {
        "tested": 0,
        "auto_published": 0,
        "sent_to_review": 0,
        "rejected": 0,
        "failed": 0,
        "inconclusive": 0,
        "apify_runs": 0,
    }

    try:
        counts["tested"] = 1
        max_variant_attempts = min(
            SEO_CANDIDATE_MAX_QUERY_VARIANTS,
            max(1, remaining_apify_runs),
        )
        outcome, message, diagnostics = process_one_seo_automation_candidate(
            candidate,
            settings,
            max_variant_attempts=max_variant_attempts,
        )
        if outcome in counts:
            counts[outcome] += 1
        counts["apify_runs"] = int(diagnostics.get("apify_runs") or 1)
        run_status = "completed"
        run_error = ""
    except Exception as exc:
        counts["tested"] = 1
        counts["failed"] = 1
        counts["apify_runs"] = 1
        run_status = "completed_with_errors"
        run_error = f"{candidate['brand_name']}: {str(exc)}"
        timed_out = "timed out" in str(exc).lower()
        try:
            update_seo_brand_candidate_quality_result(
                candidate["id"],
                status="failed",
                result_count=0,
                preview_count=0,
                quality_score=0,
                quality_status="failed",
                quality_signals={
                    "error": str(exc),
                    "diagnostics": {
                        "country": SEO_CANDIDATE_TEST_COUNTRY,
                        "timed_out": timed_out,
                        "rejection_reason": "timeout" if timed_out else "search_error",
                    },
                },
                error_message=str(exc),
            )
        except Exception as update_exc:
            print("SEO automation candidate error save failed:", str(update_exc))
        message = f"{candidate['brand_name']} failed: {str(exc)}"

    finish_seo_automation_run(
        run_id,
        run_status,
        counts,
        error=run_error,
    )

    flash(f"SEO automation processed one candidate. {message}")

    return redirect(url_for("seo_brand_candidates"))


@app.route("/admin/seo-brand-candidates/process-next", methods=["POST"])
@admin_required
def process_next_seo_brand_candidate():
    return process_next_seo_brand_candidate_response()


@app.route("/admin/seo-brand-candidates/run-next", methods=["POST"])
@admin_required
def run_next_seo_brand_candidates():
    return process_next_seo_brand_candidate_response()


@app.route("/admin/seo-brand-cache/<brand_slug>/refresh", methods=["POST"])
@admin_required
def refresh_seo_brand_cache(brand_slug):
    brand = get_brand_by_slug(brand_slug)
    if not brand:
        abort(404)

    country = SEO_CANDIDATE_TEST_COUNTRY
    reservation = reserve_seo_refresh_run(
        "seo_cache_refresh_individual",
        SEO_CANDIDATE_MAX_QUERY_VARIANTS,
    )
    if reservation["blocked_reason"]:
        error_message = (
            APIFY_USAGE_LIMIT_MESSAGE
            if reservation["blocked_reason"] == "apify_limit"
            else "SEO refresh daily safety limit reached. No refresh was attempted."
        )
        if request.form.get("return_to_admin") == "1":
            flash(error_message)
            return redirect(url_for("seo_brand_cache_admin"))
        return jsonify(
            {
                "ok": False,
                "brand_slug": brand["slug"],
                "brand_name": brand["name"],
                "error": error_message,
            }
        ), 429

    run_id = reservation["run_id"]
    max_variant_attempts = reservation["reserved_calls"]

    try:
        refresh_result = refresh_published_seo_brand_ads_cache(
            brand,
            max_variant_attempts=max_variant_attempts,
        )
        result_count = refresh_result["result_count"]
        preview_count = refresh_result["preview_count"]
        diagnostics = refresh_result.get("diagnostics") or {}
        apify_runs = max(1, int(diagnostics.get("apify_runs") or 1))
        finish_seo_automation_run(
            run_id,
            "completed",
            {
                "apify_runs": apify_runs,
                "brands_attempted": 1,
            },
        )
        preserved_existing_previews = bool(refresh_result.get("preserved_existing_previews"))
        diagnostic_summary = {
            "raw_apify_count": int(diagnostics.get("raw_result_count") or 0),
            "normalized_count": int(diagnostics.get("normalized_count") or 0),
            "relevance_filtered_count": int(diagnostics.get("relevance_filtered_count") or 0),
            "selected_query": diagnostics.get("selected_query"),
            "final_preview_count": int(diagnostics.get("final_preview_count") or preview_count or 0),
            "preserved_existing_previews": preserved_existing_previews,
        }

        if request.form.get("return_to_admin") == "1":
            flash(
                f"{brand['name']} SEO cache refreshed. {result_count} filtered ads, "
                f"{preview_count} new previews. Raw {diagnostic_summary['raw_apify_count']}, "
                f"normalized {diagnostic_summary['normalized_count']}, "
                f"relevance-filtered {diagnostic_summary['relevance_filtered_count']}, "
                f"query {diagnostic_summary['selected_query'] or brand['search_query']}."
                + (" Existing previews were preserved." if preserved_existing_previews else "")
            )
            return redirect(url_for("seo_brand_cache_admin"))

        return jsonify(
            {
                "ok": True,
                "brand_slug": brand["slug"],
                "brand_name": brand["name"],
                "result_count": result_count,
                "preview_count": preview_count,
                "expires_in_hours": SEO_BRAND_CACHE_TTL_HOURS,
                "diagnostics": diagnostic_summary,
            }
        )

    except MetaAdsPlatformLimitError as exc:
        apify_runs = max(1, int(getattr(exc, "apify_runs", 1) or 1))
        finish_seo_automation_run(
            run_id,
            "apify_limit_reached",
            {
                "apify_runs": apify_runs,
                "brands_attempted": 1,
                "platform_limit_reached": True,
            },
            APIFY_USAGE_LIMIT_MESSAGE,
        )
        if request.form.get("return_to_admin") == "1":
            flash(APIFY_USAGE_LIMIT_MESSAGE)
            return redirect(url_for("seo_brand_cache_admin"))
        return jsonify(
            {
                "ok": False,
                "brand_slug": brand["slug"],
                "brand_name": brand["name"],
                "error": APIFY_USAGE_LIMIT_MESSAGE,
            }
        ), 429

    except MetaAdsServiceError as exc:
        error_message = str(exc)
        apify_runs = max(1, int(getattr(exc, "apify_runs", 1) or 1))
        finish_seo_automation_run(
            run_id,
            "apify_unavailable",
            {
                "apify_runs": apify_runs,
                "brands_attempted": 1,
            },
            error_message,
        )

        if request.form.get("return_to_admin") == "1":
            flash(f"{brand['name']} SEO cache refresh stopped because Apify was unavailable: {error_message}")
            return redirect(url_for("seo_brand_cache_admin"))

        return jsonify(
            {
                "ok": False,
                "brand_slug": brand["slug"],
                "brand_name": brand["name"],
                "error": error_message,
            }
        ), 502

    except Exception as exc:
        error_message = str(exc)
        print("SEO brand cache refresh error:", error_message)
        finish_seo_automation_run(
            run_id,
            "completed_with_errors",
            {
                "apify_runs": max_variant_attempts,
                "brands_attempted": 1,
                "failed": 1,
            },
            error_message,
        )
        try:
            save_seo_brand_cache_error(brand, error_message, country=country)
        except Exception as save_exc:
            print("SEO brand cache error save failed:", str(save_exc))

        if request.form.get("return_to_admin") == "1":
            flash(f"{brand['name']} SEO cache refresh failed: {error_message}")
            return redirect(url_for("seo_brand_cache_admin"))

        return jsonify(
            {
                "ok": False,
                "brand_slug": brand["slug"],
                "brand_name": brand["name"],
                "error": error_message,
            }
        ), 500


@app.route("/admin/seo-brand-cache/refresh-stale", methods=["POST"])
@admin_required
def refresh_stale_seo_brand_cache():
    try:
        summary = refresh_stale_seo_brand_caches()
        flash(format_seo_cache_refresh_summary("Stale SEO cache refresh", summary))
    except Exception as exc:
        flash(f"Stale SEO cache refresh failed: {str(exc)}")

    return redirect(url_for("seo_brand_cache_admin"))


def format_seo_cache_refresh_summary(label: str, summary):
    if summary.get("blocked_reason"):
        return f"{label} did not run: {summary['blocked_reason']}"
    if summary.get("platform_limit_reached"):
        return (
            f"{APIFY_USAGE_LIMIT_MESSAGE} Attempted {summary['brands_attempted']} brand(s) "
            f"and {summary['apify_runs']} Apify call(s)."
        )
    message = (
        f"{label} complete. Estimated {summary['estimated_brands_selected']} eligible brand(s), "
        f"maximum {summary['max_brands_attempted']} this click, attempted {summary['brands_attempted']}, "
        f"refreshed {summary['brands_refreshed']}, updated with previews "
        f"{summary['brands_updated_with_previews']}, preserved {summary['brands_preserved']}, "
        f"failed {summary['brands_failed']}, timeouts {summary['timeouts']}, "
        f"Apify calls {summary['apify_runs']}; remaining today {summary['remaining_apify_runs']} "
        f"of {summary['daily_apify_run_limit']}."
    )
    if summary.get("service_unavailable"):
        message += " Batch stopped because Apify was unavailable."
    if summary["brands_no_previews"]:
        message += f" No previews found for {summary['brands_no_previews']}."
    if summary["errors"]:
        message += f" Errors: {'; '.join(summary['errors'][:3])}"
        if len(summary["errors"]) > 3:
            message += f"; plus {len(summary['errors']) - 3} more."
    return message


@app.route("/admin/seo-brand-cache/refresh-selected", methods=["POST"])
@admin_required
def refresh_selected_seo_brand_cache():
    selected_slugs = request.form.getlist("brand_slugs")
    if not selected_slugs:
        flash("Select at least one published SEO brand to refresh.")
        return redirect(url_for("seo_brand_cache_admin"))
    try:
        summary = refresh_seo_brand_cache_targets(
            get_selected_seo_cache_refresh_targets(selected_slugs),
            run_type="seo_cache_refresh_selected",
        )
        flash(format_seo_cache_refresh_summary("Selected SEO cache refresh", summary))
    except Exception as exc:
        flash(f"Selected SEO cache refresh failed: {str(exc)}")
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/refresh-missing-snapshots", methods=["POST"])
@admin_required
def refresh_missing_seo_snapshots():
    try:
        summary = refresh_seo_brand_cache_targets(
            get_missing_snapshot_refresh_targets(),
            run_type="seo_cache_refresh_missing_snapshots",
        )
        flash(format_seo_cache_refresh_summary("Missing snapshot refresh", summary))
    except Exception as exc:
        flash(f"Missing snapshot refresh failed: {str(exc)}")
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/refresh-missing-images", methods=["POST"])
@admin_required
def refresh_missing_seo_snapshot_images():
    try:
        summary = refresh_seo_brand_cache_targets(
            get_missing_snapshot_image_refresh_targets(),
            run_type="seo_cache_refresh_missing_images",
        )
        flash(format_seo_cache_refresh_summary("Missing snapshot image refresh", summary))
    except Exception as exc:
        flash(f"Missing snapshot image refresh failed: {str(exc)}")
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/preserve-missing-durable-images", methods=["POST"])
@admin_required
def preserve_missing_durable_seo_images_route():
    try:
        summary = preserve_missing_durable_seo_images()
        message = (
            f"Durable image preservation complete. Eligible {summary['eligible_count']}, "
            f"attempted {summary['brands_attempted']} brand(s) "
            f"(limit {summary['limit']} per click), updated {summary['brands_updated']}, "
            f"saved {summary['images_saved']} image(s), reused {summary['images_reused']}, "
            f"failed {summary['images_failed']}. No Apify calls were made."
        )
        if summary["errors"]:
            message += f" First error: {summary['errors'][0]}"
        flash(message)
    except Exception as exc:
        flash(f"Durable image preservation failed safely: {str(exc)}")
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/<brand_slug>/unpublish", methods=["POST"])
@admin_required
def unpublish_seo_brand_route(brand_slug):
    brand = get_brand_by_slug(brand_slug) or get_static_brand_by_slug((brand_slug or "").strip().lower())
    if not brand:
        flash("SEO brand not found.")
        return redirect(url_for("seo_brand_cache_admin"))

    brand_name = unpublish_seo_brand(brand["slug"])
    if brand_name:
        flash(f"{brand_name} unpublished. Cache was preserved.")
    else:
        flash(f"{brand['name']} could not be unpublished.")
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/<brand_slug>/delete-cache", methods=["POST"])
@admin_required
def delete_seo_brand_cache_route(brand_slug):
    brand = get_brand_by_slug(brand_slug) or get_static_brand_by_slug((brand_slug or "").strip().lower())
    deleted_count = delete_seo_brand_cache(brand_slug)
    brand_name = brand["name"] if brand else brand_slug
    flash(
        f"{brand_name} cache deleted ({deleted_count} row"
        f"{'' if deleted_count == 1 else 's'}). SEO brand publishing state was not changed."
    )
    return redirect(url_for("seo_brand_cache_admin"))


@app.route("/admin/seo-brand-cache/<brand_slug>/delete-brand", methods=["POST"])
@admin_required
def delete_seo_brand_route(brand_slug):
    brand = get_brand_by_slug(brand_slug) or get_static_brand_by_slug((brand_slug or "").strip().lower())
    if not brand:
        flash("SEO brand not found.")
        return redirect(url_for("seo_brand_cache_admin"))

    brand_name, retained_tombstone = delete_seo_brand(brand["slug"])
    if not brand_name:
        flash(f"{brand['name']} could not be deleted.")
    elif retained_tombstone:
        flash(
            f"{brand_name} unpublished and cache deleted. An unpublished DB tombstone was kept "
            "so the static fallback does not republish it."
        )
    else:
        flash(f"{brand_name} SEO brand deleted and cache deleted. Candidate records were not changed.")
    return redirect(url_for("seo_brand_cache_admin"))


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
    return render_template("privacy.html", canonical_url=absolute_url("/privacy"))


@app.route("/terms")
def terms():
    return render_template("terms.html", canonical_url=absolute_url("/terms"))


def build_brand_structured_data(brand, canonical_url: str):
    category_name = (brand.get("category") or "Brand research").strip()
    breadcrumb_schema = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "@id": f"{canonical_url}#breadcrumb",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Home",
                "item": absolute_url("/"),
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": category_name.title(),
                "item": f"{canonical_url}#category-context",
            },
            {
                "@type": "ListItem",
                "position": 3,
                "name": f"{brand['name']} ads",
                "item": canonical_url,
            },
        ],
    }
    webpage_schema = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": f"{canonical_url}#webpage",
        "name": brand["meta_title"],
        "headline": brand["headline"],
        "description": brand["meta_description"],
        "url": canonical_url,
        "isPartOf": {
            "@type": "WebSite",
            "name": "RunningAds",
            "url": absolute_url("/"),
        },
        "about": {
            "@type": "Brand",
            "name": brand["name"],
            "category": category_name,
        },
        "breadcrumb": {
            "@id": f"{canonical_url}#breadcrumb",
        },
    }
    return {
        "webpage_schema_json": json.dumps(webpage_schema),
        "breadcrumb_schema_json": json.dumps(breadcrumb_schema),
    }


@app.route("/brand/<brand_slug>")
def brand_page(brand_slug):
    brand = get_brand_by_slug(brand_slug)
    if not brand:
        abort(404)

    seo_ads_cache = get_cached_seo_brand_ads(brand["slug"], fallback_brand_name=brand["name"])
    canonical_url = absolute_url(f"/brand/{brand['slug']}")
    structured_data = build_brand_structured_data(brand, canonical_url)

    return render_template(
        "brand.html",
        brand=brand,
        related_brands=get_related_brands(brand),
        seo_ads=seo_ads_cache["ads"],
        seo_ads_cache=seo_ads_cache,
        seo_public_page=True,
        seo_nav_brand=brand["search_query"],
        canonical_url=canonical_url,
        **structured_data,
    )


@app.route("/seo-ad-image/<int:image_id>")
def seo_ad_image(image_id):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content_type, byte_size, image_data, content_hash
                    FROM seo_ad_images
                    WHERE id = %s
                    """,
                    (image_id,),
                )
                row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)
    content_type = (row[0] or "").lower()
    image_data = bytes(row[2] or b"")
    stored_size = int(row[1] or 0)
    if (
        content_type not in SAFE_SEO_IMAGE_CONTENT_TYPES
        or not image_data
        or stored_size != len(image_data)
        or len(image_data) > SEO_DURABLE_IMAGE_ABSOLUTE_MAX_BYTES
        or detect_safe_image_content_type(image_data) != content_type
    ):
        abort(404)

    response = Response(image_data, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["Content-Length"] = str(len(image_data))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.set_etag(row[3])
    return response.make_conditional(request)

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

    for brand in get_published_seo_brands():
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

