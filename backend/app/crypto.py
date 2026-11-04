from cryptography.fernet import Fernet, InvalidToken

from . import config

if not config.SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet, InvalidToken; print(Fernet.generate_key().decode())\""
    )

try:
    _f = Fernet(config.SECRET_KEY.encode())
except (ValueError, TypeError) as e:
    raise RuntimeError("SECRET_KEY has a wrong format: it must be a Fernet key (44 base64 characters)") from e

KEY_CHANGED = "ключ шифрования изменён — введи заново"


class KeyChanged(Exception):
    """Stored value was encrypted with a different SECRET_KEY."""


def enc(value: str | None) -> str | None:
    return _f.encrypt(value.encode()).decode() if value else None


def dec(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _f.decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise KeyChanged(KEY_CHANGED) from e


def mask(value: str | None) -> str:
    if not value:
        return "—"
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "••••"
