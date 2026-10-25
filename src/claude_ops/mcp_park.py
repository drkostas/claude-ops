"""Park MCP servers: keep their definitions in a file of their own instead of Claude Code's config.

Claude Code starts every MCP server its user configuration (`~/.claude.json`, under `mcpServers`)
names, in every session. When many sessions run at once, each one starts its own copy of each
server, and the servers a session never uses still cost memory. Parking keeps a server's
definition (its command, arguments and keys) in a separate file, `~/.claude/mcp-parked.json`,
where no session starts it. A session that needs a parked server can still be given it with
`--mcp-config <file>` built from `definitions()`, and `unpark` puts it back for every session.
`put` keeps a definition parked without it ever being configured, for an installer.

    claude-ops mcp-park list
    claude-ops mcp-park park <server>...
    claude-ops mcp-park unpark <server>...
    claude-ops mcp-park keep <server>...      # park every configured server except these

The configuration is rewritten with `config.update_json`, which running sessions cannot break,
and a copy of it is kept before the first change. The parked file is a JSON object of server name
to definition, written with mode 0600, since definitions hold keys. A definition is written to the
parked file before the configuration loses it, so it is never only in memory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from claude_ops import config

#: Claude Code's user configuration, whose `mcpServers` every session starts
CONFIG = config.CLAUDE_JSON
#: where parked servers are kept
PARKED = Path.home() / ".claude" / "mcp-parked.json"
KEY = "mcpServers"


def _read(path: os.PathLike | str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def configured(path: os.PathLike | str = CONFIG) -> dict[str, dict]:
    """{server name: definition} that the configuration starts in every session."""
    return dict(_read(path).get(KEY) or {})


def parked(path: os.PathLike | str = PARKED) -> dict[str, dict]:
    """{server name: definition} kept in the parked file."""
    return {k: v for k, v in _read(path).items() if isinstance(v, dict)}


def definitions(config_path: os.PathLike | str = CONFIG,
                parked_path: os.PathLike | str = PARKED) -> dict[str, dict]:
    """Every definition there is, parked or configured; a configured one wins over a parked one of
    the same name, since it is what sessions start."""
    return {**parked(parked_path), **configured(config_path)}


def _write_parked(path: os.PathLike | str, servers: dict[str, dict]) -> None:
    """Write the parked file in one atomic step, readable only by its owner."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(servers, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def park(names: Iterable[str], *, config_path: os.PathLike | str = CONFIG,
         parked_path: os.PathLike | str = PARKED, backup: os.PathLike | str | None = None) -> list[str]:
    """Move the named servers from the configuration to the parked file; the names moved, sorted.
    A name the configuration does not hold is left alone."""
    wanted = set(names)
    moved: dict[str, dict] = {}

    def change(data: dict):
        servers = dict(data.get(KEY) or {})
        moved.clear()
        moved.update({n: servers.pop(n) for n in list(servers) if n in wanted})
        if not moved:
            return None
        _write_parked(parked_path, {**parked(parked_path), **moved})
        data[KEY] = servers
        return data

    config.update_json(config_path, change, backup=backup)
    return sorted(moved)


def unpark(names: Iterable[str], *, config_path: os.PathLike | str = CONFIG,
           parked_path: os.PathLike | str = PARKED, backup: os.PathLike | str | None = None) -> list[str]:
    """Move the named servers from the parked file back to the configuration; the names moved,
    sorted. A configured server of the same name is kept as it is."""
    have = parked(parked_path)
    back = {n: have[n] for n in names if n in have}
    if not back:
        return []

    def change(data: dict):
        servers = dict(data.get(KEY) or {})
        added = {n: d for n, d in back.items() if n not in servers}
        if not added:
            return None
        data[KEY] = {**servers, **added}
        return data

    config.update_json(config_path, change, backup=backup)
    left = configured(config_path)
    _write_parked(parked_path, {n: d for n, d in parked(parked_path).items() if n not in left})
    return sorted(back)


def put(servers: dict[str, dict], *, parked_path: os.PathLike | str = PARKED) -> list[str]:
    """Keep these definitions parked, replacing any of the same name; the names whose parked
    definition changed, sorted. It is for an installer that provides a server not every session
    should start: the definition stays current where a session can still be given it. The
    configuration is left as it is, so a server it also names starts in every session until it is
    parked."""
    have = parked(parked_path)
    changed = sorted(n for n, d in servers.items() if have.get(n) != d)
    if changed:
        _write_parked(parked_path, {**have, **{n: servers[n] for n in changed}})
    return changed


def keep_only(names: Iterable[str], *, config_path: os.PathLike | str = CONFIG,
              parked_path: os.PathLike | str = PARKED, backup: os.PathLike | str | None = None) -> list[str]:
    """Park every configured server except the named ones; the names parked, sorted."""
    keep = set(names)
    return park([n for n in configured(config_path) if n not in keep], config_path=config_path,
                parked_path=parked_path, backup=backup)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops mcp-park",
                                description="move MCP servers between the configuration and a parked file")
    p.add_argument("--config", default=str(CONFIG), help="Claude Code's user configuration")
    p.add_argument("--parked", default=str(PARKED), help="the parked file")
    p.add_argument("--no-backup", action="store_true", help="keep no copy of the configuration")
    p.add_argument("action", choices=("list", "park", "unpark", "keep"))
    p.add_argument("servers", nargs="*")
    a = p.parse_args(argv)
    backup = None if a.no_backup else f"{a.config}.bak-mcp-park-{time.strftime('%Y%m%d-%H%M%S')}"
    if a.action == "list":
        on, off = configured(a.config), parked(a.parked)
        print(json.dumps({"configured": sorted(on), "parked": sorted(n for n in off if n not in on)}, indent=2))
        return 0
    if a.action != "keep" and not a.servers:
        p.error(f"{a.action} needs at least one server name")
    fn = {"park": park, "unpark": unpark, "keep": keep_only}[a.action]
    try:
        moved = fn(a.servers, config_path=a.config, parked_path=a.parked, backup=backup)
    except (ValueError, config.ConfigChanged) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"moved": moved, "configured": sorted(configured(a.config)),
                      "backup": backup if moved else None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
