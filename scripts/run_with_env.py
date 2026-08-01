#!/usr/bin/env python3
"""Run a command with variables loaded from a dotenv file.

    python scripts/run_with_env.py .env.neon uv run alembic upgrade head

Exists because ``set -a; . .env.neon`` does not work, and fails in a way that
looks like a broken file rather than a shell rule: every real connection string
contains ``&`` (Neon's is ``?sslmode=require&channel_binding=require``), and the
shell parses that as a background-job operator. zsh reports ``parse error near
'&'``; bash silently backgrounds part of the line and exports a truncated URL,
which is worse — the migration then runs against whatever ``DATABASE_URL``
happened to be set before, quite possibly the local database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def load(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Only surrounding quotes are stripped; nothing inside is interpreted.
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    env_file = Path(argv[1])
    if not env_file.is_file():
        print(f"no such env file: {env_file}", file=sys.stderr)
        return 1

    loaded = load(env_file)
    if not loaded:
        print(f"{env_file} defined nothing — refusing to run against the ambient environment",
              file=sys.stderr)
        return 1

    # Named, never valued: this output ends up in terminals and CI logs.
    print(f"[run_with_env] {env_file}: {', '.join(sorted(loaded))}", file=sys.stderr)
    # S603: argv[2:] is this script's own command line, typed by whoever ran it.
    # There is no shell and no untrusted input; the alternative is quoting a
    # connection string through a shell, which is the bug this script exists
    # to avoid.
    return subprocess.run(  # noqa: S603
        argv[2:], env={**os.environ, **loaded}, check=False
    ).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
