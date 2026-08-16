"""Credential vault + resolution chain (design §3.2/§3.3).

`resolve(creds_ref, project_id, persona) -> Credentials` walks a 3-layer chain, keyed per platform:
  1. DB     — `credential.secret_enc` for `(project_id, persona)`, Fernet-decrypted to
              `{"username": …, "password": …}`.
  2. env, per-ref  — `ANNEAL_<CREDS_REF>_USERNAME` / `_PASSWORD` (ref upper-cased, `-` → `_`).
  3. env, generic  — `ANNEAL_UI_USERNAME` / `_PASSWORD`.
The DB layer WINS when present (T1); env is the dev/CI override.

Encryption at rest is a Fernet envelope keyed by `ANNEAL_VAULT_KEY` — a deliberate divergence from
plaintext-on-disk credential storage. With NO key present we REFUSE to store or read secret
material (never a silent plaintext fallback); the env layers already cover "demo without a vault".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet
from sqlalchemy import text

_VAULT_KEY_ENV = "ANNEAL_VAULT_KEY"


class VaultKeyMissing(RuntimeError):
    """ANNEAL_VAULT_KEY absent (or invalid) when store/read of secret material was attempted. The
    message names the env var — the design's refuse-without-key rule (§3.3), not a plaintext fallback."""


class CredentialsNotFound(LookupError):
    """No credentials in ANY layer for a ref. Structured (names the ref) so the probe gate can turn it
    into an honest park reason ('no credentials for <ref>'), not an exception trace (§3.5)."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


def _fernet() -> Fernet:
    key = os.getenv(_VAULT_KEY_ENV)
    if not key:
        raise VaultKeyMissing(
            f"{_VAULT_KEY_ENV} is not set — refusing to store or read secrets (no plaintext fallback; "
            f"set {_VAULT_KEY_ENV} to a Fernet key, or use the ANNEAL_<REF>_* / ANNEAL_UI_* env layers).")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:  # a malformed key is the same failure mode as a missing one — refuse, named
        raise VaultKeyMissing(f"{_VAULT_KEY_ENV} is not a valid Fernet key: {e}") from e


def encrypt_credentials(username: str, password: str) -> bytes:
    """Fernet token of the credentials JSON. Raises VaultKeyMissing before producing anything."""
    return _fernet().encrypt(json.dumps({"username": username, "password": password}).encode())


def decrypt_credentials(secret_enc: bytes) -> Credentials:
    data = json.loads(_fernet().decrypt(bytes(secret_enc)).decode())
    return Credentials(data["username"], data["password"])


def store_credential(engine, *, project_id, persona, username, password) -> str:
    """Encrypt + upsert the `(project_id, persona)` credential. The encrypt (which raises
    VaultKeyMissing when the key is absent) runs BEFORE any DB write, so a keyless store writes
    NOTHING (T6). Returns the credential id."""
    secret_enc = encrypt_credentials(username, password)   # refuse-without-key happens here — no write
    with engine.begin() as c:
        cid = c.execute(text("""
            insert into credential (project_id, persona, secret_enc) values (:p,:pa,:s)
            on conflict (project_id, persona) do update set secret_enc = excluded.secret_enc
            returning id"""), {"p": project_id, "pa": persona, "s": secret_enc}).scalar_one()
    return str(cid)


def _db_secret_enc(engine, project_id, persona) -> bytes | None:
    with engine.connect() as c:
        row = c.execute(text("select secret_enc from credential where project_id=:p and persona=:pa"),
                        {"p": project_id, "pa": persona}).first()
    return bytes(row.secret_enc) if row is not None else None


def _ref_env(creds_ref: str) -> tuple[str | None, str | None]:
    pref = creds_ref.upper().replace("-", "_")             # acme-staging → ACME_STAGING (T2)
    return os.getenv(f"ANNEAL_{pref}_USERNAME"), os.getenv(f"ANNEAL_{pref}_PASSWORD")


def resolve(engine, *, creds_ref: str, project_id: str, persona: str) -> Credentials:
    """Resolve a target's creds_ref to credentials via the 3-layer chain (§3.2). Raises
    CredentialsNotFound (naming the ref) if no layer has a complete pair; raises VaultKeyMissing if a
    DB row exists but the vault key is absent (refuse to read, §3.3)."""
    # layer 1 — DB (decrypted). A present row WINS over env (T1). No row → fall through to env (no key
    # needed for a keyless demo). A present row with no key → VaultKeyMissing (refuse to read).
    enc = _db_secret_enc(engine, project_id, persona)
    if enc is not None:
        creds = decrypt_credentials(enc)
        if creds.username and creds.password:
            return creds
    # layer 2 — per-ref env override
    user, pwd = _ref_env(creds_ref)
    if user and pwd:
        return Credentials(user, pwd)
    # layer 3 — generic env fallback
    user, pwd = os.getenv("ANNEAL_UI_USERNAME"), os.getenv("ANNEAL_UI_PASSWORD")
    if user and pwd:
        return Credentials(user, pwd)
    raise CredentialsNotFound(
        f"no credentials for {creds_ref} (no DB credential for project={project_id!r} "
        f"persona={persona!r}, and neither ANNEAL_{creds_ref.upper().replace('-', '_')}_USERNAME/"
        f"_PASSWORD nor ANNEAL_UI_USERNAME/_PASSWORD is set)")
