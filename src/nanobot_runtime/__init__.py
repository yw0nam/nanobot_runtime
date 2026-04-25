"""nanobot-runtime: shared glue for nanobot workspaces.

Exposes AgentLoop hooks (LTM inject/args/save), a gateway launcher that
monkey-patches AgentLoop.__init__ to inject those hooks, and utility
factories that wire them up from a single user_id/agent_id/ltm_url triple.
"""
from nanobot_runtime.services.hooks import (
    LTMArgumentsHook,
    LTMInjectionHook,
    LTMMCPClient,
    LTMSavingConsolidator,
    build_ltm_hooks,
    install_ltm_saving,
)
