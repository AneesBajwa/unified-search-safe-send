"""The job runtime: claim, backoff, recovery routing, execution.

Design D3 (Postgres is the queue) and D4 (the worker is an HTTP handler, not a
resident poll loop). The correctness-critical parts are ``queue.claim`` and
``queue.finish``; everything else is scheduling around them.
"""
