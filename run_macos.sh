#!/bin/zsh
set -euo pipefail

repo_dir=${0:A:h}
python_bin="$repo_dir/.venv-macos/bin/python"
tk_path="/opt/homebrew/opt/python-tk@3.13/libexec"

if [[ ! -x "$python_bin" ]]; then
  print -u2 "Create the macOS environment first: python3.13 -m venv .venv-macos"
  exit 1
fi

export PYTHONPATH="$tk_path${PYTHONPATH:+:$PYTHONPATH}"
if ! "$python_bin" -c 'import tkinter' >/dev/null 2>&1; then
  print -u2 "Tk is unavailable. Install it with: brew install python-tk@3.13"
  exit 1
fi

exec "$python_bin" "$repo_dir/screensolve_mac.py"
