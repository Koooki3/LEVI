#!/usr/bin/env bash
# Launch or stop a detached vLLM OpenAI-compatible server for LEVI.
# The contract between LEVI and this script is described in docs/VLLM.md.
#
# Usage:
#   serve.sh [extra vllm serve args...]     launch (what `levi live` runs)
#   serve.sh --stop PORT                    stop the server on PORT (TERM the group, KILL after 60 s)
#   serve.sh --check                        print the resolved settings and check the vllm executable
#
# Settings (environment; LEVI's live service sets the LEVI_VLLM_* ones from live.toml):
#   LEVI_VLLM_MODEL        model id or local path to serve (required to launch)
#   LEVI_VLLM_SERVED_NAME  --served-model-name (default: the model)
#   PORT                   port (default 8100); bound to VLLM_HOST (default 127.0.0.1)
#   GPU_UTIL               --gpu-memory-utilization (default 0.90)
#   MAX_MODEL_LEN          --max-model-len, when set (LEVI passes it as an argument instead)
#   LEVI_VLLM_PID_DIR      where vllm_<PORT>.pid and the logs go (default ${LEVI_LIVE_HOME:-~/.levi-live}/vllm)
#   LEVI_VLLM_VENV         the virtual environment that holds vLLM (default <checkout>/.venv-vllm;
#                          without one, `vllm` on PATH is used)
#   LEVI_VLLM_CUDA_HOME    a CUDA_HOME to export (only for a machine without a system CUDA toolkit; docs/VLLM.md)
#   HF_HOME, HF_HUB_OFFLINE, HF_TOKEN, CUDA_VISIBLE_DEVICES, VLLM_*  passed through unchanged
#   VLLM_WAIT_S            seconds to wait for GET /health (default 600; 0 = return at once, as LEVI does)
#   SERVE_SKIP_PREFLIGHT=1 skip the free-VRAM check; SERVE_DRY_RUN=1 print the command and exit
#
# The pid file holds the process-group id of the server: `kill -TERM -- -PID` stops all of it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"          # the LEVI checkout
PID_DIR="${LEVI_VLLM_PID_DIR:-${LEVI_LIVE_HOME:-$HOME/.levi-live}/vllm}"
VENV="${LEVI_VLLM_VENV:-$ROOT/.venv-vllm}"
if [[ -x "$VENV/bin/vllm" ]]; then
  VLLM="$VENV/bin/vllm"
  export PATH="$VENV/bin:$PATH"            # FlashInfer's JIT runs `ninja` from the venv
else
  VLLM="$(command -v vllm || true)"
fi
if [[ -n "${LEVI_VLLM_CUDA_HOME:-}" ]]; then
  export CUDA_HOME="$LEVI_VLLM_CUDA_HOME" CUDA_PATH="$LEVI_VLLM_CUDA_HOME"
  export PATH="$CUDA_HOME/bin:$PATH"
fi
export VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1

if [[ "${1:-}" == "--stop" ]]; then
  PORT="${2:?usage: serve.sh --stop PORT}"
  PIDFILE="$PID_DIR/vllm_${PORT}.pid"
  [[ -f "$PIDFILE" ]] || { echo "no pid file $PIDFILE" >&2; exit 1; }
  PID="$(head -c 32 "$PIDFILE" | tr -d '[:space:]')"
  if [[ ! "$PID" =~ ^[1-9][0-9]*$ ]]; then
    echo "pid file $PIDFILE does not hold a process id; not signalling anything" >&2
    exit 1
  fi
  # Only a vLLM server: a stale pid file may name a process that reused the pid.
  # The command line is read into a variable first (a `tr | grep -q` pipeline
  # fails under pipefail when grep stops early and tr gets SIGPIPE); a read
  # that fails (the process just ended) is silent and leaves CMD empty.
  if kill -0 "$PID" 2>/dev/null; then
    CMD="$( { tr '\0' ' ' < "/proc/$PID/cmdline"; } 2>/dev/null || true)"
    if [[ -z "$CMD" ]] && kill -0 "$PID" 2>/dev/null; then
      echo "cannot read the command line of pid $PID; not signalling it" >&2
      exit 1
    fi
    if [[ -n "$CMD" && "$CMD" != *vllm* ]]; then
      echo "pid $PID is not a vLLM server (a stale pid file?); not signalling it" >&2
      rm -f "$PIDFILE"
      exit 1
    fi
  fi
  if kill -0 "$PID" 2>/dev/null; then
    echo "stopping vllm pgid=$PID (port $PORT)"
    kill -TERM -- "-$PID" 2>/dev/null || kill -TERM "$PID"
    for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
    if kill -0 "$PID" 2>/dev/null; then
      echo "still alive after 60 s: SIGKILL"
      kill -KILL -- "-$PID" 2>/dev/null || kill -KILL "$PID"
    fi
  else
    echo "pid $PID not running"
  fi
  rm -f "$PIDFILE"
  exit 0
fi

MODEL="${LEVI_VLLM_MODEL:-}"
PORT="${PORT:-8100}"
SERVED="${LEVI_VLLM_SERVED_NAME:-$MODEL}"

if [[ "${1:-}" == "--check" ]]; then
  echo "checkout   $ROOT"
  echo "vllm       ${VLLM:-NOT FOUND (install it into $VENV, or put vllm on PATH)}"
  echo "model      ${MODEL:-NOT SET (LEVI_VLLM_MODEL)} as ${SERVED:-?} on 127.0.0.1:$PORT"
  echo "pid dir    $PID_DIR"
  echo "HF_HOME    ${HF_HOME:-(vLLM default: ~/.cache/huggingface)}  offline=${HF_HUB_OFFLINE:-0}"
  echo "CUDA_HOME  ${CUDA_HOME:-(system)}"
  [[ -n "$VLLM" && -n "$MODEL" ]] || exit 1
  "$VLLM" --version 2>/dev/null | tail -1 || true
  exit 0
fi

[[ -n "$MODEL" ]] || { echo "set LEVI_VLLM_MODEL (live.toml: [vllm] model)" >&2; exit 2; }
[[ -n "$VLLM" ]] || { echo "vllm not found: install it into $VENV (docs/VLLM.md) or set LEVI_VLLM_VENV" >&2; exit 2; }
mkdir -p "$PID_DIR"

WAIT_S="${VLLM_WAIT_S:-600}"
unset VLLM_WAIT_S   # ours, not vLLM's (vLLM warns about unknown VLLM_* variables)

PIDFILE="$PID_DIR/vllm_${PORT}.pid"
OLD="$( [[ -f "$PIDFILE" ]] && head -c 32 "$PIDFILE" | tr -d '[:space:]' || true)"
if [[ "$OLD" =~ ^[1-9][0-9]*$ ]] && kill -0 "$OLD" 2>/dev/null; then
  echo "port $PORT is already served by pid $OLD; stop it first: $0 --stop $PORT" >&2
  exit 1
fi

ARGS=(--served-model-name "$SERVED" --gpu-memory-utilization "${GPU_UTIL:-0.90}")
[[ -n "${MAX_MODEL_LEN:-}" ]] && ARGS+=(--max-model-len "$MAX_MODEL_LEN")
ARGS+=("$@")   # later arguments win (LEVI passes its budget, context and limits here)

# Pre-flight: vLLM reserves --gpu-memory-utilization x total VRAM up front and fails late if
# that much is not free. Fail fast and show who holds the GPU instead.
UTIL="${GPU_UTIL:-0.90}"; prev=""
for a in "${ARGS[@]}"; do
  [[ "$prev" == "--gpu-memory-utilization" ]] && UTIL="$a"
  [[ "$a" == --gpu-memory-utilization=* ]] && UTIL="${a#*=}"
  prev="$a"
done
if [[ "${SERVE_SKIP_PREFLIGHT:-0}" != 1 ]] && command -v nvidia-smi >/dev/null; then
  QUERY="$(nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits \
    -i "${CUDA_VISIBLE_DEVICES:-0}" 2>&1 | head -1 | tr -d ',' || true)"
  read -r FREE TOTAL <<< "$QUERY" || true
  if [[ ! "${FREE:-}" =~ ^[0-9]+$ || ! "${TOTAL:-}" =~ ^[0-9]+$ ]]; then
    echo "[serve.sh] cannot read free GPU memory from nvidia-smi (${QUERY:-no output}); check the driver and CUDA_VISIBLE_DEVICES, or SERVE_SKIP_PREFLIGHT=1" >&2
    exit 3
  fi
  NEED=$(awk -v u="$UTIL" -v t="$TOTAL" 'BEGIN{printf "%d", u*t}')
  if (( FREE < NEED )); then
    echo "[serve.sh] refusing: --gpu-memory-utilization $UTIL needs about ${NEED} MiB, only ${FREE}/${TOTAL} MiB free." >&2
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv >&2 || true
    [[ "${SERVE_DRY_RUN:-0}" == 1 ]] || exit 3
  fi
fi
if [[ "${SERVE_DRY_RUN:-0}" == 1 ]]; then
  echo -n "[dry-run] "; printf '%q ' "$VLLM" serve "$MODEL" --host "${VLLM_HOST:-127.0.0.1}" --port "$PORT" "${ARGS[@]}"; echo
  exit 0
fi

TAG="$(echo "$MODEL" | tr '/:' '__')"
LOG="$PID_DIR/vllm_${TAG}_${PORT}_$(date +%Y%m%d_%H%M%S).log"
ln -sfn "$(basename "$LOG")" "$PID_DIR/vllm_${PORT}.log"
echo "[serve.sh] $(date -Is) model=$MODEL port=$PORT args=${ARGS[*]}" > "$LOG"
setsid nohup "$VLLM" serve "$MODEL" \
  --host "${VLLM_HOST:-127.0.0.1}" --port "$PORT" \
  "${ARGS[@]}" >> "$LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PIDFILE"
echo "vllm pid=$PID (pgid) port=$PORT log=$LOG"

if [[ "$WAIT_S" -gt 0 ]]; then
  for ((i = 0; i < WAIT_S; i += 2)); do
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "vllm exited during startup; tail of the log:" >&2
      tail -n 40 "$LOG" >&2
      rm -f "$PIDFILE"
      exit 1
    fi
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      echo "ready after ${i} s: http://127.0.0.1:${PORT}/v1"
      exit 0
    fi
    sleep 2
  done
  echo "not healthy after ${WAIT_S} s (still running, see $LOG)" >&2
  exit 2
fi
