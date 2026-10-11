#!/bin/zsh -l
# Finder starts .command files in Terminal. Resolve the project independently
# of Terminal's working directory, then use the normal stable Dev launcher.
set -eu
cd -- "${0:A:h}"
# A bare python3 on this Mac lacks the app's requirements (see AGENTS.md).
python=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
[[ -x $python ]] || python=python3
exec "$python" Production_Hub_App/main.py "$@"
