"""
Django settings for the College Attendance System.

All secrets and environment-specific values come from environment variables
(or a local `.env` file). See `.env.example` and the README section
"Production settings" before deploying.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------
# Core / security
# --------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", True)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Safe ONLY for local development. Never used when DJANGO_DEBUG=false.
        SECRET_KEY = "django-insecure-local-development-only-key-0123456789-abcdefghijklmnop"
    else:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

BEHIND_PROXY = env_bool("DJANGO_BEHIND_PROXY", False)
if BEHIND_PROXY:
    # Running behind nginx / a load balancer that terminates HTTPS and sets X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Browser protections that are safe everywhere (development too).
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    # Production hardening (requires HTTPS in front of Django).
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    # project apps
    "core",
    "accounts",
    "departments",
    "academics",
    "students",
    "attendance",
    "reports",
    "staffattendance",
    "notes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.network_lock.ApiNetworkLockMiddleware",  # optional: only listed networks may use /api/
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --------------------------------------------------------------------------
# Database: SQLite file (db.sqlite3). No external database server needed.
# Everyone who logs in reads and writes the same file, so all users see the same data.
# WAL mode lets many people read while one writes; IMMEDIATE + a 20 s timeout make
# simultaneous writers wait for each other instead of failing with "database is locked".
# For very large colleges, switch ENGINE to PostgreSQL (see README, section 5).
# --------------------------------------------------------------------------
# DB_ENGINE=sqlite (default) uses the file db.sqlite3. DB_ENGINE=postgres uses PostgreSQL,
# with DB_NAME, DB_USER, DB_PASSWORD, DB_HOST and DB_PORT read from the environment / .env.
# No application code changes between the two. Run `python manage.py migrate` after switching.
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").strip().lower()
if DB_ENGINE in {"postgres", "postgresql"}:
    if not os.getenv("DB_PASSWORD"):
        raise ImproperlyConfigured("Set DB_PASSWORD (and DB_NAME, DB_USER, DB_HOST) when DB_ENGINE=postgres.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "attendance"),
            "USER": os.getenv("DB_USER", "attendance"),
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        }
    }
elif DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": 20,
                "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }
else:
    raise ImproperlyConfigured("DB_ENGINE must be 'sqlite' or 'postgres'.")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Files teachers upload for students (notes, PDFs). Not served directly; only through the notes views.
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
NOTE_MAX_MB = int(os.getenv("NOTE_MAX_MB", "10"))

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

# Django's default PBKDF2 hasher is used. Passwords are never stored in plain text.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Request counters (login and public-page limits) must be SHARED by all server workers: with the default per-process
# memory each of 4 workers would count on its own and the real limit would be 4 times higher. A folder is shared and
# needs no extra server. (The test run uses memory, below, so tests stay isolated.)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(Path(os.getenv("CACHE_DIR", BASE_DIR / "cache"))),
        "TIMEOUT": 3600,
    }
}

# Only while running `manage.py test`: use a fast hasher so the suite runs in seconds.
# Real runs keep Django's default (PBKDF2) hasher.
if len(sys.argv) > 1 and sys.argv[1] == "test":
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --------------------------------------------------------------------------
# Django REST Framework + JWT
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["accounts.authentication.ActiveDepartmentJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "core.errors.api_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardPagination",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    # "login" is per username, "login_ip" per IP address (see accounts/throttles.py).
    "DEFAULT_THROTTLE_RATES": {
        "login": os.getenv("LOGIN_THROTTLE_RATE", "10/min"),
        "login_ip": os.getenv("LOGIN_IP_THROTTLE_RATE", "300/min"),
        "public": os.getenv("PUBLIC_THROTTLE_RATE", "1500/min"),
    },
    # Set to the number of reverse proxies in front of Django (e.g. 1 for nginx) so the real client IP is used.
    "NUM_PROXIES": int(os.environ["DRF_NUM_PROXIES"]) if os.getenv("DRF_NUM_PROXIES") else None,
}

# Teachers may add an extra lecture for a date at most this many days in the past.
ATTENDANCE_BACKDATE_DAYS = int(os.getenv("ATTENDANCE_BACKDATE_DAYS", "30"))

# A student is "low attendance" below this percentage (checked over a rolling window on the dashboard).
LOW_ATTENDANCE_THRESHOLD = int(os.getenv("LOW_ATTENDANCE_THRESHOLD", "75"))
LOW_ATTENDANCE_DAYS = int(os.getenv("LOW_ATTENDANCE_DAYS", "30"))
LOW_ATTENDANCE_MIN_LECTURES = int(os.getenv("LOW_ATTENDANCE_MIN_LECTURES", "3"))

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.getenv("JWT_ACCESS_MINUTES", "30"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_REFRESH_DAYS", "1"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# --------------------------------------------------------------------------
# CORS (React dev server)
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
)
# Optional network lock: comma separated networks (CIDR) that may call /api/. Empty = no network lock.
# Invalid entries stop the server at start, so a typo cannot silently open or close the API.
import ipaddress  # noqa: E402
API_ALLOWED_NETWORKS = [ipaddress.ip_network(n, strict=False) for n in env_list("API_ALLOWED_NETWORKS")]
CORS_EXPOSE_HEADERS = ["Content-Disposition"]

# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Teachers checking in after this local time are recorded as Late (staff attendance).
STAFF_LATE_AFTER = os.getenv("STAFF_LATE_AFTER", "10:00")


# --------------------------------------------------------------------------
# Logging: technical details go to log files on the server, never to the user.
# logs/server.log: errors and warnings with full tracebacks; logs/security.log: failed sign-ins.
# --------------------------------------------------------------------------
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
try:
    _file_logging = not (len(sys.argv) > 1 and sys.argv[1] == "test")  # test runs do not write into the real log files
    if _file_logging:
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
except OSError:  # read-only disk: fall back to console only
    _file_logging = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        **({
            "server_file": {"class": "logging.handlers.RotatingFileHandler", "filename": str(LOG_DIR / "server.log"),
                            "maxBytes": 5_000_000, "backupCount": 5, "formatter": "plain", "level": "WARNING", "encoding": "utf-8"},
            "security_file": {"class": "logging.handlers.RotatingFileHandler", "filename": str(LOG_DIR / "security.log"),
                              "maxBytes": 5_000_000, "backupCount": 5, "formatter": "plain", "encoding": "utf-8"},
        } if _file_logging else {}),
    },
    "loggers": {
        "django": {"handlers": ["console"] + (["server_file"] if _file_logging else []), "level": "WARNING", "propagate": False},
        "app.errors": {"handlers": ["console"] + (["server_file"] if _file_logging else []), "level": "WARNING", "propagate": False},
        "app.security": {"handlers": ["console"] + (["security_file"] if _file_logging else []), "level": "INFO", "propagate": False},
    },
}

# Encrypted database backups (python manage.py backup_db). Generate the key once:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
BACKUP_ENCRYPTION_KEY = os.getenv("BACKUP_ENCRYPTION_KEY", "")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", BASE_DIR / "backups"))

# Printed at the top of every PDF report.
COLLEGE_NAME = os.getenv("COLLEGE_NAME", "Modern College of Arts, Science & Commerce")
COLLEGE_ADDRESS = os.getenv("COLLEGE_ADDRESS", "Ganeshkhind Road, Pune")
