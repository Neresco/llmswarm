#!/usr/bin/env bash
# Start the swarm in horde-worker mode: polls the cluster, processes jobs
# through the local pipeline, submits results back. Endpoints stay live.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 swarm.py horde "$@"
