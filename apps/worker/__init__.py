"""Worker deployable: a thin loop over ``core``.

Design D4: this is an HTTP handler, not a resident poll loop. There is no
configuration of a continuously-running process on Cloud Run that is $0 — an
always-allocated 1 vCPU service is $44.71/month — so work is *dispatched*
(Cloud Tasks push to /work) rather than *discovered*, with Cloud Scheduler
hitting /sweep as the recovery path.

Phase 0 ships the endpoints as stubs so the deployable, its image and its
IAM wiring are proven before any job logic depends on them.
"""
