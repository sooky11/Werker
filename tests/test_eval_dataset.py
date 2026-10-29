import importlib.util
import json
import sys
from pathlib import Path

import pytest

from werker.models import Classification, Intent

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "classify_cases.jsonl"
SCRIPT = ROOT / "evals" / "run_classify_eval.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_classify_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # nodig voor dataclasses
    spec.loader.exec_module(module)
    return module


ev = _load_script()


def _raw_cases() -> list[dict]:
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))  # faalt hier als een regel geen geldige JSON is
    return cases


def test_every_line_parses_as_json():
    assert _raw_cases()


def test_ids_are_unique():
    ids = [case["id"] for case in _raw_cases()]
    assert len(ids) == len(set(ids))


def test_expected_and_must_not_are_valid_intents():
    valid = {intent.value for intent in Intent}
    for case in _raw_cases():
        assert case["expected_intent"] in valid, case["id"]
        for value in case.get("must_not", []):
            assert value in valid, case["id"]


def test_at_least_60_cases():
    assert len(_raw_cases()) >= 60


def test_every_intent_is_expected_at_least_once():
    seen = {case["expected_intent"] for case in _raw_cases()}
    assert seen == {intent.value for intent in Intent}


def test_channels_and_required_fields():
    for case in _raw_cases():
        assert case["channel"] in {"mail", "whatsapp"}, case["id"]
        for key in ("sender", "subject", "body"):
            assert isinstance(case[key], str), (case["id"], key)


class _AnswerKeyClassifier:
    """Fake classifier: geeft per case-id het opgegeven antwoord terug, zonder API-aanroep."""

    def __init__(self, answers: dict[str, Intent], confidence: float = 0.9):
        self.answers = answers
        self.confidence = confidence

    def classify(self, msg):
        return Classification(
            intent=self.answers[msg.external_id],
            urgency="normaal",
            language="nl",
            confidence=self.confidence,
            summary="test",
        )


class _RaisingClassifier:
    def classify(self, msg):
        raise RuntimeError("geen verbinding")


def test_run_eval_perfect_classifier_passes():
    cases = _raw_cases()
    answers = {case["id"]: Intent(case["expected_intent"]) for case in cases}
    result = ev.run_eval(cases, _AnswerKeyClassifier(answers))
    assert result.total == len(cases)
    assert result.accuracy == 1.0
    assert ev.passed(result)


def test_run_eval_must_not_violation_fails_even_when_accurate():
    cases = _raw_cases()
    answers = {case["id"]: Intent(case["expected_intent"]) for case in cases}
    target = next(case for case in cases if case.get("must_not"))
    answers[target["id"]] = Intent(target["must_not"][0])
    result = ev.run_eval(cases, _AnswerKeyClassifier(answers, confidence=0.95))
    violations = result.violations()
    assert [v.id for v in violations] == [target["id"]]
    assert result.accuracy < 1.0
    assert not ev.passed(result)
    summary = ev.format_summary(result)
    assert "must_not-overtredingen: 1 (waarvan 1 met confidence >= 0.85)" in summary


def test_run_eval_accuracy_below_threshold_fails():
    cases = _raw_cases()
    # Alles als overig: slechts een deel van de cases is overig.
    answers = {case["id"]: Intent.OVERIG for case in cases}
    result = ev.run_eval(cases, _AnswerKeyClassifier(answers))
    assert result.accuracy < ev.ACCURACY_THRESHOLD
    assert not ev.passed(result)


def test_run_eval_records_errors_without_aborting():
    cases = _raw_cases()[:3]
    result = ev.run_eval(cases, _RaisingClassifier())
    assert result.total == 3
    assert all(r.got == ev.ERROR_LABEL for r in result.results)
    assert all("geen verbinding" in r.error for r in result.results)


def test_per_intent_precision_and_recall():
    cases = [
        {"id": "x1", "channel": "mail", "sender": "a", "subject": "", "body": "", "expected_intent": "klacht"},
        {"id": "x2", "channel": "mail", "sender": "a", "subject": "", "body": "", "expected_intent": "prijsvraag"},
    ]
    answers = {"x1": Intent.PRIJSVRAAG, "x2": Intent.PRIJSVRAAG}
    stats = ev.run_eval(cases, _AnswerKeyClassifier(answers)).per_intent()
    precision, recall, support = stats[Intent.PRIJSVRAAG]
    assert precision == pytest.approx(0.5)
    assert recall == pytest.approx(1.0)
    assert support == 1
    assert stats[Intent.KLACHT] == (None, 0.0, 1)


def test_progress_callback_gets_every_case():
    cases = _raw_cases()[:5]
    answers = {case["id"]: Intent(case["expected_intent"]) for case in cases}
    seen = []
    ev.run_eval(cases, _AnswerKeyClassifier(answers), progress=seen.append)
    assert [r.id for r in seen] == [case["id"] for case in cases]
