import csv
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError:
    psycopg = None
    dict_row = None
    ConnectionPool = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "app.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
USE_POSTGRES = DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://"))
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "20"))
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
REGISTRATION_TOKEN = os.environ.get("REGISTRATION_TOKEN", "").strip()
STATIC_VERSION = os.environ.get("STATIC_VERSION", "20260722-17")
APP_ENV = os.environ.get("APP_ENV", os.environ.get("FLASK_ENV", "development")).strip().lower()
REQUIRE_POSTGRES = os.environ.get("REQUIRE_POSTGRES", "0").lower() in {"1", "true", "yes"}
PRODUCTION_MODE = APP_ENV in {"prod", "production"} or REQUIRE_POSTGRES
UPLOAD_MAX_MB = int(os.environ.get("UPLOAD_MAX_MB", "32"))
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1").lower() not in {"0", "false", "no"}
RATE_LIMIT_PUBLIC_WRITES_PER_MIN = int(os.environ.get("RATE_LIMIT_PUBLIC_WRITES_PER_MIN", "120"))
RATE_LIMIT_AUTH_WRITES_PER_MIN = int(os.environ.get("RATE_LIMIT_AUTH_WRITES_PER_MIN", "240"))
RATE_LIMIT_UPLOADS_PER_HOUR = int(os.environ.get("RATE_LIMIT_UPLOADS_PER_HOUR", "20"))
_PG_POOL = None
_DB_INITIALIZED = False
_DB_INIT_LOCK = threading.Lock()
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS = defaultdict(deque)

if PRODUCTION_MODE and not USE_POSTGRES:
    raise RuntimeError(
        "PostgreSQL is required when APP_ENV=production or REQUIRE_POSTGRES=1. "
        "Set DATABASE_URL to a postgresql:// URL."
    )

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["APPLICATION_ROOT"] = os.environ.get("APPLICATION_ROOT", "/")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me-" + secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_MB * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = int(os.environ.get("STATIC_CACHE_SECONDS", "3600"))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {
    "1",
    "true",
    "yes",
}

if os.environ.get("TRUST_PROXY_HEADERS", "0").lower() in {"1", "true", "yes"}:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(error):
    message = f"Upload too large. Maximum upload size is {UPLOAD_MAX_MB} MB."
    if request.path.startswith("/api/"):
        return jsonify({"status": "too_large", "message": message}), 413
    flash(message)
    return redirect(request.referrer or url_for("dashboard"))


app.jinja_env.globals["STATIC_VERSION"] = STATIC_VERSION


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def client_rate_key():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if os.environ.get("TRUST_PROXY_HEADERS", "0").lower() in {"1", "true", "yes"} and forwarded:
        ip = forwarded.split(",", 1)[0].strip()
    else:
        ip = request.remote_addr or "unknown"
    token = (request.view_args or {}).get("token", "")
    if not token:
        match = re.match(r"^/(?:api/t|t|r|c)/([^/]+)", request.path)
        if match:
            token = match.group(1)
    if token:
        return f"{ip}:token:{token}"
    user_id = session.get("user_id")
    if user_id:
        return f"{ip}:user:{user_id}"
    return ip


def rate_limit_response(retry_after):
    message = "Too many requests. Please wait before trying again."
    if request.path.startswith("/api/"):
        response = jsonify({"status": "rate_limited", "message": message})
    else:
        response = app.response_class(message + "\n", mimetype="text/plain")
    response.status_code = 429
    response.headers["Retry-After"] = str(max(int(retry_after), 1))
    return response


def check_rate_limit(bucket_name, key, limit, window_seconds):
    if limit <= 0:
        return None
    now = time.monotonic()
    bucket_key = (bucket_name, key)
    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS[bucket_key]
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return window_seconds - (now - bucket[0])
        bucket.append(now)
    return None


def request_rate_limit():
    if not RATE_LIMIT_ENABLED or request.method in {"GET", "HEAD", "OPTIONS"}:
        return None

    key = client_rate_key()
    limits = []
    public_write = (
        request.path.startswith("/api/t/")
        or request.path.startswith("/t/")
        or request.path.startswith("/r/")
        or request.path.startswith("/c/")
    )
    if public_write:
        limits.append(("public-write", RATE_LIMIT_PUBLIC_WRITES_PER_MIN, 60))
    elif session.get("user_id"):
        limits.append(("auth-write", RATE_LIMIT_AUTH_WRITES_PER_MIN, 60))

    if request.files:
        limits.append(("upload", RATE_LIMIT_UPLOADS_PER_HOUR, 3600))

    for bucket_name, limit, window_seconds in limits:
        retry_after = check_rate_limit(bucket_name, key, limit, window_seconds)
        if retry_after is not None:
            return rate_limit_response(retry_after)
    return None


def postgres_pool():
    global _PG_POOL
    if not USE_POSTGRES:
        return None
    if psycopg is None or ConnectionPool is None:
        raise RuntimeError("PostgreSQL requires psycopg[binary,pool]. Install requirements.txt.")
    if _PG_POOL is None:
        _PG_POOL = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=DB_POOL_MIN_SIZE,
            max_size=DB_POOL_SIZE,
            kwargs={"row_factory": dict_row},
        )
    return _PG_POOL


def postgres_sql(sql):
    converted = sql
    insert_or_ignore = re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", converted, re.I)
    if insert_or_ignore:
        converted = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            converted,
            count=1,
            flags=re.I,
        )
        if "ON CONFLICT" not in converted.upper():
            converted = converted.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return converted.replace("?", "%s")


class DatabaseConnection:
    def __init__(self, raw, dialect):
        self.raw = raw
        self.dialect = dialect

    @property
    def is_postgres(self):
        return self.dialect == "postgres"

    def execute(self, sql, params=None):
        if self.is_postgres:
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                return self.raw.execute("SELECT 1")
            return self.raw.execute(postgres_sql(sql), params or ())
        if params is None:
            return self.raw.execute(sql)
        return self.raw.execute(sql, params)

    def executescript(self, script):
        if self.is_postgres:
            for statement in script.split(";"):
                if statement.strip():
                    self.execute(statement)
            return None
        return self.raw.executescript(script)

    def commit(self):
        return self.raw.commit()

    def rollback(self):
        return self.raw.rollback()


class DatabaseContext:
    def __enter__(self):
        self.pool_context = None
        if USE_POSTGRES:
            self.pool_context = postgres_pool().connection()
            self.raw = self.pool_context.__enter__()
            self.conn = DatabaseConnection(self.raw, "postgres")
            return self.conn

        raw = sqlite3.connect(DB_PATH)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA journal_mode = WAL")
        raw.execute("PRAGMA busy_timeout = 5000")
        self.raw = raw
        self.conn = DatabaseConnection(raw, "sqlite")
        return self.conn

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type:
                self.raw.rollback()
            else:
                self.raw.commit()
        finally:
            if self.pool_context is not None:
                self.pool_context.__exit__(exc_type, exc, traceback)
            else:
                self.raw.close()
        return False


def db():
    return DatabaseContext()


def insert_and_get_id(conn, sql, params):
    if conn.is_postgres:
        cursor = conn.execute(sql.rstrip().rstrip(";") + " RETURNING id", params)
        return cursor.fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def is_db_row(value):
    return isinstance(value, sqlite3.Row) or isinstance(value, dict)


DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if psycopg is not None:
    DB_INTEGRITY_ERRORS = DB_INTEGRITY_ERRORS + (psycopg.IntegrityError,)


def create_hot_indexes(conn):
    index_statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_projects_owner
        ON projects (owner_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_segments_project_ordinal
        ON segments (project_id, ordinal)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_project_identifier
        ON segments (project_id, identifier)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_project_languages_project_target_lower
        ON project_languages (project_id, lower(target_language))
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_translations_segment_target_lower
        ON translations (segment_id, lower(target_language))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_translations_target_lower_status
        ON translations (lower(target_language), status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_translation_claims_link_status
        ON translation_claims (share_link_id, status)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_translation_claims_segment_target_lower
        ON translation_claims (segment_id, lower(target_language))
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_translation_comments_segment_target_lower_resolved
        ON translation_comments (segment_id, lower(target_language), resolved)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_translation_events_link_translator
        ON translation_events (share_link_id, translator_name, segment_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_flags_segment_target_lower_created
        ON source_flags (segment_id, lower(target_language), created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_variants_segment_language_lower
        ON source_variants (segment_id, lower(source_language))
        """,
    ]
    for statement in index_statements:
        conn.execute(statement)


def init_postgres_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            source_language TEXT NOT NULL,
            source_editable INTEGER NOT NULL DEFAULT 1,
            import_format_id INTEGER,
            import_mapping TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS source_editable INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS import_format_id INTEGER")
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS import_mapping TEXT NOT NULL DEFAULT '{}'")
    conn.execute("ALTER TABLE segments ADD COLUMN IF NOT EXISTS source_status TEXT NOT NULL DEFAULT 'draft'")
    conn.execute("ALTER TABLE segments ADD COLUMN IF NOT EXISTS source_reviewed_by TEXT")
    conn.execute("ALTER TABLE segments ADD COLUMN IF NOT EXISTS source_reviewed_at TEXT")
    conn.execute("ALTER TABLE translations ADD COLUMN IF NOT EXISTS target_instructions TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE translations ADD COLUMN IF NOT EXISTS draft_instructions TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE translation_events ADD COLUMN IF NOT EXISTS target_instructions TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_formats (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT 'jsonl',
            rows_path TEXT NOT NULL DEFAULT '',
            identifier_path TEXT NOT NULL DEFAULT '',
            source_language_path TEXT NOT NULL DEFAULT '',
            manual_source_language TEXT NOT NULL DEFAULT '',
            source_text_path TEXT NOT NULL DEFAULT '',
            source_text_is_list INTEGER NOT NULL DEFAULT 0,
            instruction_path TEXT NOT NULL DEFAULT '',
            has_seed_translation INTEGER NOT NULL DEFAULT 0,
            target_language_path TEXT NOT NULL DEFAULT '',
            target_language_name TEXT NOT NULL DEFAULT '',
            target_text_path TEXT NOT NULL DEFAULT '',
            target_text_is_list INTEGER NOT NULL DEFAULT 0,
            translated_instruction_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_languages (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, target_language)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            identifier TEXT NOT NULL DEFAULT '',
            ordinal INTEGER NOT NULL,
            source_language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            source_status TEXT NOT NULL DEFAULT 'draft',
            source_reviewed_by TEXT,
            source_reviewed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, ordinal),
            UNIQUE(project_id, identifier)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_variants (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            source_language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            uploaded_by TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(segment_id, source_language)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translations (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            target_text TEXT NOT NULL DEFAULT '',
            draft_text TEXT NOT NULL DEFAULT '',
            target_instructions TEXT NOT NULL DEFAULT '',
            draft_instructions TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            draft_comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'untranslated',
            qa_warnings TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 1,
            updated_by TEXT,
            draft_updated_by TEXT,
            updated_at TEXT NOT NULL,
            draft_updated_at TEXT,
            UNIQUE(segment_id, target_language)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            target_language TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            translator_name TEXT NOT NULL DEFAULT '',
            credit_limit INTEGER,
            start_ordinal INTEGER,
            end_ordinal INTEGER,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_by TEXT
        )
        """
    )
    conn.execute("ALTER TABLE share_links ADD COLUMN IF NOT EXISTS revoked_at TEXT")
    conn.execute("ALTER TABLE share_links ADD COLUMN IF NOT EXISTS revoked_by TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_events (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            translator_name TEXT NOT NULL,
            target_text TEXT NOT NULL,
            target_instructions TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_claims (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            translator_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'claimed',
            claimed_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(segment_id, target_language)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_comments (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            target_language TEXT NOT NULL,
            role TEXT NOT NULL,
            body TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_by TEXT,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_flags (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            share_link_id INTEGER REFERENCES share_links(id) ON DELETE SET NULL,
            target_language TEXT NOT NULL,
            translator_name TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_links (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            reviewer_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_links (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            creator_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    create_hot_indexes(conn)


def init_db():
    with db() as conn:
        if conn.is_postgres:
            init_postgres_schema(conn)
            backfill_translation_statuses(conn)
            backfill_project_languages(conn)
            backfill_quota_fields(conn)
            backfill_translation_events(conn)
            return

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                source_language TEXT NOT NULL,
                source_editable INTEGER NOT NULL DEFAULT 1,
                import_format_id INTEGER,
                import_mapping TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_formats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL DEFAULT 'jsonl',
                rows_path TEXT NOT NULL DEFAULT '',
                identifier_path TEXT NOT NULL DEFAULT '',
                source_language_path TEXT NOT NULL DEFAULT '',
                manual_source_language TEXT NOT NULL DEFAULT '',
                source_text_path TEXT NOT NULL DEFAULT '',
                source_text_is_list INTEGER NOT NULL DEFAULT 0,
                instruction_path TEXT NOT NULL DEFAULT '',
                has_seed_translation INTEGER NOT NULL DEFAULT 0,
                target_language_path TEXT NOT NULL DEFAULT '',
                target_language_name TEXT NOT NULL DEFAULT '',
                target_text_path TEXT NOT NULL DEFAULT '',
                target_text_is_list INTEGER NOT NULL DEFAULT 0,
                translated_instruction_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_languages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                target_language TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, target_language)
            );

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                identifier TEXT NOT NULL DEFAULT '',
                ordinal INTEGER NOT NULL,
                source_language TEXT NOT NULL,
                source_text TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                source_status TEXT NOT NULL DEFAULT 'draft',
                source_reviewed_by TEXT,
                source_reviewed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, ordinal),
                UNIQUE(project_id, identifier)
            );

            CREATE TABLE IF NOT EXISTS translations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                target_language TEXT NOT NULL,
                target_text TEXT NOT NULL DEFAULT '',
                draft_text TEXT NOT NULL DEFAULT '',
                target_instructions TEXT NOT NULL DEFAULT '',
                draft_instructions TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                draft_comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'untranslated',
                qa_warnings TEXT NOT NULL DEFAULT '[]',
                version INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT,
                draft_updated_by TEXT,
                updated_at TEXT NOT NULL,
                draft_updated_at TEXT,
                UNIQUE(segment_id, target_language)
            );

            CREATE TABLE IF NOT EXISTS source_variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                source_language TEXT NOT NULL,
                source_text TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(segment_id, source_language)
            );

            CREATE TABLE IF NOT EXISTS share_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                target_language TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                translator_name TEXT NOT NULL DEFAULT '',
                credit_limit INTEGER,
                start_ordinal INTEGER,
                end_ordinal INTEGER,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                revoked_by TEXT
            );

            """
        )
        migrate_schema(conn)
        backfill_translation_statuses(conn)
        backfill_project_languages(conn)
        backfill_quota_fields(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS translation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                target_language TEXT NOT NULL,
                translator_name TEXT NOT NULL,
                target_text TEXT NOT NULL,
                target_instructions TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_translation_events_link_translator
            ON translation_events (share_link_id, translator_name, segment_id);

            CREATE TABLE IF NOT EXISTS translation_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                target_language TEXT NOT NULL,
                translator_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(segment_id, target_language)
            );

            CREATE TABLE IF NOT EXISTS translation_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                target_language TEXT NOT NULL,
                role TEXT NOT NULL,
                body TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_by TEXT,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS source_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                share_link_id INTEGER REFERENCES share_links(id) ON DELETE SET NULL,
                target_language TEXT NOT NULL,
                translator_name TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS review_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                reviewer_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creator_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                creator_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        create_hot_indexes(conn)
        backfill_translation_events(conn)


def migrate_schema(conn):
    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "source_editable" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN source_editable INTEGER NOT NULL DEFAULT 1")
    if "import_format_id" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN import_format_id INTEGER")
    if "import_mapping" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN import_mapping TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_formats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT 'jsonl',
            rows_path TEXT NOT NULL DEFAULT '',
            identifier_path TEXT NOT NULL DEFAULT '',
            source_language_path TEXT NOT NULL DEFAULT '',
            manual_source_language TEXT NOT NULL DEFAULT '',
            source_text_path TEXT NOT NULL DEFAULT '',
            source_text_is_list INTEGER NOT NULL DEFAULT 0,
            instruction_path TEXT NOT NULL DEFAULT '',
            has_seed_translation INTEGER NOT NULL DEFAULT 0,
            target_language_path TEXT NOT NULL DEFAULT '',
            target_language_name TEXT NOT NULL DEFAULT '',
            target_text_path TEXT NOT NULL DEFAULT '',
            target_text_is_list INTEGER NOT NULL DEFAULT 0,
            translated_instruction_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
    if "identifier" not in segment_columns:
        conn.execute("ALTER TABLE segments ADD COLUMN identifier TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE segments
               SET identifier = printf('seg-%06d', ordinal)
             WHERE identifier = ''
            """
        )
    if "metadata" not in segment_columns:
        conn.execute("ALTER TABLE segments ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    if "source_status" not in segment_columns:
        conn.execute("ALTER TABLE segments ADD COLUMN source_status TEXT NOT NULL DEFAULT 'draft'")
    if "source_reviewed_by" not in segment_columns:
        conn.execute("ALTER TABLE segments ADD COLUMN source_reviewed_by TEXT")
    if "source_reviewed_at" not in segment_columns:
        conn.execute("ALTER TABLE segments ADD COLUMN source_reviewed_at TEXT")

    translation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(translations)")}
    if "comment" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN comment TEXT NOT NULL DEFAULT ''")
    if "draft_text" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN draft_text TEXT NOT NULL DEFAULT ''")
    if "target_instructions" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN target_instructions TEXT NOT NULL DEFAULT ''")
    if "draft_instructions" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN draft_instructions TEXT NOT NULL DEFAULT ''")
    if "draft_comment" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN draft_comment TEXT NOT NULL DEFAULT ''")
    if "status" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN status TEXT NOT NULL DEFAULT 'untranslated'")
        conn.execute(
            """
            UPDATE translations
               SET status = CASE
                   WHEN trim(COALESCE(target_text, '')) != '' THEN 'submitted'
                   ELSE 'untranslated'
               END
            """
        )
    if "qa_warnings" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN qa_warnings TEXT NOT NULL DEFAULT '[]'")
    if "draft_updated_by" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN draft_updated_by TEXT")
    if "draft_updated_at" not in translation_columns:
        conn.execute("ALTER TABLE translations ADD COLUMN draft_updated_at TEXT")

    share_columns = {row["name"] for row in conn.execute("PRAGMA table_info(share_links)")}
    if "label" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "translator_name" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN translator_name TEXT NOT NULL DEFAULT ''")
    if "credit_limit" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN credit_limit INTEGER")
    if "start_ordinal" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN start_ordinal INTEGER")
    if "end_ordinal" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN end_ordinal INTEGER")
    if "revoked_at" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN revoked_at TEXT")
    if "revoked_by" not in share_columns:
        conn.execute("ALTER TABLE share_links ADD COLUMN revoked_by TEXT")

    for index in conn.execute("PRAGMA index_list(share_links)").fetchall():
        if not index["unique"]:
            continue
        index_columns = [
            row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
        ]
        if index_columns == ["project_id", "target_language"]:
            rebuild_share_links(conn)
            break

    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(translation_events)")}
    if event_columns and "comment" not in event_columns:
        conn.execute("ALTER TABLE translation_events ADD COLUMN comment TEXT NOT NULL DEFAULT ''")
    if event_columns and "target_instructions" not in event_columns:
        conn.execute("ALTER TABLE translation_events ADD COLUMN target_instructions TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            source_language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            uploaded_by TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(segment_id, source_language)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_variants_segment_language
        ON source_variants (segment_id, source_language)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS creator_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            creator_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )


def rebuild_share_links(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE share_links RENAME TO share_links_old")
    conn.execute(
        """
        CREATE TABLE share_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            target_language TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            translator_name TEXT NOT NULL DEFAULT '',
            credit_limit INTEGER,
            start_ordinal INTEGER,
            end_ordinal INTEGER,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_by TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO share_links
            (
                id,
                project_id,
                token,
                target_language,
                label,
                translator_name,
                credit_limit,
                start_ordinal,
                end_ordinal,
                created_at,
                revoked_at,
                revoked_by
            )
        SELECT id,
               project_id,
               token,
               target_language,
               label,
               translator_name,
               credit_limit,
               start_ordinal,
               end_ordinal,
               created_at,
               NULL,
               NULL
        FROM share_links_old
        """
    )
    conn.execute("DROP TABLE share_links_old")
    conn.execute("PRAGMA foreign_keys = ON")


def backfill_translation_events(conn):
    conn.execute(
        """
        INSERT INTO translation_events
            (
                share_link_id,
                segment_id,
                target_language,
                translator_name,
                target_text,
                comment,
                version,
                created_at
            )
        SELECT sl.id,
               t.segment_id,
               t.target_language,
               COALESCE(NULLIF(trim(t.updated_by), ''), 'import'),
               t.target_text,
               t.comment,
               t.version,
               t.updated_at
        FROM translations t
        JOIN segments s ON s.id = t.segment_id
        JOIN share_links sl
          ON sl.project_id = s.project_id
         AND lower(sl.target_language) = lower(t.target_language)
         AND (sl.start_ordinal IS NULL OR s.ordinal >= sl.start_ordinal)
         AND (sl.end_ordinal IS NULL OR s.ordinal <= sl.end_ordinal)
        WHERE NOT EXISTS (
            SELECT 1
            FROM translation_events existing
            WHERE existing.share_link_id = sl.id
              AND existing.segment_id = t.segment_id
              AND existing.version = t.version
              AND existing.translator_name = COALESCE(NULLIF(trim(t.updated_by), ''), 'import')
        )
        """
    )


def backfill_translation_statuses(conn):
    rows = conn.execute(
        """
        SELECT t.id,
               t.status,
               t.target_text,
               t.draft_text,
               t.qa_warnings,
               s.source_text
        FROM translations t
        JOIN segments s ON s.id = t.segment_id
        """
    ).fetchall()
    for row in rows:
        status = normalize_status(row["status"], row["target_text"], row["draft_text"])
        warnings = row["qa_warnings"]
        if row["target_text"].strip() and warnings in ("", "[]", None):
            warnings = qa_warnings_json(row["source_text"], row["target_text"])
        conn.execute(
            "UPDATE translations SET status = ?, qa_warnings = ? WHERE id = ?",
            (status, warnings or "[]", row["id"]),
        )


def backfill_quota_fields(conn):
    conn.execute(
        """
        UPDATE share_links
           SET translator_name = COALESCE(NULLIF(trim(label), ''), target_language)
         WHERE trim(COALESCE(translator_name, '')) = ''
        """
    )
    conn.execute(
        """
        UPDATE share_links
           SET credit_limit = (
               SELECT COUNT(*)
               FROM segments s
               WHERE s.project_id = share_links.project_id
                 AND (share_links.start_ordinal IS NULL OR s.ordinal >= share_links.start_ordinal)
                 AND (share_links.end_ordinal IS NULL OR s.ordinal <= share_links.end_ordinal)
           )
         WHERE credit_limit IS NULL
        """
    )


def backfill_project_languages(conn):
    conn.execute(
        """
        INSERT OR IGNORE INTO project_languages (project_id, target_language, created_at)
        SELECT project_id, target_language, MIN(created_at)
        FROM share_links
        GROUP BY project_id, target_language
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO project_languages (project_id, target_language, created_at)
        SELECT s.project_id, t.target_language, MIN(t.updated_at)
        FROM translations t
        JOIN segments s ON s.id = t.segment_id
        GROUP BY s.project_id, t.target_language
        """
    )


def create_project_language(conn, project_id, target_language):
    target_language = target_language.strip()
    if not target_language:
        return False, "Target language is required."
    try:
        conn.execute(
            """
            INSERT INTO project_languages (project_id, target_language, created_at)
            VALUES (?, ?, ?)
            """,
            (project_id, target_language, now_iso()),
        )
    except DB_INTEGRITY_ERRORS:
        conn.rollback()
        return False, "That language already exists for this project."
    return True, None


@app.before_request
def enforce_rate_limits():
    return request_rate_limit()


@app.before_request
def ensure_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _DB_INIT_LOCK:
        if not _DB_INITIALIZED:
            init_db()
            _DB_INITIALIZED = True


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def require_login():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return user


def project_for_owner(project_id, owner_id):
    with db() as conn:
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (project_id, owner_id),
        ).fetchone()
    if not project:
        abort(404)
    return project


IMPORT_FORMAT_FIELDS = [
    "name",
    "description",
    "file_type",
    "rows_path",
    "identifier_path",
    "source_language_path",
    "manual_source_language",
    "source_text_path",
    "source_text_is_list",
    "instruction_path",
    "has_seed_translation",
    "target_language_path",
    "target_language_name",
    "target_text_path",
    "target_text_is_list",
    "translated_instruction_path",
]

DEFAULT_IMPORT_FORMATS = [
    {
        "name": "RhaetoChat JSONL",
        "description": "One JSON object per line with prompt text, response instruction, and optional existing translations.",
        "file_type": "jsonl",
        "rows_path": "",
        "identifier_path": "message_id",
        "source_language_path": "",
        "manual_source_language": "German",
        "source_text_path": "text",
        "source_text_is_list": 0,
        "instruction_path": "response",
        "has_seed_translation": 1,
        "target_language_path": "",
        "target_language_name": "",
        "target_text_path": "translated_prompt",
        "target_text_is_list": 0,
        "translated_instruction_path": "edit_translation",
    },
    {
        "name": "Bouquet JSON",
        "description": "Nested JSON with rows in translations and sentence lists stored under sentences.",
        "file_type": "json",
        "rows_path": "translations",
        "identifier_path": "pid",
        "source_language_path": "language",
        "manual_source_language": "",
        "source_text_path": "sentences",
        "source_text_is_list": 1,
        "instruction_path": "__none__",
        "has_seed_translation": 0,
        "target_language_path": "",
        "target_language_name": "",
        "target_text_path": "sentences",
        "target_text_is_list": 1,
        "translated_instruction_path": "__none__",
    },
]


def bool_int(value):
    return 1 if str(value or "").lower() in {"1", "true", "yes", "on"} else 0


def int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_import_format_config(data):
    data = data or {}
    file_type = (data.get("file_type") or "jsonl").strip().lower()
    if file_type not in {"jsonl", "json"}:
        file_type = "jsonl"
    return {
        "id": data.get("id"),
        "name": data.get("name") or "manual-import",
        "description": data.get("description") or "",
        "file_type": file_type,
        "rows_path": data.get("rows_path") or "",
        "identifier_path": data.get("identifier_path") or data.get("identifier_key") or "",
        "source_language_path": data.get("source_language_path") or data.get("source_language_key") or "",
        "manual_source_language": data.get("manual_source_language") or "",
        "source_text_path": data.get("source_text_path") or data.get("source_text_key") or "",
        "source_text_is_list": bool_int(data.get("source_text_is_list")),
        "instruction_path": data.get("instruction_path") or data.get("instruction_key") or "",
        "has_seed_translation": bool_int(data.get("has_seed_translation")),
        "target_language_path": data.get("target_language_path") or data.get("target_language_key") or "",
        "target_language_name": data.get("target_language_name") or "",
        "target_text_path": data.get("target_text_path") or data.get("target_text_key") or "",
        "target_text_is_list": bool_int(data.get("target_text_is_list")),
        "translated_instruction_path": (
            data.get("translated_instruction_path")
            or data.get("translated_instruction_key")
            or ""
        ),
    }


def import_format_dict(row):
    return normalize_import_format_config(dict(row))


def import_mapping_snapshot(mapping, format_name="manual-import"):
    config = normalize_import_format_config(
        {
            "name": format_name,
            "file_type": mapping.get("file_type"),
            "rows_path": mapping.get("rows_path"),
            "identifier_key": mapping.get("identifier_key"),
            "source_language_key": mapping.get("source_language_key"),
            "manual_source_language": mapping.get("manual_source_language"),
            "source_text_key": mapping.get("source_text_key"),
            "source_text_is_list": mapping.get("source_text_is_list"),
            "instruction_key": mapping.get("instruction_key"),
            "has_seed_translation": mapping.get("has_seed_translation"),
            "target_language_key": mapping.get("target_language_key"),
            "target_language_name": mapping.get("target_language_name"),
            "target_text_key": mapping.get("target_text_key"),
            "target_text_is_list": mapping.get("target_text_is_list"),
            "translated_instruction_key": mapping.get("translated_instruction_key"),
        }
    )
    return json.dumps(config, ensure_ascii=False)


def parse_mapping_from_config(config):
    if not config:
        return None
    return {
        "file_type": config["file_type"],
        "rows_path": config["rows_path"],
        "identifier_key": config["identifier_path"],
        "source_language_key": config["source_language_path"],
        "manual_source_language": config["manual_source_language"],
        "source_text_key": config["source_text_path"],
        "source_text_is_list": bool(config["source_text_is_list"]),
        "instruction_key": config["instruction_path"],
        "has_seed_translation": bool(config["has_seed_translation"]),
        "target_language_key": config["target_language_path"],
        "target_language_name": config["target_language_name"],
        "target_text_key": config["target_text_path"],
        "target_text_is_list": bool(config["target_text_is_list"]),
        "translated_instruction_key": config["translated_instruction_path"],
    }


NEW_PROJECT_FORM_FIELDS = [
    "import_format_id",
    "name",
    "file_type",
    "rows_path",
    "source_text_key",
    "instruction_key",
    "instruction_key_custom",
    "source_language_key",
    "manual_source_language",
    "identifier_key",
    "target_language_key",
    "target_text_key",
    "target_text_key_custom",
    "translated_instruction_key",
    "translated_instruction_key_custom",
    "target_language_name",
    "seed_translation_status",
]

NEW_PROJECT_CHECKBOX_FIELDS = [
    "source_text_is_list",
    "source_language_manual",
    "source_editable",
    "has_seed_translation",
    "target_text_is_list",
]


def new_project_form_state(form=None):
    if not form:
        return {
            "_submitted": "",
            "file_type": "jsonl",
            "identifier_key": "__auto__",
            "instruction_key": "__none__",
            "source_editable": "1",
            "seed_translation_status": "submitted",
            "translated_instruction_key": "__none__",
        }
    state = {field: form.get(field, "") for field in NEW_PROJECT_FORM_FIELDS}
    state["_submitted"] = "1"
    for field in NEW_PROJECT_CHECKBOX_FIELDS:
        state[field] = "1" if form.get(field) == "1" else ""
    state["file_type"] = state["file_type"] or "jsonl"
    state["identifier_key"] = state["identifier_key"] or "__auto__"
    state["instruction_key"] = state["instruction_key"] or "__none__"
    state["seed_translation_status"] = state["seed_translation_status"] or "submitted"
    state["translated_instruction_key"] = state["translated_instruction_key"] or "__none__"
    return state


def project_import_config(project, owner_id):
    raw_mapping = project["import_mapping"] if "import_mapping" in project.keys() else ""
    if raw_mapping:
        try:
            config = json.loads(raw_mapping)
        except json.JSONDecodeError:
            config = {}
        if config:
            return normalize_import_format_config(config)
    return import_format_for_owner(project["import_format_id"], owner_id) if project["import_format_id"] else None


def project_import_config_from_conn(conn, project_id):
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return None
    raw_mapping = project["import_mapping"] if "import_mapping" in project.keys() else ""
    if raw_mapping:
        try:
            config = json.loads(raw_mapping)
        except json.JSONDecodeError:
            config = {}
        if config:
            return normalize_import_format_config(config)
    if project["import_format_id"]:
        row = conn.execute(
            "SELECT * FROM import_formats WHERE id = ? AND owner_id = ?",
            (project["import_format_id"], project["owner_id"]),
        ).fetchone()
        if row:
            return import_format_dict(row)
    return None


def translation_editor_config(format_config):
    config = format_config or {}
    source_is_list = bool_int(config.get("source_text_is_list"))
    has_instruction_translation = bool(
        normalize_path(config.get("translated_instruction_path"))
        and normalize_path(config.get("translated_instruction_path")) != "__none__"
    )
    view = "sentence_list" if source_is_list else "standard"
    if has_instruction_translation:
        view = "dual_field"
    return {
        "view": view,
        "source_text_is_list": source_is_list,
        "target_text_is_list": bool_int(config.get("target_text_is_list")),
        "has_instruction_translation": has_instruction_translation,
        "format_name": config.get("name") or "",
    }


def split_lines(value):
    return str(value or "").splitlines()


def join_form_lines(values):
    return "\n".join(str(value) for value in values)


def ensure_default_import_formats(conn, owner_id):
    existing = conn.execute(
        "SELECT COUNT(*) AS count FROM import_formats WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    if existing and existing["count"]:
        return
    stamp = now_iso()
    for item in DEFAULT_IMPORT_FORMATS:
        conn.execute(
            """
            INSERT INTO import_formats
                (
                    owner_id,
                    name,
                    description,
                    file_type,
                    rows_path,
                    identifier_path,
                    source_language_path,
                    manual_source_language,
                    source_text_path,
                    source_text_is_list,
                    instruction_path,
                    has_seed_translation,
                    target_language_path,
                    target_language_name,
                    target_text_path,
                    target_text_is_list,
                    translated_instruction_path,
                    created_at,
                    updated_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                item["name"],
                item["description"],
                item["file_type"],
                item["rows_path"],
                item["identifier_path"],
                item["source_language_path"],
                item["manual_source_language"],
                item["source_text_path"],
                item["source_text_is_list"],
                item["instruction_path"],
                item["has_seed_translation"],
                item["target_language_path"],
                item["target_language_name"],
                item["target_text_path"],
                item["target_text_is_list"],
                item["translated_instruction_path"],
                stamp,
                stamp,
            ),
        )


def user_import_formats(owner_id):
    with db() as conn:
        ensure_default_import_formats(conn, owner_id)
        rows = conn.execute(
            """
            SELECT *
            FROM import_formats
            WHERE owner_id = ?
            ORDER BY lower(name)
            """,
            (owner_id,),
        ).fetchall()
    return [import_format_dict(row) for row in rows]


def import_format_for_owner(format_id, owner_id):
    if not format_id:
        return None
    with db() as conn:
        ensure_default_import_formats(conn, owner_id)
        row = conn.execute(
            "SELECT * FROM import_formats WHERE id = ? AND owner_id = ?",
            (format_id, owner_id),
        ).fetchone()
    return import_format_dict(row) if row else None


def import_format_payload(form):
    file_type = (form.get("file_type") or "jsonl").strip().lower()
    if file_type not in {"jsonl", "json"}:
        file_type = "jsonl"
    return {
        "name": (form.get("name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "file_type": file_type,
        "rows_path": (form.get("rows_path") or "").strip(),
        "identifier_path": (form.get("identifier_path") or form.get("identifier_key") or "").strip(),
        "source_language_path": (form.get("source_language_path") or form.get("source_language_key") or "").strip(),
        "manual_source_language": (form.get("manual_source_language") or "").strip(),
        "source_text_path": (form.get("source_text_path") or form.get("source_text_key") or "").strip(),
        "source_text_is_list": bool_int(form.get("source_text_is_list")),
        "instruction_path": (form.get("instruction_path") or form.get("instruction_key_custom") or form.get("instruction_key") or "").strip(),
        "has_seed_translation": bool_int(form.get("has_seed_translation")),
        "target_language_path": (form.get("target_language_path") or "").strip(),
        "target_language_name": (form.get("target_language_name") or "").strip(),
        "target_text_path": (form.get("target_text_path") or form.get("target_text_key_custom") or form.get("target_text_key") or "").strip(),
        "target_text_is_list": bool_int(form.get("target_text_is_list")),
        "translated_instruction_path": (
            form.get("translated_instruction_path")
            or form.get("translated_instruction_key_custom")
            or form.get("translated_instruction_key")
            or ""
        ).strip(),
    }


def insert_import_format(conn, owner_id, payload):
    stamp = now_iso()
    return insert_and_get_id(
        conn,
        """
        INSERT INTO import_formats
            (
                owner_id,
                name,
                description,
                file_type,
                rows_path,
                identifier_path,
                source_language_path,
                manual_source_language,
                source_text_path,
                source_text_is_list,
                instruction_path,
                has_seed_translation,
                target_language_path,
                target_language_name,
                target_text_path,
                target_text_is_list,
                translated_instruction_path,
                created_at,
                updated_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_id,
            payload["name"],
            payload["description"],
            payload["file_type"],
            payload["rows_path"],
            payload["identifier_path"],
            payload["source_language_path"],
            payload["manual_source_language"],
            payload["source_text_path"],
            payload["source_text_is_list"],
            payload["instruction_path"],
            payload["has_seed_translation"],
            payload["target_language_path"],
            payload["target_language_name"],
            payload["target_text_path"],
            payload["target_text_is_list"],
            payload["translated_instruction_path"],
            stamp,
            stamp,
        ),
    )


def update_import_format(conn, format_id, owner_id, payload):
    conn.execute(
        """
        UPDATE import_formats
           SET name = ?,
               description = ?,
               file_type = ?,
               rows_path = ?,
               identifier_path = ?,
               source_language_path = ?,
               manual_source_language = ?,
               source_text_path = ?,
               source_text_is_list = ?,
               instruction_path = ?,
               has_seed_translation = ?,
               target_language_path = ?,
               target_language_name = ?,
               target_text_path = ?,
               target_text_is_list = ?,
               translated_instruction_path = ?,
               updated_at = ?
         WHERE id = ?
           AND owner_id = ?
        """,
        (
            payload["name"],
            payload["description"],
            payload["file_type"],
            payload["rows_path"],
            payload["identifier_path"],
            payload["source_language_path"],
            payload["manual_source_language"],
            payload["source_text_path"],
            payload["source_text_is_list"],
            payload["instruction_path"],
            payload["has_seed_translation"],
            payload["target_language_path"],
            payload["target_language_name"],
            payload["target_text_path"],
            payload["target_text_is_list"],
            payload["translated_instruction_path"],
            now_iso(),
            format_id,
            owner_id,
        ),
    )


SOURCE_LANGUAGE_KEYS = ["source_language", "source_lang", "src_lang", "language", "lang", "locale"]
SOURCE_TEXT_KEYS = ["source_text", "src_text", "text", "sentences", "content", "body", "prompt"]
INSTRUCTION_KEYS = [
    "instructions",
    "instruction",
    "source_instruction",
    "source_instructions",
    "response",
]
IDENTIFIER_KEYS = ["identifier", "message_id", "id", "text_id", "segment_id"]
TARGET_LANGUAGE_KEYS = ["tgt_lang", "target_language"]
TARGET_TEXT_KEYS = ["tgt_text", "target_text", "translation", "translated_text", "translated_prompt"]
TARGET_COMMENT_KEYS = ["comment", "tgt_comment"]
COMMENT_IMPORT_ID_KEYS = [
    *IDENTIFIER_KEYS,
    "document_id",
    "doc_id",
    "pid",
    "uniq_id",
    "par_id",
    "uid",
    "key",
]
SOURCE_VARIANT_ID_KEYS = COMMENT_IMPORT_ID_KEYS
SOURCE_VARIANT_LANGUAGE_KEYS = [
    *SOURCE_LANGUAGE_KEYS,
    *TARGET_LANGUAGE_KEYS,
    "source_language",
    "target_language",
]
SOURCE_VARIANT_TEXT_KEYS = [
    *SOURCE_TEXT_KEYS,
    *TARGET_TEXT_KEYS,
    "orig_text",
    "translation",
    "sentences",
]
TRANSLATED_INSTRUCTION_KEYS = [
    "translated_instruction",
    "translated_instructions",
    "translated_response",
    "edit_translation",
    "target_instruction",
    "target_instructions",
    "tgt_instruction",
    "tgt_instructions",
]


def first_present(item, keys):
    for key in keys:
        if key and key in item:
            return item.get(key)
    return None


def normalize_path(path):
    return (path or "").strip()


def path_parts(path):
    return [part for part in normalize_path(path).split(".") if part]


def get_path(item, path, default=None):
    parts = path_parts(path)
    if not parts:
        return default
    value = item
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default
    return value


def set_path(item, path, value):
    parts = path_parts(path)
    if not parts:
        return
    cursor = item
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def remove_path_roots(item, paths):
    roots = {path_parts(path)[0] for path in paths if path_parts(path)}
    return {key: value for key, value in item.items() if key not in roots}


def import_text_value(value, value_is_list=False):
    if value is None:
        return ""
    if value_is_list and isinstance(value, list):
        return "\n".join("" if item is None else str(item) for item in value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def export_text_value(value, value_is_list=False):
    text = str(value or "")
    if not value_is_list:
        return text
    return text.splitlines()


def load_import_items(file_storage, file_type="jsonl", rows_path=""):
    file_type = (file_type or "jsonl").strip().lower()
    filename = (getattr(file_storage, "filename", "") or "").lower()
    if file_type == "jsonl" and filename.endswith(".json"):
        file_type = "json"
    if file_type == "json":
        raw = file_storage.stream.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON ({exc.msg})") from exc
        if rows_path:
            rows = get_path(payload, rows_path)
        else:
            rows = payload
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise ValueError("The configured rows path must point to a JSON object or array.")
        items = []
        for index, item in enumerate(rows, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Row {index}: expected a JSON object.")
            items.append((index, item))
        return items

    items = []
    for line_no, raw in enumerate(file_storage.stream, start=1):
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {line_no}: invalid JSON ({exc.msg})") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Line {line_no}: expected a JSON object.")
        items.append((line_no, item))
    return items


def parse_jsonl(file_storage, mapping=None):
    explicit_mapping = mapping is not None
    mapping = mapping or {}
    rows = []
    source_language = None
    seen_identifiers = set()
    file_type = (mapping.get("file_type") or "jsonl").strip()
    rows_path = (mapping.get("rows_path") or "").strip()
    source_language_key = (mapping.get("source_language_key") or "").strip()
    source_text_key = (mapping.get("source_text_key") or "").strip()
    source_text_is_list = bool_int(mapping.get("source_text_is_list"))
    instruction_key = (mapping.get("instruction_key") or "").strip()
    identifier_key = (mapping.get("identifier_key") or "").strip()
    manual_source_language = (mapping.get("manual_source_language") or "").strip()
    has_seed_translation = bool(mapping.get("has_seed_translation"))
    target_text_key = (mapping.get("target_text_key") or "").strip()
    target_text_is_list = bool_int(mapping.get("target_text_is_list"))
    translated_instruction_key = (mapping.get("translated_instruction_key") or "").strip()
    target_language_key = (mapping.get("target_language_key") or "").strip()
    target_language_name = (mapping.get("target_language_name") or "").strip()

    for line_no, item in load_import_items(file_storage, file_type, rows_path):
        if manual_source_language:
            src_lang = manual_source_language
        elif source_language_key:
            src_lang = get_path(item, source_language_key)
        else:
            src_lang = first_present(item, SOURCE_LANGUAGE_KEYS)

        if source_text_key:
            source_text = get_path(item, source_text_key)
        else:
            source_text = first_present(item, SOURCE_TEXT_KEYS)

        if instruction_key and instruction_key != "__none__":
            instructions = get_path(item, instruction_key, "")
        elif instruction_key == "__none__":
            instructions = ""
        else:
            instructions = first_present(item, INSTRUCTION_KEYS) or ""

        if explicit_mapping:
            if has_seed_translation:
                target_language = target_language_name or (
                    str(get_path(item, target_language_key, "")).strip()
                    if target_language_key
                    else ""
                )
            else:
                target_language = None
            target_text = get_path(item, target_text_key) if has_seed_translation and target_text_key else None
        else:
            target_language = first_present(item, TARGET_LANGUAGE_KEYS)
            target_text = first_present(item, TARGET_TEXT_KEYS)

        target_comment = first_present(item, TARGET_COMMENT_KEYS) or ""
        translated_instruction = ""
        if has_seed_translation and translated_instruction_key and translated_instruction_key != "__none__":
            translated_instruction = get_path(item, translated_instruction_key) or ""
        known_keys = {
            *IDENTIFIER_KEYS,
            *SOURCE_LANGUAGE_KEYS,
            *SOURCE_TEXT_KEYS,
            *INSTRUCTION_KEYS,
            *TARGET_LANGUAGE_KEYS,
            *TARGET_TEXT_KEYS,
            *TARGET_COMMENT_KEYS,
            *TRANSLATED_INSTRUCTION_KEYS,
        }
        known_keys.update(
            key
            for key in [
                source_language_key,
                source_text_key,
                instruction_key,
                identifier_key,
                target_language_key,
                target_text_key,
                translated_instruction_key,
            ]
            if key and key != "__auto__"
        )
        metadata = remove_path_roots(
            {key: value for key, value in item.items() if key not in known_keys},
            [
                source_language_key,
                source_text_key,
                instruction_key,
                identifier_key,
                target_language_key,
                target_text_key,
                translated_instruction_key,
            ],
        )
        if identifier_key and identifier_key != "__auto__":
            identifier = get_path(item, identifier_key) or f"seg-{line_no:06d}"
        elif identifier_key == "__auto__":
            identifier = f"seg-{line_no:06d}"
        else:
            identifier = first_present(item, IDENTIFIER_KEYS) or f"seg-{line_no:06d}"
        identifier = str(identifier).strip() or f"seg-{line_no:06d}"
        base_identifier = identifier
        suffix = 2
        while identifier in seen_identifiers:
            identifier = f"{base_identifier}-{suffix}"
            suffix += 1
        seen_identifiers.add(identifier)

        if not src_lang or not source_text:
            raise ValueError(
                f"Line {line_no}: expected source language and source text values"
            )

        if source_language is None:
            source_language = src_lang

        rows.append(
            {
                "identifier": identifier,
                "source_language": str(src_lang),
                "source_text": import_text_value(source_text, source_text_is_list),
                "instructions": import_text_value(instructions),
                "metadata": metadata,
                "target_language": target_language,
                "target_text": import_text_value(target_text, target_text_is_list)
                if target_text is not None
                else None,
                "target_instructions": import_text_value(translated_instruction),
                "target_comment": str(target_comment or ""),
            }
        )

    if not rows:
        raise ValueError("The JSONL file did not contain any rows.")

    return source_language, rows


def parse_translation_jsonl(
    file_storage,
    id_key,
    target_text_key,
    comment_key="",
    instruction_key="",
    language_key="",
):
    id_key = (id_key or "message_id").strip()
    target_text_key = (target_text_key or "").strip()
    comment_key = (comment_key or "").strip()
    instruction_key = (instruction_key or "").strip()
    language_key = (language_key or "").strip()
    if not id_key:
        raise ValueError("Message ID key is required.")
    if not target_text_key:
        raise ValueError("Translation text key is required.")

    rows = []
    seen_identifiers = set()
    for line_no, raw in enumerate(file_storage.stream, start=1):
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {line_no}: invalid JSON ({exc.msg})") from exc

        identifier = str(item.get(id_key, "")).strip()
        if not identifier:
            raise ValueError(f"Line {line_no}: expected message ID key '{id_key}'.")
        if identifier in seen_identifiers:
            raise ValueError(f"Line {line_no}: duplicate message ID '{identifier}'.")
        seen_identifiers.add(identifier)

        target_text = item.get(target_text_key)
        target_language = str(item.get(language_key, "")).strip() if language_key else ""
        comment = str(item.get(comment_key, "") if comment_key else "")
        translated_instruction = item.get(instruction_key) if instruction_key else ""

        rows.append(
            {
                "identifier": identifier,
                "target_text": "" if target_text is None else str(target_text),
                "target_instructions": "" if translated_instruction is None else str(translated_instruction),
                "target_language": target_language,
                "comment": comment,
            }
        )

    if not rows:
        raise ValueError("The JSONL file did not contain any rows.")
    return rows


def parse_source_variant_upload(
    file_storage,
    id_key,
    source_text_key,
    source_language="",
    language_key="",
    instruction_key="",
    file_type="jsonl",
    rows_path="",
    source_text_is_list=False,
):
    id_key = (id_key or "identifier").strip()
    source_text_key = (source_text_key or "").strip()
    source_language = (source_language or "").strip()
    language_key = (language_key or "").strip()
    instruction_key = (instruction_key or "").strip()
    if not id_key:
        raise ValueError("Message ID key is required.")
    if not source_text_key:
        raise ValueError("Source text key is required.")

    rows = []
    seen = set()
    for row_no, item in load_import_items(file_storage, file_type, rows_path):
        identifier = str(get_path(item, id_key, "")).strip()
        if not identifier:
            raise ValueError(f"Row {row_no}: expected ID key '{id_key}'.")
        row_language = source_language
        if not row_language and language_key:
            row_language = str(get_path(item, language_key, "")).strip()
        if not row_language:
            inferred_language = first_present(item, SOURCE_VARIANT_LANGUAGE_KEYS)
            row_language = str(inferred_language or "").strip()

        source_text = get_path(item, source_text_key)
        instructions = ""
        if instruction_key and instruction_key != "__none__":
            instructions = get_path(item, instruction_key, "")

        seen_key = (identifier.lower(), row_language.lower())
        if seen_key in seen:
            raise ValueError(
                f"Row {row_no}: duplicate ID/language pair '{identifier}' / '{row_language}'."
            )
        seen.add(seen_key)
        rows.append(
            {
                "identifier": identifier,
                "source_language": row_language,
                "source_text": import_text_value(source_text, source_text_is_list),
                "instructions": import_text_value(instructions),
            }
        )

    if not rows:
        raise ValueError("The source file did not contain any rows.")
    return rows


def parse_comment_csv(file_storage, has_header=True):
    try:
        text = file_storage.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The CSV file must be UTF-8 encoded.") from exc

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = []
    reader = csv.reader(StringIO(text), dialect)
    skipped_header = False
    for line_no, columns in enumerate(reader, start=1):
        if not any(str(cell or "").strip() for cell in columns):
            continue
        if has_header and not skipped_header:
            skipped_header = True
            continue

        identifier = str(columns[0] if columns else "").strip()
        comment = "\n".join(
            str(cell).strip() for cell in columns[1:] if str(cell or "").strip()
        )
        rows.append(
            {
                "line_no": line_no,
                "identifier": identifier,
                "comment": comment,
            }
        )

    if not rows:
        raise ValueError("The CSV file did not contain any comment rows.")
    return rows


def import_translation_mode(value):
    value = (value or "submitted").strip()
    if value in {"draft", "needs_revision"}:
        return "draft"
    return "submitted"


def imported_translation_payload(
    source_text,
    target_text,
    comment,
    mode,
    user_name,
    stamp,
    target_instructions="",
):
    target_text = str(target_text or "")
    target_instructions = str(target_instructions or "")
    comment = str(comment or "")
    if import_translation_mode(mode) == "draft":
        return {
            "target_text": "",
            "draft_text": target_text,
            "target_instructions": "",
            "draft_instructions": target_instructions,
            "comment": "",
            "draft_comment": comment,
            "status": normalize_status("draft", "", target_text),
            "qa_warnings": "[]",
            "version": 0,
            "updated_by": None,
            "draft_updated_by": user_name,
            "updated_at": stamp,
            "draft_updated_at": stamp,
        }
    return {
        "target_text": target_text,
        "draft_text": "",
        "target_instructions": target_instructions,
        "draft_instructions": "",
        "comment": comment,
        "draft_comment": "",
        "status": normalize_status("submitted", target_text, ""),
        "qa_warnings": qa_warnings_json(source_text, target_text),
        "version": 1,
        "updated_by": user_name,
        "draft_updated_by": None,
        "updated_at": stamp,
        "draft_updated_at": None,
    }


def parse_optional_int(value, field_name="Value"):
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must use a whole number.") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be 1 or higher.")
    return parsed


def range_label(start_ordinal, end_ordinal):
    if start_ordinal is None and end_ordinal is None:
        return "all segments"
    if start_ordinal is None:
        return f"through {end_ordinal}"
    if end_ordinal is None:
        return f"from {start_ordinal}"
    return f"{start_ordinal}-{end_ordinal}"


def create_quota_link_from_form(conn, project_id, target_language, form):
    translator_name = form.get("translator_name", "").strip()
    if not translator_name:
        return None, "Translator name is required."

    try:
        assignment_limit = form.get("assignment_limit") or form.get("credit_limit")
        credit_limit = parse_optional_int(assignment_limit, "Assignment limit")
    except ValueError as exc:
        return None, str(exc)

    if credit_limit is None:
        return None, "Assignment limit is required."

    generated_label = translator_name
    conn.execute(
        """
        INSERT OR IGNORE INTO project_languages (project_id, target_language, created_at)
        VALUES (?, ?, ?)
        """,
        (project_id, target_language, now_iso()),
    )
    link_id = insert_and_get_id(
        conn,
        """
        INSERT INTO share_links
            (
                project_id,
                token,
                target_language,
                label,
                translator_name,
                credit_limit,
                start_ordinal,
                end_ordinal,
                created_at
            )
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            project_id,
            secrets.token_urlsafe(24),
            target_language,
            generated_label,
            translator_name[:80],
            credit_limit,
            now_iso(),
        ),
    )
    return link_id, None


def link_credit_used(conn, link_id):
    return conn.execute(
        """
        SELECT COUNT(DISTINCT segment_id) AS count
        FROM translation_claims
        WHERE share_link_id = ?
          AND status = 'completed'
        """,
        (link_id,),
    ).fetchone()["count"]


def link_remaining_credits(conn, link):
    used = link_credit_used(conn, link["id"])
    if link["credit_limit"] is None:
        return None, used
    return max(link["credit_limit"] - used, 0), used


def assignment_payload(link, used, remaining):
    return {
        "assignment_limit": link["credit_limit"],
        "submitted_assignments": used,
        "remaining_assignments": remaining,
        "credit_limit": link["credit_limit"],
        "used_credits": used,
        "remaining_credits": remaining,
    }


def link_translator_name(link):
    return (link["translator_name"] or link["label"] or "anonymous").strip()[:80] or "anonymous"


def reviewer_name(link):
    return (link["reviewer_name"] or "reviewer").strip()[:80] or "reviewer"


def project_language_rows(conn, project_id):
    return conn.execute(
        """
        SELECT target_language
        FROM project_languages
        WHERE project_id = ?
        ORDER BY lower(target_language), target_language
        """,
        (project_id,),
    ).fetchall()


def row_to_dict(row):
    return dict(row) if row else None


def metadata_dict(raw):
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def normalized_lookup_values(value):
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    normalized = []
    for item in values:
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = text.strip()
        if text:
            normalized.append(text)
    return normalized


def segment_match_values(segment, match_key):
    key = (match_key or "identifier").strip() or "identifier"
    if key.startswith("metadata."):
        key = key.split(".", 1)[1]
    if key in {"identifier", "ordinal", "source_language", "source_text", "instructions"}:
        return normalized_lookup_values(segment[key])
    metadata = metadata_dict(segment["metadata"])
    return normalized_lookup_values(metadata.get(key))


def project_metadata_keys(conn, project_id):
    keys = set()
    rows = conn.execute(
        "SELECT metadata FROM segments WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    for row in rows:
        keys.update(metadata_dict(row["metadata"]).keys())
    return sorted(str(key) for key in keys if str(key).strip())


def source_variants_for_segment(conn, segment):
    segment = row_to_dict(segment) if not isinstance(segment, dict) else segment
    if not segment:
        return []
    variants = [
        {
            "source_language": segment.get("source_language", ""),
            "source_text": segment.get("source_text", ""),
            "instructions": segment.get("instructions", ""),
            "source_lines": split_lines(segment.get("source_text", "")),
            "is_default": True,
        }
    ]
    default_language = str(segment.get("source_language", "")).lower()
    rows = conn.execute(
        """
        SELECT source_language, source_text, instructions, updated_at, uploaded_by
        FROM source_variants
        WHERE segment_id = ?
        ORDER BY lower(source_language), source_language
        """,
        (segment["id"],),
    ).fetchall()
    for row in rows:
        if str(row["source_language"]).lower() == default_language:
            continue
        variants.append(
            {
                "source_language": row["source_language"],
                "source_text": row["source_text"],
                "instructions": row["instructions"],
                "source_lines": split_lines(row["source_text"]),
                "is_default": False,
                "updated_at": row["updated_at"],
                "uploaded_by": row["uploaded_by"],
            }
        )
    return variants


def source_language_rows(conn, project_id):
    rows = conn.execute(
        """
        SELECT *
        FROM (
            SELECT source_language,
                   COUNT(*) AS segment_count,
                   1 AS is_default
            FROM segments
            WHERE project_id = ?
            GROUP BY source_language
            UNION ALL
            SELECT sv.source_language,
                   COUNT(*) AS segment_count,
                   0 AS is_default
            FROM source_variants sv
            JOIN segments s ON s.id = sv.segment_id
            WHERE s.project_id = ?
            GROUP BY sv.source_language
        ) source_layers
        ORDER BY is_default DESC, lower(source_language), source_language
        """,
        (project_id, project_id),
    ).fetchall()
    return rows


def json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def grouped_source_flags(conn, segment_ids, languages=None):
    segment_ids = [int(segment_id) for segment_id in segment_ids]
    if not segment_ids:
        return {}

    where = [f"segment_id IN ({','.join('?' for _ in segment_ids)})"]
    params = list(segment_ids)
    if languages:
        clean_languages = [language for language in languages if language]
        if clean_languages:
            where.append(
                "("
                + " OR ".join("lower(target_language) = lower(?)" for _ in clean_languages)
                + ")"
            )
            params.extend(clean_languages)

    rows = conn.execute(
        f"""
        SELECT segment_id,
               target_language,
               translator_name,
               note,
               created_at
        FROM source_flags
        WHERE {" AND ".join(where)}
        ORDER BY created_at, id
        """,
        tuple(params),
    ).fetchall()

    grouped = {}
    for row in rows:
        item = {
            "language": row["target_language"],
            "translator": row["translator_name"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        grouped.setdefault(row["segment_id"], []).append(item)
    return grouped


def profile_export_item(segment, selected_languages, translations_by_segment, format_config):
    item = metadata_dict(segment["metadata"])
    if format_config["identifier_path"]:
        set_path(item, format_config["identifier_path"], segment["identifier"])
    if format_config["source_language_path"]:
        set_path(item, format_config["source_language_path"], segment["source_language"])
    if format_config["source_text_path"]:
        set_path(
            item,
            format_config["source_text_path"],
            export_text_value(segment["source_text"], format_config["source_text_is_list"]),
        )
    if format_config["instruction_path"] and format_config["instruction_path"] != "__none__":
        set_path(item, format_config["instruction_path"], segment["instructions"])

    if len(selected_languages) == 1 and format_config["target_text_path"]:
        language = selected_languages[0]
        translation = translations_by_segment.get(segment["id"], {}).get(language, {})
        set_path(
            item,
            format_config["target_text_path"],
            export_text_value(
                translation.get("target_text", ""),
                format_config["target_text_is_list"],
            ),
        )
        if format_config["target_language_path"]:
            set_path(item, format_config["target_language_path"], language)
        if (
            format_config["translated_instruction_path"]
            and format_config["translated_instruction_path"] != "__none__"
        ):
            set_path(
                item,
                format_config["translated_instruction_path"],
                translation.get("target_instructions", ""),
            )
    return item


def format_export_response(rows, format_config, project_id):
    if format_config["file_type"] == "json":
        payload = {}
        if format_config["rows_path"]:
            set_path(payload, format_config["rows_path"], rows)
        else:
            payload = rows
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        filename = secure_filename(f"project-{project_id}-{format_config['name']}.json")
        return app.response_class(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    filename = secure_filename(f"project-{project_id}-{format_config['name']}.jsonl")
    return app.response_class(
        lines,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def safe_download_name(value, default):
    cleaned = secure_filename(str(value or "").strip())
    return cleaned or default


def safe_archive_name(value, default):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned or default


def segment_txt_filename(row):
    identifier = safe_archive_name(row["identifier"], f"segment-{row['ordinal']:06d}")
    return f"{row['ordinal']:06d}-{identifier}.txt"


TRANSLATION_STATUS_LIST = [
    "untranslated",
    "draft",
    "submitted",
    "needs_revision",
    "approved",
]
TRANSLATION_STATUSES = set(TRANSLATION_STATUS_LIST)


def normalize_status(status, target_text="", draft_text=""):
    status = (status or "").strip()
    if status in {"needs_revision", "approved"} and str(target_text or "").strip():
        return status
    if str(target_text or "").strip():
        return "submitted"
    if str(draft_text or "").strip():
        return "draft"
    return "untranslated"


def qa_warning_items(source_text, target_text):
    source = str(source_text or "")
    target = str(target_text or "")
    warnings = []

    def add(code, label):
        warnings.append({"code": code, "label": label})

    if not target.strip():
        add("empty_translation", "Empty translation")
        return warnings

    def row_count(value):
        lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return len(lines)

    source_rows = row_count(source)
    target_rows = row_count(target)
    if (source_rows > 1 or target_rows > 1) and source_rows != target_rows:
        add("row_count", f"Row count differs: source {source_rows}, translation {target_rows}")

    source_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", source))
    target_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", target))
    missing_numbers = sorted(source_numbers - target_numbers)
    if missing_numbers:
        add("missing_numbers", "Missing number(s): " + ", ".join(missing_numbers[:5]))

    source_end = re.search(r"([.!?])\s*$", source)
    target_end = re.search(r"([.!?])\s*$", target)
    if source_end and target_end and source_end.group(1) != target_end.group(1):
        add("changed_punctuation", "Terminal punctuation differs")

    source_len = len(source.strip())
    target_len = len(target.strip())
    if source_len and (target_len / source_len < 0.35 or target_len / source_len > 2.8):
        add("length_ratio", "Translation length looks unusual")

    source_links = len(re.findall(r"\[[^\]]+\]\(https?://[^)\s]+\)", source))
    target_links = len(re.findall(r"\[[^\]]+\]\(https?://[^)\s]+\)", target))
    if source_links != target_links:
        add("markdown_links", "Markdown link count differs")

    source_headings = len(re.findall(r"(?m)^#{1,6}\s+", source))
    target_headings = len(re.findall(r"(?m)^#{1,6}\s+", target))
    if source_headings != target_headings:
        add("markdown_headings", "Markdown heading count differs")

    source_words = {
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", source)
        if word.lower() not in {"about", "after", "before", "could", "every", "there", "their", "which", "would"}
    }
    target_lower = target.lower()
    copied = sorted(word for word in source_words if re.search(rf"\b{re.escape(word)}\b", target_lower))
    if len(copied) >= 3:
        add("untranslated_source_words", "Several source words may be untranslated")

    return warnings


def qa_warnings_json(source_text, target_text):
    return json.dumps(qa_warning_items(source_text, target_text), ensure_ascii=False)


def qa_warning_count(raw):
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return 0
    return len(value) if isinstance(value, list) else 0


def from_json(raw):
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


app.jinja_env.filters["qa_count"] = qa_warning_count
app.jinja_env.filters["from_json"] = from_json
app.jinja_env.globals["TRANSLATION_STATUSES"] = TRANSLATION_STATUS_LIST


COMMENT_ROLES = ["translator", "reviewer", "manager"]
app.jinja_env.globals["COMMENT_ROLES"] = COMMENT_ROLES


def comments_for(conn, segment_id, target_language):
    return conn.execute(
        """
        SELECT *
        FROM translation_comments
        WHERE segment_id = ?
          AND lower(target_language) = lower(?)
        ORDER BY resolved ASC, created_at DESC, id DESC
        """,
        (segment_id, target_language),
    ).fetchall()


def can_delete_comment(conn, comment, current_user):
    current_user = str(current_user or "")
    if not current_user or comment["created_by"] != current_user:
        return False
    later_other = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM translation_comments
        WHERE segment_id = ?
          AND lower(target_language) = lower(?)
          AND created_by != ?
          AND (
              created_at > ?
              OR (created_at = ? AND id > ?)
          )
        """,
        (
            comment["segment_id"],
            comment["target_language"],
            current_user,
            comment["created_at"],
            comment["created_at"],
            comment["id"],
        ),
    ).fetchone()["count"]
    return later_other == 0


def comments_with_permissions(conn, segment_id, target_language, current_user=None):
    rows = []
    for row in comments_for(conn, segment_id, target_language):
        item = dict(row)
        item["can_delete"] = can_delete_comment(conn, item, current_user)
        rows.append(item)
    return rows


def serialized_comments(conn, segment_id, target_language, current_user=None):
    return comments_with_permissions(conn, segment_id, target_language, current_user)


def add_translation_comment(conn, segment_id, target_language, role, body, created_by):
    role = role if role in COMMENT_ROLES else "translator"
    body = str(body or "").strip()
    if not body:
        return False
    conn.execute(
        """
        INSERT INTO translation_comments
            (segment_id, target_language, role, body, resolved, created_by, created_at)
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (segment_id, target_language, role, body, created_by, now_iso()),
    )
    return True


def delete_translation_comment(conn, comment_id, segment_id, target_language, current_user):
    row = conn.execute(
        """
        SELECT *
        FROM translation_comments
        WHERE id = ?
          AND segment_id = ?
          AND lower(target_language) = lower(?)
        """,
        (comment_id, segment_id, target_language),
    ).fetchone()
    if not row:
        abort(404)
    if row["created_by"] != current_user:
        return False, "You can only delete your own comments."
    if not can_delete_comment(conn, row, current_user):
        return False, "This comment can no longer be deleted because someone replied after it."
    conn.execute("DELETE FROM translation_comments WHERE id = ?", (comment_id,))
    return True, None


def resolve_translation_comment(conn, comment_id, segment_id, target_language, resolved_by):
    row = conn.execute(
        """
        SELECT *
        FROM translation_comments
        WHERE id = ?
          AND segment_id = ?
          AND lower(target_language) = lower(?)
        """,
        (comment_id, segment_id, target_language),
    ).fetchone()
    if not row:
        abort(404)
    conn.execute(
        """
        UPDATE translation_comments
           SET resolved = CASE WHEN resolved = 1 THEN 0 ELSE 1 END,
               resolved_by = CASE WHEN resolved = 1 THEN NULL ELSE ? END,
               resolved_at = CASE WHEN resolved = 1 THEN NULL ELSE ? END
         WHERE id = ?
        """,
        (resolved_by, now_iso(), comment_id),
    )


def markdown_inline_html(text):
    safe = xml_escape(str(text or ""))
    safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", safe)
    safe = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        safe,
    )
    return safe


def markdown_to_html(text):
    lines = str(text or "").splitlines()
    html = []
    paragraph = []
    list_items = []

    def flush_paragraph():
        if paragraph:
            html.append(f"<p>{markdown_inline_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list():
        if list_items:
            html.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        quote = re.match(r"^>\s?(.+)$", stripped)

        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            html.append(f"<h{level}>{markdown_inline_html(heading.group(2))}</h{level}>")
        elif bullet:
            flush_paragraph()
            list_items.append(markdown_inline_html(bullet.group(1)))
        elif quote:
            flush_paragraph()
            flush_list()
            html.append(f"<blockquote>{markdown_inline_html(quote.group(1))}</blockquote>")
        else:
            flush_list()
            paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    return Markup("".join(html) or "<p></p>")


app.jinja_env.filters["markdown"] = markdown_to_html


def docx_text(value):
    return xml_escape(str(value or ""))


def docx_run_props(bold=False, italic=False, code=False, color=None):
    props = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if code:
        props.append("<w:rStyle w:val=\"Code\"/>")
    if color:
        props.append(f"<w:color w:val=\"{docx_text(color).lstrip('#')}\"/>")
    return f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""


def docx_paragraph(text="", bold=False, color=None, spacing_before=0, spacing_after=0):
    run_props = docx_run_props(bold=bold, color=color)
    paragraph_props = ""
    if spacing_before or spacing_after:
        paragraph_props = (
            "<w:pPr>"
            f"<w:spacing w:before=\"{int(spacing_before)}\" w:after=\"{int(spacing_after)}\"/>"
            "</w:pPr>"
        )
    lines = str(text or "").splitlines() or [""]
    pieces = []
    for index, line in enumerate(lines):
        if index:
            pieces.append("<w:br/>")
        pieces.append(f"<w:t xml:space=\"preserve\">{docx_text(line)}</w:t>")
    return f"<w:p>{paragraph_props}<w:r>{run_props}{''.join(pieces)}</w:r></w:p>"


def docx_blank_paragraphs(count=1):
    return "".join(docx_paragraph() for _ in range(max(int(count or 0), 0)))


def docx_run(text, bold=False, italic=False, code=False, color=None):
    run_props = docx_run_props(bold=bold, italic=italic, code=code, color=color)
    return f"<w:r>{run_props}<w:t xml:space=\"preserve\">{docx_text(text)}</w:t></w:r>"


def docx_inline_runs(text, color=None):
    runs = []
    pattern = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\(https?://[^)\s]+\))")
    position = 0
    for match in pattern.finditer(str(text or "")):
        if match.start() > position:
            runs.append(docx_run(text[position:match.start()], color=color))
        token = match.group(0)
        if token.startswith("**"):
            runs.append(docx_run(token[2:-2], bold=True, color=color))
        elif token.startswith("*"):
            runs.append(docx_run(token[1:-1], italic=True, color=color))
        elif token.startswith("`"):
            runs.append(docx_run(token[1:-1], code=True, color=color))
        else:
            label = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token).group(1)
            runs.append(docx_run(label, color=color))
        position = match.end()
    if position < len(str(text or "")):
        runs.append(docx_run(str(text or "")[position:], color=color))
    return "".join(runs) or docx_run("", color=color)


def docx_rich_paragraph(text="", bold=False, color=None, spacing_before=0, spacing_after=0):
    if bold:
        return docx_paragraph(
            text,
            bold=True,
            color=color,
            spacing_before=spacing_before,
            spacing_after=spacing_after,
        )
    paragraph_props = ""
    if spacing_before or spacing_after:
        paragraph_props = (
            "<w:pPr>"
            f"<w:spacing w:before=\"{int(spacing_before)}\" w:after=\"{int(spacing_after)}\"/>"
            "</w:pPr>"
        )
    lines = str(text or "").splitlines() or [""]
    pieces = []
    for index, line in enumerate(lines):
        if index:
            pieces.append("<w:r><w:br/></w:r>")
        pieces.append(docx_inline_runs(line, color=color))
    return f"<w:p>{paragraph_props}{''.join(pieces)}</w:p>"


def markdown_to_docx_paragraphs(text, color=None):
    paragraphs = []
    pending = []
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            if pending:
                paragraphs.append(docx_rich_paragraph(" ".join(pending), color=color))
                pending.clear()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        quote = re.match(r"^>\s?(.+)$", stripped)
        if heading:
            if pending:
                paragraphs.append(docx_rich_paragraph(" ".join(pending), color=color))
                pending.clear()
            paragraphs.append(docx_rich_paragraph(heading.group(2), bold=True, color=color))
        elif bullet:
            if pending:
                paragraphs.append(docx_rich_paragraph(" ".join(pending), color=color))
                pending.clear()
            paragraphs.append(docx_rich_paragraph(f"- {bullet.group(1)}", color=color))
        elif quote:
            if pending:
                paragraphs.append(docx_rich_paragraph(" ".join(pending), color=color))
                pending.clear()
            paragraphs.append(docx_rich_paragraph(f"> {quote.group(1)}", color=color))
        else:
            pending.append(stripped)
    if pending:
        paragraphs.append(docx_rich_paragraph(" ".join(pending), color=color))
    return paragraphs or [docx_paragraph()]


def docx_cell(paragraphs, width="4680", shade=None):
    body = "".join(paragraphs) or docx_paragraph()
    shade_xml = f"<w:shd w:fill=\"{docx_text(shade).lstrip('#')}\"/>" if shade else ""
    return (
        "<w:tc>"
        "<w:tcPr>"
        f"<w:tcW w:w=\"{width}\" w:type=\"dxa\"/>"
        "<w:tcMar>"
        "<w:top w:w=\"120\" w:type=\"dxa\"/>"
        "<w:left w:w=\"120\" w:type=\"dxa\"/>"
        "<w:bottom w:w=\"120\" w:type=\"dxa\"/>"
        "<w:right w:w=\"120\" w:type=\"dxa\"/>"
        "</w:tcMar>"
        f"{shade_xml}"
        "</w:tcPr>"
        f"{body}"
        "</w:tc>"
    )


def docx_row(cells):
    return f"<w:tr>{''.join(cells)}</w:tr>"


def docx_table(rows, column_count=2, column_widths=None):
    column_count = max(int(column_count or 1), 1)
    if not column_widths:
        width = str(max(9360 // column_count, 1))
        column_widths = [width] * column_count
    grid = "".join(f"<w:gridCol w:w=\"{width}\"/>" for width in column_widths)
    return (
        "<w:tbl>"
        "<w:tblPr>"
        "<w:tblW w:w=\"0\" w:type=\"auto\"/>"
        "<w:tblBorders>"
        "<w:top w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "<w:left w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "<w:bottom w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "<w:right w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "<w:insideH w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "<w:insideV w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"D6DBE1\"/>"
        "</w:tblBorders>"
        "<w:tblCellMar>"
        "<w:top w:w=\"120\" w:type=\"dxa\"/>"
        "<w:left w:w=\"120\" w:type=\"dxa\"/>"
        "<w:bottom w:w=\"120\" w:type=\"dxa\"/>"
        "<w:right w:w=\"120\" w:type=\"dxa\"/>"
        "</w:tblCellMar>"
        "</w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{''.join(rows)}"
        "</w:tbl>"
    )


def docx_header_xml(paragraphs):
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:hdr xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"{''.join(paragraphs)}"
        "</w:hdr>"
    )


def docx_footer_xml(paragraphs):
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:ftr xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"{''.join(paragraphs)}"
        "</w:ftr>"
    )


def docx_section_break(header_rid=None, footer_rid=None, next_page=False):
    refs = []
    if next_page:
        refs.append("<w:type w:val=\"nextPage\"/>")
    if header_rid:
        refs.append(f"<w:headerReference w:type=\"default\" r:id=\"{header_rid}\"/>")
    if footer_rid:
        refs.append(f"<w:footerReference w:type=\"default\" r:id=\"{footer_rid}\"/>")
    refs.append("<w:pgSz w:w=\"12240\" w:h=\"15840\"/>")
    refs.append("<w:pgMar w:top=\"720\" w:right=\"720\" w:bottom=\"720\" w:left=\"720\"/>")
    return (
        "<w:p><w:pPr><w:sectPr>"
        f"{''.join(refs)}"
        "</w:sectPr></w:pPr></w:p>"
    )


DOCX_LANGUAGE_COLORS = [
    "1F4E79",
    "8A3FFC",
    "00856F",
    "B45309",
    "B91C1C",
    "4F46E5",
    "0F766E",
    "A21CAF",
    "92400E",
    "0369A1",
]


def docx_language_color(language, fallback_index=0):
    text = str(language or "")
    if text:
        index = sum(ord(char) for char in text) % len(DOCX_LANGUAGE_COLORS)
    else:
        index = fallback_index % len(DOCX_LANGUAGE_COLORS)
    return DOCX_LANGUAGE_COLORS[index]


def docx_language_meta(item, label_prefix=""):
    language = item.get("language") or ""
    color = item.get("color") or docx_language_color(language)
    parts = [f"{label_prefix}{language}"]
    if item.get("updated_at"):
        parts.append(f"Updated: {item['updated_at']}")
    return docx_paragraph(" | ".join(parts), bold=True, color=color, spacing_after=80)


def docx_language_cell(item, width):
    language = item.get("language") or ""
    color = item.get("color") or docx_language_color(language)
    paragraphs = [
        *markdown_to_docx_paragraphs(item.get("text", ""), color=color),
        docx_blank_paragraphs(1),
    ]
    if item.get("instructions"):
        paragraphs.append(
            docx_paragraph(
                f"Instructions: {item['instructions']}",
                color=color,
                spacing_before=80,
                spacing_after=80,
            )
        )
    if item.get("comment"):
        paragraphs.append(
            docx_paragraph(
                f"Comment: {item['comment']}",
                color=color,
                spacing_before=80,
                spacing_after=80,
            )
        )
    return docx_cell(paragraphs, width=str(width))


def docx_language_grid(items, label_prefix=""):
    if not items:
        return docx_paragraph("No entries.")
    column_count = len(items) if len(items) <= 2 else 2
    width = 9360 // column_count
    pieces = []
    for index in range(0, len(items), column_count):
        row_items = items[index:index + column_count]
        pieces.extend(docx_language_meta(item, label_prefix=label_prefix) for item in row_items)
        cells = [docx_language_cell(item, width) for item in row_items]
        while len(cells) < column_count:
            cells.append(docx_cell([docx_paragraph("")], width=str(width)))
        pieces.append(
            docx_table(
                [docx_row(cells)],
                column_count=column_count,
                column_widths=[str(width)] * column_count,
            )
        )
        if index + column_count < len(items):
            pieces.append(docx_blank_paragraphs(1))
    return "".join(pieces)


def build_project_docx(project, segments, selected_languages):
    doc_rels = []
    content_overrides = [
        (
            "/word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        )
    ]
    part_files = {}
    rel_counter = 1

    def add_document_part(kind, xml):
        nonlocal rel_counter
        rel_counter += 1
        rel_id = f"rId{rel_counter}"
        path = f"word/{kind}{rel_counter}.xml"
        part_files[path] = xml
        rel_type = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
            f"{kind}"
        )
        doc_rels.append((rel_id, rel_type, f"{kind}{rel_counter}.xml"))
        content_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml."
            f"{kind}+xml"
        )
        content_overrides.append((f"/{path}", content_type))
        return rel_id

    source_languages = []
    all_languages = []
    for segment in segments:
        for source in segment.get("sources", []):
            language = source.get("language") or ""
            if language and language not in source_languages:
                source_languages.append(language)
            if language and language.lower() not in [item.lower() for item in all_languages]:
                all_languages.append(language)
        for translation in segment.get("translations", []):
            language = translation.get("language") or ""
            if language and language.lower() not in [item.lower() for item in all_languages]:
                all_languages.append(language)
    language_colors = {
        language.lower(): DOCX_LANGUAGE_COLORS[index % len(DOCX_LANGUAGE_COLORS)]
        for index, language in enumerate(all_languages)
    }
    source_language_summary = ", ".join(source_languages) or project["source_language"]

    body = [
        docx_paragraph("Translation Export", bold=True),
        docx_paragraph(f"Source languages: {source_language_summary}"),
        docx_paragraph(f"Exported languages: {', '.join(selected_languages)}"),
        docx_paragraph(f"Created: {now_iso()}"),
        docx_section_break(next_page=True),
    ]

    segment_blocks = []
    for segment in segments:
        sources = segment.get("sources", [])
        translations = segment.get("translations", [])
        for item in [*sources, *translations]:
            item["color"] = language_colors.get(
                str(item.get("language") or "").lower(),
                docx_language_color(item.get("language")),
            )
        block = [docx_paragraph(f"ID: {segment['identifier']}", bold=True)]

        if len(sources) == 1 and len(translations) == 1:
            block.append(docx_language_meta(sources[0], label_prefix="Source: "))
            block.append(docx_language_meta(translations[0], label_prefix="Translation: "))
            block.append(
                docx_table(
                    [
                        docx_row(
                            [
                                docx_language_cell(sources[0], 4680),
                                docx_language_cell(translations[0], 4680),
                            ]
                        )
                    ],
                    column_count=2,
                    column_widths=["4680", "4680"],
                )
            )
        else:
            block.append(docx_paragraph("Sources", bold=True, spacing_before=120, spacing_after=80))
            block.append(docx_language_grid(sources, label_prefix="Source: "))
            block.append(docx_blank_paragraphs(1))
            block.append(
                docx_paragraph("Translations", bold=True, spacing_before=120, spacing_after=80)
            )
            block.append(
                docx_language_grid(translations, label_prefix="Translation: ")
            )
        segment_blocks.append("".join(block))

    for index, block in enumerate(segment_blocks):
        body.append(block)
        if index < len(segment_blocks) - 1:
            body.append(docx_blank_paragraphs(3))

    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document "
        "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        f"<w:body>{''.join(body)}"
        "</w:body></w:document>"
    )
    overrides = "".join(
        f"<Override PartName=\"{part_name}\" ContentType=\"{content_type}\"/>"
        for part_name, content_type in content_overrides
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        f"{overrides}"
        "</Types>"
    )
    root_rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )
    document_rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        + "".join(
            f"<Relationship Id=\"{rel_id}\" Type=\"{rel_type}\" Target=\"{target}\"/>"
            for rel_id, rel_type, target in doc_rels
        )
        + "</Relationships>"
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/document.xml", document_xml)
        for path, xml in part_files.items():
            archive.writestr(path, xml)
    output.seek(0)
    return output.getvalue()


def docx_segments_for_project(conn, project_id, selected_languages, slot_languages=None):
    segment_filter = None
    if slot_languages is not None:
        segment_filter = {
            int(segment_id): set(languages)
            for segment_id, languages in slot_languages.items()
        }
        segment_ids = sorted(segment_filter)
        if not segment_ids:
            return []
        placeholders = ",".join("?" for _ in segment_ids)
        segment_rows = conn.execute(
            f"""
            SELECT id,
                   identifier,
                   ordinal,
                   source_language,
                   source_text,
                   instructions
            FROM segments
            WHERE project_id = ?
              AND id IN ({placeholders})
            ORDER BY ordinal
            """,
            (project_id, *segment_ids),
        ).fetchall()
    else:
        segment_rows = conn.execute(
            """
            SELECT id,
                   identifier,
                   ordinal,
                   source_language,
                   source_text,
                   instructions
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (project_id,),
        ).fetchall()
        segment_ids = [row["id"] for row in segment_rows]

    source_variants_by_segment = {segment_id: [] for segment_id in segment_ids}
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        variant_rows = conn.execute(
            f"""
            SELECT segment_id,
                   source_language,
                   source_text,
                   instructions
            FROM source_variants
            WHERE segment_id IN ({placeholders})
            ORDER BY lower(source_language), source_language
            """,
            tuple(segment_ids),
        ).fetchall()
        for row in variant_rows:
            source_variants_by_segment.setdefault(row["segment_id"], []).append(row)

    translations_by_segment = {segment_id: {} for segment_id in segment_ids}
    for target_language in selected_languages:
        if not segment_ids:
            continue
        placeholders = ",".join("?" for _ in segment_ids)
        rows = conn.execute(
            f"""
            SELECT s.id AS segment_id,
                   COALESCE(t.target_text, '') AS target_text,
                   COALESCE(t.target_instructions, '') AS target_instructions,
                   COALESCE(t.comment, '') AS comment,
                   t.updated_by,
                   t.updated_at
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
              AND s.id IN ({placeholders})
            ORDER BY s.ordinal
            """,
            (target_language, project_id, *segment_ids),
        ).fetchall()
        for row in rows:
            translations_by_segment[row["segment_id"]][target_language] = row

    docx_segments = []
    for segment in segment_rows:
        seen_sources = {str(segment["source_language"] or "").lower()}
        sources = [
            {
                "language": segment["source_language"],
                "text": segment["source_text"],
                "instructions": segment["instructions"],
            }
        ]
        for row in source_variants_by_segment.get(segment["id"], []):
            language_key = str(row["source_language"] or "").lower()
            if language_key in seen_sources:
                continue
            seen_sources.add(language_key)
            sources.append(
                {
                    "language": row["source_language"],
                    "text": row["source_text"],
                    "instructions": row["instructions"],
                }
            )

        translations = []
        for target_language in selected_languages:
            if (
                segment_filter is not None
                and target_language not in segment_filter.get(segment["id"], set())
            ):
                continue
            translation = translations_by_segment.get(segment["id"], {}).get(target_language)
            translations.append(
                {
                    "language": target_language,
                    "text": translation["target_text"] if translation else "",
                    "instructions": (
                        translation["target_instructions"] if translation else ""
                    ),
                    "comment": translation["comment"] if translation else "",
                    "updated_by": translation["updated_by"] if translation else "",
                    "updated_at": translation["updated_at"] if translation else "",
                }
            )

        if translations:
            docx_segments.append(
                {
                    "identifier": segment["identifier"],
                    "ordinal": segment["ordinal"],
                    "sources": sources,
                    "translations": translations,
                }
            )
    return docx_segments


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": "postgres" if USE_POSTGRES else "sqlite"}


@app.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/favicon.svg"), code=302)


@app.route("/register", methods=["GET", "POST"])
def register():
    registration_token_required = bool(REGISTRATION_TOKEN)
    supplied_token = request.values.get("token", "").strip()
    token_ok = not registration_token_required or secrets.compare_digest(
        supplied_token, REGISTRATION_TOKEN
    )

    if request.method == "POST":
        if not token_ok:
            flash("Registration invite token is invalid.")
            return render_template(
                "register.html",
                registration_token_required=registration_token_required,
                registration_token=supplied_token,
            )

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3 or len(password) < 6:
            flash("Use a username with 3+ characters and a password with 6+ characters.")
            return render_template(
                "register.html",
                registration_token_required=registration_token_required,
                registration_token=supplied_token,
            )

        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), now_iso()),
                )
        except DB_INTEGRITY_ERRORS:
            flash("That username is already taken.")
            return render_template(
                "register.html",
                registration_token_required=registration_token_required,
                registration_token=supplied_token,
            )

        flash("Account created. You can log in now.")
        return redirect(url_for("login"))

    return render_template(
        "register.html",
        registration_token_required=registration_token_required,
        registration_token=supplied_token,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.")
            return render_template("login.html", login_open=True)

        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))

    return render_template("login.html", login_open=False)


@app.get("/about")
def about():
    return render_template("about.html")


@app.get("/rhaetochat")
def rhaetochat():
    return render_template("rhaetochat.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    user = require_login()
    if not is_db_row(user):
        return user
    with db() as conn:
        projects = conn.execute(
            """
            SELECT p.*,
                   COUNT(DISTINCT s.id) AS segment_count,
                   COUNT(DISTINCT pl.id) AS language_count
            FROM projects p
            LEFT JOIN segments s ON s.project_id = p.id
            LEFT JOIN project_languages pl ON pl.project_id = p.id
            WHERE p.owner_id = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return render_template("dashboard.html", user=user, projects=projects)


@app.route("/formats", methods=["GET", "POST"])
def import_formats():
    user = require_login()
    if not is_db_row(user):
        return user

    if request.method == "POST":
        payload = import_format_payload(request.form)
        if not payload["name"]:
            flash("Format name is required.")
            return redirect(url_for("import_formats"))
        if not payload["source_text_path"]:
            flash("Source text path is required.")
            return redirect(url_for("import_formats"))
        with db() as conn:
            ensure_default_import_formats(conn, user["id"])
            insert_import_format(conn, user["id"], payload)
            conn.commit()
        flash("Import format created.")
        return redirect(url_for("import_formats"))

    formats = user_import_formats(user["id"])
    return render_template("import_formats.html", formats=formats)


@app.route("/formats/<int:format_id>", methods=["GET", "POST"])
def edit_import_format(format_id):
    user = require_login()
    if not is_db_row(user):
        return user

    if request.method == "POST":
        payload = import_format_payload(request.form)
        if not payload["name"]:
            flash("Format name is required.")
            return redirect(url_for("edit_import_format", format_id=format_id))
        if not payload["source_text_path"]:
            flash("Source text path is required.")
            return redirect(url_for("edit_import_format", format_id=format_id))
        with db() as conn:
            ensure_default_import_formats(conn, user["id"])
            existing = conn.execute(
                "SELECT * FROM import_formats WHERE id = ? AND owner_id = ?",
                (format_id, user["id"]),
            ).fetchone()
            if not existing:
                abort(404)
            update_import_format(conn, format_id, user["id"], payload)
            conn.commit()
        flash("Import format updated.")
        return redirect(url_for("import_formats"))

    import_format = import_format_for_owner(format_id, user["id"])
    if not import_format:
        abort(404)
    return render_template("import_format_edit.html", import_format=import_format)


@app.post("/formats/<int:format_id>/delete")
def delete_import_format(format_id):
    user = require_login()
    if not is_db_row(user):
        return user
    with db() as conn:
        ensure_default_import_formats(conn, user["id"])
        conn.execute(
            "UPDATE projects SET import_format_id = NULL WHERE owner_id = ? AND import_format_id = ?",
            (user["id"], format_id),
        )
        conn.execute(
            "DELETE FROM import_formats WHERE id = ? AND owner_id = ?",
            (format_id, user["id"]),
        )
        conn.commit()
    flash("Import format deleted.")
    return redirect(url_for("import_formats"))


@app.route("/projects/new", methods=["GET", "POST"])
def new_project():
    user = require_login()
    if not is_db_row(user):
        return user
    import_formats = user_import_formats(user["id"])
    form_state = new_project_form_state()

    def render_new_project_form():
        return render_template(
            "new_project.html",
            import_formats=import_formats,
            form_state=form_state,
        )

    if request.method == "POST":
        form_state = new_project_form_state(request.form)
        name = request.form.get("name", "").strip()
        upload = request.files.get("jsonl")
        import_format_id = int_or_none(request.form.get("import_format_id"))
        if import_format_id not in {item["id"] for item in import_formats}:
            import_format_id = None
        source_language_manual = request.form.get("source_language_manual") == "1"
        manual_source_language = request.form.get("manual_source_language", "").strip()
        mapping = {
            "file_type": request.form.get("file_type", "jsonl").strip(),
            "rows_path": request.form.get("rows_path", "").strip(),
            "source_text_key": request.form.get("source_text_key", "").strip(),
            "source_text_is_list": request.form.get("source_text_is_list") == "1",
            "instruction_key": (
                request.form.get("instruction_key_custom", "").strip()
                or request.form.get("instruction_key", "").strip()
            ),
            "identifier_key": request.form.get("identifier_key", "").strip(),
            "source_language_key": "",
            "manual_source_language": "",
            "has_seed_translation": request.form.get("has_seed_translation") == "1",
            "target_language_key": request.form.get("target_language_key", "").strip(),
            "target_text_key": (
                request.form.get("target_text_key_custom", "").strip()
                or request.form.get("target_text_key", "").strip()
            ),
            "target_text_is_list": request.form.get("target_text_is_list") == "1",
            "translated_instruction_key": (
                request.form.get("translated_instruction_key_custom", "").strip()
                or request.form.get("translated_instruction_key", "").strip()
            ),
            "target_language_name": request.form.get("target_language_name", "").strip(),
            "seed_translation_status": request.form.get(
                "seed_translation_status",
                "submitted",
            ).strip(),
        }
        if source_language_manual:
            mapping["manual_source_language"] = manual_source_language
        else:
            mapping["source_language_key"] = request.form.get(
                "source_language_key",
                "",
            ).strip()
        source_editable = 1 if request.form.get("source_editable") == "1" else 0

        if not name or not upload or not upload.filename:
            flash("Project name and JSON or JSONL file are required.")
            return render_new_project_form()

        if source_language_manual and not manual_source_language:
            flash("Enter a source language or choose a source-language key.")
            return render_new_project_form()

        if mapping["has_seed_translation"]:
            if not mapping["target_language_name"] and not mapping["target_language_key"]:
                flash("Enter the language name for the imported translations or choose a language key.")
                return render_new_project_form()
            if not mapping["target_text_key"]:
                flash("Choose the key that contains the imported translation text.")
                return render_new_project_form()
            mapping["seed_translation_status"] = import_translation_mode(
                mapping["seed_translation_status"]
            )

        try:
            source_language, rows = parse_jsonl(upload, mapping)
        except ValueError as exc:
            flash(str(exc))
            return render_new_project_form()

        upload.stream.seek(0)
        filename = secure_filename(upload.filename)
        upload.save(UPLOAD_DIR / f"{datetime.now().timestamp()}-{filename}")
        format_name = next(
            (item["name"] for item in import_formats if item["id"] == import_format_id),
            "manual-import",
        )
        import_mapping = import_mapping_snapshot(mapping, format_name)

        with db() as conn:
            project_id = insert_and_get_id(
                conn,
                """
                INSERT INTO projects
                    (
                        owner_id,
                        name,
                        source_language,
                        source_editable,
                        import_format_id,
                        import_mapping,
                        created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    name,
                    source_language,
                    source_editable,
                    import_format_id,
                    import_mapping,
                    now_iso(),
                ),
            )

            for ordinal, row in enumerate(rows, start=1):
                segment_id = insert_and_get_id(
                    conn,
                    """
                    INSERT INTO segments
                        (
                            project_id,
                            identifier,
                            ordinal,
                            source_language,
                            source_text,
                            instructions,
                            metadata,
                            created_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        row["identifier"],
                        ordinal,
                        row["source_language"],
                        row["source_text"],
                        row["instructions"],
                        json.dumps(row["metadata"], ensure_ascii=False),
                        now_iso(),
                    ),
                )

                if row["target_language"] and row["target_text"] is not None:
                    stamp = now_iso()
                    payload = imported_translation_payload(
                        row["source_text"],
                        row["target_text"],
                        row["target_comment"],
                        mapping["seed_translation_status"],
                        user["username"],
                        stamp,
                        row["target_instructions"],
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO project_languages
                            (project_id, target_language, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (project_id, str(row["target_language"]), stamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO translations
                            (
                                segment_id,
                                target_language,
                                target_text,
                                draft_text,
                                target_instructions,
                                draft_instructions,
                                comment,
                                draft_comment,
                                status,
                                qa_warnings,
                                version,
                                updated_by,
                                draft_updated_by,
                                updated_at,
                                draft_updated_at
                            )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            segment_id,
                            str(row["target_language"]),
                            payload["target_text"],
                            payload["draft_text"],
                            payload["target_instructions"],
                            payload["draft_instructions"],
                            payload["comment"],
                            payload["draft_comment"],
                            payload["status"],
                            payload["qa_warnings"],
                            payload["version"],
                            payload["updated_by"],
                            payload["draft_updated_by"],
                            payload["updated_at"],
                            payload["draft_updated_at"],
                        ),
                    )

            conn.commit()

        flash("Project imported.")
        return redirect(url_for("project_detail", project_id=project_id))

    return render_new_project_form()


@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])

    if request.method == "POST":
        action = request.form.get("action", "add_language")

        with db() as conn:
            if action == "upload_language_translations":
                target_language = request.form.get("upload_target_language", "").strip()
                upload = request.files.get("translation_jsonl")
                id_key = request.form.get("message_id_key", "message_id")
                target_text_key = request.form.get("translation_text_key", "")
                comment_key = request.form.get("translation_comment_key", "")
                instruction_key = request.form.get("translated_instruction_key", "")
                language_key = request.form.get("translation_language_key", "")
                import_status = request.form.get(
                    "uploaded_translation_status",
                    "submitted",
                ).strip()
                import_status = import_translation_mode(import_status)
                if not upload or not upload.filename:
                    flash("Choose a JSONL file with translations.")
                    return redirect(url_for("project_detail", project_id=project_id))
                try:
                    translation_rows = parse_translation_jsonl(
                        upload,
                        id_key,
                        target_text_key,
                        comment_key,
                        instruction_key,
                        language_key,
                    )
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(url_for("project_detail", project_id=project_id))
                if not target_language and language_key:
                    for row in translation_rows:
                        if row["target_language"]:
                            target_language = row["target_language"]
                            break
                if not target_language:
                    flash("Language name is required.")
                    return redirect(url_for("project_detail", project_id=project_id))

                upload.stream.seek(0)
                filename = secure_filename(upload.filename)
                upload.save(UPLOAD_DIR / f"{datetime.now().timestamp()}-translations-{filename}")

                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_languages
                        (project_id, target_language, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (project_id, target_language, now_iso()),
                )
                stamp = now_iso()
                created_count = 0
                updated_count = 0
                empty_count = 0
                unmatched_count = 0
                unmatched_examples = []
                segments = conn.execute(
                    """
                    SELECT *
                    FROM segments
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
                segment_lookup = {}
                for segment in segments:
                    segment_lookup.setdefault(str(segment["identifier"]), segment)
                    metadata_identifier = str(
                        metadata_dict(segment["metadata"]).get(id_key, "")
                    ).strip()
                    if metadata_identifier:
                        segment_lookup.setdefault(metadata_identifier, segment)

                for row in translation_rows:
                    segment = segment_lookup.get(row["identifier"])
                    if not segment:
                        unmatched_count += 1
                        if len(unmatched_examples) < 5:
                            unmatched_examples.append(row["identifier"])
                        continue
                    if not row["target_text"].strip():
                        empty_count += 1
                        continue

                    current = conn.execute(
                        """
                        SELECT *
                        FROM translations
                        WHERE segment_id = ?
                          AND lower(target_language) = lower(?)
                        """,
                        (segment["id"], target_language),
                    ).fetchone()
                    payload = imported_translation_payload(
                        segment["source_text"],
                        row["target_text"],
                        row["comment"],
                        import_status,
                        user["username"],
                        stamp,
                        row["target_instructions"],
                    )
                    if current:
                        conn.execute(
                            """
                            UPDATE translations
                               SET target_text = ?,
                                   draft_text = ?,
                                   target_instructions = ?,
                                   draft_instructions = ?,
                                   comment = ?,
                                   draft_comment = ?,
                                   status = ?,
                                   qa_warnings = ?,
                                   version = version + 1,
                                   updated_by = ?,
                                   draft_updated_by = ?,
                                   draft_updated_at = ?,
                                   updated_at = ?
                             WHERE id = ?
                            """,
                            (
                                payload["target_text"],
                                payload["draft_text"],
                                payload["target_instructions"],
                                payload["draft_instructions"],
                                payload["comment"],
                                payload["draft_comment"],
                                payload["status"],
                                payload["qa_warnings"],
                                payload["updated_by"],
                                payload["draft_updated_by"],
                                payload["draft_updated_at"],
                                payload["updated_at"],
                                current["id"],
                            ),
                        )
                        updated_count += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO translations
                                (
                                    segment_id,
                                    target_language,
                                    target_text,
                                    draft_text,
                                    target_instructions,
                                    draft_instructions,
                                    comment,
                                    draft_comment,
                                    status,
                                    qa_warnings,
                                    version,
                                    updated_by,
                                    draft_updated_by,
                                    updated_at,
                                    draft_updated_at
                                )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                segment["id"],
                                target_language,
                                payload["target_text"],
                                payload["draft_text"],
                                payload["target_instructions"],
                                payload["draft_instructions"],
                                payload["comment"],
                                payload["draft_comment"],
                                payload["status"],
                                payload["qa_warnings"],
                                payload["version"],
                                payload["updated_by"],
                                payload["draft_updated_by"],
                                payload["updated_at"],
                                payload["draft_updated_at"],
                            ),
                        )
                        created_count += 1

                conn.commit()
                flash(
                    "Translations uploaded: "
                    f"{created_count} created, {updated_count} updated, "
                    f"{unmatched_count} unmatched, {empty_count} empty."
                    + (
                        f" Unmatched IDs: {', '.join(unmatched_examples)}."
                        if unmatched_examples
                        else ""
                    )
                )
                return redirect(
                    url_for(
                        "language_detail",
                        project_id=project_id,
                        target_language=target_language,
                    )
                )

            if action == "upload_source_variants":
                source_language = request.form.get("variant_source_language", "").strip()
                upload = request.files.get("source_variant_file")
                file_type = request.form.get("source_variant_file_type", "jsonl")
                rows_path = request.form.get("source_variant_rows_path", "")
                id_key = request.form.get("source_variant_id_key", "identifier")
                source_text_key = request.form.get("source_variant_text_key", "")
                language_key = request.form.get("source_variant_language_key", "")
                instruction_key = request.form.get("source_variant_instruction_key", "")
                source_text_is_list = request.form.get("source_variant_text_is_list") == "1"
                if not upload or not upload.filename:
                    flash("Choose a JSON or JSONL file with alternative source texts.")
                    return redirect(url_for("project_detail", project_id=project_id))
                try:
                    source_rows = parse_source_variant_upload(
                        upload,
                        id_key,
                        source_text_key,
                        source_language,
                        language_key,
                        instruction_key,
                        file_type,
                        rows_path,
                        source_text_is_list,
                    )
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(url_for("project_detail", project_id=project_id))

                upload.stream.seek(0)
                filename = secure_filename(upload.filename)
                upload.save(UPLOAD_DIR / f"{datetime.now().timestamp()}-source-variants-{filename}")

                segments = conn.execute(
                    """
                    SELECT *
                    FROM segments
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
                lookup_keys = {id_key, *SOURCE_VARIANT_ID_KEYS}
                segment_lookup = {}
                for segment in segments:
                    segment_lookup.setdefault(str(segment["identifier"]).strip(), segment)
                    metadata = metadata_dict(segment["metadata"])
                    for key in lookup_keys:
                        if not key:
                            continue
                        metadata_identifier = str(metadata.get(key, "")).strip()
                        if metadata_identifier:
                            segment_lookup.setdefault(metadata_identifier, segment)

                stamp = now_iso()
                created_count = 0
                updated_count = 0
                unmatched_count = 0
                empty_count = 0
                missing_language_count = 0
                primary_language_count = 0
                unmatched_examples = []
                for row in source_rows:
                    segment = segment_lookup.get(row["identifier"])
                    if not segment:
                        unmatched_count += 1
                        if len(unmatched_examples) < 5:
                            unmatched_examples.append(row["identifier"])
                        continue
                    row_language = str(row["source_language"] or "").strip()
                    if not row_language:
                        missing_language_count += 1
                        continue
                    if row_language.lower() == str(segment["source_language"]).lower():
                        primary_language_count += 1
                        continue
                    if not row["source_text"].strip():
                        empty_count += 1
                        continue

                    current = conn.execute(
                        """
                        SELECT *
                        FROM source_variants
                        WHERE segment_id = ?
                          AND lower(source_language) = lower(?)
                        """,
                        (segment["id"], row_language),
                    ).fetchone()
                    if current:
                        conn.execute(
                            """
                            UPDATE source_variants
                               SET source_language = ?,
                                   source_text = ?,
                                   instructions = ?,
                                   uploaded_by = ?,
                                   updated_at = ?
                             WHERE id = ?
                            """,
                            (
                                row_language,
                                row["source_text"],
                                row["instructions"],
                                user["username"],
                                stamp,
                                current["id"],
                            ),
                        )
                        updated_count += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO source_variants
                                (segment_id, source_language, source_text, instructions, uploaded_by, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                segment["id"],
                                row_language,
                                row["source_text"],
                                row["instructions"],
                                user["username"],
                                stamp,
                            ),
                        )
                        created_count += 1

                conn.commit()
                flash(
                    "Alternative source texts uploaded: "
                    f"{created_count} created, {updated_count} updated, "
                    f"{unmatched_count} unmatched, {empty_count} empty, "
                    f"{missing_language_count} missing language, "
                    f"{primary_language_count} primary-source skipped."
                    + (
                        f" Unmatched IDs: {', '.join(unmatched_examples)}."
                        if unmatched_examples
                        else ""
                    )
                )
                return redirect(url_for("project_detail", project_id=project_id))

            if action == "upload_language_comments":
                target_language = request.form.get("comment_target_language", "").strip()
                upload = request.files.get("comment_csv")
                match_key = request.form.get("comment_match_key", "identifier").strip() or "identifier"
                has_header = request.form.get("comment_csv_has_header") == "1"
                role = request.form.get("comment_import_role", "reviewer").strip()
                role = role if role in COMMENT_ROLES else "reviewer"
                if not target_language:
                    flash("Language name is required.")
                    return redirect(url_for("project_detail", project_id=project_id))
                if not upload or not upload.filename:
                    flash("Choose a CSV file with comments.")
                    return redirect(url_for("project_detail", project_id=project_id))
                try:
                    comment_rows = parse_comment_csv(upload, has_header)
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(url_for("project_detail", project_id=project_id))

                upload.stream.seek(0)
                filename = secure_filename(upload.filename)
                upload.save(UPLOAD_DIR / f"{datetime.now().timestamp()}-comments-{filename}")

                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_languages
                        (project_id, target_language, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (project_id, target_language, now_iso()),
                )

                segments = conn.execute(
                    """
                    SELECT *
                    FROM segments
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
                segment_lookup = {}
                for segment in segments:
                    for value in segment_match_values(segment, match_key):
                        segment_lookup.setdefault(value, []).append(segment)

                created_count = 0
                unmatched_count = 0
                empty_count = 0
                missing_id_count = 0
                matched_row_count = 0
                unmatched_examples = []
                for row in comment_rows:
                    identifier = row["identifier"]
                    if not identifier:
                        missing_id_count += 1
                        continue
                    if not row["comment"].strip():
                        empty_count += 1
                        continue
                    matching_segments = segment_lookup.get(identifier, [])
                    if not matching_segments:
                        unmatched_count += 1
                        if len(unmatched_examples) < 5:
                            unmatched_examples.append(identifier)
                        continue
                    matched_row_count += 1
                    for segment in matching_segments:
                        if add_translation_comment(
                            conn,
                            segment["id"],
                            target_language,
                            role,
                            row["comment"],
                            user["username"],
                        ):
                            created_count += 1

                conn.commit()
                flash(
                    "Comments imported: "
                    f"{created_count} created from {matched_row_count} matched CSV row(s), "
                    f"{unmatched_count} unmatched, "
                    f"{empty_count} empty"
                    + (f", {missing_id_count} missing ID" if missing_id_count else "")
                    + f". Matched on '{match_key}'."
                    + (
                        f" Unmatched IDs: {', '.join(unmatched_examples)}."
                        if unmatched_examples
                        else ""
                    )
                )
                return redirect(
                    url_for(
                        "language_detail",
                        project_id=project_id,
                        target_language=target_language,
                    )
                )

            if action == "create_reviewer_link":
                reviewer_name_value = request.form.get("reviewer_name", "").strip()
                if not reviewer_name_value:
                    flash("Reviewer name is required.")
                    return redirect(url_for("project_detail", project_id=project_id))
                insert_and_get_id(
                    conn,
                    """
                    INSERT INTO review_links
                        (project_id, token, reviewer_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        secrets.token_urlsafe(24),
                        reviewer_name_value[:80],
                        now_iso(),
                    ),
                )
                conn.commit()
                flash("Reviewer link created.")
                return redirect(url_for("project_detail", project_id=project_id))

            if action == "create_creator_link":
                creator_name_value = request.form.get("creator_name", "").strip()
                if not creator_name_value:
                    flash("Creator name is required.")
                    return redirect(url_for("project_detail", project_id=project_id))
                insert_and_get_id(
                    conn,
                    """
                    INSERT INTO creator_links
                        (project_id, token, creator_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        secrets.token_urlsafe(24),
                        creator_name_value[:80],
                        now_iso(),
                    ),
                )
                conn.commit()
                flash("Creator link created.")
                return redirect(url_for("project_detail", project_id=project_id))

            target_language = request.form.get("target_language", "").strip()
            created, error = create_project_language(conn, project_id, target_language)
            if error:
                flash(error)
                return redirect(url_for("project_detail", project_id=project_id))
            conn.commit()

        flash("Language added.")
        return redirect(
            url_for("language_detail", project_id=project_id, target_language=target_language)
        )

    with db() as conn:
        segment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE project_id = ?",
            (project_id,),
        ).fetchone()["count"]
        metadata_keys = project_metadata_keys(conn, project_id)
        source_rows = source_language_rows(conn, project_id)
        language_rows = conn.execute(
            """
            WITH target_languages AS (
                SELECT target_language
                FROM project_languages
                WHERE project_id = ?
            ),
            translator_stats AS (
                SELECT target_language,
                       COUNT(*) AS translator_slots,
                       COUNT(DISTINCT NULLIF(translator_name, '')) AS assigned_translators,
                       COALESCE(SUM(credit_limit), 0) AS total_credits
                FROM share_links
                WHERE project_id = ?
                  AND revoked_at IS NULL
                GROUP BY target_language
            ),
            claim_stats AS (
                SELECT sl.target_language,
                       COUNT(DISTINCT c.segment_id) AS claimed_completed,
                       MAX(c.completed_at) AS last_active
                FROM share_links sl
                LEFT JOIN translation_claims c
                  ON c.share_link_id = sl.id
                 AND c.status = 'completed'
                WHERE sl.project_id = ?
                  AND sl.revoked_at IS NULL
                GROUP BY sl.target_language
            )
            SELECT tl.target_language,
                   COUNT(DISTINCT s.id) AS total_segments,
                   COUNT(
                       DISTINCT CASE
                           WHEN trim(COALESCE(t.target_text, '')) != '' THEN s.id
                       END
                   ) AS completed_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN s.id END) AS needs_revision_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN s.id END) AS approved_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN s.id END) AS warning_segments,
                   COALESCE(MAX(ls.translator_slots), 0) AS translator_count,
                   COALESCE(MAX(ls.total_credits), 0) AS total_assignments,
                   COALESCE(MAX(cs.claimed_completed), 0) AS submitted_assignments,
                   MAX(cs.last_active) AS last_active
            FROM target_languages tl
            JOIN segments s ON s.project_id = ?
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(tl.target_language)
            LEFT JOIN translator_stats ls
              ON lower(ls.target_language) = lower(tl.target_language)
            LEFT JOIN claim_stats cs
              ON lower(cs.target_language) = lower(tl.target_language)
            GROUP BY tl.target_language
            ORDER BY tl.target_language
            """,
            (project_id, project_id, project_id, project_id),
        ).fetchall()
        review_links = conn.execute(
            """
            SELECT rl.*,
                   COUNT(DISTINCT CASE WHEN t.status = 'approved' THEN t.id END) AS reviewed_translations,
                   COUNT(DISTINCT t.id) AS total_translations
            FROM review_links rl
            LEFT JOIN segments s ON s.project_id = rl.project_id
            LEFT JOIN translations t ON t.segment_id = s.id
            WHERE rl.project_id = ?
            GROUP BY rl.id
            ORDER BY rl.created_at
            """,
            (project_id,),
        ).fetchall()
        creator_links = conn.execute(
            """
            SELECT cl.*,
                   COUNT(DISTINCT CASE WHEN s.source_status = 'reviewed' THEN s.id END) AS reviewed_sources,
                   COUNT(DISTINCT s.id) AS total_sources
            FROM creator_links cl
            LEFT JOIN segments s ON s.project_id = cl.project_id
            WHERE cl.project_id = ?
            GROUP BY cl.id
            ORDER BY cl.created_at
            """,
            (project_id,),
        ).fetchall()

    return render_template(
        "project_detail.html",
        project=project,
        language_rows=language_rows,
        source_rows=source_rows,
        metadata_keys=metadata_keys,
        segment_count=segment_count,
        review_links=review_links,
        creator_links=creator_links,
    )


@app.post("/projects/<int:project_id>/delete")
def delete_project(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    confirmation = request.form.get("project_name", "").strip()
    if confirmation != project["name"]:
        flash("Project name confirmation did not match. Project was not deleted.")
        return redirect(url_for("project_detail", project_id=project_id))

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if not row:
            abort(404)
        conn.execute("DELETE FROM projects WHERE id = ? AND owner_id = ?", (project_id, user["id"]))
        conn.commit()

    flash(f"Project '{project['name']}' deleted.")
    return redirect(url_for("dashboard"))


@app.post("/projects/<int:project_id>/languages/<path:target_language>/delete")
def delete_project_language(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    confirmation = request.form.get("target_language", "").strip()
    if confirmation != target_language:
        flash("Language confirmation did not match. Language was not removed.")
        return redirect(
            url_for(
                "language_detail",
                project_id=project_id,
                target_language=target_language,
            )
        )

    with db() as conn:
        language = conn.execute(
            """
            SELECT *
            FROM project_languages
            WHERE project_id = ?
              AND lower(target_language) = lower(?)
            """,
            (project_id, target_language),
        ).fetchone()
        if not language:
            abort(404)

        segment_filter = "SELECT id FROM segments WHERE project_id = ?"
        conn.execute(
            f"""
            DELETE FROM translation_comments
             WHERE lower(target_language) = lower(?)
               AND segment_id IN ({segment_filter})
            """,
            (target_language, project_id),
        )
        conn.execute(
            f"""
            DELETE FROM source_flags
             WHERE lower(target_language) = lower(?)
               AND segment_id IN ({segment_filter})
            """,
            (target_language, project_id),
        )
        conn.execute(
            f"""
            DELETE FROM translations
             WHERE lower(target_language) = lower(?)
               AND segment_id IN ({segment_filter})
            """,
            (target_language, project_id),
        )
        conn.execute(
            """
            DELETE FROM share_links
             WHERE project_id = ?
               AND lower(target_language) = lower(?)
            """,
            (project_id, target_language),
        )
        conn.execute(
            """
            DELETE FROM project_languages
             WHERE project_id = ?
               AND lower(target_language) = lower(?)
            """,
            (project_id, target_language),
        )
        conn.commit()

    flash(f"Language '{target_language}' removed from project '{project['name']}'.")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/projects/<int:project_id>/update-import", methods=["GET", "POST"])
def project_update_import(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    if request.method == "POST":
        upload = request.files.get("jsonl")
        if not upload or not upload.filename:
            flash("Choose a revised JSON or JSONL file.")
            return render_template("project_update_import.html", project=project)

        try:
            _source_language, rows = parse_jsonl(
                upload,
                parse_mapping_from_config(project_import_config(project, user["id"])),
            )
        except ValueError as exc:
            flash(str(exc))
            return render_template("project_update_import.html", project=project)

        upload.stream.seek(0)
        filename = secure_filename(upload.filename)
        upload.save(UPLOAD_DIR / f"{datetime.now().timestamp()}-update-{filename}")

        matched = 0
        changed = 0
        skipped = 0
        flagged = 0
        stamp = now_iso()
        with db() as conn:
            for row in rows:
                segment = conn.execute(
                    """
                    SELECT *
                    FROM segments
                    WHERE project_id = ?
                      AND identifier = ?
                    """,
                    (project_id, row["identifier"]),
                ).fetchone()
                if not segment:
                    skipped += 1
                    continue
                matched += 1
                metadata_json = json.dumps(row["metadata"], ensure_ascii=False)
                existing_metadata_json = json.dumps(
                    metadata_dict(segment["metadata"]), ensure_ascii=False
                )
                changed_source = (
                    segment["source_language"] != row["source_language"]
                    or segment["source_text"] != row["source_text"]
                    or segment["instructions"] != row["instructions"]
                    or existing_metadata_json != metadata_json
                )
                if not changed_source:
                    continue

                conn.execute(
                    """
                    UPDATE segments
                       SET source_language = ?,
                           source_text = ?,
                           instructions = ?,
                           metadata = ?
                     WHERE id = ?
                    """,
                    (
                        row["source_language"],
                        row["source_text"],
                        row["instructions"],
                        metadata_json,
                        segment["id"],
                    ),
                )
                changed += 1

                translations = conn.execute(
                    """
                    SELECT *
                    FROM translations
                    WHERE segment_id = ?
                    """,
                    (segment["id"],),
                ).fetchall()
                for translation in translations:
                    qa_warnings = qa_warnings_json(row["source_text"], translation["target_text"])
                    status = translation["status"]
                    if translation["target_text"].strip():
                        status = "needs_revision"
                        flagged += 1
                    else:
                        status = normalize_status(status, "", translation["draft_text"])
                    conn.execute(
                        """
                        UPDATE translations
                           SET status = ?,
                               qa_warnings = ?,
                               version = version + 1,
                               updated_by = ?,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (
                            status,
                            qa_warnings,
                            user["username"],
                            stamp,
                            translation["id"],
                        ),
                    )
                    if translation["target_text"].strip():
                        add_translation_comment(
                            conn,
                            segment["id"],
                            translation["target_language"],
                            "manager",
                            "Source text changed during JSONL update import; please review this translation.",
                            user["username"],
                        )
            conn.commit()

        flash(
            f"Update import complete: {matched} matched, {changed} changed, "
            f"{flagged} translation(s) flagged, {skipped} skipped."
        )
        return redirect(url_for("project_detail", project_id=project_id))

    return render_template("project_update_import.html", project=project)


@app.route("/projects/<int:project_id>/languages/<path:target_language>", methods=["GET", "POST"])
def language_detail(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action", "create_link")
        with db() as conn:
            if action == "revoke_translator_link":
                try:
                    link_id = parse_optional_int(request.form.get("link_id"), "Link")
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(
                        url_for(
                            "language_detail",
                            project_id=project_id,
                            target_language=target_language,
                        )
                    )
                if link_id is None:
                    abort(400)
                link = conn.execute(
                    """
                    SELECT * FROM share_links
                    WHERE id = ?
                      AND project_id = ?
                      AND lower(target_language) = lower(?)
                      AND revoked_at IS NULL
                    """,
                    (link_id, project_id, target_language),
                ).fetchone()
                if not link:
                    abort(404)
                stamp = now_iso()
                conn.execute(
                    """
                    UPDATE share_links
                       SET revoked_at = ?,
                           revoked_by = ?
                     WHERE id = ?
                    """,
                    (stamp, user["username"], link_id),
                )
                conn.execute(
                    """
                    DELETE FROM translation_claims
                    WHERE share_link_id = ?
                      AND status = 'claimed'
                    """,
                    (link_id,),
                )
                conn.commit()
                flash(
                    f"Translator access for '{link['translator_name'] or target_language}' revoked. "
                    "Translations, comments, and saved history were kept."
                )
                return redirect(
                    url_for(
                        "language_detail",
                        project_id=project_id,
                        target_language=target_language,
                    )
                )

            if action in {"update_assignments", "update_credits"}:
                try:
                    link_id = parse_optional_int(request.form.get("link_id"), "Link")
                    assignment_limit = (
                        request.form.get("assignment_limit") or request.form.get("credit_limit")
                    )
                    credit_limit = parse_optional_int(
                        assignment_limit, "Assignment limit"
                    )
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(
                        url_for(
                            "language_detail",
                            project_id=project_id,
                            target_language=target_language,
                        )
                    )
                if link_id is None or credit_limit is None:
                    flash("Translator and assignment limit are required.")
                    return redirect(
                        url_for(
                            "language_detail",
                            project_id=project_id,
                            target_language=target_language,
                        )
                    )
                link = conn.execute(
                    """
                    SELECT * FROM share_links
                    WHERE id = ?
                      AND project_id = ?
                      AND lower(target_language) = lower(?)
                      AND revoked_at IS NULL
                    """,
                    (link_id, project_id, target_language),
                ).fetchone()
                if not link:
                    abort(404)
                conn.execute(
                    "UPDATE share_links SET credit_limit = ? WHERE id = ?",
                    (credit_limit, link_id),
                )
                conn.commit()
                flash("Assignment limit updated.")
                return redirect(
                    url_for(
                        "language_detail",
                        project_id=project_id,
                        target_language=target_language,
                    )
                )

            link_id, error = create_quota_link_from_form(
                conn, project_id, target_language, request.form
            )
            if error:
                flash(error)
                return redirect(
                    url_for(
                        "language_detail",
                        project_id=project_id,
                        target_language=target_language,
                    )
                )
            conn.commit()

        flash("Translator added.")
        return redirect(
            url_for("language_detail", project_id=project_id, target_language=target_language)
        )

    with db() as conn:
        segment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE project_id = ?",
            (project_id,),
        ).fetchone()["count"]
        language_stats = conn.execute(
            """
            SELECT ? AS target_language,
                   COUNT(DISTINCT s.id) AS total_segments,
                   COUNT(
                       DISTINCT CASE
                           WHEN trim(COALESCE(t.target_text, '')) != '' THEN s.id
                       END
                   ) AS completed_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN s.id END) AS draft_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN s.id END) AS submitted_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN s.id END) AS needs_revision_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN s.id END) AS approved_segments,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN s.id END) AS warning_segments
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            """,
            (target_language, target_language, project_id),
        ).fetchone()
        links = conn.execute(
            """
            SELECT sl.*,
                   sl.credit_limit AS assignment_limit,
                   COUNT(DISTINCT CASE WHEN c.status = 'completed' THEN c.segment_id END) AS submitted_assignments,
                   COUNT(DISTINCT CASE WHEN c.status = 'claimed' THEN c.segment_id END) AS active_claims,
                   MAX(c.completed_at) AS last_completed_at
            FROM share_links sl
            LEFT JOIN translation_claims c
              ON c.share_link_id = sl.id
            WHERE sl.project_id = ?
              AND lower(sl.target_language) = lower(?)
              AND sl.revoked_at IS NULL
            GROUP BY sl.id
            ORDER BY sl.created_at
            """,
            (project_id, target_language),
        ).fetchall()
        translator_rows = conn.execute(
            """
            SELECT sl.translator_name,
                   COUNT(DISTINCT sl.id) AS link_count,
                   COALESCE(SUM(sl.credit_limit), 0) AS total_assignments,
                   COUNT(DISTINCT CASE WHEN c.status = 'completed' THEN c.segment_id END) AS submitted_assignments,
                   MAX(c.completed_at) AS last_active
            FROM share_links sl
            LEFT JOIN translation_claims c
              ON c.share_link_id = sl.id
            WHERE sl.project_id = ?
              AND lower(sl.target_language) = lower(?)
              AND sl.revoked_at IS NULL
            GROUP BY sl.translator_name
            ORDER BY submitted_assignments DESC, sl.translator_name
            """,
            (project_id, target_language),
        ).fetchall()

    return render_template(
        "language_detail.html",
        project=project,
        target_language=target_language,
        segment_count=segment_count,
        language_stats=language_stats,
        links=links,
        translator_rows=translator_rows,
    )


@app.route("/projects/<int:project_id>/languages/<path:target_language>/texts")
def language_texts(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 500
    offset = (page - 1) * per_page
    filters = {
        "q": request.args.get("q", "").strip(),
        "status": request.args.get("status", "").strip(),
        "metadata": request.args.get("metadata", "").strip(),
        "translator": request.args.get("translator", "").strip(),
        "missing": request.args.get("missing", "").strip(),
        "comments": request.args.get("comments", "").strip(),
        "warnings": request.args.get("warnings", "").strip(),
    }

    where = ["s.project_id = ?"]
    params = [project_id]
    if filters["q"]:
        like = f"%{filters['q'].lower()}%"
        where.append(
            """
            (
                lower(s.identifier) LIKE ?
                OR lower(s.source_text) LIKE ?
                OR lower(COALESCE(t.target_text, '')) LIKE ?
                OR lower(COALESCE(t.draft_text, '')) LIKE ?
                OR lower(COALESCE(t.comment, '')) LIKE ?
                OR lower(COALESCE(t.draft_comment, '')) LIKE ?
            )
            """
        )
        params.extend([like] * 6)
    if filters["status"] in TRANSLATION_STATUSES:
        where.append("COALESCE(t.status, 'untranslated') = ?")
        params.append(filters["status"])
    if filters["metadata"]:
        where.append("lower(COALESCE(s.metadata, '')) LIKE ?")
        params.append(f"%{filters['metadata'].lower()}%")
    if filters["translator"]:
        where.append(
            """
            lower(
                COALESCE(t.updated_by, '') || ' ' ||
                COALESCE(t.draft_updated_by, '') || ' ' ||
                COALESCE(c.translator_name, '')
            ) LIKE ?
            """
        )
        params.append(f"%{filters['translator'].lower()}%")
    if filters["missing"]:
        where.append("trim(COALESCE(t.target_text, '')) = ''")
    if filters["comments"]:
        where.append(
            "trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''"
        )
    if filters["warnings"]:
        where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
    where_sql = " AND ".join(where)

    with db() as conn:
        count_params = [target_language, target_language, *params]
        text_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            LEFT JOIN translation_claims c
              ON c.segment_id = s.id
             AND lower(c.target_language) = lower(?)
            WHERE {where_sql}
            """,
            count_params,
        ).fetchone()["count"]
        overview = conn.execute(
            """
            SELECT COUNT(DISTINCT s.id) AS total,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'untranslated' THEN s.id END) AS untranslated,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN s.id END) AS draft,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN s.id END) AS submitted,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN s.id END) AS needs_revision,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN s.id END) AS approved,
                   COUNT(DISTINCT CASE WHEN trim(COALESCE(t.target_text, '')) = '' THEN s.id END) AS missing,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN s.id END) AS warnings,
                   COUNT(DISTINCT CASE WHEN trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != '' THEN s.id END) AS comments
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            """,
            (target_language, project_id),
        ).fetchone()
        text_rows = conn.execute(
            f"""
            SELECT s.id,
                   s.identifier,
                   s.ordinal,
                   s.source_text,
                   s.instructions,
                   s.metadata,
                   COALESCE(t.target_text, '') AS target_text,
                   COALESCE(t.draft_text, '') AS draft_text,
                   COALESCE(t.comment, '') AS comment,
                   COALESCE(t.draft_comment, '') AS draft_comment,
                   COALESCE(t.status, 'untranslated') AS status,
                   COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                   COALESCE(t.version, 0) AS version,
                   t.updated_by,
                   t.updated_at,
                   t.draft_updated_by,
                   t.draft_updated_at,
                   c.status AS claim_status,
                   c.translator_name AS claimed_by
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            LEFT JOIN translation_claims c
              ON c.segment_id = s.id
             AND lower(c.target_language) = lower(?)
            WHERE {where_sql}
            ORDER BY s.ordinal
            LIMIT ? OFFSET ?
            """,
            (target_language, target_language, *params, per_page, offset),
        ).fetchall()

    return render_template(
        "language_texts.html",
        project=project,
        target_language=target_language,
        text_rows=text_rows,
        page=page,
        per_page=per_page,
        text_count=text_count,
        filters=filters,
        overview=overview,
    )


@app.post("/projects/<int:project_id>/languages/<path:target_language>/texts/batch")
def language_texts_batch(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    format_config = project_import_config(project, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    action = request.form.get("batch_action", "")
    segment_ids = []
    for value in request.form.getlist("segment_ids"):
        try:
            segment_ids.append(int(value))
        except ValueError:
            continue
    segment_ids = sorted(set(segment_ids))
    if not segment_ids:
        flash("Select at least one text.")
        return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))

    placeholders = ",".join("?" for _ in segment_ids)
    stamp = now_iso()
    with db() as conn:
        segments = conn.execute(
            f"""
            SELECT *
            FROM segments
            WHERE project_id = ?
              AND id IN ({placeholders})
            ORDER BY ordinal
            """,
            (project_id, *segment_ids),
        ).fetchall()
        if not segments:
            flash("No selected texts were found.")
            return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))

        if action == "export_selected":
            rows = conn.execute(
                f"""
                SELECT s.id AS segment_id,
                       s.identifier,
                       s.source_language,
                       s.source_text,
                       s.instructions,
                       s.metadata,
                       ? AS tgt_lang,
                       COALESCE(t.target_text, '') AS tgt_text,
                       COALESCE(t.target_instructions, '') AS tgt_instructions,
                       COALESCE(t.comment, '') AS comment,
                       COALESCE(t.status, 'untranslated') AS status,
                       COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                       t.updated_by,
                       t.updated_at
                FROM segments s
                LEFT JOIN translations t
                  ON t.segment_id = s.id
                 AND lower(t.target_language) = lower(?)
                WHERE s.project_id = ?
                  AND s.id IN ({placeholders})
                ORDER BY s.ordinal
                """,
                (target_language, target_language, project_id, *segment_ids),
            ).fetchall()
            source_flags = grouped_source_flags(
                conn, [row["segment_id"] for row in rows], [target_language]
            )
            export_rows = []
            for row in rows:
                row_dict = dict(row)
                segment_id = row_dict.pop("segment_id")
                metadata = metadata_dict(row_dict.pop("metadata", "{}"))
                metadata.update(row_dict)
                flags = source_flags.get(segment_id, [])
                metadata["source_flags"] = flags
                metadata["source_flag_count"] = len(flags)
                export_rows.append(metadata)
            lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in export_rows) + "\n"
            filename = secure_filename(f"project-{project_id}-{target_language}-selected.jsonl")
            return app.response_class(
                lines,
                mimetype="application/x-ndjson",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        if action == "release_claims":
            conn.execute(
                f"""
                DELETE FROM translation_claims
                WHERE lower(target_language) = lower(?)
                  AND status = 'claimed'
                  AND segment_id IN ({placeholders})
                """,
                (target_language, *segment_ids),
            )
            conn.commit()
            flash("Selected active assignments released.")
            return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))

        translations = conn.execute(
            f"""
            SELECT t.*, s.source_text
            FROM translations t
            JOIN segments s ON s.id = t.segment_id
            WHERE s.project_id = ?
              AND lower(t.target_language) = lower(?)
              AND t.segment_id IN ({placeholders})
            """,
            (project_id, target_language, *segment_ids),
        ).fetchall()

        if action in {"approve", "needs_revision"}:
            new_status = "approved" if action == "approve" else "needs_revision"
            changed = 0
            for translation in translations:
                if not translation["target_text"].strip():
                    continue
                conn.execute(
                    """
                    UPDATE translations
                       SET status = ?,
                           version = version + 1,
                           updated_by = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (new_status, user["username"], stamp, translation["id"]),
                )
                changed += 1
            conn.commit()
            flash(f"{changed} text(s) updated.")
            return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))

        if action == "rerun_qa":
            for translation in translations:
                conn.execute(
                    "UPDATE translations SET qa_warnings = ? WHERE id = ?",
                    (
                        qa_warnings_json(translation["source_text"], translation["target_text"]),
                        translation["id"],
                    ),
                )
            conn.commit()
            flash(f"QA rerun for {len(translations)} text(s).")
            return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))

    flash("Choose a valid batch action.")
    return redirect(url_for("language_texts", project_id=project_id, target_language=target_language))


@app.route(
    "/projects/<int:project_id>/languages/<path:target_language>/texts/<int:segment_id>",
    methods=["GET", "POST"],
)
def language_text_edit(project_id, target_language, segment_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    with db() as conn:
        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, project_id),
        ).fetchone()
        if not segment:
            abort(404)
        editor_config = translation_editor_config(project_import_config_from_conn(conn, project_id))

        if request.method == "POST":
            action = request.form.get("action", "save_translation")
            if action == "add_comment":
                role = request.form.get("comment_role", "manager")
                body = request.form.get("comment_body", "")
                if add_translation_comment(
                    conn, segment_id, target_language, role, body, user["username"]
                ):
                    conn.commit()
                    flash("Comment added.")
                else:
                    flash("Comment text is required.")
                return redirect(
                    url_for(
                        "language_text_edit",
                        project_id=project_id,
                        target_language=target_language,
                        segment_id=segment_id,
                    )
                )
            if action == "toggle_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                resolve_translation_comment(
                    conn, comment_id, segment_id, target_language, user["username"]
                )
                conn.commit()
                flash("Comment updated.")
                return redirect(
                    url_for(
                        "language_text_edit",
                        project_id=project_id,
                        target_language=target_language,
                        segment_id=segment_id,
                    )
                )
            if action == "delete_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                deleted, error = delete_translation_comment(
                    conn, comment_id, segment_id, target_language, user["username"]
                )
                if deleted:
                    conn.commit()
                    flash("Comment deleted.")
                else:
                    flash(error)
                return redirect(
                    url_for(
                        "language_text_edit",
                        project_id=project_id,
                        target_language=target_language,
                        segment_id=segment_id,
                    )
                )

            target_text = (
                join_form_lines(request.form.getlist("target_lines"))
                if editor_config["view"] == "sentence_list"
                else request.form.get("target_text", "")
            )
            target_instructions = request.form.get("target_instructions", "")
            comment = request.form.get("comment", "")
            requested_status = request.form.get("status", "")
            status = normalize_status(requested_status, target_text, "")
            qa_warnings = qa_warnings_json(segment["source_text"], target_text)
            stamp = now_iso()
            current = conn.execute(
                """
                SELECT * FROM translations
                WHERE segment_id = ? AND lower(target_language) = lower(?)
                """,
                (segment_id, target_language),
            ).fetchone()
            if current:
                version = current["version"] + 1
                conn.execute(
                    """
                    UPDATE translations
                       SET target_text = ?,
                           target_instructions = ?,
                           comment = ?,
                           status = ?,
                           qa_warnings = ?,
                           version = ?,
                           updated_by = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        version,
                        user["username"],
                        stamp,
                        current["id"],
                    ),
                )
            else:
                version = 1
                conn.execute(
                    """
                    INSERT INTO translations
                        (
                            segment_id,
                            target_language,
                            target_text,
                            target_instructions,
                            comment,
                            status,
                            qa_warnings,
                            version,
                            updated_by,
                            updated_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id,
                        target_language,
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        version,
                        user["username"],
                        stamp,
                    ),
                )
            conn.commit()
            flash("Text updated.")
            return redirect(
                url_for(
                    "language_text_edit",
                    project_id=project_id,
                    target_language=target_language,
                    segment_id=segment_id,
                )
            )

        translation = conn.execute(
            """
            SELECT *
            FROM translations
            WHERE segment_id = ? AND lower(target_language) = lower(?)
            """,
            (segment_id, target_language),
        ).fetchone()
        comment_rows = comments_with_permissions(
            conn, segment_id, target_language, user["username"]
        )

    return render_template(
        "language_text_edit.html",
        project=project,
        target_language=target_language,
        segment=segment,
        translation=translation,
        comment_rows=comment_rows,
        editor_config=editor_config,
    )


def language_data_filters_from_request(values):
    return {
        "q": values.get("q", "").strip(),
        "status": values.get("status", "").strip(),
        "missing": values.get("missing", "").strip(),
        "comments": values.get("comments", "").strip(),
        "warnings": values.get("warnings", "").strip(),
    }


def language_data_where(project_id, filters):
    where = ["s.project_id = ?"]
    params = [project_id]
    if filters["q"]:
        like = f"%{filters['q'].lower()}%"
        where.append(
            """
            (
                lower(s.identifier) LIKE ?
                OR lower(s.source_text) LIKE ?
                OR lower(COALESCE(s.instructions, '')) LIKE ?
                OR lower(COALESCE(s.metadata, '')) LIKE ?
                OR lower(COALESCE(t.target_text, '')) LIKE ?
                OR lower(COALESCE(t.draft_text, '')) LIKE ?
                OR lower(COALESCE(t.target_instructions, '')) LIKE ?
                OR lower(COALESCE(t.draft_instructions, '')) LIKE ?
                OR lower(COALESCE(t.comment, '')) LIKE ?
                OR lower(COALESCE(t.draft_comment, '')) LIKE ?
            )
            """
        )
        params.extend([like] * 10)
    if filters["status"] in TRANSLATION_STATUSES:
        where.append("COALESCE(t.status, 'untranslated') = ?")
        params.append(filters["status"])
    if filters["missing"]:
        where.append("trim(COALESCE(t.target_text, '') || COALESCE(t.draft_text, '')) = ''")
    if filters["comments"]:
        where.append(
            "trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''"
        )
    if filters["warnings"]:
        where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
    return " AND ".join(where), params


def language_data_select_sql(where_sql):
    return f"""
        SELECT s.id,
               s.identifier,
               s.ordinal,
               s.source_language,
               s.source_text,
               s.instructions,
               s.metadata,
               COALESCE(t.target_text, '') AS target_text,
               COALESCE(t.draft_text, '') AS draft_text,
               COALESCE(t.target_instructions, '') AS target_instructions,
               COALESCE(t.draft_instructions, '') AS draft_instructions,
               COALESCE(t.comment, '') AS comment,
               COALESCE(t.draft_comment, '') AS draft_comment,
               COALESCE(t.status, 'untranslated') AS status,
               COALESCE(t.qa_warnings, '[]') AS qa_warnings,
               COALESCE(t.version, 0) AS version,
               t.updated_by,
               t.updated_at,
               t.draft_updated_by,
               t.draft_updated_at
        FROM segments s
        LEFT JOIN translations t
          ON t.segment_id = s.id
         AND lower(t.target_language) = lower(?)
        WHERE {where_sql}
    """


def project_translation_data_filters_from_request(values):
    return {
        "q": values.get("q", "").strip(),
        "language": values.get("language", "").strip(),
        "status": values.get("status", "").strip(),
        "missing": values.get("missing", "").strip(),
        "comments": values.get("comments", "").strip(),
        "warnings": values.get("warnings", "").strip(),
    }


def project_translation_data_where(project_id, filters):
    where = ["s.project_id = ?"]
    params = [project_id]
    if filters["q"]:
        like = f"%{filters['q'].lower()}%"
        where.append(
            """
            (
                lower(s.identifier) LIKE ?
                OR lower(s.source_text) LIKE ?
                OR lower(COALESCE(s.instructions, '')) LIKE ?
                OR lower(COALESCE(s.metadata, '')) LIKE ?
                OR lower(pl.target_language) LIKE ?
                OR lower(COALESCE(t.target_text, '')) LIKE ?
                OR lower(COALESCE(t.draft_text, '')) LIKE ?
                OR lower(COALESCE(t.target_instructions, '')) LIKE ?
                OR lower(COALESCE(t.draft_instructions, '')) LIKE ?
                OR lower(COALESCE(t.comment, '')) LIKE ?
                OR lower(COALESCE(t.draft_comment, '')) LIKE ?
            )
            """
        )
        params.extend([like] * 11)
    if filters["language"]:
        where.append("lower(pl.target_language) = lower(?)")
        params.append(filters["language"])
    if filters["status"] in TRANSLATION_STATUSES:
        where.append("COALESCE(t.status, 'untranslated') = ?")
        params.append(filters["status"])
    if filters["missing"]:
        where.append("trim(COALESCE(t.target_text, '') || COALESCE(t.draft_text, '')) = ''")
    if filters["comments"]:
        where.append(
            "trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''"
        )
    if filters["warnings"]:
        where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
    return " AND ".join(where), params


def project_translation_data_select_sql(where_sql):
    return f"""
        SELECT s.id AS segment_id,
               s.identifier,
               s.ordinal,
               s.source_language,
               s.source_text,
               s.instructions,
               s.metadata,
               pl.target_language,
               COALESCE(t.target_text, '') AS target_text,
               COALESCE(t.draft_text, '') AS draft_text,
               COALESCE(t.target_instructions, '') AS target_instructions,
               COALESCE(t.draft_instructions, '') AS draft_instructions,
               COALESCE(t.comment, '') AS comment,
               COALESCE(t.draft_comment, '') AS draft_comment,
               COALESCE(t.status, 'untranslated') AS status,
               COALESCE(t.qa_warnings, '[]') AS qa_warnings,
               COALESCE(t.version, 0) AS version,
               t.updated_by,
               t.updated_at,
               t.draft_updated_by,
               t.draft_updated_at
        FROM segments s
        JOIN project_languages pl ON pl.project_id = s.project_id
        LEFT JOIN translations t
          ON t.segment_id = s.id
         AND lower(t.target_language) = lower(pl.target_language)
        WHERE {where_sql}
    """


def project_translation_data_redirect(project_id, form):
    try:
        page = int(form.get("page", "1"))
    except ValueError:
        page = 1
    return redirect(
        url_for(
            "project_translation_data",
            project_id=project_id,
            page=max(page, 1),
            q=form.get("q", ""),
            language=form.get("language", ""),
            status=form.get("status", ""),
            missing=form.get("missing", ""),
            comments=form.get("comments", ""),
            warnings=form.get("warnings", ""),
        )
    )


def parse_translation_slot(value):
    try:
        segment_id, target_language = str(value or "").split("::", 1)
        segment_id = int(segment_id)
    except (ValueError, TypeError):
        return None
    target_language = target_language.strip()
    if not target_language:
        return None
    return segment_id, target_language


@app.route("/projects/<int:project_id>/translation-data")
def project_translation_data(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 500
    offset = (page - 1) * per_page
    filters = project_translation_data_filters_from_request(request.args)
    where_sql, params = project_translation_data_where(project_id, filters)

    with db() as conn:
        languages = [
            row["target_language"]
            for row in project_language_rows(conn, project_id)
        ]
        text_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM segments s
            JOIN project_languages pl ON pl.project_id = s.project_id
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(pl.target_language)
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["count"]
        overview = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN trim(COALESCE(t.target_text, '') || COALESCE(t.draft_text, '')) = '' THEN 1 END) AS missing,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'untranslated' THEN 1 END) AS untranslated,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN 1 END) AS draft,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN 1 END) AS submitted,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN 1 END) AS needs_revision,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN 1 END) AS approved,
                   COUNT(CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN 1 END) AS warnings,
                   COUNT(CASE WHEN trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != '' THEN 1 END) AS comments
            FROM segments s
            JOIN project_languages pl ON pl.project_id = s.project_id
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(pl.target_language)
            WHERE s.project_id = ?
            """,
            (project_id,),
        ).fetchone()
        rows = conn.execute(
            f"""
            {project_translation_data_select_sql(where_sql)}
            ORDER BY s.ordinal, lower(pl.target_language)
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        ).fetchall()

    prepared_rows = []
    for row in rows:
        item = dict(row)
        item["display_target"] = item["draft_text"] or item["target_text"]
        item["display_instructions"] = (
            item["draft_instructions"] or item["target_instructions"]
        )
        item["display_comment"] = item["draft_comment"] or item["comment"]
        prepared_rows.append(item)

    return render_template(
        "project_translation_data.html",
        project=project,
        rows=prepared_rows,
        text_count=text_count,
        overview=overview,
        filters=filters,
        languages=languages,
        page=page,
        per_page=per_page,
    )


@app.post("/projects/<int:project_id>/translation-data/batch")
def project_translation_data_batch(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project_for_owner(project_id, user["id"])
    requested_status = request.form.get("batch_status", "").strip()
    if requested_status not in TRANSLATION_STATUSES:
        flash("Choose a valid status.")
        return project_translation_data_redirect(project_id, request.form)

    apply_all_filtered = request.form.get("apply_all_filtered") == "1"
    filters = project_translation_data_filters_from_request(request.form)
    slots = []

    with db() as conn:
        if apply_all_filtered:
            where_sql, params = project_translation_data_where(project_id, filters)
            slot_rows = conn.execute(
                f"""
                SELECT segment_id, target_language
                FROM (
                    {project_translation_data_select_sql(where_sql)}
                ) filtered_slots
                ORDER BY ordinal, lower(target_language)
                """,
                params,
            ).fetchall()
            slots = [(row["segment_id"], row["target_language"]) for row in slot_rows]
        else:
            for value in request.form.getlist("translation_slots"):
                parsed = parse_translation_slot(value)
                if parsed:
                    slots.append(parsed)
            slots = sorted(set(slots))

        if not slots:
            flash("Select at least one translation row.")
            return project_translation_data_redirect(project_id, request.form)

        changed = 0
        skipped = 0
        stamp = now_iso()
        allowed_languages = {
            row["target_language"].lower(): row["target_language"]
            for row in project_language_rows(conn, project_id)
        }

        for segment_id, target_language in slots:
            canonical_language = allowed_languages.get(target_language.lower())
            if not canonical_language:
                skipped += 1
                continue
            segment = conn.execute(
                "SELECT id FROM segments WHERE id = ? AND project_id = ?",
                (segment_id, project_id),
            ).fetchone()
            if not segment:
                skipped += 1
                continue
            current = conn.execute(
                """
                SELECT *
                FROM translations
                WHERE segment_id = ? AND lower(target_language) = lower(?)
                """,
                (segment_id, canonical_language),
            ).fetchone()

            if not current:
                if requested_status == "untranslated":
                    continue
                skipped += 1
                continue

            has_target = bool(str(current["target_text"] or "").strip())
            has_draft = bool(str(current["draft_text"] or "").strip())
            if requested_status in {"submitted", "needs_revision", "approved"} and not has_target:
                skipped += 1
                continue
            if requested_status == "draft" and not (has_draft or has_target):
                skipped += 1
                continue
            if requested_status == "untranslated" and (has_target or has_draft):
                skipped += 1
                continue

            conn.execute(
                """
                UPDATE translations
                   SET status = ?,
                       version = version + 1,
                       updated_by = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (requested_status, user["username"], stamp, current["id"]),
            )
            changed += 1
        conn.commit()

    message = f"{changed} translation row(s) updated."
    if skipped:
        message += f" {skipped} skipped because the status would not match the row data."
    flash(message)
    return project_translation_data_redirect(project_id, request.form)


@app.get("/projects/<int:project_id>/translation-data.docx")
def project_translation_data_docx(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    filters = project_translation_data_filters_from_request(request.args)
    where_sql, params = project_translation_data_where(project_id, filters)

    with db() as conn:
        language_order = [
            row["target_language"]
            for row in project_language_rows(conn, project_id)
        ]
        rows = conn.execute(
            f"""
            SELECT segment_id, target_language
            FROM (
                {project_translation_data_select_sql(where_sql)}
            ) filtered_slots
            ORDER BY ordinal, lower(target_language)
            """,
            params,
        ).fetchall()
        if not rows:
            flash("No translation rows match the filters.")
            return redirect(url_for("project_translation_data", project_id=project_id, **filters))

        selected_languages = []
        slot_languages = {}
        for row in rows:
            language = row["target_language"]
            slot_languages.setdefault(row["segment_id"], set()).add(language)
        for language in language_order:
            if any(language in languages for languages in slot_languages.values()):
                selected_languages.append(language)
        docx_segments = docx_segments_for_project(
            conn,
            project_id,
            selected_languages,
            slot_languages=slot_languages,
        )

    content = build_project_docx(project, docx_segments, selected_languages)
    filename = safe_download_name(
        f"project-{project_id}-filtered-translations.docx",
        f"project-{project_id}-filtered-translations.docx",
    )
    return app.response_class(
        content,
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/projects/<int:project_id>/languages/<path:target_language>/data")
def language_translation_data(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 500
    offset = (page - 1) * per_page
    filters = language_data_filters_from_request(request.args)
    where_sql, params = language_data_where(project_id, filters)

    with db() as conn:
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, project_id)
        )
        text_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE {where_sql}
            """,
            (target_language, *params),
        ).fetchone()["count"]
        overview = conn.execute(
            """
            SELECT COUNT(DISTINCT s.id) AS total,
                   COUNT(DISTINCT CASE WHEN trim(COALESCE(t.target_text, '') || COALESCE(t.draft_text, '')) = '' THEN s.id END) AS missing,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'untranslated' THEN s.id END) AS untranslated,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN s.id END) AS draft,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN s.id END) AS submitted,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN s.id END) AS needs_revision,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN s.id END) AS approved,
                   COUNT(DISTINCT CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN s.id END) AS warnings,
                   COUNT(DISTINCT CASE WHEN trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != '' THEN s.id END) AS comments
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            """,
            (target_language, project_id),
        ).fetchone()
        rows = conn.execute(
            f"""
            {language_data_select_sql(where_sql)}
            ORDER BY s.ordinal
            LIMIT ? OFFSET ?
            """,
            (target_language, *params, per_page, offset),
        ).fetchall()

    prepared_rows = []
    for row in rows:
        item = dict(row)
        item["display_target"] = item["draft_text"] or item["target_text"]
        item["display_instructions"] = (
            item["draft_instructions"] or item["target_instructions"]
        )
        item["display_comment"] = item["draft_comment"] or item["comment"]
        prepared_rows.append(item)

    return render_template(
        "translation_data.html",
        project=project,
        target_language=target_language,
        rows=prepared_rows,
        text_count=text_count,
        overview=overview,
        filters=filters,
        page=page,
        per_page=per_page,
        editor_config=editor_config,
    )


@app.get("/projects/<int:project_id>/languages/<path:target_language>/data.jsonl")
def language_translation_data_jsonl(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    filters = language_data_filters_from_request(request.args)
    where_sql, params = language_data_where(project_id, filters)
    with db() as conn:
        rows = conn.execute(
            f"""
            {language_data_select_sql(where_sql)}
            ORDER BY s.ordinal
            """,
            (target_language, *params),
        ).fetchall()

    records = []
    for row in rows:
        metadata = metadata_dict(row["metadata"])
        records.append(
            {
                "identifier": row["identifier"],
                "ordinal": row["ordinal"],
                "source_language": row["source_language"],
                "target_language": target_language,
                "source_text": row["source_text"],
                "instructions": row["instructions"],
                "translation": row["draft_text"] or row["target_text"],
                "target_text": row["target_text"],
                "draft_text": row["draft_text"],
                "translated_instructions": (
                    row["draft_instructions"] or row["target_instructions"]
                ),
                "target_instructions": row["target_instructions"],
                "draft_instructions": row["draft_instructions"],
                "saved_comment": row["draft_comment"] or row["comment"],
                "comment": row["comment"],
                "draft_comment": row["draft_comment"],
                "status": row["status"],
                "qa_warnings": json_list(row["qa_warnings"]),
                "version": row["version"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "draft_updated_by": row["draft_updated_by"],
                "draft_updated_at": row["draft_updated_at"],
                "metadata": metadata,
            }
        )
    body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if body:
        body += "\n"
    filename = safe_download_name(
        f"{project['name']}-{target_language}-translation-data.jsonl",
        "translation-data.jsonl",
    )
    return app.response_class(
        body,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route(
    "/projects/<int:project_id>/languages/<path:target_language>/data/<int:segment_id>",
    methods=["GET", "POST"],
)
def language_translation_data_edit(project_id, target_language, segment_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    with db() as conn:
        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, project_id),
        ).fetchone()
        if not segment:
            abort(404)
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, project_id)
        )

        if request.method == "POST":
            target_text = (
                join_form_lines(request.form.getlist("target_lines"))
                if editor_config["view"] == "sentence_list"
                else request.form.get("target_text", "")
            )
            target_instructions = request.form.get("target_instructions", "")
            comment = request.form.get("comment", "")
            requested_status = request.form.get("status", "")
            status = normalize_status(requested_status, target_text, "")
            qa_warnings = qa_warnings_json(segment["source_text"], target_text)
            stamp = now_iso()
            current = conn.execute(
                """
                SELECT *
                FROM translations
                WHERE segment_id = ? AND lower(target_language) = lower(?)
                """,
                (segment_id, target_language),
            ).fetchone()
            try:
                client_version = int(request.form.get("version", "0"))
            except ValueError:
                client_version = 0
            if current and client_version != current["version"]:
                flash("This translation changed since you opened it. Reload and try again.")
                return redirect(
                    url_for(
                        "language_translation_data_edit",
                        project_id=project_id,
                        target_language=target_language,
                        segment_id=segment_id,
                    )
                )
            if not current and client_version != 0:
                flash("This translation changed since you opened it. Reload and try again.")
                return redirect(
                    url_for(
                        "language_translation_data_edit",
                        project_id=project_id,
                        target_language=target_language,
                        segment_id=segment_id,
                    )
                )

            if current:
                conn.execute(
                    """
                    UPDATE translations
                       SET target_text = ?,
                           target_instructions = ?,
                           comment = ?,
                           draft_text = '',
                           draft_instructions = '',
                           draft_comment = '',
                           status = ?,
                           qa_warnings = ?,
                           version = version + 1,
                           updated_by = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        user["username"],
                        stamp,
                        current["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO translations
                        (
                            segment_id,
                            target_language,
                            target_text,
                            target_instructions,
                            comment,
                            status,
                            qa_warnings,
                            version,
                            updated_by,
                            updated_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        segment_id,
                        target_language,
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        user["username"],
                        stamp,
                    ),
                )
            conn.commit()
            flash("Translation data updated.")
            return redirect(
                url_for(
                    "language_translation_data_edit",
                    project_id=project_id,
                    target_language=target_language,
                    segment_id=segment_id,
                )
            )

        translation = conn.execute(
            """
            SELECT *
            FROM translations
            WHERE segment_id = ? AND lower(target_language) = lower(?)
            """,
            (segment_id, target_language),
        ).fetchone()

    return render_template(
        "translation_data_edit.html",
        project=project,
        target_language=target_language,
        segment=segment,
        translation=translation,
        editor_config=editor_config,
    )


@app.route("/projects/<int:project_id>/source-data")
def project_source_data(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (project_id,),
        ).fetchall()

    return render_template("source_data.html", project=project, rows=rows)


@app.get("/projects/<int:project_id>/source-data.jsonl")
def project_source_data_jsonl(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (project_id,),
        ).fetchall()

    records = []
    for row in rows:
        records.append(
            {
                "identifier": row["identifier"],
                "ordinal": row["ordinal"],
                "source_language": row["source_language"],
                "source_text": row["source_text"],
                "instructions": row["instructions"],
                "metadata": metadata_dict(row["metadata"]),
                "source_status": row["source_status"],
                "source_reviewed_by": row["source_reviewed_by"],
                "source_reviewed_at": row["source_reviewed_at"],
            }
        )
    body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if body:
        body += "\n"
    filename = safe_download_name(
        f"{project['name']}-source-data.jsonl",
        "source-data.jsonl",
    )
    return app.response_class(
        body,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/projects/<int:project_id>/source-data/<int:segment_id>", methods=["GET", "POST"])
def source_data_edit(project_id, segment_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    with db() as conn:
        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, project_id),
        ).fetchone()
        if not segment:
            abort(404)

        if request.method == "POST":
            identifier = request.form.get("identifier", "").strip()
            source_language = request.form.get("source_language", "").strip()
            source_text = (
                request.form.get("source_text", "")
                if project["source_editable"]
                else segment["source_text"]
            )
            instructions = request.form.get("instructions", "")
            metadata_raw = request.form.get("metadata", "{}")
            add_key = request.form.get("add_key", "").strip()
            add_value = request.form.get("add_value", "")

            if not identifier or not source_language or not source_text:
                flash("Identifier, source language, and source text are required.")
                return redirect(
                    url_for("source_data_edit", project_id=project_id, segment_id=segment_id)
                )

            try:
                metadata = json.loads(metadata_raw or "{}")
                if not isinstance(metadata, dict):
                    raise ValueError("Metadata must be a JSON object.")
            except (json.JSONDecodeError, ValueError) as exc:
                flash(f"Invalid metadata JSON: {exc}")
                return redirect(
                    url_for("source_data_edit", project_id=project_id, segment_id=segment_id)
                )

            if add_key:
                metadata[add_key] = add_value

            try:
                conn.execute(
                    """
                    UPDATE segments
                       SET identifier = ?,
                           source_language = ?,
                           source_text = ?,
                           instructions = ?,
                           metadata = ?
                     WHERE id = ?
                    """,
                    (
                        identifier,
                        source_language,
                        source_text,
                        instructions,
                        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                        segment_id,
                    ),
                )
                translations = conn.execute(
                    """
                    SELECT id, target_text
                    FROM translations
                    WHERE segment_id = ?
                    """,
                    (segment_id,),
                ).fetchall()
                for translation in translations:
                    conn.execute(
                        "UPDATE translations SET qa_warnings = ? WHERE id = ?",
                        (qa_warnings_json(source_text, translation["target_text"]), translation["id"]),
                    )
                conn.commit()
            except DB_INTEGRITY_ERRORS:
                conn.rollback()
                flash("That identifier is already used in this project.")
                return redirect(
                    url_for("source_data_edit", project_id=project_id, segment_id=segment_id)
                )

            flash("Source data updated.")
            return redirect(url_for("source_data_edit", project_id=project_id, segment_id=segment_id))

        metadata_pretty = json.dumps(
            metadata_dict(segment["metadata"]),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    return render_template(
        "source_data_edit.html",
        project=project,
        segment=segment,
        metadata_pretty=metadata_pretty,
    )


def review_link_for_token(conn, token):
    return conn.execute(
        """
        SELECT rl.*, p.name AS project_name, p.source_language
        FROM review_links rl
        JOIN projects p ON p.id = rl.project_id
        WHERE rl.token = ?
        """,
        (token,),
    ).fetchone()


def reviewer_redirect(token, page, segment_id=None):
    target = url_for("review_project", token=token, page=max(int(page or 1), 1))
    if segment_id:
        target = f"{target}#segment-{segment_id}"
    return redirect(target)


def reviewer_texts_redirect(token, form):
    try:
        page = int(form.get("page", "1"))
    except ValueError:
        page = 1
    return redirect(
        url_for(
            "review_project_texts",
            token=token,
            page=max(page, 1),
            q=form.get("q", ""),
            language=form.get("language", ""),
            status=form.get("filter_status", form.get("status", "")),
            missing=form.get("missing", ""),
            comments=form.get("comments", ""),
            warnings=form.get("warnings", ""),
        )
    )


@app.route("/r/<token>/texts", methods=["GET", "POST"])
def review_project_texts(token):
    try:
        page = int(request.values.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 100
    offset = (page - 1) * per_page
    filters = {
        "q": request.values.get("q", "").strip(),
        "language": request.values.get("language", "").strip(),
        "status": request.values.get("status", "").strip(),
        "missing": request.values.get("missing", "").strip(),
        "comments": request.values.get("comments", "").strip(),
        "warnings": request.values.get("warnings", "").strip(),
    }

    with db() as conn:
        link = review_link_for_token(conn, token)
        if not link:
            abort(404)
        reviewer = reviewer_name(link)
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, link["project_id"])
        )
        languages = [
            row["target_language"]
            for row in project_language_rows(conn, link["project_id"])
        ]
        language_lookup = {language.lower(): language for language in languages}

        if request.method == "POST":
            action = request.form.get("action", "save_translation")
            try:
                segment_id = int(request.form.get("segment_id", "0"))
            except ValueError:
                abort(400)
            segment = conn.execute(
                "SELECT * FROM segments WHERE id = ? AND project_id = ?",
                (segment_id, link["project_id"]),
            ).fetchone()
            if not segment:
                abort(404)
            target_language_key = request.form.get("target_language", "").strip().lower()
            target_language = language_lookup.get(target_language_key)
            if not target_language:
                abort(404)

            target_text = (
                join_form_lines(request.form.getlist("target_lines"))
                if editor_config["view"] == "sentence_list"
                else request.form.get("target_text", "")
            )
            target_instructions = request.form.get("target_instructions", "")
            comment = request.form.get("comment", "")
            requested_status = request.form.get("translation_status", "")
            if action == "mark_reviewed":
                requested_status = "approved"
            status = normalize_status(requested_status, target_text, "")
            if action == "mark_reviewed" and status != "approved":
                flash("A translation is required before it can be marked reviewed.")
                return reviewer_texts_redirect(token, request.form)

            current = conn.execute(
                """
                SELECT *
                FROM translations
                WHERE segment_id = ?
                  AND lower(target_language) = lower(?)
                """,
                (segment_id, target_language),
            ).fetchone()
            try:
                client_version = int(request.form.get("version", "0"))
            except ValueError:
                client_version = 0
            if current and client_version != current["version"]:
                flash("This translation changed since you opened it. Reload and try again.")
                return reviewer_texts_redirect(token, request.form)
            if not current and client_version != 0:
                flash("This translation changed since you opened it. Reload and try again.")
                return reviewer_texts_redirect(token, request.form)

            stamp = now_iso()
            qa_warnings = qa_warnings_json(segment["source_text"], target_text)
            if current:
                conn.execute(
                    """
                    UPDATE translations
                       SET target_text = ?,
                           target_instructions = ?,
                           comment = ?,
                           status = ?,
                           qa_warnings = ?,
                           version = version + 1,
                           updated_by = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        reviewer,
                        stamp,
                        current["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO translations
                        (
                            segment_id,
                            target_language,
                            target_text,
                            target_instructions,
                            comment,
                            status,
                            qa_warnings,
                            version,
                            updated_by,
                            updated_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        segment_id,
                        target_language,
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        reviewer,
                        stamp,
                    ),
                )
            conn.commit()
            flash("Translation reviewed." if status == "approved" else "Translation updated.")
            return reviewer_texts_redirect(token, request.form)

        where = ["s.project_id = ?"]
        params = [link["project_id"]]
        if filters["q"]:
            like = f"%{filters['q'].lower()}%"
            where.append(
                """
                (
                    lower(s.identifier) LIKE ?
                    OR lower(s.source_text) LIKE ?
                    OR lower(COALESCE(s.metadata, '')) LIKE ?
                    OR lower(pl.target_language) LIKE ?
                    OR lower(COALESCE(t.target_text, '')) LIKE ?
                    OR lower(COALESCE(t.draft_text, '')) LIKE ?
                    OR lower(COALESCE(t.comment, '')) LIKE ?
                    OR lower(COALESCE(t.draft_comment, '')) LIKE ?
                )
                """
            )
            params.extend([like] * 8)
        if filters["language"]:
            where.append("lower(pl.target_language) = lower(?)")
            params.append(filters["language"])
        if filters["status"] in TRANSLATION_STATUSES:
            where.append("COALESCE(t.status, 'untranslated') = ?")
            params.append(filters["status"])
        if filters["missing"]:
            where.append("trim(COALESCE(t.target_text, '')) = ''")
        if filters["comments"]:
            where.append(
                """
                (
                    COALESCE(cc.comment_count, 0) > 0
                    OR trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''
                )
                """
            )
        if filters["warnings"]:
            where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
        where_sql = " AND ".join(where)
        slot_join = """
            FROM segments s
            JOIN project_languages pl ON pl.project_id = s.project_id
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(pl.target_language)
            LEFT JOIN (
                SELECT segment_id,
                       lower(target_language) AS language_key,
                       COUNT(*) AS comment_count
                FROM translation_comments
                GROUP BY segment_id, lower(target_language)
            ) cc
              ON cc.segment_id = s.id
             AND cc.language_key = lower(pl.target_language)
        """

        text_count = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            {slot_join}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["count"]
        overview = conn.execute(
            f"""
            SELECT COUNT(*) AS total_slots,
                   COUNT(CASE WHEN trim(COALESCE(t.target_text, '')) != '' THEN 1 END) AS translated,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'untranslated' THEN 1 END) AS untranslated,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN 1 END) AS draft,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN 1 END) AS submitted,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN 1 END) AS needs_revision,
                   COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN 1 END) AS reviewed,
                   COUNT(CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN 1 END) AS warnings,
                   COUNT(
                       CASE
                           WHEN COALESCE(cc.comment_count, 0) > 0
                             OR trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''
                           THEN 1
                       END
                   ) AS comments
            {slot_join}
            WHERE s.project_id = ?
            """,
            (link["project_id"],),
        ).fetchone()
        text_rows = conn.execute(
            f"""
            SELECT s.id AS segment_id,
                   s.identifier,
                   s.ordinal,
                   s.source_language,
                   s.source_text,
                   s.instructions,
                   s.metadata,
                   pl.target_language,
                   COALESCE(t.target_text, '') AS target_text,
                   COALESCE(t.draft_text, '') AS draft_text,
                   COALESCE(t.target_instructions, '') AS target_instructions,
                   COALESCE(t.draft_instructions, '') AS draft_instructions,
                   COALESCE(t.comment, '') AS comment,
                   COALESCE(t.draft_comment, '') AS draft_comment,
                   COALESCE(t.status, 'untranslated') AS status,
                   COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                   COALESCE(t.version, 0) AS version,
                   t.updated_by,
                   t.updated_at,
                   t.draft_updated_by,
                   t.draft_updated_at,
                   COALESCE(cc.comment_count, 0) AS thread_comment_count
            {slot_join}
            WHERE {where_sql}
            ORDER BY s.ordinal, lower(pl.target_language)
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        ).fetchall()

    rows = []
    for row in text_rows:
        item = dict(row)
        item["source_lines"] = split_lines(item["source_text"])
        active_target = item["draft_text"] or item["target_text"] or ""
        item["target_lines"] = split_lines(active_target)
        rows.append(item)

    return render_template(
        "review_texts.html",
        link=link,
        reviewer=reviewer,
        languages=languages,
        editor_config=editor_config,
        overview=overview,
        text_rows=rows,
        text_count=text_count,
        page=page,
        per_page=per_page,
        filters=filters,
    )


@app.get("/r/<token>/texts/download")
def review_project_texts_download(token):
    filters = {
        "q": request.args.get("q", "").strip(),
        "language": request.args.get("language", "").strip(),
        "status": request.args.get("status", "").strip(),
        "missing": request.args.get("missing", "").strip(),
        "comments": request.args.get("comments", "").strip(),
        "warnings": request.args.get("warnings", "").strip(),
    }

    with db() as conn:
        link = review_link_for_token(conn, token)
        if not link:
            abort(404)

        where = ["s.project_id = ?"]
        params = [link["project_id"]]
        if filters["q"]:
            like = f"%{filters['q'].lower()}%"
            where.append(
                """
                (
                    lower(s.identifier) LIKE ?
                    OR lower(s.source_text) LIKE ?
                    OR lower(COALESCE(s.metadata, '')) LIKE ?
                    OR lower(pl.target_language) LIKE ?
                    OR lower(COALESCE(t.target_text, '')) LIKE ?
                    OR lower(COALESCE(t.draft_text, '')) LIKE ?
                    OR lower(COALESCE(t.comment, '')) LIKE ?
                    OR lower(COALESCE(t.draft_comment, '')) LIKE ?
                )
                """
            )
            params.extend([like] * 8)
        if filters["language"]:
            where.append("lower(pl.target_language) = lower(?)")
            params.append(filters["language"])
        if filters["status"] in TRANSLATION_STATUSES:
            where.append("COALESCE(t.status, 'untranslated') = ?")
            params.append(filters["status"])
        if filters["missing"]:
            where.append("trim(COALESCE(t.target_text, '')) = ''")
        if filters["comments"]:
            where.append(
                """
                (
                    COALESCE(cc.comment_count, 0) > 0
                    OR trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''
                )
                """
            )
        if filters["warnings"]:
            where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
        where_sql = " AND ".join(where)
        slot_join = """
            FROM segments s
            JOIN project_languages pl ON pl.project_id = s.project_id
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(pl.target_language)
            LEFT JOIN (
                SELECT segment_id,
                       lower(target_language) AS language_key,
                       COUNT(*) AS comment_count
                FROM translation_comments
                GROUP BY segment_id, lower(target_language)
            ) cc
              ON cc.segment_id = s.id
             AND cc.language_key = lower(pl.target_language)
        """
        rows = conn.execute(
            f"""
            SELECT s.identifier,
                   s.ordinal,
                   s.source_language,
                   s.source_text,
                   s.instructions,
                   s.metadata,
                   pl.target_language,
                   COALESCE(t.target_text, '') AS target_text,
                   COALESCE(t.draft_text, '') AS draft_text,
                   COALESCE(t.target_instructions, '') AS target_instructions,
                   COALESCE(t.draft_instructions, '') AS draft_instructions,
                   COALESCE(t.comment, '') AS comment,
                   COALESCE(t.draft_comment, '') AS draft_comment,
                   COALESCE(t.status, 'untranslated') AS status,
                   COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                   COALESCE(t.version, 0) AS version,
                   t.updated_by,
                   t.updated_at,
                   t.draft_updated_by,
                   t.draft_updated_at,
                   COALESCE(cc.comment_count, 0) AS thread_comment_count
            {slot_join}
            WHERE {where_sql}
            ORDER BY s.ordinal, lower(pl.target_language)
            """,
            params,
        ).fetchall()

    records = []
    for row in rows:
        records.append(
            {
                "identifier": row["identifier"],
                "ordinal": row["ordinal"],
                "source_language": row["source_language"],
                "target_language": row["target_language"],
                "source_text": row["source_text"],
                "instructions": row["instructions"],
                "translation": row["draft_text"] or row["target_text"],
                "target_text": row["target_text"],
                "draft_text": row["draft_text"],
                "translated_instructions": (
                    row["draft_instructions"] or row["target_instructions"]
                ),
                "target_instructions": row["target_instructions"],
                "draft_instructions": row["draft_instructions"],
                "saved_comment": row["draft_comment"] or row["comment"],
                "comment": row["comment"],
                "draft_comment": row["draft_comment"],
                "status": row["status"],
                "qa_warnings": json_list(row["qa_warnings"]),
                "thread_comment_count": row["thread_comment_count"],
                "version": row["version"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
                "draft_updated_by": row["draft_updated_by"],
                "draft_updated_at": row["draft_updated_at"],
                "metadata": metadata_dict(row["metadata"]),
            }
        )

    body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if body:
        body += "\n"
    filename = safe_download_name(
        f"{link['project_name']}-fast-review.jsonl",
        "fast-review.jsonl",
    )
    return app.response_class(
        body,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def creator_link_for_token(conn, token):
    return conn.execute(
        """
        SELECT cl.*, p.name AS project_name, p.source_language
        FROM creator_links cl
        JOIN projects p ON p.id = cl.project_id
        WHERE cl.token = ?
        """,
        (token,),
    ).fetchone()


def creator_name(link):
    return (link["creator_name"] or "creator").strip() or "creator"


def creator_redirect(token, page, segment_id=None):
    target = url_for("creator_source_review", token=token, page=max(int(page or 1), 1))
    if segment_id:
        target = f"{target}#segment-{segment_id}"
    return redirect(target)


@app.get("/c/<token>/download-source")
def creator_download_source(token):
    with db() as conn:
        link = creator_link_for_token(conn, token)
        if not link:
            abort(404)
        rows = conn.execute(
            """
            SELECT ordinal,
                   identifier,
                   source_text,
                   instructions
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (link["project_id"],),
        ).fetchall()

    source_folder = safe_archive_name(link["source_language"], "source")
    instruction_folder = f"{source_folder}-instructions"
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            filename = segment_txt_filename(row)
            archive.writestr(f"{source_folder}/{filename}", row["source_text"] or "")
            if str(row["instructions"] or "").strip():
                archive.writestr(
                    f"{instruction_folder}/{filename}",
                    row["instructions"] or "",
                )
    output.seek(0)

    filename = safe_download_name(
        f"{link['project_name']}-source-review.zip",
        "source-review.zip",
    )
    return app.response_class(
        output.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/c/<token>", methods=["GET", "POST"])
def creator_source_review(token):
    try:
        page = int(request.values.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 40
    offset = (page - 1) * per_page

    with db() as conn:
        link = creator_link_for_token(conn, token)
        if not link:
            abort(404)
        creator = creator_name(link)
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, link["project_id"])
        )

        if request.method == "POST":
            try:
                segment_id = int(request.form.get("segment_id", "0"))
            except ValueError:
                abort(400)
            segment = conn.execute(
                "SELECT * FROM segments WHERE id = ? AND project_id = ?",
                (segment_id, link["project_id"]),
            ).fetchone()
            if not segment:
                abort(404)

            action = request.form.get("action", "save_source")
            source_text = request.form.get("source_text", "")
            if editor_config["view"] == "sentence_list":
                source_text = join_form_lines(request.form.getlist("source_lines"))
            instructions = request.form.get("instructions", "")
            source_status = "reviewed" if action in {"mark_reviewed", "save_and_review"} else "draft"
            reviewed_by = creator if source_status == "reviewed" else None
            reviewed_at = now_iso() if source_status == "reviewed" else None
            if not source_text.strip():
                flash("Source text is required.")
                return creator_redirect(token, page, segment_id)
            conn.execute(
                """
                UPDATE segments
                   SET source_text = ?,
                       instructions = ?,
                       source_status = ?,
                       source_reviewed_by = ?,
                       source_reviewed_at = ?
                 WHERE id = ?
                """,
                (
                    source_text,
                    instructions,
                    source_status,
                    reviewed_by,
                    reviewed_at,
                    segment_id,
                ),
            )
            translations = conn.execute(
                "SELECT id, target_text FROM translations WHERE segment_id = ?",
                (segment_id,),
            ).fetchall()
            for translation in translations:
                conn.execute(
                    "UPDATE translations SET qa_warnings = ? WHERE id = ?",
                    (
                        qa_warnings_json(source_text, translation["target_text"]),
                        translation["id"],
                    ),
                )
            conn.commit()
            flash("Source reviewed." if source_status == "reviewed" else "Source saved.")
            return creator_redirect(token, page, segment_id)

        segment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE project_id = ?",
            (link["project_id"],),
        ).fetchone()["count"]
        overview = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN source_status = 'reviewed' THEN 1 END) AS reviewed,
                   COUNT(CASE WHEN source_status != 'reviewed' THEN 1 END) AS draft
            FROM segments
            WHERE project_id = ?
            """,
            (link["project_id"],),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT *
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            LIMIT ? OFFSET ?
            """,
            (link["project_id"], per_page, offset),
        ).fetchall()
        source_rows = []
        for row in rows:
            item = dict(row)
            item["source_lines"] = split_lines(item["source_text"])
            source_rows.append(item)

    return render_template(
        "creator_source_review.html",
        link=link,
        creator=creator,
        editor_config=editor_config,
        overview=overview,
        source_rows=source_rows,
        page=page,
        per_page=per_page,
        segment_count=segment_count,
    )


@app.route("/r/<token>", methods=["GET", "POST"])
def review_project(token):
    try:
        page = int(request.values.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 20
    offset = (page - 1) * per_page

    with db() as conn:
        link = review_link_for_token(conn, token)
        if not link:
            abort(404)
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, link["project_id"])
        )
        reviewer = reviewer_name(link)
        languages = [
            row["target_language"]
            for row in project_language_rows(conn, link["project_id"])
        ]
        language_lookup = {language.lower(): language for language in languages}

        if request.method == "POST":
            action = request.form.get("action", "save_translation")
            try:
                segment_id = int(request.form.get("segment_id", "0"))
            except ValueError:
                abort(400)
            segment = conn.execute(
                "SELECT * FROM segments WHERE id = ? AND project_id = ?",
                (segment_id, link["project_id"]),
            ).fetchone()
            if not segment:
                abort(404)

            if action == "mark_segment_reviewed":
                if not languages:
                    flash("No target languages exist for this project yet.")
                    return reviewer_redirect(token, page, segment_id)
                language_conditions = " OR ".join(
                    "lower(target_language) = lower(?)" for _ in languages
                )
                conn.execute(
                    f"""
                    UPDATE translations
                       SET status = 'approved',
                           version = version + 1,
                           updated_by = ?,
                           updated_at = ?
                     WHERE segment_id = ?
                       AND trim(target_text) != ''
                       AND ({language_conditions})
                    """,
                    (reviewer, now_iso(), segment_id, *languages),
                )
                conn.commit()
                flash("Segment translations marked reviewed.")
                return reviewer_redirect(token, page, segment_id)

            target_language_key = request.form.get("target_language", "").strip().lower()
            target_language = language_lookup.get(target_language_key)
            if not target_language:
                abort(404)

            if action == "add_comment":
                body = request.form.get("comment_body", "")
                if add_translation_comment(
                    conn, segment_id, target_language, "reviewer", body, reviewer
                ):
                    conn.commit()
                    flash("Comment added.")
                else:
                    flash("Comment text is required.")
                return reviewer_redirect(token, page, segment_id)

            if action == "toggle_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                resolve_translation_comment(
                    conn, comment_id, segment_id, target_language, reviewer
                )
                conn.commit()
                flash("Comment updated.")
                return reviewer_redirect(token, page, segment_id)

            if action == "delete_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                deleted, error = delete_translation_comment(
                    conn, comment_id, segment_id, target_language, reviewer
                )
                if deleted:
                    conn.commit()
                    flash("Comment deleted.")
                else:
                    flash(error)
                return reviewer_redirect(token, page, segment_id)

            target_text = (
                join_form_lines(request.form.getlist("target_lines"))
                if editor_config["view"] == "sentence_list"
                else request.form.get("target_text", "")
            )
            target_instructions = request.form.get("target_instructions", "")
            comment = request.form.get("comment", "")
            requested_status = request.form.get("status", "")
            if action == "mark_reviewed":
                requested_status = "approved"
            status = normalize_status(requested_status, target_text, "")
            if action == "mark_reviewed" and status != "approved":
                flash("A translation is required before it can be marked reviewed.")
                return reviewer_redirect(token, page, segment_id)

            current = conn.execute(
                """
                SELECT *
                FROM translations
                WHERE segment_id = ?
                  AND lower(target_language) = lower(?)
                """,
                (segment_id, target_language),
            ).fetchone()
            try:
                client_version = int(request.form.get("version", "0"))
            except ValueError:
                client_version = 0
            if current and client_version != current["version"]:
                flash("This translation changed since you opened it. Reload and try again.")
                return reviewer_redirect(token, page, segment_id)
            if not current and client_version != 0:
                flash("This translation changed since you opened it. Reload and try again.")
                return reviewer_redirect(token, page, segment_id)

            stamp = now_iso()
            qa_warnings = qa_warnings_json(segment["source_text"], target_text)
            if current:
                conn.execute(
                    """
                    UPDATE translations
                       SET target_text = ?,
                           target_instructions = ?,
                           comment = ?,
                           status = ?,
                           qa_warnings = ?,
                           version = version + 1,
                           updated_by = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        reviewer,
                        stamp,
                        current["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO translations
                        (
                            segment_id,
                            target_language,
                            target_text,
                            target_instructions,
                            comment,
                            status,
                            qa_warnings,
                            version,
                            updated_by,
                            updated_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        segment_id,
                        target_language,
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        reviewer,
                        stamp,
                    ),
                )
            conn.commit()
            flash("Translation reviewed." if status == "approved" else "Translation updated.")
            return reviewer_redirect(token, page, segment_id)

        segment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM segments WHERE project_id = ?",
            (link["project_id"],),
        ).fetchone()["count"]
        if languages:
            overview = conn.execute(
                """
                WITH comment_counts AS (
                    SELECT segment_id,
                           lower(target_language) AS language_key,
                           COUNT(*) AS comment_count
                    FROM translation_comments
                    GROUP BY segment_id, lower(target_language)
                )
                SELECT COUNT(*) AS total_slots,
                       COUNT(CASE WHEN trim(COALESCE(t.target_text, '')) != '' THEN 1 END) AS translated,
                       COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'untranslated' THEN 1 END) AS untranslated,
                       COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'draft' THEN 1 END) AS draft,
                       COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'submitted' THEN 1 END) AS submitted,
                       COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'needs_revision' THEN 1 END) AS needs_revision,
                       COUNT(CASE WHEN COALESCE(t.status, 'untranslated') = 'approved' THEN 1 END) AS reviewed,
                       COUNT(CASE WHEN COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]') THEN 1 END) AS warnings,
                       COUNT(
                           CASE
                               WHEN COALESCE(cc.comment_count, 0) > 0
                                 OR trim(COALESCE(t.comment, '') || COALESCE(t.draft_comment, '')) != ''
                               THEN 1
                           END
                       ) AS comments
                FROM segments s
                JOIN project_languages pl ON pl.project_id = s.project_id
                LEFT JOIN translations t
                  ON t.segment_id = s.id
                 AND lower(t.target_language) = lower(pl.target_language)
                LEFT JOIN comment_counts cc
                  ON cc.segment_id = s.id
                 AND cc.language_key = lower(pl.target_language)
                WHERE s.project_id = ?
                """,
                (link["project_id"],),
            ).fetchone()
        else:
            overview = {
                "total_slots": 0,
                "translated": 0,
                "untranslated": 0,
                "draft": 0,
                "submitted": 0,
                "needs_revision": 0,
                "reviewed": 0,
                "warnings": 0,
                "comments": 0,
            }

        segments = conn.execute(
            """
            SELECT *
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            LIMIT ? OFFSET ?
            """,
            (link["project_id"], per_page, offset),
        ).fetchall()
        segment_ids = [row["id"] for row in segments]
        translations_by_key = {}
        comments_by_key = {}
        if segment_ids:
            placeholders = ",".join("?" for _ in segment_ids)
            translation_rows = conn.execute(
                f"""
                SELECT t.*
                FROM translations t
                JOIN segments s ON s.id = t.segment_id
                WHERE s.project_id = ?
                  AND t.segment_id IN ({placeholders})
                """,
                (link["project_id"], *segment_ids),
            ).fetchall()
            for row in translation_rows:
                if row["target_language"].lower() in language_lookup:
                    translations_by_key[
                        (row["segment_id"], row["target_language"].lower())
                    ] = dict(row)

            comment_rows = conn.execute(
                f"""
                SELECT *
                FROM translation_comments
                WHERE segment_id IN ({placeholders})
                ORDER BY resolved ASC, created_at DESC, id DESC
                """,
                tuple(segment_ids),
            ).fetchall()
            for row in comment_rows:
                if row["target_language"].lower() not in language_lookup:
                    continue
                item = dict(row)
                item["can_delete"] = can_delete_comment(conn, item, reviewer)
                comments_by_key.setdefault(
                    (row["segment_id"], row["target_language"].lower()),
                    [],
                ).append(item)

        review_rows = []
        for segment in segments:
            segment_item = dict(segment)
            translations = []
            for language in languages:
                language_key = language.lower()
                translation = translations_by_key.get((segment["id"], language_key))
                if translation is None:
                    translation = {
                        "id": None,
                        "segment_id": segment["id"],
                        "target_language": language,
                        "target_text": "",
                        "draft_text": "",
                        "target_instructions": "",
                        "draft_instructions": "",
                        "comment": "",
                        "draft_comment": "",
                        "status": "untranslated",
                        "qa_warnings": "[]",
                        "version": 0,
                        "updated_by": None,
                        "updated_at": None,
                        "draft_updated_by": None,
                        "draft_updated_at": None,
                    }
                translation["language"] = language
                translation["comments"] = comments_by_key.get(
                    (segment["id"], language_key),
                    [],
                )
                translations.append(translation)
            review_rows.append({"segment": segment_item, "translations": translations})

    return render_template(
        "review_project.html",
        link=link,
        reviewer=reviewer,
        editor_config=editor_config,
        languages=languages,
        overview=overview,
        review_rows=review_rows,
        page=page,
        per_page=per_page,
        segment_count=segment_count,
    )


@app.route("/t/<token>")
def translate(token):
    with db() as conn:
        link = conn.execute(
            """
            SELECT sl.*, p.name AS project_name, p.source_language
            FROM share_links sl
            JOIN projects p ON p.id = sl.project_id
            WHERE sl.token = ?
              AND sl.revoked_at IS NULL
            """,
            (token,),
        ).fetchone()
    if not link:
        abort(404)
    return render_template("translate.html", link=link)


def link_for_token(conn, token):
    return conn.execute(
        """
        SELECT sl.*, p.name AS project_name, p.source_language
        FROM share_links sl
        JOIN projects p ON p.id = sl.project_id
        WHERE sl.token = ?
          AND sl.revoked_at IS NULL
        """,
        (token,),
    ).fetchone()


@app.route("/t/<token>/translations")
def translator_translations(token):
    filters = {
        "q": request.args.get("q", "").strip(),
        "status": request.args.get("status", "").strip(),
        "metadata": request.args.get("metadata", "").strip(),
        "translator": request.args.get("translator", "").strip(),
        "comments": request.args.get("comments", "").strip(),
        "warnings": request.args.get("warnings", "").strip(),
    }
    with db() as conn:
        link = link_for_token(conn, token)
        if not link:
            abort(404)
        where = [
            "s.project_id = ?",
            "lower(t.target_language) = lower(?)",
            "trim(t.target_text) != ''",
        ]
        params = [link["project_id"], link["target_language"]]
        if filters["q"]:
            like = f"%{filters['q'].lower()}%"
            where.append(
                """
                (
                    lower(s.identifier) LIKE ?
                    OR lower(s.source_text) LIKE ?
                    OR lower(t.target_text) LIKE ?
                    OR lower(COALESCE(t.comment, '')) LIKE ?
                )
                """
            )
            params.extend([like] * 4)
        if filters["status"] in TRANSLATION_STATUSES:
            where.append("t.status = ?")
            params.append(filters["status"])
        if filters["metadata"]:
            where.append("lower(COALESCE(s.metadata, '')) LIKE ?")
            params.append(f"%{filters['metadata'].lower()}%")
        if filters["translator"]:
            where.append("lower(COALESCE(t.updated_by, '')) LIKE ?")
            params.append(f"%{filters['translator'].lower()}%")
        if filters["comments"]:
            where.append("trim(COALESCE(t.comment, '')) != ''")
        if filters["warnings"]:
            where.append("COALESCE(t.qa_warnings, '[]') NOT IN ('', '[]')")
        rows = conn.execute(
            f"""
            SELECT s.id,
                   s.identifier,
                   s.source_text,
                   s.metadata,
                   t.target_text,
                   t.comment,
                   t.status,
                   t.qa_warnings,
                   t.version,
                   t.updated_by,
                   t.updated_at
            FROM translations t
            JOIN segments s ON s.id = t.segment_id
            WHERE {" AND ".join(where)}
            ORDER BY t.updated_at DESC, s.ordinal
            """,
            params,
        ).fetchall()

    return render_template("translator_translations.html", link=link, rows=rows, filters=filters)


@app.route("/t/<token>/translations/<int:segment_id>", methods=["GET", "POST"])
def translator_translation_edit(token, segment_id):
    back_to_work = request.args.get("back") == "work" or request.form.get("back") == "work"

    def edit_url():
        if back_to_work:
            return url_for(
                "translator_translation_edit",
                token=token,
                segment_id=segment_id,
                back="work",
            )
        return url_for("translator_translation_edit", token=token, segment_id=segment_id)

    with db() as conn:
        link = link_for_token(conn, token)
        if not link:
            abort(404)
        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, link["project_id"]),
        ).fetchone()
        if not segment:
            abort(404)
        translation = conn.execute(
            """
            SELECT *
            FROM translations
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
              AND trim(target_text) != ''
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        if not translation:
            abort(404)
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, link["project_id"])
        )

        if request.method == "POST":
            action = request.form.get("action", "save_translation")
            translator = link_translator_name(link)
            if action == "add_comment":
                role = request.form.get("comment_role", "translator")
                if role not in {"translator", "reviewer"}:
                    role = "translator"
                body = request.form.get("comment_body", "")
                if add_translation_comment(
                    conn, segment_id, link["target_language"], role, body, translator
                ):
                    conn.commit()
                    flash("Comment added.")
                else:
                    flash("Comment text is required.")
                return redirect(edit_url())
            if action == "toggle_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                resolve_translation_comment(
                    conn, comment_id, segment_id, link["target_language"], translator
                )
                conn.commit()
                flash("Comment updated.")
                return redirect(edit_url())
            if action == "delete_comment":
                comment_id = parse_optional_int(request.form.get("comment_id"), "Comment")
                if comment_id is None:
                    abort(400)
                deleted, error = delete_translation_comment(
                    conn, comment_id, segment_id, link["target_language"], translator
                )
                if deleted:
                    conn.commit()
                    flash("Comment deleted.")
                else:
                    flash(error)
                return redirect(edit_url())

            client_version = int(request.form.get("version", "0"))
            if client_version != translation["version"]:
                flash("This translation changed since you opened it. Reload and try again.")
                return redirect(edit_url())

            target_text = (
                join_form_lines(request.form.getlist("target_lines"))
                if editor_config["view"] == "sentence_list"
                else request.form.get("target_text", "")
            )
            target_instructions = request.form.get("target_instructions", "")
            comment = request.form.get("comment", "")
            stamp = now_iso()
            version = translation["version"] + 1
            status = normalize_status("submitted", target_text, "")
            qa_warnings = qa_warnings_json(segment["source_text"], target_text)
            conn.execute(
                """
                UPDATE translations
                   SET target_text = ?,
                       target_instructions = ?,
                       comment = ?,
                       status = ?,
                       qa_warnings = ?,
                       version = ?,
                       updated_by = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    target_text,
                    target_instructions,
                    comment,
                    status,
                    qa_warnings,
                    version,
                    translator,
                    stamp,
                    translation["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO translation_events
                    (
                        share_link_id,
                        segment_id,
                        target_language,
                        translator_name,
                        target_text,
                        target_instructions,
                        comment,
                        version,
                        created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link["id"],
                    segment_id,
                    link["target_language"],
                    translator,
                    target_text,
                    target_instructions,
                    comment,
                    version,
                    stamp,
                ),
            )
            conn.commit()
            flash("Translation updated.")
            return redirect(edit_url())
        comment_rows = comments_with_permissions(
            conn, segment_id, link["target_language"], link_translator_name(link)
        )

    return render_template(
        "translator_translation_edit.html",
        link=link,
        segment=segment,
        translation=translation,
        back_to_work=back_to_work,
        comment_rows=comment_rows,
        editor_config=editor_config,
    )


@app.get("/api/t/<token>/segments")
def api_segments(token):
    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)

        rows = conn.execute(
            """
            SELECT s.id,
                   s.identifier,
                   s.ordinal,
                   s.source_language,
                   s.source_text,
                   s.instructions,
                   COALESCE(t.target_text, '') AS target_text,
                   COALESCE(t.comment, '') AS comment,
                   COALESCE(t.version, 0) AS version,
                   t.updated_by,
                   t.updated_at
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
              AND (? IS NULL OR s.ordinal >= ?)
              AND (? IS NULL OR s.ordinal <= ?)
            ORDER BY s.ordinal
            """,
            (
                link["target_language"],
                link["project_id"],
                link["start_ordinal"],
                link["start_ordinal"],
                link["end_ordinal"],
                link["end_ordinal"],
            ),
        ).fetchall()

    return jsonify(
        {
            "target_language": link["target_language"],
            "label": link["label"],
            "start_ordinal": link["start_ordinal"],
            "end_ordinal": link["end_ordinal"],
            "segments": [dict(row) for row in rows],
        }
    )


@app.get("/api/t/<token>/status")
def api_link_status(token):
    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)

        remaining, used = link_remaining_credits(conn, link)
        counts = conn.execute(
            """
            SELECT COUNT(DISTINCT s.id) AS total_segments,
                   COUNT(
                       DISTINCT CASE
                           WHEN trim(COALESCE(t.target_text, '')) != '' THEN s.id
                       END
                   ) AS completed_segments
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            """,
            (link["target_language"], link["project_id"]),
        ).fetchone()
        pending_claim = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM translation_claims
            WHERE share_link_id = ?
              AND status = 'claimed'
            """,
            (link["id"],),
        ).fetchone()["count"]
        recent_rows = recent_submissions(conn, link)

    return jsonify(
        {
            "target_language": link["target_language"],
            "label": link["label"],
            "translator_name": link_translator_name(link),
            **assignment_payload(link, used, remaining),
            "pending_claims": pending_claim,
            "total_segments": counts["total_segments"],
            "completed_segments": counts["completed_segments"],
            "recent_submissions": recent_rows,
        }
    )


def serialize_claimed_segment(conn, link, segment_id):
    row = conn.execute(
        """
        SELECT s.id,
               s.identifier,
               s.ordinal,
               s.source_language,
               s.source_text,
               s.instructions,
               COALESCE(t.target_text, '') AS target_text,
               COALESCE(t.draft_text, '') AS draft_text,
               COALESCE(t.target_instructions, '') AS target_instructions,
               COALESCE(t.draft_instructions, '') AS draft_instructions,
               COALESCE(t.comment, '') AS comment,
               COALESCE(t.draft_comment, '') AS draft_comment,
               COALESCE(t.status, 'untranslated') AS status,
               COALESCE(t.qa_warnings, '[]') AS qa_warnings,
               COALESCE(t.version, 0) AS version,
               t.updated_by,
               t.updated_at,
               t.draft_updated_by,
               t.draft_updated_at
        FROM segments s
        LEFT JOIN translations t
          ON t.segment_id = s.id
         AND lower(t.target_language) = lower(?)
        WHERE s.id = ?
          AND s.project_id = ?
        """,
        (link["target_language"], segment_id, link["project_id"]),
    ).fetchone()
    segment = row_to_dict(row)
    if segment:
        editor_config = translation_editor_config(
            project_import_config_from_conn(conn, link["project_id"])
        )
        segment["editor"] = editor_config
        segment["source_lines"] = split_lines(segment["source_text"])
        segment["source_variants"] = source_variants_for_segment(conn, segment)
        active_target = segment.get("draft_text") or segment.get("target_text") or ""
        segment["target_lines"] = split_lines(active_target)
        segment["source_flags"] = grouped_source_flags(
            conn, [segment_id], [link["target_language"]]
        ).get(segment_id, [])
        segment["comments"] = serialized_comments(
            conn, segment_id, link["target_language"], link_translator_name(link)
        )
    return segment


def recent_submissions(conn, link, limit=3):
    rows = conn.execute(
        """
        SELECT s.id,
               s.identifier,
               t.target_text,
               t.updated_by,
               t.updated_at
        FROM translations t
        JOIN segments s ON s.id = t.segment_id
        WHERE s.project_id = ?
          AND lower(t.target_language) = lower(?)
          AND trim(t.target_text) != ''
        ORDER BY t.updated_at DESC, s.ordinal DESC
        LIMIT ?
        """,
        (link["project_id"], link["target_language"], limit),
    ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/t/<token>/next")
def api_next_segment(token):
    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)

        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            """
            SELECT *
            FROM translation_claims
            WHERE share_link_id = ?
              AND status = 'claimed'
            ORDER BY claimed_at
            LIMIT 1
            """,
            (link["id"],),
        ).fetchone()
        if pending:
            remaining, used = link_remaining_credits(conn, link)
            return jsonify(
                {
                    "status": "ok",
                    "segment": serialize_claimed_segment(conn, link, pending["segment_id"]),
                    **assignment_payload(link, used, remaining),
                }
            )

        remaining, used = link_remaining_credits(conn, link)
        if remaining is not None and remaining <= 0:
            return jsonify(
                {
                    "status": "limit_reached",
                    "message": "Assignment limit reached.",
                    **assignment_payload(link, used, remaining),
                }
            )

        candidate_params = (
            link["target_language"],
            link["target_language"],
            link["project_id"],
        )
        if conn.is_postgres:
            candidates = conn.execute(
                """
                SELECT s.id
                FROM segments s
                LEFT JOIN translations t
                  ON t.segment_id = s.id
                 AND lower(t.target_language) = lower(?)
                LEFT JOIN translation_claims c
                  ON c.segment_id = s.id
                 AND lower(c.target_language) = lower(?)
                WHERE s.project_id = ?
                  AND trim(COALESCE(t.target_text, '')) = ''
                  AND c.id IS NULL
                ORDER BY random()
                LIMIT 1
                FOR UPDATE OF s SKIP LOCKED
                """,
                candidate_params,
            ).fetchall()
        else:
            candidates = conn.execute(
                """
                SELECT s.id
                FROM segments s
                LEFT JOIN translations t
                  ON t.segment_id = s.id
                 AND lower(t.target_language) = lower(?)
                LEFT JOIN translation_claims c
                  ON c.segment_id = s.id
                 AND lower(c.target_language) = lower(?)
                WHERE s.project_id = ?
                  AND trim(COALESCE(t.target_text, '')) = ''
                  AND c.id IS NULL
                """,
                candidate_params,
            ).fetchall()

        if not candidates:
            return jsonify(
                {
                    "status": "done",
                    "message": "No untranslated segments remain.",
                    **assignment_payload(link, used, remaining),
                }
            )

        segment_id = candidates[0]["id"] if conn.is_postgres else random.choice(candidates)["id"]
        conn.execute(
            """
            INSERT INTO translation_claims
                (share_link_id, segment_id, target_language, translator_name, status, claimed_at)
            VALUES (?, ?, ?, ?, 'claimed', ?)
            """,
            (
                link["id"],
                segment_id,
                link["target_language"],
                link_translator_name(link),
                now_iso(),
            ),
        )
        conn.commit()
        return jsonify(
            {
                "status": "ok",
                "segment": serialize_claimed_segment(conn, link, segment_id),
                **assignment_payload(link, used, remaining),
            }
        )


@app.post("/api/t/<token>/segments/<int:segment_id>/skip")
def api_skip_segment(token, segment_id):
    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)

        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, link["project_id"]),
        ).fetchone()
        if not segment:
            abort(404)

        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            """
            SELECT *
            FROM translation_claims
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        if not claim or claim["share_link_id"] != link["id"]:
            abort(403)
        if claim["status"] != "claimed":
            return (
                jsonify(
                    {
                        "status": "already_submitted",
                        "message": "This text has already been submitted.",
                    }
                ),
                409,
            )

        translator = link_translator_name(link)
        current = conn.execute(
            """
            SELECT *
            FROM translations
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        if current and not current["target_text"].strip() and current["draft_updated_by"] == translator:
            conn.execute(
                """
                UPDATE translations
                   SET draft_text = '',
                       draft_instructions = '',
                       draft_comment = '',
                       draft_updated_by = NULL,
                       draft_updated_at = NULL,
                       status = 'untranslated'
                 WHERE id = ?
                """,
                (current["id"],),
            )
        conn.execute("DELETE FROM translation_claims WHERE id = ?", (claim["id"],))
        conn.commit()
        remaining, used = link_remaining_credits(conn, link)

    return jsonify({"status": "skipped", **assignment_payload(link, used, remaining)})


def require_link_segment_claim(conn, token, segment_id):
    link = conn.execute(
        "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
        (token,),
    ).fetchone()
    if not link:
        abort(404)

    segment = conn.execute(
        "SELECT * FROM segments WHERE id = ? AND project_id = ?",
        (segment_id, link["project_id"]),
    ).fetchone()
    if not segment:
        abort(404)

    claim = conn.execute(
        """
        SELECT *
        FROM translation_claims
        WHERE segment_id = ?
          AND share_link_id = ?
          AND lower(target_language) = lower(?)
        """,
        (segment_id, link["id"], link["target_language"]),
    ).fetchone()
    if not claim:
        abort(403)
    return link, segment, claim


@app.get("/api/t/<token>/segments/<int:segment_id>/comments")
def api_segment_comments(token, segment_id):
    with db() as conn:
        link, _segment, _claim = require_link_segment_claim(conn, token, segment_id)
        comments = serialized_comments(
            conn, segment_id, link["target_language"], link_translator_name(link)
        )
    return jsonify({"status": "ok", "comments": comments})


@app.post("/api/t/<token>/segments/<int:segment_id>/comments")
def api_add_segment_comment(token, segment_id):
    payload = request.get_json(silent=True) or {}
    body = str(payload.get("body", "")).strip()
    if not body:
        return jsonify({"status": "error", "message": "Comment text is required."}), 400

    with db() as conn:
        link, _segment, _claim = require_link_segment_claim(conn, token, segment_id)
        add_translation_comment(
            conn,
            segment_id,
            link["target_language"],
            "translator",
            body,
            link_translator_name(link),
        )
        conn.commit()
        comments = serialized_comments(
            conn, segment_id, link["target_language"], link_translator_name(link)
        )

    return jsonify({"status": "comment_added", "comments": comments})


@app.delete("/api/t/<token>/segments/<int:segment_id>/comments/<int:comment_id>")
def api_delete_segment_comment(token, segment_id, comment_id):
    with db() as conn:
        link, _segment, _claim = require_link_segment_claim(conn, token, segment_id)
        translator = link_translator_name(link)
        deleted, error = delete_translation_comment(
            conn, comment_id, segment_id, link["target_language"], translator
        )
        if not deleted:
            return jsonify({"status": "error", "message": error}), 409
        conn.commit()
        comments = serialized_comments(
            conn, segment_id, link["target_language"], translator
        )

    return jsonify({"status": "comment_deleted", "comments": comments})


@app.post("/api/t/<token>/segments/<int:segment_id>/source-flag")
def api_flag_source(token, segment_id):
    payload = request.get_json(silent=True) or {}
    note = str(payload.get("note", "")).strip()
    if not note:
        return jsonify({"status": "error", "message": "Flag note is required."}), 400

    with db() as conn:
        link, _segment, _claim = require_link_segment_claim(conn, token, segment_id)
        translator = link_translator_name(link)
        stamp = now_iso()
        conn.execute(
            """
            INSERT INTO source_flags
                (
                    segment_id,
                    share_link_id,
                    target_language,
                    translator_name,
                    note,
                    created_at
                )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (segment_id, link["id"], link["target_language"], translator, note, stamp),
        )
        add_translation_comment(
            conn,
            segment_id,
            link["target_language"],
            "translator",
            f"Source flagged: {note}",
            translator,
        )
        conn.commit()
        flags = grouped_source_flags(conn, [segment_id], [link["target_language"]]).get(
            segment_id, []
        )
        comments = serialized_comments(
            conn, segment_id, link["target_language"], translator
        )

    return jsonify({"status": "flagged", "source_flags": flags, "comments": comments})


@app.post("/api/t/<token>/segments/<int:segment_id>/source-unflag")
def api_unflag_source(token, segment_id):
    with db() as conn:
        link, _segment, _claim = require_link_segment_claim(conn, token, segment_id)
        translator = link_translator_name(link)
        conn.execute(
            """
            DELETE FROM source_flags
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        )
        add_translation_comment(
            conn,
            segment_id,
            link["target_language"],
            "translator",
            "Source unflagged.",
            translator,
        )
        conn.commit()
        flags = grouped_source_flags(conn, [segment_id], [link["target_language"]]).get(
            segment_id, []
        )
        comments = serialized_comments(
            conn, segment_id, link["target_language"], translator
        )

    return jsonify({"status": "unflagged", "source_flags": flags, "comments": comments})


@app.post("/api/t/<token>/segments/<int:segment_id>/draft")
def api_save_draft(token, segment_id):
    payload = request.get_json(silent=True) or {}
    draft_text = str(payload.get("target_text", ""))
    draft_instructions = str(payload.get("target_instructions", ""))
    draft_comment = str(payload.get("comment", ""))

    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)
        translator = link_translator_name(link)

        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, link["project_id"]),
        ).fetchone()
        if not segment:
            abort(404)

        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            """
            SELECT *
            FROM translation_claims
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        if not claim or claim["share_link_id"] != link["id"] or claim["status"] != "claimed":
            abort(403)

        current = conn.execute(
            """
            SELECT *
            FROM translations
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        stamp = now_iso()
        if current:
            status = normalize_status(current["status"], current["target_text"], draft_text)
            conn.execute(
                """
                UPDATE translations
                   SET draft_text = ?,
                       draft_instructions = ?,
                       draft_comment = ?,
                       draft_updated_by = ?,
                       draft_updated_at = ?,
                       status = ?
                 WHERE id = ?
                """,
                (
                    draft_text,
                    draft_instructions,
                    draft_comment,
                    translator,
                    stamp,
                    status,
                    current["id"],
                ),
            )
        else:
            status = normalize_status("", "", draft_text)
            conn.execute(
                """
                INSERT INTO translations
                    (
                        segment_id,
                        target_language,
                        draft_text,
                        draft_instructions,
                        draft_comment,
                        status,
                        qa_warnings,
                        version,
                        draft_updated_by,
                        updated_at,
                        draft_updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, '[]', 0, ?, ?, ?)
                """,
                (
                    segment_id,
                    link["target_language"],
                    draft_text,
                    draft_instructions,
                    draft_comment,
                    status,
                    translator,
                    stamp,
                    stamp,
                ),
            )
        conn.commit()

    return jsonify({"status": "draft_saved", "translation_status": status, "draft_updated_at": stamp})


@app.post("/api/t/<token>/segments/<int:segment_id>")
def api_save_segment(token, segment_id):
    payload = request.get_json(silent=True) or {}
    target_text = payload.get("target_text", "")
    target_instructions = payload.get("target_instructions", "")
    comment = payload.get("comment", "")
    client_version = int(payload.get("version", 0))

    with db() as conn:
        link = conn.execute(
            "SELECT * FROM share_links WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        if not link:
            abort(404)
        translator = link_translator_name(link)

        segment = conn.execute(
            "SELECT * FROM segments WHERE id = ? AND project_id = ?",
            (segment_id, link["project_id"]),
        ).fetchone()
        if not segment:
            abort(404)

        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            """
            SELECT *
            FROM translation_claims
            WHERE segment_id = ?
              AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()
        if not claim or claim["share_link_id"] != link["id"]:
            abort(403)

        current = conn.execute(
            """
            SELECT * FROM translations
            WHERE segment_id = ? AND lower(target_language) = lower(?)
            """,
            (segment_id, link["target_language"]),
        ).fetchone()

        stamp = now_iso()
        submitted_status = normalize_status("submitted", target_text, "")
        qa_warnings = qa_warnings_json(segment["source_text"], target_text)
        if current:
            if client_version != current["version"]:
                return (
                    jsonify(
                        {
                            "status": "conflict",
                            "message": "This segment changed since you loaded it.",
                            "server": {
                                "target_text": current["target_text"],
                                "target_instructions": current["target_instructions"],
                                "comment": current["comment"],
                                "version": current["version"],
                                "updated_by": current["updated_by"],
                                "updated_at": current["updated_at"],
                            },
                        }
                    ),
                    409,
                )

            version = current["version"] + 1
            conn.execute(
                """
                UPDATE translations
                   SET target_text = ?,
                       draft_text = '',
                       target_instructions = ?,
                       draft_instructions = '',
                       comment = ?,
                       draft_comment = '',
                       status = ?,
                       qa_warnings = ?,
                       version = ?,
                       updated_by = ?,
                       draft_updated_by = NULL,
                       updated_at = ?,
                       draft_updated_at = NULL
                 WHERE id = ?
                """,
                (
                    str(target_text),
                    str(target_instructions),
                    str(comment),
                    submitted_status,
                    qa_warnings,
                    version,
                    translator,
                    stamp,
                    current["id"],
                ),
            )
        else:
            if client_version != 0:
                return (
                    jsonify(
                        {
                            "status": "conflict",
                            "message": "This segment now has a translation.",
                            "server": {
                                "target_text": "",
                                "comment": "",
                                "version": 0,
                                "updated_by": None,
                                "updated_at": None,
                            },
                        }
                    ),
                    409,
                )
            version = 1
            conn.execute(
                """
                INSERT INTO translations
                    (
                        segment_id,
                        target_language,
                        target_text,
                        target_instructions,
                        comment,
                        status,
                        qa_warnings,
                        version,
                        updated_by,
                        updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    link["target_language"],
                    str(target_text),
                    str(target_instructions),
                    str(comment),
                    submitted_status,
                    qa_warnings,
                    version,
                    translator,
                    stamp,
                ),
            )

        claim_status = "completed" if str(target_text).strip() else "claimed"
        conn.execute(
            """
            UPDATE translation_claims
               SET status = ?,
                   completed_at = CASE WHEN ? = 'completed' THEN ? ELSE NULL END
             WHERE id = ?
            """,
            (claim_status, claim_status, stamp, claim["id"]),
        )
        conn.execute(
            """
            INSERT INTO translation_events
                (
                    share_link_id,
                    segment_id,
                    target_language,
                    translator_name,
                    target_text,
                    target_instructions,
                    comment,
                    version,
                    created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link["id"],
                segment_id,
                link["target_language"],
                translator,
                str(target_text),
                str(target_instructions),
                str(comment),
                version,
                stamp,
            ),
        )
        conn.commit()

        remaining, used = link_remaining_credits(conn, link)

    return jsonify(
        {
            "status": "saved",
            "version": version,
            "comment": str(comment),
            "target_instructions": str(target_instructions),
            "updated_by": translator,
            "updated_at": stamp,
            "translation_status": submitted_status,
            "qa_warnings": json.loads(qa_warnings),
            **assignment_payload(link, used, remaining),
        }
    )


@app.get("/projects/<int:project_id>/export/<target_language>")
def export_project(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project_for_owner(project_id, user["id"])
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.id AS segment_id,
                   s.identifier,
                   s.source_language,
                   s.source_text,
                   s.instructions,
                   s.metadata,
                   ? AS tgt_lang,
                   COALESCE(t.target_text, '') AS tgt_text,
                   COALESCE(t.target_instructions, '') AS tgt_instructions,
                   COALESCE(t.comment, '') AS comment,
                   COALESCE(t.status, 'untranslated') AS status,
                   COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                   t.updated_by,
                   t.updated_at
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            ORDER BY s.ordinal
            """,
            (target_language, target_language, project_id),
        ).fetchall()
        source_flags = grouped_source_flags(
            conn, [row["segment_id"] for row in rows], [target_language]
        )

    export_rows = []
    for row in rows:
        row_dict = dict(row)
        segment_id = row_dict.pop("segment_id")
        metadata = metadata_dict(row_dict.pop("metadata", "{}"))
        metadata.update(row_dict)
        flags = source_flags.get(segment_id, [])
        metadata["source_flags"] = flags
        metadata["source_flag_count"] = len(flags)
        export_rows.append(metadata)

    lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in export_rows) + "\n"
    filename = secure_filename(f"project-{project_id}-{target_language}.jsonl")
    return app.response_class(
        lines,
        mimetype="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        },
    )


@app.get("/projects/<int:project_id>/export-txt/<path:target_language>")
def export_project_txt_zip(project_id, target_language):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    target_language = target_language.strip()
    if not target_language:
        abort(404)

    source_folder = safe_archive_name(project["source_language"], "source")
    target_folder = safe_archive_name(target_language, "target")
    if target_folder == source_folder:
        target_folder = f"{target_folder}-target"

    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.ordinal,
                   s.identifier,
                   s.source_text,
                   COALESCE(t.target_text, '') AS target_text
            FROM segments s
            LEFT JOIN translations t
              ON t.segment_id = s.id
             AND lower(t.target_language) = lower(?)
            WHERE s.project_id = ?
            ORDER BY s.ordinal
            """,
            (target_language, project_id),
        ).fetchall()

    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            filename = segment_txt_filename(row)
            archive.writestr(f"{source_folder}/{filename}", row["source_text"] or "")
            archive.writestr(f"{target_folder}/{filename}", row["target_text"] or "")
    output.seek(0)

    filename = safe_download_name(
        f"project-{project_id}-{target_language}-txt.zip",
        f"project-{project_id}-txt.zip",
    )
    return app.response_class(
        output.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/projects/<int:project_id>/export-jsonl")
def export_project_jsonl_multi(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project_for_owner(project_id, user["id"])
    selected_languages = [
        value.strip() for value in request.form.getlist("languages") if value.strip()
    ]
    if not selected_languages:
        flash("Select at least one language to export.")
        return redirect(url_for("project_detail", project_id=project_id))

    with db() as conn:
        allowed_languages = [
            row["target_language"]
            for row in conn.execute(
                "SELECT target_language FROM project_languages WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
        allowed_lookup = {language.lower(): language for language in allowed_languages}
        selected_languages = [
            allowed_lookup[language.lower()]
            for language in selected_languages
            if language.lower() in allowed_lookup
        ]
        if not selected_languages:
            flash("No valid languages were selected.")
            return redirect(url_for("project_detail", project_id=project_id))

        segments = conn.execute(
            """
            SELECT id,
                   identifier,
                   source_language,
                   source_text,
                   instructions,
                   metadata
            FROM segments
            WHERE project_id = ?
            ORDER BY ordinal
            """,
            (project_id,),
        ).fetchall()
        segment_ids = [row["id"] for row in segments]
        source_flags = grouped_source_flags(conn, segment_ids, selected_languages)

        translations_by_segment = {segment_id: {} for segment_id in segment_ids}
        for language in selected_languages:
            rows = conn.execute(
                """
                SELECT s.id AS segment_id,
                       COALESCE(t.target_text, '') AS target_text,
                       COALESCE(t.target_instructions, '') AS target_instructions,
                       COALESCE(t.comment, '') AS comment,
                       COALESCE(t.status, 'untranslated') AS status,
                       COALESCE(t.qa_warnings, '[]') AS qa_warnings,
                       t.updated_by,
                       t.updated_at
                FROM segments s
                LEFT JOIN translations t
                  ON t.segment_id = s.id
                 AND lower(t.target_language) = lower(?)
                WHERE s.project_id = ?
                ORDER BY s.ordinal
                """,
                (language, project_id),
            ).fetchall()
            for row in rows:
                translations_by_segment[row["segment_id"]][language] = dict(row)

    export_rows = []
    for segment in segments:
        segment_id = segment["id"]
        if format_config and len(selected_languages) == 1:
            export_rows.append(
                profile_export_item(
                    segment,
                    selected_languages,
                    translations_by_segment,
                    format_config,
                )
            )
            continue
        item = metadata_dict(segment["metadata"])
        flags = source_flags.get(segment_id, [])
        item.update(
            {
                "identifier": segment["identifier"],
                "source_language": segment["source_language"],
                "source_text": segment["source_text"],
                "instructions": segment["instructions"],
                "languages": selected_languages,
                "source_flags": flags,
                "source_flag_count": len(flags),
                "translations": [],
            }
        )
        for language in selected_languages:
            translation = translations_by_segment.get(segment_id, {}).get(language, {})
            language_flags = [
                flag for flag in flags if flag["language"].lower() == language.lower()
            ]
            item["translations"].append(
                {
                    "language": language,
                   "target_text": translation.get("target_text", ""),
                    "target_instructions": translation.get("target_instructions", ""),
                    "comment": translation.get("comment", ""),
                    "status": translation.get("status", "untranslated"),
                    "qa_warnings": json_list(translation.get("qa_warnings", "[]")),
                    "updated_by": translation.get("updated_by"),
                    "updated_at": translation.get("updated_at"),
                    "source_flags": language_flags,
                    "source_flag_count": len(language_flags),
                }
            )
        export_rows.append(item)

    if format_config and len(selected_languages) == 1:
        return format_export_response(export_rows, format_config, project_id)

    lines = "\n".join(json.dumps(row, ensure_ascii=False) for row in export_rows) + "\n"
    filename = secure_filename(f"project-{project_id}-selected-languages.jsonl")
    return app.response_class(
        lines,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/projects/<int:project_id>/export-docx")
def export_project_docx(project_id):
    user = require_login()
    if not is_db_row(user):
        return user

    project = project_for_owner(project_id, user["id"])
    selected_languages = [value.strip() for value in request.form.getlist("languages") if value.strip()]
    if not selected_languages:
        flash("Select at least one language to export.")
        return redirect(url_for("project_detail", project_id=project_id))

    with db() as conn:
        allowed_languages = {
            row["target_language"]
            for row in conn.execute(
                "SELECT target_language FROM project_languages WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        }
        selected_languages = [
            language for language in selected_languages if language in allowed_languages
        ]
        if not selected_languages:
            flash("No valid languages were selected.")
            return redirect(url_for("project_detail", project_id=project_id))

        docx_segments = docx_segments_for_project(conn, project_id, selected_languages)

    content = build_project_docx(project, docx_segments, selected_languages)
    filename = secure_filename(f"project-{project_id}-selected-languages.docx")
    return app.response_class(
        content,
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
