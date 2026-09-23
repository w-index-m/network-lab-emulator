# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-vendor network device CLI/protocol emulator. It's not a "show text
generator" — several protocols run as real sockets/real packets (OSPF over
raw IP proto 89, BGP over TCP 179, RIPv2 over UDP 520, SNMP over UDP 161,
NETCONF/SSH-CLI/Telnet-CLI, gNMI over gRPC), so real clients (ncclient,
gnmic/pygnmi, snmpwalk, an actual ssh/telnet client) can connect to emulated
devices. See `README.md` for the full feature/protocol matrix and supported
device types (`device_type`: `catalyst`, `nexus`, `cisco`, `asa`, `sir`,
`srs`, `apresia`, `bigip`, `ipcom`, `pc`).

## Commands

```bash
# Run the app
python app.py                                    # http://localhost:8000
NETLAB_AUTH_DISABLE=1 python app.py               # skip login (admin/admin by default)

# Full regression suite (~15 min, ~1350+ tests) — run this before every push
pytest tests/ -q -p no:cacheprovider

# Single file / single test
pytest tests/test_ospf_neighbor.py -v -p no:cacheprovider
pytest tests/test_ospf_neighbor.py::test_name -v -p no:cacheprovider

# Lint (ruff.toml present)
ruff check .
```

Real protocol listeners (raw OSPF sockets, TCP 830/22/23, UDP 161, etc.)
need elevated privileges to bind; without them the app still starts and CLI
emulation still works, just without the real-socket protocol agents.

## Architecture

### Two-layer CLI dispatch — read this before touching command handling

`app.py`'s `cli_command()` docstring (around the top of the function) spells
out the dispatch order; it is the single most important thing to understand
before changing how a command is parsed:

1. `handle_protocol_show(...)` (app.py) — dynamic `show` output backed by
   live protocol-engine state (RIP/OSPF/BGP/rib_engine/etc.)
2. `handle_protocol_config(...)` (app.py) — config commands that mutate
   protocol-engine state (adds a static route to `rib_engine`, starts
   `rip_engine`/`ospf_engine`, etc.)
3. `engine/rules.py`'s `RuleEngine.process(...)` — the catch-all for
   whatever the above two didn't handle: vendor-specific canned
   responses, mode transitions, completion, help text.

**The first layer to return non-None/truthy wins — layers below it never
run for that command.** This means:
- If you add a command handler in `app.py`, the equivalent code in
  `rules.py` (if any) becomes dead for that command. Check `app.py` first
  when a `rules.py` change "does nothing."
- Conversely, several vendor-specific CLI regexes in `handle_protocol_config`
  key off `state._routing_mode` (a side-channel attribute set by e.g.
  `router rip`/`router ospf`) rather than `state.device_type`, so a new
  device type's `router rip`/`network ...` commands can "just work" for
  free without reimplementing RIP/OSPF — see how `device_type == "ipcom"`
  was wired up in `engine/rules.py` (`_ipcom_process`) for the pattern:
  transition the mode, then let the already-executed `app.py` layer's
  side effects stand rather than re-deriving state locally. Duplicating
  state between the two layers (e.g. a device-specific `state.static_routes`
  list next to the shared `rib_engine`) has caused real duplicate/garbled
  output bugs in this codebase — prefer delegating to the shared engine.

### Device-specific CLI dialects: dedicated `_xxx_process` handlers

`RuleEngine.process()` dispatches entirely different CLI grammars (`pc`,
`asa`, `apresia`, `bigip`, `ipcom`) to a self-contained `_xxx_process(cmd, c,
state)` method before falling into the shared Cisco/IOS-style command tree
that `catalyst`/`cisco`/`srs`/`sir`/`nexus` share. When adding a device
whose CLI paradigm doesn't fit the Cisco `exec → config → config-if` mode
model, follow this pattern rather than bolting new device-type checks onto
the generic Cisco branch.

### Config sub-modes (Cisco-style devices)

`CONFIG_SUBMODES` in `engine/rules.py` is a single registry that both (a)
the "which modes accept config commands" allow-list and (b) `exit`'s
"which mode does this one return to" table are derived from. Previously
these had to be kept in sync by hand in two places, which was a real
source of "% Invalid input detected" bugs when adding a new sub-mode.
**Add exactly one line to `CONFIG_SUBMODES`** when introducing a new
config sub-mode; don't hand-edit the allow-list or the exit table
separately. `tests/test_config_submodes.py` exhaustively checks the
registry's internal consistency (every submode's parent is reachable, no
cycles, etc.) — device-specific handlers like `_ipcom_process` that manage
their own mode machine (not via `CONFIG_SUBMODES`) are exempt.

### Key modules

- `app.py` — FastAPI app, HTTP/WebSocket API, the two dispatch layers
  above, `DEFAULT_DEVICES`, device session management (`device_sessions:
  Dict[device_id, DeviceState]`).
- `engine/rules.py` — `DeviceState` (per-device mutable state,
  `__init__` branches by `device_type`) and `RuleEngine` (CLI command
  processing, ~11k lines, one `_xxx_process`/`_xxx_show_*` family per
  device type plus the shared Cisco-style tree).
- `engine/protocols.py` — the shared protocol engines (RIP/OSPF/BGP/EIGRP/
  STP/VRRP/etc. state machines and their `rib_engine`-style route tables).
  These are shared across device types; don't duplicate their state.
- `engine/real_*_agent.py`, `engine/snmp_udp_agent.py`,
  `engine/netconf_agent.py`, `engine/ssh_cli_agent.py`,
  `engine/telnet_cli_agent.py`, `engine/gnmi_agent.py` — the real-socket
  protocol listeners mentioned above.
- `static/index.html` — the browser CLI/topology UI. Device-type-specific
  frontend bits (prompt suffix in `getPrompt()`, interface-name templates
  in `defPort()`/`portOptions()`, the "add device" buttons, the vendor
  auto-detect regex in `_inferDeviceType()`) need updating in parallel
  with backend device-type additions, or the new device type won't be
  selectable/won't render its prompt correctly in the UI.
- `tools/` — operational scripts (Prometheus exporter, Grafana/Loki stack
  setup for both Linux `.sh` and Windows `.ps1`, syslog→Loki bridge,
  LogicMonitor mock, network ontology query tool, etc.) — see `docs/*.md`
  for the write-up of each.
- `docs/` — one markdown file per feature area, written with an actual
  live-verification transcript (real commands run against a real running
  instance, not just code review) rather than just a design description.
  Follow that pattern for new docs.

## Working conventions for this repo (established in this session)

- Development happens on `claude/affectionate-johnson-rz69wa`; push there
  directly unless told otherwise.
- **Run the full `pytest tests/ -q -p no:cacheprovider` suite before every
  push**, in the background if it's the ~15-minute full run, and confirm
  the only failures are the two known-flaky
  `tests/test_ipsec_dpd_timers.py::TestSirConfigParsing` tests (pass in
  isolation) — anything else is a real regression to fix, not to wave off.
- Don't run a live multi-service stack (the monitoring stack's `setup`,
  a live app.py instance binding real protocol ports) concurrently with
  the regression suite — the real BGP/OSPF/SNMP port binding in both
  collides and produces spurious `ERROR`/`ports already in use` failures
  that look like real breakage but aren't; stop one before running the
  other.
- Live-verify new features against a real running instance
  (`NETLAB_AUTH_DISABLE=1 python -m uvicorn app:app ...` + `curl`/`/api/cli`)
  before writing it up — this has repeatedly caught real bugs (duplicate
  route entries, a device-specific CLI quirk like IPCOM's `router ospf`
  taking no process-id, Windows-only `UnicodeEncodeError`s) that code
  review alone missed.
