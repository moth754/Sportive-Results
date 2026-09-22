#!/usr/bin/env bash
# Run Race Results in this terminal (the installed service does this for you at boot).
#   ./run.sh              port 8080
#   ./run.sh --port 8090
cd "$(dirname "${BASH_SOURCE[0]}")"
exec .venv/bin/python -m raceresults "$@"
