from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import config, health, main
from app.crypto import enc
from app.db import AdAccount, AlertLog, KeitaroDay, Profile, Session, SyncRun, set_settings, utcnow
from tests.test_core import TODAY


def by_name(r):
    return {c["name"]: c for c in r["checks"]}


def _healthy_setup():
    with Session() as s:
        p = Profile(name="P", token_enc=enc("x" * 40), token_ok=True, last_sync_at=utcnow())
        s.add(p)
        s.flush()
        s.add(AdAccount(id="act_1", profile_id=p.id, name="A", status=1, currency="EUR"))
        s.add(SyncRun(trigger="schedule", started_at=utcnow() - timedelta(minutes=5),
                      finished_at=utcnow() - timedelta(minutes=4), ok=True, message="P: 1 каб. | Keitaro: 3 строк"))
        s.add(KeitaroDay(day=TODAY, sub_value="c1", regs=1, updated_at=utcnow()))
        set_settings(s, {"fx_to_usd": {"USD": 1.0, "EUR": 1.1}, "fx_updated_at": TODAY.isoformat(),
                         "tg_chat_id": "-100"})
        s.commit()


def test_healthy(monkeypatch):
    monkeypatch.setattr(config, "TG_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    _healthy_setup()
    r = health.report(scheduler_running=True)
    assert r["status"] == "ok", health.as_text(r)
    assert "без ошибок" in by_name(r)["Синхронизация"]["text"]


def test_problems_are_detected(monkeypatch):
    monkeypatch.setattr(config, "TG_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    foreign = Fernet(Fernet.generate_key()).encrypt(b"x" * 40).decode()
    with Session() as s:
        s.add(Profile(name="Dead", token_enc=enc("x" * 40), token_ok=False, last_sync_at=utcnow()))
        s.add(Profile(name="Proxy", token_enc=enc("x" * 40), token_ok=True, net_fails=3, last_sync_at=utcnow()))
        s.add(Profile(name="Rotated", token_enc=foreign))
        s.add(SyncRun(trigger="schedule", started_at=utcnow() - timedelta(hours=2)))  # stuck
        s.add(SyncRun(trigger="schedule", started_at=utcnow() - timedelta(hours=5),
                      finished_at=utcnow() - timedelta(hours=5), ok=False, message="Keitaro: ошибка — 502"))
        s.add(AlertLog(key="old", text="x", sent=False, created_at=utcnow() - timedelta(hours=3)))
        set_settings(s, {"tg_chat_id": "-100"})
        s.commit()
    r = health.report(scheduler_running=False)
    c = by_name(r)
    assert r["status"] == "fail"
    assert c["Планировщик"]["level"] == "fail"
    assert c["Синхронизация"]["level"] == "fail" and "висит" in c["Синхронизация"]["text"]
    assert c["Ключ шифрования"]["level"] == "fail"
    assert c["Профили"]["level"] == "fail"  # every active profile is broken
    assert c["Keitaro"]["level"] == "fail"
    assert c["Алерты"]["level"] == "fail"
    text = health.as_text(r)
    assert "[FAIL]" in text and "Rotated" in text


def test_missing_fx_and_stale_sync(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    with Session() as s:
        p = Profile(name="P", token_enc=enc("x" * 40), token_ok=True, last_sync_at=utcnow())
        s.add(p)
        s.flush()
        s.add(AdAccount(id="act_1", profile_id=p.id, name="A", status=1, currency="PLN"))
        s.add(SyncRun(trigger="schedule", started_at=utcnow() - timedelta(minutes=90),
                      finished_at=utcnow() - timedelta(minutes=89), ok=True, message=""))
        s.commit()
    c = by_name(health.report(scheduler_running=True))
    assert c["Курсы валют"]["level"] == "warn" and "PLN" in c["Курсы валют"]["text"]
    assert c["Синхронизация"]["level"] == "warn"


def test_cli_exit_code(monkeypatch, capsys):
    import runpy, sys
    _healthy_setup()
    monkeypatch.setattr(sys, "argv", ["health"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("app.health", run_name="__main__")
    out = capsys.readouterr().out
    assert "FB-дашборд:" in out and e.value.code in (0, 1)


def test_endpoint_auth_and_formats(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_TOKEN", "secret-health")
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"X-Health-Token": "wrong"}).status_code == 401
        r = c.get("/api/health", headers={"X-Health-Token": "secret-health"})
        assert r.status_code in (200, 503) and "checks" in r.json()
        t = c.get("/api/health", params={"format": "text", "token": "secret-health"})
        assert t.text.startswith("FB-дашборд:")
        # admin session works without a token
        c.post("/api/auth/login", json={"login": "admin", "password": "adminpass123"})
        assert c.get("/api/health").status_code in (200, 503)


def test_endpoint_503_on_fail(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_TOKEN", "t0k")
    with Session() as s:
        s.add(Profile(name="Rotated", token_enc=Fernet(Fernet.generate_key()).encrypt(b"x" * 40).decode()))
        s.commit()
    with TestClient(main.app) as c:
        assert c.get("/api/health", headers={"X-Health-Token": "t0k"}).status_code == 503
