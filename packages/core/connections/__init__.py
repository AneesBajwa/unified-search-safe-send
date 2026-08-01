"""Provider connections: OAuth, encrypted token storage, silent refresh.

Split by what each part is responsible for rather than by provider, because the
things that differ between Google and Slack (scope lists, token counts, account
identifiers) are *data*, while the things that are hard (refresh stampedes,
never nulling a credential, identity-preserving reconnect) are the same for
both and worth having exactly one implementation of.

- ``oauth``   — the two flows as descriptors plus exchange/refresh
- ``state``   — signed ``state``, so the callback carries its own proof
- ``store``   — the ``connections`` table, with tokens encrypted at rest
- ``tokens``  — silent refresh under an advisory lock; the callable adapters get
- ``service`` — begin/complete/disconnect, and the by-hand invalidation helper
"""
