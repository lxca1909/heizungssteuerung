"""Passwort-Hashing (scrypt, nur Standardbibliothek) und Login-Bremse."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    # Trennzeichen "." statt "$": docker compose würde "$..." in .env als Variable ersetzen.
    return f"scrypt.{_N}.{_R}.{_P}.{salt.hex()}.{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split(".")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# Ein gültiger Dummy-Hash, damit unbekannte Benutzer gleich lange brauchen.
_DUMMY = hash_password(secrets.token_hex(8))


def check_login(users: dict[str, str], username: str, password: str) -> bool:
    stored = users.get(username)
    ok = verify_password(password, stored or _DUMMY)
    return ok and stored is not None


class LoginLimiter:
    """Max. ``limit`` Fehlversuche je IP in ``window_s`` Sekunden."""

    def __init__(self, limit: int = 5, window_s: float = 600) -> None:
        self.limit, self.window_s = limit, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._fails[ip]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, ip: str, now: float | None = None) -> bool:
        return len(self._prune(ip, now or time.time())) >= self.limit

    def failed(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
