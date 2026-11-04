"""Ads Manager session tokens: cookies + user agent, and the adsmanager-graph host fallback."""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import main, sync
from app.db import Profile, Session
from app.fb import FBClient, normalize_cookies
from app.sync import sync_all
from tests.test_core import KT, acc, insight, mock_kt

G = "https://graph.facebook.com/v24.0"
AM = "https://adsmanager-graph.facebook.com/v24.0"
COOKIE = "datr=AAA; c_user=1000123; xs=12%3Aabc%3A2; fr=0x"
UA = "Mozilla/5.0 (Macintosh) Chrome/131"


@pytest.mark.parametrize("raw", [
    COOKIE,
    "cookie: " + COOKIE,
    json.dumps([{"name": "c_user", "value": "1000123", "domain": ".facebook.com"},
                {"name": "xs", "value": "12%3Aabc%3A2", "domain": ".facebook.com"},
                {"name": "sid", "value": "x", "domain": ".google.com"}]),
])
def test_normalize_cookies(raw):
    out = normalize_cookies(raw)
    assert "c_user=1000123" in out and "xs=12%3Aabc%3A2" in out and "sid=" not in out


def test_normalize_cookies_requires_session():
    with pytest.raises(ValueError):
        normalize_cookies("datr=AAA; fr=0x")
    with pytest.raises(ValueError):
        normalize_cookies("[not json")
    assert normalize_cookies("") is None


@respx.mock
def test_client_sends_cookies_and_falls_back_to_adsmanager_host(respx_mock):
    respx_mock.get(f"{G}/me").mock(return_value=httpx.Response(400, json={"error": {"message": "Invalid request.", "code": 1}}))
    am = respx_mock.get(f"{AM}/me").mock(return_value=httpx.Response(200, json={"id": "1", "name": "U"}))
    am_acc = respx_mock.get(f"{AM}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": []}))
    c = FBClient("EAAB" + "x" * 40, cookies=normalize_cookies(COOKIE), user_agent=UA)
    assert c.me()["name"] == "U" and c.host == "adsmanager-graph.facebook.com"
    req = am.calls.last.request
    assert "c_user=1000123" in req.headers["cookie"] and req.headers["user-agent"] == UA
    list(c.ad_accounts())
    assert am_acc.called  # stuck with the working host


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "run_in_background", lambda days, trigger, ids=None: (sync.sync_all(days, trigger, ids), True)[1])
    with TestClient(main.app) as c:
        c.post("/api/auth/login", json={"login": "admin", "password": "adminpass123"})
        yield c


@respx.mock(assert_all_called=False)
def test_profile_with_cookies_end_to_end(client, respx_mock):
    me = respx_mock.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"id": "1", "name": "Agency U"}))
    respx_mock.get(f"{G}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": [acc("act_1", "Cab")]}))
    respx_mock.get(f"{G}/act_1/insights").mock(return_value=httpx.Response(200, json={"data": [insight("c1", "A", 5)]}))
    mock_kt(respx_mock, [])
    body = {"token": "EAAB" + "y" * 40, "cookies": COOKIE, "user_agent": "User-Agent: " + UA}
    r = client.post("/api/profiles/check", json=body).json()
    assert r["ok"] and r["host"] == "graph.facebook.com"
    assert client.post("/api/profiles/check", json={**body, "cookies": "fr=1"}).status_code == 400
    assert client.post("/api/profiles", json={"name": "P", **body}).status_code == 200
    p = client.get("/api/profiles").json()[0]
    assert p["has_cookies"] and p["user_agent"] == UA and "xs" not in json.dumps(p)  # cookies never returned
    assert me.calls.last.request.headers["cookie"].startswith("datr=AAA")  # the sync used them too
    # update without cookies keeps them; "" removes them
    client.patch("/api/profiles/1", json={"name": "P2"})
    assert client.get("/api/profiles").json()[0]["has_cookies"]
    client.patch("/api/profiles/1", json={"name": "P2", "cookies": ""})
    assert not client.get("/api/profiles").json()[0]["has_cookies"]


ADS = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"


@respx.mock
def test_token_from_cookies(respx_mock):
    from app.fb import token_from_cookies
    respx_mock.get(ADS).mock(return_value=httpx.Response(200, text='...,"accessToken":"EAAB' + "z" * 60 + '",...'))
    tok = token_from_cookies(normalize_cookies(COOKIE), user_agent=UA)
    assert tok.startswith("EAAB") and len(tok) > 40


@respx.mock
def test_token_from_cookies_logged_out(respx_mock):
    from app.fb import token_from_cookies, FBError
    respx_mock.get(ADS).mock(return_value=httpx.Response(302, headers={"location": "https://www.facebook.com/login/"}))
    respx_mock.get("https://www.facebook.com/login/").mock(return_value=httpx.Response(200, text="log in"))
    with pytest.raises(FBError) as e:
        token_from_cookies(normalize_cookies(COOKIE))
    assert e.value.code == 459


@respx.mock(assert_all_called=False)
def test_check_fetches_token_from_cookies_when_none_given(client, respx_mock):
    respx_mock.get(ADS).mock(return_value=httpx.Response(200, text='"accessToken":"EAAB' + "k" * 60 + '"'))
    respx_mock.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"id": "1", "name": "U"}))
    respx_mock.get(f"{G}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": []}))
    r = client.post("/api/profiles/check", json={"cookies": COOKIE, "user_agent": UA}).json()
    assert r["ok"] and r["token_from_cookies"] is True
    # can create a profile with cookies and no token
    assert client.post("/api/profiles", json={"name": "C", "cookies": COOKIE, "user_agent": UA}).status_code == 200


@respx.mock(assert_all_called=False)
def test_sync_refreshes_dead_token_from_cookies(client, respx_mock):
    client.post("/api/profiles", json={"name": "P", "token": "EAAB" + "o" * 40, "cookies": COOKIE, "user_agent": UA})
    respx_mock.reset()
    respx_mock.get(f"{G}/me").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "expired", "code": 190}}),  # old token dies
        httpx.Response(200, json={"id": "1", "name": "U"}),                        # fresh token works
    ])
    respx_mock.get(f"{G}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": []}))
    respx_mock.get(ADS).mock(return_value=httpx.Response(200, text='"accessToken":"EAAB' + "n" * 60 + '"'))
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    with Session() as s:
        from app.crypto import dec
        p = s.scalar(__import__("sqlalchemy").select(Profile))
        assert p.token_ok is True and dec(p.token_enc).startswith("EAAB" + "n")  # token was replaced
