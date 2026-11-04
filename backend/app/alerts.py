"""Alerts: stored in alert_log (dedup by key), then pushed to Telegram."""
import html
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select

from . import config
from .db import AdAccount, AlertLog, CampaignDay, KeitaroDay, Profile, Session, get_settings, utcnow
from .fb import ACTIVE_STATUSES, ACCOUNT_STATUS

log = logging.getLogger("alerts")
TG_MAX = 3500  # Telegram hard limit is 4096; keep headroom for the "...and N more" tail
NET_FAILS_TO_ALERT = 2  # consecutive proxy/network failures before we alert


def emit(s, key: str, text: str, level: str = "warn") -> bool:
    """Returns True if this is a new alert (not seen before with this key)."""
    if s.get(AlertLog, key):
        return False
    s.add(AlertLog(key=key, text=text, level=level, created_at=utcnow()))
    return True


def account_today(tz_name: str) -> date:
    """Facebook counts the day in the ad account's time zone, so 'today' differs per account."""
    try:
        return datetime.now(ZoneInfo(tz_name)).date() if tz_name else date.today()
    except (ZoneInfoNotFoundError, ValueError):
        return date.today()


def account_status_changed(s, acc: AdAccount, old_status: int | None, profile: Profile):
    if old_status is None or old_status == acc.status:
        return
    was_active, now_active = old_status in ACTIVE_STATUSES, acc.status in ACTIVE_STATUSES
    if was_active and not now_active:
        emit(s, f"acc_down:{acc.id}:{acc.status}:{date.today()}",
             f"🔴 Ad account <b>{html.escape(acc.name)}</b> ({acc.id}) — {ACCOUNT_STATUS.get(acc.status, acc.status)}\n"
             f"Profile: {html.escape(profile.name)} · {html.escape(profile.supplier or '—')}", "crit")
    elif not was_active and now_active:
        emit(s, f"acc_up:{acc.id}:{date.today()}",
             f"🟢 Ad account <b>{html.escape(acc.name)}</b> ({acc.id}) is active again", "info")


def accounts_unshared(s, profile: Profile, accs: list[AdAccount]):
    """Accounts that disappeared from the profile (agency pulled access)."""
    if not accs:
        return
    names = ", ".join(f"{html.escape(a.name)} ({a.id})" for a in accs[:10])
    more = f" and {len(accs) - 10} more" if len(accs) > 10 else ""
    emit(s, f"acc_gone:{profile.id}:{date.today()}:{len(accs)}",
         f"⚪ Ad accounts disappeared from profile <b>{html.escape(profile.name)}</b> ({len(accs)}): {names}{more}", "warn")


def token_died(s, profile: Profile, error: str):
    emit(s, f"token:{profile.id}:{date.today()}",
         f"🔑 Token of profile <b>{html.escape(profile.name)}</b> ({html.escape(profile.supplier or '—')}) stopped working:\n"
         f"{html.escape(error[:300])}", "crit")


def token_refreshed(s, profile: Profile):
    emit(s, f"token_refreshed:{profile.id}:{utcnow():%Y-%m-%d %H}",
         f"🔄 Token of profile <b>{html.escape(profile.name)}</b> ({html.escape(profile.supplier or '—')}) "
         f"expired — a fresh one was taken from the saved cookies, sync continues", "info")


def network_failing(s, profile: Profile, error: str):
    emit(s, f"net:{profile.id}:{date.today()}",
         f"🌐 Profile <b>{html.escape(profile.name)}</b> ({html.escape(profile.supplier or '—')}) failed "
         f"{profile.net_fails} times in a row — the proxy is most likely dead:\n{html.escape(error[:300])}", "crit")


def check_thresholds(s, fx: dict, settings: dict):
    # 1) campaign spent N USD today (account's own "today") with zero regs in Keitaro
    limit = float(settings.get("alert_spend_no_reg_usd") or 0)
    if limit > 0:
        lo = date.today() - timedelta(days=1)
        hi = date.today() + timedelta(days=1)
        rows = s.execute(
            select(CampaignDay.day, CampaignDay.campaign_id, CampaignDay.campaign_name, CampaignDay.spend,
                   AdAccount.currency, AdAccount.name, AdAccount.timezone)
            .join(AdAccount, AdAccount.id == CampaignDay.account_id)
            .where(CampaignDay.day >= lo, CampaignDay.day <= hi)
        ).all()
        regs = {}
        for day, sub, n in s.execute(
                select(KeitaroDay.day, KeitaroDay.sub_value, func.sum(KeitaroDay.regs))
                .where(KeitaroDay.day >= lo, KeitaroDay.day <= hi).group_by(KeitaroDay.day, KeitaroDay.sub_value)):
            regs[(day, sub)] = n
        for day, cid, cname, spend, cur, acc_name, tz in rows:
            if day != account_today(tz):
                continue
            usd = spend * fx.get(cur, 1.0)
            if usd >= limit and not regs.get((day, cid)):
                emit(s, f"noreg:{cid}:{day}",
                     f"🟠 <b>{html.escape(cname)}</b> ({html.escape(acc_name)}): spent ${usd:.0f} today, 0 registrations")
    # 2) account close to its spend cap
    ratio = float(settings.get("alert_cap_ratio") or 0)
    if ratio > 0:
        for acc in s.scalars(select(AdAccount).where(AdAccount.spend_cap > 0, AdAccount.missing.is_(False))):
            if acc.status in ACTIVE_STATUSES and acc.amount_spent >= acc.spend_cap * ratio:
                emit(s, f"cap:{acc.id}:{acc.spend_cap}",
                     f"🟡 Ad account <b>{html.escape(acc.name)}</b>: spent {acc.amount_spent:.0f} of the spend cap "
                     f"{acc.spend_cap:.0f} {acc.currency}")


def send_telegram(chat_id: str, text: str) -> tuple[bool, str]:
    if not config.TG_BOT_TOKEN or not chat_id:
        return False, "TG_BOT_TOKEN or chat_id is not set"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": True}, timeout=20)
        data = r.json()
        return bool(data.get("ok")), data.get("description", "")
    except Exception as e:  # network issues must not break the sync
        return False, str(e)


def _chunks(alerts: list[AlertLog]) -> list[list[AlertLog]]:
    """Group alerts into messages that fit Telegram's length limit."""
    out, cur, size = [], [], 0
    for a in alerts:
        t = a.text[:TG_MAX]  # a single oversized alert is cut, never stuck
        if cur and size + len(t) + 2 > TG_MAX:
            out.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += len(t) + 2
    if cur:
        out.append(cur)
    return out


def flush(max_messages: int = 3):
    """Send unsent alerts. Several messages per run if needed, sized under Telegram's limit."""
    with Session() as s:
        settings = get_settings(s)
        pending = s.scalars(select(AlertLog).where(AlertLog.sent.is_(False))
                            .order_by(AlertLog.created_at).limit(200)).all()
        if not pending:
            return
        if not settings.get("alerts_enabled"):
            for a in pending:  # alerts off: don't build a backlog that floods the chat later
                a.sent = True
            s.commit()
            return
        if not settings.get("tg_chat_id"):
            return  # keep them unsent, they're still visible in the UI
        # chat was just configured: don't replay last month's history into it
        stale_cut = utcnow() - timedelta(days=1)
        fresh = []
        for a in pending:
            if a.created_at < stale_cut:
                a.sent = True
            else:
                fresh.append(a)
        s.commit()
        chunks = _chunks(fresh)
        for i, chunk in enumerate(chunks[:max_messages]):
            text = "\n\n".join(a.text[:TG_MAX] for a in chunk)
            if i == max_messages - 1 and len(chunks) > max_messages:
                left = sum(len(c) for c in chunks[max_messages:])
                text += f"\n\n…and {left} more — see the dashboard"
            ok, err = send_telegram(str(settings["tg_chat_id"]), text)
            if not ok:
                log.warning("telegram send failed: %s", err)
                break
            for a in chunk:
                a.sent = True
            if i == max_messages - 1 and len(chunks) > max_messages:
                for c in chunks[max_messages:]:
                    for a in c:
                        a.sent = True  # summarized in the tail
            s.commit()


def prune(days: int = 30):
    with Session() as s:
        cutoff = utcnow() - timedelta(days=days)
        for a in s.scalars(select(AlertLog).where(AlertLog.created_at < cutoff)):
            if a.key.startswith("cap:"):
                continue  # dateless dedup key: deleting it would re-fire the same cap alert
            s.delete(a)
        s.commit()
