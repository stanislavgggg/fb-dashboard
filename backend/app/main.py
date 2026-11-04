import csv
import hashlib
import hmac
import io
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select
from starlette.middleware.sessions import SessionMiddleware

from . import alerts, auth, config, fx as fxmod, health, keitaro, stats
from .crypto import KEY_CHANGED, KeyChanged, dec, enc, mask
from .db import (AdAccount, AlertLog, CampaignDay, Profile, Session, SyncRun, User, get_settings, init_db,
                 set_settings)
from .fb import ACCOUNT_STATUS, ACTIVE_STATUSES, FBClient, FBError, normalize_proxy
from .sync import on_startup, run_in_background

# Railway marks everything on stderr as [err]: send normal logs to stdout so real errors stand out
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
for _name in ("uvicorn", "uvicorn.error"):
    for _h in logging.getLogger(_name).handlers:
        if isinstance(_h, logging.StreamHandler):
            _h.setStream(sys.stdout)
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # "Adding job tentatively" noise on every start
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per FB/Keitaro request drowns the useful lines
log = logging.getLogger("app")
scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    auth.ensure_admin()
    on_startup()  # a run can't survive a restart: close whatever was left unfinished
    if config.SCHEDULER_ENABLED:
        scheduler.add_job(lambda: run_in_background(config.SYNC_DAYS_BACK, "schedule"), "interval",
                          minutes=config.SYNC_MINUTES, id="sync", max_instances=1, coalesce=True)
        scheduler.add_job(alerts.prune, "interval", hours=24, id="prune")
        scheduler.start()
        run_in_background(config.SYNC_DAYS_BACK, "startup")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="FB Accounts Dashboard", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=hashlib.sha256(("session:" + config.SECRET_KEY).encode()).hexdigest(),
    session_cookie="fbd_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)


# ---------------------------------------------------------------- helpers
def _dates(since: date | None, until: date | None) -> tuple[date, date]:
    until = until or date.today()
    since = since or until - timedelta(days=6)
    if since > until:
        since, until = until, since
    if (until - since).days > 366:
        raise HTTPException(400, "Период не больше года")
    return since, until


def _fx(s) -> tuple[dict, float, dict]:
    st = get_settings(s)
    fx = {k.upper(): float(v) for k, v in (st.get("fx_to_usd") or {}).items()}
    fx.setdefault("USD", 1.0)
    k_rate = fx.get(str(st.get("keitaro_currency") or "USD").upper(), 1.0)
    return fx, k_rate, st


def _profile_out(p: Profile, n_accounts: int = 0, n_active: int = 0) -> dict:
    key_changed = False
    try:
        token, proxy = dec(p.token_enc), dec(p.proxy_enc)
    except KeyChanged:  # SECRET_KEY was rotated: the page must still open so the token can be re-entered
        token, proxy, key_changed = None, None, True
    return {
        "id": p.id, "name": p.name, "supplier": p.supplier, "buyer": p.buyer, "notes": p.notes,
        "active": p.active, "token_ok": False if key_changed else p.token_ok, "fb_user": p.fb_user,
        "token_masked": KEY_CHANGED if key_changed else mask(token),
        "proxy_masked": KEY_CHANGED if key_changed else (proxy.split("@")[-1] if proxy else ""),
        "key_changed": key_changed,
        "last_sync_at": p.last_sync_at.isoformat() + "Z" if p.last_sync_at else None,
        "last_error": p.last_error or (KEY_CHANGED if key_changed else ""),
        "accounts": n_accounts, "accounts_active": n_active,
    }


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    login: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    if auth.too_many_attempts(ip):
        raise HTTPException(429, "Слишком много попыток, подожди 10 минут")
    with Session() as s:
        user = s.scalar(select(User).where(User.login == body.login.strip()))
    if not user or not auth.check_password(body.password, user.password_hash):
        auth.note_failure(ip)
        raise HTTPException(401, "Неверный логин или пароль")
    auth.note_success(ip)
    request.session["uid"], request.session["sv"] = user.id, user.session_version
    return {"id": user.id, "login": user.login, "role": user.role, "buyer": user.buyer}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(auth.current_user)):
    return {"id": user.id, "login": user.login, "role": user.role, "buyer": user.buyer}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


@app.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user: User = Depends(auth.current_user)):
    with Session() as s:
        u = s.get(User, user.id)
        if not auth.check_password(body.old_password, u.password_hash):
            raise HTTPException(400, "Старый пароль неверный")
        u.password_hash = auth.hash_password(body.new_password)
        u.session_version = (u.session_version or 1) + 1  # other devices get logged out
        s.commit()
        request.session["sv"] = u.session_version  # this one stays
    return {"ok": True}


# ---------------------------------------------------------------- meta & stats
@app.get("/api/meta")
def meta(user: User = Depends(auth.current_user)):
    with Session() as s:
        last = s.scalar(select(SyncRun).order_by(desc(SyncRun.started_at)).limit(1))
        running = s.scalar(select(SyncRun.id).where(SyncRun.finished_at.is_(None)).limit(1)) is not None
        st = get_settings(s)
        scope = auth.scope_buyer(user)
        return {
            **stats.filter_options(s, scope),
            "scope_buyer": scope,
            "keitaro_enabled": keitaro.enabled(),
            "keitaro_sub": config.KEITARO_CAMPAIGN_SUB,
            "sync_minutes": config.SYNC_MINUTES,
            "sync_running": running,
            "last_sync": {"at": last.started_at.isoformat() + "Z", "ok": last.ok, "message": last.message,
                          "finished": last.finished_at is not None} if last else None,
            "statuses": ACCOUNT_STATUS,
            "fx_currencies": sorted((st.get("fx_to_usd") or {}).keys()),
            "fx_updated_at": st.get("fx_updated_at"),
            "keitaro_days_back": config.KEITARO_DAYS_BACK,
        }


def _stat_filters(supplier: str | None, buyer: str | None, profile_id: int | None, account_id: str | None,
                  geo: str | None = None):
    return {"supplier": supplier or None, "buyer": buyer or None,
            "profile_id": profile_id or None, "account_id": account_id or None, "geo": geo or None}


@app.get("/api/stats")
def get_stats(since: date | None = None, until: date | None = None, group: str = "account",
              supplier: str | None = None, buyer: str | None = None, profile_id: int | None = None,
              account_id: str | None = None, geo: str | None = None, user: User = Depends(auth.current_user)):
    since, until = _dates(since, until)
    scope = auth.scope_buyer(user)
    if scope:
        buyer = scope
    with Session() as s:
        fx, k_rate, _ = _fx(s)
        f = _stat_filters(supplier, buyer, profile_id, account_id, geo)
        if scope:
            f["hide_unmatched"] = True
        data = stats.build(s, since, until, group, fx, k_rate, **f)
        data["daily"] = stats.daily(s, since, until, fx, k_rate, **f)
        data["since"], data["until"] = since.isoformat(), until.isoformat()
        return data


@app.get("/api/stats/export.csv")
def export_csv(since: date | None = None, until: date | None = None, group: str = "campaign",
               supplier: str | None = None, buyer: str | None = None, profile_id: int | None = None,
               account_id: str | None = None, geo: str | None = None, user: User = Depends(auth.current_user)):
    since, until = _dates(since, until)
    scope = auth.scope_buyer(user)
    if scope:
        buyer = scope
    with Session() as s:
        fx, k_rate, _ = _fx(s)
        data = stats.build(s, since, until, group, fx, k_rate,
                           **_stat_filters(supplier, buyer, profile_id, account_id, geo))
    cols = ["key", "label", "spend", "impressions", "clicks", "ctr", "cpc", "cpm", "fb_results", "k_clicks",
            "regs", "deps", "cpr", "cpd", "reg2dep", "revenue", "profit", "roi"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(data["rows"])
    w.writerow({"key": "TOTAL", **data["total"]})
    name = f"fb_{group}_{since}_{until}.csv"
    # "sep=," makes Excel split columns correctly in locales where ; is the default separator
    return StreamingResponse(iter(["﻿" + "sep=,\n" + buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- accounts
@app.get("/api/accounts")
def list_accounts(user: User = Depends(auth.current_user)):
    with Session() as s:
        fx, _, _ = _fx(s)
        # "today" in each account's own time zone (a US account's day starts at 09:00 Madrid)
        spend_by_day = {}
        for acc_id, day, spend in s.execute(
                select(CampaignDay.account_id, CampaignDay.day, CampaignDay.spend)
                .where(CampaignDay.day >= date.today() - timedelta(days=1), CampaignDay.day <= date.today() + timedelta(days=1))).all():
            spend_by_day[(acc_id, day)] = spend_by_day.get((acc_id, day), 0) + spend
        q = select(AdAccount, Profile).join(Profile, Profile.id == AdAccount.profile_id)
        scope = auth.scope_buyer(user)
        if scope:
            q = q.where(Profile.buyer == scope)
        rows = s.execute(q.order_by(AdAccount.missing, Profile.supplier, AdAccount.name)).all()
        out = []
        for a, p in rows:
            rate = fx.get(a.currency, 1.0)
            acc_today = alerts.account_today(a.timezone)
            out.append({
                "id": a.id, "name": a.name, "label": a.label, "geo": a.geo,
                "status": a.status, "status_text": ACCOUNT_STATUS.get(a.status, str(a.status)),
                "active": a.status in ACTIVE_STATUSES and not a.missing, "missing": a.missing,
                "disable_reason": a.disable_reason, "currency": a.currency, "timezone": a.timezone,
                "amount_spent": round(a.amount_spent, 2), "spend_cap": round(a.spend_cap, 2),
                "balance": round(a.balance, 2),
                "cap_used": round(a.amount_spent / a.spend_cap * 100, 1) if a.spend_cap else None,
                "spend_today_usd": round(spend_by_day.get((a.id, acc_today), 0) * rate, 2),
                "today": acc_today.isoformat(),
                "profile_id": p.id, "profile": p.name, "supplier": p.supplier, "buyer": p.buyer,
                "last_error": a.last_error, "updated_at": a.updated_at.isoformat() + "Z",
            })
        return out


class AccountPatch(BaseModel):
    label: str | None = None
    geo: str | None = None


@app.patch("/api/accounts/{acc_id}")
def patch_account(acc_id: str, body: AccountPatch, user: User = Depends(auth.admin_user)):
    with Session() as s:
        a = s.get(AdAccount, acc_id)
        if not a:
            raise HTTPException(404, "Кабинет не найден")
        if body.label is not None:
            a.label = body.label.strip()[:200]
        if body.geo is not None:
            a.geo = body.geo.strip().upper()[:40]
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- profiles
class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    supplier: str = ""
    buyer: str = ""
    token: str | None = None  # None on update = keep
    proxy: str | None = None  # None on update = keep, "" = remove
    notes: str = ""
    active: bool = True


def _clean_token(t: str) -> str:
    t = t.strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if len(t) < 20 or " " in t:
        raise HTTPException(400, "Токен выглядит неправильно")
    return t


def _clean_proxy(p: str | None) -> str | None:
    try:
        return normalize_proxy(p)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/profiles")
def list_profiles(user: User = Depends(auth.current_user)):
    with Session() as s:
        counts, active = {}, {}
        for pid, status, missing in s.execute(select(AdAccount.profile_id, AdAccount.status, AdAccount.missing)):
            counts[pid] = counts.get(pid, 0) + 1
            if status in ACTIVE_STATUSES and not missing:
                active[pid] = active.get(pid, 0) + 1
        q = select(Profile).order_by(Profile.supplier, Profile.name)
        scope = auth.scope_buyer(user)
        if scope:
            q = q.where(Profile.buyer == scope)
        return [_profile_out(p, counts.get(p.id, 0), active.get(p.id, 0)) for p in s.scalars(q).all()]


@app.post("/api/profiles")
def create_profile(body: ProfileIn, user: User = Depends(auth.admin_user)):
    if not body.token:
        raise HTTPException(400, "Нужен токен")
    token, proxy = _clean_token(body.token), _clean_proxy(body.proxy)
    with Session() as s:
        p = Profile(name=body.name.strip(), supplier=body.supplier.strip(), buyer=body.buyer.strip(),
                    notes=body.notes, active=body.active, token_enc=enc(token), proxy_enc=enc(proxy))
        s.add(p)
        s.commit()
        pid = p.id
    if body.active:
        run_in_background(30, "new_profile", [pid])
    return {"id": pid}


@app.patch("/api/profiles/{pid}")
def update_profile(pid: int, body: ProfileIn, user: User = Depends(auth.admin_user)):
    with Session() as s:
        p = s.get(Profile, pid)
        if not p:
            raise HTTPException(404, "Профиль не найден")
        p.name, p.supplier, p.buyer = body.name.strip(), body.supplier.strip(), body.buyer.strip()
        p.notes, p.active = body.notes, body.active
        changed = False
        if body.token:
            p.token_enc, p.token_ok, changed = enc(_clean_token(body.token)), None, True
        if body.proxy is not None:
            p.proxy_enc, changed = enc(_clean_proxy(body.proxy)), True
        if changed:
            p.last_error, p.net_fails = "", 0
        s.commit()
    if changed and body.active:
        run_in_background(7, "manual_profile", [pid])  # new credentials: verify and refresh right away
    return {"ok": True, "sync_started": changed and body.active}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int, user: User = Depends(auth.admin_user)):
    with Session() as s:
        p = s.get(Profile, pid)
        if not p:
            raise HTTPException(404, "Профиль не найден")
        acc_ids = s.scalars(select(AdAccount.id).where(AdAccount.profile_id == pid)).all()
        if acc_ids:
            s.execute(delete(CampaignDay).where(CampaignDay.account_id.in_(acc_ids)))
            s.execute(delete(AdAccount).where(AdAccount.id.in_(acc_ids)))
        s.delete(p)
        s.commit()
    return {"ok": True}


class CheckIn(BaseModel):
    token: str | None = None
    proxy: str | None = None
    profile_id: int | None = None  # check a saved profile


@app.post("/api/profiles/check")
def check_profile(body: CheckIn, user: User = Depends(auth.admin_user)):
    if body.profile_id:
        with Session() as s:
            p = s.get(Profile, body.profile_id)
            if not p:
                raise HTTPException(404, "Профиль не найден")
            try:
                token = _clean_token(body.token) if body.token else dec(p.token_enc)
                proxy = _clean_proxy(body.proxy) if body.proxy is not None else dec(p.proxy_enc)
            except KeyChanged:
                return {"ok": False, "error": KEY_CHANGED}
    else:
        if not body.token:
            raise HTTPException(400, "Нужен токен")
        token, proxy = _clean_token(body.token), _clean_proxy(body.proxy)
    client = FBClient(token, proxy, timeout=15, retries=0)  # a check must answer fast, not hang for minutes
    try:
        me_ = client.me()
        accs = list(client.ad_accounts())
    except FBError as e:
        return {"ok": False, "error": str(e), "code": e.code}
    except Exception as e:
        return {"ok": False, "error": f"Прокси или сеть: {e}"}
    finally:
        client.close()
    return {"ok": True, "user": me_.get("name"), "accounts": [
        {"id": a["id"], "name": a.get("name"), "status": ACCOUNT_STATUS.get(int(a.get("account_status") or 0)),
         "currency": a.get("currency")} for a in accs[:100]], "total": len(accs)}


@app.post("/api/profiles/{pid}/sync")
def sync_one(pid: int, days: int = 7, user: User = Depends(auth.admin_user)):
    if not run_in_background(days, "manual_profile", [pid]):
        raise HTTPException(409, "Синхронизация уже идёт")
    return {"started": True}


# ---------------------------------------------------------------- sync & alerts
class SyncIn(BaseModel):
    days: int = Field(3, ge=1, le=90)


@app.post("/api/sync")
def sync_now(body: SyncIn, user: User = Depends(auth.admin_user)):
    if not run_in_background(body.days, "manual"):
        raise HTTPException(409, "Синхронизация уже идёт")
    return {"started": True}


@app.get("/api/sync/runs")
def sync_runs(limit: int = Query(50, le=200), user: User = Depends(auth.current_user)):
    with Session() as s:
        scoped = auth.scope_buyer(user) is not None
        return [{"id": r.id, "trigger": r.trigger, "days": r.days, "ok": r.ok,
                 "message": "" if scoped else r.message,
                 "started_at": r.started_at.isoformat() + "Z",
                 "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None}
                for r in s.scalars(select(SyncRun).order_by(desc(SyncRun.started_at)).limit(limit))]


@app.get("/api/alerts")
def list_alerts(limit: int = Query(100, le=500), user: User = Depends(auth.current_user)):
    with Session() as s:
        rows = s.scalars(select(AlertLog).order_by(desc(AlertLog.created_at)).limit(limit)).all()
        scope = auth.scope_buyer(user)
        if scope:
            rows = [a for a in rows if _alert_in_scope(s, a.key, scope)]
        return [{"key": a.key, "level": a.level, "text": a.text, "sent": a.sent,
                 "created_at": a.created_at.isoformat() + "Z"} for a in rows]


def _alert_in_scope(s, key: str, buyer: str) -> bool:
    """Alert keys carry a profile id, an account id or a campaign id — map it back to the buyer."""
    kind, _, rest = key.partition(":")
    ident = rest.split(":")[0]
    if kind in ("token", "net", "acc_gone"):
        p = s.get(Profile, int(ident)) if ident.isdigit() else None
        return p is not None and p.buyer == buyer
    if kind in ("acc_down", "acc_up", "cap"):
        a = s.get(AdAccount, ident)
        return a is not None and (p := s.get(Profile, a.profile_id)) is not None and p.buyer == buyer
    if kind == "noreg":
        acc_id = s.scalar(select(CampaignDay.account_id).where(CampaignDay.campaign_id == ident).limit(1))
        a = s.get(AdAccount, acc_id) if acc_id else None
        return a is not None and (p := s.get(Profile, a.profile_id)) is not None and p.buyer == buyer
    return False


# ---------------------------------------------------------------- settings & users
class SettingsIn(BaseModel):
    fx_to_usd: dict[str, float] | None = None
    fx_auto: bool | None = None
    fx_manual: dict[str, float] | None = None
    keitaro_currency: str | None = None
    alerts_enabled: bool | None = None
    tg_chat_id: str | None = None
    alert_spend_no_reg_usd: float | None = Field(None, ge=0)
    alert_cap_ratio: float | None = Field(None, ge=0, le=1)


@app.get("/api/settings")
def read_settings(user: User = Depends(auth.admin_user)):
    with Session() as s:
        st = get_settings(s)
    st["tg_bot_configured"] = bool(config.TG_BOT_TOKEN)
    return st


@app.put("/api/settings")
def write_settings(body: SettingsIn, user: User = Depends(auth.admin_user)):
    values = body.model_dump(exclude_none=True)
    if "fx_manual" in values:
        values["fx_manual"] = {k.strip().upper(): v for k, v in values["fx_manual"].items() if k.strip() and v > 0}
    if "fx_to_usd" in values:
        fx = {k.strip().upper(): v for k, v in values["fx_to_usd"].items() if k.strip() and v > 0}
        fx["USD"] = 1.0
        values["fx_to_usd"] = fx
        values["fx_updated_at"] = date.today().isoformat()
        values["fx_source"] = "manual"
    if "keitaro_currency" in values:
        values["keitaro_currency"] = values["keitaro_currency"].strip().upper() or "USD"
    with Session() as s:
        set_settings(s, values)
        s.commit()
    if values.get("fx_auto") or "fx_manual" in values:
        fxmod.refresh(force=True)  # apply overrides / pull rates right away, not tomorrow
    return {"ok": True}


@app.post("/api/settings/fx-refresh")
def fx_refresh(user: User = Depends(auth.admin_user)):
    ok, msg = fxmod.refresh(force=True)
    if not ok:
        raise HTTPException(400, msg)
    with Session() as s:
        st = get_settings(s)
    return {"ok": True, "message": msg, "fx_to_usd": st["fx_to_usd"], "fx_updated_at": st["fx_updated_at"]}


class TgTestIn(BaseModel):
    tg_chat_id: str | None = None  # the value typed in the form, even if not saved yet


@app.post("/api/settings/test-telegram")
def test_telegram(body: TgTestIn | None = None, user: User = Depends(auth.admin_user)):
    chat = (body.tg_chat_id or "").strip() if body else ""
    if not chat:
        with Session() as s:
            chat = str(get_settings(s).get("tg_chat_id") or "")
    ok, err = alerts.send_telegram(chat, "✅ Тест: алерты FB-дашборда работают")
    if not ok:
        raise HTTPException(400, f"Не отправилось: {err}")
    return {"ok": True}


class UserIn(BaseModel):
    login: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8)
    role: str = "viewer"
    buyer: str = ""  # viewer restricted to this buyer's profiles; empty = sees everything


class UserPatch(BaseModel):
    password: str | None = Field(None, min_length=8)
    role: str | None = None
    buyer: str | None = None


@app.get("/api/users")
def list_users(user: User = Depends(auth.admin_user)):
    with Session() as s:
        return [{"id": u.id, "login": u.login, "role": u.role, "buyer": u.buyer or ""}
                for u in s.scalars(select(User).order_by(User.id))]


@app.post("/api/users")
def create_user(body: UserIn, user: User = Depends(auth.admin_user)):
    if body.role not in ("admin", "viewer"):
        raise HTTPException(400, "Роль: admin или viewer")
    with Session() as s:
        if s.scalar(select(User.id).where(User.login == body.login.strip())):
            raise HTTPException(400, "Такой логин уже есть")
        s.add(User(login=body.login.strip(), password_hash=auth.hash_password(body.password), role=body.role,
                   buyer=body.buyer.strip()))
        s.commit()
    return {"ok": True}


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, user: User = Depends(auth.admin_user)):
    with Session() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(404, "Пользователь не найден")
        if body.role:
            if body.role not in ("admin", "viewer"):
                raise HTTPException(400, "Роль: admin или viewer")
            if u.id == user.id and body.role != "admin":
                raise HTTPException(400, "Нельзя снять админа с самого себя")
            u.role = body.role
        if body.password:
            u.password_hash = auth.hash_password(body.password)
            u.session_version = (u.session_version or 1) + 1  # kick their open sessions
        if body.buyer is not None:
            u.buyer = body.buyer.strip()
        s.commit()
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user: User = Depends(auth.admin_user)):
    if uid == user.id:
        raise HTTPException(400, "Нельзя удалить самого себя")
    with Session() as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()
    return {"ok": True}


@app.get("/healthz")
def healthz():
    # Railway's deploy healthcheck: only "the process is up". Sync problems must not fail a deploy
    return {"ok": True}


def _health_allowed(request: Request) -> bool:
    token = request.headers.get("x-health-token") or request.query_params.get("token") or ""
    if config.HEALTH_TOKEN and token and hmac.compare_digest(token, config.HEALTH_TOKEN):
        return True
    try:
        return auth.current_user(request).role == "admin"
    except HTTPException:
        return False


@app.get("/api/health")
def health_report(request: Request, format: str = "json"):
    """Full health report. 200 when ok/warn, 503 when something is broken — so uptime monitors can alert on it."""
    if not _health_allowed(request):
        raise HTTPException(401, "Нужен админ или заголовок X-Health-Token")
    r = health.report(scheduler_running=scheduler.running if config.SCHEDULER_ENABLED else None)
    code = 503 if r["status"] == "fail" else 200
    if format == "text":
        return PlainTextResponse(health.as_text(r), status_code=code)
    return JSONResponse(r, status_code=code)


# ---------------------------------------------------------------- frontend (SPA)
_static = os.path.abspath(config.STATIC_DIR)
if os.path.isdir(os.path.join(_static, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(_static, "assets")), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = os.path.abspath(os.path.join(_static, path))
    if path and candidate.startswith(_static + os.sep) and os.path.isfile(candidate):
        return FileResponse(candidate)
    index = os.path.join(_static, "index.html")
    if os.path.isfile(index):
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Фронтенд не собран (frontend → npm run build)"}, status_code=404)
