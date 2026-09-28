"""Hash a dashboard password with Hermes' own ``hash_password`` (E8.6).

Run with the Hermes runtime interpreter (``hermes --print-runtime-command``),
not the Arc venv:  ``<runtime-python> -I hash_password.py <hermes-agent-dir>``.
The password is read from stdin (one line), never argv. Prints only the hash.
"""

import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: hash_password.py <hermes-agent-dir>  (password on stdin)\n")
        return 2
    root = sys.argv[1]
    sys.argv[1:] = []  # hermes_bootstrap inspects argv; give it none
    sys.path.insert(0, root)
    from hermes_constants import get_default_hermes_root

    os.environ["HERMES_HOME"] = os.environ.get("HERMES_HOME") or str(get_default_hermes_root())
    import hermes_bootstrap  # noqa: F401  (activates the dependency environment)
    from plugins.dashboard_auth.basic import hash_password

    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        sys.stderr.write("empty password\n")
        return 2
    sys.stdout.write(hash_password(password) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
