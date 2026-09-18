"""
SNUKED HOSTER - Single File Backend for Railway
All backend functionality merged into one file.
No .env required. Edit config below directly.
Deploy with: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional

from fastapi import FastAPI, Cookie, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, Query, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# =============================================================================
# HARD-CODED CONFIGURATION - EDIT THESE VALUES
# =============================================================================

BOT_TOKEN = "8521376955:AAHlP7ZuZXSkIJbDS_bVUUx8Lg6zpstBH3s"
OWNER_USER_ID = 7743406267
ADMIN_USER_IDS = []  # e.g. [123456789, 987654321]
SECRET_KEY = "slayer5811"

# Public URL where Mini App / frontend is hosted
MINI_APP_URL = "https://legendary-cendol-79e5df.netlify.app"
CORS_ORIGINS = ["https://legendary-cendol-79e5df.netlify.app"]

# Storage paths - Railway compatible (persistent if volume mounted)
DATABASE_PATH = "./data/bothost.db"
PROJECTS_DIR = "./projects"

# Advanced settings
HOST = "0.0.0.0"
PORT = 8000
COOKIE_SECURE = False
MAX_PROJECTS_PER_USER = 10
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PROCESS_CPU_SECONDS = 3600
PROCESS_MEMORY_MB = 256
LOGIN_TOKEN_TTL_SECONDS = 900
SESSION_TTL_SECONDS = 12 * 3600
SUPPORTED_RUNTIMES = ["python", "node", "python+node"]

# Allow Railway env vars to override hard-coded values (optional, for compatibility)
def _env(name: str, default):
    return os.getenv(name, default)

def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except:
        return default

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def _env_list(name: str, default: list) -> list:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return [x.strip() for x in v.split(",") if x.strip()]

# Apply env overrides if present (Railway)
BOT_TOKEN = _env("BOT_TOKEN", BOT_TOKEN)
OWNER_USER_ID = _env_int("OWNER_USER_ID", OWNER_USER_ID)
_admin_ids_env = _env_list("ADMIN_USER_IDS", [str(i) for i in ADMIN_USER_IDS])
ADMIN_USER_IDS = [int(x) for x in _admin_ids_env if str(x).isdigit()]
SECRET_KEY = _env("SECRET_KEY", SECRET_KEY)
DATABASE_PATH = _env("DATABASE_PATH", DATABASE_PATH)
PROJECTS_DIR = _env("PROJECTS_DIR", PROJECTS_DIR)
MINI_APP_URL = _env("MINI_APP_URL", _env("DASHBOARD_BASE_URL", MINI_APP_URL))
CORS_ORIGINS = _env_list("CORS_ORIGINS", CORS_ORIGINS) or ["*"]
HOST = _env("HOST", HOST)
PORT = _env_int("PORT", PORT)

# Ensure directories
PROJECTS_DIR_PATH = Path(PROJECTS_DIR).resolve()
PROJECTS_DIR_PATH.mkdir(parents=True, exist_ok=True)
Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# =============================================================================
# SECURITY HELPERS
# =============================================================================

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64d(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def sign_session(payload: dict[str, Any], secret_key: str, ttl_seconds: int) -> str:
    body = dict(payload)
    body["exp"] = time.time() + ttl_seconds
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = hmac.new(secret_key.encode(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"

def verify_session(token: str, secret_key: str) -> dict[str, Any] | None:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _b64d(raw_b64)
        sig = _b64d(sig_b64)
    except Exception:
        return None
    expected = hmac.new(secret_key.encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    body = json.loads(raw)
    if body.get("exp", 0) < time.time():
        return None
    return body

def generate_csrf_token() -> str:
    return uuid.uuid4().hex

# =============================================================================
# NAMING HELPERS
# =============================================================================

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SLUG = re.compile(r"[^a-z0-9-]+")

def sanitize_filename(name: str, max_len: int = 200) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.replace("\\", "/").split("/")[-1]
    name = name.strip().lstrip(".")
    name = _SAFE_FILENAME.sub("_", name)
    if not name:
        name = "file"
    return name[:max_len]

def sanitize_slug(name: str, max_len: int = 64) -> str:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = slug.strip().replace(" ", "-")
    slug = _SAFE_SLUG.sub("-", slug).strip("-")
    if not slug:
        slug = "project"
    return slug[:max_len]

ALLOWED_UPLOAD_EXTENSIONS = {".py", ".js", ".txt", ".json", ".zip", ".env", ".cfg", ".ini", ".toml"}

def is_allowed_upload(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_UPLOAD_EXTENSIONS)

# =============================================================================
# TELEGRAM AUTH HELPERS
# =============================================================================

def validate_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> Optional[Dict[str, Any]]:
    if not init_data or not bot_token:
        return None
    try:
        parsed_qs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict = dict(parsed_qs)
    except Exception:
        return None
    received_hash = data_dict.pop("hash", None)
    if not received_hash:
        return None
    auth_date_str = data_dict.get("auth_date")
    if auth_date_str:
        try:
            auth_date = int(auth_date_str)
            now = int(time.time())
            if now - auth_date > max_age_seconds:
                return None
        except ValueError:
            return None
    data_check_arr = []
    for key in sorted(data_dict.keys()):
        data_check_arr.append(f"{key}={data_dict[key]}")
    data_check_string = "\n".join(data_check_arr)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    result: Dict[str, Any] = {"auth_date": auth_date_str}
    for k, v in data_dict.items():
        result[k] = v
    user_json = data_dict.get("user")
    if user_json:
        try:
            user_obj = json.loads(user_json)
            result["user"] = user_obj
            result["user_id"] = user_obj.get("id")
            result["username"] = user_obj.get("username")
            result["first_name"] = user_obj.get("first_name")
            result["last_name"] = user_obj.get("last_name")
            result["language_code"] = user_obj.get("language_code")
            result["is_premium"] = user_obj.get("is_premium", False)
            result["photo_url"] = user_obj.get("photo_url", "")
        except Exception:
            return None
    else:
        return None
    if "user_id" not in result or not result["user_id"]:
        return None
    return result

def extract_init_data_from_header(authorization: Optional[str], x_telegram_header: Optional[str]) -> Optional[str]:
    if x_telegram_header:
        return x_telegram_header.strip()
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("tma "):
            return auth[4:].strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if "hash=" in token:
                return token
            return None
        if "hash=" in auth:
            return auth
    return None

def is_owner_user(user_id: int, owner_id: int, admin_ids: set[int] | list[int]) -> bool:
    if owner_id and user_id == owner_id:
        return True
    if user_id in set(admin_ids):
        return True
    return False

# =============================================================================
# DATABASE
# =============================================================================

def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None

def utc_now() -> float:
    return time.time()

class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    main_file TEXT,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    pid INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    runtime TEXT DEFAULT 'python',
                    language TEXT DEFAULT 'python',
                    description TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    uploaded_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_tokens (
                    token TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    project_id INTEGER,
                    details TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    added_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    is_premium INTEGER DEFAULT 0,
                    photo_url TEXT DEFAULT '',
                    last_seen REAL NOT NULL
                )
            """)
        self._migrate()

    def _migrate(self) -> None:
        try:
            with self._conn() as conn:
                try:
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
                except Exception:
                    cols = []
                if cols:
                    for col, default in [
                        ("runtime", "TEXT DEFAULT 'python'"),
                        ("language", "TEXT DEFAULT 'python'"),
                        ("description", "TEXT DEFAULT ''"),
                    ]:
                        if col not in cols:
                            try:
                                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {default}")
                            except Exception:
                                pass
                try:
                    tu_cols = [r[1] for r in conn.execute("PRAGMA table_info(telegram_users)").fetchall()]
                except Exception:
                    tu_cols = []
                if tu_cols and "photo_url" not in tu_cols:
                    try:
                        conn.execute("ALTER TABLE telegram_users ADD COLUMN photo_url TEXT DEFAULT ''")
                    except Exception:
                        pass
        except Exception:
            pass

    def seed_admins(self, user_ids: set[int]) -> None:
        with self._conn() as conn:
            for uid in user_ids:
                conn.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (uid, utc_now()))

    def add_admin(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (user_id, utc_now()))

    def remove_admin(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

    def list_admins(self) -> set[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM admins").fetchall()
        return {int(r["user_id"]) for r in rows}

    def is_admin(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

    def record_active_user(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO active_users (user_id, first_seen) VALUES (?, ?)", (user_id, utc_now()))

    def list_active_users(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id FROM active_users").fetchall()
        return [int(r["user_id"]) for r in rows]

    def upsert_telegram_user(self, user_id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None, language_code: str | None = None, is_premium: bool = False, photo_url: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO telegram_users (user_id, username, first_name, last_name, language_code, is_premium, photo_url, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=COALESCE(excluded.username, username),
                    first_name=COALESCE(excluded.first_name, first_name),
                    last_name=COALESCE(excluded.last_name, last_name),
                    language_code=COALESCE(excluded.language_code, language_code),
                    is_premium=excluded.is_premium,
                    photo_url=COALESCE(NULLIF(excluded.photo_url, ''), photo_url, telegram_users.photo_url),
                    last_seen=excluded.last_seen
            """, (user_id, username, first_name, last_name, language_code, 1 if is_premium else 0, photo_url or '', utc_now()))

    def get_telegram_user(self, user_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            return _row(conn.execute("SELECT * FROM telegram_users WHERE user_id = ?", (user_id,)).fetchone())

    def list_telegram_users(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM telegram_users ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def list_all_users_with_stats(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            users = conn.execute("SELECT * FROM telegram_users ORDER BY last_seen DESC").fetchall()
            result = []
            for u in users:
                uid = u["user_id"]
                count_row = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ?", (uid,)).fetchone()
                total = count_row["c"] if count_row else 0
                running_row = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ? AND status='running'", (uid,)).fetchone()
                running = running_row["c"] if running_row else 0
                result.append({
                    "user_id": uid,
                    "username": u["username"] or None,
                    "photo_url": u["photo_url"] or "",
                    "first_name": u["first_name"],
                    "last_name": u["last_name"],
                    "last_seen": u["last_seen"],
                    "total_projects": total,
                    "running_projects": running,
                })
            active_ids = conn.execute("SELECT user_id FROM active_users").fetchall()
            existing_ids = {r["user_id"] for r in result}
            for row in active_ids:
                aid = row["user_id"]
                if aid not in existing_ids:
                    count_row = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ?", (aid,)).fetchone()
                    total = count_row["c"] if count_row else 0
                    running_row = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ? AND status='running'", (aid,)).fetchone()
                    running = running_row["c"] if running_row else 0
                    result.append({
                        "user_id": aid,
                        "username": None,
                        "photo_url": "",
                        "first_name": None,
                        "last_name": None,
                        "last_seen": 0,
                        "total_projects": total,
                        "running_projects": running,
                    })
            proj_owners = conn.execute("SELECT DISTINCT owner_id FROM projects").fetchall()
            for row in proj_owners:
                oid = row["owner_id"]
                if oid not in existing_ids and oid not in {r["user_id"] for r in result}:
                    count_row = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ?", (oid,)).fetchone()
                    total = count_row["c"] if count_row else 0
                    result.append({
                        "user_id": oid,
                        "username": None,
                        "photo_url": "",
                        "first_name": None,
                        "last_name": None,
                        "last_seen": 0,
                        "total_projects": total,
                        "running_projects": 0,
                    })
            return result

    def create_project(self, owner_id: int, name: str, slug: str, runtime: str = "python", description: str = "") -> dict[str, Any]:
        with self._conn() as conn:
            now = utc_now()
            cur = conn.execute("INSERT INTO projects (owner_id, name, slug, status, created_at, updated_at, runtime, description) VALUES (?, ?, ?, 'stopped', ?, ?, ?, ?)", (owner_id, name, slug, now, now, runtime, description))
            pid = cur.lastrowid
        return self.get_project(pid)

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            return _row(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())

    def get_project_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return _row(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())

    def list_projects(self, owner_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM projects WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_all_projects(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def list_projects_by_user(self, owner_id: int) -> list[dict[str, Any]]:
        return self.list_projects(owner_id)

    def count_projects(self, owner_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM projects WHERE owner_id = ?", (owner_id,)).fetchone()
        return int(row["c"])

    def count_all_projects(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()
        return int(row["c"])

    def get_global_stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) as c FROM telegram_users").fetchone()["c"]
            distinct_owners = conn.execute("SELECT COUNT(DISTINCT owner_id) as c FROM projects").fetchone()["c"]
            total_users = max(total_users, distinct_owners)
            total_projects = conn.execute("SELECT COUNT(*) as c FROM projects").fetchone()["c"]
            running = conn.execute("SELECT COUNT(*) as c FROM projects WHERE status='running'").fetchone()["c"]
            stopped = total_projects - running
            recent = conn.execute("SELECT * FROM activity_logs ORDER BY created_at DESC LIMIT 20").fetchall()
        return {
            "total_users": total_users,
            "total_projects": total_projects,
            "running_projects": running,
            "stopped_projects": stopped,
            "recent_activity": [dict(r) for r in recent],
        }

    def update_project(self, project_id: int, **changes: Any) -> dict[str, Any] | None:
        if changes:
            changes["updated_at"] = utc_now()
            assignments = ", ".join(f"{k} = ?" for k in changes)
            with self._conn() as conn:
                conn.execute(f"UPDATE projects SET {assignments} WHERE id = ?", (*changes.values(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> dict[str, Any] | None:
        record = self.get_project(project_id)
        if record:
            with self._conn() as conn:
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return record

    def add_file(self, project_id: int, filename: str, size: int) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO files (project_id, filename, size, uploaded_at) VALUES (?, ?, ?, ?)", (project_id, filename, size, utc_now()))

    def list_files(self, project_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM files WHERE project_id = ? ORDER BY filename", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def create_login_token(self, owner_id: int, ttl_seconds: int) -> str:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute("INSERT INTO login_tokens (token, owner_id, expires_at, used) VALUES (?, ?, ?, 0)", (token, owner_id, utc_now() + ttl_seconds))
        return token

    def revoke_all_tokens(self, owner_id: int) -> int:
        with self._conn() as conn:
            cur = conn.execute("UPDATE login_tokens SET used = 1 WHERE owner_id = ? AND used = 0", (owner_id,))
        return cur.rowcount

    def consume_login_token(self, token: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM login_tokens WHERE token = ? AND used = 0 AND expires_at > ?", (token, utc_now())).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE login_tokens SET used = 1 WHERE token = ?", (token,))
        return int(row["owner_id"])

    def add_activity(self, action: str, actor: str, project_id: int | None = None, details: str = "") -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO activity_logs (action, actor, project_id, details, created_at) VALUES (?, ?, ?, ?, ?)", (action, actor, project_id, details, utc_now()))

    def list_activity(self, limit: int = 50, offset: int = 0, owner_id: int | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if owner_id is not None:
                rows = conn.execute("SELECT * FROM activity_logs WHERE actor = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (str(owner_id), limit, offset)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM activity_logs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def get_user_stats(self, owner_id: int) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ?", (owner_id,)).fetchone()["c"]
            running_stored = conn.execute("SELECT COUNT(*) as c FROM projects WHERE owner_id = ? AND status='running'", (owner_id,)).fetchone()["c"]
            stopped_stored = total - running_stored
            recent_activity = conn.execute("SELECT * FROM activity_logs WHERE actor = ? ORDER BY created_at DESC LIMIT 10", (str(owner_id),)).fetchall()
        return {
            "total": total,
            "running_stored": running_stored,
            "stopped_stored": stopped_stored,
            "recent_activity": [dict(r) for r in recent_activity],
        }

# =============================================================================
# PROCESS MANAGER
# =============================================================================

MAX_LOG_LINES = 1000

MODULE_PACKAGE_MAP = {
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "sqlalchemy": "SQLAlchemy",
    "dateutil": "python-dateutil",
}

MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([\w\.]+)['\"]")

class ManagedProject:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.process: asyncio.subprocess.Process | None = None
        self.log_lines: Deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._reader_task: asyncio.Task | None = None
        self.started_at: float | None = None
        self.restart_count: int = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

def _preexec_limits(cpu_seconds: int, memory_mb: int):
    def _limit() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            mem_bytes = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            os.setsid()
        except Exception:
            pass
    return _limit

class ProcessManager:
    def __init__(self, projects_dir: Path, cpu_seconds: int, memory_mb: int) -> None:
        self.projects_dir = projects_dir
        self.cpu_seconds = cpu_seconds
        self.memory_mb = memory_mb
        self._runtime: dict[int, ManagedProject] = {}

    def project_dir(self, project_id: int) -> Path:
        d = self.projects_dir / str(project_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def venv_python(self, project_id: int) -> Path:
        return self.project_dir(project_id) / ".venv" / "bin" / "python"

    def save_upload(self, project_id: int, filename: str, content: bytes) -> Path:
        dest = self.project_dir(project_id) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        if filename.lower().endswith(".zip"):
            self._extract_zip(dest)
        return dest

    def _extract_zip(self, zip_path: Path) -> None:
        target = zip_path.parent.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                member_path = (target / member.filename).resolve()
                if not str(member_path).startswith(str(target)):
                    raise ValueError(f"Unsafe zip member (path traversal): {member.filename}")
            zf.extractall(target)
        zip_path.unlink(missing_ok=True)

    def list_files(self, project_id: int) -> list[str]:
        d = self.project_dir(project_id)
        return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and ".venv" not in p.parts)

    def list_files_detailed(self, project_id: int) -> List[Dict[str, Any]]:
        d = self.project_dir(project_id)
        result = []
        for p in d.rglob("*"):
            if p.is_file() and ".venv" not in p.parts:
                rel = str(p.relative_to(d))
                try:
                    stat = p.stat()
                    result.append({
                        "path": rel,
                        "name": p.name,
                        "size": stat.st_size,
                        "extension": p.suffix.lower(),
                        "modified": stat.st_mtime,
                        "is_text": self._is_text_file(p),
                    })
                except Exception:
                    continue
        return sorted(result, key=lambda x: x["path"])

    def _is_text_file(self, path: Path) -> bool:
        text_exts = {".py", ".js", ".json", ".txt", ".md", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env", ".html", ".css", ".ts", ".jsx", ".tsx", ".sh", ".bat"}
        if path.suffix.lower() in text_exts:
            return True
        try:
            with open(path, 'rb') as f:
                chunk = f.read(1024)
                if b'\0' in chunk:
                    return False
            return True
        except Exception:
            return False

    def read_file(self, project_id: int, relative_path: str) -> str:
        path = self._safe_path(project_id, relative_path)
        return path.read_text(errors="replace")

    def write_file(self, project_id: int, relative_path: str, content: str) -> None:
        path = self._safe_path(project_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def delete_file(self, project_id: int, relative_path: str) -> bool:
        path = self._safe_path(project_id, relative_path)
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True

    def rename_file(self, project_id: int, old_path: str, new_path: str) -> bool:
        old = self._safe_path(project_id, old_path)
        new = self._safe_path(project_id, new_path)
        if not old.exists():
            raise FileNotFoundError(f"Source not found: {old_path}")
        if new.exists():
            raise ValueError("Destination already exists")
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        return True

    def get_file_info(self, project_id: int, relative_path: str) -> Dict[str, Any] | None:
        try:
            path = self._safe_path(project_id, relative_path)
            if not path.exists():
                return None
            stat = path.stat()
            return {
                "path": relative_path,
                "name": path.name,
                "size": stat.st_size,
                "extension": path.suffix.lower(),
                "modified": stat.st_mtime,
                "is_dir": path.is_dir(),
            }
        except Exception:
            return None

    def _safe_path(self, project_id: int, relative_path: str) -> Path:
        base = self.project_dir(project_id).resolve()
        if not relative_path or relative_path.startswith("/"):
            relative_path = relative_path.lstrip("/")
        path = (base / relative_path).resolve()
        if not str(path).startswith(str(base)):
            raise ValueError("Path escapes project directory")
        return path

    def ensure_venv(self, project_id: int) -> None:
        venv_dir = self.project_dir(project_id) / ".venv"
        if not venv_dir.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    def install_requirements(self, project_id: int) -> tuple[bool, str]:
        self.ensure_venv(project_id)
        project_dir = self.project_dir(project_id)
        req = project_dir / "requirements.txt"
        pkg_json = project_dir / "package.json"
        messages: list[str] = []
        ok = True
        if req.exists():
            pip = project_dir / ".venv" / "bin" / "pip"
            result = subprocess.run([str(pip), "install", "-r", str(req)], capture_output=True, text=True, timeout=600)
            ok = ok and result.returncode == 0
            messages.append(result.stdout[-1500:] + result.stderr[-1500:])
        else:
            messages.append("No requirements.txt found — skipping Python dependency install.")
        if pkg_json.exists():
            if shutil.which("npm") is None:
                ok = False
                messages.append("package.json found but 'npm' is not installed on this server.")
            else:
                result = subprocess.run(["npm", "install"], capture_output=True, text=True, cwd=str(project_dir), timeout=600)
                ok = ok and result.returncode == 0
                messages.append(result.stdout[-1500:] + result.stderr[-1500:])
        return ok, "\n".join(messages)

    def detect_runtime(self, project_id: int) -> str:
        files = self.list_files(project_id)
        has_py = any(f.endswith(".py") for f in files)
        has_js = any(f.endswith(".js") for f in files)
        has_req = any("requirements.txt" in f for f in files)
        has_pkg = any("package.json" in f for f in files)
        if has_py and has_js:
            return "python+node"
        if has_pkg or (has_js and not has_py):
            return "node"
        if has_py or has_req:
            return "python"
        return "python"

    def get_resource_info(self, project_id: int) -> Dict[str, Any]:
        rt = self._runtime.get(project_id)
        if not rt or not rt.running:
            return {"status": "stopped", "cpu": 0, "memory": 0, "uptime": 0}
        uptime = 0
        if rt.started_at:
            uptime = time.time() - rt.started_at
        mem_mb = 0
        cpu_percent = 0
        try:
            if rt.process and rt.process.pid:
                result = subprocess.run(["ps", "-o", "rss=,pcpu=", "-p", str(rt.process.pid)], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split()
                    if len(parts) >= 1:
                        mem_kb = int(parts[0])
                        mem_mb = mem_kb / 1024
                    if len(parts) >= 2:
                        cpu_percent = float(parts[1])
        except Exception:
            pass
        return {
            "status": "running",
            "cpu": cpu_percent,
            "memory": mem_mb,
            "memory_limit": self.memory_mb,
            "cpu_limit": self.cpu_seconds,
            "uptime": uptime,
            "pid": rt.process.pid if rt.process else None,
            "restart_count": rt.restart_count,
        }

    async def _autoinstall_for_missing_module(self, project_id: int, stderr_text: str) -> str:
        match = MISSING_MODULE_RE.search(stderr_text)
        if not match:
            return ""
        module = match.group(1).split(".")[0]
        package = MODULE_PACKAGE_MAP.get(module, module)
        pip = self.project_dir(project_id) / ".venv" / "bin" / "pip"
        try:
            proc = await asyncio.create_subprocess_exec(str(pip), "install", package, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except Exception as exc:
            return f"⚠️ Missing module `{module}` detected but auto-install failed: {exc}"
        if proc.returncode == 0:
            return f"📦 Auto-installed missing package `{package}` (for `{module}`)."
        return f"⚠️ Missing module `{module}` detected but auto-install of `{package}` failed."

    async def start(self, project_id: int, main_file: str) -> tuple[ManagedProject, str]:
        self.ensure_venv(project_id)
        rt = self._runtime.setdefault(project_id, ManagedProject(self.project_dir(project_id)))
        if rt.running:
            return rt, ""
        is_js = main_file.endswith(".js")
        python = self.venv_python(project_id)
        python_bin = str(python) if python.exists() else sys.executable
        interpreter = "node" if is_js else python_bin
        note = ""
        if not is_js:
            try:
                pre = await asyncio.create_subprocess_exec(interpreter, main_file, cwd=str(self.project_dir(project_id)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    _, stderr = await asyncio.wait_for(pre.communicate(), timeout=6)
                    stderr_text = stderr.decode(errors="replace")
                    if pre.returncode not in (0, None) and "ModuleNotFoundError" in stderr_text:
                        note = await self._autoinstall_for_missing_module(project_id, stderr_text)
                except asyncio.TimeoutError:
                    pre.kill()
                    await pre.wait()
            except Exception as exc:
                log.warning("Pre-check failed for project %s: %s", project_id, exc)
        rt.process = await asyncio.create_subprocess_exec(interpreter, main_file, cwd=str(self.project_dir(project_id)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, preexec_fn=_preexec_limits(self.cpu_seconds, self.memory_mb))
        rt.log_lines.clear()
        rt.started_at = time.time()
        rt._reader_task = asyncio.create_task(self._pump_output(rt))
        return rt, note

    async def restart(self, project_id: int, main_file: str) -> tuple[ManagedProject, str]:
        await self.stop(project_id)
        await asyncio.sleep(0.5)
        rt, note = await self.start(project_id, main_file)
        rt.restart_count += 1
        return rt, note

    async def _pump_output(self, rt: ManagedProject) -> None:
        assert rt.process is not None and rt.process.stdout is not None
        try:
            async for raw_line in rt.process.stdout:
                rt.log_lines.append(raw_line.decode(errors="replace").rstrip("\n"))
        except Exception as exc:
            rt.log_lines.append(f"[log stream ended: {exc}]")

    async def stop(self, project_id: int) -> bool:
        rt = self._runtime.get(project_id)
        if rt is None or not rt.running:
            return False
        assert rt.process is not None
        try:
            pgid = os.getpgid(rt.process.pid)
            os.killpg(pgid, 15)
        except Exception:
            rt.process.terminate()
        try:
            await asyncio.wait_for(rt.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            rt.process.kill()
        rt.started_at = None
        return True

    def status(self, project_id: int) -> str:
        rt = self._runtime.get(project_id)
        return "running" if (rt and rt.running) else "stopped"

    def tail(self, project_id: int, n: int = 200) -> list[str]:
        rt = self._runtime.get(project_id)
        if rt is None:
            return []
        return list(rt.log_lines)[-n:]

    def clear_logs(self, project_id: int) -> bool:
        rt = self._runtime.get(project_id)
        if rt is None:
            return False
        rt.log_lines.clear()
        return True

    def delete(self, project_id: int) -> None:
        shutil.rmtree(self.project_dir(project_id), ignore_errors=True)
        self._runtime.pop(project_id, None)

    def count_all_running(self, owner_id: int | None = None) -> int:
        count = 0
        for rt in self._runtime.values():
            if rt.running:
                count += 1
        return count

# =============================================================================
# TELEGRAM BOT (Optional, for legacy bot commands)
# =============================================================================

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update, WebAppInfo
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    log.warning("python-telegram-bot not installed, bot functionality disabled")

if TELEGRAM_AVAILABLE:
    BTN_MY_FILES = "📁 My Files"
    BTN_UPLOAD_FILE = "📤 Upload File"
    BTN_EDIT_FILES = "✏️ Edit Files"
    BTN_DELETE_FOLDER = "🗑️ Delete Folder"
    BTN_RUN_MODULE = "▶️ Run Module"
    BTN_STATUS = "📊 Status"
    BTN_MY_LOGS = "📋 My Logs"
    BTN_REVOKE_TOKEN = "🔄 Revoke Token"
    BTN_WEB_DASHBOARD = "🌐 Web Dashboard"
    BTN_OPEN_PANEL = "🚀 Open Hosting Panel"

    MAIN_MENU = ReplyKeyboardMarkup(
        [
            [BTN_OPEN_PANEL],
            [BTN_MY_FILES, BTN_UPLOAD_FILE],
            [BTN_EDIT_FILES, BTN_DELETE_FOLDER],
            [BTN_RUN_MODULE, BTN_STATUS],
            [BTN_MY_LOGS, BTN_REVOKE_TOKEN],
            [BTN_WEB_DASHBOARD],
        ],
        resize_keyboard=True,
    )

    def _is_owner_bot(owner_id: int, user_id: int) -> bool:
        return owner_id != 0 and user_id == owner_id

    class BotHostBot:
        def __init__(self, db: Database, pm: ProcessManager, bot_token: str, owner_id: int, admin_ids: list[int], mini_app_url: str, max_projects: int, max_upload: int, login_ttl: int) -> None:
            self.db = db
            self.pm = pm
            self.bot_token = bot_token
            self.owner_user_id = owner_id
            self.admin_user_ids = set(admin_ids)
            self.mini_app_url = mini_app_url
            self.max_projects_per_user = max_projects
            self.max_upload_bytes = max_upload
            self.login_token_ttl_seconds = login_ttl
            self.application: Application | None = None
            self._pending_uploads: dict[int, dict] = {}
            self._pending_edits: dict[int, dict] = {}

        def build(self) -> Application:
            app = Application.builder().token(self.bot_token).build()
            app.add_handler(CommandHandler("start", self.cmd_start))
            app.add_handler(CommandHandler("panel", self.cmd_panel))
            app.add_handler(CommandHandler("hosting", self.cmd_panel))
            app.add_handler(CommandHandler("myfiles", self.cmd_myfiles))
            app.add_handler(CommandHandler("status", self.cmd_status))
            app.add_handler(CommandHandler("dashboard", self.cmd_dashboard))
            app.add_handler(CommandHandler("addadmin", self.cmd_add_admin))
            app.add_handler(CommandHandler("removeadmin", self.cmd_remove_admin))
            app.add_handler(CommandHandler("listadmins", self.cmd_list_admins))
            app.add_handler(CommandHandler("broadcast", self.cmd_broadcast))
            app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
            app.add_handler(CallbackQueryHandler(self.on_callback))
            self.application = app
            return app

        def _guard(self, update: Update) -> bool:
            user = update.effective_user
            if not user:
                return False
            if not self.admin_user_ids and self.owner_user_id == 0:
                return True
            return self.db.is_admin(user.id) or user.id == self.owner_user_id or user.id in self.admin_user_ids

        def _get_mini_app_keyboard(self) -> InlineKeyboardMarkup:
            mini_app_url = self.mini_app_url
            buttons = []
            if mini_app_url:
                try:
                    buttons.append([InlineKeyboardButton("🚀 Open Hosting Panel", web_app=WebAppInfo(url=mini_app_url))])
                except Exception:
                    buttons.append([InlineKeyboardButton("🚀 Open Hosting Panel", url=mini_app_url)])
            buttons.append([InlineKeyboardButton("📊 My Projects", callback_data="myfiles:0")])
            buttons.append([InlineKeyboardButton("🌐 Legacy Dashboard", callback_data="dashboard")])
            return InlineKeyboardMarkup(buttons)

        async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                await update.message.reply_text("This bot is private. Contact owner for access.")
                return
            user = update.effective_user
            self.db.record_active_user(user.id)
            try:
                self.db.upsert_telegram_user(user.id, username=user.username, first_name=user.first_name, last_name=user.last_name, language_code=user.language_code)
            except Exception:
                pass
            welcome_text = (
                "🚀 *Welcome to SNUKED HOSTER — Premium Bot Hosting*\\n\\n"
                "Host your Python & Node.js bots directly from Telegram\\!\\n\\n"
                "✨ *What you can do:*\\n"
                "• 📤 Upload \\.py, \\.js, or \\.zip files\\n"
                "• ▶️ Start / Stop / Restart with one tap\\n"
                "• 📁 File manager & code editor\\n"
                "• 📋 Live logs & monitoring\\n"
                "• 🌐 Premium dashboard inside Telegram\\n\\n"
                "👇 *Tap below to open your hosting panel*\\n\\n"
                "Or send me a file to create a project instantly\\."
            )
            keyboard = self._get_mini_app_keyboard()
            await update.message.reply_text(welcome_text, parse_mode="MarkdownV2", reply_markup=keyboard)
            await update.message.reply_text("Use the menu below anytime or type /panel to open hosting panel:", reply_markup=MAIN_MENU)

        async def cmd_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                await update.message.reply_text("This bot is private.")
                return
            self.db.record_active_user(update.effective_user.id)
            if not self.mini_app_url:
                await update.message.reply_text("⚠️ Mini App URL not configured.")
                return
            keyboard = self._get_mini_app_keyboard()
            await update.message.reply_text(
                "🚀 *SNUKED HOSTER Hosting Panel*\\n\\n"
                "Tap the button below to open your premium hosting dashboard inside Telegram\\!\\n\\n"
                "• Manage projects\\n"
                "• Edit files\\n"
                "• View live logs\\n"
                "• Start/Stop bots\\n\\n"
                f"URL: {self.mini_app_url}",
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )

        async def cmd_add_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_owner_bot(self.owner_user_id, update.effective_user.id):
                await update.message.reply_text("Owner only.")
                return
            if not context.args or not context.args[0].isdigit():
                await update.message.reply_text("Usage: /addadmin <telegram_user_id>")
                return
            new_admin_id = int(context.args[0])
            self.db.add_admin(new_admin_id)
            await update.message.reply_text(f"✅ Added admin: {new_admin_id}")

        async def cmd_remove_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_owner_bot(self.owner_user_id, update.effective_user.id):
                await update.message.reply_text("Owner only.")
                return
            if not context.args or not context.args[0].isdigit():
                await update.message.reply_text("Usage: /removeadmin <telegram_user_id>")
                return
            target_id = int(context.args[0])
            if target_id == self.owner_user_id:
                await update.message.reply_text("❌ Can't remove the owner.")
                return
            removed = self.db.remove_admin(target_id)
            await update.message.reply_text("✅ Removed." if removed else "That ID wasn't an admin.")

        async def cmd_list_admins(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            admins = sorted(self.db.list_admins())
            lines = [f"• `{a}`" + (" (owner)" if a == self.owner_user_id else "") for a in admins]
            await update.message.reply_text("*Admins:*\\n" + "\\n".join(lines), parse_mode="Markdown")

        async def cmd_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _is_owner_bot(self.owner_user_id, update.effective_user.id):
                await update.message.reply_text("Owner only.")
                return
            text = " ".join(context.args) if context.args else ""
            if not text:
                await update.message.reply_text("Usage: /broadcast <message>")
                return
            recipients = self.db.list_active_users()
            sent, failed = 0, 0
            for uid in recipients:
                try:
                    await context.bot.send_message(uid, f"📢 {text}")
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(f"Broadcast sent: {sent} ok, {failed} failed.")

        async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            doc = update.message.document
            filename = sanitize_filename(doc.file_name or "file")
            if not is_allowed_upload(filename):
                await update.message.reply_text("❌ Unsupported file type. Send a .py, .js, .txt, .json or .zip file.")
                return
            if doc.file_size and doc.file_size > self.max_upload_bytes:
                await update.message.reply_text("❌ File too large.")
                return
            tg_file = await doc.get_file()
            content = bytes(await tg_file.download_as_bytearray())
            user_id = update.effective_user.id
            self._pending_uploads[user_id] = {"filename": filename, "content": content}
            await update.message.reply_text("📝 Enter a project name (or tap 🚀 Open Hosting Panel to manage in Mini App):")

        async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            user_id = update.effective_user.id
            text = update.message.text.strip()
            pending_edit = self._pending_edits.get(user_id)
            if pending_edit is not None:
                del self._pending_edits[user_id]
                try:
                    self.pm.write_file(pending_edit["project_id"], pending_edit["path"], update.message.text)
                    self.db.add_activity("edit_file", str(user_id), pending_edit["project_id"], pending_edit["path"])
                    await update.message.reply_text(f"✅ Saved `{pending_edit['path']}`.", parse_mode="Markdown")
                except ValueError:
                    await update.message.reply_text("❌ Invalid file path.")
                return
            if text == BTN_OPEN_PANEL:
                await self.cmd_panel(update, context)
                return
            if text == BTN_MY_FILES:
                await self._send_project_list(update.effective_chat.id, user_id, context)
                return
            if text == BTN_UPLOAD_FILE:
                await update.message.reply_text("📤 Send me a .py, .txt, .json or .zip file to get started. Or use 🚀 Open Hosting Panel for advanced upload.")
                return
            if text == BTN_EDIT_FILES:
                await self._send_project_picker(update.effective_chat.id, user_id, context, "editfiles", "Pick a project to edit:")
                return
            if text == BTN_DELETE_FOLDER:
                await self._send_project_picker(update.effective_chat.id, user_id, context, "delete", "Pick a project to delete:")
                return
            if text == BTN_RUN_MODULE:
                await self._send_project_picker(update.effective_chat.id, user_id, context, "start", "Pick a project to run:")
                return
            if text == BTN_STATUS:
                await self._reply_status(update.effective_chat.id, user_id, context)
                return
            if text == BTN_MY_LOGS:
                await self._send_project_picker(update.effective_chat.id, user_id, context, "logs", "Pick a project for logs:")
                return
            if text == BTN_REVOKE_TOKEN:
                count = self.db.revoke_all_tokens(user_id)
                await update.message.reply_text(f"🔄 Revoked {count} outstanding dashboard link(s). Use /dashboard for a fresh one.")
                return
            if text == BTN_WEB_DASHBOARD:
                await self._send_dashboard_link(update.effective_chat.id, user_id, context)
                return
            pending = self._pending_uploads.pop(user_id, None)
            if pending is None:
                return
            if self.db.count_projects(user_id) >= self.max_projects_per_user:
                await update.message.reply_text(f"❌ Project limit reached ({self.max_projects_per_user}). Delete a project first with My Files or in the Mini App.")
                return
            name = update.message.text.strip()[:64]
            slug = sanitize_slug(f"{user_id}-{name}-{int(time.time())}")
            project = self.db.create_project(user_id, name, slug)
            project_id = project["id"]
            try:
                self.pm.save_upload(project_id, pending["filename"], pending["content"])
            except ValueError as exc:
                self.pm.delete(project_id)
                self.db.delete_project(project_id)
                await update.message.reply_text(f"❌ Rejected: {exc}")
                return
            main_file = pending["filename"] if pending["filename"].endswith((".py", ".js")) else None
            if main_file is None:
                candidates = [f for f in self.pm.list_files(project_id) if f.endswith(("main.py", "bot.py", "app.py", "index.js", "main.js", "bot.js"))]
                main_file = candidates[0] if candidates else None
            self.db.update_project(project_id, main_file=main_file)
            self.db.add_file(project_id, pending["filename"], len(pending["content"]))
            await update.message.reply_text("📦 Installing dependencies…")
            ok, log_tail = self.pm.install_requirements(project_id)
            status_line = "✅ All dependencies OK" if ok else f"⚠️ Dependency install had issues:\\n{log_tail[-500:]}"
            self.db.add_activity("upload", str(user_id), project_id, pending["filename"])
            keyboard = self._get_mini_app_keyboard()
            await update.message.reply_text(
                f"✅ *File Uploaded!*\\n\\n"
                f"Project: *{name}*\\n"
                f"File: `{pending['filename']}`\\n"
                f"Size: {len(pending['content']) / 1024:.1f} KB\\n"
                f"{status_line}\\n\\n"
                "Use 📂 My Files or 🚀 Open Hosting Panel to manage it.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        async def cmd_myfiles(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            await self._send_project_list(update.effective_chat.id, update.effective_user.id, context)

        async def _send_project_list(self, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
            projects = self.db.list_projects(user_id)
            if not projects:
                keyboard = self._get_mini_app_keyboard()
                await context.bot.send_message(chat_id, "You have no projects yet. Send me a file or open the Hosting Panel!", reply_markup=keyboard)
                return
            running = sum(1 for p in projects if self.pm.status(p["id"]) == "running")
            lines = [f"*Your Projects* ({len(projects)} total)", ""]
            buttons = []
            for p in projects:
                status = self.pm.status(p["id"])
                dot = "🟢" if status == "running" else "⚪"
                buttons.append([InlineKeyboardButton(f"{dot} {p['name']}", callback_data=f"project:{p['id']}")])
            lines.append(f"Running: {running} / {len(projects)}")
            mini_app_url = self.mini_app_url
            if mini_app_url:
                try:
                    buttons.append([InlineKeyboardButton("🚀 Open Hosting Panel", web_app=WebAppInfo(url=mini_app_url))])
                except Exception:
                    buttons.append([InlineKeyboardButton("🚀 Open Hosting Panel", url=mini_app_url)])
            await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

        async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            await self._reply_status(update.effective_chat.id, update.effective_user.id, context)

        async def _reply_status(self, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
            projects = self.db.list_projects(user_id)
            running = sum(1 for p in projects if self.pm.status(p["id"]) == "running")
            await context.bot.send_message(chat_id, "*Your Status*\\n\\n" f"Projects: {len(projects)}\\n" f"Running: {running}\\n" f"Limit: {len(projects)}/{self.max_projects_per_user}", parse_mode="Markdown")

        async def _send_project_picker(self, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, action_prefix: str, title: str) -> None:
            projects = self.db.list_projects(user_id)
            if not projects:
                await context.bot.send_message(chat_id, "You have no projects yet.")
                return
            buttons = [[InlineKeyboardButton(p["name"], callback_data=f"{action_prefix}:{p['id']}")] for p in projects]
            await context.bot.send_message(chat_id, title, reply_markup=InlineKeyboardMarkup(buttons))

        async def cmd_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._guard(update):
                return
            await self._send_dashboard_link(update.effective_chat.id, update.effective_user.id, context)

        async def _send_dashboard_link(self, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
            token = self.db.create_login_token(user_id, self.login_token_ttl_seconds)
            legacy_url = f"{self.mini_app_url}/login.html?token={token}" if self.mini_app_url else f"/login.html?token={token}"
            minutes = self.login_token_ttl_seconds // 60
            keyboard = self._get_mini_app_keyboard()
            await context.bot.send_message(chat_id, "🌐 *Hosting Dashboard Access*\\n\\n" f"🚀 *Mini App (Recommended):*\\n{self.mini_app_url}\\n\\n" f"🔗 *Legacy Web Dashboard:*\\n{legacy_url}\\n\\n" f"Legacy link expires in {minutes} minutes.\\n" "Mini App uses secure Telegram authentication automatically.", parse_mode="Markdown", disable_web_page_preview=True, reply_markup=keyboard)

        async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            query = update.callback_query
            user = update.effective_user
            if not user or not self._guard(update):
                await query.answer("Not authorized.", show_alert=True)
                return
            await query.answer()
            data = query.data or ""
            if data == "dashboard":
                await self._send_dashboard_link(query.message.chat_id, user.id, context)
                return
            if data.startswith("myfiles"):
                await self._send_project_list(query.message.chat_id, user.id, context)
                return
            if data.startswith("noop"):
                return
            if data.startswith("project:"):
                project_id = int(data.split(":", 1)[1])
                await self._send_project_detail(query.message.chat_id, project_id, context)
                return
            if data.startswith("logs:"):
                project_id = int(data.split(":", 1)[1])
                project = self.db.get_project(project_id)
                if not project or project["owner_id"] != user.id:
                    await context.bot.send_message(query.message.chat_id, "Project not found.")
                    return
                lines = self.pm.tail(project_id, 60)
                body = "\n".join(lines) if lines else "(no output yet — start the project first)"
                await context.bot.send_message(query.message.chat_id, f"📋 *Logs — {project['name']}*\\n```\\n{body[-3500:]}\\n```", parse_mode="Markdown")
                return
            if data.startswith("editfiles:"):
                project_id = int(data.split(":", 1)[1])
                project = self.db.get_project(project_id)
                if not project or project["owner_id"] != user.id:
                    await context.bot.send_message(query.message.chat_id, "Project not found.")
                    return
                files = self.pm.list_files(project_id)
                if not files:
                    await context.bot.send_message(query.message.chat_id, "No files in this project.")
                    return
                buttons = [[InlineKeyboardButton(f, callback_data=f"editfile:{project_id}:{i}")] for i, f in enumerate(files)]
                context.chat_data[f"files:{project_id}"] = files
                await context.bot.send_message(query.message.chat_id, "Pick a file to edit:", reply_markup=InlineKeyboardMarkup(buttons))
                return
            if data.startswith("editfile:"):
                _, project_id_raw, index_raw = data.split(":", 2)
                project_id = int(project_id_raw)
                files = context.chat_data.get(f"files:{project_id}") or self.pm.list_files(project_id)
                index = int(index_raw)
                if index >= len(files):
                    await context.bot.send_message(query.message.chat_id, "File list expired, tap ✏️ Edit Files again.")
                    return
                path = files[index]
                try:
                    content = self.pm.read_file(project_id, path)
                except Exception:
                    await context.bot.send_message(query.message.chat_id, "Could not read that file.")
                    return
                self._pending_edits[user.id] = {"project_id": project_id, "path": path}
                preview = content if len(content) <= 3500 else content[:3500] + "\n…(truncated)"
                await context.bot.send_message(query.message.chat_id, f"✏️ *{path}* — current content:\\n```\\n{preview}\\n```\\n\\nReply with the new full content to save it.", parse_mode="Markdown")
                return
            if data.startswith("start:") or data.startswith("stop:") or data.startswith("delete:"):
                action, raw_id = data.split(":", 1)
                project_id = int(raw_id)
                project = self.db.get_project(project_id)
                if not project or project["owner_id"] != user.id:
                    await context.bot.send_message(query.message.chat_id, "Project not found.")
                    return
                if action == "start":
                    if not project["main_file"]:
                        await context.bot.send_message(query.message.chat_id, "❌ No entrypoint (.py) found for this project.")
                        return
                    _, note = await self.pm.start(project_id, project["main_file"])
                    self.db.update_project(project_id, status="running")
                    self.db.add_activity("start", str(user.id), project_id)
                    msg = f"▶️ Started *{project['name']}*"
                    if note:
                        msg += f"\n{note}"
                    await context.bot.send_message(query.message.chat_id, msg, parse_mode="Markdown")
                elif action == "stop":
                    await self.pm.stop(project_id)
                    self.db.update_project(project_id, status="stopped")
                    self.db.add_activity("stop", str(user.id), project_id)
                    await context.bot.send_message(query.message.chat_id, f"⏹ Stopped *{project['name']}*", parse_mode="Markdown")
                elif action == "delete":
                    await self.pm.stop(project_id)
                    self.db.add_activity("delete", str(user.id), project_id, project['name'])
                    self.pm.delete(project_id)
                    self.db.delete_project(project_id)
                    await context.bot.send_message(query.message.chat_id, f"🗑 Deleted *{project['name']}*", parse_mode="Markdown")
                return

        async def _send_project_detail(self, chat_id: int, project_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
            project = self.db.get_project(project_id)
            if not project:
                await context.bot.send_message(chat_id, "Project not found.")
                return
            status = self.pm.status(project_id)
            files = self.db.list_files(project_id)
            buttons = [
                [InlineKeyboardButton("▶️ Start", callback_data=f"start:{project_id}"), InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{project_id}")],
                [InlineKeyboardButton("🗑 Delete Project", callback_data=f"delete:{project_id}")],
                [InlineKeyboardButton("« Back", callback_data="myfiles:0")],
            ]
            mini_app_url = self.mini_app_url
            if mini_app_url:
                try:
                    buttons.insert(0, [InlineKeyboardButton("🚀 Open in Hosting Panel", web_app=WebAppInfo(url=mini_app_url))])
                except Exception:
                    pass
            await context.bot.send_message(chat_id, f"*{project['name']}*\\n\\n" f"Status: {'🟢 running' if status == 'running' else '⚪ stopped'}\\n" f"Entrypoint: `{project['main_file'] or 'not set'}`\\n" f"Files: {len(files)}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    class BotRunner:
        def __init__(self, db: Database, pm: ProcessManager, bot_token: str, owner_id: int, admin_ids: list[int]) -> None:
            self.bot_token = bot_token
            self.owner_user_id = owner_id
            self.admin_user_ids = admin_ids
            self.bot_host = BotHostBot(db, pm, bot_token, owner_id, admin_ids, MINI_APP_URL, MAX_PROJECTS_PER_USER, MAX_UPLOAD_BYTES, LOGIN_TOKEN_TTL_SECONDS) if TELEGRAM_AVAILABLE else None
            self.application = None

        @property
        def configured(self) -> bool:
            return bool(self.bot_token and self.bot_token != "PASTE_BOT_TOKEN_HERE" and (self.admin_user_ids or self.owner_user_id))

        async def start(self) -> None:
            if not self.configured or not TELEGRAM_AVAILABLE:
                log.warning("BOT_TOKEN or ADMIN_USER_IDS not set; Telegram bot disabled.")
                return
            seed = set(self.admin_user_ids)
            if self.owner_user_id:
                seed.add(self.owner_user_id)
            self.bot_host.db.seed_admins(seed)
            self.application = self.bot_host.build()
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()

        async def stop(self) -> None:
            if self.application is None:
                return
            try:
                await self.application.updater.stop()
            except Exception:
                pass
            await self.application.stop()
            await self.application.shutdown()
else:
    BotRunner = None

# =============================================================================
# FASTAPI APP & AUTH HELPERS
# =============================================================================

SESSION_COOKIE = "bh_session"

class TokenLoginRequest(BaseModel):
    token: str

class TelegramAuthRequest(BaseModel):
    initData: str

class FileWriteRequest(BaseModel):
    content: str

class RenameRequest(BaseModel):
    new_path: str

class CreateFileRequest(BaseModel):
    path: str
    content: str = ""
    is_dir: bool = False

def _check_owner(user_id: int) -> bool:
    return is_owner_user(user_id, OWNER_USER_ID, set(ADMIN_USER_IDS))

def require_session(cookie_value: str | None = None, authorization: str | None = None, x_telegram_init_data: str | None = None) -> dict:
    init_data = extract_init_data_from_header(authorization, x_telegram_init_data)
    if init_data and BOT_TOKEN and BOT_TOKEN != "PASTE_BOT_TOKEN_HERE":
        validated = validate_telegram_init_data(init_data, BOT_TOKEN)
        if validated:
            user_id = validated["user_id"]
            is_owner = is_owner_user(user_id, OWNER_USER_ID, set(ADMIN_USER_IDS))
            return {
                "owner_id": user_id,
                "tg_username": validated.get("username"),
                "tg_photo_url": validated.get("photo_url", ""),
                "tg_user": validated.get("user"),
                "is_owner": is_owner,
                "auth_method": "telegram_init_data",
            }
    if not cookie_value:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = verify_session(cookie_value, SECRET_KEY)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired")
    owner_id = session.get("owner_id")
    if owner_id:
        session["is_owner"] = is_owner_user(owner_id, OWNER_USER_ID, set(ADMIN_USER_IDS))
    return session

def require_owner(cookie_value: str | None = None, authorization: str | None = None, x_telegram_init_data: str | None = None) -> dict:
    session = require_session(cookie_value, authorization, x_telegram_init_data)
    if not session.get("is_owner"):
        raise HTTPException(status_code=403, detail="Owner access required")
    return session

def owned_project(db: Database, project_id: int, cookie_value: str | None, authorization: str | None = None, x_telegram_init_data: str | None = None) -> dict:
    session = require_session(cookie_value, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if session.get("is_owner"):
        return project
    if project["owner_id"] != session["owner_id"]:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

# Initialize core services
db = Database(DATABASE_PATH)
pm = ProcessManager(PROJECTS_DIR_PATH, PROCESS_CPU_SECONDS, PROCESS_MEMORY_MB)
bot_runner = BotRunner(db, pm, BOT_TOKEN, OWNER_USER_ID, ADMIN_USER_IDS) if TELEGRAM_AVAILABLE and BotRunner else None

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if bot_runner:
        try:
            await bot_runner.start()
        except Exception:
            log.exception("Telegram bot failed to start; API will remain available without it")
    yield
    if bot_runner:
        try:
            await bot_runner.stop()
        except Exception:
            pass

app = FastAPI(title="SNUKED HOSTER - Telegram Mini App Hosting", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if "*" not in CORS_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "ALLOWALL")
    return response

# =============================================================================
# AUTH ROUTES
# =============================================================================

@app.post("/api/auth/token-login")
async def token_login(body: TokenLoginRequest):
    from fastapi.responses import JSONResponse
    owner_id = db.consume_login_token(body.token)
    if owner_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    csrf = generate_csrf_token()
    is_owner = _check_owner(owner_id)
    session = sign_session({"owner_id": owner_id, "csrf": csrf, "is_owner": is_owner}, SECRET_KEY, SESSION_TTL_SECONDS)
    response = JSONResponse({"ok": True, "owner_id": owner_id, "csrf_token": csrf, "is_owner": is_owner})
    response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=SESSION_TTL_SECONDS)
    db.add_activity("login", str(owner_id))
    return response

@app.post("/api/auth/telegram")
async def telegram_auth(body: TelegramAuthRequest, request: Request):
    from fastapi.responses import JSONResponse
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise HTTPException(status_code=500, detail="Bot token not configured")
    validated = validate_telegram_init_data(body.initData, BOT_TOKEN)
    if not validated:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")
    user_id = validated.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid Telegram user")
    db.record_active_user(user_id)
    try:
        db.upsert_telegram_user(user_id, username=validated.get("username"), first_name=validated.get("first_name"), last_name=validated.get("last_name"), language_code=validated.get("language_code"), is_premium=validated.get("is_premium", False), photo_url=validated.get("photo_url", ""))
    except Exception:
        pass
    csrf = generate_csrf_token()
    is_owner = _check_owner(user_id)
    session_payload = {"owner_id": user_id, "csrf": csrf, "tg_username": validated.get("username"), "tg_photo_url": validated.get("photo_url", ""), "is_owner": is_owner}
    session = sign_session(session_payload, SECRET_KEY, SESSION_TTL_SECONDS)
    response = JSONResponse({"ok": True, "owner_id": user_id, "csrf_token": csrf, "is_owner": is_owner, "username": validated.get("username"), "photo_url": validated.get("photo_url", ""), "user": validated.get("user")})
    response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax", secure=COOKIE_SECURE, max_age=SESSION_TTL_SECONDS)
    db.add_activity("telegram_login", str(user_id))
    return response

@app.get("/api/auth/me")
async def auth_me(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = None
    tg_data = None
    init_data = extract_init_data_from_header(authorization, x_telegram_init_data)
    if init_data and BOT_TOKEN and BOT_TOKEN != "PASTE_BOT_TOKEN_HERE":
        validated = validate_telegram_init_data(init_data, BOT_TOKEN)
        if validated:
            tg_data = validated
            is_owner = _check_owner(validated["user_id"])
            session = {"owner_id": validated["user_id"], "tg_username": validated.get("username"), "tg_photo_url": validated.get("photo_url", ""), "is_owner": is_owner}
            try:
                db.upsert_telegram_user(validated["user_id"], username=validated.get("username"), first_name=validated.get("first_name"), last_name=validated.get("last_name"), language_code=validated.get("language_code"), is_premium=validated.get("is_premium", False), photo_url=validated.get("photo_url", ""))
            except Exception:
                pass
    if not session:
        if not bh_session:
            raise HTTPException(status_code=401, detail="Not authenticated")
        session = verify_session(bh_session, SECRET_KEY)
        if session is None:
            raise HTTPException(status_code=401, detail="Session expired")
        owner_id = session.get("owner_id")
        if owner_id:
            session["is_owner"] = _check_owner(owner_id)
    owner_id = session["owner_id"]
    tg_user = db.get_telegram_user(owner_id)
    is_owner = _check_owner(owner_id)
    username = session.get("tg_username") or (tg_user["username"] if tg_user else None)
    photo_url = session.get("tg_photo_url") or (tg_user["photo_url"] if tg_user else "")
    display_username = f"@{username}" if username else "@username_unavailable"
    return {"owner_id": owner_id, "username": username, "display_username": display_username, "photo_url": photo_url, "is_owner": is_owner, "is_admin": is_owner, "telegram_user": {"username": username, "photo_url": photo_url} if tg_user else None}

@app.post("/api/auth/logout")
async def logout():
    from fastapi.responses import JSONResponse
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response

# =============================================================================
# MAIN API ROUTES (Projects, etc.)
# =============================================================================

@app.get("/api/me")
async def get_me(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_session(bh_session, authorization, x_telegram_init_data)
    owner_id = session["owner_id"]
    projects = db.list_projects(owner_id)
    running = 0
    for p in projects:
        p["runtime_status"] = pm.status(p["id"])
        if p["runtime_status"] == "running":
            running += 1
    total = len(projects)
    stopped = total - running
    tg_user = db.get_telegram_user(owner_id)
    recent = db.list_activity(limit=10, owner_id=owner_id)
    total_memory = 0
    for p in projects:
        if p["runtime_status"] == "running":
            res = pm.get_resource_info(p["id"])
            total_memory += res.get("memory", 0)
    username = session.get("tg_username") or (tg_user["username"] if tg_user else None)
    photo_url = session.get("tg_photo_url") or (tg_user["photo_url"] if tg_user else "")
    display_username = f"@{username}" if username else "@username_unavailable"
    is_owner = is_owner_user(owner_id, OWNER_USER_ID, set(ADMIN_USER_IDS))
    return {"owner_id": owner_id, "username": username, "display_username": display_username, "photo_url": photo_url, "is_owner": is_owner, "stats": {"total": total, "running": running, "stopped": stopped, "memory_usage": total_memory}, "recent_activity": recent}

@app.get("/api/activity")
async def get_activity(limit: int = 20, offset: int = 0, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_session(bh_session, authorization, x_telegram_init_data)
    owner_id = session["owner_id"]
    if session.get("is_owner"):
        activity = db.list_activity(limit=limit, offset=offset, owner_id=None)
    else:
        activity = db.list_activity(limit=limit, offset=offset, owner_id=owner_id)
    return {"activity": activity}

@app.get("/api/projects")
async def list_projects(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_session(bh_session, authorization, x_telegram_init_data)
    owner_id = session["owner_id"]
    projects = db.list_projects(owner_id)
    for p in projects:
        p["runtime_status"] = pm.status(p["id"])
        p["runtime"] = p.get("runtime") or pm.detect_runtime(p["id"])
        p["resource"] = pm.get_resource_info(p["id"])
        p["created_date"] = p.get("created_at")
    return {"projects": projects}

@app.post("/api/projects")
async def create_project(request: Request, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_session(bh_session, authorization, x_telegram_init_data)
    owner_id = session["owner_id"]
    content_type = request.headers.get("content-type", "")
    name = None
    runtime = "python"
    description = ""
    upload_file: UploadFile | None = None
    if "multipart/form-data" in content_type:
        form = await request.form()
        name = form.get("name")
        runtime = form.get("runtime", "python")
        description = form.get("description", "")
        if "file" in form:
            upload_file = form["file"]
        elif "zip" in form:
            upload_file = form["zip"]
    else:
        try:
            body = await request.json()
            name = body.get("name")
            runtime = body.get("runtime", "python")
            description = body.get("description", "")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid request body")
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Project name is required")
    name = name.strip()[:64]
    if runtime not in SUPPORTED_RUNTIMES and runtime not in ["python", "node", "python+node"]:
        runtime = "python"
    if db.count_projects(owner_id) >= MAX_PROJECTS_PER_USER:
        raise HTTPException(status_code=400, detail=f"Project limit reached ({MAX_PROJECTS_PER_USER})")
    slug = sanitize_slug(f"{owner_id}-{name}-{int(time.time())}")
    project = db.create_project(owner_id, name, slug, runtime=runtime, description=description)
    project_id = project["id"]
    main_file = None
    if upload_file:
        filename = sanitize_filename(upload_file.filename or "upload.zip")
        if not is_allowed_upload(filename):
            pm.delete(project_id)
            db.delete_project(project_id)
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {filename}")
        content = await upload_file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            pm.delete(project_id)
            db.delete_project(project_id)
            raise HTTPException(status_code=400, detail="File too large")
        try:
            pm.save_upload(project_id, filename, content)
        except ValueError as exc:
            pm.delete(project_id)
            db.delete_project(project_id)
            raise HTTPException(status_code=400, detail=str(exc))
        db.add_file(project_id, filename, len(content))
        if filename.endswith((".py", ".js")):
            main_file = filename
        else:
            candidates = [f for f in pm.list_files(project_id) if f.endswith(("main.py", "bot.py", "app.py", "index.js", "main.js", "bot.js", "server.js"))]
            main_file = candidates[0] if candidates else None
        if not main_file:
            all_files = pm.list_files(project_id)
            py_files = [f for f in all_files if f.endswith(".py")]
            js_files = [f for f in all_files if f.endswith(".js")]
            if py_files:
                main_file = py_files[0]
            elif js_files:
                main_file = js_files[0]
        pm.install_requirements(project_id)
    if main_file:
        db.update_project(project_id, main_file=main_file)
    db.add_activity("create_project", str(owner_id), project_id, name)
    project = db.get_project(project_id)
    project["runtime_status"] = pm.status(project_id)
    project["runtime"] = runtime
    return project

@app.get("/api/projects/{project_id}")
async def get_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    project["runtime_status"] = pm.status(project_id)
    project["runtime"] = project.get("runtime") or pm.detect_runtime(project_id)
    project["files"] = pm.list_files(project_id)
    project["files_detailed"] = pm.list_files_detailed(project_id)
    project["resource"] = pm.get_resource_info(project_id)
    owner = db.get_telegram_user(project["owner_id"])
    project["owner_username"] = owner["username"] if owner and owner.get("username") else None
    project["owner_display_username"] = f"@{project['owner_username']}" if project["owner_username"] else "@username_unavailable"
    return project

@app.post("/api/projects/{project_id}/start")
async def start_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    if not project["main_file"]:
        files = pm.list_files(project_id)
        candidates = [f for f in files if f.endswith(("main.py", "bot.py", "app.py", "index.js", "main.js"))]
        if candidates:
            project["main_file"] = candidates[0]
            db.update_project(project_id, main_file=candidates[0])
        else:
            raise HTTPException(status_code=400, detail="No entrypoint found. Set main file or upload a valid bot file.")
    _, note = await pm.start(project_id, project["main_file"])
    db.update_project(project_id, status="running")
    db.add_activity("start", str(project["owner_id"]), project_id)
    return {"status": "running", "note": note, "resource": pm.get_resource_info(project_id)}

@app.post("/api/projects/{project_id}/stop")
async def stop_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    await pm.stop(project_id)
    db.update_project(project_id, status="stopped")
    db.add_activity("stop", str(project["owner_id"]), project_id)
    return {"status": "stopped"}

@app.post("/api/projects/{project_id}/restart")
async def restart_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    if not project["main_file"]:
        raise HTTPException(status_code=400, detail="No entrypoint set for this project")
    _, note = await pm.restart(project_id, project["main_file"])
    db.update_project(project_id, status="running")
    db.add_activity("restart", str(project["owner_id"]), project_id)
    return {"status": "running", "note": note, "resource": pm.get_resource_info(project_id)}

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    await pm.stop(project_id)
    db.add_activity("delete", str(project["owner_id"]), project_id, project["name"])
    pm.delete(project_id)
    db.delete_project(project_id)
    return {"ok": True}

@app.get("/api/projects/{project_id}/logs")
async def get_logs(project_id: int, lines: int = 200, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    lines = min(max(lines, 1), 1000)
    return {"lines": pm.tail(project_id, lines), "status": pm.status(project_id)}

@app.delete("/api/projects/{project_id}/logs")
async def clear_logs(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    pm.clear_logs(project_id)
    return {"ok": True}

@app.get("/api/projects/{project_id}/files")
async def list_files(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    files = pm.list_files_detailed(project_id)
    return {"files": files}

@app.post("/api/projects/{project_id}/files")
async def upload_or_create_file(project_id: int, request: Request, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        if "file" not in form:
            raise HTTPException(status_code=400, detail="No file uploaded")
        upload_file = form["file"]
        path = form.get("path") or upload_file.filename
        content = await upload_file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File too large")
        target_path = path or upload_file.filename
        try:
            pm.write_file(project_id, target_path, content.decode(errors="replace") if isinstance(content, bytes) else content)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")
        db.add_file(project_id, target_path, len(content))
        db.add_activity("upload_file", str(project["owner_id"]), project_id, target_path)
        return {"ok": True, "path": target_path, "size": len(content)}
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        path = body.get("path")
        content = body.get("content", "")
        if not path:
            raise HTTPException(status_code=400, detail="Path is required")
        try:
            pm.write_file(project_id, path, content)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")
        db.add_file(project_id, path, len(content.encode()))
        db.add_activity("create_file", str(project["owner_id"]), project_id, path)
        return {"ok": True, "path": path}

@app.get("/api/projects/{project_id}/files/{path:path}")
async def read_file(project_id: int, path: str, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    try:
        content = pm.read_file(project_id, path)
        info = pm.get_file_info(project_id, path)
        ext = info["extension"] if info else ""
        language = "python" if ext == ".py" else "javascript" if ext in [".js", ".jsx", ".ts", ".tsx"] else ext.lstrip(".") or "text"
        return {"path": path, "content": content, "info": info, "language": language}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

@app.put("/api/projects/{project_id}/files/{path:path}")
async def write_file(project_id: int, path: str, body: FileWriteRequest, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    try:
        pm.write_file(project_id, path, body.content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    db.add_activity("edit_file", str(project["owner_id"]), project_id, path)
    return {"ok": True}

@app.post("/api/projects/{project_id}/files/{path:path}/rename")
async def rename_file(project_id: int, path: str, body: RenameRequest, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    if not body.new_path or not body.new_path.strip():
        raise HTTPException(status_code=400, detail="New path required")
    try:
        pm.rename_file(project_id, path, body.new_path.strip())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source file not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.add_activity("rename_file", str(project["owner_id"]), project_id, f"{path} -> {body.new_path}")
    return {"ok": True, "old_path": path, "new_path": body.new_path}

@app.delete("/api/projects/{project_id}/files/{path:path}")
async def delete_file(project_id: int, path: str, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    project = owned_project(db, project_id, bh_session, authorization, x_telegram_init_data)
    try:
        ok = pm.delete_file(project_id, path)
        if not ok:
            raise HTTPException(status_code=404, detail="File not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    db.add_activity("delete_file", str(project["owner_id"]), project_id, path)
    return {"ok": True}

# =============================================================================
# ADMIN ROUTES (Owner only)
# =============================================================================

@app.get("/api/admin/stats")
async def admin_stats(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    stats = db.get_global_stats()
    all_projects = db.list_all_projects()
    running = 0
    for p in all_projects:
        if pm.status(p["id"]) == "running":
            running += 1
    stats["running_projects"] = running
    stats["stopped_projects"] = stats["total_projects"] - running
    return stats

@app.get("/api/admin/users")
async def admin_list_users(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    users = db.list_all_users_with_stats()
    result = []
    for u in users:
        username = u.get("username")
        display_username = f"@{username}" if username else "@username_unavailable"
        result.append({
            "user_id": u["user_id"],
            "username": username,
            "display_username": display_username,
            "photo_url": u.get("photo_url", ""),
            "total_projects": u.get("total_projects", 0),
            "running_projects": u.get("running_projects", 0),
            "last_seen": u.get("last_seen", 0),
        })
    return {"users": result}

@app.get("/api/admin/users/{user_id}/projects")
async def admin_list_user_projects(user_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    projects = db.list_projects_by_user(user_id)
    for p in projects:
        p["runtime_status"] = pm.status(p["id"])
        p["runtime"] = p.get("runtime") or pm.detect_runtime(p["id"])
        p["resource"] = pm.get_resource_info(p["id"])
    tg_user = db.get_telegram_user(user_id)
    username = tg_user["username"] if tg_user and tg_user.get("username") else None
    display_username = f"@{username}" if username else "@username_unavailable"
    return {"user_id": user_id, "username": username, "display_username": display_username, "photo_url": tg_user["photo_url"] if tg_user else "", "projects": projects}

@app.get("/api/admin/projects")
async def admin_list_all_projects(bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    projects = db.list_all_projects()
    for p in projects:
        p["runtime_status"] = pm.status(p["id"])
        p["runtime"] = p.get("runtime") or pm.detect_runtime(p["id"])
        p["resource"] = pm.get_resource_info(p["id"])
        owner = db.get_telegram_user(p["owner_id"])
        p["owner_username"] = owner["username"] if owner and owner.get("username") else None
        p["owner_display_username"] = f"@{p['owner_username']}" if p["owner_username"] else "@username_unavailable"
    return {"projects": projects}

@app.get("/api/admin/projects/{project_id}")
async def admin_get_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project["runtime_status"] = pm.status(project_id)
    project["runtime"] = project.get("runtime") or pm.detect_runtime(project_id)
    project["files"] = pm.list_files(project_id)
    project["files_detailed"] = pm.list_files_detailed(project_id)
    project["resource"] = pm.get_resource_info(project_id)
    owner = db.get_telegram_user(project["owner_id"])
    project["owner_username"] = owner["username"] if owner and owner.get("username") else None
    project["owner_display_username"] = f"@{project['owner_username']}" if project["owner_username"] else "@username_unavailable"
    project["owner_photo_url"] = owner["photo_url"] if owner else ""
    return project

@app.delete("/api/admin/projects/{project_id}")
async def admin_delete_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await pm.stop(project_id)
    db.add_activity("admin_delete", str(session["owner_id"]), project_id, f"owner deleted {project['name']} owned by {project['owner_id']}")
    pm.delete(project_id)
    db.delete_project(project_id)
    return {"ok": True}

@app.post("/api/admin/projects/{project_id}/start")
async def admin_start_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project["main_file"]:
        files = pm.list_files(project_id)
        candidates = [f for f in files if f.endswith(("main.py", "bot.py", "app.py", "index.js", "main.js"))]
        if candidates:
            project["main_file"] = candidates[0]
            db.update_project(project_id, main_file=candidates[0])
        else:
            raise HTTPException(status_code=400, detail="No entrypoint found")
    _, note = await pm.start(project_id, project["main_file"])
    db.update_project(project_id, status="running")
    db.add_activity("admin_start", str(session["owner_id"]), project_id)
    return {"status": "running", "note": note, "resource": pm.get_resource_info(project_id)}

@app.post("/api/admin/projects/{project_id}/stop")
async def admin_stop_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await pm.stop(project_id)
    db.update_project(project_id, status="stopped")
    db.add_activity("admin_stop", str(session["owner_id"]), project_id)
    return {"status": "stopped"}

@app.post("/api/admin/projects/{project_id}/restart")
async def admin_restart_project(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project["main_file"]:
        raise HTTPException(status_code=400, detail="No entrypoint set")
    _, note = await pm.restart(project_id, project["main_file"])
    db.update_project(project_id, status="running")
    db.add_activity("admin_restart", str(session["owner_id"]), project_id)
    return {"status": "running", "note": note, "resource": pm.get_resource_info(project_id)}

@app.get("/api/admin/projects/{project_id}/files")
async def admin_list_files(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    files = pm.list_files_detailed(project_id)
    return {"files": files}

@app.get("/api/admin/projects/{project_id}/files/{path:path}")
async def admin_read_file(project_id: int, path: str, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        content = pm.read_file(project_id, path)
        info = pm.get_file_info(project_id, path)
        ext = info["extension"] if info else ""
        language = "python" if ext == ".py" else "javascript" if ext in [".js", ".jsx", ".ts", ".tsx"] else ext.lstrip(".") or "text"
        return {"path": path, "content": content, "info": info, "language": language}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

@app.put("/api/admin/projects/{project_id}/files/{path:path}")
async def admin_write_file(project_id: int, path: str, body: FileWriteRequest, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        pm.write_file(project_id, path, body.content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    db.add_activity("admin_edit_file", str(session["owner_id"]), project_id, path)
    return {"ok": True}

@app.post("/api/admin/projects/{project_id}/files")
async def admin_create_or_upload_file(project_id: int, request: Request, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        if "file" not in form:
            raise HTTPException(status_code=400, detail="No file uploaded")
        upload_file = form["file"]
        path = form.get("path") or upload_file.filename
        content = await upload_file.read()
        try:
            pm.write_file(project_id, path, content.decode(errors="replace") if isinstance(content, bytes) else content)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")
        db.add_activity("admin_upload_file", str(session["owner_id"]), project_id, path)
        return {"ok": True, "path": path}
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        path = body.get("path")
        content = body.get("content", "")
        if not path:
            raise HTTPException(status_code=400, detail="Path required")
        try:
            pm.write_file(project_id, path, content)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid path")
        db.add_activity("admin_create_file", str(session["owner_id"]), project_id, path)
        return {"ok": True, "path": path}

@app.delete("/api/admin/projects/{project_id}/files/{path:path}")
async def admin_delete_file(project_id: int, path: str, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        ok = pm.delete_file(project_id, path)
        if not ok:
            raise HTTPException(status_code=404, detail="File not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    db.add_activity("admin_delete_file", str(session["owner_id"]), project_id, path)
    return {"ok": True}

@app.post("/api/admin/projects/{project_id}/files/{path:path}/rename")
async def admin_rename_file(project_id: int, path: str, body: RenameRequest, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    session = require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not body.new_path or not body.new_path.strip():
        raise HTTPException(status_code=400, detail="New path required")
    try:
        pm.rename_file(project_id, path, body.new_path.strip())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source file not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.add_activity("admin_rename_file", str(session["owner_id"]), project_id, f"{path} -> {body.new_path}")
    return {"ok": True, "old_path": path, "new_path": body.new_path}

@app.get("/api/admin/projects/{project_id}/logs")
async def admin_get_logs(project_id: int, lines: int = 200, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    lines = min(max(lines, 1), 1000)
    return {"lines": pm.tail(project_id, lines), "status": pm.status(project_id)}

@app.delete("/api/admin/projects/{project_id}/logs")
async def admin_clear_logs(project_id: int, bh_session: str | None = Cookie(default=None), authorization: str | None = Header(default=None), x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")):
    require_owner(bh_session, authorization, x_telegram_init_data)
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    pm.clear_logs(project_id)
    return {"ok": True}

# =============================================================================
# TERMINAL WEBSOCKET
# =============================================================================

@app.websocket("/ws/projects/{project_id}/logs")
async def logs_ws(websocket: WebSocket, project_id: int, initData: Optional[str] = Query(default=None)):
    authenticated = False
    owner_id = None
    is_owner = False
    cookie_value = websocket.cookies.get("bh_session")
    session = verify_session(cookie_value, SECRET_KEY) if cookie_value else None
    if session:
        owner_id = session.get("owner_id")
        is_owner = session.get("is_owner") or is_owner_user(owner_id, OWNER_USER_ID, set(ADMIN_USER_IDS))
        project = db.get_project(project_id)
        if project and (project["owner_id"] == owner_id or is_owner):
            authenticated = True
    if not authenticated and initData and BOT_TOKEN and BOT_TOKEN != "PASTE_BOT_TOKEN_HERE":
        validated = validate_telegram_init_data(initData, BOT_TOKEN)
        if validated:
            owner_id = validated["user_id"]
            is_owner = is_owner_user(owner_id, OWNER_USER_ID, set(ADMIN_USER_IDS))
            project = db.get_project(project_id)
            if project and (project["owner_id"] == owner_id or is_owner):
                authenticated = True
    if not authenticated:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    sent = 0
    try:
        while True:
            lines = pm.tail(project_id, 1000)
            if len(lines) > sent:
                for line in lines[sent:]:
                    await websocket.send_text(line)
                sent = len(lines)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception as e:
        log.warning(f"WS error: {e}")
        try:
            await websocket.close()
        except Exception:
            pass

# =============================================================================
# SYSTEM ROUTES
# =============================================================================

@app.get("/health", tags=["system"])
async def health() -> dict[str, str | bool]:
    return {"status": "ok", "telegram_configured": bool(bot_runner and bot_runner.configured) if bot_runner else False, "version": "2.0.0-miniapp"}

@app.get("/api/health", tags=["system"])
async def api_health():
    return {"status": "ok", "telegram_configured": bool(bot_runner and bot_runner.configured) if bot_runner else False}

@app.get("/api/runtimes", tags=["system"])
async def get_runtimes():
    return {"runtimes": SUPPORTED_RUNTIMES}

# For local dev, optionally serve frontend if present (kept separate for Netlify)
# This does not merge frontend into backend, just serves static files if available
try:
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    frontend_dir = Path(__file__).parent / "frontend"
    if frontend_dir.exists():
        @app.get("/", include_in_schema=False)
        async def serve_frontend_root():
            index_path = frontend_dir / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
            return JSONResponse({"message": "SNUKED HOSTER API - Frontend not built, deploy frontend to Netlify"})
except Exception:
    pass
