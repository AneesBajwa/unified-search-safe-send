#!/usr/bin/env python3
"""Re-encrypt stored tokens under the current keyring version (task 6.1d).

    # add the new key, keeping the old one so existing rows stay readable
    TOKEN_KEYRING="v1:<old-base64>,v2:<new-base64>"
    uv run python scripts/rotate_keys.py            # what would change
    uv run python scripts/rotate_keys.py --apply    # do it
    # only once this reports 0 remaining, drop v1 from the keyring

**Safe alongside live traffic**, which is the whole reason it looks like this:

- ``SELECT ... FOR UPDATE SKIP LOCKED`` in batches, so a row currently being
  refreshed by a worker is skipped rather than blocking — and picked up on the
  next pass.
- Rows are selected by ``WHERE key_version < :current``. That is only possible
  because ``key_version`` is **plaintext in the envelope header** and mirrored
  on the row; without it, rotation would mean trial-decrypting every row with
  every key.
- Each row is decrypted and re-encrypted under **its own AAD**, which is bound
  to ``conn:{id}:{provider}:{field}`` — so this cannot accidentally launder a
  token from one row into another. A row whose ciphertext will not authenticate
  is reported and left alone, never overwritten.

Idempotent: re-running after a complete pass finds nothing to do.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.connections.store import TOKEN_FIELDS
from core.db import dispose_engine, session_scope
from core.security import crypto
from sqlalchemy import text

BATCH = 50


async def rotate(*, apply: bool, batch: int) -> int:
    keyring = crypto.get_keyring()
    target = keyring.current
    rotated = failed = 0

    while True:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, provider::text AS provider, key_version,
                               access_token_ct, refresh_token_ct, bot_token_ct
                          FROM connections
                         WHERE key_version < :target
                         ORDER BY id
                         LIMIT :batch
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"target": target, "batch": batch},
                )
            ).mappings().all()

            if not rows:
                await session.commit()
                break

            for row in rows:
                updates: dict[str, object] = {"id": row["id"]}
                assignments: list[str] = []
                broken: list[str] = []

                for column, field in TOKEN_FIELDS.items():
                    blob = row[column]
                    if blob is None:
                        continue
                    aad = crypto.aad_for(
                        connection_id=int(row["id"]),
                        provider=str(row["provider"]),
                        field=field,
                    )
                    try:
                        plaintext = crypto.decrypt(blob, aad=aad, keyring=keyring)
                    except (crypto.EnvelopeError, crypto.KeyNotFound) as exc:
                        # Reported, never overwritten. A row we cannot read is an
                        # operations problem — usually a key dropped before the
                        # previous rotation finished — and destroying it here
                        # would turn a recoverable one into a permanent one.
                        broken.append(f"{column}: {exc}")
                        continue
                    updates[column] = crypto.encrypt(plaintext, aad=aad, keyring=keyring)
                    assignments.append(f"{column} = :{column}")

                if broken:
                    failed += 1
                    print(f"  ! connection {row['id']}: {'; '.join(broken)}", file=sys.stderr)
                    continue

                print(f"  connection {row['id']}: v{row['key_version']} -> v{target}")
                rotated += 1

                if not apply:
                    continue

                assignments.append("key_version = :target")
                updates["target"] = target
                columns = ", ".join(assignments)
                statement = f"UPDATE connections SET {columns} WHERE id = :id"  # noqa: S608
                await session.execute(text(statement), updates)

            if apply:
                await session.commit()
            else:
                # Nothing was written, and without a commit the FOR UPDATE locks
                # would be held for the whole dry run.
                await session.rollback()
                break

    verb = "re-encrypted" if apply else "would re-encrypt"
    print(f"\n{verb} {rotated} connection(s) under key version {target}")
    if failed:
        print(f"{failed} connection(s) could not be read and were left untouched", file=sys.stderr)
    if not apply and rotated:
        print("dry run — pass --apply to write")
    return 1 if failed else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args()
    try:
        return await rotate(apply=args.apply, batch=args.batch)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except crypto.KeyringUnavailable as exc:
        print(f"cannot rotate: {exc}", file=sys.stderr)
        sys.exit(2)
