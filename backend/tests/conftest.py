import os
import sys
import tempfile

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "SECRET_KEY": Fernet.generate_key().decode(),
    "ADMIN_LOGIN": "admin",
    "ADMIN_PASSWORD": "adminpass123",
    "SCHEDULER_ENABLED": "0",
    "COOKIE_SECURE": "0",
    "KEITARO_URL": "https://kt.example.com",
    "KEITARO_API_KEY": "kkey",
    "KEITARO_CAMPAIGN_SUB": "sub_id_1",
    "TG_BOT_TOKEN": "",
    "FB_API_VERSION": "v24.0",
})
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from app import fb, fx, sync  # noqa: E402
from app.db import Base, engine, init_db  # noqa: E402

fb._sleep = lambda *_: None
sync._pause = lambda *_: None
fx._real_fetch = fx.fetch
fx.fetch = lambda currencies: {}  # no network in tests; test_scope_fx restores _real_fetch where needed


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield
