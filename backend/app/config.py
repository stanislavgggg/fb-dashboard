import os

from dotenv import load_dotenv

load_dotenv()


def _db_url(raw: str) -> str:
    # Railway gives postgres:// or postgresql:// — SQLAlchemy needs the psycopg driver name
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+psycopg://", 1)
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


DATABASE_URL = _db_url(os.getenv("DATABASE_URL", "sqlite:///./data.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "")  # Fernet key: encrypts tokens/proxies and signs sessions

ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # used only to create the first admin
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"

FB_API_VERSION = os.getenv("FB_API_VERSION", "v24.0")
FB_RESULT_ACTIONS = [a.strip() for a in os.getenv("FB_RESULT_ACTIONS", "lead,complete_registration").split(",") if a.strip()]

KEITARO_URL = os.getenv("KEITARO_URL", "").rstrip("/")
KEITARO_API_KEY = os.getenv("KEITARO_API_KEY", "")
KEITARO_CAMPAIGN_SUB = os.getenv("KEITARO_CAMPAIGN_SUB", "sub_id_1")
KEITARO_TZ = os.getenv("KEITARO_TZ", "Europe/Madrid")
# optional: Keitaro traffic-source ID for Facebook, so other sources' sub_ids don't pollute "unmatched"
KEITARO_TS_ID = os.getenv("KEITARO_TS_ID", "").strip()
# Keitaro attributes leads/sales to the CLICK day, so late deposits land on old days: keep a wide window
KEITARO_DAYS_BACK = int(os.getenv("KEITARO_DAYS_BACK", "45"))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

SYNC_MINUTES = int(os.getenv("SYNC_MINUTES", "30"))
SYNC_DAYS_BACK = int(os.getenv("SYNC_DAYS_BACK", "7"))  # FB backfills spend/conversions up to 72h
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "1") == "1"

STATIC_DIR = os.getenv("STATIC_DIR", os.path.join(os.path.dirname(__file__), "..", "static"))
