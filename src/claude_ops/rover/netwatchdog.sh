#!/bin/bash
# netwatchdog.sh — command-loss watchdog + staged recovery ladder for macOS wifi.
# Flight-software model: command-loss timer (CLT) + staged FDIR + safe-mode reset.
# Runs once per launchd tick (root context expected for REAL recovery).
#
# Escalation is keyed on ABSOLUTE downtime (now - last_ok), so a missed tick
# (e.g. during sleep) still resolves to the correct tier on the next fire.
# Every rung except the reboot is idempotent. The reboot is anti-thrash guarded.
#
# Safe-test env vars:
#   SIMULATE=1   -> recovery commands are ECHOED, never run
#   FORCE_DOWN=1 -> pretend the reachability probe failed
#   DRY_RUN=1    -> never actually reboot (logs "would reboot")
set -u

# ---------- config (env-overridable; /etc/netwatchdog.conf wins if present) ----------
IFACE="${IFACE:-en0}"
SSID="${SSID:-}"
PSK="${PSK:-}"                              # empty = rely on remembered-network rejoin (no password)
STATE_DIR="${STATE_DIR:-/var/db/netwatchdog}"
LOGFILE="${LOGFILE:-/var/log/netwatchdog.log}"
BEACON_URL="${BEACON_URL:-}"               # optional webhook to ping on recovery (out-of-band telemetry)
DRY_RUN="${DRY_RUN:-0}"; SIMULATE="${SIMULATE:-0}"; FORCE_DOWN="${FORCE_DOWN:-0}"
REBOOT_ENABLED="${REBOOT_ENABLED:-0}"      # reboot rung OFF by default; opt in only after FileVault is handled
[ -r /etc/netwatchdog.conf ] && . /etc/netwatchdog.conf

# downtime thresholds (seconds) that trigger each tier
T1="${T1:-60}"; T2="${T2:-180}"; T3="${T3:-360}"; T4="${T4:-600}"; T5="${T5:-900}"   # > T5 => reboot

STATE="$STATE_DIR/state"
mkdir -p "$STATE_DIR" 2>/dev/null
log(){ echo "$(date '+%F %T') [netwatchdog] $*" >>"$LOGFILE" 2>/dev/null; echo "$(date '+%F %T') [netwatchdog] $*"; }
run(){ if [ "$SIMULATE" = "1" ]; then log "SIM> $*"; else eval "$*" >>"$LOGFILE" 2>&1; fi; }
rejoin(){ if [ -n "$SSID" ]; then run "networksetup -setairportnetwork $IFACE '$SSID'"; else log "SSID not set in /etc/netwatchdog.conf, skipping the rejoin"; fi; }

now=$(date +%s)
last_ok=$now; fails=0
[ -f "$STATE" ] && read -r last_ok fails < "$STATE" 2>/dev/null
[ -z "${last_ok:-}" ] && last_ok=$now; [ -z "${fails:-}" ] && fails=0
write_state(){ printf '%s %s\n' "$1" "$2" >"$STATE.tmp" 2>/dev/null && mv "$STATE.tmp" "$STATE" 2>/dev/null; }

# ---------- redundant reachability probe: two independent signals (L3 ping + L7 captive) ----------
probe(){
  [ "$FORCE_DOWN" = "1" ] && return 1
  local h
  for h in 1.1.1.1 8.8.8.8 9.9.9.9; do
    /sbin/ping -c1 -t3 "$h" >/dev/null 2>&1 && return 0
  done
  /usr/bin/curl -sf --max-time 5 http://captive.apple.com/hotspot-detect.html 2>/dev/null | grep -qi success && return 0
  return 1
}

# ---------- nominal: link is up. Pet the command-loss timer and exit. ----------
if probe; then
  if [ "$fails" -gt 0 ]; then
    log "RECOVERED after ${fails} cycle(s), ip=$(/usr/sbin/ipconfig getifaddr "$IFACE" 2>/dev/null || echo none)"
    [ -n "$BEACON_URL" ] && /usr/bin/curl -sf --max-time 5 "$BEACON_URL" -d "recovered after ${fails} cycles" >/dev/null 2>&1
  fi
  write_state "$now" 0
  exit 0
fi

# ---------- link is down: climb the ladder by absolute downtime ----------
fails=$((fails+1)); down=$(( now - last_ok )); write_state "$last_ok" "$fails"
log "DOWN fails=${fails} downtime=${down}s -> staged recovery"

# Recovery order is deliberately stored-credential-FIRST. An explicit configured PSK is
# only used at the DEEPEST pre-reboot rung (T5): passing a possibly-wrong password to
# `networksetup -setairportnetwork` earlier would overwrite the good keychain credential
# and defeat the PSK-independent power-cycle rejoin, so we exhaust every stored-credential
# path first and reach the explicit-PSK fallback only when the stored credential is gone.
if   [ "$down" -lt "$T1" ]; then
  log "TIER1 dns flush + dhcp renew"
  run "dscacheutil -flushcache"; run "killall -HUP mDNSResponder"; run "ipconfig set $IFACE DHCP"
elif [ "$down" -lt "$T2" ]; then
  log "TIER2 radio power-cycle (stored-credential auto-rejoin; PSK-independent, proven path)"
  run "networksetup -setairportpower $IFACE off"; run "sleep 5"; run "networksetup -setairportpower $IFACE on"
elif [ "$down" -lt "$T3" ]; then
  log "TIER3 rejoin remembered SSID ($SSID) with stored credential + dhcp"
  rejoin; run "ipconfig set $IFACE DHCP"
elif [ "$down" -lt "$T4" ]; then
  log "TIER4 interface bounce + airportd restart + power-cycle + rejoin (stored credential)"
  run "ifconfig $IFACE down"; run "sleep 3"; run "ifconfig $IFACE up"
  run "launchctl kickstart -k system/com.apple.airportd"
  run "networksetup -setairportpower $IFACE off"; run "sleep 3"; run "networksetup -setairportpower $IFACE on"
  rejoin
elif [ "$down" -lt "$T5" ]; then
  if [ -n "$PSK" ]; then
    log "TIER5 DEEP FALLBACK: explicit rejoin with configured password (stored-credential paths exhausted)"
    run "networksetup -setairportnetwork $IFACE '$SSID' '$PSK'"; run "ipconfig set $IFACE DHCP"
  else
    log "TIER5 no PSK configured; repeat power-cycle"
    run "networksetup -setairportpower $IFACE off"; run "sleep 5"; run "networksetup -setairportpower $IFACE on"
  fi
else
  log "TIER6 SAFE-MODE RESET requested (downtime ${down}s)"
  write_state "$now" 0        # anti-thrash: reset clock so a blocked/failed reboot does not loop
  if [ "$REBOOT_ENABLED" != "1" ]; then
    log "reboot rung DISABLED by default: staying DEGRADED and waiting is safer than a blind reboot. Set REBOOT_ENABLED=1 to opt in."
  elif fdesetup status 2>/dev/null | grep -q "FileVault is On"; then
    log "REFUSING reboot: FileVault is On -> a bare reboot strands the Mac at the pre-boot unlock screen with no network. Configure authenticated-restart (fdesetup authrestart) before enabling this rung."
  elif [ "$DRY_RUN" = "1" ] || [ "$SIMULATE" = "1" ]; then
    log "DRY_RUN/SIM: would reboot now"
  else
    run "/sbin/reboot"
  fi
fi
exit 0
