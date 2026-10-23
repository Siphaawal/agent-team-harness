#!/usr/bin/env bash
# Start Insight. Extra arguments go to server.mjs:  ./start.sh --port 7717 --open
cd "$(dirname "$0")" && exec node server.mjs "$@"
