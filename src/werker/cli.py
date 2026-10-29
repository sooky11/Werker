from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Mapping

from werker.classify import Classifier
from werker.config import Settings
from werker.connectors import imessage
from werker.connectors.base import Connector
from werker.connectors.mail import IMAPConnector, parse_email
from werker.models import Message
from werker.pipeline import Pipeline, SendError
from werker.policy import PolicyConfig
from werker.prices import PriceBook, fetch_sheet_csv, format_amount, load_csv, load_yaml
from werker.reply import Replier
from werker.store import Store


log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def mailbox_connectors(env: Mapping[str, str]) -> list[IMAPConnector]:
    """Lees WERKER_MAILBOXES: een JSON-lijst met mailboxen. Wachtwoorden staan in eigen variabelen."""
    entries = json.loads(env.get("WERKER_MAILBOXES", "[]") or "[]")
    connectors = []
    for entry in entries:
        password = env.get(entry.get("password_env", ""), "")
        if not password:
            log.warning("mailbox %s overgeslagen: er is nog geen wachtwoord ingesteld", entry.get("name"))
            continue
        connectors.append(
            IMAPConnector(
                host=entry["host"],
                user=entry["user"],
                password=password,
                smtp_host=entry["smtp_host"],
                sender_address=entry["sender"],
                name=f"mail:{entry['name']}",
                port=int(entry.get("port", 993)),
                smtp_port=int(entry.get("smtp_port", 465)),
                folder=entry.get("folder", "INBOX"),
            )
        )
    return connectors


def outlook_token_cache_path(env: Mapping[str, str]) -> str:
    """Waar de Outlook-tokens staan: WERKER_OUTLOOK_TOKEN_CACHE, anders naast de database."""
    explicit = env.get("WERKER_OUTLOOK_TOKEN_CACHE", "")
    if explicit:
        return explicit
    return str(Path(env.get("WERKER_DB", "werker.db")).resolve().parent / "werker-outlook-token.json")


def outlook_connector(env: Mapping[str, str]) -> Connector | None:
    """Outlook/Microsoft 365 werkt alleen na 'werker outlook-login' en met een client-ID van de Azure-app."""
    client_id = env.get("WERKER_OUTLOOK_CLIENT_ID", "")
    if not client_id:
        return None
    cache_path = outlook_token_cache_path(env)
    if not Path(cache_path).exists():
        log.warning("Outlook overgeslagen: draai eerst 'werker outlook-login'")
        return None
    from werker.connectors.outlook import OutlookConnector, token_provider_from_cache

    return OutlookConnector(token_provider_from_cache(client_id, cache_path))


def build_connectors(env: Mapping[str, str]) -> dict[str, Connector]:
    connectors: dict[str, Connector] = {}
    for connector in mailbox_connectors(env):
        connectors[connector.name] = connector
    if env.get("WERKER_IMAP_HOST") and "mail:imap" not in connectors:
        connectors["mail:imap"] = IMAPConnector(
            host=env["WERKER_IMAP_HOST"],
            user=env["WERKER_IMAP_USER"],
            password=env["WERKER_IMAP_PASSWORD"],
            smtp_host=env["WERKER_SMTP_HOST"],
            sender_address=env["WERKER_SENDER_ADDRESS"],
            name="mail:imap",
        )
    if env.get("WERKER_GMAIL_TOKEN"):
        from werker.connectors.gmail import GmailConnector, build_service

        connectors["mail:gmail"] = GmailConnector(
            build_service(env["WERKER_GMAIL_TOKEN"]), sender_address=env["WERKER_SENDER_ADDRESS"]
        )
    outlook = outlook_connector(env)
    if outlook is not None:
        connectors[outlook.name] = outlook
    if env.get("WHATSAPP_TOKEN"):
        from werker.connectors.whatsapp import WhatsAppConnector

        connectors["whatsapp"] = WhatsAppConnector(env["WHATSAPP_PHONE_NUMBER_ID"], env["WHATSAPP_TOKEN"])
    if env.get("WERKER_IMESSAGE", "true") == "true" and imessage.available():
        connectors["imessage"] = imessage.IMessageConnector()
    return connectors


def price_loader(settings: Settings) -> Callable[[], list]:
    if settings.prices_sheet_csv_url:
        return lambda: load_csv(fetch_sheet_csv(settings.prices_sheet_csv_url))
    return lambda: load_yaml(Path(settings.prices_path))


def build_pipeline(
    settings: Settings,
    store: Store,
    connectors: Mapping[str, Connector],
    policy: PolicyConfig | None = None,
) -> Pipeline:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY ontbreekt; zet die omgevingsvariabele om berichten te classificeren.")
    client = anthropic.Anthropic()
    prices = PriceBook(price_loader(settings))
    prices.refresh()
    return Pipeline(
        store=store,
        classifier=Classifier(client),
        replier=Replier(client),
        prices=prices,
        policy=policy or settings.policy,
        rules=settings.rules,
        connectors=connectors,
    )


def _print_decision(path: str, message: Message, decision) -> None:
    print(f"{path}: {message.subject!r} van {message.sender} -> {decision.action.value} ({decision.reason})")


def cmd_prices(settings: Settings, _: argparse.Namespace) -> int:
    book = PriceBook(price_loader(settings))
    if not book.refresh():
        return 1
    for price in book.prices:
        print(f"{price.product}: € {format_amount(price.amount)} {price.unit}".rstrip())
    return 0


def cmd_process_eml(settings: Settings, args: argparse.Namespace) -> int:
    # Testen op bestanden verzendt nooit, ook niet als auto-verzenden aan staat.
    dry_policy = dataclasses.replace(settings.policy, autosend_enabled=False)
    store = Store(settings.db_path)
    pipeline = build_pipeline(settings, store, {}, policy=dry_policy)
    for path in args.files:
        msg = parse_email(Path(path).read_bytes())
        decision = pipeline.process(msg)
        if decision is None:
            print(f"{path}: al verwerkt")
        else:
            _print_decision(path, msg, decision)
    return 0


def cmd_sync(settings: Settings, _: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    pipeline = build_pipeline(settings, store, build_connectors(os.environ))
    print(f"{pipeline.sync()} nieuwe berichten verwerkt")
    return 0


def cmd_outlook_login(settings: Settings, _: argparse.Namespace) -> int:
    from werker.connectors.outlook import login

    # Ook instellingen uit het instelscherm tellen mee, niet alleen de omgeving.
    stored = {k: v for k, v in Store(settings.db_path).get_settings().items() if v}
    env = {**os.environ, **stored}
    client_id = env.get("WERKER_OUTLOOK_CLIENT_ID", "")
    if not client_id:
        print("Vul eerst de client-ID van je Azure-app in (WERKER_OUTLOOK_CLIENT_ID).", file=sys.stderr)
        return 1
    try:
        login(client_id, outlook_token_cache_path(env))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Ingelogd bij Outlook. Nieuwe berichten worden vanaf nu meegenomen.")
    return 0


def cmd_digest(settings: Settings, _: argparse.Namespace) -> int:
    from datetime import datetime, timedelta

    from werker.digest import build_digest

    store = Store(settings.db_path)
    now = datetime.now().astimezone()
    print(build_digest(store, now - timedelta(hours=24), now=now))
    return 0


def cmd_drafts(settings: Settings, _: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    for row in store.pending_drafts():
        print(f"[{row['id']}] {row['channel']} | {row['sender']} | {row['subject']}")
        print(f"    intentie: {row['summary']}")
        if row["errors"]:
            print(f"    let op: {row['errors']}")
        print(f"    {row['body']}\n")
    return 0


def cmd_approve(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    pipeline = build_pipeline(settings, store, build_connectors(os.environ))
    body = Path(args.text).read_text(encoding="utf-8") if args.text else None
    try:
        pipeline.approve(args.draft_id, body=body, force=args.force)
    except (LookupError, ValueError, SendError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"concept {args.draft_id} verzonden")
    return 0


def cmd_reject(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    build_pipeline(settings, store, {}).reject(args.draft_id)
    print(f"concept {args.draft_id} afgewezen")
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import threading

    import uvicorn

    from werker.auth import accounts_configured
    from werker.runtime import Runtime, run_background
    from werker.web import create_app

    env = os.environ
    ui_password = env.get("WERKER_UI_PASSWORD", "")
    store = Store(settings.db_path)
    if args.host not in LOCAL_HOSTS and not ui_password and not accounts_configured(store):
        print("Weiger te starten: zet WERKER_UI_PASSWORD of maak teamaccounts aan voordat je op een netwerk-interface luistert.", file=sys.stderr)
        return 1
    runtime = Runtime(store, Path(settings.db_path).resolve().parent, env)
    runtime.reconfigure()
    app = create_app(runtime, ui_password=ui_password)
    run_background(runtime, threading.Event())
    print(f"Werker draait op http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="werker", description="Sorteer en beantwoord zakelijke berichten.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("prices", help="Lees de prijslijst en toon die.")

    eml = sub.add_parser("process-eml", help="Verwerk .eml-bestanden zonder te verzenden.")
    eml.add_argument("files", nargs="+")

    sub.add_parser("sync", help="Haal nieuwe mail op en verwerk die.")
    sub.add_parser("drafts", help="Toon concepten die op goedkeuring wachten.")
    sub.add_parser("digest", help="Toon het dagoverzicht van de afgelopen 24 uur.")

    approve = sub.add_parser("approve", help="Keur een concept goed en verstuur het.")
    approve.add_argument("draft_id", type=int)
    approve.add_argument("--text", help="Bestand met de tekst die in plaats van het concept verstuurd wordt.")
    approve.add_argument("--force", action="store_true", help="Verstuur ondanks een prijscontrole-fout.")

    reject = sub.add_parser("reject", help="Wijs een concept af.")
    reject.add_argument("draft_id", type=int)

    sub.add_parser("outlook-login", help="Log in bij Outlook (Microsoft 365) met een inlogcode.")
    serve = sub.add_parser("serve", help="Start de inbox-webpagina en de WhatsApp-webhook.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    handlers = {
        "prices": cmd_prices,
        "process-eml": cmd_process_eml,
        "sync": cmd_sync,
        "drafts": cmd_drafts,
        "digest": cmd_digest,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "serve": cmd_serve,
        "outlook-login": cmd_outlook_login,
    }
    args = parser.parse_args(argv)
    return handlers[args.command](Settings.from_env(), args)


if __name__ == "__main__":
    raise SystemExit(main())
