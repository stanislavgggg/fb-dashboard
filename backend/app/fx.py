"""Daily currency rates to USD. Source: Frankfurter (ECB rates, no key, ~30 currencies).

fx_to_usd stores "how many USD is 1 unit of the currency". Manual overrides live in fx_manual and win."""
import logging
from datetime import date

import httpx
from sqlalchemy import select

from .db import AdAccount, Session, get_settings, set_settings

log = logging.getLogger("fx")
URL = "https://api.frankfurter.dev/v1/latest"  # the old api.frankfurter.app answers 301 to this


def fetch(currencies: set[str]) -> dict[str, float]:
    """Returns {CUR: usd_per_unit} for the currencies the source knows; unknown ones are skipped."""
    wanted = {c for c in currencies if c and c != "USD"}
    if not wanted:
        return {}
    r = httpx.get(URL, params={"base": "USD", "symbols": ",".join(sorted(wanted))}, timeout=20,
                  follow_redirects=True)  # survive the next move of the API too
    r.raise_for_status()
    rates = r.json().get("rates") or {}
    return {cur: round(1 / float(v), 6) for cur, v in rates.items() if float(v) > 0}


def refresh(force: bool = False) -> tuple[bool, str]:
    """Update automatic rates once a day for every currency seen on ad accounts (+ already configured)."""
    with Session() as s:
        st = get_settings(s)
        if not st.get("fx_auto") and not force:
            return True, "automatic rates are off"
        if not force and st.get("fx_updated_at") == date.today().isoformat() and st.get("fx_source") == "auto":
            return True, "rates already updated today"
        seen = set(s.scalars(select(AdAccount.currency).distinct()).all())
        seen |= set((st.get("fx_to_usd") or {}).keys())
        try:
            auto = fetch(seen)
        except Exception as e:
            log.warning("fx fetch failed: %s", e)
            return False, f"rates not updated: {e}"
        manual = {k.upper(): float(v) for k, v in (st.get("fx_manual") or {}).items()}
        fx = {**(st.get("fx_to_usd") or {}), **auto, **manual, "USD": 1.0}
        missing = sorted(c for c in seen if c not in fx)
        set_settings(s, {"fx_to_usd": fx, "fx_updated_at": date.today().isoformat(), "fx_source": "auto"})
        s.commit()
    msg = f"rates updated: {', '.join(sorted(auto))}" if auto else "rates: nothing to update"
    if missing:
        msg += f"; not in the source: {', '.join(missing)} — set them manually"
    return True, msg
