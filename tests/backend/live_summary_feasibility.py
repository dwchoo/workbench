"""Fixed probe-local Q01 port for the current installed-OMP eight-item batch.

This development adapter is never imported by product runtime.
"""
from __future__ import annotations

from q01_sdk_full_probe import current_probe


def prepare_q01_context(context: dict) -> dict:
    return current_probe.prepare(context)


def collect_q01_observations(prepared: dict) -> dict:
    return current_probe.collect(prepared)


def close_q01_context(prepared: dict) -> None:
    current_probe.close(prepared)
