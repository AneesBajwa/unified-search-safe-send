"""HTTP clients for the two real providers.

Shared by the **search adapter** and the **send provider** for each provider,
because the awkward parts — the Gmail 401 ladder, Slack's per-call token
routing, turning a provider's own error vocabulary into a ``ProviderError`` —
are identical on both paths and are exactly the parts that must not be
implemented twice and drift.

Everything here is thin. The clients do not decide whether to retry, do not
touch the database, and do not know what a job is: they make one call, raise a
``ProviderError`` carrying the provider's own words if it failed, and return
parsed JSON if it did not.
"""
