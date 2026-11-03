#!/bin/sh
# dsh serves on the container's loopback; probe it there.
set -eu
exec curl -fsS -H 'x-orchestrator-request: 1' http://127.0.0.1:3082/orchestrator/api/state
