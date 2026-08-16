"""The network oracle (§5) — arm-before-action listeners + attribution.

The compiler emits a `NetExpectation` into a step's `arm_before` for each `network` postcondition; the
interpreter opens a `page.expect_response` listener BEFORE the action fires (CR-3) and asserts the
match after. Same-host attribution + `_rsc=` prefetch filtering (A9) are a WARM/HOT concern (M2); the
cold oracle just matches method + templated path + status class.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _status_ok(status: int, status_class: str) -> bool:
    lo = int(status_class[0]) * 100
    return lo <= status < lo + 100


def net_match(expectation, response) -> bool:
    """method + templated-path (`{id}`→`[^/]+`, compiled at §5.2) + status class."""
    try:
        if response.request.method != expectation.method:
            return False
        path = urlsplit(response.url).path
        if not re.fullmatch(expectation.path_pattern, path):
            return False
        return _status_ok(response.status, expectation.status_class)
    except Exception:
        return False


def predicate(expectation):
    """A closure Playwright's expect_response can call per response."""
    return lambda response: net_match(expectation, response)
