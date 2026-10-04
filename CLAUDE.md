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
`srs`, `apresia`, `bigip`, `arista`, `juniper`, `yamaha`, `bas`, `pc`).

**Publicly deployed** at https://network-lab-emulator.onrender.com/ (Render).
That deployment doesn't have Ollama available, so its `network_ontology_query.py
--summarize`-style AI-summary features show "Ollamaが検出されませんでした。
ollama pull llama3 実行後、python app.py を再起動してください。" — expected
there, not a bug to chase from this sandbox.

**このプロジェクトは https://network-lab-emulator.onrender.com/ (Render) で
一般公開している。** そのデプロイ環境にはOllamaが無いため、
`network_ontology_query.py --summarize`のようなAI要約機能では
「Ollamaが検出されませんでした。ollama pull llama3 実行後、python app.py
を再起動してください。」という表示が出るのが正常(このサンドボックスから
追いかけて直すべきバグではない)。

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
  free without reimplementing RIP/OSPF — the pattern is: transition the
  mode in the device-specific `_xxx_process` handler, then let the
  already-executed `app.py` layer's side effects stand rather than
  re-deriving state locally. Duplicating
  state between the two layers (e.g. a device-specific `state.static_routes`
  list next to the shared `rib_engine`) has caused real duplicate/garbled
  output bugs in this codebase — prefer delegating to the shared engine.

### Device-specific CLI dialects: dedicated `_xxx_process` handlers

`RuleEngine.process()` dispatches entirely different CLI grammars (`pc`,
`asa`, `apresia`, `bigip`) to a self-contained `_xxx_process(cmd, c,
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
cycles, etc.) — device-specific handlers like `_apresia_process` that
manage their own mode machine (not via `CONFIG_SUBMODES`) are exempt.

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

**IPCOM EX2 support was added earlier this session, then removed at the
user's request** ("ipcomに関するエミュレータ動作があれば削除して欲しい").
Removed: the `device_type == "ipcom"` branch of `DeviceState.__init__`,
the `_ipcom_process`/`_ipcom_show_interfaces`/`_ipcom_show_config`
handlers and their dispatch in `RuleEngine.process()` (`engine/rules.py`),
the two `state.device_type == 'ipcom'` checks in `app.py`, all IPCOM
references in `static/index.html` (add-device buttons, `DEVICE_META`
entry, `getPrompt()` branch, `ipcom_admin` sync, `defPort()`/
`portOptions()` entries, `_inferDeviceType()` regex, the color map), and
`docs/ipcom-ex2.md`. No test file referenced "ipcom" at all, so this was
a clean removal with nothing to update on the test side. Full regression
suite confirmed clean afterward (only the two known-flaky IPsec DPD
tests). If IPCOM support is wanted again, `git log` on
`claude/affectionate-johnson-rz69wa`/`main` around this point has the
original implementation to revive rather than rebuilding from scratch.

**IPCOM EX2対応はこのセッションの前半で追加したが、ユーザーの依頼**
（「ipcomに関するエミュレータ動作があれば削除して欲しい」）**により削除した。**
削除対象: `DeviceState.__init__`の`device_type == "ipcom"`分岐、
`_ipcom_process`/`_ipcom_show_interfaces`/`_ipcom_show_config`ハンドラと
`RuleEngine.process()`でのディスパッチ(`engine/rules.py`)、`app.py`の
`state.device_type == 'ipcom'`チェック2箇所、`static/index.html`内の
全IPCOM参照(装置追加ボタン、`DEVICE_META`エントリ、`getPrompt()`の分岐、
`ipcom_admin`同期、`defPort()`/`portOptions()`のエントリ、
`_inferDeviceType()`の正規表現、色マップ)、`docs/ipcom-ex2.md`。
テストファイルは一切"ipcom"を参照していなかったため、テスト側の
修正は不要なクリーンな削除だった。削除後、全体回帰テストで
クリーンであることを確認済み(既知のflaky IPsec DPDテスト2件のみ)。
もし再度IPCOM対応が必要になった場合、`claude/affectionate-johnson-rz69wa`
/`main`のこの時点付近の`git log`に元の実装が残っているので、
ゼロから作り直すのではなくそちらを復活させればよい。

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
  route entries, device-specific CLI quirks, Windows-only
  `UnicodeEncodeError`s) that code review alone missed.

**`ansible/roles/dify/`**: an independent, unrelated-to-the-emulator
Ansible role that stands up [Dify](https://github.com/langgenius/dify)
(an LLM app/RAG platform) via docker compose, added because the user
asked whether this sandbox could run one. `cr.weaviate.io` (Dify's
vector-DB image registry) is blocked by this environment's egress
policy the same way `download.pytorch.org`/`api.groq.com` are — worked
around by pulling the same image from Docker Hub
(`semitechnologies/weaviate`) and re-tagging it locally. Not wired into
the default `ansible/site.yml` run (only via `--tags dify`) since it's
a heavy, unrelated 16-container stack. Docker/Docker Compose are
already installed in this sandbox and the daemon starts fine via
`nohup dockerd &` despite there being no systemd (`systemctl` fails
with "System has not been booted with systemd as init system") — the
role handles both cases. See `docs/dify-ansible.md` for the full
live-verification transcript (cold start with the daemon stopped,
Web UI + API responding, and a second idempotent run).

**AAA named method lists + `line con 0`** (`app.py`'s `handle_protocol_config`,
the `device_type in ('cisco', 'catalyst')` AAA block around
`aaa new-model`): found while looking at
[CiscoDevNet/cml-community](https://github.com/CiscoDevNet/cml-community)'s
`lab-topologies/aaa-tacacs-exploration` for ideas — that lab's exact
command sequence (`aaa authentication login CONSOLE local` / `aaa
authorization console` / `aaa authorization exec CONSOLE local` /
`line con 0` / `login authentication CONSOLE` / `authorization exec
CONSOLE`) was silently swallowed with no error and never reflected in
`show running-config`: only the `default` named method list was
supported for `aaa authentication login`/`aaa authorization exec`, and
`line con 0` itself wasn't accepted (`line vty` only), so anything
typed "inside" it (via `config-line` mode, which never got entered)
was a no-op too. Fixed to accept arbitrary method-list names
(`state.aaa_authentication_login_lists`/`aaa_authorization_exec_lists`,
keyed by name, case-preserved) while keeping the pre-existing
`state.aaa_authentication_login`/`aaa_authorization_exec` single-dict
attributes as aliases for the `default` entry (existing Nexus
TACACS+ tests read these directly); added `aaa authorization console`
and `line con 0` → `config-line` with `login authentication
<name>`/`authorization exec <name>` sub-commands, tracked separately
per line type (`state.line_con_*` vs `state.line_vty_*`). Live-verified
the exact cml-community command sequence against a running instance
end to end (`show running-config` reflects every line, case preserved);
`tests/test_aaa_named_method_lists.py` (14 tests) fixes this plus the
`default`-list backward-compat.

**`arista` device type**: added after confirming via cml-community's
`node-definitions/arista/` that EOS's `configure`/`configure terminal`,
`write`/`write memory`/`copy running-config startup-config`, and
`interface` → config-if mode transitions are essentially identical to
IOS. Unlike `apresia`/`bigip`, no dedicated `_arista_process` handler
was needed — `arista` just falls through into the existing shared
Cisco-style tree in `RuleEngine.process()` (confirmed live: `router
ospf 1` correctly entered `config-router`, no gNMI/OSPF-engine wiring
needed). Only 3 things are EOS-specific: interface naming
(`Ethernet<N>`, not `GigabitEthernet0/0/N`, set in `DeviceState.__init__`'s
interfaces ternary), `show version` (EOS's short field-list format
instead of IOS's long banner, in `_show_version`), and the `show
running-config` header (`! Command: show running-config` / `! device:
...` instead of IOS's `Building configuration...`, in
`app.py`'s `is_cisco` block — `is_cisco` now includes `arista`).
Frontend: `static/index.html`'s `DEVICE_META`, add-device buttons,
`defPort()`/`portOptions()`, `_inferDeviceType()`, and the bulk-import
color map all got an `arista` entry.

**Found while live-verifying**: `/api/device`'s body keys are
`id`/`type`/`hostname` — **not** `device_id`/`device_type` like
`/api/cli` uses. Posting the wrong keys to `/api/device` silently
no-ops (the `if dev_id and ...` guard just never fires), and the
device then gets auto-created as a plain `cisco` default on the first
`/api/cli` call that touches it (`DEFAULT_DEVICES.get(device_id,
{"type": "cisco", ...})` fallback) — no error, just the wrong
device_type. Caught this because an "Arista" test device kept
rendering as a vanilla Cisco ISR4321 until the request body was fixed
to use `id`/`type`. Worth remembering next time a live-verification
curl call "works" but shows unexpected default-looking output — check
the endpoint's actual body keys before assuming the feature itself is
broken.

`tests/test_arista_eos.py` (8 tests) covers all of the above.

**Live-verified Arista⇔Cisco LLDP** (user request: "Arista⇔Ciscoで実際に
LLDP試してみて"): `_update_neighbors`/`_rebuild_all_neighbors` in
`app.py` build `lldp_neighbors` dynamically from `vnet` topology links
for *all* device types (no per-vendor allowlist), so Arista needed no
code change to participate — confirmed live by linking an `arista` and
a `cisco` device via `/api/link` and running `show lldp neighbors` on
both sides. Two real things found while doing this (not guessed from
code review):
1. `_show_lldp`/`_show_lldp_detail` in `engine/rules.py` only gate on
   `lldp_enabled` (off by default, needs `lldp run`) for
   `device_type in ('cisco', 'catalyst', 'nexus')` — `arista` isn't in
   that tuple, so an Arista device shows LLDP neighbors immediately
   with no `lldp run` needed. Left as-is: this matches real Arista EOS,
   which runs LLDP on all interfaces by default unlike Cisco IOS.
2. **Real bug, fixed**: `_show_lldp`'s Cisco-format table hard-pads
   `Local Intf`/`Port ID` to fixed widths (16/trailing) using the full
   interface name (`GigabitEthernet0/0/0`, 20 chars) — wider than the
   column, so it ran into the `Hold-time` column with no separating
   space (`GigabitEthernet0/0/0120        B`). Real Cisco IOS always
   abbreviates interface names in this table (`Gi0/0/0`), which is
   exactly the `_abbrev_if()` static method already used elsewhere in
   `engine/rules.py` for other show commands — just wasn't applied
   here. Fixed by routing `local_if`/`port_id` through `_abbrev_if()`
   in `_show_lldp`. `TestAristaCiscoLldp` in `tests/test_arista_eos.py`
   (4 new tests) covers the default-disabled-on-Cisco/enabled-by-
   default-on-Arista asymmetry, the real neighbor discovery both
   directions, and pins the alignment fix with a long-interface-name
   regression case.

**`juniper` device type**: added after cml-community's `node-definitions/
juniper/` (vSRX, vJunos-Router/Switch/Evolved, QFX, vMX) — the first
vendor added that does *not* ride the shared Cisco-style tree. Junos's
CLI paradigm (`set`/`delete`/`commit` against a two-stage candidate/
active config, rather than Cisco's immediately-applied `interface` →
config-if submode editing) is different enough that it gets its own
`_juniper_process(cmd, c, state)` handler in `engine/rules.py`,
dispatched at the very top of `RuleEngine.process()` (same pattern as
`apresia`/`bigip`) — it never touches the shared Cisco tree at all.

Implemented scope: `configure`/`exit`/`commit`/`commit and-quit`/
`rollback` mode transitions; `set interfaces <if> unit <n> family inet
address <ip>/<prefix>` and `set system host-name <name>` staged into
`state.junos_candidate` and only applied to `state.interfaces`/
`state.hostname` on `commit` (real two-stage Junos behavior —
confirmed live that an address set before `commit` does **not** show
in `show interfaces terse` until after `commit`); `delete <path>`;
`show configuration` (rendered as real Junos's brace-hierarchy, built
by grouping the flat committed `set` lines by path segment) and `show
configuration | display set` (the flat form); `show version` (Junos-
style); `show interfaces terse`; `show lldp neighbors` (delegates to
the existing shared `_show_lldp` Cisco-format table — Junos's real
neighbor-table format wasn't reimplemented, scoped out). Interface
naming is `ge-0/0/<N>` (vSRX convention), set in `DeviceState.__init__`'s
interfaces ternary. Not implemented (scoped out, no concrete need
yet): `edit <path>` hierarchy descent (only full-path `set`/`delete`
is supported, no path-stack/`[edit ...]` prompt tracking), and the
"uncommitted changes, exit anyway?" confirmation real Junos shows.

**Found while implementing**: the shared `_expand_abbreviation()` CLI-
abbreviation expander (e.g. `sh` → `show`) runs unconditionally before
device-type dispatch and is Cisco-abbreviation-table-driven, so it
mangled `show interfaces terse` into
`show interfaces tengigabitethernetrse` (`te` → `TenGigabitEthernet`)
for `juniper` devices — same class of problem `apresia` already had
and already carries a `device_type == 'apresia'` skip for. Fixed by
adding `juniper` to that skip list rather than inventing a new
mechanism.

Frontend (`static/index.html`): `DEV_TEMPLATES` (not `DEVICE_META` —
corrected from an earlier, slightly wrong note in this file) gets a
`juniper` entry; add-device buttons (launcher + sidebar);
`defPort()`/`portOptions()` (`ge-0/0/<N>`); `_inferDeviceType()` regex
(`set interfaces|system|routing-options`/`delete interfaces`/`junos`);
bulk-import color map; and `getPrompt()` gets a `d.type==='juniper'`
branch since Junos prompts are `hostname>` (exec)/`hostname#`
(config) with no `(config-if)`-style parenthesized submode suffix,
unlike every other device type here.

Live-verified end-to-end via a real running instance: `configure` →
`set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24` →
`commit` → `show interfaces terse` shows the new address;
`show configuration` renders the real brace hierarchy.
`tests/test_juniper_junos.py` (12 tests) covers all of the above,
including the not-applied-before-commit / applied-after-commit
distinction and `delete` removing a committed line.

**`/dashboard`**: a "cool-looking" live resource monitor page, added on
request ("稼働してるモックのリソース感じもかっこいい画面で見たい").
`static/dashboard.html` is a standalone dark/neon-green terminal-themed
page that polls the pre-existing `/api/snmp/dashboard` endpoint (which
already aggregated per-device CPU%/interface octet counters/route
count/60-sample history via `engine.protocols.SnmpAgent` — it just had
no frontend before this) every 2s and renders a card grid: per-device
CPU bar, interfaces-up fraction, a canvas sparkline of CPU history, and
a computed throughput rate from the byte-count delta between polls.
No new metrics/measurement code was added — this is purely a
visualization on top of data the SNMP agent already collected.
Served via a new `@app.get("/dashboard")` route in `app.py` (reads
`static/dashboard.html`, same pattern as the `/` → `index.html` route);
added `/dashboard` to `_NO_AUTH_PATHS` so the page itself loads without
a login redirect (its `fetch('/api/snmp/dashboard')` still goes through
normal token auth — it reads the same `netlabToken_v1` localStorage key
`index.html` writes on login, so being logged into the main CLI tab is
enough). Live-verified with Playwright (`/opt/pw-browsers/chromium`):
screenshotted the page against a real running instance with 20+ real
devices (Cisco/Catalyst/Arista/APRESIA mix accumulated from this
session's testing) showing live CPU bars and sparklines.
`tests/test_resource_dashboard_page.py` (3 tests) pins the route, the
auth-exempt path list entry, and that `dashboard.html`'s token-storage
key stays in sync with `index.html`'s (a drift there would silently
break the dashboard for logged-in users with a 401).

**gNMI on `/dashboard`**: added on follow-up request ("gnmiもお願いします").
`engine/gnmi_agent.py` only ran its gRPC server for devices that had
`gnxi server` configured (Cisco/Catalyst only), tracked in a private
`_servers: {device_id: GnmiServer}` registry with no public accessor —
added `active_gnmi_device_ids()`/`gnmi_port_for(device_id)` so `app.py`
doesn't reach into the private dict directly. `/api/snmp/dashboard`
now adds a `gnmi` field per device (`None` unless gNMI is actually
active for it) with `port`/`interfaces_enabled`/`interfaces_total` —
read directly from the same `state.interfaces` gNMI's own `get_value()`
serves, **not** a real gRPC `Get`/`Subscribe` call (no new RPC traffic
added, this is just surfacing what gNMI already exposes). Deliberately
separate from the SNMP-sourced `cpu_percent`/traffic history fields:
this emulator's gNMI implementation doesn't expose CPU or interface
octet counters at all (`get_value()` only serves OpenConfig-style
admin/enabled + IP config), so a gNMI-enabled device shows its own
small cyan "gNMI :50052 live / IF 2/3" row on the dashboard card
instead of fabricating CPU/traffic numbers gNMI doesn't actually have.
`static/dashboard.html` renders it both on initial card build and on
each poll tick (added/removed live if `gnxi server`/`no gnxi server`
toggles while the dashboard is open). Live-verified: enabled `gnxi` +
`gnxi server` on a live Cisco device, confirmed `/api/snmp/dashboard`'s
`gnmi` field populated, and screenshotted the rendered badge on a real
running instance. Asked about WMI too (Windows host management, not a
network-device protocol) — out of scope, this emulator only models
network-equipment protocols (SNMP/NETCONF/SSH-CLI/Telnet-CLI/gNMI/
RESTCONF), so declined rather than bolting on an unrelated Windows-host
emulation feature; noted as a separate, larger ask if ever wanted.
`TestGnmiOnDashboard` in `tests/test_resource_dashboard_page.py`
(3 tests) covers the off/on `gnmi` field states and the badge markup.

**netmiko / Ansible real-automation compatibility for Arista & Juniper**:
on request ("その他ansibleで取得や、netmikoでの運用など柔軟に行きましょう"),
live-verified (not just unit tests) that real `netmiko.ConnectHandler`
(`device_type='arista_eos'`/`'juniper_junos'`) and real
`ansible-playbook` (`arista.eos.eos_command`) can connect to this
emulator's own real SSH-CLI listener (`engine/ssh_cli_agent.py`) for
the two device types added earlier this session. Found and fixed real
gaps along the way, not guessed from reading code:

1. **SSH was never reachable at all for Arista/Juniper.** The real-SSH-
   listener activation command (`crypto key generate rsa`, in `app.py`'s
   `handle_protocol_config`) was gated to `device_type in ('cisco',
   'catalyst')` only. Added `'arista'` to that tuple (EOS doesn't use
   this exact IOS command for SSH in reality, but it's the same
   pragmatic "good enough to test against" choice already made for
   Arista elsewhere in this codebase). Juniper got the *authentic*
   command instead, since it already has a real set/commit model:
   `set system services ssh` stages into `junos_candidate`
   (`_juniper_process` in `engine/rules.py`) and sets
   `state.ssh_rsa_key = True` on `commit` (`_junos_apply_commit`).

2. **Ordering bug found via live netmiko/Ansible testing, not visible
   from code review**: the SSH-listener startup for Juniper was
   initially wired into `app.py`'s `handle_protocol_config`, which (per
   the two-layer dispatch order) runs **before**
   `rule_engine.process()` — so it started the listener using
   `state.interfaces`' *old* IP, before `_juniper_process`'s own commit
   handling had applied the newly-`set` interface address. A real
   `set interfaces ge-0/0/0 ... address X` + `set system services ssh`
   + `commit` sequence would bind the SSH listener to the *default*
   203.0.113.2 instead of the address just configured, then netmiko/
   Ansible would time out connecting to the (correct) new address.
   Fixed by moving the listener-start check to right after
   `rule_engine.process()` in `app.py`'s `cli_command()`, keyed off
   `'system services ssh' in state.junos_committed` (i.e., after the
   real commit has already run).

3. **netmiko's own terminal-setup commands weren't answered.** Both
   drivers send device-specific "quiet the terminal" commands right
   after login and hang/timeout waiting for a specific response text
   (not just any response) — discovered by literally running netmiko
   against a live instance, not from netmiko's docs:
   - `arista_eos`: `terminal width <n>` expects `"Width set to N
     columns."`, `terminal length 0` expects `"Pagination disabled."`
     (both real EOS behavior; added to `app.py`'s terminal-command
     handler, the length-0 one scoped to `device_type == 'arista'`
     since real IOS stays silent for the same command).
   - `juniper_junos`: `set cli screen-width <n>` /
     `set cli screen-length <n>` / `set cli complete-on-space off`
     expect `"Screen width set to N"` / `"Screen length set to N"` /
     `"Disabling complete-on-space"` respectively — these are real
     Junos *exec-mode* display preferences (not candidate config, no
     commit involved, mode-independent), added as their own branch in
     `_juniper_process` before the `configure`-mode gate.

4. **Ansible's `arista.eos.*` modules don't work at all without `show
   version | json` and `show hostname | json`** — EOS's cliconf plugin
   queries both for `get_device_info()` on every single task
   invocation (confirmed live: even an unrelated `eos_command` task for
   `show running-config` failed here first). Implemented both as
   eAPI-shaped JSON (`app.py`, gated to `device_type == 'arista'`,
   intentionally *not* folded into the generic `include`/`exclude`-
   style `_split_output_modifier`/`_apply_output_modifier` pipeline
   since JSON is a different response shape per command, not a filter
   over existing text). Live-verified end-to-end: a real
   `ansible-playbook` run with `arista.eos.eos_command` (`network_cli`
   connection, real SSH, host-key checking off) successfully fetched
   this emulator's real `show running-config` output over genuine
   Ansible automation.

**Known gap, not fixed (next step if wanted)**: `junipernetworks.junos`
Ansible modules default to a NETCONF transport (`ansible_connection:
netconf`), not `network_cli`/SSH — live-testing hit this directly
(`junos_command` over `network_cli` failed with an internal
`'NoneType' object has no attribute 'strip'`, consistent with the
module expecting NETCONF RPC replies it never got over plain SSH).
This emulator already has a real NETCONF agent
(`engine/netconf_agent.py`), but — like SSH was — its activation isn't
wired up for `juniper` yet (real Junos command is `set system services
netconf ssh`, which `_juniper_process`'s `set` allowlist doesn't
recognize yet, confirmed live: `"syntax error: 'system services
netconf ssh'"`). `netmiko`'s `juniper_junos` SSH driver (verified
working, see above) is unaffected since it's plain CLI-over-SSH, not
NETCONF.

`tests/test_netmiko_ansible_compat.py` (14 tests) covers all the fixes
above (HTTP-API-only, same pattern as the existing
`tests/test_netmiko_catalyst.py` — no live netmiko/Ansible dependency
in the regression suite itself, consistent with the project's existing
approach of keeping real-automation-tool testing as a manual/optional
live-verification step rather than a CI dependency). `netmiko` (4.8.0)
and `ansible`/`arista.eos`/`junipernetworks.junos`/`ansible.netcommon`
collections were already available in this sandbox for the live
verification; not added to `requirements*.txt` since nothing in the
actual test suite imports them.

**`yamaha` device type (RTX1300)**: added on explicit request
("Ｙａｍａｈａルータ　ＲＴＸ１３００もコマンド投入試験できますか？" →
"できればルーティングなど反映してほしいです" — the routing-must-actually-
work requirement was the priority). RTX's CLI paradigm (`administrator`
to elevate from user mode straight into a config-accepting mode with no
separate `configure terminal` submode, `ip lanN address`, `ip route
<dest> gateway <ip>`, `nat descriptor`, `pp select <N>` for PPPoE) is
its own grammar, so it gets a dedicated `_yamaha_process` handler in
`engine/rules.py` (same `apresia`/`bigip`/`juniper` pattern) dispatched
before the shared Cisco tree. `defPort()`/`_expand_abbreviation()` skip
list updated the same way Juniper's was.

**Routing is delegated to the shared `rib_engine`, not reimplemented**
(the explicit ask): `ip route (default|<net>/<prefix>) gateway <ip>`
got its own regex in `app.py`'s `handle_protocol_config` (alongside the
existing `cisco_route`/`sir_route` patterns) calling
`rib_engine.add_static_route()` directly — `_yamaha_process` itself
just silently acknowledges the command once `handle_protocol_config`
has already run (two-layer dispatch order). **`show ip route` needed
zero Yamaha-specific code**: `app.py`'s `handle_protocol_show` already
renders `rib_engine`'s routes device-type-independently, so a Yamaha
device's connected/static routes (gateway of last resort included) show
up for free — confirmed live that `ip route default gateway X` then
`show ip route` shows a real `S* 0.0.0.0/0 [1/0] via X` line, and that
`ip lan1 address` immediately produces a real `C`/`L` connected-route
pair. (An initial draft wrote a separate `_yamaha_show_ip_route`
renderer before noticing it was unreachable dead code — the shared
handler always wins per the two-layer dispatch rule; removed it rather
than leaving dead code around, per the same "check app.py first" advice
this file already gives for exactly this situation.)

**NAT descriptor (`nat descriptor type N masquerade` / `ip lanN nat
descriptor N`) and PPPoE (`pp select N` sub-context: `pppoe use lanN`,
`pp auth myname`, `pp enable N`) are config-acceptance-and-`show
config`-reflection only** — deliberately scoped out: real packet-level
NAT translation and real PPPoE session negotiation would each be a
project on the scale of the real-socket BGP/OSPF agents, not a fit for
this request's actual ask (which was specifically about routing).
Interface naming is `lan1`/`lan2`/`lan3` (no `GigabitEthernet0/0/N`).
`tests/test_yamaha_rtx.py` (16 tests) covers the mode transitions,
interface config, the NAT/PPPoE config-reflection, and — the priority —
5 tests specifically asserting routes actually show up in both `show
ip route` and `show config` after being configured, including a
connected-route and a `no ip route` removal case.

**Pseudo-FLET'S network (`bas` device type + `engine.protocols.PppoeEngine`)**:
on follow-up request ("擬似ふれっつ網も作れますか？" → "擬似ふれっつ網と
繋ぎたいと言えば繋がる？"), implemented real PPPoE negotiation between a
new `bas` device type (a pseudo access-concentrator/収容局, representing
NTT FLET'S-style PPPoE termination) and Yamaha's `pp select` PPPoE client
config, which was previously config-acceptance-only.

**Explicit architectural constraint discussed with the user before
building this** (important for any future real-socket protocol work):
real PPPoE Discovery (PADI/PADO/PADS) runs as raw Ethernet frames
(Ethertype 0x8863, below IP, needs AF_PACKET on a shared L2 broadcast
domain) — but this emulator's devices only share loopback-IP-alias
adjacency, not real L2, so genuine Ethernet-frame PPPoE Discovery isn't
achievable here. Went with the same pattern `OspfEngine`/`RipEngine`
already use instead: a software-level engine (`PppoeEngine` in
`engine/protocols.py`) that resolves adjacency via `vnet.get_neighbors()`/
`vnet.interface_links`/`vnet.device_types` (same mechanism those engines
use) and performs the LCP/PAP-CHAP-auth/IPCP-IP-assignment *stages*
faithfully as real state transitions — just not as literal real Ethernet
frames. This keeps the "real protocol state machine" part of the
project's ethos while being honest that it isn't literally
interoperable with a real OS-level PPPoE client (which needs real L2).

Implementation: `PppoeEngine.nodes[bas_device_id]` holds the configured
IP pool (`ip pool <start> <end>/<prefix>`) and accepted credentials
(`pppoe-user <name> <password>`) — set directly by `bas`'s own CLI
handler (`_bas_process` in `engine/rules.py`, dispatched like
`apresia`/`juniper`/`yamaha`; a custom non-vendor-specific CLI since
"pseudo-FLET'S BAS" isn't a real commercial product to emulate
faithfully). `PppoeEngine.connect(rtx_id, pp_id, pppoe_lan_iface, user,
password, hostname)` finds a `device_type == 'bas'` neighbor reachable
via that exact interface, checks credentials, and allocates the next
free IP from the pool — called from `app.py`'s `handle_protocol_config`
on `pp enable <N>` (not from `_yamaha_process`, same reasoning as the
Juniper SSH-listener-ordering fix: this needs to mutate shared state
`_yamaha_process` doesn't own). On success, the assigned IP becomes a
real `state.interfaces['pp<N>']` entry (same source of truth
`_register_icmp()`/`rib_engine`'s connected-route derivation/`show`
commands already use for every other interface — not a parallel
tracking structure) and, if `ip route default gateway pp <N>` was
configured (parsed separately, stored pending until the gateway IP is
actually known), a real default route is registered via
`rib_engine.add_static_route()` pointing at the BAS's own interface IP.
`pp disable <N>` tears both back down. Auth failure or no `bas` device
linked on that interface leaves everything unchanged (no fabricated
success).

Live-verified end-to-end via a real running instance: BAS configured
with a pool + user, RTX's `pp select 1` configured with matching
credentials and `ip route default gateway pp 1`, linked via `/api/link`
— `pp enable 1` assigned a real pool IP, `show ip route`/`show config`
both reflected the new default route via the BAS's real IP, `show
pppoe session` on the BAS showed the session, and a wrong password /
no link / linking to a non-`bas` device all correctly assigned nothing.
**Found via this live run, not guessed**: `show running-config` for
`bas` devices was being intercepted by `app.py`'s `_build_running_config`
(same two-layer-dispatch trap as Juniper's `show ip route` earlier this
session — a generic app.py handler runs before any device-specific
RuleEngine handler gets a chance) and returned garbled Si-R-style
output instead of `_bas_process`'s own config rendering; fixed by
adding `bas` to the explicit device-type delegation list there (same
pattern already used for `apresia`).

`tests/test_pseudo_flets_pppoe.py` (9 tests) covers the BAS CLI, a
successful connect assigning a real pool IP, the BAS seeing the
session, the default-route-via-pp case (the routing-reflected
requirement, PPPoE edition), `pp disable` teardown, and three failure
cases (wrong password, no BAS linked, linked to a non-`bas` device).

**Yamaha RTX ⇔ Cisco IOS IPsec over the pseudo-FLET'S PPPoE link**:
on direct follow-up request ("擬似ふれっつもうを経由して他のCiscoと
IPsec貼れるか試して欲しい"), extended Yamaha's `tunnel select N` from
config-acceptance-only to a real, negotiating IPsec tunnel — and
confirmed live that it actually establishes over the dynamically-
assigned PPPoE address from the `bas` work above, against a real Cisco
IOS `crypto map` peer.

`engine/ike_engine.py` already negotiated Si-R↔Si-R/Si-R↔Cisco IOS/ASA/
Cisco↔Cisco via a `state.ipsec_tunnels` dict format (Si-R's shape:
`local_ip`/`remote_ip`/`preshared`/`encryption`/`hash`/`dh_group`/
`ike_mode`/`ike_lifetime`/`protocol`/`phase1`/`phase2`/`status`) — rather
than inventing a new Yamaha-specific negotiation path, `_yamaha_process`
populates that exact same dict shape for `tunnel select N` (`ipsec sa
policy`/`ipsec ike local-address`/`remote-address`/`pre-shared-key`/
`group`), and every `dt in ('sir', 'srs')`/`pdt in ('sir', 'srs')` check
throughout `ike_engine.py` (9 occurrences — `_find_peer`'s adjacency
match, `negotiate_ipsec`'s initiator and peer-info branches, the Phase1/
Phase2 state mutation branches) became `('sir', 'srs', 'yamaha')`. This
means Yamaha↔Cisco and Cisco↔Yamaha both just work through the existing
negotiation logic with zero new matching code — same "extend the
existing tuple, don't add a parallel code path" pattern already used
for `is_cisco`/`_expand_abbreviation`'s device-type tuples elsewhere in
this codebase.

`tunnel enable <N>` is wired the same way Si-R's `ike use on`/`ipsec use
on` already were: a device-type-agnostic command-text match in `app.py`
(both the `handle_protocol_config` pre-RuleEngine trigger, and the
post-`rule_engine.process()` re-trigger for ordering robustness) sets
`state.ike_enabled`/`state.ipsec_enabled` and calls
`_trigger_ike_negotiation()`; `tunnel disable <N>` tears the specific
tunnel's phase1/phase2/status back to `LARVAL`/`wait`. `show status
tunnel <N>` (new, `_yamaha_show_tunnel_status`) renders the real
negotiated state — real RTX's actual command name, not invented.

Live-verified end-to-end against real running devices: Cisco with a
static WAN IP and a standard `crypto isakmp key`/`crypto ipsec
transform-set`/`crypto map` config; a `bas` device with a pool and
PPPoE credentials; a `yamaha` device that first ran `pp enable 1` to
get a real dynamic IP from the pool (confirming the IP actually used
for `ipsec ike local-address` was the one PPPoE assigned, not a
hardcoded value), then `tunnel select 1` + the IPsec commands + `tunnel
enable 1` — `show status tunnel 1` showed `IKE negotiation: MATURE` /
`IPsec SA: MATURE` / `status: established`. Also verified failure paths
don't fabricate success: wrong pre-shared-key → `DYING`/`wait`, and no
PPPoE connection (no valid dynamic local IP) → never reaches
`established`. `tunnel disable 1` correctly tears the SA back down.

`tests/test_yamaha_cisco_ipsec_over_pppoe.py` (6 tests) covers the
PPPoE-assigned-IP-feeds-into-IPsec chain, successful establishment (and
that `tunnel enable` is silent on success, matching real RTX), the
wrong-PSK failure case, teardown via `tunnel disable`, and the
no-PPPoE-connection case. Also re-ran `tests/test_ipsec_dpd_timers.py`/
`tests/test_ipsec_manual_key.py` (the existing Si-R/Cisco IPsec test
files) to confirm the `ike_engine.py` tuple-widening didn't regress the
pre-existing Si-R↔Cisco paths — both clean.

## ML / anomaly detection notes（機械学習・異常検知メモ）

PyTorch could not be installed in this sandbox early on (pulled in a
multi-GB CUDA/cuDNN toolkit that exhausted the ~2.6GB free disk space at
the time), so the existing ML-ish tools are plain NumPy:
`tools/anomaly_autoencoder.py` (reconstruction-error-based anomaly
detection) and `tools/link_capacity_forecast.py` (traffic forecasting).
This turned out to be enough — the data volume/complexity this emulator
produces doesn't need a deep-learning framework. `tools/network_ontology_query.py
--summarize` separately does AI *interpretation* of retrieved logs via
Ollama (not anomaly detection).

Re-checked later in the session (disk had since grown to ~27GB free):
disk space is no longer the blocker, but `pip install torch` from the
default PyPI index now pulls in the full CUDA/cuDNN wheel set regardless
(`nvidia-cudnn-cu13`, `cuda-toolkit`, `triton` ~248MB, etc. — CPU-only
PyPI wheels for CUDA-capable platforms don't seem to exist for this
Python/platform combo), and the official CPU-only wheel index
(`download.pytorch.org`) is blocked by this environment's egress policy
(403 on CONNECT). So PyTorch remains impractical here for a different
reason than before — decided to keep the NumPy implementation rather than
install the CUDA stack for a CPU-only, GPU-less sandbox.

このサンドボックスではPyTorchが当初インストールできなかった（数GB規模の
CUDA/cuDNNツールチェーンを引き込み、当時の空きディスク容量(~2.6GB)を
使い切るため）。そのため既存の機械学習系ツールはNumPyのみで実装している:
`tools/anomaly_autoencoder.py`（再構成誤差ベースの異常検知）と
`tools/link_capacity_forecast.py`（トラフィック予測）。結果的にこれで
十分だった — このエミュレータが生成するデータの量・複雑さは深層学習
フレームワークを必要としない。`tools/network_ontology_query.py
--summarize`は別途、取得したログをOllama経由でAI要約する機能
（異常検知ではない）。

セッション後半で再確認したところ(ディスク空き容量は~27GBまで増えていた)、
容量自体はもう制約にならないが、デフォルトのPyPIから`pip install torch`
すると依然としてCUDA/cuDNN一式(`nvidia-cudnn-cu13`, `cuda-toolkit`,
`triton`約248MB等)が付いてくる（このPython/プラットフォームの組み合わせ
向けにはCPU専用のPyPIホイールが無い模様）。かつCPU専用ホイールの配布元
`download.pytorch.org`はこの環境のegressポリシーでブロックされている
(CONNECTに403)。つまり別の理由でPyTorchは依然として実用的でない —
GPUの無いこのサンドボックスのためにCUDA一式を入れるより、NumPy実装を
維持する判断をした。

**PyTorch installed via a GitHub Release mirror workaround**: the user
downloaded the official CPU-only wheel
(`torch-2.6.0+cpu-cp311-cp311-linux_x86_64.whl`) from
`download.pytorch.org/whl/cpu/` on their own machine (not blocked there)
and uploaded it as a GitHub Release asset on this repo
(`w-index-m/network-lab-emulator`, release tag `torch-cpu-wheel`) —
`github.com`/`objects.githubusercontent.com` are *not* blocked by this
environment's egress policy, only `download.pytorch.org` is. That
Release asset URL is now pinned in `requirements-ml.txt` as a direct PEP
508 URL reference (`torch @ https://github.com/.../torch-2.6.0%2Bcpu-...whl`)
— install it with `pip install -r requirements-ml.txt`. It's intentionally
kept out of `requirements.txt`/`requirements-dev.txt` since the core app
and test suite don't need it (still true even now that torch is
installable) and the pin is Python 3.11/linux_x86_64-specific, not
portable to other environments without re-uploading a matching wheel.
Live-verified: `import torch; torch.nn.Linear(3,3)(torch.randn(3,3))`
runs successfully, `torch.__version__` == `2.6.0+cpu`.

**`tools/anomaly_autoencoder_torch.py`**: with torch now installable, the
user's stated goal was "use torch as a tech-stack foundation for trying
more complex models later" rather than an immediate feature need, so
rather than rewriting the working NumPy implementation, this adds a
parallel torch-backed twin of `tools/anomaly_autoencoder.py`'s
`DeviceAnomalyModel` (same public API: `fit`/`score`/`is_anomaly`/
`threshold`, same 1-hidden-layer/tanh architecture, same mean+k*std
threshold) using `torch.nn.Module`/`torch.optim.SGD` instead of hand-
written NumPy forward/backward passes. The NumPy version stays as the
default/torch-free implementation; this is the base to build a more
complex model (e.g. an LSTM for `link_capacity_forecast.py`-style time
series) on top of, once there's a concrete need. Live-verified via its
own `_demo()` (mirrors `anomaly_autoencoder.py`'s): reproduces the same
qualitative result (flags the CPU=55% case a fixed 80% threshold would
miss). `tests/test_anomaly_autoencoder_torch.py` mirrors
`tests/test_anomaly_autoencoder.py`'s test cases 1:1 against the torch
backend, `pytest.importorskip('torch')`'d so the file skips cleanly on
environments without `requirements-ml.txt` installed — 8/8 passed here.

**Found via the full regression suite, not standalone**: the new torch
tests passed in isolation but failed with
`AttributeError: type object '__file__' has no attribute 'endswith'`
when run as part of the ~1350-test full suite — `torch.optim.SGD.__init__`
lazily imports `torch._dynamo` on its first call, and that import's
module-level init walks `sys.modules` checking each module's `__file__`;
some grpc/protobuf-heavy test module collected earlier in the same pytest
session (this repo has several — gNMI, telemetry, NETCONF) apparently
registers something in `sys.modules` with a non-string `__file__`,
which crashes that walk. **Fix**: `tools/anomaly_autoencoder_torch.py`
now does `import torch._dynamo` at its own module-import time (which
happens early, during pytest's collection phase, before the
grpc/protobuf-heavy test modules further down the alphabet get
collected) — once cached in `sys.modules` it's never re-imported, so the
optimizer construction inside `fit()` never re-triggers the broken lazy
path. Confirmed by bisecting a reproducing subset (first 40 test files +
this one) down to the exact failure, then confirming 0 torch failures
with the fix in that same subset, then a clean full-suite run
(1358 passed, only the 2 known-flaky IPsec DPD tests failed).

**フルスイート回帰テストで初めて見つかった、単体では再現しない不具合**:
新しいtorchテストは単体では通るが、~1350件の全体テストの一部として
実行すると`AttributeError: type object '__file__' has no attribute
'endswith'`で失敗した — `torch.optim.SGD.__init__`は初回呼び出し時に
`torch._dynamo`を遅延importするが、そのモジュール初期化処理が
`sys.modules`を走査して各モジュールの`__file__`をチェックする。
同じpytestセッション内でそれより前に収集された何らかのgrpc/protobuf系
テストモジュール(このリポジトリにはgNMI・テレメトリ・NETCONFなど
複数該当する)が、`__file__`が文字列でない何かを`sys.modules`に
登録してしまい、その走査をクラッシュさせていた模様。**対処**:
`tools/anomaly_autoencoder_torch.py`自身のモジュールimport時点
(pytestの収集フェーズの早い段階、アルファベット順で後ろにある
grpc/protobuf系テストモジュールが収集されるより前)で
`import torch._dynamo`を実行するようにした — 一度`sys.modules`に
キャッシュされれば再importされないため、`fit()`内でのオプティマイザ
構築時に壊れた遅延importパスを再度踏むことがなくなる。再現する
サブセット(アルファベット順先頭40ファイル+このファイル)まで
二分探索で絞り込んで確認し、同じサブセットで修正後torch関連の失敗が
0件になることを確認、その後クリーンな全体テストを実行
(1358件成功、既知のflaky IPsec DPDテスト2件のみ失敗)。

**`tools/anomaly_autoencoder_torch.py`**: torchが導入可能になったことを
受けて、ユーザーの目的が「今すぐ機能として必要」ではなく「将来もっと
複雑なモデルを試すための技術スタックの土台」だったため、動いている
NumPy実装を書き換えるのではなく、`tools/anomaly_autoencoder.py`の
`DeviceAnomalyModel`と同じ公開API(`fit`/`score`/`is_anomaly`/
`threshold`、同じ1隠れ層/tanh構成、同じ平均+k*標準偏差しきい値)を
持つtorchバックエンド版を並行して追加した(`torch.nn.Module`/
`torch.optim.SGD`を使い、手書きNumPy版の前方伝播・逆伝播を置き換え)。
NumPy版はtorch不要のデフォルト実装として維持し、こちらは今後もっと
複雑なモデル(例: `link_capacity_forecast.py`的な時系列予測をLSTM化
する等)を試す際の土台という位置づけ。自身の`_demo()`
(`anomaly_autoencoder.py`のものと対応)で実際に動作確認済み:
固定80%閾値では見逃すCPU=55%のケースを同様に検知するなど、定性的に
同じ結果を再現。`tests/test_anomaly_autoencoder_torch.py`は
`tests/test_anomaly_autoencoder.py`のテストケースをそのままtorch
バックエンド向けに対応させたもので、`pytest.importorskip('torch')`
により`requirements-ml.txt`未導入の環境ではこのファイルごと
きれいにskipされる — ここでは8/8成功。

**PyTorchをGitHub Releaseミラー経由でインストール済み**: ユーザーが
`download.pytorch.org/whl/cpu/`（手元の環境ではブロックされていない）
から公式CPU専用ホイール
(`torch-2.6.0+cpu-cp311-cp311-linux_x86_64.whl`)をダウンロードし、この
リポジトリ(`w-index-m/network-lab-emulator`)のGitHub Releaseアセット
(タグ`torch-cpu-wheel`)としてアップロードした — `github.com`/
`objects.githubusercontent.com`はこの環境のegressポリシーでブロック
されて**いない**（ブロックされているのは`download.pytorch.org`のみ）。
そのReleaseアセットのURLを`requirements-ml.txt`にPEP 508形式の直接URL
参照としてピン留めしてある(`torch @ https://github.com/.../
torch-2.6.0%2Bcpu-...whl`) — `pip install -r requirements-ml.txt`で
導入できる。本体アプリ・テストスイートは(torchが導入可能になった今でも)
torchを必要としないため、意図的に`requirements.txt`/`requirements-dev.txt`
には含めていない。またこのピン留めはPython 3.11/linux_x86_64専用で、
他環境ではそのままでは使えない（対象環境向けのwhlを同じ手順で再度
アップロードしてURLを差し替える必要がある）。実際に動作確認済み:
`import torch; torch.nn.Linear(3,3)(torch.randn(3,3))`が正常に動作、
`torch.__version__` は `2.6.0+cpu`。

**Implemented**: `tools/syslog_anomaly_detector.py` applies exactly this —
a 1D simplification of `anomaly_autoencoder.py`'s mean+k*std threshold
approach to Loki-ingested syslog message counts per time bucket per
device, flags a spike, and can feed the flagged window into
`network_ontology_query.py`'s `summarize_logs_via_ollama()` for an AI
explanation (`--summarize`). Live-verified end-to-end against a real
running Loki + a real device generating a real syslog burst — see
`docs/syslog-anomaly-detection.md`.

**実装済み**: `tools/syslog_anomaly_detector.py`がまさにこれを実装した
— `anomaly_autoencoder.py`の平均+k*標準偏差しきい値方式を1次元
（装置ごと・時間バケットごとのsyslogメッセージ件数）に単純化して適用し、
急増を検知したら`network_ontology_query.py`の
`summarize_logs_via_ollama()`に渡してAI要約もできる（`--summarize`）。
実際に動いているLoki＋実装置が生成した実syslogバーストで
エンドツーエンド検証済み。詳細は`docs/syslog-anomaly-detection.md`参照。

**v6プラス(MAP-E)対応**: ユーザー質問「ちなみに CiscoやYAMAHAから
IPOEでの接続も検証できますか？」→「v6プラスを想定してます」→
「想定コンフィグを元に接続可能かを試験できるようにしたい。もちろん
AI側でコンフィグを作って試験もして欲しい」への対応。

**実装前にユーザーへ説明し合意を得た制約**: `engine/protocols.py`は
IPv6のアドレッシング/ルーティングを一切実装していない
(`grep -c "ipv6" engine/protocols.py` = 0。`engine/rules.py`の37件の
"ipv6"言及はすべて見た目だけの固定文字列応答で、実ルーティングでは
ない)。v6プラスの実際の流れ(IPv6 IPoEアクセス→DHCPv6-PDでプレフィックス
取得→MAP-Eルールで共有IPv4+制限ポートレンジ+BRアドレスを受け取る)の
うち、「IPv6 IPoEアクセス」部分は見た目の設定反映のみ
(`state.yamaha_ipv6_prefix`、実IPv6到達性判定はしない)、「MAP-Eで実際に
IPv4インターネットに出られる」部分だけは、既存の`PppoeEngine`と同じ
方式(vnet経由のソフトウェア的ネゴシエーション、実Ethernetフレームでは
ない)で実際の状態遷移として再現する — 払い出された共有IPv4アドレスは
本物の`state.interfaces`/`rib_engine`に乗るので、既存のIPv4
ping/tracerouteがそのまま使える。

実装: `engine/protocols.py`に`MapEEngine`(`map_e_engine`シングルトン)を
新設。`nodes[rule_server_device_id]`が`ipv4_pool`/`br_ipv6`/
`ports_per_user`/`rules`を保持。`request_rule(rtx_id, iface, rule_id)`は
`vnet.get_neighbors()`/`vnet.interface_links`で指定インタフェース越しに
繋がっているルールサーバーを探し、既存ルール数から次の空きIPv4+PSID
(ports_per_user=4096固定、65536÷4096=16スロット/IPv4)を割り出して
払い出す。ポートレンジの実パケットフィルタリングは行わない
(icmp_engineがポートを一切モデル化していないのに合わせたスコープカット)。

MAP-Eルールサーバー役は新しいdevice_typeを増やさず、既存の擬似FLETS
`bas`デバイスに相乗りさせた(PPPoE-BASもMAP-Eルールサーバーも「契約者が
繋ぐISP収容設備」という点で同じ、という判断) — これによりフロントエンド
(`static/index.html`)の変更は一切不要だった(`bas`/`yamaha`とも既存の
PPPoE/IPsec対応で装置追加ボタン・`DEV_TEMPLATES`・ポート名・色分け等が
既に揃っている)。

CLI側: `_bas_process`(`engine/rules.py`)に`map-e ipv4-pool <start> <end>`/
`map-e br-address <addr>`(config-mode)と`show map-e rules`を追加、
`_bas_show_config`にプール/BRアドレスの反映を追加。`_yamaha_process`に
`ipv6 prefix <value>`(見た目のみ)、`map-e use <lanN>`/`no map-e use`
(実際の払い出し/解放は`app.py`側が担当)、`show map-e`
(`_yamaha_show_map_e`)を追加、`_yamaha_show_config`に`ipv6 prefix`/
`map-e use`の反映を追加。実際の払い出しロジックは(PPPoEの`pp enable`/
IPsecの`tunnel enable`と同じ理由で)`app.py`の`handle_protocol_config`に
置いた — `map-e use <lanN>`で`map_e_engine.request_rule()`を呼び、成功
すれば払い出された共有IPv4を`state.interfaces['map0']`
(prefix 32、`_register_icmp()`呼び出し)に反映する。これは`pp<N>`と同じ
パターンで、`rib_engine`のconnected-route導出や`show`系コマンドが
`map0`に対しても追加コードなしでそのまま動く。

Live-verified end-to-end via a real running instance: `bas`に
`map-e ipv4-pool`/`map-e br-address`を設定、`yamaha`に
`ipv6 prefix dhcp-prefix@lan2::/64`(見た目)+`map-e use lan2`を投入→
`show map-e`が実際に払い出されたIPv4/ポートレンジ/PSID/BRアドレスを表示、
`show config`に`ip map0 address .../32`/`ipv6 prefix ...`/`map-e use
lan2`が反映、`bas`側`show map-e rules`にもセッションが見える。さらに
**別装置のCiscoから、RTXの別インタフェース(`lan3`)経由でMAP-E払い出し
IPv4アドレスへの実際のping/traceroute**を実施(Ciscoに`ip route
203.0.116.50 255.255.255.255 10.200.0.1`で/32への到達経路を設定)し、
`ping 203.0.116.50` → `Success rate is 100 percent (5/5)`、
`traceroute 203.0.116.50` → `1 rtx-v6p-4 (10.200.0.1)` と、実際に
IPv4パケットがMAP-E払い出しアドレスまで到達することを確認(ルールサーバー
へのリンクなし/認証相当のプール未設定/非`bas`デバイスへのリンクでは
一切payされないフェイルケースも確認)。

`tests/test_v6plus_mape.py`(9 tests)が`bas`側のプール/BR設定反映、
`map-e use`での実IPv4払い出し、`show map-e`/`show map-e rules`の内容、
`show ip route`/`state.interfaces['map0']`への反映(ping/tracerouteで
確認した実到達性の裏付け)、`no map-e use`での解放、および3種の失敗
ケース(ルールサーバー未リンク・非`bas`デバイスへのリンク・プール未設定)
を固定する。

**Yamaha RTX ⇔ Cisco IOS IPsec over IPoE(v6プラス/MAP-E) — ノウハウ**:
ユーザー依頼「YAMAHA CiscoでIPOEしてIPsec接続設定作ってノウハウに残して」。
上記の擬似FLET'S PPPoE経由IPsec(`tests/test_yamaha_cisco_ipsec_over_pppoe.py`)
のIPoE版。**実装コードは一切追加していない** — `engine/ike_engine.py`の
IKE/IPsecネゴシエーションは`ipsec ike local-address`に設定された値を
そのまま使うだけで、そのIPが固定/PPPoE払い出し/MAP-E払い出しのどれで
得られたものかを一切区別しない設計だったため、MAP-E払い出しの`map0`
アドレスをそのまま`local-address`に指定するだけで、既存の`pp<N>`
(PPPoE)のケースと全く同じ経路で確立することをライブ検証で確認できた
(`dt in ('sir', 'srs', 'yamaha')`判定は既にPPPoE対応時に拡張済みで
流用できた)。

**手順(ノウハウとして再利用可能な想定コンフィグの骨格)**:
1. v6プラスのルールサーバー役(`bas`)に `map-e ipv4-pool <start> <end>` /
   `map-e br-address <ipv6>` を設定。
2. Yamaha RTXで `administrator` → (見た目の)IPoEアクセス設定として
   `ipv6 prefix dhcp-prefix@lanN::/64` → ルールサーバーとリンクした
   インタフェースで `map-e use lanN` を実行し、共有IPv4を取得
   (`show map-e` で払い出されたIPv4/ポートレンジ/PSID/BRアドレスを確認)。
3. `tunnel select N` → `ipsec tunnel`/`ipsec sa policy`/
   `ipsec ike local-address N <map-eで払い出された共有IPv4>` /
   `ipsec ike remote-address N <Cisco側WAN IP>` /
   `ipsec ike pre-shared-key N text <psk>` → `exit`。
4. 対向のCisco IOSは通常の`crypto isakmp key <psk> address <共有IPv4>` /
   `crypto ipsec transform-set` / `crypto map`一式 — 対向アドレスとして
   RTXの固定WANではなく、MAP-E払い出しの共有IPv4をそのまま指定する点が
   唯一の違い(PPPoE版でも同様、動的に払い出されたIPを対向として扱う)。
5. `tunnel enable N` で実際にネゴシエーションが走る(成功時は実機同様
   無言)。`show status tunnel N` で `IKE negotiation: MATURE` /
   `IPsec SA: MATURE` / `status: established` を確認。

ライブ検証: 上記手順をTestClientスクリプトで実行し、Cisco↔RTX間で実際に
IPsecが確立することを確認(`IKE negotiation : MATURE` / `IPsec SA :
MATURE` / `status : established`)。`tests/test_yamaha_cisco_ipsec_over_ipoe_mape.py`
(6 tests)がこの手順を固定する: MAP-E払い出しIPがCisco側peer設定と一致
すること、確立成功(`tunnel enable`が無言である実機仕様込み)、
`state.ipsec_tunnels`への直接反映、誤PSKでの`DYING`/`wait`失敗、
`tunnel disable`でのSA解体、およびMAP-E未接続(共有IPv4が存在しない)
状態では確立しないケース。

**BGP Route Reflector(cluster-id / route-reflector-client)と
RTBH(Remote-Triggered Black-Hole)**: ユーザーに
[vincentbernat/network-lab](https://github.com/vincentbernat/network-lab)
から持ち込めそうな機能を提案し(`lab-routereflector`/`lab-rtbh`含む)、
この2つを選んで実装した。どちらも新しいエンジンは作らず、既存の
`engine/protocols.py`の`BgpEngine`に機能を足す形にした。

**Route Reflector**: `_compute_adverts`の既存iBGP split-horizon判定
(`src_is_ibgp and not is_ebgp: continue`)に、送信先または学習元の
セッションが`reflector_client`(新設、`BgpSession.reflector_client`)
なら反射する例外を追加しただけ。CLI: `neighbor <ip>
route-reflector-client`(`set_neighbor_route_reflector_client`)、
`bgp cluster-id <id>`(`set_cluster_id`、ループ防止用の
originator-id/cluster-listまでは実装せず表示保持のみ、とコメントで
明記)。`show ip bgp neighbors`に`Route-Reflector Client`行、
`show ip bgp summary`に`RR instance cluster-id is ...`行、
`show running-config`にも両コマンドを追加。

**実装中に見つけた既存の実バグ(RRを作るまで露見しなかった)**:
`_propagate_bgp`のAS-pathループ防止(`if nbr['local_as'] in r.as_path:
continue`)が、eBGP/iBGPを区別せず常に適用されていた。自己発信経路の
as_pathには常に自ASが入る実装(`_compute_adverts`の1)なので、
**同一AS内のiBGPピア同士(= RRクライアントを含むiBGP全般)への配布が
常にこの判定で誤って拒否されてしまう**(受信側の自AS=自分のASなので
`nbr['local_as'] in r.as_path`が常にTrueになる)。既存テスト
(`tests/test_bgp_advanced.py`等)は全てeBGP(装置ごとに別AS)だけで
RRのような同一AS iBGP構成を一度も検証していなかったため今まで露見
していなかった。修正: このAS-pathループ防止はeBGP越境時のみ適用
(`is_ebgp_hop = session.remote_as != n['local_as']`で判定)し、iBGPの
ループ防止はsplit-horizon/反射ルールの方に委ねるようにした。

もう1つ、`session_down`で片側ずつ「Idle化→学習経路撤去→即
`_propagate_bgp()`」としていたため、まだ相手側がEstablishedのまま
残っている間に`_propagate_bgp()`が走り、直前に撤去したはずの経路が
相手から再配布されて復活してしまうタイミング問題も見つけて修正
(両端をIdle化・撤去してから1回だけ`_propagate_bgp()`を呼ぶように
ループを再構成)。RTBHの「経路撤回でNull0経路も解除される」テストで
初めて踏んだ。

**RTBH**: 既存のBGP community機構(`route-map ... set community`/
`send-community`)はそのまま使い、新設の
`bgp rtbh-community <AS:NUM>`(`set_rtbh_community`)で指定した
communityを持つベストパスを、`rib_engine`へ`next_hop='Null0'`の
静的経路として自動installする(`_apply_rtbh`、`_propagate_bgp`の
ベストパス再計算後と`session_down`の両方から呼ぶ)。
`IcmpEngine._resolve_next_hop_detail`に`next_hop == 'Null0'`なら
どの隣接にも誤って解決せず不達(破棄)として返す分岐を追加。

Live-verified(TestClientスクリプト): RR1(cluster-id設定)+
2台のRRクライアント(ハブ&スポーク、クライアント同士は未リンク)で、
RRが無ければ`show ip bgp`に経路が出ない(split-horizon)状態から、
`route-reflector-client`を設定した途端にクライアント間で経路が反射
されること、`show ip bgp neighbors`/`show ip bgp summary`/
`show running-config`への反映を確認。RTBHは`route-map`で
community付与→`bgp rtbh-community`設定側で実際に`show ip route`に
`Null0`経路が入り、**実際にpingが失敗する(0% success、ブラックホール
として破棄される)**ことまで確認。

`tests/test_bgp_route_reflector_and_rtbh.py`(10 tests)が、
`tests/test_bgp_advanced.py`と同じ方式(`engine.protocols`を
pytest-asyncioで直接操作。BGPのFSMは実際の`asyncio.sleep`を使うため、
同期TestClient越しだとタイマーが進まず確立しない)で、RR反射の成立/
reflector-client未設定時のsplit-horizon維持(regression防止)/
学習元への広告し返さないガード、RTBHのNull0インストール/community
不一致時の非インストール/撤回での解除/ICMPエンジンでの実際の不達、
およびCLI側の新規正規表現3つ(`bgp cluster-id`/
`route-reflector-client`/`bgp rtbh-community`)の処理を固定する。

**VRRP unicast-peer(keepalivedの`unicast_peer`相当)**:
[vincentbernat/network-lab](https://github.com/vincentbernat/network-lab)
の`lab-keepalived-unicast`から持ち込んだ機能(ユーザーが「両方」実装を
指定した2機能のうち1つ、もう1つはCisco ZTP)。

**実装前の調査で見つかった制約**: 既存の`VrrpEngine`はVRRP
advertisementを`vnet.broadcast_to_neighbors`(= multicast相当)でしか
送らず、`app.py`側のVRRP設定コマンド(`vrrp <gid> ip <vip>`等)も選出の
即時同期を`vnet.get_neighbors(device_id)`(= vnet上の直結隣接)だけに
頼っていた。つまり**VRRPピア同士がvnet上で直結(同一リンク)でなければ
advertisementがそもそも届かず、永久にInitのまま選出されない**制約が
あった。これはkeepalivedの`unicast_peer`が実運用で解決する課題そのもの
(multicastが届かない/フィルタされる環境、あるいはL3越しの構成で
VRRPを使うため、相手のIPへ明示的にユニキャストで送る)なので、同じ
方法でこのエミュレータにも持ち込んだ。

実装: `VrrpGroup`に`unicast_peers: List[str]`を追加、
`VrrpEngine._vrrp_send_advert`はbroadcast_to_neighborsに加えて
`unicast_peers`に設定されたIPの装置へ`vnet.send_to()`で直接
advertisementを送る(`icmp_engine._find_device_owning_ip()`でIPから
装置を解決。vnetの直結隣接である必要はない — `send_to`は
`vrrp_advert`を自動的に`vrrp_engine.vrrp_receive_advert`へルーティング
する既存の仕組みをそのまま使っている)。CLI: `vrrp <gid> unicast-peer
<ip>`(Cisco/Catalyst形式に相乗り。実際のCisco IOSにこのコマンドは
無く、unicast VRRPはkeepalived固有の機能だが、このエミュレータに
keepalived相当の専用device_typeは無いため、既存のpragmatic判断
(EOSでcrypto key generate rsa流用等)と同じ考え方でCisco形式のVRRP
配下にそのまま追加した)。設定直後にも既存の直結隣接パターンと同じ
即時ネゴシエーションを行う。`show vrrp`に`VRRP Unicast Peer(s): ...`
行、`show running-config`にも反映。

Live-verified: R1 -- MID(中間L3装置) -- R2 の3台構成(R1/R2はvnet上で
直結しない)で、`vrrp <gid> ip <vip>`だけでは両方Initのまま永久に
選出されないことを確認し、両側に`vrrp <gid> unicast-peer <相手IP>`を
設定した途端に正しくMaster/Backupへ選出されることを確認
(`show vrrp`/`show running-config`への反映も含む)。

`tests/test_vrrp_unicast_peer.py`(5 tests)が、直結しないピアが
unicast-peer無しでは選出されないこと(regression防止: この制約自体の
固定)、unicast-peer設定で選出が成立すること、`show running-config`/
`show vrrp`への反映、priority逆転でのMaster/Backup入れ替わりを固定する。

**Cisco ZTP(AUTOINSTALL) — DHCP option 150/67経由の自動プロビジョニング**:
同じく[vincentbernat/network-lab](https://github.com/vincentbernat/network-lab)
の`lab-dhcp-ztp`から持ち込んだ機能。実機のAUTOINSTALLは「起動時に
startup-configが無ければ自動でDHCP→TFTP経由のconfig取得を試みる」
という挙動で、CLIで明示的に叩くコマンドではない。このエミュレータには
「再起動」の概念が無いため、実機でこのシナリオを再現する際の実際の
手順そのもの(`write erase`でNVRAM/startup-configを空にし、`reload`で
再起動する)に相乗りした — 新しい疑似コマンドを増やさず、2つの実
コマンドの組み合わせをトリガーに使っている。

実装: DHCPサーバー役(既存の`ip dhcp pool`機能、`cisco`/`catalyst`)に、
既存の汎用`option <code> {ascii|hex|ip} <value>`機構で
`option 150 ip <tftp_ip>`/`option 67 ascii <bootfile>`をそのまま設定
できた(コード追加不要)。配布する"ファイル"の内容は新設の
`tftp-server config <bootfile>`サブモード(`CONFIG_SUBMODES`に1行追加)
でCLI行としてそのままステージする(実機はflash上のファイルを配るが、
このエミュレータにファイルシステムは無いため、CLIで流し込んだものを
"ファイル"として保持するpragmaticな方式)。

**実装中に見つかった実バグ(2か所)**:
1. ステージモード中(`state.mode == 'config-tftp-file'`)はexit/end/quit
   以外の行を一切解釈せず生テキストとしてキャプチャする必要があるが、
   `engine/rules.py`側だけにガードを入れても防げなかった —
   **二層ディスパッチで`app.py`の`handle_protocol_config`が常に
   `rule_engine.process()`より先に実行される**ため、ステージ中の
   `interface ...`/`ip address ...`のような行がapp.py層の既存regexに
   先にマッチし、サーバー自身の設定として実際に適用されてしまっていた
   (CLAUDE.mdの「app.pyを先に確認する」という既存の教訓そのもの)。
   `handle_protocol_config`の冒頭にも同じモードガードを追加して解決。
2. `tftp-server config <bootfile>`のファイル名を小文字化された`c`から
   取り出していたため(`SE11.txt`→`se11.txt`)、クライアント側が
   option 67で受け取る大文字小文字区別ありのファイル名と一致せず
   "file not found"になっていた。元の大文字小文字を保持した`cmd`から
   再マッチして修正。

クライアント側は`write erase`→`reload`で、直結隣接からDHCP
option150/67を持つ装置を探し、IP/デフォルトゲートウェイ(pool の
default-router)を実際に割り当て(`rib_engine.add_static_route`)、
ステージされたファイルの内容を`configure terminal`で入ってから
本物の`cli_command()`経由で1行ずつ適用する(= ユーザーが手で打つのと
同じ経路を再利用。新しい適用ロジックは書いていない)。見つからない/
未staging の場合は何も変更せず、実機のAUTOINSTALLログ風のエラー行
だけを返す(フェイクの成功を作らない)。

Live-verified: サーバーにpool+option150/67+ステージ済みファイル
(hostname変更+新規interface)を設定し、クライアントで`write erase`→
`reload`を実行→実際にDHCP払い出しIP(`show running-config`に反映)、
デフォルトゲートウェイ経路、ステージされたhostname変更・新規
interfaceまで全て適用されることを確認
(`%AUTOINSTALL-5-ADDR_ASSIGNED`/`Loading <file> from <tftp_ip> (via
<iface>): !`/`[OK - N bytes]`/`%AUTOINSTALL-5-CONFIG_APPLIED`という
実機ログ風の出力込み)。ブートファイル未設定/ファイル未staging/
サーバーリンクなし/`write erase`せずの`reload`、の各ケースで一切
変更されないことも確認。

`tests/test_cisco_ztp_autoinstall.py`(8 tests)がサーバー側の
option150/67反映とステージ内容が生テキストとして保持されサーバー
自身には適用されないこと、クライアント側の実際のDHCP払い出し+
デフォルトルート+ステージ済みconfigの適用、および4種の失敗ケース
(サーバー未リンク・ブートファイル未設定・ファイル未staging・
write eraseせずのreload)を固定する。

**BGP: Hostname Capability / Graceful Restart・LLGR / RPKI / Confederation**:
「lab bgpも実装お願いします」という依頼に対し、
[vincentbernat/network-lab](https://github.com/vincentbernat/network-lab)
の`lab-bgp-*`群からユーザーが選んだ4機能
(`lab-bgp-hostname`/`lab-bgp-graceful-restart`・`lab-bgp-llgr`/
`lab-bgp-rpki`/`lab-bgp-confederation`)。どれも新しいエンジンは作らず、
既存の`BgpEngine`(`engine/protocols.py`)に機能を足した。

**Hostname Capability**: 実機(FRR/BIRD)は自動ネゴシエーションだが、
このエミュレータでは`bgp hostname-capability`で明示的にトグルする
pragmatic判断。双方で有効な場合のみ`show ip bgp summary`のNeighbor列
が実機のFRR同様`hostname(ip)`形式になり(`_hostname_capability_active`)、
`show ip bgp neighbors`にも`Hostname Capability: advertised and
received`行が出る。

**Graceful Restart / LLGR**: `BgpSession`に`graceful_restart`/`llgr`/
`restart_time`、`BgpRoute`に`stale: bool`を追加。`session_down`は
GR/LLGRが有効なセッションの場合、学習経路を即時削除せずstale化する
だけに変更(`r.stale = True`)。GRは`restart_time`秒以内に再確立
しなければ`_schedule_gr_expiry`が本当に撤去するが、LLGRはタイマーを
一切張らず、再確立後に改めて広告されなければ(_propagate_bgpの通常の
撤回ロジックに委ねる形で)自然に消える、というだけで明示的な撤去
コードは書いていない。ベストパス選択(`_recalc_best_path`の
`_better()`)はstale同士でなければ常に非staleを優先するよう1行追加。
`show ip bgp`のベストパス行は`*s`(stale)マーカーで区別する。

**RPKI**: `bgp rpki roa <prefix>/<len> max-length <max> origin-as <as>`
で登録したROAテーブル(`n['roas']`)と、学習経路のorigin AS
(`as_path[-1]` — このエンジンの自己発信経路は常に末尾が発信元という
既存の内部表現に乗っている)を`_rpki_validate()`で照合し
valid/invalid/notfoundを判定、`BgpRoute.rpki_state`に記録して
`_propagate_bgp`のインバウンド処理時点で評価する。
`bgp rpki invalid-drop`が有効ならinvalidをこの時点で拒否する
(実機のreject policy相当)。`show ip bgp`に`(RPKI: Valid/Invalid)`
のサフィックス、`show bgp rpki table`でROA一覧を表示。

**Confederation**: `bgp confederation identifier <as>` /
`bgp confederation peers <as...>`は実機そのままのCisco IOS構文
(pragmaticな代替コマンドではない、本物)。実装は`_compute_adverts`の
as_path構築ロジックに集約: 加盟国(confederation peers)同士のeBGPは
通常のeBGPと同じく自分の(sub-)AS番号をas_pathにprependするが、
**真の外部eBGPへ出る瞬間だけ**(`_leaving_confederation()`が
True、すなわち相手が加盟国でもiBGPでもなく、confederation
identifierが設定されている場合)、それまでas_pathに積まれた
内部sub-AS群を`confed_members`で全て取り除き、identifier番号を
1つだけ付け直す — 実機同様、confederation外からは1つのpublic ASに
圧縮されて見える。AS-pathループ防止(`is_ebgp_hop`判定)は加盟国同士も
各自が別のsub-AS番号を持つため、既存のeBGP/iBGP判定をそのまま
転用でき、特別な分岐は不要だった。

Live-verified(TestClientスクリプト、4機能とも別々に): Hostname
Capabilityは双方有効時だけNeighbor列がhostname(ip)になり片方のみ
では素のIPのまま、Graceful Restartはセッション断後すぐに`*s`付きで
経路が残り、restart_time経過後に本当に消えることを確認、LLGRは
同条件で何秒経っても消えないことを確認、RPKIはvalid/invalid/
notfoundの判定とinvalid-dropでの拒否、ConfederationはCA(sub-AS
65010)⇔CB(sub-AS 65020、どちらもpublic AS 65000)⇔CX(真の外部AS
65999)の3段構成で、CBから見える経路のas-pathにはCAのsub-AS 65010が
残るが、CXから見える同じ経路のas-pathは65000だけ(65010/65020は
一切見えない)ことを確認。

**実装中に見つけた実バグ(2件)**: (1) `show running-config`のBGP
ブロックは実は2系統(NX-OS/Nexus向けブロックと通常のIOS/Catalyst向け
ブロック)が同じ関数内に存在し、両方に同じ新規設定行(cluster-id等)を
追記する必要があったが、片方(IOS向けブロック)にだけ
`bgp graceful-restart restart-time <sec>`行の追記を書き忘れており、
`bgp graceful-restart`は出るのにrestart-timeだけ消える不具合があった
— 両方に追記して解決。(2) この調査中に気づいた**既存の無関係な
バグ**(今回の変更とは無関係、修正はスコープ外とした): 同じ
show running-configのIOS向けブロックで、neighbor行のpeer_ipが
実際の設定IPではなく常に文字列`'10.0.0.2'`のハードコード値になって
おり、実際のneighbor IPの行と合わせて同じneighborが2行出る
(`neighbor 10.0.0.2 remote-as X` / `neighbor <実IP> remote-as X`)。
変更前のコードでも再現することを確認済み(今回のBGP機能追加が
原因ではない)。

`tests/test_bgp_hostname_gr_llgr_rpki_confederation.py`(20 tests)が
`tests/test_bgp_advanced.py`と同じ方式(engine.protocolsを
pytest-asyncioで直接操作)で、Hostname Capabilityの双方有効/片方
のみのケース、GRのstale化・restart_time経過後の撤去・GR未設定時の
regression防止、LLGRの無期限保持・非stale代替経路への切替、RPKIの
valid/notfound/invalid(AS不一致・max-length超過)/invalid-drop、
Confederationの加盟国間eBGP・外部への圧縮・confederation未設定時の
regression防止、およびCLI側の新規正規表現5つを固定する。

**ASA / FortiGateをPPPoE+IPsecマトリクスに追加**: 「そう互換接続ASAや
fortigateも追加で試験幅を増やして欲しい」という依頼
(`docs/pppoe-ipsec-mesh-status.md`参照)への対応。

**Cisco ASA**: 新しいIPsecネゴシエーションロジックは一切追加していない
— `engine/ike_engine.py`は元々`('cisco', 'catalyst', 'asa')`を対向
として対応済みなので、実際に繋がるかをライブ検証で確認するのが目的
だった。その過程で**既存の潜在バグを2件発見・修正**:

1. `crypto map <name> <seq> match address <acl>`のハンドラ(IOS版・
   ASA版の両方、`engine/rules.py`)が`[seq] = {'acl': acl}`で辞書を
   丸ごと代入していたため、`match address`を`set peer`/
   `set transform-set`より**後**に打つ(実機でもよくある順序)と、
   既存のpeer/transform_setが消えてしまっていた。`match address`を
   先に打つ設定順なら表面化しないため、これまでのテストでは踏んで
   いなかった。`setdefault(seq, {})['acl'] = acl`に修正(IOS/ASA
   両方)。
2. ASAの`tunnel-group <peer> ipsec-attributes`サブモードは
   `state.mode`を変えずに`_tg_attr_mode`フラグだけで管理する作り
   だったが、`_asa_process`冒頭の`exit`/`end`/`quit`ハンドラがこの
   フラグを見ておらず、`ipsec-attributes`配下から`exit`すると
   `state.mode=='config'`のまま一般の「config→exec」判定に落ちて
   1回の`exit`でconfigモードごと抜けてしまっていた(実機は
   ipsec-attributesだけ抜けてconfigに留まる)。このバグのせいで
   `crypto isakmp enable outside`が「config モードでない」と誤判定
   されて無視され、IKEが永久に確立しなかった(ライブ検証で最初に
   踏んだ不具合)。`_tg_attr_mode`を先にチェックしてクリアするよう
   修正。

Live-verified: Yamaha RTX(PPPoE払い出しIP)⇔ASA(固定WAN)で実際に
IPsecが確立(`IKE negotiation : MATURE`/`status : established`)、
誤PSKでは確立しないことを確認。`tests/test_asa_ipsec_mesh.py`
(6 tests)がこの2つのバグのregressionテストと、ASA⇔PPPoE払い出し
Yamahaの成功/誤PSK失敗ケースを固定する。

**FortiGate(FortiOS)device_type(新規)**: FortiOSの`config`/`edit`/
`set`/`next`/`end`階層型CLIはCiscoのexec→config→config-ifモデルに
乗らないため、Juniper/Yamaha/APRESIA/BIG-IPと同じパターンで
`_fortigate_process`を`RuleEngine.process()`の先頭で分岐させた。
インタフェース名は`port1`/`port2`/`port3`(実機のFortiGate命名)。

スコープ: `config system interface`(IP設定・`set mode pppoe`での
WAN化)、`config vpn ipsec phase1-interface`/`phase2-interface`
(IKE/IPsec)。IPsecは既存のSi-R用`ipsec_tunnels`辞書形式にそのまま
乗せ、`ike_engine.py`の`dt`/`pdt`判定タプルに`'fortigate'`を追加
しただけ(Yamaha追加時と全く同じパターン)。PPPoE WAN化も既存の
`PppoeEngine`をそのまま使う(Yamahaの"pp enable"と同じ経路)。
phase2-interfaceの`set phase1name`は受理のみで、本エンジンは
phase1単位でトンネルを管理するためphase2側の状態には反映しない
(スコープ外、コメントで明記)。

**実機にはSi-R/Yamahaの"ike use on"/"ipsec use on"のような明示有効化
コマンドが無く**、phase1-interfaceを作った時点で常に有効扱いになる
ため、`_fortigate_process`はphase1の`edit`時点で
`state.ike_enabled`/`state.ipsec_enabled`を直接Trueにしている
(ike_engine.py側のゲート条件に合わせるための処置)。

**実装中に見つけた実バグ(2件)**:
1. 二層ディスパッチで`app.py`の`handle_protocol_config`が常に
   `rule_engine.process()`より先に実行されるため、FortiGateの
   PPPoE接続/IKEネゴシエーションの実際のトリガーを
   `handle_protocol_config`側に書くと、`_fortigate_process`が
   `state.interfaces`/`state.ipsec_tunnels`/`state.fortigate_pppoe`
   を確定させる前の古い値を使ってしまう(Juniperのcommit→SSH
   リスナー起動と全く同じ理由)。`cli_command()`の
   `rule_engine.process()`実行後にトリガーを置いて解決 — `next`/
   `end`実行前のモードスタック/edit対象を`_fg_stack_before`/
   `_fg_edit_before`として先に控えておく必要がある(GRE tunnel確立
   等でも使われている既存の「直前の値を控えておく」パターンを流用)。
2. Yamahaの"pp enable"トリガーを複製する際、ローカル変数名を
   `hostname`と書いたが、`cli_command()`のスコープには存在しない
   (`state.hostname`が正しい)。`NameError`で即座に発覚(Yamaha側は
   別の関数`handle_protocol_config`内で実際に`hostname`という
   ローカル変数が存在するため、同じコードに見えて問題が無かった)。

フロントエンド(`static/index.html`): `DEV_TEMPLATES`・add-deviceボタン・
`defPort()`/`portOptions()`(`port1`/`port2`/`port3`)・
`_inferDeviceType()`正規表現(`config system interface`/
`config vpn ipsec`/`set psksecret`)・色マップに`fortigate`エントリを
追加。`getPrompt()`はFortiOS実機通り常に`hostname #`固定
(config/edit階層に関わらずプロンプトが変わらない)。

Live-verified: `bas`経由のPPPoEで実際に共有IPv4を取得
(`show full-configuration`に反映)、そのIPを使ってCisco IOS/Si-Rの
双方と実際にIPsecが確立することを確認(`get vpn ipsec tunnel
summary`で`status: up`)、誤PSKでは`status: down`のままであることも
確認。`tests/test_fortigate.py`(9 tests)がinterface設定・PPPoE WAN
(成功/誤パスワード失敗)・Cisco/Si-R双方とのIPsec確立(成功/誤PSK
失敗)・config/edit/next/endのモード管理そのものを固定する。

**ASA 5505のPPPoEクライアント機能(追加)**: 「PPPoE ASAやfortigateも
いけると思うのでお願いします」という追加依頼への対応。調査の結果
FortiGateは上記で既にPPPoE WAN(`set mode pppoe`)対応済みだったため、
本件は残っていたASA側のみを対象にした。

実機ASA 5505のPPPoEクライアント構文(`interface`配下`ip address
pppoe [setroute]` + config配下`vpdn group <name> request dialout
pppoe`/`localname`/`ppp authentication` + `vpdn username <user>
password <pass>`)をそのまま実装。Yamahaの"pp enable"/FortiGateの
"set mode pppoe"と同じ`engine.protocols.PppoeEngine`を使い、新しい
ネゴシエーションロジックは追加していない。コマンド順序に依存せず
(`vpdn group`→`vpdn username`の順でなくても)、localname/username/
passwordが揃うたびに未接続のpending interfaceへ接続を試みる
(`app.py`の`cli_command()`、`rule_engine.process()`実行後 — Juniperの
SSHリスナー起動と同じ理由)。"setroute"時は実際にPPPoEゲートウェイ
経由のデフォルトルートを`rib_engine`へ登録する(Yamahaの"ip route
default gateway pp N"と同じ役割)。

**実装中に見つけた既存の実バグ(ASA固有ではなく汎用の`_validate_command`
のバグ)**: `RuleEngine._validate_command`の"ip address"
Incompleteチェック(`c.startswith('ip address') and mode ==
'config-if'`)が、device_typeを問わず「IPアドレス+マスクが無ければ
4トークン未満はエラー」という前提で判定していたため、"ip address
pppoe"(3トークン、実機のASA 5505で有効な構文)が常に"% Incomplete
command."扱いになり、`_asa_process`の該当ハンドラへ到達する前に
`process()`の冒頭で弾かれていた(ライブ検証で最初に踏んだ——
`_asa_process`側にハンドラを書いても一切呼ばれず、CLAUDE.mdの
「二層ディスパッチで先に処理されると下位が死ぬ」と全く同じ class の
問題が`_validate_command`という第0層でも起きていた)。"ip address
pppoe(\s+setroute)?"のパターンをこのチェックの対象外に追加して修正。

**もう1つの既存スコープ上の発見(修正せず・ASA固有ではなくCisco IOS/
ASA共通)**: ASAの"show route"(`_asa_show_route`)は元々`state.routes`
(ASAの"route"コマンドでのみ追加される独自リスト)しか見ておらず、
rib_engine経由で登録された経路(本機能のPPPoE setrouteを含む)を
表示できなかったため、他vendorの"show ip route"同様rib_engineの
スタティック経路も合わせて表示するよう拡張した(dedupeはnetwork/
prefix単位)。さらに調査中、ASA/Cisco間のIPsecで"show crypto ipsec
sa"(`app.py`の`handle_protocol_show`)が`icmp_engine.ipsec_tunnels`
という"crypto isakmp keepalive"のDPD機能専用の別トラッキング構造
しか見ておらず、DPDを設定していない通常のIPsecネゴシエーションでは
何も登録されないため常に"There are no ipsec sas."を返すことが
分かった——これは今回のPPPoE追加が露見させたが原因ではない
pre-existingのスコープ(DPD専用表示)で、修正はスコープ外とした
(テストは`show crypto isakmp sa`とDPDに依存しない`state.ipsec_peers`
の直接確認で成立を検証する)。

Live-verified: PPPoE経由でASAが共有IPv4を取得(`show interface ip
brief`に反映)、"setroute"で実デフォルトルートが`show route`に
反映されること、そのIPを使ってCisco IOSと実際にIKE Phase1/Phase2が
確立すること(`state.ipsec_peers`で`status: established`、`show
crypto isakmp sa`で`MM_ACTIVE`)を確認。`tests/test_asa_pppoe.py`
(8 tests)がinterface設定(コマンド順序非依存を含む)・誤パスワード/
BAS未リンク失敗ケース・setroute有無での経路反映差・Cisco相手への
IPsec確立(成功/誤PSK失敗)を固定する。

**Groq APIをLLMバックエンドに追加**: 「groqなどのAPIを使うアプローチ
で行ってみます」という依頼への対応。元々`app.py`は起動時に1回だけ
Ollamaを自動検出し(`detect_ollama()`)、ルールエンジンが空応答の
コマンドをOllamaに投げて補完する(`USE_OLLAMA`フラグ)仕組みだった。
これと全く同じ構造で`detect_groq()`/`query_groq()`を追加し、
`GROQ_API_KEY`環境変数(+`GROQ_MODEL`、デフォルト
`llama-3.3-70b-versatile`)でGroqのOpenAI互換Chat Completions API
(`https://api.groq.com/openai/v1/chat/completions`)を叩く。新規
パッケージは不要(`httpx`は既存依存で足りる)。

優先順位は**Groq > Ollama > ルールベース**: Groqはローカルインストール
不要・高速なためOllamaより優先、どちらも使えなければ従来通り
ルールベースにフォールバック。`USE_OLLAMA`/`USE_GROQ`という個別の
boolフラグに加えて、実際にどちらを使うかを表す`LLM_BACKEND`
("groq"/"ollama"/"rules")を新設し、以降の分岐(`/api/status`、
ルールエンジンが空応答を返した時の補完呼び出し)は全て
`LLM_BACKEND`を見るように統一した(`query_ollama`/`query_groq`を
直接呼び分けていた箇所を`query_llm()`という共通入口に統合)。

**detect_groq()は空応答チェックではなく実際に1回APIを叩いて確認する**
(`detect_ollama()`が`/api/tags`を叩いて確認しているのと同じ考え方)
——`GROQ_API_KEY`が設定されていても失効/無効な場合に気づかず
"Groqが使える"と誤判定してしまうのを防ぐため。

**このサンドボックスからは実際のGroq API疎通は確認できない**:
CLAUDE.mdの「PyTorch」節に既出の通り`api.groq.com`はこの環境の
egressポリシーでブロックされている(`download.pytorch.org`と同じ
扱い)。ライブ検証では、`GROQ_API_KEY`未設定時に`detect_groq()`が
ネットワーク越しに確認するまでもなく即座に`False`を返すこと、
および偽のAPIキーを設定した状態で実際にサーバー起動
(`uvicorn app:app`)してみて、`detect_groq()`が403(このサンドボックス
のegressブロック)を例外にせず安全に捕捉し、`LLM_BACKEND`が
正しく`"rules"`へフォールバックし`/api/status`にも反映されること
を確認した(本番のRender環境は別のegressポリシーのため、実際の
Groq疎通はそちらでのみ確認可能——このサンドボックスでの検証は
「キーが無い/繋がらない場合に壊れず安全側に倒れる」ことの確認が
目的)。

フロントエンド(`static/index.html`): モードバッジに`.badge-groq`
(シアン系)を追加、ランチャー画面のモードバッジ・メイン画面上部の
モードバッジ・「ℹ」クリックで開くモード説明パネル・メニューバーの
LLMトグル(`ollama-toggle`要素、旧`toggleOllama()`)を全て
"groq"/"ollama"/"rules"の3値に対応させた。`/api/status`のレスポンス
形状を`mode`(3値)/`ollama_model`/`groq_model`に変更(後方互換:
`mode`に`"rules"`/`"ollama"`という既存の2値はそのまま含まれる)。

`tests/test_groq_llm_backend.py`(8 tests)が、APIキー無し/無効な
APIキーでの安全なFalseフォールバック(例外を出さないこと)、
`query_llm()`のバックエンド別ディスパッチ(groq/ollama/rules)、
`/api/status`のレスポンス形状(3値のmode、groq時のみ`groq_model`が
値を持ちそれ以外はnull)を固定する。
