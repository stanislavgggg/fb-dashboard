"""Regression tests for the bugs found in the review."""
from datetime import timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import alerts, main, sync
from app.crypto import enc
from app.db import AdAccount, AlertLog, CampaignDay, KeitaroDay, Profile, Session, SyncRun, User, utcnow
from app.fb import normalize_proxy
from app.sync import sync_all
from tests.test_core import G, KT, TODAY, acc, insight, make_profile, mock_fb, mock_kt


# ---- 2. proxies with special characters in the password
@pytest.mark.parametrize("raw,expected", [
    ("1.2.3.4:8000:user:p@ss#1", "http://user:p%40ss%231@1.2.3.4:8000"),
    ("1.2.3.4:8000:user:pa:ss", "http://user:pa%3Ass@1.2.3.4:8000"),
    ("user:p@ss@1.2.3.4:8000", "http://user:p%40ss@1.2.3.4:8000"),
    ("http://u:p@x@h.com:80/", "http://u:p%40x@h.com:80"),
    ("socks5://u:p%40x@h:1080", "socks5://u:p%40x@h:1080"),  # already encoded stays as is
])
def test_proxy_special_chars(raw, expected):
    assert normalize_proxy(raw) == expected
    httpx.Client(proxy=expected).close()  # httpx must accept it


def test_proxy_bad_port():
    with pytest.raises(ValueError):
        normalize_proxy("host:port")
    with pytest.raises(ValueError):
        normalize_proxy("1.2.3.4:99999")


# ---- 1. restart mid-run must not block syncing for 45 minutes
def test_startup_closes_fresh_unfinished_run():
    with Session() as s:
        s.add(SyncRun(trigger="schedule", started_at=utcnow() - timedelta(minutes=5)))
        s.commit()
    sync.on_startup()
    with respx.mock(assert_all_called=False) as r:
        mock_kt(r, [])
        assert "already running" not in sync_all(1, "startup")
    with Session() as s:
        runs = s.scalars(select(SyncRun).order_by(SyncRun.id)).all()
        assert runs[0].finished_at and "restart" in runs[0].message and runs[1].ok is True


# ---- 3. rotated SECRET_KEY: profiles page still opens, sync reports it
def test_rotated_key_does_not_break_profiles_page(monkeypatch):
    foreign = Fernet(Fernet.generate_key()).encrypt(b"EAABold" + b"x" * 40).decode()
    with Session() as s:
        s.add(Profile(name="Old", token_enc=foreign))
        s.commit()
    with TestClient(main.app) as c:
        c.post("/api/auth/login", json={"login": "admin", "password": "adminpass123"})
        r = c.get("/api/profiles")
        assert r.status_code == 200
        assert r.json()[0]["key_changed"] is True and r.json()[0]["token_ok"] is False
        chk = c.post("/api/profiles/check", json={"profile_id": r.json()[0]["id"]})
        assert chk.json()["ok"] is False and "key changed" in chk.json()["error"]
    with respx.mock(assert_all_called=False) as r:
        mock_kt(r, [])
        msg = sync_all(1, "t")
    assert "key changed" in msg
    with Session() as s:
        assert "key changed" in s.scalar(select(Profile)).last_error


# ---- 4. Telegram digest is split by length and never gets stuck
def test_flush_splits_long_digest(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_telegram", lambda chat, text: (sent.append(text), (True, ""))[1])
    monkeypatch.setattr(alerts.config, "TG_BOT_TOKEN", "t")
    with Session() as s:
        from app.db import set_settings
        set_settings(s, {"tg_chat_id": "-100", "alerts_enabled": True})
        for i in range(20):
            s.add(AlertLog(key=f"k{i}", text="🔑 " + "x" * 400, created_at=utcnow()))
        s.commit()
    alerts.flush()
    assert len(sent) >= 2 and all(len(t) <= 4096 for t in sent)
    with Session() as s:
        assert all(a.sent for a in s.scalars(select(AlertLog)))


def test_flush_does_not_replay_old_backlog(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_telegram", lambda chat, text: (sent.append(text), (True, ""))[1])
    with Session() as s:
        from app.db import set_settings
        set_settings(s, {"tg_chat_id": "-100", "alerts_enabled": True})
        s.add(AlertLog(key="old", text="old", created_at=utcnow() - timedelta(days=5)))
        s.add(AlertLog(key="new", text="new", created_at=utcnow()))
        s.commit()
    alerts.flush()
    assert sent == ["new"]
    with Session() as s:
        assert all(a.sent for a in s.scalars(select(AlertLog)))


# ---- 5/6. Keitaro is pulled for a new single profile and always with a wide window
@respx.mock(assert_all_called=False)
def test_single_profile_sync_pulls_keitaro_wide_window(respx_mock):
    pid = make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab")], {"act_1": [insight("c1", "A", 10)]})
    old = TODAY - timedelta(days=20)
    mock_kt(respx_mock, [{"day": old.isoformat(), "sub_id_1": "c1", "clicks": 1, "leads": 1, "sales": 1, "revenue": 50}])
    sync_all(3, "new_profile", [pid])
    with Session() as s:
        assert s.get(KeitaroDay, (old, "c1")).deps == 1
    body = respx_mock.calls.last.request.read()
    assert b'"from":"' + (TODAY - timedelta(days=44)).isoformat().encode() in body


# ---- Keitaro pagination + traffic-source filter
@respx.mock(assert_all_called=False)
def test_keitaro_pagination_and_filter(respx_mock, monkeypatch):
    from app import keitaro
    monkeypatch.setattr(keitaro, "PAGE", 2)
    monkeypatch.setattr(keitaro.config, "KEITARO_TS_ID", "7")
    pages = [[{"day": TODAY.isoformat(), "sub_id_1": "a", "leads": 1}, {"day": TODAY.isoformat(), "sub_id_1": "b", "leads": 1}],
             [{"day": TODAY.isoformat(), "sub_id_1": "c", "leads": 1}]]
    route = respx_mock.post(KT).mock(side_effect=[httpx.Response(200, json={"rows": p}) for p in pages])
    rows = keitaro.report(TODAY, TODAY)
    assert [r["sub_value"] for r in rows] == ["a", "b", "c"]
    assert route.call_count == 2
    assert b'"ts_id"' in route.calls[0].request.read()


# ---- 7. dead proxy alerts after two consecutive failures
@respx.mock(assert_all_called=False)
def test_dead_proxy_alerts(respx_mock):
    pid = make_profile()
    respx_mock.get(f"{G}/me").mock(side_effect=httpx.ProxyError("407 Proxy Authentication Required"))
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    with Session() as s:
        assert s.get(Profile, pid).net_fails == 1
        assert not s.scalar(select(AlertLog).where(AlertLog.key.like("net:%")))
    sync_all(1, "t")
    with Session() as s:
        assert s.get(Profile, pid).net_fails == 2
        a = s.scalar(select(AlertLog).where(AlertLog.key.like("net:%")))
        assert a and "proxy" in a.text
    # recovers: counter resets
    respx_mock.reset()
    mock_fb(respx_mock, [], {})
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    with Session() as s:
        assert s.get(Profile, pid).net_fails == 0


# ---- empty account list from FB must not flip everything to "no access"
@respx.mock(assert_all_called=False)
def test_transient_empty_account_list(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab")], {"act_1": []})
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    respx_mock.reset()
    mock_fb(respx_mock, [], {})
    mock_kt(respx_mock, [])
    msg = sync_all(1, "t")
    assert "kept as is" in msg
    with Session() as s:
        assert s.get(AdAccount, "act_1").missing is False


# ---- unshared account produces an alert
@respx.mock(assert_all_called=False)
def test_unshared_account_alert(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab1"), acc("act_2", "Cab2")], {"act_1": [], "act_2": []})
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    respx_mock.reset()
    mock_fb(respx_mock, [acc("act_1", "Cab1")], {"act_1": []})
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    with Session() as s:
        a = s.scalar(select(AlertLog).where(AlertLog.key.like("acc_gone:%")))
        assert a and "Cab2" in a.text


# ---- account shared between two profiles stays with the first active owner
@respx.mock(assert_all_called=False)
def test_shared_account_keeps_owner(respx_mock):
    p1 = make_profile("P1")
    p2 = make_profile("P2")
    mock_fb(respx_mock, [acc("act_1", "Shared")], {"act_1": [insight("c1", "A", 10)]})
    mock_kt(respx_mock, [])
    sync_all(1, "t")
    with Session() as s:
        assert s.get(AdAccount, "act_1").profile_id == p1
        assert len(s.scalars(select(CampaignDay)).all()) == 1
    # P1 deactivated: P2 takes over
    with Session() as s:
        s.get(Profile, p1).active = False
        s.commit()
    sync_all(1, "t")
    with Session() as s:
        assert s.get(AdAccount, "act_1").profile_id == p2


# ---- unexpected payload for one account doesn't kill the profile
@respx.mock(assert_all_called=False)
def test_bad_payload_one_account(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Bad"), acc("act_2", "Good")],
            {"act_1": [{"campaign_id": "c1", "date_start": "not-a-date", "spend": "1"}],
             "act_2": [insight("c2", "B", 5)]})
    mock_kt(respx_mock, [])
    msg = sync_all(1, "t")
    assert "errors: 1" in msg
    with Session() as s:
        assert "processing error" in s.get(AdAccount, "act_1").last_error
        assert s.scalar(select(CampaignDay).where(CampaignDay.account_id == "act_2"))
        assert s.scalar(select(Profile)).last_sync_at is not None


# ---- "no regs" alert uses the account's own time zone
def test_noreg_alert_respects_account_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pacific_today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()  # UTC+14: often "tomorrow"
    with Session() as s:
        p = Profile(name="P", token_enc=enc("x" * 40))
        s.add(p)
        s.flush()
        s.add(AdAccount(id="act_1", profile_id=p.id, name="A", status=1, currency="USD", timezone="Pacific/Kiritimati"))
        s.add(CampaignDay(day=pacific_today, campaign_id="c1", account_id="act_1", campaign_name="X", spend=100))
        s.commit()
        alerts.check_thresholds(s, {"USD": 1.0}, {"alert_spend_no_reg_usd": 30, "alert_cap_ratio": 0})
        s.commit()
        assert s.get(AlertLog, f"noreg:c1:{pacific_today}")


# ---- max instead of sum when several result action types fire
def test_count_results_takes_max(monkeypatch):
    from app import fb
    monkeypatch.setattr(fb.config, "FB_RESULT_ACTIONS", ["lead", "complete_registration"])
    assert fb.count_results([{"action_type": "lead", "value": "7"}, {"action_type": "complete_registration", "value": "7"}]) == 7


# ---- API-level: session invalidation, tg test with unsaved chat id, brute-force reset, geo group, csv sep
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "run_in_background", lambda days, trigger, ids=None: (sync.sync_all(days, trigger, ids), True)[1])
    with TestClient(main.app) as c:
        yield c


def _login(c, user="admin", pw="adminpass123"):
    r = c.post("/api/auth/login", json={"login": user, "password": pw})
    assert r.status_code == 200, r.text


def test_password_change_logs_out_other_sessions(client):
    _login(client)
    r = client.post("/api/users", json={"login": "bob", "password": "buyerpass1", "role": "viewer"})
    assert r.status_code == 200, r.text
    other = TestClient(main.app)
    _login(other, "bob", "buyerpass1")
    assert other.get("/api/auth/me").status_code == 200
    assert client.patch("/api/users/2", json={"password": "newpassw0rd"}).status_code == 200  # admin reset
    assert other.get("/api/auth/me").status_code == 401
    # own password change keeps this session
    assert client.post("/api/auth/password", json={"old_password": "adminpass123", "new_password": "adminpass456"}).status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_bruteforce_counter_resets_on_success(client):
    for _ in range(9):
        client.post("/api/auth/login", json={"login": "admin", "password": "nope"})
    _login(client)
    assert client.post("/api/auth/login", json={"login": "admin", "password": "nope"}).status_code == 401
    main.auth._fails.clear()


def test_telegram_test_uses_unsaved_chat_id(client, monkeypatch):
    _login(client)
    got = {}
    monkeypatch.setattr(main.alerts, "send_telegram", lambda chat, text: (got.setdefault("chat", chat), (True, ""))[1])
    assert client.post("/api/settings/test-telegram", json={"tg_chat_id": " -100999 "}).status_code == 200
    assert got["chat"] == "-100999"


@respx.mock(assert_all_called=False)
def test_geo_group_and_csv_sep(client, respx_mock):
    _login(client)
    mock_fb(respx_mock, [acc("act_1", "LT"), acc("act_2", "ES")],
            {"act_1": [insight("c1", "A", 10)], "act_2": [insight("c2", "B", 20)]})
    mock_kt(respx_mock, [])
    client.post("/api/profiles", json={"name": "P", "token": "EAAB" + "y" * 40})
    client.patch("/api/accounts/act_1", json={"geo": "lt"})
    client.patch("/api/accounts/act_2", json={"geo": "es"})
    st = client.get("/api/stats", params={"group": "geo"}).json()
    assert {r["key"]: r["spend"] for r in st["rows"]} == {"LT": 10, "ES": 20}
    assert client.get("/api/meta").json()["geos"] == ["ES", "LT"]
    assert client.get("/api/stats", params={"group": "campaign", "geo": "ES"}).json()["rows"][0]["key"] == "c2"
    csv = client.get("/api/stats/export.csv").text
    assert csv.startswith("\ufeffsep=,\n")


def test_fx_updated_at_is_stamped(client):
    _login(client)
    client.put("/api/settings", json={"fx_to_usd": {"EUR": 1.1}})
    assert client.get("/api/meta").json()["fx_updated_at"] == TODAY.isoformat()


def test_token_change_resyncs_and_clears_error(client):
    _login(client)
    with Session() as s:
        s.add(Profile(name="P", token_enc=enc("x" * 40), last_error="old error", token_ok=False))
        s.commit()
    with respx.mock(assert_all_called=False) as r:
        mock_fb(r, [], {})
        mock_kt(r, [])
        resp = client.patch("/api/profiles/1", json={"name": "P", "token": "EAAB" + "n" * 40})
    assert resp.json()["sync_started"] is True
    p = client.get("/api/profiles").json()[0]
    assert p["token_ok"] is True and p["last_error"] == ""


# ---- mini migration adds new columns to an old database
def test_add_missing_columns():
    from sqlalchemy import text
    from app.db import engine, init_db
    with engine.begin() as c:
        c.execute(text("ALTER TABLE profiles DROP COLUMN net_fails"))
    init_db()
    with Session() as s:
        s.add(Profile(name="x", token_enc="t"))
        s.commit()
        assert s.scalar(select(Profile)).net_fails == 0
