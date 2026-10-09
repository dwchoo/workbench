"""Probe-local runtime port, intentionally unavailable before judge freeze.

Runtime implementation is a separate DVL-08 phase. A missing observation here
cannot be promoted by the Q01 judge.
"""
from __future__ import annotations


def prepare_q01_context(context: dict) -> dict:
    raise ValueError("runtime_probe_not_implemented")


def collect_q01_observations(prepared: dict) -> dict:
    raise ValueError("runtime_probe_not_implemented")


def close_q01_context(prepared: dict) -> None:
    pass
