"""Minimal Meta Marketing API client. Every request goes through the profile's own proxy."""
import json
import time
from datetime import date

import httpx
from urllib.parse import quote, unquote

from . import config

_sleep = time.sleep  # patched in tests

ACCOUNT_STATUS = {
    1: "Active", 2: "Disabled", 3: "Unsettled", 7: "Pending review",
    8: "Pending settlement", 9: "Grace period", 100: "Pending closure", 101: "Closed",
    201: "Active", 202: "Closed",
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
        raise ValueError(f"Can't parse host:port in proxy: {hp!r}")
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
            raise ValueError(f"Unsupported proxy scheme: {scheme}")
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
    raise ValueError("Can't parse the proxy. Example: host:port:user:pass or http://user:pass@host:port")


# What the common Graph API errors usually mean for agency tokens
HINTS = {
    190: "The token is expired, revoked or was copied incompletely — take a fresh one from the profile.",
    102: "The session behind the token has ended — take a fresh token.",
    1: "Facebook rejected the request without details. For agency tokens this almost always means the request "
       "came from a different IP than the profile's — add the profile's proxy.",
    2: "Temporary Facebook error — try again in a minute.",
    10: "The token has no permission for ads data (ads_read / ads_management).",
    200: "The token has no permission for ads data (ads_read / ads_management).",
    100: "The request was rejected as invalid for this token — often an IP/session mismatch (add the profile's proxy) "
         "or a token from a different app type.",
    368: "Facebook has temporarily blocked this account for suspicious activity.",
    459: "Facebook wants a checkpoint/login confirmation in the profile — open it in the anti-detect browser.",
    460: "The password of the account was changed — the token is no longer valid.",
    464: "The account behind the token is not confirmed / checkpointed.",
}


class FBError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 etype: str = "", user_msg: str = ""):
        super().__init__(message)
        self.code, self.subcode, self.etype, self.user_msg = code, subcode, etype, user_msg

    @property
    def hint(self) -> str:
        if self.code is None:
            return "Network or proxy error — check that the proxy is alive and the format is right."
        return HINTS.get(self.code, "")

    def detail(self) -> str:
        """One line with everything Facebook told us, for logs and the UI."""
        bits = [str(self)]
        meta = [f"code {self.code}"] if self.code is not None else []
        if self.subcode:
            meta.append(f"subcode {self.subcode}")
        if self.etype:
            meta.append(self.etype)
        if meta:
            bits.append(f"({', '.join(meta)})")
        if self.user_msg and self.user_msg not in str(self):
            bits.append(f"— {self.user_msg}")
        return " ".join(bits)

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
            raise FBError(err.get("message", "unknown error"), code, err.get("error_subcode"),
                          err.get("type", ""), err.get("error_user_msg") or err.get("error_user_title") or "")
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
