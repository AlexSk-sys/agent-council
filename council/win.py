"""Hide console windows for children of the GUI-less hook process.

The hooks run under ``pythonw`` (no console).  A console-subsystem child such
as ``git``, ``npm`` or a ``.cmd`` harness shim then allocates a console of its
own, which flashes a cmd window on every prompt.  ``CREATE_NO_WINDOW`` stops
that without changing the child's stdout/stderr pipes.
"""

from __future__ import annotations

import os

CREATE_NO_WINDOW = 0x08000000


def kwargs() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
