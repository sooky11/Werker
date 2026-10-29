from __future__ import annotations

from typing import Any

from werker.models import Message


class LLMError(RuntimeError):
    pass


def tool_input(response: Any, name: str) -> dict[str, Any]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return dict(block.input)
    raise LLMError(f"het model gaf geen aanroep van {name} terug")


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
