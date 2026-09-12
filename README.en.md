# Network Lab Emulator

[日本語](./README.md) | **English**

A multi-vendor network device emulator. It gives you a realistic CLI in the browser,
and — unlike most CLI emulators — **several protocols are implemented as real wire
protocols**, so genuine clients such as ncclient, gnmic and `snmpwalk` can connect to it.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![Tests](https://img.shields.io/badge/tests-1038%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> Most of the documentation under `docs/` is written in Japanese.

---

## What makes this different

### It doesn't just print `show` output — some protocols actually speak on the wire

Many CLI emulators only return canned strings. This project implements
**six protocols over real sockets and real packets**.

| Implementation | Transport | What you can connect with |
|---|---|---|
| `engine/real_ospf_agent.py` | scapy / raw IP proto 89 | Forms adjacencies with other OSPF implementations |
| `engine/real_bgp_agent.py` | TCP 179 | Establishes sessions with real BGP speakers |
| `engine/real_rip_agent.py` | UDP 520 | Sends and receives RIPv2 packets |
| `engine/snmp_udp_agent.py` | UDP 161 | Poll it with `snmpwalk` and friends |
| `engine/netconf_agent.py` | paramiko SSH / TCP 830 | **ncclient** — `get-config`, `edit-config` |
| `engine/gnmi_agent.py` | gRPC / TCP 50052 | **gnmic / pygnmi** — Get, Set, Subscribe |

gNMI uses the **original `gnmi.proto` from openconfig/gnmi**, compiled with `protoc` —
not a hand-written stand-in. NETCONF, RESTCONF and gNMI share one data model, so
**a value written over gNMI shows up in the CLI's `show running-config`.**

### Failure re-convergence is actually tested

A protocol converging correctly does not mean it *re*-converges correctly. In practice
this project found **ten defects in OSPF** ("the adjacency never drops and the route is
never withdrawn") and **one crash in EIGRP** (infinite recursion the moment two devices
are configured).

Each protocol is pinned against the same scenario: shut the primary link, confirm the
failover to a floating static route (AD 210), bring it back, confirm it returns.
See [`docs/ospf-failover-floating-static.md`](./docs/ospf-failover-floating-static.md).

```
Steady state   O  172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1
Link down      S  172.31.2.2/32 [210/0]  via 10.90.2.2, GigabitEthernet1/0/2   <- failover
Recovered      O  172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1   <- restored
```

### Concept: Network Infrastructure Digital Twin + AI Observability

| Digital twin aspect | How it is implemented here |
|---|---|
| Physical assets mirrored virtually | Catalyst / Cisco / Si-R / SR-S / ASA / Nexus / APRESIA / BIG-IP emulated by protocol engines |
| Real telemetry out of the virtual model | Real SNMP agent (MIB-II), syslog and SNMP traps over real UDP |
| Monitoring and observation layer | SNMP dashboard, Syslog AI monitor, Prometheus / Grafana integration |
| Bridge to the real world | netmiko / paramiko integration, route-injection load tests, log collection tools |

---

## Supported devices

| Device | `device_type` | CLI dialect | Main features |
|------|---|------|------|
| **Cisco Catalyst 9300** | `catalyst` | IOS-XE 17.x | OSPF / BGP / EIGRP / HSRP / STP / EtherChannel / MPLS / ZBFW / NETCONF / RESTCONF / gNMI |
| **Cisco Nexus 9300** | `nexus` | NX-OS 10.2 | OSPF / BGP / vPC / VRRP / LACP / MPLS |
| **Cisco IOS router** | `cisco` | IOS 15.x | RIP / OSPF / BGP / NAT / IPsec |
| **Cisco ASA** | `asa` | ASA 9.x | Firewall / NAT / ACL |
| **Fujitsu Si-R G120/G210** | `sir` | Si-R G series | RIP / OSPF / BGP / VRRP / STP / IPsec VPN |
| **Fujitsu SR-S324TR1** | `srs` | SR-S series | VLAN / LACP / STP |
| **APRESIA ApresiaLight GM200** | `apresia` | ApresiaLight | VLAN / STP / LACP (L2 access switch — no L3 routing, by design) |
| **F5 BIG-IP** | `bigip` | TMOS / tmsh | LTM / Pool / Virtual Server |
| **PC (Linux host)** | `pc` | bash-like | ifconfig / ip / ping / traceroute / curl |

---

## Features

### Routing and switching
- **RIP v2 / OSPF / BGP / EIGRP** — adjacency, route learning, re-convergence, MD5 authentication, route filtering, ECMP
- **Static routes** — administrative distance comparison, floating static, multi-protocol route selection
- **VRRP / HSRP / GLBP** — master/backup transitions, preempt, object tracking
- **STP / Rapid-PVST+** — root bridge election, PortFast, BPDU Guard
- **LACP / EtherChannel** — bundling, min-links, parallel links
- **vPC (NX-OS)** / **MPLS (LDP)** / **NHRP / WCCP**

### Management and programmability
- **NETCONF** (TCP 830) — reachable from ncclient → [`docs/netconf-catalyst.md`](./docs/netconf-catalyst.md)
- **RESTCONF** — ietf-interfaces
- **gNMI** (gRPC) — Capabilities / Get / Set / Subscribe → [`docs/gnmi-telemetry.md`](./docs/gnmi-telemetry.md)
- **Model-driven telemetry (MDT)** — `telemetry ietf subscription`
- **Model-based AAA (NACM, RFC 8341)** → [`docs/model-based-aaa-nacm.md`](./docs/model-based-aaa-nacm.md)
- **Service-level ACLs** — restrict NETCONF/RESTCONF by source address
- **ISMU** — in-service data model update packages (`.dmp.bin`)
- **EEM** — applets really do change device configuration
- **App Hosting / OpenFlow** → [`docs/eem-apphosting-openflow.md`](./docs/eem-apphosting-openflow.md)

### Security and services
- **ZBFW** — zone-based firewall
- **IPsec VPN** — IKE / DPD (Si-R ↔ Cisco interop)
- **NAT / NAPT / ACL / DHCP / DHCPv6 / IPv6 ND**
- **Auto-QoS and QoS policies**

### CLI
- Tab completion, `?` help, abbreviations (`sh ip os ne`), multi-line paste
- Vendor-specific error messages (`% Invalid input detected at '^' marker.`)

### Logging and monitoring
- Device log buffer (`show logging`), syslog forwarding (UDP 514), SNMP traps, NTP
- Prometheus exporter → Grafana dashboards

---

## Getting started

```bash
git clone https://github.com/w-index-m/network-lab-emulator.git
cd network-lab-emulator
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000. On Windows, double-click `start.bat`.

- Python 3.10+ / 1 GB RAM / Windows, macOS, Linux
- Default login is `admin` / `admin` (override with `NETLAB_AUTH_USER` /
  `NETLAB_AUTH_PASS`, or disable with `NETLAB_AUTH_DISABLE=1`)
- The **real protocol listeners** (OSPF raw sockets, TCP 830, UDP 161, …) need
  elevated privileges. Without them the CLI emulation still works
- gNMI additionally needs `grpcio` / `grpcio-tools`; if they are missing, only the
  gNMI feature is disabled

---

## Usage

### Example lab (multi-vendor)

```
APRESIA ─── Si-R G120 ─── Catalyst 9300 ─── Nexus 9300
10.0.12.0/30  10.0.23.0/30       10.0.34.0/30
     RIPv2          OSPF Area 0          eBGP
                              AS65001 <-> AS65002
```

<details>
<summary>Catalyst configuration</summary>

```
conf t
hostname Cat-SW1
interface GigabitEthernet1/0/1
 no switchport
 ip address 10.0.23.2 255.255.255.252
 no shutdown
router ospf 1
 network 10.0.23.0 0.0.0.3 area 0
router bgp 65001
 neighbor 10.0.34.2 remote-as 65002
end
write memory
```
</details>

<details>
<summary>Si-R configuration</summary>

```
configure
hostname Router-A
lan 0 ip address 10.0.23.1/30
ospf use on
ospf area 0.0.0.0
lan 0 ip ospf use on
syslog host 192.168.1.100
save
```
</details>

### Useful show commands

```
show ip route                 # routing table, with AD and metric
show ip route 172.31.2.2      # why this route won (source, AD, next hop)
show ip ospf neighbor
show ip bgp summary
show etherchannel 1 detail
show vpc                      # NX-OS
```

### HTTP API

```bash
# Create a device
curl -X POST localhost:8000/api/device \
  -H 'Content-Type: application/json' \
  -d '{"id":"sw1","type":"catalyst","hostname":"SW1"}'

# Send a CLI command (the response key is "output")
curl -X POST localhost:8000/api/cli \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"sw1","command":"show ip route"}'

# Link two devices (the parameters are iface_a / iface_b)
curl -X POST localhost:8000/api/link \
  -H 'Content-Type: application/json' \
  -d '{"a":"sw1","b":"sw2","iface_a":"GigabitEthernet1/0/1","iface_b":"GigabitEthernet1/0/1"}'
```

---

## Tests

```bash
pytest tests/                           # full suite (1038 tests, ~9 min)
pytest tests/test_ospf_failover.py -v   # OSPF failover
pytest tests/test_gnmi.py -v            # gNMI
python verify_all.py                    # feature sweep script
```

**Current status: 1038 passed / 5 skipped / 0 failed** across 79 test files.

Coverage: `app.py` 51% · `engine/protocols.py` 70% · `engine/rules.py` 53%

---

## Documentation

There are 75 documents under `docs/`, **mostly in Japanese**. Start here:

| Document | Contents |
|---|---|
| [`architecture-pitfalls.md`](./docs/architecture-pitfalls.md) | **Read before changing code.** The structural traps this codebase keeps falling into |
| [`ospf-failover-floating-static.md`](./docs/ospf-failover-floating-static.md) | Failover verification and the ten defects it uncovered |
| [`netconf-catalyst.md`](./docs/netconf-catalyst.md) | NETCONF implementation and real ncclient transcripts |
| [`gnmi-telemetry.md`](./docs/gnmi-telemetry.md) | gNMI and model-driven telemetry |
| [`nexpose-api.md`](./docs/nexpose-api.md) | Nexpose / InsightVM Console API v3 emulation (vulnerability data is fictional) |
| [`monitoring-stack-guide.md`](./docs/monitoring-stack-guide.md) | Prometheus / Grafana integration |
| [`feature-inventory.md`](./docs/feature-inventory.md) | Feature inventory (core emulation vs. tooling) |

---

## Layout

```
network-lab-emulator/
├── app.py                      # FastAPI server (entry point for CLI dispatch)
├── engine/
│   ├── protocols.py            # Protocol engines (RIP/OSPF/BGP/EIGRP/STP/vPC/MPLS…)
│   ├── rules.py                # CLI rule engine (vendor dialects, completion)
│   ├── real_{ospf,bgp,rip}_agent.py   # Real-packet protocol listeners
│   ├── netconf_agent.py        # NETCONF server (SSH/830)
│   ├── gnmi_agent.py           # gNMI server (gRPC/50052)
│   ├── snmp_udp_agent.py       # SNMP agent (UDP/161)
│   ├── programmability.py      # EEM / App Hosting / OpenFlow
│   └── syslog_sender.py        # syslog / SNMP trap / NTP
├── static/                     # Web UI
├── tests/                      # pytest (79 files)
├── tools/                      # Operational and verification tools (20+)
└── docs/                       # Documentation (75 files)
```

---

## Caveats

- **This is a learning and verification tool, not a drop-in replacement for real
  hardware.** Every feature document has an "unsupported" section spelling out where
  it diverges from the real device
- The default credentials are `admin` / `admin`. Change them before running anywhere
  that is not a closed environment
- `docs/reference/` contains vendor manual PDFs. Redistribution is subject to each
  vendor's terms — please check before relying on them

---

## License

MIT License.

## References

- [Fujitsu Si-R G series command reference](https://www.fsastech.com/ja-jp/products/network/router/manual/sir-g/)
- [Cisco IOS-XE Configuration Guide](https://www.cisco.com/c/en/us/support/ios-nx-os-software/ios-xe-17/series.html)
- [Cisco NX-OS Configuration Guide](https://www.cisco.com/c/en/us/support/switches/nexus-9000-series-switches/series.html)
- [APRESIA ApresiaLight user guide](https://www.apresia.jp/)
- [openconfig/gnmi](https://github.com/openconfig/gnmi) — source of the gNMI proto
