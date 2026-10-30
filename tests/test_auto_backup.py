import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from werker.backup import SECRETS_TABLE, auto_backup, auto_backups
from werker.runtime import Runtime
from werker.store import Store

START = datetime(2026, 11, 1, 3, 0, 0)


@pytest.fixture
def runtime(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    (tmp_path / "secrets.json").write_text(json.dumps({"ANTHROPIC_API_KEY": "sk-geheim"}), encoding="utf-8")
    rt = Runtime(db, tmp_path, {"WERKER_IMESSAGE": "false"})
    yield rt
    db.close()


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
    finally:
        connection.close()


def test_first_backup_is_made_and_a_second_waits_for_the_interval(runtime, tmp_path):
    folder = tmp_path / "backups"
    first = auto_backup(runtime.store.backup_into, folder, hours=24, keep=14, now=START)
    assert first is not None and first.name == "werker-auto-20261101-030000.db"
    assert "messages" in _tables(first) and SECRETS_TABLE not in _tables(first)
    assert b"sk-geheim" not in first.read_bytes()
    assert auto_backup(runtime.store.backup_into, folder, 24, 14, now=START + timedelta(hours=23)) is None
    assert auto_backup(runtime.store.backup_into, folder, 24, 14, now=START + timedelta(hours=24)) is not None
    assert len(auto_backups(folder)) == 2


def test_only_the_newest_backups_are_kept_and_other_files_are_left_alone(runtime, tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    own = folder / "mijn-eigen-kopie.db"
    own.write_bytes(b"x")
    for day in range(5):
        auto_backup(runtime.store.backup_into, folder, 24, keep=3, now=START + timedelta(days=day))
    names = [p.name for p in auto_backups(folder)]
    assert names == [f"werker-auto-202611{d:02d}-030000.db" for d in (3, 4, 5)]
    assert own.exists()


def test_zero_hours_turns_automatic_backups_off(runtime, tmp_path):
    assert auto_backup(runtime.store.backup_into, tmp_path / "backups", hours=0, keep=14, now=START) is None
    assert not (tmp_path / "backups").exists()


def test_runtime_uses_the_settings_and_logs_the_backup(runtime, tmp_path):
    created = runtime.auto_backup()
    assert created is not None and created.parent == tmp_path / "backups"
    assert runtime.auto_backup() is None  # de vorige is minder dan 24 uur oud
    view = runtime.settings_view()
    assert view["backup_map"] == str(tmp_path / "backups")
    assert [b["naam"] for b in view["backups"]] == [created.name]
    runtime.update(values={"WERKER_BACKUP_HOURS": "0"})
    assert runtime.auto_backup() is None


def test_backup_settings_are_validated(runtime):
    runtime.update(values={"WERKER_BACKUP_HOURS": "6", "WERKER_BACKUP_KEEP": "30"})
    assert runtime.env()["WERKER_BACKUP_KEEP"] == "30"
    for key, bad in (("WERKER_BACKUP_HOURS", "-1"), ("WERKER_BACKUP_HOURS", "vaak"),
                     ("WERKER_BACKUP_KEEP", "0"), ("WERKER_BACKUP_KEEP", "1000")):
        with pytest.raises(ValueError):
            runtime.update(values={key: bad})


def test_desktop_self_test_starts_everything_but_the_window(tmp_path, monkeypatch):
    pytest.importorskip("webview")
    from werker import desktop

    monkeypatch.setenv("WERKER_HOME", str(tmp_path))
    monkeypatch.setenv("WERKER_IMESSAGE", "false")
    for key in ("WERKER_DB", "WERKER_PRICES_FILE"):
        monkeypatch.delenv(key, raising=False)
    assert desktop.main(["--zelftest"]) == 0
    assert (tmp_path / "werker.db").exists()
