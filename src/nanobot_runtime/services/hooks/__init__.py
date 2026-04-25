"""AgentLoop hooks + a factory that wires a standard LTM hook set."""
from nanobot.agent.hook import AgentHook

from nanobot_runtime.services.hooks.ltm_args import LTMArgumentsHook
from nanobot_runtime.clients.ltm import LTMMCPClient
from nanobot_runtime.services.hooks.ltm_consolidator import (
    LTMSavingConsolidator,
    install_ltm_saving,
)
from nanobot_runtime.services.hooks.ltm_injection import LTMInjectionHook


def build_ltm_hooks(
    loop: object,
    *,
    user_id: str,
    agent_id: str | None,
    ltm_url: str,
    top_k: int = 5,
) -> list[AgentHook]:
    """Install LTM-saving on loop.consolidator and return the read/write hooks.

    The returned hooks are meant to be appended to ``loop._extra_hooks`` by
    the gateway launcher. This function has a deliberate side-effect:
    monkey-patching ``loop.consolidator.archive`` so archived conversation
    turns get mirrored into the long-term memory store.
    """
    client = LTMMCPClient(url=ltm_url)
    install_ltm_saving(loop, ltm_client=client, user_id=user_id, agent_id=agent_id)
    return [
        LTMInjectionHook(
            ltm_client=client,
            user_id=user_id,
            agent_id=agent_id,
            limit=top_k,
        ),
        LTMArgumentsHook(user_id=user_id, agent_id=agent_id),
    ]


