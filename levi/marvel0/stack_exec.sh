#!/usr/bin/env bash
# Start wrapper of the marvel0 arm-stack units (levi/marvel0/stack_units.py):
#
#     stack_exec.sh <fr3_env.sh> -- <command> [args ...]
#
# Sources the rig's ROS environment, then replaces itself with the command, so
# the unit's main process is the command itself and a SIGINT sent by systemd
# reaches it as Ctrl+C reaches the foreground job of a terminal. The 32
# variables fr3_env.sh sets are therefore never copied into a unit (they go
# stale after a colcon rebuild); the unit gets them from the file itself.
#
# fr3_env.sh forces coloured logs, which fills a unit's log file with escape
# codes: the two variables below are set after it. The wrapper starts nothing
# of the robot by itself and has no other mode; a check that needs more than
# one command is more than one unit.
#
# Exit 64: wrong arguments. Exit 78: the environment file could not be read or
# sourced (the command is then not run).
if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
  echo "usage: stack_exec.sh <fr3_env.sh> -- <command> [args ...]" >&2
  exit 64
fi
env_file="$1"
shift 2
# The sourced files read "$@" in some setups; they get none of the command's.
cmd=("$@")
set --
if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
  echo "stack_exec: the environment file cannot be read" >&2
  exit 78
fi
# shellcheck disable=SC1090
source "$env_file" >/dev/null || {
  echo "stack_exec: sourcing the environment file failed" >&2
  exit 78
}
export RCUTILS_COLORIZED_OUTPUT=0
export PYTHONUNBUFFERED=1
exec "${cmd[@]}"
