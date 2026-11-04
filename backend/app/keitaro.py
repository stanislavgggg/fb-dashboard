"""Keitaro Admin API: report grouped by day + the sub_id carrying the FB campaign ID.

In Keitaro: lead = registration, sale = deposit (standard postback statuses).
Note: with a "day" grouping Keitaro attributes leads/sales to the day of the CLICK, not the postback,
so a deposit made a week after the click lands on the click's day. That's why the Keitaro window is wide.
"""
from datetime import date

import httpx

from . import config

PAGE = 10000


def enabled() -> bool:
    return bool(config.KEITARO_URL and config.KEITARO_API_KEY)


def _filters() -> list[dict]:
    if config.KEITARO_TS_ID:  # only Facebook traffic; other sources' sub_ids would show up as "unmatched"
        return [{"name": "ts_id", "operator": "EQUALS", "expression": config.KEITARO_TS_ID}]
    return []


def report(since: date, until: date) -> list[dict]:
    sub = config.KEITARO_CAMPAIGN_SUB
    rows, offset = [], 0
    while True:
        body = {
            "range": {"from": since.isoformat(), "to": until.isoformat(), "timezone": config.KEITARO_TZ},
            "dimensions": ["day", sub],
            "measures": ["clicks", "leads", "sales", "revenue"],
            "filters": _filters(),
            "sort": [{"name": "day", "order": "ASC"}],
            "limit": PAGE,
            "offset": offset,
        }
        r = httpx.post(
            f"{config.KEITARO_URL}/admin_api/v1/report/build",
            json=body,
            headers={"Api-Key": config.KEITARO_API_KEY},
            timeout=120,
        )
        r.raise_for_status()
        page = r.json().get("rows", []) or []
        for row in page:
            value = str(row.get(sub) or "").strip()
            if not value or value.startswith("{"):  # empty or unresolved macro like {{campaign.id}}
                value = "(пусто)"
            rows.append({
                "day": date.fromisoformat(str(row["day"])[:10]),
                "sub_value": value[:200],
                "clicks": int(row.get("clicks") or 0),
                "regs": int(row.get("leads") or 0),
                "deps": int(row.get("sales") or 0),
                "revenue": float(row.get("revenue") or 0),
            })
        if len(page) < PAGE:
            return rows
        offset += PAGE
        if offset > 500_000:  # safety valve
            return rows
