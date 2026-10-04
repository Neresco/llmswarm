#!/usr/bin/env python3
"""LLMSwarm entry point.

The implementation now lives in the ``llmswarm/`` package (split into config,
fleet, blackboard, client, swarm, agent, ui, server, serve, horde and cli
modules). This file is kept as a thin shim so ``python swarm.py serve`` /
``python swarm.py horde`` keeps working exactly as before.

Public API is re-exported here for backward compatibility with scripts that do
``import swarm``.
"""
from llmswarm import *  # noqa: F401,F403  re-export public API
from llmswarm import main  # noqa: F401


if __name__ == "__main__":
    main()
