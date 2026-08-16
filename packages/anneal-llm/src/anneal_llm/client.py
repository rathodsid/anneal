"""The LLM seam — the one place any LLM provider is reached.

Target-agnostic: no target knowledge here. Provider is pluggable behind this seam, selected by
`ANNEAL_LLM_PROVIDER` (anthropic | openai); keys from env (ANTHROPIC_API_KEY / OPENAI_API_KEY).
Provider fallback chain: Anthropic primary → OpenAI fallback on ANY error. A balanced-
brace JSON extractor (Opus wraps JSON in prose) and provider-param quirks behind the seam (Opus 4.7/
4.8 reject temperature/top_p → HTTP 400).

anneal-executor MUST NOT import this module (D31): the cold lane is zero-LLM by construction.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from ._env import load_env

load_env()  # populate API keys from the gitignored repo .env (A15)

# Default the hot/warm lanes to a current top-tier Claude model (GOAL.md); OpenAI is the fallback
# the seam provides. Model choice is config, kept in this one place — not a frozen decision.
PROVIDER = os.getenv("ANNEAL_LLM_PROVIDER", "anthropic").strip().lower()
FALLBACK = os.getenv("ANNEAL_LLM_FALLBACK", "openai").strip().lower()
_OPENAI_MODEL = os.getenv("ANNEAL_OPENAI_MODEL", "gpt-4o")
_ANTHROPIC_MODEL = os.getenv("ANNEAL_ANTHROPIC_MODEL", "claude-opus-4-8")
DEFAULT_MODEL = _OPENAI_MODEL if PROVIDER == "openai" else _ANTHROPIC_MODEL

_clients: dict = {}


def _model_for(provider: str, model: str) -> str:
    if provider == "openai":
        return _OPENAI_MODEL if (not model or model.startswith("claude")) else model
    return _ANTHROPIC_MODEL if (not model or model.startswith(("gpt", "o1", "o3", "o4"))) else model


def _client_for(provider: str):
    if provider not in _clients:
        if provider == "openai":
            import openai
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY not set")
            _clients[provider] = openai.OpenAI(timeout=90.0, max_retries=2)
        else:
            import anthropic
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            _clients[provider] = anthropic.Anthropic(timeout=90.0, max_retries=2)
    return _clients[provider]


def _providers() -> list[str]:
    order = [PROVIDER]
    if FALLBACK and FALLBACK != PROVIDER:
        key = "OPENAI_API_KEY" if FALLBACK == "openai" else "ANTHROPIC_API_KEY"
        if os.getenv(key):
            order.append(FALLBACK)
    return order


def _b64_png(image: bytes | str | Path) -> str:
    raw = bytes(image) if isinstance(image, (bytes, bytearray)) else Path(image).read_bytes()
    return base64.b64encode(raw).decode()


def _call(provider: str, system: str, user: str, model: str, max_tokens: int,
          image: bytes | str | Path | None) -> str:
    # Deliberately pass NO temperature/top_p/budget_tokens — Opus 4.7/4.8 reject them (HTTP 400).
    client = _client_for(provider)
    mdl = _model_for(provider, model)
    if provider == "openai":
        user_content: object = user
        if image is not None:
            b64 = _b64_png(image)
            user_content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
        kwargs: dict = {"messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": user_content}]}
        if mdl.startswith(("o1", "o3", "o4", "gpt-5")):
            kwargs["max_completion_tokens"] = max(max_tokens, 1500) + 9000
            kwargs["reasoning_effort"] = os.getenv("ANNEAL_OPENAI_EFFORT", "medium")
        else:
            kwargs["max_tokens"] = max_tokens
        resp = client.chat.completions.create(model=mdl, **kwargs)
        return (resp.choices[0].message.content or "").strip()

    content: object = user
    if image is not None:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": _b64_png(image)}},
            {"type": "text", "text": user},
        ]
    resp = client.messages.create(
        model=mdl, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in resp.content if block.type == "text").strip()


def complete(system: str, user: str, model: str = DEFAULT_MODEL, max_tokens: int = 1500,
             image: bytes | str | Path | None = None) -> str:
    """Try the primary provider, then the configured fallback on ANY error (B6)."""
    errors = []
    for provider in _providers():
        try:
            return _call(provider, system, user, model, max_tokens, image)
        except Exception as e:  # noqa: BLE001 — any provider error trips the fallback
            errors.append(f"{provider}: {str(e)[:160]}")
            continue
    raise RuntimeError("all LLM providers failed -> " + " | ".join(errors))


def _balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """Extract the first balanced {...}/[...] span, ignoring braces inside strings (A15)."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for o, c in (("{", "}"), ("[", "]")):
            span = _balanced(text, o, c)
            if span:
                return json.loads(span)
        raise


def complete_json(system: str, user: str, model: str = DEFAULT_MODEL, max_tokens: int = 1500,
                  image: bytes | str | Path | None = None):
    """Return parsed JSON. One retry with a stricter nudge on parse failure."""
    raw = complete(system + "\n\nRespond with ONLY valid JSON, no prose, no code fences.", user,
                   model, max_tokens, image=image)
    try:
        return _extract_json(raw)
    except json.JSONDecodeError:
        raw2 = complete(system + "\n\nYour previous reply was not valid JSON. Output ONLY a JSON value.",
                        user, model, max_tokens, image=image)
        return _extract_json(raw2)
