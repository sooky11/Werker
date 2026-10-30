"""Inloggen voor de webapp.

Zonder teamaccounts geldt het gedeelde wachtwoord uit WERKER_UI_PASSWORD, zoals voorheen. Zodra er accounts zijn,
logt iedereen in met een eigen naam en wachtwoord. Een beheerder mag instellingen en gebruikers wijzigen; een
medewerker mag berichten lezen en concepten goed- of afkeuren.

Accounts (naam en rol) staan in de instellingen onder WERKER_USERS. De wachtwoorden staan als hash in secrets.json.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

ROLES = ("beheerder", "medewerker")
USERS_KEY = "WERKER_USERS"
ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")

# Geslaagde inlogpogingen onthouden we even, zodat elke API-aanroep niet opnieuw 200.000 hashrondes kost.
# De sleutel bevat de opgeslagen hash, dus na een wachtwoordwijziging geldt de oude combinatie niet meer.
_CACHE_KEY = secrets.token_bytes(32)
_CACHE_LIMIT = 512
_verified: set[str] = set()
_verified_lock = threading.Lock()


# Na MAX_FAILURES mislukte pogingen binnen FAILURE_WINDOW seconden weigert de app verdere pogingen van hetzelfde IP-adres.
MAX_FAILURES = 5
FAILURE_WINDOW = 600
_THROTTLE_LIMIT = 10_000


class LoginThrottle:
    """Telt mislukte inlogpogingen per IP-adres, in het geheugen en met een vaste bovengrens aan adressen."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window: float = FAILURE_WINDOW,
        limit: int = _THROTTLE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window = window
        self.limit = limit
        self.clock = clock
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        times = [t for t in self._failures.get(ip, ()) if now - t < self.window]
        if times:
            self._failures[ip] = times
        else:
            self._failures.pop(ip, None)
        return times

    def retry_after(self, ip: str) -> int:
        """Seconden tot dit adres weer mag proberen; 0 als het nu mag."""
        with self._lock:
            now = self.clock()
            times = self._recent(ip, now)
            if len(times) < self.max_failures:
                return 0
            # Het venster schuift: zodra de oudste van de laatste pogingen verloopt, mag er weer één bij.
            return max(1, int(times[-self.max_failures] + self.window - now + 0.999))

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            times = self._recent(ip, now)
            times.append(now)
            self._failures[ip] = times
            self._failures.move_to_end(ip)
            if len(self._failures) > self.limit:
                for key in [k for k, v in self._failures.items() if now - v[-1] >= self.window]:
                    del self._failures[key]
                while len(self._failures) > self.limit:
                    self._failures.popitem(last=False)


def _client_ip(request: Request) -> str:
    # Bewust niet X-Forwarded-For: die kan elke bezoeker zelf invullen.
    return request.client.host if request.client else "onbekend"


def _too_many(seconds: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"te veel mislukte inlogpogingen, probeer het over {max(1, (seconds + 59) // 60)} minuten opnieuw",
        headers={"Retry-After": str(seconds)},
    )


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # Voor onbekende gebruikersnamen rekenen we ook een hash, zodat de wachttijd niet verraadt of een naam bestaat.
    return hash_password("niet-een-echt-wachtwoord")


def password_secret_key(name: str) -> str:
    return "USER_" + re.sub(r"\W", "_", name).upper() + "_PW"


def load_accounts(store: Any) -> list[dict[str, str]]:
    raw = store.get_settings().get(USERS_KEY, "")
    if not raw:
        return []
    return [{"name": str(entry["name"]), "role": str(entry["role"])} for entry in json.loads(raw)]


def accounts_configured(store: Any) -> bool:
    return bool(load_accounts(store))


def user_role(store: Any, username: str | None) -> str | None:
    for account in load_accounts(store):
        if account["name"] == username:
            return account["role"]
    return None


def clean_accounts(entries: list[dict[str, Any]], secrets_map: dict[str, str]) -> list[dict[str, str]]:
    """Controleer de gewenste accountlijst en zet nieuwe of gewijzigde wachtwoorden om in hashes.

    secrets_map wordt aangepast: nieuwe hashes erin, wachtwoorden van verwijderde gebruikers eruit.
    Een gebruiker die nog niet bestaat, heeft een wachtwoord nodig.
    """
    cleaned: list[dict[str, str]] = []
    names: set[str] = set()
    keys: set[str] = set()
    for entry in entries:
        name = str(entry.get("name", "")).strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError(f"ongeldige gebruikersnaam {name!r}: gebruik 2 tot 32 letters, cijfers, punten, streepjes of underscores")
        if name in names:
            raise ValueError(f"gebruiker '{name}' staat twee keer in de lijst")
        role = str(entry.get("role", ""))
        if role not in ROLES:
            raise ValueError(f"ongeldige rol voor '{name}': {role!r}")
        key = password_secret_key(name)
        if key in keys:
            raise ValueError(f"gebruikersnaam '{name}' lijkt te veel op een andere gebruiker")
        names.add(name)
        keys.add(key)
        password = str(entry.get("password") or "")
        if password:
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"het wachtwoord van '{name}' moet minstens {MIN_PASSWORD_LENGTH} tekens zijn")
            secrets_map[key] = hash_password(password)
        elif not secrets_map.get(key):
            raise ValueError(f"gebruiker '{name}' heeft een wachtwoord nodig")
        cleaned.append({"name": name, "role": role})
    for key in [k for k in secrets_map if k.startswith("USER_") and k.endswith("_PW") and k not in keys]:
        secrets_map.pop(key)
    if not any(account["role"] == "beheerder" for account in cleaned):
        raise ValueError("er moet minstens één beheerder zijn")
    return cleaned


def _authenticate(runtime: Any, credentials: HTTPBasicCredentials | None) -> str | None:
    if credentials is None:
        return None
    name = credentials.username.strip().lower()
    password = credentials.password
    known = any(account["name"] == name for account in load_accounts(runtime.store))
    stored = runtime.secret_value(password_secret_key(name)) if known else ""
    if not stored:
        verify_password(password, _dummy_hash())
        return None
    cache_key = hmac.new(_CACHE_KEY, f"{name}\0{password}\0{stored}".encode("utf-8"), hashlib.sha256).hexdigest()
    with _verified_lock:
        if cache_key in _verified:
            return name
    if not verify_password(password, stored):
        return None
    with _verified_lock:
        if len(_verified) >= _CACHE_LIMIT:
            _verified.clear()
        _verified.add(cache_key)
    return name


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def make_auth(
    runtime: Any, legacy_password: str, throttle: LoginThrottle | None = None
) -> Callable[..., str | None]:
    """FastAPI-afhankelijkheid die de ingelogde gebruikersnaam teruggeeft (None zonder accounts).

    Na te veel mislukte pogingen vanaf één IP-adres volgt een 429 met Retry-After, ook bij het juiste wachtwoord.
    """
    basic = HTTPBasic(auto_error=False)
    throttle = throttle or LoginThrottle()

    def check(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)) -> str | None:
        accounts = accounts_configured(runtime.store)
        if not accounts and not legacy_password:
            return None
        ip = _client_ip(request)
        wait = throttle.retry_after(ip)
        if wait:
            raise _too_many(wait)
        if accounts:
            username = _authenticate(runtime, credentials)
            if username is None:
                if credentials is not None:
                    throttle.record_failure(ip)
                raise _unauthorized()
            return username
        if credentials is None:
            raise _unauthorized()
        if not secrets.compare_digest(credentials.password.encode(), legacy_password.encode()):
            throttle.record_failure(ip)
            raise _unauthorized()
        return None

    return check


def require_beheerder(
    runtime: Any, username: str | None, detail: str = "alleen een beheerder kan instellingen wijzigen"
) -> None:
    """Alleen beheerders mogen instellingen en gebruikers wijzigen. Zonder accounts geldt het oude gedrag."""
    if not accounts_configured(runtime.store):
        return
    if user_role(runtime.store, username) != "beheerder":
        raise HTTPException(status_code=403, detail=detail)
