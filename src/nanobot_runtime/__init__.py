"""nanobot-runtime: shared glue for nanobot workspaces.

Exposes AgentLoop hooks (LTM inject/args/save), a gateway launcher that
monkey-patches AgentLoop.__init__ to inject those hooks, and utility
factories that wire them up from a single user_id/agent_id/ltm_url triple.
"""
from __future__ import annotations

from nanobot_runtime.hooks import (
    LTMArgumentsHook,
    LTMInjectionHook,
    LTMMCPClient,
    LTMSavingConsolidator,
    build_ltm_hooks,
    install_ltm_saving,
)

__all__ = [
    "LTMArgumentsHook",
    "LTMInjectionHook",
    "LTMMCPClient",
    "LTMSavingConsolidator",
    "build_ltm_hooks",
    "install_ltm_saving",
]
