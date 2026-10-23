"""What the owners of stoppable commands (the Worker terminal, the experiment flow, the Manager tool) share.

``COMMAND_ID`` is the one Python definition of a command id (the bridge's ``UUID_PATTERN``: a lowercase canonical
uuid of version 1-8 and variant 8/9/a/b), so every entry point refuses the same ids.

The helpers are the owners' calls into the stop service. A fault of the service (or no service at all) never changes
how an owner's command runs, is followed or ends: each helper swallows it and returns what "nothing" means for its call.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from workbench.backend.command_stop import CommandStopService, StopTarget

COMMAND_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def register_target(stop: CommandStopService | None, target: StopTarget) -> bool:
    """Offer ``target`` to the stop service; True when it is registered.

    On a fault the command keeps running and stays unstoppable, and the service is told (one audit record, the
    error's type name only) so that a later stop is answered, not guessed. No service: nothing is registered.
    """
    if stop is None:
        return False
    try:
        stop.register(target)
        return True
    except Exception as exc:
        try:
            stop.registration_failed(target, type(exc).__name__)
        except Exception:
            pass
        return False


def report_ended(stop: CommandStopService | None, command_id: str, result: Mapping[str, Any]) -> None:
    """The owner observed the command's end: the stop service learns the final result."""
    if stop is None:
        return
    try:
        stop.command_ended(command_id, result)
    except Exception:
        pass


def stop_marks(stop: CommandStopService | None, command_id: str) -> dict[str, str]:
    """``stopped_by``/``stop_signal`` when a stop signalled this command, else nothing (a service fault: nothing)."""
    if stop is None:
        return {}
    try:
        return stop.stop_marker(command_id)
    except Exception:
        return {}
