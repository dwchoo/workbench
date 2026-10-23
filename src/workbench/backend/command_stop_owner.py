"""What the owners of stoppable commands (the Worker terminal, the experiment flow, the Manager tool) share.

``COMMAND_ID`` is the one Python definition of a command id (the bridge's ``UUID_PATTERN``: a lowercase canonical
uuid of version 1-8 and variant 8/9/a/b), so every entry point refuses the same ids.
"""

from __future__ import annotations

import re

COMMAND_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
