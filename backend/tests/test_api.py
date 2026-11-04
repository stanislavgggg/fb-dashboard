import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import main, sync

G = "https://graph.facebook.com/v24.0"


@pytest.fixture
def client(monkeypatch):
    # run background syncs inline so tests are deterministic
    monkeypatch.setattr(main, "run_in_background",
                        lambda days, trigger, ids=None: (sync.sync_all(days, trigger, ids), True)[1])
    with TestClient(main.app) as c:
        yield c


def login(c, user="admin", pw="adminpass123"):
    r = c.post("/api/auth/login", json={"login": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_requires_login(client):
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/profiles").status_code == 401


def test_bad_password_and_bruteforce(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"login": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"login": "admin", "password": "adminpass123"}).status_code == 429
    main.auth._fails.clear()


@respx.mock(assert_all_called=False)
def test_profile_flow(client, respx_mock):
    login(client)
    respx_mock.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"id": "1", "name": "Agency U"}))
    respx_mock.get(f"{G}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "act_9", "name": "Cab 9", "account_status": 1, "currency": "USD", "amount_spent": "100"}]}))
    respx_mock.get(f"{G}/act_9/insights").mock(return_value=httpx.Response(200, json={"data": []}))
    respx_mock.post("https://kt.example.com/admin_api/v1/report/build").mock(
        return_value=httpx.Response(200, json={"rows": []}))

    r = client.post("/api/profiles/check", json={"token": "EAAB" + "y" * 40, "proxy": "1.2.3.4:80:u:p"})
    assert r.json()["ok"] and r.json()["total"] == 1

    bad = client.post("/api/profiles", json={"name": "X", "token": "EAAB" + "y" * 40, "proxy": "a:b:c"})
    assert bad.status_code == 400

    r = client.post("/api/profiles", json={"name": "Agency A #1", "supplier": "Agency A", "buyer": "Stefan",
                                          "token": "EAAB" + "y" * 40, "proxy": "1.2.3.4:80:u:p"})
    assert r.status_code == 200
    pid = r.json()["id"]

    profiles = client.get("/api/profiles").json()
    assert profiles[0]["accounts"] == 1 and profiles[0]["token_ok"] is True
    assert "yyyy" not in profiles[0]["token_masked"][6:-4]  # token never returned in full
    assert profiles[0]["proxy_masked"] == "1.2.3.4:80"

    accs = client.get("/api/accounts").json()
    assert accs[0]["id"] == "act_9"
    assert client.patch("/api/accounts/act_9", json={"geo": "lt"}).status_code == 200
    assert client.get("/api/accounts").json()[0]["geo"] == "LT"

    # update without token keeps the token
    assert client.patch(f"/api/profiles/{pid}", json={"name": "Renamed", "supplier": "Agency A"}).status_code == 200
    assert client.get("/api/profiles").json()[0]["name"] == "Renamed"

    meta = client.get("/api/meta").json()
    assert meta["suppliers"] == ["Agency A"] and meta["keitaro_enabled"]

    st = client.get("/api/stats", params={"group": "supplier"}).json()
    assert len(st["daily"]) == 7

    csv = client.get("/api/stats/export.csv")
    assert csv.status_code == 200 and "TOTAL" in csv.text

    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    assert client.get("/api/accounts").json() == []


def test_viewer_cannot_edit(client):
    login(client)
    assert client.post("/api/users", json={"login": "buyer", "password": "buyerpass1", "role": "viewer"}).status_code == 200
    client.post("/api/auth/logout")
    login(client, "buyer", "buyerpass1")
    assert client.get("/api/stats").status_code == 200
    assert client.post("/api/profiles", json={"name": "x", "token": "EAAB" + "z" * 40}).status_code == 403
    assert client.get("/api/settings").status_code == 403


def test_settings_roundtrip(client):
    login(client)
    r = client.put("/api/settings", json={"fx_to_usd": {"eur": 1.1, "PLN": 0.25, "BAD": 0}, "alert_cap_ratio": 0.8})
    assert r.status_code == 200
    st = client.get("/api/settings").json()
    assert st["fx_to_usd"] == {"EUR": 1.1, "PLN": 0.25, "USD": 1.0}
    assert st["alert_cap_ratio"] == 0.8
    assert client.put("/api/settings", json={"alert_cap_ratio": 2}).status_code == 422


def test_cannot_delete_self(client):
    login(client)
    assert client.delete("/api/users/1").status_code == 400
    assert client.patch("/api/users/1", json={"role": "viewer"}).status_code == 400


def test_spa_fallback_and_api_404(client):
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/healthz").json() == {"ok": True}
