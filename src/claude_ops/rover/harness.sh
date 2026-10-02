#!/bin/bash
# rover harness: run a payload that may cut this Mac off its wifi, then bring the wifi back.
#
#   harness.sh <payload-script>
#
# Settings come from ~/.config/claude-ops/rover.conf (or ROVER_CONF), and environment variables
# override them:
#   IFACE        wifi interface (default en0)
#   HOME_SSID    the network to return to (required: the harness refuses to start without it)
#   DEADMAN      seconds after which the reconnect runs even if the payload hangs (default 180)
#   PAYLOAD_MAX  seconds the payload may run (default 60)
#   OUTDIR       where the flight logs go (default ~/rover-out)
#
# Launch it detached, because joining another network can end the shell that started it:
#   caffeinate -dimsu nohup harness.sh mission.sh >/tmp/rover-boot.log 2>&1 & disown
#
# The reconnect path uses only local commands. It must never need the network it is restoring.
set +e   # never stop before the reconnect

CONF="${ROVER_CONF:-$HOME/.config/claude-ops/rover.conf}"
[ -r "$CONF" ] && . "$CONF"
IFACE="${IFACE:-en0}"
HOME_SSID="${HOME_SSID:-}"
DEADMAN="${DEADMAN:-180}"
PAYLOAD_MAX="${PAYLOAD_MAX:-60}"
OUTDIR="${OUTDIR:-$HOME/rover-out}"
PAYLOAD="${1:-}"

if [ -z "$HOME_SSID" ]; then
  echo "HOME_SSID is not set (in $CONF or the environment). Refusing to start a mission with no way back." >&2
  exit 2
fi
if [ -n "$PAYLOAD" ] && [ ! -r "$PAYLOAD" ]; then
  echo "payload script not found: $PAYLOAD" >&2
  exit 2
fi

mkdir -p "$OUTDIR"
LOG="$OUTDIR/run-$(date +%Y%m%d-%H%M%S).log"
log(){ echo "$(date '+%F %T') $*" >>"$LOG"; }

connected(){ /usr/sbin/ipconfig getifaddr "$IFACE" >/dev/null 2>&1 && /sbin/ping -c1 -t3 1.1.1.1 >/dev/null 2>&1; }

RECONNECT_STATE="$OUTDIR/.reconnect.running"
rm -f "$RECONNECT_STATE"

diag(){
  local ip gw pw
  ip=$(/usr/sbin/ipconfig getifaddr "$IFACE" 2>/dev/null || echo NONE)
  gw=$(netstat -rn -f inet 2>/dev/null | awk '/^default/{print $2; exit}')
  pw=$(networksetup -getairportpower "$IFACE" 2>&1 | tail -1)
  log "    diag ip=${ip:-NONE} gw=${gw:-NONE} ${pw}"
}

settle(){
  local secs="$1" t0 now
  t0=$(date +%s)
  while :; do
    connected && return 0
    now=$(date +%s)
    [ $(( now - t0 )) -ge "$secs" ] && return 1
    sleep 3
  done
}

reconnect(){
  if [ -e "$RECONNECT_STATE" ]; then
    log "reconnect: already running (pid $(cat "$RECONNECT_STATE" 2>/dev/null)), not starting a second one"
    return 0
  fi
  echo "$BASHPID" >"$RECONNECT_STATE"

  if connected; then
    log "reconnect: already online"
    rm -f "$RECONNECT_STATE"; return 0
  fi

  # The radio power-cycle comes first. It rejoins with the saved credential and needs no password.
  log "reconnect step 1: wifi power-cycle"
  networksetup -setairportpower "$IFACE" off >>"$LOG" 2>&1
  sleep 4
  networksetup -setairportpower "$IFACE" on  >>"$LOG" 2>&1
  diag
  if settle 30; then
    log "RECONNECTED in step 1, ip=$(/usr/sbin/ipconfig getifaddr "$IFACE")"
    rm -f "$RECONNECT_STATE"; return 0
  fi

  log "reconnect step 2: join $HOME_SSID with the saved credential"
  networksetup -setairportnetwork "$IFACE" "$HOME_SSID" >>"$LOG" 2>&1
  diag
  if settle 25; then
    log "RECONNECTED in step 2, ip=$(/usr/sbin/ipconfig getifaddr "$IFACE")"
    rm -f "$RECONNECT_STATE"; return 0
  fi

  log "STILL DOWN after both steps. Stopping here so the root watchdog (netwatchdog) can work without interference."
  diag
  rm -f "$RECONNECT_STATE"
  return 1
}

log "=== harness start (iface=$IFACE home=$HOME_SSID payload=${PAYLOAD:-none}) ==="

( sleep "$DEADMAN"; echo "$(date '+%F %T') [deadman] firing" >>"$LOG"; reconnect ) & DEADMAN_PID=$!
log "deadman armed (pid $DEADMAN_PID), reconnect forced in ${DEADMAN}s"

log "--- payload start ---"
if [ -n "$PAYLOAD" ]; then
  timeout "$PAYLOAD_MAX" bash "$PAYLOAD" >>"$LOG" 2>&1
  rc=$?
else
  echo "no payload given" >>"$LOG"
  rc=0
fi
log "--- payload end (exit $rc) ---"

reconnect
kill "$DEADMAN_PID" 2>/dev/null
log "=== done. final ip=$(/usr/sbin/ipconfig getifaddr "$IFACE" 2>/dev/null || echo NONE) ==="
