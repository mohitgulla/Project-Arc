"""Write the dashboard basic-auth keys into ``~/.hermes/.env`` (E8.6).

``hermes/remote/set-password.sh`` pipes two lines to
``python -m arc.remote.envfile <path>`` on stdin: the username and the scrypt hash
from Hermes' ``hash_password``. This module:

- sets/replaces ``HERMES_DASHBOARD_BASIC_AUTH_USERNAME`` and ``..._PASSWORD_HASH``;
- sets ``..._SECRET`` (32 random bytes, base64, same as ``openssl rand -base64 32``)
  **only if it is unset**, so existing sessions survive a password change/restart;
- removes any plaintext ``HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`` line;
- keeps every other line as it was, writes atomically, and leaves the file ``0600``;
- prints only key names, never values.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from arc.remote import BASIC_AUTH_KEYS, PLAINTEXT_PASSWORD_KEY

USERNAME_KEY, HASH_KEY, SECRET_KEY = BASIC_AUTH_KEYS
_HASH = re.compile(r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")
_USERNAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.removeprefix("export ").partition("=")[0].strip()


def _has_value(line: str) -> bool:
    value = line.strip().removeprefix("export ").partition("=")[2].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return bool(value.strip())


def new_secret() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def update(text: str, username: str, password_hash: str) -> tuple[str, list[str]]:
    """New .env text and the list of human-readable changes (key names only)."""
    if not _USERNAME.fullmatch(username):
        msg = "username must be 1-64 chars of letters, digits, '.', '_', '@' or '-'"
        raise ValueError(msg)
    if not _HASH.fullmatch(password_hash):
        msg = "password hash is not a Hermes scrypt hash"
        raise ValueError(msg)
    wanted = {USERNAME_KEY: username, HASH_KEY: password_hash}
    lines = text.splitlines()
    out: list[str] = []
    changes: list[str] = []
    written: set[str] = set()
    secret_set = False
    for line in lines:
        key = _key_of(line)
        if key == PLAINTEXT_PASSWORD_KEY:
            changes.append(f"removed {PLAINTEXT_PASSWORD_KEY} (plaintext)")
            continue
        if key in wanted:
            if key in written:
                continue  # drop duplicates; the first occurrence carries the new value
            out.append(f"{key}='{wanted[key]}'")
            written.add(key)
            changes.append(f"replaced {key}")
            continue
        if key == SECRET_KEY:
            if secret_set:
                continue
            if _has_value(line):
                secret_set = True
                out.append(line)
                changes.append(f"kept {SECRET_KEY}")
                continue
            continue  # blank secret: dropped, re-added below
        out.append(line)
    for key in (USERNAME_KEY, HASH_KEY):
        if key not in written:
            out.append(f"{key}='{wanted[key]}'")
            changes.append(f"set {key}")
    if not secret_set:
        out.append(f"{SECRET_KEY}='{new_secret()}'")
        changes.append(f"set {SECRET_KEY}")
    return "\n".join(out) + "\n", changes


def write_env(path: Path, username: str, password_hash: str) -> list[str]:
    """Update *path* in place (atomic replace, mode 0600). Returns the change list."""
    path = path.expanduser()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new, changes = update(text, username, password_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return changes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m arc.remote.envfile <env-path>  (stdin: user, hash)\n")
        return 2
    username = sys.stdin.readline().strip()
    password_hash = sys.stdin.readline().strip()
    try:
        changes = write_env(Path(args[0]), username, password_hash)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    for change in changes:
        sys.stdout.write(f"  {change}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
