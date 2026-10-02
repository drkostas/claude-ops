---
name: rover
description: >
  Use when an operation on this Mac will cut the very network the chat reaches it over, and must
  run offline and then reconnect by itself. Examples are changing Wi-Fi or router settings,
  joining a device's temporary setup network, or restarting the network stack. The mission is
  planned completely, launched detached, runs offline, logs everything, and the wifi comes back so
  you can read the log and try again. Also use when the user says "rover" or "run this offline".
---

# rover

Some operations cut the connection they would be watched over. While they run nothing can be
decided, so the plan must be complete before it starts. The name comes from working with a Mars
rover. You send the full plan, it runs alone, it logs everything, and it reports back.

## The one guarantee is that the wifi comes back

Three independent layers make sure of it, and each one works when the layer above it is gone.

1. The reconnect steps in the harness. It checks for an address and a ping to 1.1.1.1, and while
   that fails it power-cycles the wifi and then joins `HOME_SSID` with the saved credential.
2. The dead-man timer, started before the payload runs. If the payload hangs, the timer runs the
   reconnect anyway.
3. The root watchdog `netwatchdog` (a LaunchDaemon that runs every 60 seconds). If the harness is
   killed, the user session is gone or the Mac restarted, it brings the wifi back through its own
   longer ladder. Its restart step is off by default, and it refuses to restart when FileVault is
   on, because the Mac would wait at the FileVault password screen with no network.

The reconnect path uses only local commands, on purpose. The network is down, so anything that
needs the network cannot be part of the way back. Never weaken the return path to make a mission
simpler.

## Install

```bash
D=$(claude-ops rover path)
mkdir -p ~/.config/claude-ops && cp "$D/rover.conf.example" ~/.config/claude-ops/rover.conf
# edit HOME_SSID in rover.conf, then the root watchdog
sudo install -m 755 "$D/netwatchdog.sh" /usr/local/sbin/netwatchdog.sh
sudo install -m 644 "$D/netwatchdog.plist" /Library/LaunchDaemons/com.local.netwatchdog.plist
echo 'SSID="my-home-network"' | sudo tee /etc/netwatchdog.conf >/dev/null && sudo chmod 600 /etc/netwatchdog.conf
sudo launchctl bootstrap system /Library/LaunchDaemons/com.local.netwatchdog.plist
```

Try the watchdog safely with `sudo SIMULATE=1 FORCE_DOWN=1 bash /usr/local/sbin/netwatchdog.sh`.
It prints the commands it would run and runs none.

## Run a mission

1. Write the payload as a shell script (`mission.sh`), with every input it needs written into it.
2. Launch it with `claude-ops rover launch mission.sh`. It starts in its own session under
   `caffeinate`, so it continues when the chat that started it loses the network.
3. Wait longer than the mission can take, then read the newest `run-*.log` in `~/rover-out`. Change
   the payload and run again.

## Rules, each learned from a real failure

- Test the way back first, alone. Run a mission with no payload and see the Mac return before you
  run one that depends on it. Recovery that never ran is not recovery.
- Carry every input. Credentials, addresses and names are written into the payload before it
  starts. A mission that needs one more secret halfway cannot ask for it.
- The log is the result of a failed run. Log and take screenshots before and after every step, and
  never let a failure stop the logging. A run that fails at step 2 with good logs is useful.
- Return no matter what. The reconnect runs after success, failure, timeout and kill.
- The mission must outlive the shell that launched it. Joining another network ends that shell.
- Do the fragile step first, while you still can reach it. Order the steps by what each one
  removes, and put the step that cannot be undone last.
- A word on a page is not a result. Check the control itself (a checkbox you can read), not text
  near it.
- Every mission has a time limit, and running out of time starts the return like any other end.

## After the return

The reconnect brings the Mac back, but it does not tell anyone the mission ended, and the chat that
launched it has usually lost its connection by then. Make the last step of the payload, after the
network is back, write its result somewhere a new chat will find it, or send it into a running chat
with `claude-ops inject`. Never try to send anything while the network is down.

## Limits

Use it only on machines and networks you own or are allowed to change. The scripts assume macOS
and a wifi interface (en0 by default). On another machine, check the interface and the network
name first, and test the way back again.
