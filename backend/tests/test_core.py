from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app import stats
from app.crypto import enc
from app.db import AdAccount, AlertLog, CampaignDay, KeitaroDay, Profile, Session, SyncRun
from app.fb import normalize_proxy
from app.sync import sync_all

G = "https://graph.facebook.com/v24.0"
KT = "https://kt.example.com/admin_api/v1/report/build"
TODAY = date.today()


def make_profile(name="Agency A login", supplier="Agency A", token="EAAB" + "x" * 40, proxy=None):
    with Session() as s:
        p = Profile(name=name, supplier=supplier, buyer="Stefan", token_enc=enc(token), proxy_enc=enc(proxy))
        s.add(p)
        s.commit()
        return p.id


def acc(id_, name, status=1, currency="USD", spent="12345", cap="0"):
    return {"id": id_, "name": name, "account_status": status, "disable_reason": 0, "currency": currency,
            "timezone_name": "Europe/Madrid", "amount_spent": spent, "spend_cap": cap, "balance": "0"}


def insight(cid, name, spend, day=TODAY, clicks=10, leads=0):
    actions = [{"action_type": "lead", "value": str(leads)}] if leads else []
    return {"campaign_id": cid, "campaign_name": name, "spend": str(spend), "impressions": "1000",
            "clicks": str(clicks), "actions": actions, "date_start": day.isoformat(), "date_stop": day.isoformat()}


def mock_fb(router, accounts, insights_by_acc, me_status=200, me_body=None):
    router.get(f"{G}/me").mock(return_value=httpx.Response(me_status, json=me_body or {"id": "1", "name": "Agency User"}))
    # paging: first page returns 1 account + next link, second page the rest
    first, rest = accounts[:1], accounts[1:]
    next_url = f"{G}/me/adaccounts?after=CUR&access_token=t"
    router.get(f"{G}/me/adaccounts", params={"after": "CUR"}).mock(
        return_value=httpx.Response(200, json={"data": rest}))
    router.get(f"{G}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": first, "paging": {"next": next_url} if rest else {}}))
    for act_id, rows in insights_by_acc.items():
        if isinstance(rows, dict):  # error
            router.get(f"{G}/{act_id}/insights").mock(return_value=httpx.Response(400, json=rows))
        else:
            router.get(f"{G}/{act_id}/insights").mock(return_value=httpx.Response(200, json={"data": rows}))


def mock_kt(router, rows):
    router.post(KT).mock(return_value=httpx.Response(200, json={"rows": rows}))


# ------------------------------------------------------------------ proxy formats
@pytest.mark.parametrize("raw,expected", [
    ("1.2.3.4:8000:user:pass", "http://user:pass@1.2.3.4:8000"),
    ("user:pass@1.2.3.4:8000", "http://user:pass@1.2.3.4:8000"),
    ("1.2.3.4:8000", "http://1.2.3.4:8000"),
    ("socks5://u:p@h:1080", "socks5://u:p@h:1080"),
    ("", None),
    (None, None),
])
def test_normalize_proxy(raw, expected):
    assert normalize_proxy(raw) == expected


def test_normalize_proxy_bad():
    with pytest.raises(ValueError):
        normalize_proxy("ftp://x")
    with pytest.raises(ValueError):
        normalize_proxy("a:b:c")


# ------------------------------------------------------------------ full sync + join
@respx.mock(assert_all_called=False)
def test_sync_and_stats(respx_mock):
    make_profile()
    mock_fb(respx_mock,
            [acc("act_1", "Cab USD"), acc("act_2", "Cab EUR", currency="EUR")],
            {"act_1": [insight("c1", "LT casino", 50, leads=3), insight("c2", "LV sport", 20)],
             "act_2": [insight("c3", "ES casino", 100)]})
    mock_kt(respx_mock, [
        {"day": TODAY.isoformat(), "sub_id_1": "c1", "clicks": 40, "leads": 5, "sales": 2, "revenue": 120},
        {"day": TODAY.isoformat(), "sub_id_1": "c3", "clicks": 80, "leads": 4, "sales": 1, "revenue": 50},
        {"day": TODAY.isoformat(), "sub_id_1": "{{campaign.id}}", "clicks": 7, "leads": 1, "sales": 0, "revenue": 0},
        {"day": TODAY.isoformat(), "sub_id_1": "zzz", "clicks": 3, "leads": 1, "sales": 1, "revenue": 60},
    ])
    msg = sync_all(3, "test")
    assert "2 accounts" in msg

    with Session() as s:
        assert {a.id for a in s.scalars(select(AdAccount))} == {"act_1", "act_2"}
        assert s.scalar(select(CampaignDay).where(CampaignDay.campaign_id == "c1")).fb_results == 3
        run = s.scalar(select(SyncRun))
        assert run.ok is True and run.finished_at

        fx = {"USD": 1.0, "EUR": 1.1}
        data = stats.build(s, TODAY - timedelta(days=2), TODAY, "campaign", fx, 1.0)
        by = {r["key"]: r for r in data["rows"]}
        assert by["c1"]["regs"] == 5 and by["c1"]["deps"] == 2 and by["c1"]["profit"] == 70
        assert by["c3"]["spend"] == pytest.approx(110)  # EUR converted
        assert by["c2"]["regs"] == 0 and by["c2"]["cpr"] is None
        tot = data["total"]
        assert tot["spend"] == pytest.approx(180) and tot["regs"] == 9 and tot["deps"] == 3
        unmatched = {u["sub_value"] for u in data["unmatched"]}
        assert unmatched == {"(empty)", "zzz"}

        acc_view = stats.build(s, TODAY - timedelta(days=2), TODAY, "account", fx, 1.0)
        a1 = next(r for r in acc_view["rows"] if r["key"] == "act_1")
        assert a1["regs"] == 5 and a1["spend"] == 70

        # filters drop unmatched and other accounts
        f = stats.build(s, TODAY, TODAY, "campaign", fx, 1.0, account_id="act_2")
        assert [r["key"] for r in f["rows"]] == ["c3"] and f["unmatched"] == []

        days = stats.daily(s, TODAY - timedelta(days=2), TODAY, fx, 1.0)
        assert len(days) == 3 and days[-1]["regs"] == 9


@respx.mock(assert_all_called=False)
def test_resync_replaces_window_not_duplicates(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab")], {"act_1": [insight("c1", "A", 10)]})
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    respx_mock.reset()
    mock_fb(respx_mock, [acc("act_1", "Cab")], {"act_1": [insight("c1", "A", 25)]})
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    with Session() as s:
        rows = s.scalars(select(CampaignDay)).all()
        assert len(rows) == 1 and rows[0].spend == 25


# ------------------------------------------------------------------ failure modes
@respx.mock(assert_all_called=False)
def test_dead_token_flags_profile_and_alerts(respx_mock):
    pid = make_profile()
    respx_mock.get(f"{G}/me").mock(return_value=httpx.Response(400, json={
        "error": {"message": "Error validating access token: Session has expired", "code": 190}}))
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    with Session() as s:
        p = s.get(Profile, pid)
        assert p.token_ok is False and "190" in p.last_error
        assert s.scalar(select(AlertLog).where(AlertLog.key.like("token:%")))
        assert s.scalar(select(SyncRun)).ok is False
    # second failure the same day doesn't create a duplicate alert
    sync_all(3, "t")
    with Session() as s:
        assert len(s.scalars(select(AlertLog).where(AlertLog.key.like("token:%"))).all()) == 1


@respx.mock(assert_all_called=False)
def test_one_bad_account_does_not_break_others(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Good"), acc("act_2", "Broken")],
            {"act_1": [insight("c1", "A", 10)],
             "act_2": {"error": {"message": "Ad account has been disabled", "code": 200}}})
    mock_kt(respx_mock, [])
    msg = sync_all(3, "t")
    assert "errors: 1" in msg
    with Session() as s:
        assert s.get(AdAccount, "act_2").last_error
        assert s.scalar(select(CampaignDay).where(CampaignDay.account_id == "act_1"))


@respx.mock(assert_all_called=False)
def test_rate_limit_is_retried(respx_mock):
    make_profile()
    respx_mock.get(f"{G}/me").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "User request limit reached", "code": 17}}),
        httpx.Response(200, json={"id": "1", "name": "U"}),
    ])
    respx_mock.get(f"{G}/me/adaccounts").mock(return_value=httpx.Response(200, json={"data": []}))
    mock_kt(respx_mock, [])
    assert "0 accounts" in sync_all(3, "t")


@respx.mock(assert_all_called=False)
def test_status_change_and_missing_account(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab1"), acc("act_2", "Cab2")], {"act_1": [], "act_2": []})
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    respx_mock.reset()
    # act_1 gets disabled, act_2 disappears (agency unshared it)
    mock_fb(respx_mock, [acc("act_1", "Cab1", status=2)], {"act_1": []})
    mock_kt(respx_mock, [])
    sync_all(3, "t")
    with Session() as s:
        assert s.get(AdAccount, "act_2").missing is True
        assert s.scalar(select(AlertLog).where(AlertLog.key.like("acc_down:act_1%")))


@respx.mock(assert_all_called=False)
def test_spend_without_regs_and_cap_alerts(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab", spent="9500", cap="10000")],
            {"act_1": [insight("c1", "Burner", 45), insight("c2", "OK", 45)]})
    mock_kt(respx_mock, [{"day": TODAY.isoformat(), "sub_id_1": "c2", "clicks": 5, "leads": 2, "sales": 0, "revenue": 0}])
    sync_all(3, "t")
    with Session() as s:
        keys = {a.key for a in s.scalars(select(AlertLog))}
        assert any(k.startswith("noreg:c1") for k in keys)
        assert not any(k.startswith("noreg:c2") for k in keys)
        assert any(k.startswith("cap:act_1") for k in keys)


@respx.mock(assert_all_called=False)
def test_keitaro_down_does_not_lose_fb_data(respx_mock):
    make_profile()
    mock_fb(respx_mock, [acc("act_1", "Cab")], {"act_1": [insight("c1", "A", 10)]})
    respx_mock.post(KT).mock(return_value=httpx.Response(502))
    msg = sync_all(3, "t")
    assert "Keitaro: error" in msg
    with Session() as s:
        assert s.scalar(select(CampaignDay))
        assert s.scalar(select(SyncRun)).ok is False


def test_stale_run_is_closed():
    from app.db import utcnow
    with Session() as s:
        s.add(SyncRun(trigger="x", started_at=utcnow() - timedelta(hours=2)))
        s.commit()
    with respx.mock(assert_all_called=False) as r:
        mock_kt(r, [])
        msg = sync_all(1, "t")
    assert "already running" not in msg
    with Session() as s:
        assert all(run.finished_at for run in s.scalars(select(SyncRun)))


def test_keitaro_day_model_roundtrip():
    with Session() as s:
        s.add(KeitaroDay(day=TODAY, sub_value="c9", regs=1))
        s.commit()
        assert s.get(KeitaroDay, (TODAY, "c9")).regs == 1
