"""council - three agent harnesses that check each other's work.

Public entry points:
    from council.protocol import run_task_council, run_diff_review
"""

__version__ = "0.1.0"

from .adapters import AGENTS, AgentConfig, AgentResult, run_agent  # noqa: F401
from .protocol import run_diff_review, run_task_council  # noqa: F401
