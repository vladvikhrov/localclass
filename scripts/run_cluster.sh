#!/usr/bin/env bash
# Dev-режим (ТЗ 5.4): N узлов на одном ПК, каждый со своим device_id, портом и базой.
# Использование: scripts/run_cluster.sh [N] [--gui]   (по умолчанию 3 headless-узла в tmux/фоне)
set -euo pipefail
cd "$(dirname "$0")/.."
N="${1:-3}"; MODE="${2:-}"
PY=".venv/bin/python"; [ -x "$PY" ] || PY=python3
DPORT=45820
LABELS=(A B C D E F G H I J K L M N O P Q R S T)
if [ "$MODE" = "--gui" ]; then
  for i in $(seq 0 $((N-1))); do
    L=${LABELS[$i]}; ARGS=(--device "$L" --port $((5001+i)) --data "run/$L" --discovery-port $DPORT)
    [ "$i" = 0 ] && ARGS+=(--create-session "Dev")
    $PY -m localclass "${ARGS[@]}" &
    sleep 1
  done
  wait
  exit 0
fi
if command -v tmux >/dev/null; then
  tmux kill-session -t localclass 2>/dev/null || true
  tmux new-session -d -s localclass -n A "$PY -m localclass.node --device A --port 5001 --data run/A --discovery-port $DPORT --create-session Dev; read"
  sleep 2
  CODE=$(grep -ho 'код=[A-Z0-9-]*' run/A/logs/*.log 2>/dev/null | tail -1 | cut -d= -f2 || true)
  for i in $(seq 1 $((N-1))); do
    L=${LABELS[$i]}
    tmux new-window -t localclass -n "$L" "$PY -m localclass.node --device $L --port $((5001+i)) --data run/$L --discovery-port $DPORT ${CODE:+--join-code $CODE}; read"
  done
  echo "Узлы запущены в tmux-сессии 'localclass': tmux attach -t localclass   (окна A, B, C…; /help — команды)"
else
  echo "tmux не найден: запустите узлы в отдельных терминалах, например:"
  for i in $(seq 0 $((N-1))); do
    echo "  $PY -m localclass.node --device ${LABELS[$i]} --port $((5001+i)) --data run/${LABELS[$i]} --discovery-port $DPORT $([ $i = 0 ] && echo '--create-session Dev' || echo '--join-code <КОД>')"
  done
fi
