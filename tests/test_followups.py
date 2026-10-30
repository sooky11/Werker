import argparse

from werker import cli
from werker.clients import migrate_stored_phones
from werker.store import Store


def test_old_dutch_numbers_are_converted_on_migration(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    client = store.create_client("Jan", [("phone", "0612345678")])
    counts = migrate_stored_phones(store)
    assert counts["omgezet"] == 1
    assert store.find_client_by_identifier("phone", "31612345678") == client
    assert store.find_client_by_identifier("phone", "0612345678") is None
    assert migrate_stored_phones(store)["omgezet"] == 0  # tweede keer: niets meer te doen
    store.close()


def test_duplicate_number_of_same_client_is_merged(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    client = store.create_client("Jan", [("phone", "0612345678"), ("phone", "31612345678")])
    counts = migrate_stored_phones(store)
    assert counts["samengevoegd"] == 1
    assert [i["value"] for i in store.client_identifiers(client)] == ["31612345678"]
    store.close()


def test_number_shared_by_two_clients_is_left_alone(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    first = store.create_client("Jan", [("phone", "0612345678")])
    second = store.create_client("Jan B.", [("phone", "31612345678")])
    counts = migrate_stored_phones(store)
    assert counts["botsing"] == 1
    assert store.find_client_by_identifier("phone", "0612345678") == first
    assert store.find_client_by_identifier("phone", "31612345678") == second
    store.close()


def test_serve_trusts_only_configured_proxy(tmp_path, monkeypatch):
    seen = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setattr("werker.runtime.run_background", lambda runtime, stop: None)
    monkeypatch.setenv("WERKER_TRUSTED_PROXIES", "10.0.0.5")
    monkeypatch.setenv("WERKER_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WERKER_PRICES_FILE", "prices.example.yaml")
    args = argparse.Namespace(host="127.0.0.1", port=8123)
    assert cli.cmd_serve(cli.Settings.from_env(), args) == 0
    assert seen["proxy_headers"] is True
    assert seen["forwarded_allow_ips"] == "10.0.0.5"


def test_serve_defaults_to_trusting_localhost_proxy_only(tmp_path, monkeypatch):
    seen = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    monkeypatch.setattr("werker.runtime.run_background", lambda runtime, stop: None)
    monkeypatch.delenv("WERKER_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("WERKER_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("WERKER_PRICES_FILE", "prices.example.yaml")
    assert cli.cmd_serve(cli.Settings.from_env(), argparse.Namespace(host="127.0.0.1", port=8124)) == 0
    assert seen["forwarded_allow_ips"] == "127.0.0.1"
