"""Aggregation: FB spend (converted to USD) joined with Keitaro regs/deps by FB campaign ID."""
from collections import defaultdict
from datetime import date

from sqlalchemy import select

from .db import AdAccount, CampaignDay, KeitaroDay, Profile

GROUPS = ("supplier", "buyer", "geo", "profile", "account", "campaign", "day")


def _blank():
    return {"spend": 0.0, "impressions": 0, "clicks": 0, "fb_results": 0,
            "k_clicks": 0, "regs": 0, "deps": 0, "revenue": 0.0}


def _finish(m: dict) -> dict:
    spend, regs, deps, rev = m["spend"], m["regs"], m["deps"], m["revenue"]
    m["spend"] = round(spend, 2)
    m["revenue"] = round(rev, 2)
    m["profit"] = round(rev - spend, 2)
    m["roi"] = round((rev - spend) / spend * 100, 1) if spend > 0 else None
    m["cpr"] = round(spend / regs, 2) if regs else None  # cost per registration
    m["cpd"] = round(spend / deps, 2) if deps else None  # cost per deposit
    m["reg2dep"] = round(deps / regs * 100, 1) if regs else None
    m["ctr"] = round(m["clicks"] / m["impressions"] * 100, 2) if m["impressions"] else None
    m["cpc"] = round(spend / m["clicks"], 3) if m["clicks"] else None
    m["cpm"] = round(spend / m["impressions"] * 1000, 2) if m["impressions"] else None
    return m


def _add(dst: dict, src: dict):
    for k in ("spend", "impressions", "clicks", "fb_results", "k_clicks", "regs", "deps", "revenue"):
        dst[k] += src.get(k, 0) or 0


def build(s, since: date, until: date, group: str, fx: dict, k_rate: float,
          supplier: str | None = None, buyer: str | None = None,
          profile_id: int | None = None, account_id: str | None = None, geo: str | None = None,
          hide_unmatched: bool = False) -> dict:
    if group not in GROUPS:
        group = "account"

    q = (select(CampaignDay.day, CampaignDay.campaign_id, CampaignDay.campaign_name, CampaignDay.spend,
                CampaignDay.impressions, CampaignDay.clicks, CampaignDay.fb_results,
                AdAccount.id, AdAccount.name, AdAccount.currency, AdAccount.status, AdAccount.missing,
                Profile.id, Profile.name, Profile.supplier, Profile.buyer, AdAccount.geo)
         .join(AdAccount, AdAccount.id == CampaignDay.account_id)
         .join(Profile, Profile.id == AdAccount.profile_id)
         .where(CampaignDay.day >= since, CampaignDay.day <= until))
    filtered = bool(supplier or buyer or profile_id or account_id or geo)
    if geo:
        q = q.where(AdAccount.geo == geo)
    if supplier:
        q = q.where(Profile.supplier == supplier)
    if buyer:
        q = q.where(Profile.buyer == buyer)
    if profile_id:
        q = q.where(Profile.id == profile_id)
    if account_id:
        q = q.where(AdAccount.id == account_id)
    fb_rows = s.execute(q).all()

    # Keitaro: by campaign for range totals, by (day, campaign) for the day view
    k_by_cid, k_by_day_cid = defaultdict(_blank), defaultdict(_blank)
    for day, sub, clicks, regs, deps, rev in s.execute(
            select(KeitaroDay.day, KeitaroDay.sub_value, KeitaroDay.clicks, KeitaroDay.regs,
                   KeitaroDay.deps, KeitaroDay.revenue)
            .where(KeitaroDay.day >= since, KeitaroDay.day <= until)).all():
        rec = {"k_clicks": clicks, "regs": regs, "deps": deps, "revenue": rev * k_rate}
        _add(k_by_cid[sub], rec)
        _add(k_by_day_cid[(day, sub)], rec)

    unknown_currencies = set()
    groups: dict[str, dict] = {}
    known_cids = set()
    campaign_owner = {}  # cid -> group key, so Keitaro data lands in the right row once

    def key_of(r):
        (day, cid, cname, _sp, _im, _cl, _fr, acc_id, acc_name, _cur, acc_status, acc_missing,
         pid, pname, sup, buy, acc_geo) = r
        if group == "supplier":
            return sup or "(без поставщика)", {"label": sup or "(без поставщика)"}
        if group == "geo":
            return acc_geo or "(без гео)", {"label": acc_geo or "(без гео)"}
        if group == "buyer":
            return buy or "(без байера)", {"label": buy or "(без байера)"}
        if group == "profile":
            return str(pid), {"label": pname, "supplier": sup, "buyer": buy, "profile_id": pid}
        if group == "account":
            return acc_id, {"label": acc_name, "account_id": acc_id, "profile": pname, "supplier": sup,
                            "status": acc_status, "missing": acc_missing}
        if group == "campaign":
            return cid, {"label": cname, "campaign_id": cid, "account": acc_name, "account_id": acc_id,
                         "supplier": sup}
        return day.isoformat(), {"label": day.isoformat()}

    for r in fb_rows:
        day, cid, cname, spend, imp, clk, res, acc_id, acc_name, cur, *_ = r
        rate = fx.get(cur)
        if rate is None:
            unknown_currencies.add(cur)
            rate = 1.0
        k, meta = key_of(r)
        g = groups.setdefault(k, {"key": k, **meta, **_blank()})
        _add(g, {"spend": spend * rate, "impressions": imp, "clicks": clk, "fb_results": res})
        known_cids.add(cid)
        if group == "day":
            pass
        else:
            campaign_owner.setdefault(cid, k)

    if group == "day":
        for (day, cid), rec in k_by_day_cid.items():
            if cid in known_cids:
                g = groups.setdefault(day.isoformat(), {"key": day.isoformat(), "label": day.isoformat(), **_blank()})
                _add(g, rec)
    else:
        for cid, k in campaign_owner.items():
            if cid in k_by_cid:
                _add(groups[k], k_by_cid[cid])

    rows = [_finish(g) for g in groups.values()]
    rows.sort(key=lambda x: x["key"] if group == "day" else -x["spend"])

    total = _blank()
    for g in rows:
        _add(total, g)

    # Keitaro traffic we couldn't tie to any known FB campaign (only meaningful without filters)
    unmatched = []
    if not filtered and not hide_unmatched:
        all_cids = set(s.scalars(select(CampaignDay.campaign_id).distinct()).all())
        for cid, rec in k_by_cid.items():
            if cid not in all_cids and (rec["regs"] or rec["deps"] or rec["k_clicks"]):
                unmatched.append({"sub_value": cid, **_finish(dict(rec))})
        unmatched.sort(key=lambda x: -(x["deps"] * 1000 + x["regs"]))

    return {"group": group, "rows": rows, "total": _finish(total), "unmatched": unmatched[:100],
            "unknown_currencies": sorted(unknown_currencies)}


def daily(s, since: date, until: date, fx: dict, k_rate: float, **filters) -> list[dict]:
    data = build(s, since, until, "day", fx, k_rate, **filters)
    by_day = {r["key"]: r for r in data["rows"]}
    out, d = [], since
    while d <= until:  # fill gaps so the chart has every day
        r = by_day.get(d.isoformat())
        out.append({"day": d.isoformat(), "spend": r["spend"] if r else 0, "revenue": r["revenue"] if r else 0,
                    "regs": r["regs"] if r else 0, "deps": r["deps"] if r else 0})
        d = date.fromordinal(d.toordinal() + 1)
    return out


def filter_options(s, scope_buyer: str | None = None) -> dict:
    pq = select(Profile)
    aq = select(AdAccount.id, AdAccount.name, AdAccount.geo).join(Profile, Profile.id == AdAccount.profile_id)
    if scope_buyer:
        pq = pq.where(Profile.buyer == scope_buyer)
        aq = aq.where(Profile.buyer == scope_buyer)
    profiles = s.scalars(pq.order_by(Profile.name)).all()
    accounts = s.execute(aq.order_by(AdAccount.name)).all()
    return {
        "suppliers": sorted({p.supplier for p in profiles if p.supplier}),
        "buyers": sorted({p.buyer for p in profiles if p.buyer}),
        "geos": sorted({g for _, _, g in accounts if g}),
        "profiles": [{"id": p.id, "name": p.name} for p in profiles],
        "accounts": [{"id": i, "name": n} for i, n, _ in accounts],
    }
