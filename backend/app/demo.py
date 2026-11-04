"""Fill the database with fake data to look at the UI without real tokens.

python -m app.demo      (never run against production)
"""
import random
from datetime import date, timedelta

from .crypto import enc
from .db import AdAccount, AlertLog, CampaignDay, KeitaroDay, Profile, Session, SyncRun, init_db, utcnow


def run():
    init_db()
    rnd = random.Random(7)
    today = date.today()
    with Session() as s:
        if s.query(Profile).count():
            print("The database already has profiles — demo not added")
            return
        suppliers = [("Agency Alpha", "Stefan"), ("Agency Alpha", "Stefan"), ("Nova Accounts", "Stefan"), ("Kings Ads", "Milos")]
        geos = ["LT", "LV", "ES", "HR", "RS"]
        n = 0
        for i, (sup, buyer) in enumerate(suppliers, 1):
            p = Profile(name=f"{sup} — login {i}", supplier=sup, buyer=buyer, token_enc=enc("EAABdemo" + "x" * 40),
                        proxy_enc=enc(f"http://u:p@10.0.0.{i}:8000"), token_ok=i != 4, fb_user=f"Agency user {i}",
                        last_sync_at=utcnow(), last_error="[190] Error validating access token: Session has expired" if i == 4 else "")
            s.add(p)
            s.flush()
            for j in range(rnd.randint(2, 4)):
                n += 1
                status = 1 if rnd.random() > 0.2 else 2
                cur = "EUR" if rnd.random() > 0.6 else "USD"
                acc = AdAccount(id=f"act_{1000 + n}", profile_id=p.id, name=f"{sup.split()[0]} {geos[n % 5]} #{j + 1}",
                                geo=geos[n % 5], status=status, currency=cur, timezone="Europe/Madrid",
                                amount_spent=rnd.randint(300, 9000), spend_cap=rnd.choice([0, 5000, 10000]))
                s.add(acc)
                for c in range(rnd.randint(1, 3)):
                    cid = f"2385{n:03d}{c}"
                    quality = rnd.uniform(0.3, 2.2)
                    for d in range(30):
                        day = today - timedelta(days=d)
                        if status != 1 and d < 3:
                            continue
                        spend = rnd.uniform(15, 140)
                        clicks = int(spend * rnd.uniform(2, 5))
                        s.add(CampaignDay(day=day, campaign_id=cid, account_id=acc.id,
                                          campaign_name=f"{acc.geo} casino {['broad', 'lal 3%', 'interests'][c]}",
                                          spend=spend, impressions=clicks * rnd.randint(30, 80), clicks=clicks,
                                          fb_results=int(clicks * 0.08)))
                        regs = int(clicks * rnd.uniform(0.02, 0.09) * quality)
                        deps = int(regs * rnd.uniform(0.15, 0.4))
                        s.add(KeitaroDay(day=day, sub_value=cid, clicks=int(clicks * 0.9), regs=regs, deps=deps,
                                         revenue=deps * rnd.uniform(40, 110)))
        s.add(KeitaroDay(day=today, sub_value="(empty)", clicks=40, regs=6, deps=2, revenue=150))
        s.add(SyncRun(trigger="schedule", days=3, started_at=utcnow() - timedelta(minutes=12),
                      finished_at=utcnow() - timedelta(minutes=11), ok=False,
                      message="Agency Alpha — login 1: 3 accounts, 9 rows | Kings Ads — login 4: Error validating access token | Keitaro: 120 rows over 3 days"))
        s.add(AlertLog(key="demo1", level="crit", text="🔑 Token of profile <b>Kings Ads — login 4</b> stopped working"))
        s.add(AlertLog(key="demo2", level="warn", text="🟠 <b>LT casino broad</b>: spent $62 today, 0 registrations"))
        s.commit()
    print(f"Demo ready: {n} ad accounts")


if __name__ == "__main__":
    run()
