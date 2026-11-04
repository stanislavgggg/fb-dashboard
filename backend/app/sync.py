"""Pull fresh stats from every active profile and from Keitaro.

CLI backfill:  python -m app.sync --days 30
"""
import argparse
import logging
import threading
import time
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, select, update

from . import alerts, config, fx, keitaro
from .crypto import KeyChanged, dec
from .db import (AdAccount, CampaignDay, KeitaroDay, Profile, Session, SyncRun, get_settings, init_db,
                 utcnow)
from .fb import FBClient, FBError, count_results, money

log = logging.getLogger("sync")
_lock = threading.Lock()
STALE_RUN = timedelta(minutes=45)
_pause = time.sleep  # patched in tests


def _date_range(days: int) -> tuple[date, date]:
    days = max(1, min(int(days), 90))  # Insights with time_increment=1 gets slow past ~90 days
    until = date.today()
    return until - timedelta(days=days - 1), until


def _network_failure(s, p: Profile, err: str):
    p.net_fails = (p.net_fails or 0) + 1
    if p.net_fails >= alerts.NET_FAILS_TO_ALERT:
        alerts.network_failing(s, p, err)


def sync_profile(profile_id: int, since: date, until: date) -> tuple[bool, str]:
    with Session() as s:
        p = s.get(Profile, profile_id)
        if p is None:
            return False, f"profile {profile_id} not found"
        try:
            client = FBClient(dec(p.token_enc), dec(p.proxy_enc))
        except KeyChanged as e:
            p.last_error, p.last_sync_at, p.token_ok = str(e), utcnow(), False
            s.commit()
            return False, f"{p.name}: {e}"
        except Exception as e:
            p.last_error, p.last_sync_at = f"proxy/token: {e}", utcnow()
            s.commit()
            return False, f"{p.name}: {e}"
        try:
            try:
                me = client.me()
                accounts = list(client.ad_accounts())
            except FBError as e:
                if e.token_invalid:
                    if p.token_ok is not False:
                        alerts.token_died(s, p, str(e))
                    p.token_ok = False
                elif e.network:
                    _network_failure(s, p, str(e))
                p.last_error, p.last_sync_at = f"[{e.code}] {e}" if e.code else str(e), utcnow()
                s.commit()
                return False, f"{p.name}: {e}"

            p.fb_user, p.token_ok, p.net_fails = me.get("name", ""), True, 0
            known_before = s.scalars(select(AdAccount.id).where(AdAccount.profile_id == p.id,
                                                                AdAccount.missing.is_(False))).all()
            if not accounts and known_before:
                # a transient empty answer must not flip every account to "no access"
                p.last_error, p.last_sync_at = "Facebook returned 0 ad accounts (had %d before) — skipped" % len(known_before), utcnow()
                s.commit()
                return False, f"{p.name}: 0 ad accounts from FB, kept as is"

            seen, errors, rows, skipped = set(), [], 0, 0
            for a in accounts:
                acc = s.get(AdAccount, a["id"])
                old_status = acc.status if acc else None
                if acc is None:
                    acc = AdAccount(id=a["id"], first_seen=utcnow())
                    s.add(acc)
                elif acc.profile_id != p.id:
                    # the same account is shared with another profile: keep the first active owner,
                    # otherwise rows would flip between profiles every sync
                    owner = s.get(Profile, acc.profile_id)
                    if owner is not None and owner.active and not acc.missing:
                        skipped += 1
                        continue
                acc.profile_id = p.id
                acc.name = a.get("name", "") or a["id"]
                acc.status = int(a.get("account_status") or 0)
                acc.disable_reason = int(a.get("disable_reason") or 0)
                acc.currency = (a.get("currency") or "USD").upper()
                acc.timezone = a.get("timezone_name", "") or ""
                acc.amount_spent = money(a.get("amount_spent"))
                acc.spend_cap = money(a.get("spend_cap"))
                acc.balance = money(a.get("balance"))
                acc.missing = False
                acc.updated_at = utcnow()
                seen.add(acc.id)
                alerts.account_status_changed(s, acc, old_status, p)
                try:
                    agg = defaultdict(lambda: {"spend": 0.0, "imp": 0, "clk": 0, "res": 0, "name": ""})
                    for row in client.campaign_insights(acc.id, since, until):
                        if not row.get("campaign_id"):
                            continue
                        k = (date.fromisoformat(str(row["date_start"])[:10]), row["campaign_id"])
                        agg[k]["spend"] += float(row.get("spend") or 0)
                        agg[k]["imp"] += int(row.get("impressions") or 0)
                        agg[k]["clk"] += int(row.get("clicks") or 0)
                        agg[k]["res"] += count_results(row.get("actions"))
                        agg[k]["name"] = row.get("campaign_name", "")
                    # replace the window atomically for this account
                    s.execute(delete(CampaignDay).where(
                        CampaignDay.account_id == acc.id, CampaignDay.day >= since, CampaignDay.day <= until))
                    s.flush()
                    for (day, cid), v in agg.items():
                        s.merge(CampaignDay(day=day, campaign_id=cid, account_id=acc.id,
                                            campaign_name=v["name"][:400], spend=v["spend"],
                                            impressions=v["imp"], clicks=v["clk"], fb_results=v["res"],
                                            updated_at=utcnow()))
                        rows += 1
                    acc.last_error = ""
                except FBError as e:
                    acc.last_error = f"[{e.code}] {e}"
                    errors.append(f"{acc.name}: {e}")
                except Exception as e:  # unexpected payload etc. — one account must not kill the profile
                    # (parsing happens before the delete, so nothing destructive is pending here)
                    log.exception("account %s", acc.id)
                    acc.last_error = f"processing error: {e}"
                    errors.append(f"{acc.name}: {e}")
                s.commit()
                _pause(0.3)  # be gentle with the API

            # accounts that disappeared from this profile (unshared by the agency, etc.)
            gone_q = select(AdAccount).where(AdAccount.profile_id == p.id, AdAccount.missing.is_(False))
            if seen:
                gone_q = gone_q.where(AdAccount.id.not_in(seen))
            gone = s.scalars(gone_q).all()
            if gone:
                alerts.accounts_unshared(s, p, gone)
                s.execute(update(AdAccount).where(AdAccount.id.in_([g.id for g in gone])).values(missing=True))
            p.last_error = "; ".join(errors)[:2000]
            p.last_sync_at = utcnow()
            s.commit()
            msg = f"{p.name}: {len(accounts) - skipped} accounts, {rows} rows"
            if skipped:
                msg += f", {skipped} shared with another profile"
            return not errors, msg + (f", errors: {len(errors)}" if errors else "")
        finally:
            client.close()


def sync_keitaro(since: date, until: date) -> tuple[bool, str]:
    if not keitaro.enabled():
        return True, "Keitaro not configured"
    rows = keitaro.report(since, until)
    merged = defaultdict(lambda: {"clicks": 0, "regs": 0, "deps": 0, "revenue": 0.0})
    for r in rows:
        m = merged[(r["day"], r["sub_value"])]
        for k in ("clicks", "regs", "deps", "revenue"):
            m[k] += r[k]
    with Session() as s:
        s.execute(delete(KeitaroDay).where(KeitaroDay.day >= since, KeitaroDay.day <= until))
        s.flush()
        for (day, sub), v in merged.items():
            s.add(KeitaroDay(day=day, sub_value=sub, updated_at=utcnow(), **v))
        s.commit()
    return True, f"Keitaro: {len(merged)} rows over {(until - since).days + 1} days"


def close_unfinished_runs(s, only_stale: bool = True) -> bool:
    """Close runs that will never finish. On process start nothing can be running, so close all.
    Returns True if a fresh run is still (presumably) active in this process."""
    fresh = False
    for r in s.scalars(select(SyncRun).where(SyncRun.finished_at.is_(None))).all():
        if not only_stale or utcnow() - r.started_at > STALE_RUN:
            r.finished_at, r.ok, r.message = utcnow(), False, (r.message + " | " if r.message else "") + "interrupted (service restart)"
        else:
            fresh = True
    s.commit()
    return fresh


def on_startup():
    with Session() as s:
        close_unfinished_runs(s, only_stale=False)


def _set_progress(run_id: int, text: str):
    with Session() as s:
        run = s.get(SyncRun, run_id)
        if run:
            run.message = text[:5000]
            s.commit()


def sync_all(days: int, trigger: str = "schedule", profile_ids: list[int] | None = None) -> str:
    if not _lock.acquire(blocking=False):
        return "Sync is already running"
    try:
        with Session() as s:
            if close_unfinished_runs(s, only_stale=True):
                return "Sync is already running"
            run = SyncRun(trigger=trigger, days=days, started_at=utcnow())
            s.add(run)
            s.commit()
            run_id = run.id
            q = select(Profile.id).where(Profile.active.is_(True))
            if profile_ids:
                q = select(Profile.id).where(Profile.id.in_(profile_ids))
            ids = s.scalars(q).all()

        since, until = _date_range(days)
        msgs, all_ok = [], True
        for i, pid in enumerate(ids, 1):
            _set_progress(run_id, f"profile {i} of {len(ids)}… | " + " | ".join(msgs))
            try:
                ok, m = sync_profile(pid, since, until)
            except Exception as e:  # never let one profile kill the run
                log.exception("profile %s", pid)
                ok, m = False, f"profile {pid}: {e}"
            all_ok &= ok
            msgs.append(m)
        # Keitaro is one cheap request, so it runs on every sync (including a single new profile) and
        # always with a wide window: conversions are attributed to the click day and arrive late
        k_since, _ = _date_range(max(days, config.KEITARO_DAYS_BACK))
        try:
            ok, m = sync_keitaro(k_since, until)
        except Exception as e:
            log.exception("keitaro")
            ok, m = False, f"Keitaro: error — {e}"
        all_ok &= ok
        msgs.append(m)
        try:
            with Session() as s:
                settings = get_settings(s)
                if keitaro.enabled():
                    alerts.check_thresholds(s, settings.get("fx_to_usd") or {}, settings)
                else:
                    alerts.check_thresholds(s, settings.get("fx_to_usd") or {},
                                            {**settings, "alert_spend_no_reg_usd": 0})
                s.commit()
        except Exception:
            log.exception("thresholds")
        try:
            alerts.flush()
        except Exception:
            log.exception("alerts flush")
        try:
            ok, m = fx.refresh()
            if not ok:
                msgs.append(m)  # rates failing to refresh is worth seeing in the log, not a run failure
        except Exception:
            log.exception("fx")

        message = " | ".join(msgs) or "No active profiles"
        with Session() as s:
            run = s.get(SyncRun, run_id)
            run.finished_at, run.ok, run.message = utcnow(), all_ok, message[:5000]
            s.commit()
        log.info(message)
        return message
    finally:
        _lock.release()


def run_in_background(days: int, trigger: str, profile_ids: list[int] | None = None) -> bool:
    if _lock.locked():
        return False
    threading.Thread(target=sync_all, args=(days, trigger, profile_ids), daemon=True).start()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    init_db()
    print(sync_all(ap.parse_args().days, "cli"))
