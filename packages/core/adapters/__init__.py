"""The adapter layer (openspec task group 4).

Four modules, and the split between them is the design:

``types``        the closed ``Result``, ``AdapterContext``, the protocol
``registry``     source name -> adapter, the only way a source is resolved
``fakes``        the adapters this phase actually runs; phase 3 swaps them
``orchestrator`` fan-out and per-run execution
``merge``        ranking and interleave

``orchestrator`` and ``merge`` are the two modules a test greps for the strings
``gmail``, ``slack`` and ``web`` (task 4.9). Everything source-specific lives on
one side of that line; everything generic lives on the other.
"""
