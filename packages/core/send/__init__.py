"""The safe-send gate (openspec task group 5, design D5).

The core of the submission, and the part the brief says it verifies most
carefully. Six modules:

``digest``      the confirmation proof, which doubles as the idempotency
                fingerprint
``recipients``  channel id -> the name a human recognises
``claim``       the two-statement conditional insert, at READ COMMITTED
``providers``   the dispatch interface, and a fake that counts deliveries
``crash``       named, armable fault seams at every commit boundary
``handler``     the send job: dispatch, reconcile, resolve
``service``     the gate itself — drafts, the send decision table, replay

The one-sentence version: **the database decides who owns a send**, and
everything else is bookkeeping around that decision.
"""
