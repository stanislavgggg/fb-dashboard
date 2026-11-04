import json
from datetime import UTC, date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, inspect, select, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

_sqlite = config.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _sqlite else {},
)
Session = sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # naive UTC, portable across sqlite/postgres


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin | viewer
    session_version: Mapped[int] = mapped_column(Integer, default=1)  # bump to log the user out everywhere
    buyer: Mapped[str] = mapped_column(String(120), default="")  # viewer sees only profiles of this buyer; "" = all
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Profile(Base):
    """One agency login (token + proxy). Ad accounts visible to it are discovered automatically."""
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    supplier: Mapped[str] = mapped_column(String(120), default="")  # agency / vendor
    buyer: Mapped[str] = mapped_column(String(120), default="")
    token_enc: Mapped[str] = mapped_column(Text)
    proxy_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fb_user: Mapped[str] = mapped_column(String(200), default="")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    net_fails: Mapped[int] = mapped_column(Integer, default=0)  # consecutive proxy/network failures
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdAccount(Base):
    __tablename__ = "ad_accounts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # act_123
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    label: Mapped[str] = mapped_column(String(200), default="")  # our own note
    geo: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    disable_reason: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    timezone: Mapped[str] = mapped_column(String(80), default="")
    amount_spent: Mapped[float] = mapped_column(Float, default=0)  # lifetime, account currency
    spend_cap: Mapped[float] = mapped_column(Float, default=0)
    balance: Mapped[float] = mapped_column(Float, default=0)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)  # not returned by last sync
    last_error: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CampaignDay(Base):
    __tablename__ = "campaign_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(40), index=True)
    campaign_name: Mapped[str] = mapped_column(String(400), default="")
    spend: Mapped[float] = mapped_column(Float, default=0)  # account currency
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    fb_results: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KeitaroDay(Base):
    """Keitaro stats by day + the sub_id carrying the FB campaign ID."""
    __tablename__ = "keitaro_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    sub_value: Mapped[str] = mapped_column(String(200), primary_key=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    regs: Mapped[int] = mapped_column(Integer, default=0)
    deps: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0)  # Keitaro report currency
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(40), default="schedule")
    days: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")


class AlertLog(Base):
    __tablename__ = "alert_log"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)  # dedup key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(20), default="warn")
    text: Mapped[str] = mapped_column(Text, default="")
    sent: Mapped[bool] = mapped_column(Boolean, default=False)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="null")


DEFAULT_SETTINGS = {
    "fx_to_usd": {"USD": 1.0, "EUR": 1.08},
    "keitaro_currency": "USD",
    "alerts_enabled": True,
    "tg_chat_id": "",
    "alert_spend_no_reg_usd": 30.0,  # campaign spent this much today with 0 regs
    "alert_cap_ratio": 0.9,  # account used 90% of its spend cap
    "fx_updated_at": None,  # ISO date when fx_to_usd was last saved
    "fx_auto": True,  # refresh rates daily from Frankfurter (ECB)
    "fx_manual": {},  # manual overrides that win over automatic rates
    "fx_source": "",  # "auto" | "manual"
}


def get_settings(s) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in s.scalars(select(Setting)).all():
        try:
            out[row.key] = json.loads(row.value)
        except ValueError:
            pass
    return out


def set_settings(s, values: dict):
    for k, v in values.items():
        if k in DEFAULT_SETTINGS:
            s.merge(Setting(key=k, value=json.dumps(v)))


def _add_missing_columns():
    """Poor man's migration: add columns that exist in the models but not in the DB.
    create_all never alters existing tables, so without this the first model change breaks prod."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = col.type.compile(engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                dflt = ""
                if default is not None:
                    dflt = f" DEFAULT {int(default) if isinstance(default, bool) else repr(default)}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}{dflt}'))


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()
