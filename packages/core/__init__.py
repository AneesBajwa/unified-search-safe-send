"""Reusable core: adapters, sending, jobs, connections.

Imports nothing from ``apps``. The dependency arrow only ever points inward
(design D2), enforced by the import-linter contract in pyproject.toml.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
