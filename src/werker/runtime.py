"""Instellingen en de draaiende app: alles wat op het instelscherm kan worden aangepast.

Waarden komen uit drie plaatsen, in deze volgorde (later wint):
1. de omgeving van het proces (bijvoorbeeld Docker-variabelen);
2. secrets.json in de datamap: API-sleutels en mailbox-wachtwoorden, alleen leesbaar voor jou;
3. de instellingen in de database: alles wat niet geheim is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import anthropic

from werker.classify import Classifier
from werker.cli import build_connectors, price_loader
from werker.config import Settings
from werker.pipeline import Pipeline
from werker.prices import PriceBook
from werker.invoicing import Invoicing
from werker.reply import Replier
from werker.store import Store

log = logging.getLogger(__name__)

SECRET_KEYS = ("ANTHROPIC_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")
SETTING_KEYS = (
    "WERKER_AUTOSEND",
    "WERKER_AUTOSEND_INTENTS",
    "WERKER_MIN_CONFIDENCE",
    "WERKER_MAX_AUTO_AMOUNT",
    "WERKER_OWN_ADDRESSES",
    "WERKER_BLOCKLIST",
    "WERKER_SYNC_MINUTES",
    "WERKER_PRICES_CSV_URL",
    "WERKER_IMESSAGE",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WERKER_MAILBOXES",
)
DEFAULTS = {
    "WERKER_AUTOSEND": "false",
    "WERKER_AUTOSEND_INTENTS": "prijsvraag",
    "WERKER_MIN_CONFIDENCE": "0.85",
    "WERKER_MAX_AUTO_AMOUNT": "500",
    "WERKER_OWN_ADDRESSES": "",
    "WERKER_BLOCKLIST": "",
    "WERKER_SYNC_MINUTES": "5",
    "WERKER_PRICES_CSV_URL": "",
    "WERKER_IMESSAGE": "true",
    "WHATSAPP_PHONE_NUMBER_ID": "",
    "WERKER_MAILBOXES": "[]",
}
MAILBOX_FIELDS = ("name", "host", "user", "smtp_host", "sender")


class Unconfigured:
    """Stand-in voor de AI zolang er geen API-sleutel is. Zo start de app wel, zodat je de instellingen kunt invullen."""

    def __init__(self, reason: str):
        self._reason = reason

    def classify(self, msg):
        raise RuntimeError(self._reason)

    def draft(self, msg, cls, prices):
        raise RuntimeError(self._reason)

    def revise(self, msg, current, prices):
        raise RuntimeError(self._reason)


def _validate_values(values: Mapping[str, Any]) -> None:
    """Controleer getallen en de aan/uit-schakelaars voordat ze worden opgeslagen."""
    checks = {
        "WERKER_MIN_CONFIDENCE": lambda v: 0 <= float(v) <= 1,
        "WERKER_MAX_AUTO_AMOUNT": lambda v: float(v) >= 0,
        "WERKER_SYNC_MINUTES": lambda v: float(v) >= 0,
        "WERKER_AUTOSEND": lambda v: str(v) in ("true", "false"),
        "WERKER_IMESSAGE": lambda v: str(v) in ("true", "false"),
    }
    for key, value in values.items():
        check = checks.get(key)
        if value is None or check is None:
            continue
        try:
            valid = check(value)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f"ongeldige waarde voor {key}: {value!r}")


def mailbox_secret_key(name: str) -> str:
    """Unieke sleutel per mailbox: de naam wordt opgeschoond, plus een korte hash zodat 'a.b' en 'a_b' niet botsen."""
    readable = re.sub(r"\W", "_", name).upper()
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"MAILBOX_{readable}_{digest}_PW"


class Runtime:
    def __init__(self, store: Store, home: Path, base_env: Mapping[str, str]):
        self.store = store
        self.home = home
        self._base = dict(base_env)
        self.pipeline: Pipeline | None = None
        self.invoicing = Invoicing(store, home / "peppol-map")
        self._lock = threading.Lock()

    # ---------- opslag ----------

    @property
    def _secrets_path(self) -> Path:
        return self.home / "secrets.json"

    def _read_secrets(self) -> dict[str, str]:
        if not self._secrets_path.exists():
            return {}
        return json.loads(self._secrets_path.read_text(encoding="utf-8"))

    def _write_secrets(self, data: dict[str, str]) -> None:
        self._secrets_path.parent.mkdir(parents=True, exist_ok=True)
        # Het bestand krijgt meteen de juiste rechten, zodat het nooit even open te lezen is.
        descriptor = os.open(self._secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2))
        try:
            os.chmod(self._secrets_path, 0o600)
        except OSError:
            pass  # Windows kent geen Unix-rechten; de datamap is daar al van de gebruiker.

    def env(self) -> dict[str, str]:
        merged = dict(self._base)
        merged.update({k: v for k, v in self._read_secrets().items() if v})
        merged.update({k: v for k, v in self.store.get_settings().items()})
        return merged

    def sync_minutes(self) -> float:
        return float(self.env().get("WERKER_SYNC_MINUTES", DEFAULTS["WERKER_SYNC_MINUTES"]) or 0)

    # ---------- weergave ----------

    def settings_view(self) -> dict[str, Any]:
        env = self.env()
        stored = self.store.get_settings()
        values = {key: stored.get(key, self._base.get(key, DEFAULTS[key])) for key in SETTING_KEYS if key != "WERKER_MAILBOXES"}
        secrets = self._read_secrets()
        mailboxes = []
        for entry in json.loads(env.get("WERKER_MAILBOXES", "[]") or "[]"):
            key = entry.get("password_env", "")
            mailboxes.append({**{f: entry.get(f, "") for f in MAILBOX_FIELDS},
                              "port": entry.get("port", 993), "smtp_port": entry.get("smtp_port", 465),
                              "folder": entry.get("folder", "INBOX"),
                              "password_set": bool(secrets.get(key) or env.get(key))})
        return {
            "values": values,
            "mailboxes": mailboxes,
            "secrets_set": {key: bool(env.get(key)) for key in SECRET_KEYS},
            "peppol_map": str(self.invoicing.folder),
            "peppol_connected": self.invoicing.access_point is not None,
        }

    # ---------- wijzigen ----------

    def update(
        self,
        values: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
        mailboxes: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            # Eerst alles controleren, dan pas opslaan: een fout laat de instellingen ongemoeid.
            values = dict(values or {})
            unknown = set(values) - set(SETTING_KEYS)
            if unknown:
                raise ValueError(f"onbekende instelling: {', '.join(sorted(unknown))}")
            if set(secrets or {}) - set(SECRET_KEYS):
                raise ValueError(f"onbekend geheim: {', '.join(sorted(set(secrets) - set(SECRET_KEYS)))}")
            _validate_values(values)
            stored_secrets = self._read_secrets()
            for key, value in (secrets or {}).items():
                if value:
                    stored_secrets[key] = value
                else:
                    stored_secrets.pop(key, None)
            cleaned_mailboxes = None
            if mailboxes is not None:
                cleaned_mailboxes = self._clean_mailboxes(mailboxes, stored_secrets)
            self.store.set_settings({k: None if v is None else str(v) for k, v in values.items()})
            if cleaned_mailboxes is not None:
                self.store.set_settings({"WERKER_MAILBOXES": json.dumps(cleaned_mailboxes)})
            self._write_secrets(stored_secrets)
        self.reconfigure()

    def _clean_mailboxes(self, mailboxes: list[dict[str, Any]], secrets: dict[str, str]) -> list[dict[str, Any]]:
        cleaned = []
        names = set()
        for entry in mailboxes:
            for field in ("name", "host", "user", "smtp_host", "sender"):
                if not str(entry.get(field, "")).strip():
                    raise ValueError(f"mailbox: veld '{field}' ontbreekt")
            name = re.sub(r"\s+", "-", str(entry["name"]).strip().lower())
            if name in names:
                raise ValueError(f"mailbox '{name}' staat twee keer in de lijst")
            names.add(name)
            key = mailbox_secret_key(name)
            password = entry.get("password", "")
            if password:
                secrets[key] = password
            cleaned.append(
                {
                    "name": name,
                    "host": str(entry["host"]).strip(),
                    "user": str(entry["user"]).strip(),
                    "password_env": key,
                    "smtp_host": str(entry["smtp_host"]).strip(),
                    "sender": str(entry["sender"]).strip(),
                    "port": int(entry.get("port") or 993),
                    "smtp_port": int(entry.get("smtp_port") or 465),
                    "folder": str(entry.get("folder") or "INBOX"),
                }
            )
        keep = {c["password_env"] for c in cleaned}
        for key in [k for k in secrets if k.startswith("MAILBOX_") and k not in keep]:
            secrets.pop(key)
        return cleaned

    # ---------- de app bouwen en bijwerken ----------

    def _components(self) -> dict[str, Any]:
        env = self.env()
        settings = Settings.from_env(env)
        api_key = env.get("ANTHROPIC_API_KEY", "")
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
            classifier, replier = Classifier(client), Replier(client)
        else:
            reason = "De API-sleutel ontbreekt. Vul die in bij Instellingen."
            classifier, replier = Unconfigured(reason), Unconfigured(reason)
        prices = PriceBook(price_loader(settings))
        return {
            "classifier": classifier,
            "replier": replier,
            "prices": prices,
            "policy": settings.policy,
            "rules": settings.rules,
            "connectors": build_connectors(env),
        }

    def reconfigure(self) -> None:
        components = self._components()
        if self.pipeline is None:
            self.pipeline = Pipeline(self.store, **components)
        else:
            self.pipeline.reconfigure(**components)
        self.pipeline.prices.refresh()


def run_background(runtime: Runtime, stop: threading.Event, tick_seconds: float = 30) -> threading.Thread:
    """Verstuur geplande berichten en haal nieuwe berichten op, volgens de ingestelde interval."""

    def loop() -> None:
        last_sync = 0.0
        while not stop.wait(tick_seconds):
            try:
                if runtime.pipeline is None:
                    continue
                runtime.invoicing.dispatch()
                minutes = runtime.sync_minutes()
                if minutes > 0 and time.monotonic() - last_sync >= minutes * 60:
                    runtime.pipeline.sync()
                    last_sync = time.monotonic()
                else:
                    runtime.pipeline.send_due()
            except Exception:
                log.exception("achtergrondtaak mislukt; het wordt opnieuw geprobeerd")

    thread = threading.Thread(target=loop, name="werker-achtergrond", daemon=True)
    thread.start()
    return thread
