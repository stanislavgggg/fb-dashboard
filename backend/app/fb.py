"""Minimal Meta Marketing API client. Every request goes through the profile's own proxy."""
import json
import time
from datetime import date

import httpx
from urllib.parse import quote, unquote

from . import config

_sleep = time.sleep  # patched in tests

ACCOUNT_STATUS = {
    1: "Активен", 2: "Отключён", 3: "Долг (unsettled)", 7: "На проверке",
    8: "Ожидает оплаты", 9: "Grace period", 100: "Закрывается", 101: "Закрыт",
    201: "Активен", 202: "Закрыт",
}
ACTIVE_STATUSES = {1, 201}

ACCOUNT_FIELDS = "id,name,account_status,disable_reason,currency,timezone_name,amount_spent,spend_cap,balance"


def _creds(user: str, pwd: str) -> str:
    # quote(unquote()) is idempotent: already-encoded %40 stays %40, a raw @ becomes %40
    q = lambda v: quote(unquote(v), safe="")  # noqa: E731
    return q(user) + (":" + q(pwd) if pwd else "")


def _hostport(hp: str) -> str:
    host, sep, port = hp.rpartition(":")
    if not sep or not port.isdigit() or not host or not (1 <= int(port) <= 65535):
        raise ValueError(f"Не понял host:port в прокси: {hp!r}")
    return f"{host}:{port}"


def normalize_proxy(raw: str | None) -> str | None:
    """Accepts the formats vendors usually give and returns a URL httpx understands.

    http://user:pass@host:port | socks5://... -> as is (credentials re-encoded if needed)
    host:port:user:pass                    -> http://user:pass@host:port   (pass may contain : @ #)
    user:pass@host:port                    -> http://user:pass@host:port   (pass may contain : @)
    host:port                              -> http://host:port
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            raise ValueError(f"Неподдерживаемая схема прокси: {scheme}")
        rest = rest.rstrip("/")
        if "@" in rest:
            creds, hp = rest.rsplit("@", 1)
            user, _, pwd = creds.partition(":")
            return f"{scheme}://{_creds(user, pwd)}@{_hostport(hp)}"
        return f"{scheme}://{_hostport(rest)}"
    # host:port:user:pass — checked first, because the password may itself contain @ or :
    parts = raw.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, pwd = parts
        return f"http://{_creds(user, pwd)}@{_hostport(host + ':' + port)}"
    if "@" in raw:
        creds, hp = raw.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
        return f"http://{_creds(user, pwd)}@{_hostport(hp)}"
    if len(parts) == 2:
        return f"http://{_hostport(raw)}"
    raise ValueError("Не понял формат прокси. Пример: host:port:user:pass или http://user:pass@host:port")


class FBError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def token_invalid(self) -> bool:
        return self.code in (190, 102, 463, 467)

    @property
    def network(self) -> bool:
        """Proxy down / DNS / timeout — not a Facebook answer at all."""
        return self.code is None


class FBClient:
    def __init__(self, token: str, proxy: str | None = None, timeout: float = 60, retries: int = 2):
        self.token = token
        self.retries = retries  # network retries; 0 for a quick "check access"
        self.http = httpx.Client(
            base_url=f"https://graph.facebook.com/{config.FB_API_VERSION}",
            proxy=proxy or None,
            timeout=timeout,
        )

    def close(self):
        self.http.close()

    def _request(self, url: str, params: dict | None = None, attempt: int = 0) -> dict:
        try:
            r = self.http.get(url, params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            if attempt < self.retries:
                _sleep(3)
                return self._request(url, params, attempt + 1)
            raise FBError(f"network: {e}") from e
        if "error" in data:
            err = data["error"]
            code = err.get("code")
            # 4 / 17 / 32 / 613 / 80004 — rate limits: wait and retry
            if code in (4, 17, 32, 613, 80004) and attempt < 3:
                _sleep(30 * (attempt + 1))
                return self._request(url, params, attempt + 1)
            raise FBError(err.get("message", "unknown error"), code)
        return data

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self._request(path, params)

    def paged(self, path: str, **params):
        data = self.get(path, **params)
        while True:
            yield from data.get("data", [])
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                break
            data = self._request(nxt)  # full URL, token already inside

    def me(self) -> dict:
        return self.get("/me", fields="id,name")

    def ad_accounts(self):
        return self.paged("/me/adaccounts", fields=ACCOUNT_FIELDS, limit=200)

    def campaign_insights(self, act_id: str, since: date, until: date):
        return self.paged(
            f"/{act_id}/insights",
            level="campaign",
            fields="campaign_id,campaign_name,spend,impressions,clicks,actions",
            time_range=json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            time_increment=1,
            limit=500,
        )


def money(raw) -> float:
    """amount_spent / spend_cap / balance come in minor units (cents) as strings."""
    try:
        return int(raw) / 100
    except (TypeError, ValueError):
        return 0.0


def count_results(actions: list | None) -> int:
    """Registrations as the pixel sees them. If several configured action types fire for the same
    registration (lead + complete_registration is common), summing would double count — take the max."""
    best = 0
    for a in actions or []:
        if a.get("action_type") in config.FB_RESULT_ACTIONS:
            best = max(best, int(float(a.get("value", 0))))
    return best
