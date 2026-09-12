"""The application layer: everything a front-end needs to wire and drive a run.

cli.py is one front-end and tui/ is another; both compose a run out of
these modules rather than out of each other's internals.
"""
from __future__ import annotations
