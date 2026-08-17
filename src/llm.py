"""Vision-model client: OpenAI primary, Anthropic fallback.

Two call sites use this:

* extraction - read the order image into a strict JSON object
* verification - read a value back out of a cropped screenshot, where Tesseract
  is not accurate enough to trust (it renders `PO-2026-0412` as `(PO-2026-0419`)

Both providers are optional imports so a missing SDK degrades to the other one
rather than breaking the run.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

try:  # optional - the run falls back to the other provider
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - convenience only
    pass

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
#: Anthropic's current frontier model. Thinking is on by default, and max_tokens
#: bounds thinking plus response text, so the budget below is deliberately loose.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = 8000


class LLMUnavailable(RuntimeError):
    """No provider could serve the request."""


def _encode(image_path: str) -> tuple[str, str]:
    data = Path(image_path).read_bytes()
    suffix = Path(image_path).suffix.lower()
    media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(
        suffix, "image/png"
    )
    return base64.standard_b64encode(data).decode("ascii"), media_type


def _strip_fence(text: str) -> str:
    """Tolerate a model that wraps JSON in a markdown fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


def _via_openai(image_path: str, system: str, user: str, schema: dict[str, Any]) -> dict:
    from openai import OpenAI  # imported lazily so the SDK stays optional

    if not os.environ.get("OPENAI_API_KEY"):
        raise LLMUnavailable("OPENAI_API_KEY is not set")

    encoded, media_type = _encode(image_path)
    response = OpenAI().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    },
                    {"type": "text", "text": user},
                ],
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": schema, "strict": False},
        },
    )
    return json.loads(_strip_fence(response.choices[0].message.content or ""))


def _via_anthropic(image_path: str, system: str, user: str, schema: dict[str, Any]) -> dict:
    """Call Claude.

    Deliberately does *not* check for ANTHROPIC_API_KEY. The SDK resolves
    credentials itself, in order: ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, then
    an OAuth profile created by `ant auth login`. Gating on the environment
    variable here would break the profile path, which is how this runs on a
    machine with no key exported.
    """
    import anthropic  # imported lazily so the SDK stays optional

    encoded, media_type = _encode(image_path)
    response = anthropic.Anthropic().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": user},
                ],
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise LLMUnavailable("Anthropic declined the request")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(_strip_fence(text))


def vision_json(
    image_path: str, system: str, user: str, schema: dict[str, Any], log=None
) -> dict:
    """Send an image plus instructions and return parsed JSON.

    Tries OpenAI first, then Anthropic. The last error is re-raised only when
    both providers fail, so a missing key for one is never fatal on its own.
    """
    errors: list[str] = []
    for name, call in (("openai", _via_openai), ("anthropic", _via_anthropic)):
        try:
            result = call(image_path, system, user, schema)
            if log:
                log.info(f"vision call served by {name}")
            return result
        except Exception as exc:  # noqa: BLE001 - any provider failure falls through
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            if log:
                log.warn(f"{name} vision call failed", detail=str(exc)[:200])
    raise LLMUnavailable(
        "no vision provider succeeded. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.\n  "
        + "\n  ".join(errors)
    )
