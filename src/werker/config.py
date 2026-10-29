from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from werker.models import Intent
from werker.policy import PolicyConfig
from werker.rules import RuleConfig


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    db_path: str
    prices_path: str
    prices_sheet_csv_url: str
    rules: RuleConfig
    policy: PolicyConfig

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        return cls(
            db_path=env.get("WERKER_DB", "werker.db"),
            prices_path=env.get("WERKER_PRICES_FILE", "prices.example.yaml"),
            prices_sheet_csv_url=env.get("WERKER_PRICES_CSV_URL", ""),
            rules=RuleConfig(
                blocklist=_csv_set(env.get("WERKER_BLOCKLIST", "")),
                own_addresses=_csv_set(env.get("WERKER_OWN_ADDRESSES", "")),
            ),
            policy=PolicyConfig(
                autosend_enabled=env.get("WERKER_AUTOSEND", "false").lower() == "true",
                autosend_intents=frozenset(
                    Intent(value) for value in _csv_set(env.get("WERKER_AUTOSEND_INTENTS", "prijsvraag"))
                ),
                min_confidence=float(env.get("WERKER_MIN_CONFIDENCE", "0.85")),
                max_auto_amount=Decimal(env.get("WERKER_MAX_AUTO_AMOUNT", "500")),
            ),
        )
