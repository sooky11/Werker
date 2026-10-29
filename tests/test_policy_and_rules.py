from datetime import datetime, timezone
from decimal import Decimal

from werker.models import Classification, Draft, Intent, Message
from werker.policy import Action, PolicyConfig, decide
from werker.rules import RuleConfig, apply_rules


def _cls(intent: Intent, confidence: float = 0.95) -> Classification:
    return Classification(intent=intent, urgency="normaal", language="nl", confidence=confidence, summary="")


def _draft(body: str = "Een herenknipbeurt is € 25,00.", needs_human: bool = False) -> Draft:
    return Draft(body=body, needs_human=needs_human, reason="")


def _msg(sender: str = "klant@example.com", headers: dict | None = None) -> Message:
    return Message(
        channel="mail",
        external_id="<1@example.com>",
        thread_id="<1@example.com>",
        sender=sender,
        subject="Prijs?",
        body="Wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
        headers=headers or {},
    )


SEND_ON = PolicyConfig(autosend_enabled=True)


def test_default_policy_never_sends():
    decision = decide(_cls(Intent.PRIJSVRAAG), _draft(), [], PolicyConfig())
    assert decision.action is Action.DRAFT


def test_valid_price_question_is_sent_when_enabled():
    decision = decide(_cls(Intent.PRIJSVRAAG), _draft(), [], SEND_ON)
    assert decision.action is Action.SEND


def test_complaint_is_never_sent_automatically():
    decision = decide(_cls(Intent.KLACHT), _draft(), [], SEND_ON)
    assert decision.action is Action.DRAFT


def test_invoice_is_never_sent_automatically():
    decision = decide(_cls(Intent.FACTUUR), _draft(), [], SEND_ON)
    assert decision.action is Action.DRAFT


def test_low_confidence_goes_to_human():
    decision = decide(_cls(Intent.PRIJSVRAAG, confidence=0.5), _draft(), [], SEND_ON)
    assert decision.action is Action.DRAFT


def test_price_errors_block_sending():
    decision = decide(_cls(Intent.PRIJSVRAAG), _draft(), ["bedrag € 30,00 staat niet"], SEND_ON)
    assert decision.action is Action.DRAFT


def test_model_asking_for_human_blocks_sending():
    decision = decide(_cls(Intent.PRIJSVRAAG), _draft(needs_human=True), [], SEND_ON)
    assert decision.action is Action.DRAFT


def test_amount_above_threshold_blocks_sending():
    config = PolicyConfig(autosend_enabled=True, max_auto_amount=Decimal("20"))
    decision = decide(_cls(Intent.PRIJSVRAAG), _draft(), [], config)
    assert decision.action is Action.DRAFT


def test_intent_not_enabled_for_autosend_stays_draft():
    decision = decide(_cls(Intent.AFSPRAAK), _draft(), [], SEND_ON)
    assert decision.action is Action.DRAFT


def test_spam_gets_label_only():
    decision = decide(_cls(Intent.SPAM), None, [], SEND_ON)
    assert decision.action is Action.LABEL


def test_blocklisted_sender_is_labelled_spam():
    rules = RuleConfig(blocklist=frozenset({"spammer@example.com"}))
    result = apply_rules(_msg("Spammer <spammer@example.com>"), rules)
    assert result.intent is Intent.SPAM


def test_own_address_is_skipped():
    rules = RuleConfig(own_addresses=frozenset({"info@salon.nl"}))
    assert apply_rules(_msg("Salon <info@salon.nl>"), rules).skip is True


def test_list_unsubscribe_header_marks_newsletter():
    result = apply_rules(_msg(headers={"list-unsubscribe": "<mailto:x@y.nl>"}), RuleConfig())
    assert result.intent is Intent.NIEUWSBRIEF


def test_normal_mail_passes_rules_untouched():
    result = apply_rules(_msg(), RuleConfig())
    assert result.skip is False and result.intent is None
