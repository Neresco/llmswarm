"""LLMSwarm: run several llama.cpp models together, coordinated by a supervisor.

Modes:
  solo     one member answers (baseline)
  ensemble every member answers the same problem in parallel, a judge merges
  swarm    planner decomposes, workers solve subtasks in parallel, critics
           review, synthesizer writes the final answer

All agent output is stored on a shared blackboard (SQLite + FTS5) that every
agent can query for relevant prior findings.
"""
from .blackboard import Blackboard
from .client import (chat, chat_stream, chat_with_tools, chat_with_retry,
                     completion_messages, healthy_members, log_member_call,
                     msg_text, raw_complete, reasoning_fields,
                     set_request_reasoning, set_serve_reasoning)
from .config import (load_config, members_public, norm_members, serialize_toml,
                     toml_str, validate_config)
from .fleet import HERE, RUNTIME, RUNTIME_DIR, Fleet, Member, _alive
from .swarm import (parse_subtasks, run_ensemble, run_ensemble_chat, run_solo,
                    run_swarm)
from .agent import run_agent_swarm
from .serve import run_serve
from .horde import run_horde
from .cli import main

__all__ = [
    "Blackboard",
    "chat", "chat_stream", "chat_with_tools", "chat_with_retry",
    "completion_messages", "healthy_members", "log_member_call", "msg_text",
    "raw_complete", "reasoning_fields", "set_request_reasoning",
    "set_serve_reasoning",
    "load_config", "members_public", "norm_members", "serialize_toml",
    "toml_str", "validate_config",
    "HERE", "RUNTIME", "RUNTIME_DIR", "Fleet", "Member", "_alive",
    "parse_subtasks", "run_ensemble", "run_ensemble_chat", "run_solo",
    "run_swarm", "run_agent_swarm",
    "run_serve", "run_horde", "main",
]
