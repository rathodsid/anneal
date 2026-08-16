"""Minimal JSONC loader — strips `//` line and `/* */` block comments, string-aware.

The normative fixture (`fixtures/campaign-crud.bundle.jsonc`) is authored with comments.
A regex strip would corrupt string values that contain `//` (e.g. a URL such as `http://host/x`
or a path regex), so this scans character by character and never treats `//` inside a JSON string
as a comment.
"""
from __future__ import annotations

import json
from typing import Any


def strip_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads(text: str) -> Any:
    return json.loads(strip_comments(text))
