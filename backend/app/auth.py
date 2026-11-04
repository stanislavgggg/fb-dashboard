import hashlib
import hmac
import os
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from . import config
from .db import Session, User

_ITER = 240_000


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except ValueError:
        return False


def ensure_admin():
    """Create the first admin from env if there are no users yet."""
    with Session() as s:
        if s.scalar(select(User.id).limit(1)) is not None:
            return
        if not config.ADMIN_PASSWORD:
            raise RuntimeError("No users yet and ADMIN_PASSWORD is not set — set it for the first login")
        s.add(User(login=config.ADMIN_LOGIN, password_hash=hash_password(config.ADMIN_PASSWORD), role="admin"))
        s.commit()


# naive brute-force guard: 10 failed attempts per IP per 10 minutes
_fails: dict[str, list[float]] = defaultdict(list)


def too_many_attempts(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 600]
    return len(_fails[ip]) >= 10


def note_failure(ip: str):
    _fails[ip].append(time.time())


def note_success(ip: str):
    _fails.pop(ip, None)


def current_user(request: Request) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Please log in")
    with Session() as s:
        user = s.get(User, uid)
    # session_version changes on password change / admin reset: old cookies stop working everywhere
    if user is None or request.session.get("sv") != user.session_version:
        request.session.clear()
        raise HTTPException(401, "Please log in")
    return user


def scope_buyer(user: User) -> str | None:
    """Buyer the user is restricted to, or None for full access."""
    if user.role == "admin" or not user.buyer:
        return None
    return user.buyer


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admins only")
    return user
