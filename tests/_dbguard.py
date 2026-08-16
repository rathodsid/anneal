"""Safety rail for the destructive test suites.

The milestone suites WIPE their database (`delete from` every table) for per-test isolation — correct
for a disposable test DB, catastrophic against a live one. `assert_wipeable_db()` refuses to proceed
unless the target is provably disposable, so pointing ANNEAL_DB_URL at the console/dev DB can never
nuke real data again.

Allowed: ANNEAL_DB_URL unset or equal to the compose default (verify.sh's path — that DB is recreated
every run), OR an explicit ANNEAL_TEST_DB=1 opt-in (a throwaway DB you affirm is safe). Anything else
is a hard stop. This does NOT weaken any gate: the suites still run and assert identically against a
proper test DB; it only blocks the WRONG database.
"""
from __future__ import annotations

import os


def assert_wipeable_db() -> None:
    from anneal_db.engine import DEFAULT_URL
    if os.getenv("ANNEAL_TEST_DB"):
        return
    url = os.getenv("ANNEAL_DB_URL")
    if not url or url == DEFAULT_URL:
        return
    raise RuntimeError(
        f"\n\nREFUSING to wipe the database: ANNEAL_DB_URL={url}\n"
        f"The test suites delete every row for isolation, and this is not the compose/default test DB "
        f"({DEFAULT_URL}). If this really is a disposable test database, set ANNEAL_TEST_DB=1.\n"
        f"NEVER point the gates at the live console/dev DB (e.g. the :55442 UI database).\n")
