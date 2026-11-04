"""Buyer-scoped viewers and automatic currency rates."""
from datetime import date

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import fx, main, sync
from app.crypto import enc
from app.db import AdAccount, CampaignDay, KeitaroDay, Profile, Session, AlertLog, utcnow
from tests.test_core import TODAY


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "run_in_background", lambda days, trigger, ids=None: (sync.sync_all(days, trigger, ids), True)[1])
    with TestClient(main.app) as c:
        yield c


def _login(c, user="admin", pw="adminpass123"):
    r = c.post("/api/auth/login", json={"login": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _seed():
    with Session() as s:
        p1 = Profile(name="Stefan login", buyer="Stefan", supplier="A", token_enc=enc("x" * 40))
        p2 = Profile(name="Milos login", buyer="Milos", supplier="B", token_enc=enc("x" * 40))
        s.add_all([p1, p2])
        s.flush()
        s.add(AdAccount(id="act_s", profile_id=p1.id, name="S cab", status=1, currency="USD", geo="LT"))
        s.add(AdAccount(id="act_m", profile_id=p2.id, name="M cab", status=1, currency="USD", geo="ES"))
        s.add(CampaignDay(day=TODAY, campaign_id="cs", account_id="act_s", campaign_name="S camp", spend=10))
        s.add(CampaignDay(day=TODAY, campaign_id="cm", account_id="act_m", campaign_name="M camp", spend=20))
        s.add(KeitaroDay(day=TODAY, sub_value="zzz", regs=5))
        s.add(AlertLog(key=f"token:{p1.id}:{TODAY}", text="stefan token"))
        s.add(AlertLog(key=f"token:{p2.id}:{TODAY}", text="milos token"))
        s.add(AlertLog(key="noreg:cm:x", text="milos noreg"))
        s.add(AlertLog(key="cap:act_s:1", text="stefan cap"))
        s.commit()


def test_scoped_viewer_sees_only_own_buyer(client):
    _seed()
    _login(client)
    assert client.post("/api/users", json={"login": "stefan", "password": "stefanpass1", "role": "viewer", "buyer": "Stefan"}).status_code == 200
    assert client.get("/api/users").json()[1]["buyer"] == "Stefan"
    client.post("/api/auth/logout")
    me = _login(client, "stefan", "stefanpass1")
    assert me["buyer"] == "Stefan"

    meta = client.get("/api/meta").json()
    assert meta["scope_buyer"] == "Stefan" and meta["buyers"] == ["Stefan"] and meta["geos"] == ["LT"]
    assert [p["name"] for p in meta["profiles"]] == ["Stefan login"]

    st = client.get("/api/stats", params={"group": "campaign"}).json()
    assert [r["key"] for r in st["rows"]] == ["cs"] and st["total"]["spend"] == 10
    assert st["unmatched"] == []  # cross-buyer Keitaro leftovers are not shown to a scoped viewer
    # trying to look at another buyer is overridden, not honoured
    st = client.get("/api/stats", params={"group": "campaign", "buyer": "Milos"}).json()
    assert [r["key"] for r in st["rows"]] == ["cs"]
    assert "cm" not in client.get("/api/stats/export.csv", params={"group": "campaign"}).text

    assert [a["id"] for a in client.get("/api/accounts").json()] == ["act_s"]
    assert [p["name"] for p in client.get("/api/profiles").json()] == ["Stefan login"]
    assert {a["text"] for a in client.get("/api/alerts").json()} == {"stefan token", "stefan cap"}
    runs = client.get("/api/sync/runs").json()
    assert all(r["message"] == "" for r in runs)


def test_admin_and_unscoped_viewer_see_everything(client):
    _seed()
    _login(client)
    assert len(client.get("/api/stats", params={"group": "campaign"}).json()["rows"]) == 2
    client.post("/api/users", json={"login": "viewer", "password": "viewerpass1", "role": "viewer"})
    # buyer can be set later and cleared
    assert client.patch("/api/users/2", json={"buyer": "Milos"}).status_code == 200
    assert client.patch("/api/users/2", json={"buyer": ""}).status_code == 200
    client.post("/api/auth/logout")
    _login(client, "viewer", "viewerpass1")
    assert client.get("/api/meta").json()["scope_buyer"] is None
    assert len(client.get("/api/stats", params={"group": "campaign"}).json()["rows"]) == 2


# ---------------------------------------------------------------- fx
def test_fx_fetch_inverts_rates(monkeypatch):
    monkeypatch.setattr(fx, "fetch", fx._real_fetch)
    with respx.mock() as r:
        r.get(fx.URL).mock(return_value=httpx.Response(200, json={"base": "USD", "rates": {"EUR": 0.92, "PLN": 4.0}}))
        got = fx.fetch({"EUR", "PLN", "USD"})
    assert got == {"EUR": round(1 / 0.92, 6), "PLN": 0.25}


def test_fx_refresh_uses_account_currencies_and_manual_overrides(monkeypatch):
    with Session() as s:
        p = Profile(name="P", token_enc=enc("x" * 40))
        s.add(p)
        s.flush()
        s.add(AdAccount(id="act_1", profile_id=p.id, name="A", currency="EUR"))
        s.add(AdAccount(id="act_2", profile_id=p.id, name="B", currency="XYZ"))
        from app.db import set_settings
        set_settings(s, {"fx_manual": {"EUR": 1.5}})
        s.commit()
    calls = []
    monkeypatch.setattr(fx, "fetch", lambda cur: (calls.append(set(cur)), {"EUR": 1.1, "GBP": 1.3})[1])
    ok, msg = fx.refresh()
    assert ok and "XYZ" in msg
    assert calls[0] >= {"EUR", "XYZ"}
    with Session() as s:
        from app.db import get_settings
        st = get_settings(s)
        assert st["fx_to_usd"]["EUR"] == 1.5  # manual wins
        assert st["fx_to_usd"]["GBP"] == 1.3 and st["fx_to_usd"]["USD"] == 1.0
        assert st["fx_updated_at"] == date.today().isoformat() and st["fx_source"] == "auto"
    # second call the same day is a no-op
    ok, msg = fx.refresh()
    assert ok and "already" in msg and len(calls) == 1


def test_fx_refresh_disabled_and_failure(monkeypatch):
    with Session() as s:
        from app.db import set_settings
        set_settings(s, {"fx_auto": False})
        s.commit()
    assert fx.refresh() == (True, "automatic rates are off")
    monkeypatch.setattr(fx, "fetch", lambda cur: (_ for _ in ()).throw(httpx.ConnectError("down")))
    ok, msg = fx.refresh(force=True)
    assert not ok and "down" in msg


def test_fx_endpoints(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(fx, "fetch", lambda cur: {"EUR": 1.2})
    r = client.post("/api/settings/fx-refresh")
    assert r.status_code == 200 and r.json()["fx_to_usd"]["EUR"] == 1.2
    # manual override via settings applies immediately
    assert client.put("/api/settings", json={"fx_manual": {"eur": 1.05}, "fx_auto": True}).status_code == 200
    st = client.get("/api/settings").json()
    assert st["fx_to_usd"]["EUR"] == 1.05 and st["fx_manual"] == {"EUR": 1.05}
    # refresh failure is a 400 with the reason
    monkeypatch.setattr(fx, "fetch", lambda cur: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert client.post("/api/settings/fx-refresh").status_code == 400
