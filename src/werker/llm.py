from __future__ import annotations

import logging
from typing import Any, Callable

from werker.models import Message

log = logging.getLogger(__name__)

# Krijgt (purpose, model, input_tokens, output_tokens) na elke aanroep van het model.
UsageCallback = Callable[[str, str, int, int], None]


class LLMError(RuntimeError):
    pass


def tool_input(response: Any, name: str) -> dict[str, Any]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return dict(block.input)
    raise LLMError(f"het model gaf geen aanroep van {name} terug")


def report_usage(callback: UsageCallback | None, purpose: str, model: str, response: Any) -> None:
    """Geef het tokenverbruik uit response.usage door. Een fout hierbij mag het antwoord nooit tegenhouden."""
    if callback is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    try:
        callback(
            purpose,
            str(getattr(response, "model", None) or model),
            int(getattr(usage, "input_tokens", 0) or 0),
            int(getattr(usage, "output_tokens", 0) or 0),
        )
    except Exception:
        log.exception("AI-verbruik kon niet worden vastgelegd")


def render_message(msg: Message) -> str:
    # Het bericht is onbetrouwbare invoer; een eigen sluittag zou het blok kunnen verlaten.
    body = msg.body.replace("</bericht>", "")
    return (
        "<bericht>\n"
        f"Kanaal: {msg.channel}\n"
        f"Van: {msg.sender}\n"
        f"Onderwerp: {msg.subject}\n\n"
        f"{body}\n"
        "</bericht>"
    )
