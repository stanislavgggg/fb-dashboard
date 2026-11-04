"""Health report: database, scheduler, syncs, profiles, Keitaro, currency rates, alerts.

CLI (inside the container or locally):  python -m app.health        text, exit code 0 ok / 1 warn / 2 fail
                                         python -m app.health --json
HTTP:  GET /api/health  (admin session or X-Health-Token header), ?format=text for a terminal-friendly view
"""
import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text

from . import config, keitaro
from .crypto import KeyChanged, dec
from .db import AdAccount, AlertLog, CampaignDay, KeitaroDay, Profile, Session, SyncRun, get_settings, utcnow
from .fb import ACTIVE_STATUSES

LEVELS = {"ok": 0, "warn": 1, "fail": 2}


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    m = int((utcnow() - dt).total_seconds() // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 48 * 60:
        return f"{m // 60} h {m % 60} min ago"
    return f"{m // 1440} days ago"


def _check(name: str, level: str, text_: str, **details) -> dict:
    return {"name": name, "level": level, "text": text_, **({"details": details} if details else {})}


def _db(s) -> dict:
    t = time.perf_counter()
    s.execute(text("SELECT 1"))
    ms = (time.perf_counter() - t) * 1000
    return _check("Database", "ok" if ms < 500 else "warn", f"responding, {ms:.0f} ms")


def _scheduler(running: bool | None) -> dict:
    if not config.SCHEDULER_ENABLED:
        return _check("Scheduler", "warn", "disabled (SCHEDULER_ENABLED=0) — manual syncs only")
    if running is None:
        return _check("Scheduler", "ok", "can only be checked in the running service (see the sync log below)")
    if not running:
        return _check("Scheduler", "fail", "not running — no scheduled syncs will happen, restart the service")
    return _check("Scheduler", "ok", f"running, every {config.SYNC_MINUTES} min")


def _sync(s) -> dict:
    period = timedelta(minutes=config.SYNC_MINUTES)
    running = s.scalar(select(SyncRun).where(SyncRun.finished_at.is_(None)).order_by(SyncRun.started_at.desc()).limit(1))
    last = s.scalar(select(SyncRun).where(SyncRun.finished_at.is_not(None)).order_by(SyncRun.started_at.desc()).limit(1))
    day_ago = utcnow() - timedelta(days=1)
    total = s.scalar(select(func.count()).select_from(SyncRun).where(SyncRun.started_at >= day_ago)) or 0
    failed = s.scalar(select(func.count()).select_from(SyncRun)
                      .where(SyncRun.started_at >= day_ago, SyncRun.ok.is_(False))) or 0
    stats = {"runs_24h": total, "failed_24h": failed}

    if running and utcnow() - running.started_at > timedelta(minutes=45):
        return _check("Sync", "fail", f"a run has been stuck since {_ago(running.started_at)} — restart the service", **stats)
    if last is None:
        if running:
            return _check("Sync", "ok", "first run in progress", **stats)
        return _check("Sync", "warn", "no completed runs yet", **stats)

    age = utcnow() - last.finished_at
    tail = f"; last 24h: {total} runs, {failed} with errors"
    if age > period * 6 and config.SCHEDULER_ENABLED:
        return _check("Sync", "fail", f"last one finished {_ago(last.finished_at)} — data is not updating{tail}", **stats)
    if age > period * 2 + timedelta(minutes=10) and config.SCHEDULER_ENABLED:
        return _check("Sync", "warn", f"last one finished {_ago(last.finished_at)}, expected sooner{tail}", **stats)
    if last.ok is False:
        errs = [m for m in (last.message or "").split(" | ") if any(w in m.lower() for w in ("error", "stopped working", "0 ad accounts", "not updated", "key changed", "proxy", "network"))]
        return _check("Sync", "warn", f"last one {_ago(last.finished_at)}, with errors{tail}",
                      errors=errs[:10], **stats)
    now = " (the next one is running now)" if running else ""
    return _check("Sync", "ok", f"last one {_ago(last.finished_at)}, no errors{now}{tail}", **stats)


def _profiles(s) -> list[dict]:
    out = []
    profiles = s.scalars(select(Profile).where(Profile.active.is_(True))).all()
    if not profiles:
        return [_check("Profiles", "warn", "no active profiles")]
    key_broken, dead, proxy, stale = [], [], [], []
    stale_after = timedelta(minutes=config.SYNC_MINUTES * 3 + 10)
    for p in profiles:
        try:
            dec(p.token_enc)
            dec(p.proxy_enc)
        except KeyChanged:
            key_broken.append(p.name)
            continue
        if p.token_ok is False:
            dead.append(p.name)
        elif (p.net_fails or 0) >= 2:
            proxy.append(p.name)
        elif p.last_sync_at is None or utcnow() - p.last_sync_at > stale_after:
            stale.append(p.name)
    if key_broken:
        out.append(_check("Encryption key", "fail",
                          f"tokens of {len(key_broken)} profiles were encrypted with a different SECRET_KEY — enter them again",
                          profiles=key_broken))
    bad = len(key_broken) + len(dead) + len(proxy)
    level = "ok" if not (dead or proxy or stale) else ("fail" if bad == len(profiles) else "warn")
    parts = [f"{len(profiles)} active"]
    if dead:
        parts.append(f"dead token: {len(dead)}")
    if proxy:
        parts.append(f"proxy not responding: {len(proxy)}")
    if stale:
        parts.append(f"not updated for a while: {len(stale)}")
    out.append(_check("Profiles", level, ", ".join(parts),
                      **{k: v for k, v in (("dead_token", dead), ("proxy_down", proxy), ("stale", stale)) if v}))
    return out


def _accounts(s) -> dict:
    accs = s.scalars(select(AdAccount)).all()
    if not accs:
        return _check("Ad accounts", "warn", "none yet — they appear after the first profile sync")
    active = [a for a in accs if a.status in ACTIVE_STATUSES and not a.missing]
    missing = [a for a in accs if a.missing]
    errors = [a for a in accs if a.last_error and not a.missing]
    fresh = s.scalar(select(func.max(CampaignDay.updated_at)))
    text_ = f"{len(active)} of {len(accs)} active"
    if missing:
        text_ += f", no access: {len(missing)}"
    if errors:
        text_ += f", with errors: {len(errors)}"
    text_ += f"; FB stats updated {_ago(fresh)}"
    level = "warn" if errors or not active else "ok"
    return _check("Ad accounts", level, text_,
                  **({"errors": [f"{a.name} ({a.id}): {a.last_error[:150]}" for a in errors[:10]]} if errors else {}))


def _keitaro(s, last_msg: str) -> dict:
    if not keitaro.enabled():
        return _check("Keitaro", "warn", "not connected — registrations, deposits and profit are empty")
    fresh = s.scalar(select(func.max(KeitaroDay.updated_at)))
    if "Keitaro: error" in last_msg:
        err = next((m for m in last_msg.split(" | ") if m.startswith("Keitaro")), "")
        return _check("Keitaro", "fail" if fresh is None else "warn",
                      f"last request failed, data from {_ago(fresh)}", error=err[:300])
    if fresh is None:
        return _check("Keitaro", "warn", "no data yet")
    if utcnow() - fresh > timedelta(minutes=config.SYNC_MINUTES * 3 + 10):
        return _check("Keitaro", "warn", f"data is stale: updated {_ago(fresh)}")
    return _check("Keitaro", "ok", f"updated {_ago(fresh)}, {config.KEITARO_DAYS_BACK}-day window")


def _fx(s, st: dict) -> dict:
    fx = {k.upper() for k in (st.get("fx_to_usd") or {})} | {"USD"}
    used = {c for c in s.scalars(select(AdAccount.currency).distinct()).all() if c}
    missing = sorted(used - fx)
    upd = st.get("fx_updated_at")
    age = (date.today() - date.fromisoformat(upd)).days if upd else None
    if missing:
        return _check("Currency rates", "warn", f"no rate for {', '.join(missing)} — that spend is counted as USD")
    if used <= {"USD"}:
        return _check("Currency rates", "ok", "all ad accounts are in USD")
    if age is None:
        return _check("Currency rates", "warn", "rates have never been updated")
    limit = 2 if st.get("fx_auto") else 45
    mode = "auto" if st.get("fx_auto") else "manual"
    if age > limit:
        return _check("Currency rates", "warn", f"updated {age} days ago ({mode})")
    return _check("Currency rates", "ok", f"updated {'today' if age == 0 else f'{age} days ago'} ({mode})")


def _alerts(s, st: dict) -> dict:
    if not st.get("alerts_enabled"):
        return _check("Alerts", "warn", "turned off in settings")
    if not config.TG_BOT_TOKEN:
        return _check("Alerts", "warn", "TG_BOT_TOKEN is not set — alerts are only visible in the dashboard")
    if not st.get("tg_chat_id"):
        return _check("Alerts", "warn", "no chat ID in settings")
    stuck = s.scalar(select(func.count()).select_from(AlertLog)
                     .where(AlertLog.sent.is_(False), AlertLog.created_at < utcnow() - timedelta(hours=1))) or 0
    if stuck:
        return _check("Alerts", "fail", f"{stuck} alerts haven't reached Telegram for over an hour — check the bot and chat ID (Send test button)")
    day = s.scalar(select(func.count()).select_from(AlertLog).where(AlertLog.created_at >= utcnow() - timedelta(days=1))) or 0
    return _check("Alerts", "ok", f"delivered, {day} in the last 24h")


def report(scheduler_running: bool | None = None) -> dict:
    checks = []
    try:
        with Session() as s:
            checks.append(_db(s))
            checks.append(_scheduler(scheduler_running))
            checks.append(_sync(s))
            checks.extend(_profiles(s))
            checks.append(_accounts(s))
            last = s.scalar(select(SyncRun).where(SyncRun.finished_at.is_not(None)).order_by(SyncRun.started_at.desc()).limit(1))
            st = get_settings(s)
            checks.append(_keitaro(s, last.message if last else ""))
            checks.append(_fx(s, st))
            checks.append(_alerts(s, st))
    except Exception as e:  # the report itself must never crash
        checks.append(_check("Database", "fail", f"error: {e}"))
    status = max((c["level"] for c in checks), key=LEVELS.__getitem__, default="ok")
    return {"status": status, "checked_at": utcnow().isoformat() + "Z", "checks": checks}


def as_text(r: dict) -> str:
    mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
    head = {"ok": "all good", "warn": "warnings", "fail": "problems found"}[r["status"]]
    lines = [f"FB dashboard: {r['status'].upper()} — {head}  ({r['checked_at'][:16].replace('T', ' ')} UTC)", ""]
    for c in r["checks"]:
        lines.append(f"{mark[c['level']]} {c['name']}: {c['text']}")
        for k, v in (c.get("details") or {}).items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"         · {item}")
            elif k not in ("runs_24h", "failed_24h"):
                lines.append(f"         · {v}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    from .db import init_db
    ap = argparse.ArgumentParser(description="FB dashboard health check")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args()
    init_db()
    r = report()
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else as_text(r), end="")
    sys.exit(LEVELS[r["status"]])
