from datetime import datetime, timezone
from pathlib import Path

import pytest

from werker import cli
from werker.auth import password_secret_key, verify_password
from werker.clients import normalize
from werker.connectors.mail import IMAPConnector
from werker.models import Message
from werker.runtime import Runtime
from werker.store import Store

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Een lege datamap; de omgeving bevat zelf geen mailboxen of sleutels."""
    for key in ("ANTHROPIC_API_KEY", "WERKER_MAILBOXES", "WERKER_IMAP_HOST", "WERKER_GMAIL_TOKEN",
                "WERKER_OUTLOOK_CLIENT_ID", "WHATSAPP_TOKEN", "WERKER_PRICES_CSV_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WERKER_DB", str(tmp_path / "werker.db"))
    monkeypatch.setenv("WERKER_PRICES_FILE", str(EXAMPLE))
    monkeypatch.setenv("WERKER_IMESSAGE", "false")
    return tmp_path


def _runtime(home: Path) -> Runtime:
    import os

    return Runtime(Store(str(home / "werker.db")), home, os.environ)


def test_sync_uses_mailbox_from_the_settings_screen(home, monkeypatch, capsys):
    _runtime(home).update(mailboxes=[{
        "name": "winkel", "host": "imap.winkel.nl", "user": "info", "password": "geheim123",
        "smtp_host": "smtp.winkel.nl", "sender": "info@winkel.nl",
    }])
    fetched = []

    def fake_fetch(self):
        fetched.append((self.name, self._host, self._password))
        return [Message("mail:winkel", "<1@x>", "<1@x>", "klant@x.nl", "Prijs?", "Wat kost het?",
                        datetime.now(timezone.utc))]

    monkeypatch.setattr(IMAPConnector, "fetch_new", fake_fetch)

    assert cli.main(["sync"]) == 0

    assert fetched == [("mail:winkel", "imap.winkel.nl", "geheim123")]
    captured = capsys.readouterr()
    assert "nieuwe berichten verwerkt" in captured.out
    # Zonder API-sleutel draait de sync wel, maar classificeren lukt niet: dat wordt gemeld.
    assert "API-sleutel ontbreekt" in captured.err


def test_reject_unknown_draft_reports_error_without_api_key(home, capsys):
    assert cli.main(["reject", "999"]) == 1
    assert "bestaat niet" in capsys.readouterr().err


def test_approve_unknown_draft_reports_error(home, capsys):
    assert cli.main(["approve", "999"]) == 1


# ---------- telefoonnummers ----------


def test_dutch_national_and_international_numbers_match():
    expected = ("phone", "31612345678")
    assert normalize("0612345678") == expected
    assert normalize("+31 6 12345678") == expected
    assert normalize("0031 6 1234 5678") == expected
    assert normalize("06-12345678") == expected


def test_other_country_codes_are_unchanged():
    assert normalize("+32 470 12 34 56") == ("phone", "32470123456")
    assert normalize("+31 20 123 4567") == ("phone", "31201234567")
    assert normalize("020 123 4567") == ("phone", "31201234567")


# ---------- wachtwoord opnieuw instellen ----------


def _with_user(home: Path) -> Runtime:
    runtime = _runtime(home)
    runtime.update(users=[{"name": "anna", "role": "beheerder", "password": "oud-wachtwoord"}])
    return runtime


def _answers(monkeypatch, *answers):
    queue = list(answers)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": queue.pop(0))


def test_reset_password_stores_new_hash(home, monkeypatch, capsys):
    runtime = _with_user(home)
    _answers(monkeypatch, "nieuw-wachtwoord", "nieuw-wachtwoord")

    assert cli.main(["reset-password", "anna"]) == 0

    stored = runtime.secret_value(password_secret_key("anna"))
    assert verify_password("nieuw-wachtwoord", stored)
    assert not verify_password("oud-wachtwoord", stored)
    assert "gewijzigd" in capsys.readouterr().out


def test_reset_password_refuses_unknown_user(home, monkeypatch, capsys):
    runtime = _with_user(home)
    before = runtime.secret_value(password_secret_key("anna"))
    _answers(monkeypatch)  # er mag niet eens om een wachtwoord gevraagd worden

    assert cli.main(["reset-password", "bob"]) == 1

    assert "bestaat niet" in capsys.readouterr().err
    assert runtime.secret_value(password_secret_key("bob")) == ""
    assert runtime.secret_value(password_secret_key("anna")) == before


def test_reset_password_refuses_short_password(home, monkeypatch, capsys):
    runtime = _with_user(home)
    before = runtime.secret_value(password_secret_key("anna"))
    _answers(monkeypatch, "kort", "kort")

    assert cli.main(["reset-password", "anna"]) == 1

    assert "minstens 8" in capsys.readouterr().err
    assert runtime.secret_value(password_secret_key("anna")) == before


def test_reset_password_refuses_different_passwords(home, monkeypatch):
    runtime = _with_user(home)
    before = runtime.secret_value(password_secret_key("anna"))
    _answers(monkeypatch, "nieuw-wachtwoord", "ander-wachtwoord")

    assert cli.main(["reset-password", "anna"]) == 1
    assert runtime.secret_value(password_secret_key("anna")) == before
