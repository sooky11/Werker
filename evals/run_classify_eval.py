"""Evaluatie van werker.classify.Classifier op evals/classify_cases.jsonl.

Gebruik (vanuit de repo-root, met de venv actief en ANTHROPIC_API_KEY gezet):

    python evals/run_classify_eval.py
    python evals/run_classify_eval.py --limit 10
    python evals/run_classify_eval.py --model claude-haiku-5-5

Het script roept de echte API aan. Het slaagt (exitcode 0) bij een nauwkeurigheid
van minimaal 0,90 en zonder enige must_not-overtreding; anders exitcode 1.
Het wordt niet door de tests geimporteerd en hoort niet bij het pakket.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from werker.classify import CLASSIFY_MODEL, Classifier  # noqa: E402
from werker.models import Intent, Message  # noqa: E402

DATASET = Path(__file__).resolve().parent / "classify_cases.jsonl"
ACCURACY_THRESHOLD = 0.90
HIGH_CONFIDENCE = 0.85
ERROR_LABEL = "FOUT"


@dataclass(frozen=True)
class CaseResult:
    id: str
    expected: Intent
    got: str  # intentwaarde, of ERROR_LABEL bij een API- of parsefout
    confidence: float | None
    must_not: tuple[Intent, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.got == self.expected.value

    @property
    def violation(self) -> bool:
        return self.got in {intent.value for intent in self.must_not}


@dataclass
class EvalResult:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def violations(self) -> list[CaseResult]:
        return [r for r in self.results if r.violation]

    def per_intent(self) -> dict[Intent, tuple[float | None, float | None, int]]:
        """Per intent: (precisie, recall, support). None als de noemer nul is."""
        stats: dict[Intent, tuple[float | None, float | None, int]] = {}
        for intent in Intent:
            tp = sum(1 for r in self.results if r.expected is intent and r.got == intent.value)
            fp = sum(1 for r in self.results if r.expected is not intent and r.got == intent.value)
            fn = sum(1 for r in self.results if r.expected is intent and r.got != intent.value)
            support = tp + fn
            precision = tp / (tp + fp) if tp + fp else None
            recall = tp / support if support else None
            stats[intent] = (precision, recall, support)
        return stats


def load_cases(path: Path = DATASET) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def to_message(case: dict[str, Any], received_at: datetime | None = None) -> Message:
    return Message(
        channel=case["channel"],
        external_id=case["id"],
        thread_id=case["id"],
        sender=case["sender"],
        subject=case["subject"],
        body=case["body"],
        received_at=received_at or datetime.now(timezone.utc),
    )


def run_eval(
    cases: Iterable[dict[str, Any]],
    classifier: Any,
    progress: Callable[[CaseResult], None] | None = None,
) -> EvalResult:
    """Classificeert elk geval en geeft de resultaten terug.

    classifier moet een methode classify(msg) hebben die een Classification teruggeeft.
    Een fout in een enkel geval telt als FOUT en stopt de rest van de run niet.
    """
    result = EvalResult()
    for case in cases:
        expected = Intent(case["expected_intent"])
        must_not = tuple(Intent(value) for value in case.get("must_not", []))
        try:
            classification = classifier.classify(to_message(case))
            case_result = CaseResult(
                id=case["id"],
                expected=expected,
                got=Intent(classification.intent).value,
                confidence=float(classification.confidence),
                must_not=must_not,
            )
        except Exception as exc:  # noqa: BLE001 - een harnas moet door blijven draaien
            case_result = CaseResult(
                id=case["id"],
                expected=expected,
                got=ERROR_LABEL,
                confidence=None,
                must_not=must_not,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        result.results.append(case_result)
        if progress is not None:
            progress(case_result)
    return result


def passed(result: EvalResult) -> bool:
    return result.accuracy >= ACCURACY_THRESHOLD and not result.violations()


def _fmt_ratio(value: float | None) -> str:
    return "  -  " if value is None else f"{value * 100:5.1f}%"


def format_case(r: CaseResult) -> str:
    conf = "   -  " if r.confidence is None else f"{r.confidence:6.2f}"
    if r.ok:
        status = "OK"
    elif r.violation:
        status = "FAIL (must_not)"
    else:
        status = "FAIL"
    line = f"{r.id:<6} {r.expected.value:<12} {r.got:<12} {conf}  {status}"
    if r.error:
        line += f"  [{r.error}]"
    return line


def format_summary(result: EvalResult) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(f"Nauwkeurigheid: {result.correct}/{result.total} = {result.accuracy * 100:.1f}%"
                 f" (drempel {ACCURACY_THRESHOLD * 100:.0f}%)")
    lines.append("")
    lines.append(f"{'intent':<12} {'precisie':>9} {'recall':>9} {'support':>8}")
    for intent, (precision, recall, support) in result.per_intent().items():
        lines.append(f"{intent.value:<12} {_fmt_ratio(precision):>9} {_fmt_ratio(recall):>9} {support:>8}")
    lines.append("")
    violations = result.violations()
    high = [r for r in violations if r.confidence is not None and r.confidence >= HIGH_CONFIDENCE]
    lines.append(f"must_not-overtredingen: {len(violations)}"
                 f" (waarvan {len(high)} met confidence >= {HIGH_CONFIDENCE})")
    for r in violations:
        conf = "onbekend" if r.confidence is None else f"{r.confidence:.2f}"
        lines.append(f"  - {r.id}: verwacht {r.expected.value}, gekregen {r.got} (confidence {conf})")
    lines.append("")
    lines.append("RESULTAAT: " + ("GESLAAGD" if passed(result) else "GEZAKT"))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalueer de berichtclassificatie op de labelset.")
    parser.add_argument("--limit", type=int, default=None, help="alleen de eerste N cases draaien")
    parser.add_argument("--model", default=CLASSIFY_MODEL, help=f"modelnaam (standaard {CLASSIFY_MODEL})")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit moet minimaal 1 zijn")

    cases = load_cases(DATASET)
    if args.limit is not None:
        cases = cases[: args.limit]

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Geen API-sleutel gevonden: zet ANTHROPIC_API_KEY in de omgeving.", file=sys.stderr)
        return 2

    import anthropic

    client = anthropic.Anthropic()

    classifier = Classifier(client, model=args.model)
    print(f"Model: {args.model}  cases: {len(cases)}")
    print(f"{'id':<6} {'verwacht':<12} {'gekregen':<12} {'conf':>6}  uitkomst")
    result = run_eval(cases, classifier, progress=lambda r: print(format_case(r), flush=True))
    print(format_summary(result))
    return 0 if passed(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
