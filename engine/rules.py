"""
ルールベースCLIエンジン
Si-R / SR-S / Catalyst 9300 / Cisco IOS の出力を決定論的に生成
マニュアル原文フォーマット準拠
"""
import re
import time
import random
from datetime import datetime, timedelta

def _prefix_to_mask(prefix: int) -> str:
    bits = (0xffffffff >> (32 - prefix)) << (32 - prefix)
    return '.'.join(str((bits >> (8 * i)) & 0xff) for i in reversed(range(4)))

# ══════════════════════════════════════════
# デバイス状態（セッション単位で保持）
# ══════════════════════════════════════════
class DeviceState:
    def __init__(self, device_type: str, hostname: str):
        self.device_type = device_type  # sir / srs / catalyst / cisco / apresia / nexus / pc
        self.hostname = hostname
        self.mode = "exec"              # exec / config / config-if / config-router / config-vlan / config-vpc-domain
        self.current_if = None
        self.startup_time = datetime.now() - timedelta(hours=random.randint(1,72))

        # Cisco ASA (シングルコンテキスト ファイアウォール)
        if device_type == "asa":
            self.interfaces = {
                "GigabitEthernet0/0": {
                    "ip": "203.0.113.1", "prefix": 30,
                    "status": "up", "nameif": "outside", "security_level": 0,
                    "speed": "1000", "duplex": "full",
                },
                "GigabitEthernet0/1": {
                    "ip": "192.168.1.1", "prefix": 24,
                    "status": "up", "nameif": "inside",  "security_level": 100,
                    "speed": "1000", "duplex": "full",
                },
                "GigabitEthernet0/2": {
                    "ip": "172.16.0.1", "prefix": 24,
                    "status": "up", "nameif": "dmz",     "security_level": 50,
                    "speed": "1000", "duplex": "full",
                },
                "Management0/0": {
                    "ip": "192.168.100.1", "prefix": 24,
                    "status": "up", "nameif": "management", "security_level": 0,
                    "speed": "100", "duplex": "full",
                },
            }
            self.mode = "exec"
            self.acls        = {}   # {"acl_name": [{"action","proto","src","dst","port"}]}
            self.nat_rules   = []   # [{"type","real_iface","mapped_iface","real_net","mapped_net"}]
            self.routes      = []
            self.service_policies = []
            self.inspect_protocols = [
                "ftp","http","https","smtp","dns","h323","sip","skinny","sqlnet","tftp"
            ]
            self.ipsec_crypto  = {}  # {"map_name": {...}}
            self.ipsec_peers   = {}  # {"peer_ip": {...}}
            self.syslog_servers   = []
            self.snmp_hosts       = []
            self.snmp_community   = []
            self.snmp_location    = ""
            self.snmp_contact     = ""
            self.logging_level    = "informational"
            self.banner = ""
            return

        # PC (Linux汎用エンドポイント)
        if device_type == "pc":
            self.interfaces = {
                "eth0": {
                    "ip": "192.168.1.10", "prefix": 24,
                    "status": "up", "mac": "aa:bb:cc:dd:ee:01",
                    "speed": "1000", "duplex": "full",
                },
                "lo": {
                    "ip": "127.0.0.1", "prefix": 8,
                    "status": "up", "mac": "00:00:00:00:00:00",
                },
            }
            self.gateway = "192.168.1.1"
            self.routes = [
                {"dest": "0.0.0.0/0",       "gw": "192.168.1.1", "iface": "eth0", "metric": 0},
                {"dest": "192.168.1.0/24",   "gw": "0.0.0.0",    "iface": "eth0", "metric": 100},
                {"dest": "127.0.0.0/8",      "gw": "0.0.0.0",    "iface": "lo",   "metric": 0},
            ]
            self.arp_table = []
            self.syslog_servers = []
            self.snmp_hosts = []
            self.snmp_community = []
            self.snmp_location = ""
            self.snmp_contact = ""
            self.logging_level = "informational"
            return

        # NX-OS (Nexus 9000)
        if device_type == "nexus":
            self.interfaces = {
                **{f"Ethernet1/{i}": {
                    "ip": "", "prefix": 0,
                    "status": "connected" if i <= 4 else "notconnect",
                    "vlan": "1", "speed": "10G", "duplex": "full",
                    "desc": f"To Device-{i}" if i <= 4 else "",
                    "type": "10Gbase-SR",
                } for i in range(1, 49)},
                **{f"port-channel{i}": {
                    "ip": "", "prefix": 0,
                    "status": "connected" if i <= 2 else "notconnect",
                    "vlan": "trunk", "speed": "10G", "duplex": "full",
                    "desc": f"Port-Channel{i}",
                } for i in range(1, 5)},
                "mgmt0": {
                    "ip": "192.168.100.1", "prefix": 24,
                    "status": "connected", "speed": "1000", "duplex": "full",
                    "desc": "Out-of-Band Management",
                },
            }
            self.routes = [
                {"code":"C","dest":"192.168.100.0/24","gw":"directly","dist":0,"iface":"mgmt0"},
            ]
            self.vlans = {1: {'name':'default','status':'active','ports':[]}}
            self.arp_table = []
            self.vrrp = {}
            self.acls = {}
            self.syslog_servers = []
            self.logging_level = 'informational'
            self.snmp_community = []
            self.snmp_hosts = []
            self.snmp_location = ''
            self.banner = ''
            return

        # 設定値
        self.interfaces = {
            "lan0": {"ip": "192.168.1.1", "prefix": 24, "status": "up", "mac": "00:0e:0e:f1:41:dc"},
            "lan1": {"ip": "", "prefix": 24, "status": "down", "mac": "00:0e:0e:f1:41:dd"},
            "wan1": {"ip": "203.0.113.1", "prefix": 30, "status": "up", "mac": "00:0e:0e:f1:41:de"},
        } if device_type == "sir" else ({
            # APRESIA: Port1/0/1〜Port1/0/24 形式（ApresiaLightGM200マニュアル準拠）
            **{f"Port1/0/{i}": {
                "ip": "", "prefix": 0,
                "status": "connected" if i <= 2 else "notconnect",
                "vlan": "10" if i <= 2 else "1",
                "speed": "1000" if i <= 2 else "auto",
                "duplex": "full" if i <= 2 else "auto",
                "desc": f"To PC-{i}" if i <= 2 else "",
                "type": "1000BASE-T",
                "loopdetect": "enabled",
            } for i in range(1, 24)},
            "Port1/0/24": {"ip": "", "prefix": 0, "status": "connected",
                           "vlan": "trunk", "speed": "1000", "duplex": "full",
                           "desc": "To Uplink", "type": "1000BASE-T",
                           "allowed_vlan": "1,10,20"},
            "Vlan10": {"ip": "192.168.10.1", "prefix": 24, "status": "up", "desc": "Management"},
        }) if device_type == "apresia" else ({
            **{f"GigabitEthernet1/0/{i}": {
                "ip": "", "prefix": 0,
                "status": ("connected" if i <= 2 else
                           "err-disabled" if i == 3 else
                           "disabled" if i == 17 else "notconnect"),
                "vlan": "10" if i <= 2 else "1",
                "speed": "1000" if i <= 2 else "auto",
                "duplex": "full" if i <= 2 else "auto",
                "desc": f"#### To PC-{i} ####" if i <= 3 else "",
                "type": "1000BaseLX SFP" if i == 17 else "10/100/1000BaseTX",
                "err_reason": "bpduguard" if i == 3 else "",
            } for i in range(1, 24)},
            "GigabitEthernet1/0/24": {"ip": "", "prefix": 0, "status": "connected",
                                      "vlan": "trunk", "speed": "1000", "duplex": "full",
                                      "desc": "#### To Router ####", "type": "10/100/1000BaseTX",
                                      "native_vlan": "1", "allowed_vlan": "1,10,200"},
            "Vlan10": {"ip": "192.168.10.1", "prefix": 24, "status": "up", "desc": ""},
        }) if device_type in ("catalyst", "srs") else {
            "GigabitEthernet0/0/0": {"ip": "203.0.113.2", "prefix": 30, "status": "up",   "speed": "1000", "duplex": "full"},
            "GigabitEthernet0/0/1": {"ip": "10.0.0.1",    "prefix": 24, "status": "up",   "speed": "1000", "duplex": "full"},
            "GigabitEthernet0/0/2": {"ip": "",             "prefix": 0,  "status": "down", "speed": "auto", "duplex": "auto"},
        }

        self.routes = [
            {"fp":"*C","dest":"192.168.1.0/24","gw":"192.168.1.1","dist":0, "iface":"lan0"},
            {"fp":"*S","dest":"0.0.0.0/0",     "gw":"203.0.113.1","dist":200,"iface":"wan1"},
        ] if device_type == "sir" else [
            {"code":"C","dest":"192.168.10.0/24","gw":"directly","dist":0, "iface":"Vlan10"},
            {"code":"S","dest":"0.0.0.0/0",      "gw":"203.0.113.1","dist":1,"iface":"GigabitEthernet1/0/24"},
        ]

        self.arp_table = [
            {"ip":"192.168.1.100","mac":"00:1a:2b:3c:4d:5e","iface":"lan0","age":120},
            {"ip":"192.168.1.200","mac":"00:5e:6f:70:81:92","iface":"lan0","age":45},
        ]
        self.vrrp = {"vrid":10,"state":"Master","priority":254,"vip":"192.168.1.254","iface":"lan0"}
        self.rip_table = [
            {"fp":"*C","net":"192.168.1.0/24","gw":"0.0.0.0",       "metric":1,"time":"none","iface":"lan0"},
            {"fp":"*R","net":"192.168.2.0/24","gw":"192.168.1.254", "metric":2,"time":"02:49","iface":"lan0"},
        ]
        self.vlans = {
            1:   {"name":"default",    "status":"active","ports":["Gi1/0/3","Gi1/0/4"]},
            10:  {"name":"LAN-Segment","status":"active","ports":["Gi1/0/1","Gi1/0/2"]},
            20:  {"name":"Management", "status":"active","ports":["Gi1/0/10"]},
            100: {"name":"Server",     "status":"active","ports":[]},
        }
        self.stp = {"mode":"rapid-pvst","root":"8001.00:0e:0e:f1:00:01","priority":32768,"root_port":"Gi1/0/24"}
        self.cdp_neighbors = [
            {"device":"Core-SW",  "local_if":"Gi1/0/24","hold":150,"cap":"S","platform":"SR-S324TR1","port":"ether 10"},
            {"device":"GW-Router","local_if":"Gi1/0/1", "hold":120,"cap":"R","platform":"ISR4321",   "port":"Gi0/0/0"},
        ]
        self.hsrp = {"group":1,"vip":"192.168.1.254","priority":110,"state":"Active","preempt":True,"iface":"GigabitEthernet1/0/1"}
        self.ospf = {"process":1,"router_id":"10.0.0.1","area":"0.0.0.0",
            "neighbors":[{"id":"10.0.0.2","state":"FULL","iface":"Gi1/0/24","dead":"00:00:35"}]}
        self.bgp = {"asn":65001,"router_id":"10.1.0.1",
            "neighbors":[{"ip":"10.1.0.2","asn":65002,"state":"Established","uptime":"01:23:45","pfx":12}]}
        self.eigrp = {"asn":100,
            "neighbors":[{"ip":"10.0.0.2","iface":"Gi0/0/0","hold":12,"uptime":"01:23:45","srtt":5,"rto":200}]}

        # syslog / SNMP trap 設定（全機種共通）
        self.syslog_servers  = []   # [{"host":"x.x.x.x","port":514,"facility":"local7","level":"informational"}]
        self.snmp_hosts      = []   # [{"host":"x.x.x.x","community":"public","version":"2c","traps":"all"}]
        self.snmp_community  = []   # [{"name":"public","perm":"ro"},{"name":"private","perm":"rw"}]
        self.snmp_location   = ""
        self.snmp_contact    = ""
        self.logging_level   = "informational"   # emergencies/alerts/critical/errors/warnings/notifications/informational/debugging

        # Si-R IPsec VPN 状態（Si-R/Si-R brin 系）
        if device_type in ("sir", "srs"):
            self.ipsec_tunnels   = {}
            self.ike_policies    = {}
            self.ipsec_proposals = {}
            self.ipsec_enabled   = False
            self.ike_enabled     = False

        # Cisco IOS IPsec 状態（ASA は上で初期化済み）
        if device_type in ("cisco", "catalyst"):
            self.ipsec_crypto = {}   # isakmp_policies / transform_sets / crypto_maps / isakmp_keys / isakmp_enabled
            self.ipsec_peers  = {}   # {peer_ip: {status, phase1, phase2, spi_in, spi_out, ...}}

    def uptime_str(self):
        delta = datetime.now() - self.startup_time
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        if h >= 24:
            return f"{h//24} days, {h%24} hours, {m} minutes"
        return f"{h} hours, {m} minutes"


# ══════════════════════════════════════════
# コマンドエンジン本体
# ══════════════════════════════════════════
class RuleEngine:

    # ── CLI略称展開テーブル（Cisco IOS準拠）──────────────────
    # 先頭トークンの展開
    _ABBREV_FIRST = {
        'sh':    'show',
        'sho':   'show',
        'in':    'interface',
        'int':   'interface',
        'inte':  'interface',
        'inter': 'interface',
        'conf':  'configure',
        'confi': 'configure',
        'config':'configure',
        'ro':    'router',
        'rou':   'router',
        'rout':  'router',
        'no':    'no',
        'en':    'enable',
        'ex':    'exit',
        'exi':   'exit',
        'end':   'end',
        'do':    'do',
        'pi':    'ping',
        'pin':   'ping',
        'tr':    'traceroute',
        'tra':   'traceroute',
        'trac':  'traceroute',
        'wr':    'write',
        'wri':   'write',
        'writ':  'write',
        'cop':   'copy',
        'cl':    'clear',
        'cle':   'clear',
        'clea':  'clear',
        'deb':   'debug',
        'debu':  'debug',
        'un':    'undebug',
        'und':   'undebug',
    }

    # サブコマンドの展開（前のトークンをキーに）
    _ABBREV_SUB = {
        'show': {
            'ver':   'version',
            'vers':  'version',
            'versi': 'version',
            'versio':'version',
            'run':   'running-config',
            'runn':  'running-config',
            'runni': 'running-config',
            'runnin':'running-config',
            'running':'running-config',
            'start': 'startup-config',
            'star':  'startup-config',
            'ip':    'ip',
            'int':   'interfaces',
            'inte':  'interfaces',
            'inter': 'interfaces',
            'interf':'interfaces',
            'vl':    'vlan',
            'vla':   'vlan',
            'span':  'spanning-tree',
            'spann': 'spanning-tree',
            'spanni':'spanning-tree',
            'mac':   'mac',
            'log':   'logging',
            'logg':  'logging',
            'arp':   'arp',
            'cdp':   'cdp',
            'lldp':  'lldp',
            'sp':    'spanning-tree',
            'spa':   'spanning-tree',
            'eth':   'etherchannel',
            'ethe':  'etherchannel',
            'ether': 'etherchannel',
            'ethc':  'etherchannel',
            'etherchannel': {
                '_desc':   'EtherChannel information',
                'summary': 'One-line summary per channel-group',
                'detail':  'Detailed information of channel-groups',
                'port-channel': 'Port-channel information',
            },
            'vr':    'vrrp',
            'vrr':   'vrrp',
            'st':    'standby',
            'stan':  'standby',
            'stand': 'standby',
            'ar':    'arp',
            'standby': {
                '_desc': 'HSRP information',
                'brief': 'Brief status of standby groups',
                'all':   'All HSRP groups',
            },
            'cl':    'clock',
            'clo':   'clock',
            'mod':   'module',
            'modu':  'module',
            'feat':  'feature',
            'featu': 'feature',
            'vpc':   {
                '_desc':         'vPC information (NX-OS)',
                'brief':         'Brief vPC status',
                'peer-keepalive':'Peer keepalive status',
                'role':          'vPC role information',
                'consistency-parameters': 'Consistency parameters',
            },
        },
        'show ip': {
            'ro':    'route',
            'rou':   'route',
            'rout':  'route',
            'route': 'route',
            'os':    'ospf',
            'osp':   'ospf',
            'ospf':  'ospf',
            'ri':    'rip',
            'rip':   'rip',
            'bg':    'bgp',
            'bgp':   'bgp',
            'arp':   'arp',
            'int':   'interface',
        },
        'show ip ospf': {
            'ne':    'neighbor',
            'nei':   'neighbor',
            'neig':  'neighbor',
            'neigh': 'neighbor',
            'in':    'interface',
            'int':   'interface',
            'da':    'database',
            'dat':   'database',
            'data':  'database',
            'datab': 'database',
            'ro':    'route',
        },
        'show ip rip': {
            'ne':    'neighbor',
            'ro':    'route',
            'st':    'status',
        },
        'show ip bgp': {
            'su':    'summary',
            'sum':   'summary',
            'summ':  'summary',
            'ne':    'neighbor',
        },
        'show vpc': {
            'br':    'brief',
            'bri':   'brief',
            'peer':  'peer-keepalive',
            'peer-k':'peer-keepalive',
            'peer-ke':'peer-keepalive',
            'ro':    'role',
            'consi': 'consistency-parameters',
            'consist':'consistency-parameters',
        },
        'show interfaces': {
            'st':    'status',
            'sta':   'status',
            'stat':  'status',
            'tr':    'trunk',
            'tru':   'trunk',
            'trun':  'trunk',
            'de':    'description',
            'des':   'description',
            'desc':  'description',
            'co':    'counters',
            'cou':   'counters',
        },
        'configure': {
            't':     'terminal',
            'te':    'terminal',
            'ter':   'terminal',
            'term':  'terminal',
            'terminal':'terminal',
        },
        'write': {
            'me':    'memory',
            'mem':   'memory',
            'memo':  'memory',
            'memory':'memory',
            'ter':   'terminal',
        },
        'show spanning-tree': {
            'su':    'summary',
            'sum':   'summary',
            'br':    'brief',
            'bri':   'brief',
            'vl':    'vlan',
            'vla':   'vlan',
        },
        'router': {
            'os':    'ospf',
            'osp':   'ospf',
            'ri':    'rip',
            'rip':   'rip',
            'bg':    'bgp',
            'bgp':   'bgp',
            'ei':    'eigrp',
        },
        'interface': {
            'gi':    'gigabitethernet',
            'Gi':    'GigabitEthernet',
            'fa':    'fastethernet',
            'Fa':    'FastEthernet',
            'te':    'tengigabitethernet',
            'po':    'port-channel',
            'Po':    'Port-channel',
            'lo':    'loopback',
            'Lo':    'Loopback',
            'vl':    'vlan',
            'Vl':    'Vlan',
            'tu':    'tunnel',
        },
        'no': {
            'sh':    'shutdown',
            'shu':   'shutdown',
            'shut':  'shutdown',
            'ro':    'router',
            'in':    'interface',
        },
        'copy': {
            'run':   'running-config',
            'star':  'startup-config',
        },
        'clear': {
            'ip':    'ip',
            'arp':   'arp',
            'mac':   'mac',
            'span':  'spanning-tree',
        },
    }

    def _expand_abbreviation(self, cmd: str) -> str:
        """
        Cisco IOS準拠のCLI略称を展開する。
        例: 'sh ip ro' → 'show ip route'
            'in gi1/0/1' → 'interface GigabitEthernet1/0/1'
            'ro os 1' → 'router ospf 1'
        """
        if not cmd.strip():
            return cmd
        tokens = cmd.strip().split()
        if not tokens:
            return cmd

        result = []
        ctx = ''  # 現在のコンテキスト（展開済みトークンの累積）

        for i, tok in enumerate(tokens):
            tok_lower = tok.lower()

            if i == 0:
                # 先頭トークン展開
                expanded = self._ABBREV_FIRST.get(tok_lower, tok)
                result.append(expanded)
                ctx = expanded.lower()
            else:
                # サブコマンド展開（コンテキスト依存）
                expanded = tok
                # 現在のコンテキストで展開を試みる
                for ctx_key in [ctx, ctx.split()[-1] if ' ' in ctx else ctx]:
                    sub_map = self._ABBREV_SUB.get(ctx_key, {})
                    if tok_lower in sub_map:
                        expanded = sub_map[tok_lower]
                        break
                    # インターフェース略称の特殊処理
                    # gi1/0/1 → GigabitEthernet1/0/1
                    # ただし既にフル名（GigabitEthernetで始まる等）の場合はスキップ
                    if ctx_key in ('interface', 'show interfaces'):
                        _full_prefixes = ('gigabitethernet','fastethernet',
                                          'tengigabitethernet','port-channel',
                                          'loopback','vlan','tunnel')
                        if not any(tok_lower.startswith(fp) for fp in _full_prefixes):
                            for abbr, full in self._ABBREV_SUB.get('interface', {}).items():
                                if tok_lower.startswith(abbr) and len(tok) > len(abbr):
                                    rest = tok[len(abbr):]
                                    expanded = full + rest
                                    break
                result.append(expanded)
                ctx = ' '.join(result).lower()

        return ' '.join(result)


    def process(self, cmd: str, state: DeviceState) -> str:
        cmd = cmd.strip()
        if not cmd:
            return ""

        # ── ? ヘルプ（実機準拠）──
        # "?" / "show ?" / "show ip ?" などに対応
        if '?' in cmd:
            return cli_completion.get_help(cmd, state)

        # CLI略称展開（sh → show, in → interface 等）
        cmd = self._expand_abbreviation(cmd)

        c = cmd.lower().strip()

        # ── コマンド検証（エラー検出）──
        error = self._validate_command(cmd, c, state)
        if error:
            return error

        # PCはLinuxコマンド体系 → 専用ハンドラへ
        if state.device_type == 'pc':
            return self._pc_process(cmd, c, state)

        # Cisco ASA → 専用ハンドラへ
        if state.device_type == 'asa':
            return self._asa_process(cmd, c, state)

        # APRESIAはOSコマンド体系が独自 → 専用ハンドラへ
        if state.device_type == 'apresia':
            return self._apresia_process(cmd, c, state)

        # モード遷移
        if c in ("exit", "end", "quit"):
            return self._cmd_exit(cmd, state)
        # Cisco/Catalyst/SR-S: t または terminal が必須
        if state.device_type in ("catalyst", "cisco", "srs"):
            if c in ("configure terminal", "conf t", "conf terminal",
                     "config t", "config terminal",
                     "enable configure", "en conf"):
                return self._cmd_configure(state)
            if re.match(r'^conf(?:ig)?\s+te', c):
                return self._cmd_configure(state)
            # "configure" 単体はNG
            if c in ("configure", "conf", "config"):
                return f"% Invalid input detected at '^' marker.\n  {c}\n  ^"
        # Si-R / APRESIA: conf / config / configure 単体でOK（t/terminal不要）
        else:
            if c in ("configure", "conf", "config",
                     "configure terminal", "conf t", "config t"):
                return self._cmd_configure(state)
        if re.match(r'^interface\s+\S+', c):
            return self._cmd_interface(cmd, state)
        if re.match(r'^router\s+(ospf|bgp|eigrp|rip)(\s+\d+)?', c):
            return self._cmd_router_mode(cmd, state)
        if re.match(r'^vlan\s+\d+$', c) and state.mode == "config":
            return self._cmd_vlan_mode(cmd, state)

        # show系
        if c.startswith("show "):
            return self._cmd_show(cmd, state)

        # 設定コマンド
        if state.mode in ("config", "config-if", "config-router", "config-vlan"):
            return self._cmd_config(cmd, state)

        # 運用コマンド
        if c.startswith("ping "):
            return self._cmd_ping(cmd, state)
        if c.startswith("traceroute "):
            return self._cmd_traceroute(cmd, state)
        if c in ("write memory", "write", "copy running-config startup-config", "save", "commit"):
            return self._cmd_save(state)
        if c in ("reload", "reset"):
            return "Proceed with reload? [confirm] y\nSystem configuration has been modified. Save? [yes/no]: \n%SYS-5-RELOAD: Reload requested."
        if c in ("enable", "admin"):
            return ""
        if c in ("disable",):
            return ""
        if c in ("cls", "clear screen"):
            return "\x0c"  # フロントエンドでクリア処理

        # Si-R: IPsec/IKE/remote commands are valid in exec mode
        if state.device_type in ("sir", "srs") and re.match(
            r'^(remote\s+\d+|ipsec\s+|ike\s+)', c
        ):
            return self._cmd_config(cmd, state)

        # 不明コマンド
        return self._unknown(cmd, state)

    # ─── モード遷移 ───────────────────────────
    def _cmd_exit(self, cmd, state):
        if state.mode == "config-if":
            state.mode = "config"
        elif state.mode in ("config-router", "config-vlan", "config-vpc-domain"):
            state.mode = "config"
        elif state.mode == "config":
            state.mode = "exec"
        return ""

    def _cmd_configure(self, state):
        state.mode = "config"
        return "Enter configuration commands, one per line.  End with CNTL/Z."

    def _cmd_interface(self, cmd, state):
        m = re.match(r'^interface\s+(\S+)', cmd, re.I)
        if m:
            state.current_if = m.group(1)
            state.mode = "config-if"
        return ""

    def _cmd_router_mode(self, cmd, state):
        state.mode = "config-router"
        return ""

    def _cmd_vlan_mode(self, cmd, state):
        m = re.match(r'^vlan\s+(\d+)', cmd, re.I)
        if m:
            vid = int(m.group(1))
            if vid not in state.vlans:
                state.vlans[vid] = {"name": f"VLAN{vid:04d}", "status": "active", "ports": []}
            state.mode = "config-vlan"
        return ""

    # ─── show コマンド ────────────────────────
    def _cmd_show(self, cmd, state):
        c = cmd.lower().strip()
        dt = state.device_type

        # ── show version ──
        if re.match(r'^show\s+version$', c):
            return self._show_version(state)

        # ── show interfaces ──
        # 記事「Cisco動作確認を極める ～インターフェイス状態編～」準拠の7コマンド
        if re.match(r'^show\s+interfaces?\s+status\s+err-disabled$', c):
            return self._show_int_status_errdisabled(state)
        if re.match(r'^show\s+interfaces?\s+status$', c):
            return self._show_interfaces_status(state)
        if re.match(r'^show\s+interfaces?\s+description$', c):
            return self._show_int_description(state)
        if re.match(r'^show\s+interfaces?\s+trunk$', c):
            return self._show_int_trunk(state)
        if re.match(r'^show\s+interfaces?\s+transceiver$', c):
            return self._show_int_transceiver(state)
        if re.match(r'^show\s+interfaces?\s+counters\s+errors$', c):
            return self._show_int_counters_errors(state)
        if re.match(r'^show\s+interfaces?\s+brief$', c) or re.match(r'^show\s+ip\s+interfaces?\s+brief$', c):
            return self._show_ip_int_brief(state)

        # ── Stack（3.1章）──
        if re.match(r'^show\s+switch\s+detail', c):
            return self._show_switch_detail(state)
        if re.match(r'^show\s+switch', c):
            return self._show_switch(state)

        # ── IOS Management（3.2章）──
        if re.match(r'^show\s+ntp\s+status', c):
            return self._show_ntp_status(state)
        if re.match(r'^show\s+ntp\s+associations', c):
            return self._show_ntp_associations(state)
        if re.match(r'^show\s+logging', c):
            return self._show_logging(state)
        if re.match(r'^show\s+snmp', c):
            return self._show_snmp(state)

        # ── Security（3.5章）──
        psec_int = re.match(r'^show\s+port-security\s+interface\s+(\S+)', c)
        if psec_int:
            return self._show_port_security_interface(psec_int.group(1), state)
        if re.match(r'^show\s+port-security', c):
            return self._show_port_security(state)
        if re.match(r'^show\s+ip\s+dhcp\s+snooping', c):
            return self._show_dhcp_snooping(state)
        if re.match(r'^show\s+ip\s+arp\s+inspection', c):
            return self._show_dai(state)
        if re.match(r'^show\s+interfaces?$', c):
            return self._show_interfaces(state)
        if re.match(r'^show\s+interfaces?\s+(\S+)$', c):
            m = re.match(r'^show\s+interfaces?\s+(\S+)$', c)
            return self._show_interface_detail(m.group(1), state)

        # ── show ip route ──
        if re.match(r'^show\s+ip\s+route', c):
            return self._show_ip_route(state)

        # ── show arp ──
        if re.match(r'^show\s+(ip\s+)?arp', c):
            return self._show_arp(state)

        # ── show vlan ──
        if re.match(r'^show\s+vlan', c):
            return self._show_vlan(state)

        # ── show spanning-tree ──
        if re.match(r'^show\s+spanning-tree', c):
            return self._show_spanning_tree(state)

        # ── show cdp ──
        if re.match(r'^show\s+cdp\s+neighbors\s+detail', c):
            return self._show_cdp_detail(state)
        if re.match(r'^show\s+cdp\s+neighbors', c):
            return self._show_cdp(state)

        # ── show standby (HSRP) ──
        if re.match(r'^show\s+standby', c):
            return self._show_standby(state)

        # ── show ip ospf ──
        if re.match(r'^show\s+ip\s+ospf\s+neighbor', c):
            return self._show_ospf_neighbor(state)
        if re.match(r'^show\s+ip\s+ospf\s+database', c):
            return self._show_ospf_database(state)
        if re.match(r'^show\s+ip\s+ospf$', c):
            return self._show_ospf(state)

        # ── show bgp ──
        if re.match(r'^show\s+(ip\s+)?bgp\s+summary', c):
            return self._show_bgp_summary(state)
        if re.match(r'^show\s+(ip\s+)?bgp$', c):
            return self._show_bgp_table(state)

        # ── show ip eigrp ──
        if re.match(r'^show\s+ip\s+eigrp\s+neighbor', c):
            return self._show_eigrp_neighbors(state)
        if re.match(r'^show\s+ip\s+eigrp\s+topology', c):
            return self._show_eigrp_topology(state)

        # ── show vrrp (Si-R) ──
        if re.match(r'^show\s+vrrp\s+brief', c):
            return self._show_vrrp_brief(state)
        if re.match(r'^show\s+vrrp', c):
            return self._show_vrrp(state)

        # ── show ip rip (Si-R) ──
        if re.match(r'^show\s+ip\s+rip\s+route', c):
            return self._show_rip_route(state)
        if re.match(r'^show\s+ip\s+rip\s+protocol', c):
            return self._show_rip_protocol(state)

        # ── show ether (Si-R/SR-S) ──
        if re.match(r'^show\s+ether\s+brief$', c):
            return self._show_ether_brief(state)
        if re.match(r'^show\s+ether', c):
            return self._show_ether(state)

        # ── show system (Si-R) ──
        if re.match(r'^show\s+system\s+information', c):
            return self._show_system_info(state)
        if re.match(r'^show\s+system\s+status', c):
            return self._show_system_status(state)

        # ── show mac address-table ──
        if re.match(r'^show\s+mac\s+(address-table|address\s+table)', c):
            return self._show_mac_table(state)

        # ── show vtp ──
        if re.match(r'^show\s+vtp\s+status', c):
            return self._show_vtp(state)

        # ── show ipsec / ike (Si-R) ──
        if re.match(r'^show\s+ipsec\s+sa', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ipsec_sa(state)
        if re.match(r'^show\s+ipsec\s+tunnel', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ipsec_tunnel(state)
        if re.match(r'^show\s+ipsec\s+polic', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ipsec_policy(state)
        if re.match(r'^show\s+ipsec\s+stat', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ipsec_statistics(state)
        if re.match(r'^show\s+ipsec', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ipsec_sa(state)
        if re.match(r'^show\s+ike\s+sa', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ike_sa(state)
        if re.match(r'^show\s+ike\s+polic', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ike_policy(state)
        if re.match(r'^show\s+ike\s+stat', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ike_statistics(state)
        if re.match(r'^show\s+ike', c) and state.device_type in ('sir', 'srs'):
            return self._sir_show_ike_sa(state)

        # ── show running-config / candidate-config ──
        if re.match(r'^show\s+(run(ning-config)?|candidate-config|start(up-config)?|conf?(ig)?)\b', c) \
           or c in ('show run', 'sh run', 'show start', 'write terminal'):
            return self._show_running_config(state)

        # ── show logging ──
        if re.match(r'^show\s+logging', c):
            return self._show_logging(state)

        # ── Cisco IOS: show crypto ──
        if state.device_type in ('cisco', 'catalyst') and re.match(r'^show\s+crypto', c):
            return self._ios_show_crypto(cmd, c, state)

        # ── show tunnel-header / トンネルヘッダー付加エミュレーション ──
        if re.match(r'^show\s+tunnel[-\s]?header', c):
            return self._show_tunnel_header(cmd, state)

        # ── show processes cpu ──
        if re.match(r'^show\s+(processes\s+cpu|cpu)', c):
            return f"CPU utilization for five seconds: {random.randint(2,15)}%/{random.randint(1,8)}%; one minute: {random.randint(3,12)}%; five minutes: {random.randint(3,10)}%"

        # ── show memory ──
        if re.match(r'^show\s+(processes\s+)?memory', c):
            return f"Total: 512MB, Used: {random.randint(120,200)}MB, Free: {random.randint(300,390)}MB"

        return f"% Invalid input detected at '^' marker.\n  {cmd}\n  ^"

    # ─── show version ─────────────────────────
    def _show_version(self, state):
        now = datetime.now()
        if state.device_type == "sir":
            return f"""  Current-time : {now.strftime('%a %b %d %H:%M:%S %Y')}
  Startup-time : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}
  System : Si-R G120
  Serial No. : 00000001
  ROM Ver. : U-Boot 2018.03
  Firm Ver. : V20.00 NY0001 Sun Sep  1 10:11:05 JST 2024
  Running-firmware : firmware1
  Firmware1 Ver. : V20.00 NY0001
  Firmware2 Ver. : V20.00 NY0001
  MAC : 000e0ef141dc-000e0ef141dc
  Memory : 320MB"""
        elif state.device_type == "srs":
            return f"""  SR-S324TR1 V20 セキュアスイッチ
  Serial No. : SRS00000001
  Firm Ver. : V20.00 NY0001
  MAC : 000e0ef14200
  Memory : 256MB
  Startup-time : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}"""
        elif state.device_type == "nexus":
            return f"""Cisco Nexus Operating System (NX-OS) Software
TAC support: http://www.cisco.com/tac
Copyright (C) 2002-2024, Cisco and/or its affiliates.
All rights reserved.

Software
  BIOS: version 07.39
  NXOS: version 10.2(7)M
  BIOS compile time: 05/29/2023
  NXOS image file is: bootflash:///nxos64-cs.10.2.7.M.bin
  NXOS compile time: 1/26/2024 3:00:00 [01/26/2024 11:25:04]

Hardware
  cisco Nexus9000 C9364C-GX Chassis ("Supervisor Module")
  Intel(R) Xeon(R) CPU E5-2650 v4 @ 2.20GHz with 24534016 kB of memory.
  Processor Board ID FDO23521LKP

  Device name: {state.hostname}
  bootflash:   53298520 kB
Kernel uptime is {state.uptime_str()}

Last reset at 123456 usecs after  Mon Jan  1 00:00:00 2024
  Reason: Reset Requested by CLI command reload
  System version: 10.2(6)
  Service: 

plugin
  Core Plugin, Ethernet Plugin"""
        elif state.device_type == "catalyst":
            return f"""Cisco IOS XE Software, Version 17.09.01
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.1
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2022 by Cisco Systems, Inc.

cisco C9300-24T (X86) processor with 1474560K/6147K bytes of memory.
Processor board ID FCW2xxx0001

8 Virtual Ethernet interfaces
56 Gigabit Ethernet interfaces
2048K bytes of non-volatile configuration memory.
4194304K bytes of physical memory.

Base Ethernet MAC Address          : 00:0e:0e:f1:42:00
Motherboard Assembly Number        : 73-18506-04
System serial number               : FCW2xxx0001

Configuration register is 0x102

{state.hostname} uptime is {state.uptime_str()}"""
        else:
            return f"""Cisco IOS Software [Cupertino], ISR Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.9.1
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2022 by Cisco Systems, Inc.

Cisco ISR4321/K9 (1RU) processor with 1647295K/6147K bytes of memory.
Processor board ID FLM2xxx0001

2 Gigabit Ethernet interfaces
32768K bytes of non-volatile configuration memory.
4194304K bytes of physical memory.

{state.hostname} uptime is {state.uptime_str()}
System image file is "bootflash:isr4300-universalk9.17.09.01.SPA.bin" """

    # ─── show interfaces ──────────────────────
    def _show_interfaces(self, state):
        lines = []
        for name, iface in state.interfaces.items():
            status = iface.get("status","up")
            ip = iface.get("ip","")
            prefix = iface.get("prefix",24)
            mac = iface.get("mac","00:00:00:00:00:00")
            up = "up" if status in ("up","connected") else "down"
            lines.append(f"""{name}                MTU 1500      <{'UP' if up=='up' else 'DOWN'},BROADCAST,{'RUNNING,' if up=='up' else ''}MULTICAST>
     Status: {up} since {state.startup_time.strftime('%b %d %H:%M:%S %Y')}
     MAC address: {mac}""")
            if ip:
                lines.append(f"     IP address/masklen:\n       {ip}/{prefix}")
            lines.append("")
        return "\n".join(lines)

    def _show_interfaces_status(self, state):
        # 記事準拠フォーマット: Port Name Status Vlan Duplex Speed Type
        lines = ["",
                 "Port      Name               Status       Vlan       Duplex  Speed Type"]
        for name, iface in state.interfaces.items():
            short = name.replace("GigabitEthernet","Gi").replace("Vlan","Vl").replace("Port-channel","Po")
            desc = iface.get("desc","")[:18]
            status = iface.get("status","notconnect")
            vlan = iface.get("vlan","1")
            # notconnectのトランクはVlanに1と表示（記事の注意点）
            if status == "notconnect" and vlan == "trunk":
                vlan = "1"
            duplex = iface.get("duplex","auto")
            speed = iface.get("speed","auto")
            # connectedなら a- プレフィックス（オートネゴ結果）
            if status == "connected":
                dstr = f"a-{duplex}"
                sstr = f"a-{speed}"
            else:
                dstr = duplex
                sstr = speed
            typ = iface.get("type","10/100/1000BaseTX") if not name.startswith("Vlan") else ""
            lines.append(f"{short:<10}{desc:<19}{status:<13}{vlan:<11}{dstr:<8}{sstr:<7}{typ}")
        return "\n".join(lines)

    def _show_int_status_errdisabled(self, state):
        """show interfaces status err-disabled（記事準拠）"""
        lines = ["",
                 "Port      Name               Status       Reason               Err-disabled Vlans"]
        found = False
        for name, iface in state.interfaces.items():
            if iface.get("status") == "err-disabled":
                found = True
                short = name.replace("GigabitEthernet","Gi")
                desc = iface.get("desc","")[:18]
                reason = iface.get("err_reason","bpduguard")
                lines.append(f"{short:<10}{desc:<19}err-disabled {reason}")
        if not found:
            lines.append("(err-disabledのポートはありません)")
        return "\n".join(lines)

    def _show_int_description(self, state):
        """show interfaces description（記事準拠・200文字まで折り返し）"""
        lines = ["Interface                      Status         Protocol Description"]
        for name, iface in state.interfaces.items():
            short = name.replace("GigabitEthernet","Gi").replace("Vlan","Vl").replace("Port-channel","Po")
            status = iface.get("status","notconnect")
            if status in ("connected","up"):
                st, proto = "up", "up"
            elif status == "disabled":
                st, proto = "admin down", "down"
            elif status == "err-disabled":
                st, proto = "err-disabled", "down"
            else:
                st, proto = "down", "down"
            desc = iface.get("desc","")
            lines.append(f"{short:<31}{st:<15}{proto:<9}{desc}")
        return "\n".join(lines)

    def _show_int_trunk(self, state):
        """show interfaces trunk（記事準拠・リンクアップしたトランクのみ）"""
        trunks = [(n,i) for n,i in state.interfaces.items()
                  if i.get("vlan")=="trunk" and i.get("status")=="connected"]
        if not trunks:
            return "(トランクポートでconnectedのものはありません)"
        lines = ["", "Port        Mode             Encapsulation  Status        Native vlan"]
        for name, iface in trunks:
            short = name.replace("GigabitEthernet","Gi").replace("Port-channel","Po")
            native = iface.get("native_vlan","1")
            lines.append(f"{short:<12}on               802.1q         trunking      {native}")
        lines.append("")
        lines.append("Port        Vlans allowed on trunk")
        for name, iface in trunks:
            short = name.replace("GigabitEthernet","Gi").replace("Port-channel","Po")
            lines.append(f"{short:<12}1-4094")
        lines.append("")
        lines.append("Port        Vlans allowed and active in management domain")
        for name, iface in trunks:
            short = name.replace("GigabitEthernet","Gi").replace("Port-channel","Po")
            allowed = iface.get("allowed_vlan","1,10,200")
            lines.append(f"{short:<12}{allowed}")
        lines.append("")
        lines.append("Port        Vlans in spanning tree forwarding state and not pruned")
        for name, iface in trunks:
            short = name.replace("GigabitEthernet","Gi").replace("Port-channel","Po")
            allowed = iface.get("allowed_vlan","1,10,200")
            lines.append(f"{short:<12}{allowed}")
        return "\n".join(lines)

    def _show_int_transceiver(self, state):
        """show interfaces transceiver（記事準拠・光ポートのみ）"""
        # SFP/光ポートを持つものだけ表示（type に SFP/LX を含む）
        optical = [(n,i) for n,i in state.interfaces.items()
                   if "SFP" in i.get("type","") or "LX" in i.get("type","")]
        lines = [
            "If device is externally calibrated, only calibrated values are printed.",
            "++ : high alarm, +  : high warning, -  : low warning, -- : low alarm.",
            "NA or N/A: not applicable, Tx: transmit, Rx: receive.",
            "mA: milliamperes, dBm: decibels (milliwatts).",
            "",
            "                                 Optical   Optical",
            "           Temperature  Voltage  Tx Power  Rx Power",
            "Port       (Celsius)    (Volts)  (dBm)     (dBm)",
            "---------  -----------  -------  --------  --------",
        ]
        if not optical:
            lines.append("(光接続ポートはありません。SFP-LX等を接続してください)")
            return "\n".join(lines)
        import random as _r
        for name, iface in optical:
            short = name.replace("GigabitEthernet","Gi")
            temp = round(_r.uniform(30,40),1)
            volt = round(_r.uniform(3.2,3.4),2)
            tx = round(_r.uniform(-5,-3),1)
            rx = round(_r.uniform(-10,-6),1)
            lines.append(f"{short:<11}{temp:<13}{volt:<9}{tx:<10}{rx}")
        return "\n".join(lines)

    def _show_int_counters_errors(self, state):
        """show interfaces counters errors（記事準拠・エラーカウンタ）"""
        lines = ["",
                 "Port        Align-Err     FCS-Err    Xmit-Err     Rcv-Err  UnderSize  OutDiscards"]
        phys = [(n,i) for n,i in state.interfaces.items()
                if n.startswith("GigabitEthernet")]
        for name, iface in phys:
            short = name.replace("GigabitEthernet","Gi")
            # 正常時は全部0
            lines.append(f"{short:<12}{'0':<14}{'0':<11}{'0':<12}{'0':<9}{'0':<11}0")
        lines.append("")
        lines.append("Port      Single-Col  Multi-Col   Late-Col  Excess-Col  Carri-Sen      Runts     Giants")
        for name, iface in phys:
            short = name.replace("GigabitEthernet","Gi")
            lines.append(f"{short:<10}{'0':<12}{'0':<11}{'0':<11}{'0':<12}{'0':<11}{'0':<11}0")
        return "\n".join(lines)

    def _show_ip_int_brief(self, state):
        lines = ["Interface              IP-Address      OK? Method Status                Protocol"]
        for name, iface in state.interfaces.items():
            ip = iface.get("ip") or "unassigned"
            ok = "YES" if iface.get("ip") else "YES"
            method = "NVRAM" if iface.get("ip") else "unset"
            status = "up" if iface.get("status") in ("up","connected") else "administratively down"
            proto = "up" if iface.get("status") in ("up","connected") else "down"
            lines.append(f"{name:<23}{ip:<16}{ok:<4}{method:<8}{status:<22}{proto}")
        return "\n".join(lines)

    def _show_interface_detail(self, ifname, state):
        # 記事準拠の個別インタフェース詳細（Cisco IOS形式）
        target = None
        tname = ifname.lower().replace("gigabitethernet","gi").replace("gi","gi")
        for name, iface in state.interfaces.items():
            short = name.lower().replace("gigabitethernet","gi").replace("vlan","vl")
            if name.lower() == ifname.lower() or short == ifname.lower():
                target = (name, iface)
                break
        if not target:
            return f"% Invalid input detected: interface {ifname} not found"
        if state.device_type == "sir":
            return self._show_interfaces(state)
        name, iface = target
        status = iface.get("status","notconnect")
        up = status in ("up","connected")
        line_proto = "up (connected)" if up else ("down (notconnect)" if status=="notconnect"
                     else "down (err-disabled)" if status=="err-disabled" else "administratively down")
        mac = "5061.bfda." + f"{abs(hash(name))%10000:04d}"
        desc = iface.get("desc","")
        duplex = iface.get("duplex","auto")
        speed = iface.get("speed","auto")
        dup_str = "Full-duplex" if duplex in ("full","a-full") else ("Half-duplex" if duplex=="half" else "Auto-duplex")
        spd_str = f"{speed}Mb/s" if speed not in ("auto","a-1000","a-100") else "1000Mb/s"
        typ = iface.get("type","10/100/1000BaseTX")
        L = []
        L.append(f"{name} is {'up' if up else 'down'}, line protocol is {'up' if up else 'down'} ({'connected' if up else 'notconnect'})")
        L.append(f"  Hardware is Gigabit Ethernet, address is {mac} (bia {mac})")
        if desc:
            L.append(f"  Description: {desc}")
        L.append("  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,")
        L.append("     reliability 255/255, txload 1/255, rxload 1/255")
        L.append("  Encapsulation ARPA, loopback not set")
        L.append("  Keepalive set (10 sec)")
        L.append(f"  {dup_str}, {spd_str}, media type is {typ}")
        L.append("  input flow-control is off, output flow-control is unsupported")
        L.append("  ARP type: ARPA, ARP Timeout 04:00:00")
        L.append("  Last input 00:00:01, output 00:00:01, output hang never")
        L.append('  Last clearing of "show interface" counters never')
        L.append("  Input queue: 0/75/0/0 (size/max/drops/flushes); Total output drops: 0")
        L.append("  Queueing strategy: fifo")
        L.append("  Output queue: 0/40 (size/max)")
        L.append("  5 minute input rate 0 bits/sec, 0 packets/sec")
        L.append("  5 minute output rate 0 bits/sec, 0 packets/sec")
        L.append("     5 packets input, 1568 bytes, 0 no buffer")
        L.append("     Received 5 broadcasts (5 multicasts)")
        L.append("     0 runts, 0 giants, 0 throttles")
        L.append("     0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored")
        L.append("     0 watchdog, 5 multicast, 0 pause input")
        L.append("     0 input packets with dribble condition detected")
        L.append("     1781 packets output, 163390 bytes, 0 underruns")
        L.append("     0 output errors, 0 collisions, 1 interface resets")
        L.append("     0 unknown protocol drops")
        L.append("     0 babbles, 0 late collision, 0 deferred")
        L.append("     0 lost carrier, 0 no carrier, 0 pause output")
        L.append("     0 output buffer failures, 0 output buffers swapped out")
        return "\n".join(L)

    # ─── show ip route ────────────────────────
    def _show_ip_route(self, state):
        if state.device_type == "sir":
            lines = ["FP Destination/Mask    Gateway         Distance UpTime    Interface",
                     "--- ------------------ --------------- -------- --------- ------------------"]
            for r in state.routes:
                lines.append(f"{r['fp']:<3} {r['dest']:<19}{r['gw']:<16}{str(r['dist']):<9}00:01:23  {r['iface']}")
        else:
            lines = ["Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP",
                     "       D - EIGRP, EX - EIGRP external, i - IS-IS",
                     "       * - candidate default",
                     ""]
            for r in state.routes:
                code = r.get("code","C")
                lines.append(f"{code}     {r['dest']} [{r['dist']}/0] via {r['gw']}, {r['iface']}")
        return "\n".join(lines)

    # ─── show arp ─────────────────────────────
    def _show_arp(self, state):
        if state.device_type == "sir":
            lines = ["IP Address      MAC Address       F Rest  Interface",
                     "--------------- ----------------- -- ----- ---------"]
            for a in state.arp_table:
                lines.append(f"{a['ip']:<16}{a['mac']:<18}  {str(a['age']).rjust(5)}  {a['iface']}")
            lines.append(f"    Entry:{len(state.arp_table)}")
        else:
            lines = ["Protocol  Address          Age (min)  Hardware Addr   Type   Interface"]
            for a in state.arp_table:
                lines.append(f"Internet  {a['ip']:<17}{a['age']:<11}{a['mac']:<16}ARPA   {a['iface']}")
        return "\n".join(lines)

    # ─── show ether (Si-R / SR-S) ─────────────
    def _show_ether(self, state):
        lines = []
        for i, (name, iface) in enumerate(state.interfaces.items(), 1):
            grp = 1 if i <= 4 else 2
            port = ((i-1) % 4) + 1
            status = iface.get("status","up")
            speed = "1000M Full" if status == "up" else "down"
            lines.append(f"[ETHER GROUP-{grp} PORT-{port}]")
            lines.append(f"description      : {name}")
            lines.append(f"status           : {'auto '+speed if status=='up' else 'down'} MDI-X")
            lines.append(f"media            : Metal")
            lines.append(f"flow control     : send on, receive on")
            lines.append(f"type             : Normal")
            lines.append(f"since            : {state.startup_time.strftime('%b %d %H:%M:%S %Y')}")
            lines.append(f"config           : mode(auto), mdi(auto), media(-)")
            lines.append("")
        return "\n".join(lines)

    def _show_ether_brief(self, state):
        lines = ["Group Port  Status           Type",
                 "----- ----- ---------------- -------"]
        for i, (name, iface) in enumerate(state.interfaces.items(), 1):
            grp = 1 if i <= 4 else 2
            port = ((i-1) % 4) + 1
            status = "1000M Full" if iface.get("status") == "up" else "down"
            lines.append(f"{grp:<6}{port:<6}{status:<17}Normal")
        return "\n".join(lines)

    # ─── show system info (Si-R) ──────────────
    def _show_system_info(self, state):
        now = datetime.now()
        return f"""  Current-time : {now.strftime('%a %b %d %H:%M:%S %Y')}
  Startup-time : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}
  System : Si-R G120
  Serial No. : 00000001
  ROM Ver. : U-Boot 2018.03-00070
  Firm Ver. : V20.00 NY0001 Sun Sep  1 10:11:05 JST 2024
  Running-firmware : firmware1
  Firmware1 Ver. : V20.00 NY0001
  Firmware2 Ver. : V20.00 NY0001
  Startup-config : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}
  Running-config : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}
  MAC : 000e0ef141dc-000e0ef141dc
  Memory : 320MB
  USB     : ------
  WWAN1   : ------"""

    def _show_system_status(self, state):
        now = datetime.now()
        temp = random.randint(42, 55)
        return f"""  Current-time         : {now.strftime('%a %b %d %H:%M:%S %Y')}
  Startup-time         : {state.startup_time.strftime('%a %b %d %H:%M:%S %Y')}
  restart_cause        : power on
  machine_state        : RUNNING
  corefile             : empty
  power_state          : NORMAL
  power_consumption    : 8 W
  internal_state       : NORMAL
  internal_temp        : {temp} C"""

    # ─── show vlan ────────────────────────────
    def _show_vlan(self, state):
        lines = ["VLAN Name                             Status    Ports",
                 "---- -------------------------------- --------- -------------------------------"]
        for vid, v in state.vlans.items():
            ports = ", ".join(v["ports"][:4]) if v["ports"] else ""
            lines.append(f"{str(vid):<5}{v['name']:<33}{v['status']:<10}{ports}")
        return "\n".join(lines)

    # ─── show spanning-tree ───────────────────
    def _show_spanning_tree(self, state):
        stp = state.stp
        return f"""VLAN0010
  Spanning tree enabled protocol {stp['mode']}
  Root ID    Priority    {state.vlans.get(10,{}).get('name','')and 32768}
             Address     {stp['root']}
             This bridge is the root
             Hello Time   2 sec  Max Age 20 sec  Forward Delay 15 sec

  Bridge ID  Priority    32768 (priority 32768 sys-id-ext 10)
             Address     00:0e:0e:f1:42:00
             Hello Time   2 sec  Max Age 20 sec  Forward Delay 15 sec
             Aging Time  300 sec

Interface           Role Sts Cost      Prio.Nbr Type
------------------- ---- --- --------- -------- --------------------------------
Gi1/0/1             Desgn FWD 4         128.1    P2p
Gi1/0/2             Desgn FWD 4         128.2    P2p
Gi1/0/24            Root  FWD 4         128.24   P2p"""

    # ─── show cdp ─────────────────────────────
    def _show_cdp(self, state):
        lines = ["Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge",
                 "                  S - Switch, H - Host, I - IGMP, r - Repeater",
                 "",
                 "Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID"]
        for n in state.cdp_neighbors:
            lines.append(f"{n['device']:<17}{n['local_if']:<18}{n['hold']:<11}{n['cap']:<14}{n['platform']:<10}{n['port']}")
        lines.append(f"\nTotal cdp entries displayed : {len(state.cdp_neighbors)}")
        return "\n".join(lines)

    def _show_cdp_detail(self, state):
        lines = []
        for n in state.cdp_neighbors:
            lines += [f"-------------------------",
                f"Device ID: {n['device']}",
                f"Entry address(es):",
                f"  IP address: 192.168.1.{random.randint(2,254)}",
                f"Platform: Cisco {n['platform']},  Capabilities: {'Switch' if n['cap']=='S' else 'Router'}",
                f"Interface: {n['local_if']},  Port ID (outgoing port): {n['port']}",
                f"Holdtime : {n['hold']} sec", ""]
        return "\n".join(lines)

    # ─── show standby (HSRP) ──────────────────
    def _show_standby(self, state):
        h = state.hsrp
        return f"""{h['iface']} - Group {h['group']}
  State is {h['state']}
    2 state changes, last state change 00:01:23
  Virtual IP address is {h['vip']}
  Active virtual MAC address is 00:00:0c:07:ac:{h['group']:02x}
  Hello time 3 sec, hold time 10 sec
    Next hello sent in 1 secs
  Preemption {'enabled' if h['preempt'] else 'disabled'}
  Active router is {'local' if h['state']=='Active' else '192.168.1.2'}
  Standby router is {'192.168.1.2' if h['state']=='Active' else 'local'}
  Priority {h['priority']} (configured {h['priority']})"""

    # ─── show ospf ────────────────────────────
    def _show_ospf(self, state):
        o = state.ospf
        return f""" Routing Process "ospf {o['process']}" with ID {o['router_id']}
 Start time: {state.startup_time.strftime('%H:%M:%S.%f')[:15]}, Time elapsed: {state.uptime_str()}
 Supports only single TOS(TOS0) routes
 Supports opaque LSA
 Supports Link-local Signaling (LLS)
 Area BACKBONE({o['area']})
     Number of interfaces in this area is {len(state.interfaces)}
     Area has no authentication
     SPF algorithm last executed {random.randint(1,60):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d} ago
     Number of LSA {random.randint(5,20)}"""

    def _show_ospf_neighbor(self, state):
        lines = ["Neighbor ID     Pri   State           Dead Time   Address         Interface"]
        for n in state.ospf["neighbors"]:
            lines.append(f"{n['id']:<16}{1:<6}{('FULL/  -'):<16}{n['dead']:<12}{'192.168.1.'+n['id'].split('.')[-1]:<16}{n['iface']}")
        return "\n".join(lines)

    def _show_ospf_database(self, state):
        o = state.ospf
        return f"""            OSPF Router with ID ({o['router_id']}) (Process ID {o['process']})

\t\tRouter Link States (Area 0)

Link ID         ADV Router      Age         Seq#       Checksum Link count
{o['router_id']:<16}{o['router_id']:<16}{random.randint(100,900):<12}0x{random.randint(0x80000001,0x8000ffff):08x}  0x{random.randint(0x1000,0xffff):04x}   3
10.0.0.2        10.0.0.2        {random.randint(100,900):<12}0x{random.randint(0x80000001,0x8000ffff):08x}  0x{random.randint(0x1000,0xffff):04x}   2"""

    # ─── show bgp ─────────────────────────────
    def _show_bgp_summary(self, state):
        b = state.bgp
        lines = [f"BGP router identifier {b['router_id']}, local AS number {b['asn']}",
                 f"BGP table version is 1, main routing table version 1",
                 "",
                 "Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd"]
        for n in b["neighbors"]:
            lines.append(f"{n['ip']:<16}4{str(n['asn']).rjust(6)}{str(random.randint(50,200)).rjust(8)}{str(random.randint(50,200)).rjust(8)}{str(1).rjust(9)}{str(0).rjust(5)}{str(0).rjust(5)} {n['uptime']:<9} {n['pfx']}")
        return "\n".join(lines)

    def _show_bgp_table(self, state):
        b = state.bgp
        lines = [f"BGP table version is 1, local router ID is {b['router_id']}",
                 "Status codes: s suppressed, d damped, h history, * valid, > best, i - internal",
                 "Origin codes: i - IGP, e - EGP, ? - incomplete",
                 "",
                 "   Network          Next Hop            Metric LocPrf Weight Path"]
        lines.append(f"*> 192.168.1.0/24   0.0.0.0                  0         32768 i")
        lines.append(f"*> 192.168.2.0/24   10.1.0.2                 0    100      0 {b['neighbors'][0]['asn']} i")
        return "\n".join(lines)

    # ─── show eigrp ───────────────────────────
    def _show_eigrp_neighbors(self, state):
        e = state.eigrp
        lines = [f"EIGRP-IPv4 Neighbors for AS({e['asn']})",
                 "H   Address         Interface         Hold Uptime   SRTT   RTO  Q  Seq",
                 "                                      (sec)         (ms)       Cnt Num"]
        for i, n in enumerate(e["neighbors"]):
            lines.append(f"{i:<4}{n['ip']:<16}{n['iface']:<18}{n['hold']:<6}{n['uptime']:<10}{n['srtt']:<7}{n['rto']:<5}0  {random.randint(10,50)}")
        return "\n".join(lines)

    def _show_eigrp_topology(self, state):
        e = state.eigrp
        return f"""EIGRP-IPv4 Topology Table for AS({e['asn']})/ID(10.2.0.1)
Codes: P - Passive, A - Active, U - Update, Q - Query, R - Reply

P 192.168.1.0/24, 1 successors, FD is 28160
        via Connected, GigabitEthernet0/0/0
P 192.168.2.0/24, 1 successors, FD is 30720
        via 10.0.0.2 (30720/28160), GigabitEthernet0/0/0"""

    # ─── show vrrp (Si-R) ─────────────────────
    def _show_vrrp(self, state):
        v = state.vrrp
        return f"""[{v['iface']}]
  VRID {v['vrid']}
    State: {v['state']}
    Priority: {v['priority']}
    Virtual MAC Address: 00:00:5e:00:01:{v['vrid']:02x}
    Virtual Router IP Address: {v['vip']}
    Advertisement Interval: 1 sec
    Master IP Address: {'192.168.1.1' if v['state']=='Master' else '192.168.1.2'}
    Preempt: {'ON' if v['priority']>=100 else 'OFF'}"""

    def _show_vrrp_brief(self, state):
        v = state.vrrp
        return f"""[{v['iface']}]
  VRID Status
    {v['vrid']}  {v['state']}"""

    # ─── show ip rip (Si-R) ───────────────────
    def _show_rip_route(self, state):
        lines = ["FP Destination/Mask     Gateway           Metric   Time    Interface"]
        for r in state.rip_table:
            lines.append(f"{r['fp']:<3}{r['net']:<23}{r['gw']:<18}{str(r['metric']):<9}{r['time']:<8}{r['iface']}")
        lines.append(f"The number of entries : {len(state.rip_table)}")
        return "\n".join(lines)

    def _show_rip_protocol(self, state):
        return f"""Sending updates every 30 seconds with +/-50%, next due in {random.randint(5,25)} seconds
Timeout after 180 seconds, garbage collect after 120 seconds
Redistributing: Connected, Static
Interface        Send     Recv
  lan0            2        1 2
Routing Information Sources:
  Gateway           Rcv-Bad-Packets Rcv-Bad-Routes Last-Update
  192.168.1.254                   0              0 00:00:{random.randint(10,59):02d}
Distance: 120
The number of entries : {len(state.rip_table)}"""

    # ─── show mac address-table ───────────────
    def _show_mac_table(self, state):
        lines = ["          Mac Address Table",
                 "-------------------------------------------",
                 "",
                 "Vlan    Mac Address       Type        Ports",
                 "----    -----------       --------    -----"]
        macs = [
            ("10","00:1a:2b:3c:4d:5e","DYNAMIC","Gi1/0/1"),
            ("10","00:2b:3c:4d:5e:6f","DYNAMIC","Gi1/0/2"),
            ("1", "ff:ff:ff:ff:ff:ff","STATIC", "CPU"),
        ]
        for m in macs:
            lines.append(f" {m[0]:<8}{m[1]:<20}{m[2]:<12}{m[3]}")
        lines.append(f"Total Mac Addresses for this criterion: {len(macs)}")
        return "\n".join(lines)

    # ─── show vtp ─────────────────────────────
    def _show_vtp(self, state):
        return f"""VTP Version capable             : 1 to 3
VTP version running             : 2
VTP Domain Name                 : CORP
VTP Pruning Mode                : Disabled
VTP Traps Generation            : Disabled

Feature VLAN:
--------------
VTP Operating Mode                : Server
Maximum VLANs supported locally   : 1005
Number of existing VLANs          : {len(state.vlans)}
Configuration Revision            : 5"""

    # ─── Si-R IPsec show コマンド ─────────────
    def _sir_show_ipsec_sa(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IPsec SA established.'
        lines = [
            '  Remote       Local        Protocol  SPI(In)    SPI(Out)   State',
            '  -----------  -----------  --------  ---------  ---------  -----------',
        ]
        for tid, t in tunnels.items():
            remote = t.get('remote_ip', '?.?.?.?')
            local  = t.get('local_ip',  '?.?.?.?')
            proto  = t.get('protocol', 'esp').upper()
            phase2 = t.get('phase2', 'LARVAL')
            status = t.get('status', 'wait')
            if status == 'established' and phase2 == 'MATURE':
                spi_in  = t.get('spi_in',  f'0x{abs(hash(remote+str(tid)))%0xffffffff:08x}')
                spi_out = t.get('spi_out', f'0x{abs(hash(local +str(tid)))%0xffffffff:08x}')
                state_str = 'MATURE'
            elif phase2 == 'DYING':
                spi_in = spi_out = '-'
                state_str = 'DYING'
            else:
                spi_in = spi_out = '-'
                state_str = 'LARVAL'
            lines.append(f'  {remote:<13}{local:<13}{proto:<10}{spi_in:<11}{spi_out:<11}{state_str}')
        return '\n'.join(lines)

    def _sir_show_ipsec_tunnel(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IPsec tunnel configured.'
        lines = ['  Tunnel  Status       Local-IP        Remote-IP       Encrypt    Hash   DH   Mode']
        lines.append('  ------  -----------  --------------  --------------  ---------  -----  ---  ----------')
        for tid, t in tunnels.items():
            remote = t.get('remote_ip', '-')
            local  = t.get('local_ip',  '-')
            enc    = t.get('encryption', 'aes256')
            hsh    = t.get('hash', 'sha256')
            dh     = str(t.get('dh_group', 14))
            mode   = t.get('ike_mode', 'main')
            status = 'Established' if t.get('status') == 'established' else 'Waiting'
            lines.append(f'  {tid:<8}{status:<13}{local:<16}{remote:<16}{enc:<11}{hsh:<7}{dh:<5}{mode}')
        return '\n'.join(lines)

    def _sir_show_ike_sa(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IKE SA.'
        lines = ['  Peer             Phase1   Initiator  DH   Lifetime(rem)  Mode']
        lines.append('  ---------------  -------  ---------  ---  -------------  ----------')
        for tid, t in tunnels.items():
            remote = t.get('remote_ip', '?.?.?.?')
            ph1    = t.get('phase1', 'LARVAL')
            dh     = str(t.get('dh_group', 14))
            lt_rem = str(t.get('ike_lifetime', 86400) - random.randint(0, 3600))
            mode   = t.get('ike_mode', 'main')
            lines.append(f'  {remote:<17}{ph1:<9}{"yes":<11}{dh:<5}{lt_rem:<15}{mode}')
        return '\n'.join(lines)

    # ─── Cisco IOS show crypto ────────────────
    def _ios_show_crypto(self, cmd: str, c: str, state: DeviceState) -> str:
        crypto = getattr(state, 'ipsec_crypto', {})
        peers  = getattr(state, 'ipsec_peers',  {})

        # show crypto isakmp sa
        if re.match(r'^show\s+crypto\s+isakmp\s+sa', c):
            estab = {ip: p for ip, p in peers.items() if p.get('phase1') == 'MATURE'}
            if not estab:
                return 'IPv4 Crypto ISAKMP SA\ndst             src             state          conn-id status\n(none)'
            lines = ['IPv4 Crypto ISAKMP SA',
                     'dst             src             state          conn-id status']
            local_ip = next((i.get('ip','') for i in state.interfaces.values()
                             if i.get('ip') and i.get('ip') != '127.0.0.1'), '-')
            for peer_ip, p in estab.items():
                lines.append(f'{peer_ip:<16}{local_ip:<16}QM_IDLE        {random.randint(1000,9999)} ACTIVE')
            return '\n'.join(lines)

        # show crypto ipsec sa
        if re.match(r'^show\s+crypto\s+ipsec\s+sa', c):
            estab = {ip: p for ip, p in peers.items() if p.get('phase2') == 'MATURE'}
            if not estab:
                return 'There are no ipsec sas.'
            lines = []
            local_ip = next((i.get('ip','') for i in state.interfaces.values()
                             if i.get('ip') and i.get('ip') != '127.0.0.1'), '-')
            for peer_ip, p in estab.items():
                spi_in  = p.get('spi_in',  f'0x{random.randint(0,0xffffffff):08x}')
                spi_out = p.get('spi_out', f'0x{random.randint(0,0xffffffff):08x}')
                sa_rem  = p.get('sa_lifetime_remaining', 28800)
                lines += [
                    f'interface: GigabitEthernet0/0/0',
                    f'    Crypto map tag: CMAP, local addr {local_ip}',
                    f'',
                    f'   local  ident (addr/mask/prot/port): (0.0.0.0/0.0.0.0/0/0)',
                    f'   remote ident (addr/mask/prot/port): (0.0.0.0/0.0.0.0/0/0)',
                    f'   current_peer {peer_ip} port 500',
                    f'    PERMIT, flags={{origin_is_acl,}}',
                    f'   #pkts encaps: {random.randint(100,9999)}, #pkts encrypt: {random.randint(100,9999)}, #pkts digest: {random.randint(100,9999)}',
                    f'   #pkts decaps: {random.randint(100,9999)}, #pkts decrypt: {random.randint(100,9999)}, #pkts verify: {random.randint(100,9999)}',
                    f'   #send errors 0, #recv errors 0',
                    f'',
                    f'     local crypto endpt.: {local_ip}, remote crypto endpt.: {peer_ip}',
                    f'     path mtu 1500, ip mtu 1500',
                    f'',
                    f'    inbound esp sas:',
                    f'     spi: {spi_in}()',
                    f'      transform: esp-aes-256 esp-sha-hmac ,',
                    f'      in use settings ={{Tunnel, }}',
                    f'      sa timing: remaining key lifetime (k/sec): (4608000/{sa_rem})',
                    f'      Status: ACTIVE(ACTIVE)',
                    f'',
                    f'    outbound esp sas:',
                    f'     spi: {spi_out}()',
                    f'      transform: esp-aes-256 esp-sha-hmac ,',
                    f'      in use settings ={{Tunnel, }}',
                    f'      sa timing: remaining key lifetime (k/sec): (4608000/{sa_rem})',
                    f'      Status: ACTIVE(ACTIVE)',
                    f'',
                ]
            return '\n'.join(lines)

        # show crypto isakmp policy
        if re.match(r'^show\s+crypto\s+isakmp\s+policy', c):
            pols = crypto.get('isakmp_policies', {})
            if not pols:
                return 'Default IKE policy\n  authentication pre-share\n  encryption aes-256\n  hash sha256\n  Diffie-Hellman group 14\n  lifetime 86400 seconds'
            lines = []
            for num, pol in sorted(pols.items()):
                lines += [
                    f'IKE policy {num}:',
                    f'  authentication {pol.get("authentication","pre-share")}',
                    f'  encryption {pol.get("encryption","aes-256")}',
                    f'  hash {pol.get("hash","sha256")}',
                    f'  Diffie-Hellman group {pol.get("group",14)}',
                    f'  lifetime {pol.get("lifetime",86400)} seconds, No volume limit',
                    '',
                ]
            return '\n'.join(lines)

        # show crypto map
        if re.match(r'^show\s+crypto\s+map', c):
            cmaps = crypto.get('crypto_maps', {})
            if not cmaps:
                return '% Crypto map not configured.'
            lines = []
            for mapname, seqs in cmaps.items():
                for seq, entry in sorted(seqs.items()):
                    lines += [
                        f'Crypto Map "{mapname}" {seq} ipsec-isakmp',
                        f'  Peer = {entry.get("peer","-")}',
                        f'  Extended IP access list {entry.get("acl","-")}',
                        f'  Security association lifetime: 4608000 kilobytes/28800 seconds',
                        f'  Transform sets={{ {entry.get("transform_set","-")} }}',
                        f'  Interfaces using crypto map {mapname}:',
                        '',
                    ]
            return '\n'.join(lines)

        return '% Incomplete crypto show command.'

    def _sir_show_ipsec_policy(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IPsec policy configured.'
        lines = [
            '  No.  Direction  Local-IP        Remote-IP       Protocol  Encrypt    Hash    PFS',
            '  ---  ---------  --------------  --------------  --------  ---------  ------  ---',
        ]
        for tid, t in sorted(tunnels.items()):
            local  = t.get('local_ip',  '-')
            remote = t.get('remote_ip', '-')
            proto  = t.get('protocol', 'esp').upper()
            enc    = t.get('encryption', 'aes256')
            hsh    = t.get('hash', 'sha256')
            pfs    = t.get('pfs', 'off')
            lines.append(
                f'  {tid:<5}{"both":<11}{local:<16}{remote:<16}{proto:<10}{enc:<11}{hsh:<8}{pfs}'
            )
        return '\n'.join(lines)

    def _sir_show_ipsec_statistics(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IPsec SA established.'
        lines = [
            '  IPsec Statistics',
            '  ----------------',
        ]
        for tid, t in sorted(tunnels.items()):
            remote = t.get('remote_ip', '-')
            name   = t.get('name', f'tunnel{tid}')
            if t.get('status') == 'established':
                pkts_in  = random.randint(1000, 99999)
                pkts_out = random.randint(1000, 99999)
                bytes_in  = pkts_in  * random.randint(500, 1400)
                bytes_out = pkts_out * random.randint(500, 1400)
                lines += [
                    f'',
                    f'  Remote {remote}  ({name})',
                    f'    Inbound  ESP:  {pkts_in} packets  {bytes_in} bytes  0 errors',
                    f'    Outbound ESP:  {pkts_out} packets  {bytes_out} bytes  0 errors',
                    f'    Replay check:  0 failures',
                    f'    SA rekeys:     {random.randint(0, 5)}',
                ]
            else:
                lines += [f'', f'  Remote {remote}  ({name})', f'    SA not yet established.']
        return '\n'.join(lines)

    def _sir_show_ike_policy(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IKE policy configured.'
        lines = [
            '  No.  Peer             Mode        DH   Encrypt    Hash    Auth         Lifetime',
            '  ---  ---------------  ----------  ---  ---------  ------  -----------  --------',
        ]
        for tid, t in sorted(tunnels.items()):
            remote   = t.get('remote_ip', '-')
            mode     = t.get('ike_mode', 'main')
            dh       = str(t.get('dh_group', 14))
            enc      = t.get('encryption', 'aes256')
            hsh      = t.get('hash', 'sha256')
            lifetime = str(t.get('ike_lifetime', 86400))
            lines.append(
                f'  {tid:<5}{remote:<17}{mode:<12}{dh:<5}{enc:<11}{hsh:<8}{"preshared":<13}{lifetime}'
            )
        return '\n'.join(lines)

    def _sir_show_ike_statistics(self, state: DeviceState) -> str:
        tunnels = getattr(state, 'ipsec_tunnels', {})
        if not tunnels:
            return '  No IKE SA established.'
        lines = [
            '  IKE Statistics',
            '  --------------',
        ]
        for tid, t in sorted(tunnels.items()):
            remote = t.get('remote_ip', '-')
            name   = t.get('name', f'tunnel{tid}')
            if t.get('status') == 'established':
                lines += [
                    f'',
                    f'  Peer {remote}  ({name})',
                    f'    Phase1 negotiations:  {random.randint(1,5)} success  0 failure',
                    f'    Phase2 negotiations:  {random.randint(1,5)} success  0 failure',
                    f'    DPD probes sent:      {random.randint(0,20)}',
                    f'    DPD probes received:  {random.randint(0,20)}',
                    f'    Rekeys:               {random.randint(0,3)}',
                ]
            else:
                lines += [f'', f'  Peer {remote}  ({name})', f'    SA not yet established.']
        return '\n'.join(lines)

    # ─── show running-config ──────────────────
    def _show_running_config(self, state):
        if state.device_type in ("sir", "srs"):
            lines = [
                f"hostname {state.hostname}",
                "!",
                "password admin set ****",
                "!",
                "lan 0 ip address 192.168.1.1/24 1",
                "lan 0 ip dhcp service server",
                "lan 0 ip dhcp pool address 192.168.1.100 192.168.1.200",
                "lan 0 ip dhcp server dns 8.8.8.8 8.8.4.4",
                "lan 0 ip napt use use",
                "lan 0 vrrp use on",
                f"lan 0 vrrp group 0 id 10 {state.vrrp['priority']} {state.vrrp['vip']}",
                "!",
                "wan 1 use use",
                "wan 1 bind ether 0",
                "wan 1 ppp auth send user@isp.ne.jp ****",
                "wan 1 ppp ipcp ipaddress on",
                "wan 1 ip route default metric 100",
                "!",
                "ip dns service use",
                "ip dns server 8.8.8.8 8.8.4.4",
                "ip rip use use",
                "ip rip network 192.168.1.0/24",
                "ip rip version 2",
                "!",
            ]
            # IPsec/IKE設定を動的に出力
            tunnels = getattr(state, 'ipsec_tunnels', {})
            ipsec_enabled = getattr(state, 'ipsec_enabled', False)
            ike_enabled   = getattr(state, 'ike_enabled', False)
            if tunnels:
                for tid, t in sorted(tunnels.items()):
                    rn, ap = tid if isinstance(tid, tuple) else (tid, 0)
                    prefix = f"remote {rn} ap {ap}"
                    if t.get('name'):
                        lines.append(f"{prefix} name {t['name']}")
                    if t.get('datalink'):
                        lines.append(f"{prefix} datalink type {t['datalink']}")
                    if t.get('ipsec_type'):
                        lines.append(f"{prefix} ipsec type {t['ipsec_type']}")
                    if t.get('local_ip'):
                        lines.append(f"{prefix} tunnel local {t['local_ip']}")
                    if t.get('remote_ip'):
                        lines.append(f"{prefix} tunnel remote {t['remote_ip']}")
                    if t.get('protocol'):
                        lines.append(f"{prefix} ipsec protocol {t['protocol']}")
                    enc = t.get('encryption', '')
                    hsh = t.get('hash', '')
                    if enc:
                        lines.append(f"{prefix} ipsec encrypt {enc}{' ' + hsh if hsh else ''}")
                    if t.get('pfs'):
                        lines.append(f"{prefix} ipsec pfs use {t['pfs']}")
                    if t.get('ike_mode'):
                        lines.append(f"{prefix} ipsec ike mode {t['ike_mode']}")
                    if t.get('dh_group'):
                        lines.append(f"{prefix} ipsec ike dh {t['dh_group']}")
                    if t.get('ike_lifetime'):
                        lines.append(f"{prefix} ipsec ike lifetime {t['ike_lifetime']}")
                    if t.get('sa_lifetime'):
                        lines.append(f"{prefix} ipsec sa lifetime {t['sa_lifetime']}")
                    if t.get('preshared'):
                        lines.append(f"{prefix} ipsec ike preshared-key ****")
                    lines.append("!")
                if ipsec_enabled:
                    lines.append("ipsec use on")
                if ike_enabled:
                    lines.append("ike use on")
                lines.append("!")
            lines += [
                "timezone +9",
                "ntp use use",
                "ntp server 192.168.1.1",
            ]
            return "\n".join(lines)
        elif state.device_type in ("cisco", "catalyst"):
            lines = [f"hostname {state.hostname}", "!", "version 17.9", "!"]
            # インタフェース
            for ifname, ifdata in state.interfaces.items():
                ip = ifdata.get('ip', '')
                prefix = ifdata.get('prefix', 0)
                status = ifdata.get('status', 'down')
                if not ip and status == 'down':
                    continue
                mask = _prefix_to_mask(prefix) if prefix else '255.255.255.0'
                lines.append(f"interface {ifname}")
                if ip:
                    lines.append(f" ip address {ip} {mask}")
                if status == 'up':
                    lines.append(" no shutdown")
                else:
                    lines.append(" shutdown")
                lines.append("!")
            lines.append("ip routing")
            lines.append("!")
            # IPsec/IKE crypto
            crypto = getattr(state, 'ipsec_crypto', {})
            if crypto:
                for num, pol in sorted(crypto.get('isakmp_policies', {}).items()):
                    lines.append(f"crypto isakmp policy {num}")
                    lines.append(f" authentication {pol.get('authentication','pre-share')}")
                    enc = pol.get('encryption', 'aes 256')
                    lines.append(f" encryption {enc}")
                    lines.append(f" hash {pol.get('hash','sha256')}")
                    lines.append(f" group {pol.get('group',14)}")
                    lines.append(f" lifetime {pol.get('lifetime',86400)}")
                    lines.append("!")
                for peer, key in crypto.get('isakmp_keys', {}).items():
                    lines.append(f"crypto isakmp key **** address {peer}")
                lines.append("!")
                for ts_name, ts in crypto.get('transform_sets', {}).items():
                    transforms = ' '.join(ts.get('transforms', []))
                    lines.append(f"crypto ipsec transform-set {ts_name} {transforms}")
                    lines.append(" mode tunnel")
                lines.append("!")
                for mapname, seqs in crypto.get('crypto_maps', {}).items():
                    for seq, entry in sorted(seqs.items()):
                        if entry.get('acl'):
                            lines.append(f"crypto map {mapname} {seq} ipsec-isakmp")
                            lines.append(f" match address {entry['acl']}")
                            if entry.get('peer'):
                                lines.append(f" set peer {entry['peer']}")
                            if entry.get('transform_set'):
                                lines.append(f" set transform-set {entry['transform_set']}")
                            lines.append("!")
                cm_if = crypto.get('crypto_map_interface', {})
                if cm_if:
                    lines.append(f"crypto map {cm_if['name']} interface {cm_if['interface']}")
                ike_if = crypto.get('isakmp_enabled', '')
                if ike_if:
                    lines.append(f"crypto isakmp enable {ike_if}")
                lines.append("!")
            lines.append("end")
            return "\n".join(lines)
        else:
            lines = [f"hostname {state.hostname}", "!", "version 17.9", "!"]
            for vid, v in state.vlans.items():
                if vid != 1:
                    lines += [f"vlan {vid}", f" name {v['name']}", "!"]
            lines += ["interface GigabitEthernet1/0/1",
                      " switchport mode access", " switchport access vlan 10",
                      " spanning-tree portfast", " no shutdown", "!",
                      "interface Vlan10",
                      " ip address 192.168.10.1 255.255.255.0", " no shutdown", "!",
                      "ip routing", "!",
                      "end"]
            return "\n".join(lines)

    # ─── show logging ─────────────────────────
    def _show_logging(self, state):
        now = datetime.now()
        return f"""{now.strftime('%b %d %H:%M:%S')}: %SYS-5-CONFIG_I: Configured from console
{now.strftime('%b %d %H:%M:%S')}: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to up
{now.strftime('%b %d %H:%M:%S')}: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/1, changed state to up
{now.strftime('%b %d %H:%M:%S')}: %SYS-5-RESTART: System restarted"""

    # ─── 設定コマンド ────────────────────────
    def _cmd_config(self, cmd, state):
        c = cmd.lower().strip()

        # hostname / sysname
        m = re.match(r'^(?:hostname|sysname)\s+(\S+)', cmd, re.I)
        if m:
            state.hostname = m.group(1)
            return ""

        # interface ip address
        if re.match(r'^ip\s+address\s+[\d.]+\s+[\d.]+', c):
            m = re.match(r'^ip\s+address\s+([\d.]+)\s+([\d.]+)', c)
            if m and state.current_if:
                state.interfaces.setdefault(state.current_if, {})["ip"] = m.group(1)
                return ""

        # switchport
        if re.match(r'^switchport\s+mode\s+(access|trunk)', c):
            return ""
        if re.match(r'^switchport\s+access\s+vlan\s+\d+', c):
            m = re.match(r'^switchport\s+access\s+vlan\s+(\d+)', c)
            if m and state.current_if:
                state.interfaces.setdefault(state.current_if, {})["vlan"] = m.group(1)
            return ""

        # no shutdown
        if c in ("no shutdown", "no shut"):
            if state.current_if:
                state.interfaces.setdefault(state.current_if, {})["status"] = "up"
            return ""

        # shutdown
        if c == "shutdown":
            if state.current_if:
                state.interfaces.setdefault(state.current_if, {})["status"] = "down"
            return ""

        # vlan name
        if re.match(r'^name\s+\S+', c) and state.mode == "config-vlan":
            m = re.match(r'^name\s+(\S+)', cmd, re.I)
            return ""

        # spanning-tree
        if re.match(r'^spanning-tree\s+mode\s+', c):
            m = re.match(r'^spanning-tree\s+mode\s+(\S+)', c)
            if m:
                state.stp["mode"] = m.group(1)
            return ""

        # Si-R 固有
        if re.match(r'^lan\s+\d+\s+', c):
            # lan X ip address で IPを反映
            m_lan_ip = re.match(r'^lan\s+(\d+)\s+ip\s+address\s+([\d.]+)/(\d+)', c)
            if m_lan_ip and state.device_type in ('sir', 'srs'):
                iface = f'lan{m_lan_ip.group(1)}'
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['ip'] = m_lan_ip.group(2)
                state.interfaces[iface]['prefix'] = int(m_lan_ip.group(3))
            return ""
        if re.match(r'^wan\s+\d+\s+', c):
            return ""
        if re.match(r'^ip\s+rip\s+', c):
            return ""
        if re.match(r'^ip\s+dns\s+', c):
            return ""

        # ── Si-R IPsec VPN コマンド ──
        # remote 1 ap 0 name <name>  (名前は元のケースを保持)
        m_remote_name = re.match(r'^remote\s+(\d+)\s+ap\s+(\d+)\s+name\s+(\S+)', c)
        if m_remote_name and state.device_type in ('sir', 'srs'):
            tid = int(m_remote_name.group(1))
            m_orig = re.match(r'^remote\s+\d+\s+ap\s+\d+\s+name\s+(\S+)', cmd, re.I)
            state.ipsec_tunnels.setdefault(tid, {})['name'] = m_orig.group(1) if m_orig else m_remote_name.group(3)
            return ""

        # remote 1 ap 0 datalink type ipsec
        m_remote_dl = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+datalink\s+type\s+(\S+)', c)
        if m_remote_dl and state.device_type in ('sir', 'srs'):
            tid = int(m_remote_dl.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['datalink'] = m_remote_dl.group(2)
            return ""

        # remote 1 ap 0 ipsec type ike
        m_remote_ipsec_type = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+type\s+(\S+)', c)
        if m_remote_ipsec_type and state.device_type in ('sir', 'srs'):
            tid = int(m_remote_ipsec_type.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['ipsec_type'] = m_remote_ipsec_type.group(2)
            return ""

        # remote 1 ap 0 tunnel local <ip>
        m_tunnel_local = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+tunnel\s+local\s+([\d.]+)', c)
        if m_tunnel_local and state.device_type in ('sir', 'srs'):
            tid = int(m_tunnel_local.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['local_ip'] = m_tunnel_local.group(2)
            state.ipsec_tunnels[tid].setdefault('status', 'wait')
            return ""

        # remote 1 ap 0 tunnel remote <ip>
        m_tunnel_remote = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+tunnel\s+remote\s+([\d.]+)', c)
        if m_tunnel_remote and state.device_type in ('sir', 'srs'):
            tid = int(m_tunnel_remote.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['remote_ip'] = m_tunnel_remote.group(2)
            return ""

        # remote 1 ap 0 ipsec ike preshared-key <key>
        m_psk = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+ike\s+preshared-key\s+(\S+)', c)
        if m_psk and state.device_type in ('sir', 'srs'):
            tid = int(m_psk.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['preshared'] = m_psk.group(2)
            state.ipsec_tunnels[tid]['status'] = 'established'  # 鍵設定でNEG開始を模擬
            state.ipsec_tunnels[tid]['phase1'] = 'MATURE'
            state.ipsec_tunnels[tid]['phase2'] = 'MATURE'
            return ""

        # remote 1 ap 0 ipsec protocol esp
        m_proto = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+protocol\s+(\S+)', c)
        if m_proto and state.device_type in ('sir', 'srs'):
            tid = int(m_proto.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['protocol'] = m_proto.group(2)
            return ""

        # remote 1 ap 0 ipsec encrypt aes256 sha256
        m_enc = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+encrypt\s+(\S+)(?:\s+(\S+))?', c)
        if m_enc and state.device_type in ('sir', 'srs'):
            tid = int(m_enc.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['encryption'] = m_enc.group(2)
            if m_enc.group(3):
                state.ipsec_tunnels[tid]['hash'] = m_enc.group(3)
            return ""

        # remote 1 ap 0 ipsec pfs use on
        m_pfs = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+pfs\s+(use\s+on|use\s+off|on|off)', c)
        if m_pfs and state.device_type in ('sir', 'srs'):
            tid = int(m_pfs.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['pfs'] = 'on' if 'on' in m_pfs.group(2) else 'off'
            return ""

        # remote 1 ap 0 ipsec ike mode main|aggressive
        m_mode = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+ike\s+mode\s+(\S+)', c)
        if m_mode and state.device_type in ('sir', 'srs'):
            tid = int(m_mode.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['ike_mode'] = m_mode.group(2)
            return ""

        # remote 1 ap 0 ipsec ike dh 14|2|5
        m_dh = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+ike\s+dh\s+(\d+)', c)
        if m_dh and state.device_type in ('sir', 'srs'):
            tid = int(m_dh.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['dh_group'] = int(m_dh.group(2))
            return ""

        # remote 1 ap 0 ipsec ike lifetime <sec>
        m_lt = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+ike\s+lifetime\s+(\d+)', c)
        if m_lt and state.device_type in ('sir', 'srs'):
            tid = int(m_lt.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['ike_lifetime'] = int(m_lt.group(2))
            return ""

        # remote 1 ap 0 ipsec sa lifetime <sec>
        m_sa_lt = re.match(r'^remote\s+(\d+)\s+ap\s+\d+\s+ipsec\s+sa\s+lifetime\s+(\d+)', c)
        if m_sa_lt and state.device_type in ('sir', 'srs'):
            tid = int(m_sa_lt.group(1))
            state.ipsec_tunnels.setdefault(tid, {})['sa_lifetime'] = int(m_sa_lt.group(2))
            return ""

        # remote 1 ip route <dst>/<prefix> <gw> metric <n>
        m_remote_route = re.match(r'^remote\s+(\d+)\s+ip\s+route\s+([\d.]+)/(\d+)', c)
        if m_remote_route and state.device_type in ('sir', 'srs'):
            return ""  # ルーティング設定は受け入れ

        # ipsec use on (グローバル有効化)
        if re.match(r'^ipsec\s+use\s+on', c) and state.device_type in ('sir', 'srs'):
            state.ipsec_enabled = True
            return ""

        # ike use on
        if re.match(r'^ike\s+use\s+on', c) and state.device_type in ('sir', 'srs'):
            state.ike_enabled = True
            return ""

        # ── Cisco IOS IPsec/IKE コマンド (cisco / catalyst) ──
        if state.device_type in ('cisco', 'catalyst'):
            # crypto isakmp policy <n>
            m_isakmp = re.match(r'^crypto\s+isakmp\s+policy\s+(\d+)', c)
            if m_isakmp:
                state._ike_policy_num = int(m_isakmp.group(1))
                state.ipsec_crypto.setdefault('isakmp_policies', {})[state._ike_policy_num] = {
                    'encryption': 'aes-256', 'hash': 'sha256',
                    'authentication': 'pre-share', 'group': 14, 'lifetime': 86400,
                }
                return ''

            if hasattr(state, '_ike_policy_num'):
                num = state._ike_policy_num
                pol = state.ipsec_crypto.get('isakmp_policies', {}).get(num, {})
                m_enc2 = re.match(r'^encryption\s+(.+)', c)
                if m_enc2: pol['encryption'] = m_enc2.group(1).strip(); return ''
                m_hash2 = re.match(r'^hash\s+(\S+)', c)
                if m_hash2: pol['hash'] = m_hash2.group(1); return ''
                m_auth2 = re.match(r'^authentication\s+(\S+)', c)
                if m_auth2: pol['authentication'] = m_auth2.group(1); return ''
                m_grp2 = re.match(r'^group\s+(\d+)', c)
                if m_grp2: pol['group'] = int(m_grp2.group(1)); return ''
                m_lt2 = re.match(r'^lifetime\s+(\d+)', c)
                if m_lt2: pol['lifetime'] = int(m_lt2.group(1)); return ''

            # crypto ipsec transform-set <name> <transforms>
            m_ts = re.match(r'^crypto\s+ipsec\s+transform-set\s+(\S+)\s+(.+)', c)
            if m_ts:
                ts_name = m_ts.group(1)
                state.ipsec_crypto.setdefault('transform_sets', {})[ts_name] = {
                    'transforms': m_ts.group(2).split()
                }
                return ''

            # crypto map <name> <seq> match address <acl>
            m_cmap_match = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+match\s+address\s+(\S+)', c)
            if m_cmap_match:
                mapname, seq, acl = m_cmap_match.group(1), int(m_cmap_match.group(2)), m_cmap_match.group(3)
                state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {})[seq] = {'acl': acl}
                return ''

            # crypto map <name> <seq> set peer <ip>
            m_cmap_peer = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+set\s+peer\s+([\d.]+)', c)
            if m_cmap_peer:
                mapname, seq, peer = m_cmap_peer.group(1), int(m_cmap_peer.group(2)), m_cmap_peer.group(3)
                state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {}).setdefault(seq, {})['peer'] = peer
                state.ipsec_peers.setdefault(peer, {})['remote_ip'] = peer
                return ''

            # crypto map <name> <seq> set transform-set <ts>
            m_cmap_ts = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+set\s+transform-set\s+(\S+)', c)
            if m_cmap_ts:
                mapname, seq, ts = m_cmap_ts.group(1), int(m_cmap_ts.group(2)), m_cmap_ts.group(3)
                state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {}).setdefault(seq, {})['transform_set'] = ts
                return ''

            # crypto map <name> interface <if>
            m_cmap_if = re.match(r'^crypto\s+map\s+(\S+)\s+interface\s+(\S+)', c)
            if m_cmap_if:
                state.ipsec_crypto['crypto_map_interface'] = {
                    'name': m_cmap_if.group(1), 'interface': m_cmap_if.group(2)}
                return ''

            # crypto isakmp enable <if>
            m_ike_enable = re.match(r'^crypto\s+isakmp\s+enable\s+(\S+)', c)
            if m_ike_enable:
                state.ipsec_crypto['isakmp_enabled'] = m_ike_enable.group(1)
                return ''

            # crypto isakmp key <key> address <peer>  (already handled above but fallback)
            m_ios_psk2 = re.match(r'^crypto\s+isakmp\s+key\s+(\S+)\s+address\s+([\d.]+)', c)
            if m_ios_psk2:
                key, peer = m_ios_psk2.group(1), m_ios_psk2.group(2)
                state.ipsec_crypto.setdefault('isakmp_keys', {})[peer] = key
                return ''

        # network
        if re.match(r'^network\s+', c):
            return ""

        # neighbor
        if re.match(r'^neighbor\s+', c):
            return ""

        # 不明な設定コマンドは静かに受け付け
        return ""

    # ─── ping ─────────────────────────────────
    def _cmd_ping(self, cmd, state):
        m = re.match(r'^ping\s+([\d.]+)', cmd, re.I)
        if not m:
            return "% Incomplete command."
        dest = m.group(1)
        rtt = [round(random.uniform(0.3, 2.5), 3) for _ in range(5)]
        lines = [f"PING {dest} ({dest}): 56 data bytes"]
        for i, r in enumerate(rtt):
            lines.append(f"64 bytes from {dest}: icmp_seq={i} ttl=64 time={r} ms")
        lines += ["",
            f"--- {dest} ping statistics ---",
            f"5 packets transmitted, 5 packets received, 0% packet loss",
            f"round-trip min/avg/max = {min(rtt)}/{round(sum(rtt)/len(rtt),3)}/{max(rtt)} ms"]
        return "\n".join(lines)

    # ─── トンネルヘッダー付加エミュレーション ──────────────────
    def _show_tunnel_header(self, cmd, state):
        """
        IPsec トンネルモードのパケットヘッダー付加をシミュレート表示。
        'show tunnel-header <src-ip> <dst-ip>' コマンドに対応。
        Si-R / Cisco IOS 共通。
        """
        m = re.match(r'^show\s+tunnel[-\s]?header\s+([\d.]+)\s+([\d.]+)', cmd, re.I)
        if not m:
            return ("Usage: show tunnel-header <src-ip> <dst-ip>\n"
                    "  Example: show tunnel-header 192.168.1.10 10.10.0.20")

        orig_src = m.group(1)
        orig_dst = m.group(2)

        # アクティブなトンネルを探す
        tunnel_local  = None
        tunnel_remote = None
        spi_out       = None
        enc_alg       = "AES-256"
        protocol      = "ESP"

        if state.device_type in ('sir', 'srs'):
            for tid, t in getattr(state, 'ipsec_tunnels', {}).items():
                if t.get('status') == 'established':
                    tunnel_local  = t.get('local_ip',  '203.0.113.1')
                    tunnel_remote = t.get('remote_ip', '203.0.113.2')
                    spi_out       = t.get('spi_out',   '0x00000001')
                    enc_alg = t.get('encryption', 'aes256').upper().replace('AES256', 'AES-256')
                    break
        elif state.device_type in ('cisco', 'catalyst'):
            crypto = getattr(state, 'ipsec_crypto', {})
            peers  = getattr(state, 'ipsec_peers',  {})
            for peer_ip, p in peers.items():
                if p.get('status') == 'established':
                    tunnel_remote = peer_ip
                    spi_out = p.get('spi_out', '0x00000001')
                    # WAN IP を探す
                    for _, ifd in state.interfaces.items():
                        if ifd.get('ip') and ifd['ip'] != '127.0.0.1':
                            tunnel_local = ifd['ip']
                            break
                    # transform-set から暗号アルゴリズム取得
                    ts_map = crypto.get('transform_sets', {})
                    ts = next(iter(ts_map.values()), {}) if ts_map else {}
                    tfs = ts.get('transforms', [])
                    if 'esp-aes-256' in tfs or 'esp-aes256' in tfs:
                        enc_alg = 'AES-256'
                    elif 'esp-3des' in tfs:
                        enc_alg = '3DES'
                    else:
                        enc_alg = 'AES-128'
                    break

        if not tunnel_local or not tunnel_remote:
            return ("% No established IPsec tunnel found.\n"
                    "  Run 'show ipsec sa' or 'show crypto ipsec sa' to check tunnel status.")

        orig_size   = 60   # ICMP echo (IP 20 + ICMP 8 + data 32)
        esp_hdr     = 8    # SPI(4) + Seq(4)
        esp_trl     = 22   # Pad + PadLen + NextHdr + ICV(16 for AES-256)
        tunnel_hdr  = 20   # outer IP header
        total_size  = orig_size + esp_hdr + esp_trl + tunnel_hdr

        lines = [
            "",
            "IPsec Tunnel Mode - Packet Header Encapsulation",
            "=" * 56,
            "",
            "Original (Inner) Packet:",
            f"  +---------------------------+---------------------------+",
            f"  | IP Header (20 bytes)      | Payload                   |",
            f"  | Src: {orig_src:<18} |                           |",
            f"  | Dst: {orig_dst:<18} | ICMP / TCP / UDP          |",
            f"  | Proto: ICMP               | ({orig_size - 20} bytes)                 |",
            f"  +---------------------------+---------------------------+",
            f"  Total: {orig_size} bytes",
            "",
            "After IPsec ESP Encapsulation (Tunnel Mode):",
            f"  +---------------+-------+---------------------------+-------+",
            f"  | Outer IP Hdr  | ESP   | Encrypted Payload         | ESP   |",
            f"  | (20 bytes)    | Header| (Inner IP + Data)         | Trail |",
            f"  | Src: {tunnel_local:<9} | ({esp_hdr}B)  | Alg: {enc_alg:<20} | ({esp_trl}B) |",
            f"  | Dst: {tunnel_remote:<9} | SPI:  | Original {orig_size} bytes         |       |",
            f"  | Proto: ESP    | {spi_out} |                           |       |",
            f"  +---------------+-------+---------------------------+-------+",
            f"  Total: {total_size} bytes  (overhead: +{total_size - orig_size} bytes)",
            "",
            "Header Details:",
            f"  [Outer IP Header]",
            f"    Version  : 4",
            f"    IHL      : 20 bytes",
            f"    Protocol : 50 (ESP)",
            f"    Src      : {tunnel_local}",
            f"    Dst      : {tunnel_remote}",
            "",
            f"  [ESP Header]",
            f"    SPI      : {spi_out}",
            f"    Seq No   : 0x00000001",
            "",
            f"  [Encrypted Payload]",
            f"    Algorithm: {enc_alg} (CBC)",
            f"    Content  : [Original IP Header + {protocol} Data] (encrypted)",
            "",
            f"  [ESP Trailer]",
            f"    Padding  : variable",
            f"    Next Hdr : 4 (IPv4)",
            f"    ICV      : 16 bytes (HMAC-SHA256)",
            "",
            f"Tunnel: {tunnel_local} --> {tunnel_remote}",
            f"Status: ACTIVE  SPI(out)={spi_out}  Encr={enc_alg}",
        ]
        return "\n".join(lines)

    # ─── traceroute ───────────────────────────
    def _cmd_traceroute(self, cmd, state):
        m = re.match(r'^traceroute\s+([\d.]+)', cmd, re.I)
        if not m:
            return "% Incomplete command."
        dest = m.group(1)
        hops = [("192.168.1.254", 1), ("10.0.0.1", 2), (dest, 3)]
        lines = [f"traceroute to {dest} ({dest}), 30 hops max, 40 byte packets"]
        for hop, (ip, n) in enumerate(hops, 1):
            rtts = [round(random.uniform(1, 15), 3) for _ in range(3)]
            lines.append(f" {hop}  {ip} ({ip})  {rtts[0]} ms  {rtts[1]} ms  {rtts[2]} ms")
        return "\n".join(lines)

    # ─── Cisco ASA コマンドエンジン（シングルコンテキスト 9.x準拠）────
    def _asa_process(self, cmd: str, c: str, state: DeviceState) -> str:
        """Cisco ASA シングルコンテキスト CLIコマンドを処理する"""

        # ── モード遷移 ──
        if c in ('exit', 'end', 'quit'):
            if state.mode == 'config-if':
                state.mode = 'config'
                return ''
            elif state.mode in ('config-router', 'config-policy-map',
                                'config-pmap-c', 'config-class-map'):
                state.mode = 'config'
                return ''
            elif state.mode == 'config':
                state.mode = 'exec'
                return ''
            return ''

        if c in ('enable', 'en'):
            return 'Password: \nType help or \'?\' for a list of available commands.'

        if c in ('configure terminal', 'conf t', 'conf terminal', 'config t'):
            state.mode = 'config'
            return f'\n{state.hostname}(config)# '

        if state.mode == 'exec' and c not in ('configure terminal', 'conf t',
                                               'enable', 'show version',
                                               'show interface', 'show running',
                                               'show route', 'show conn',
                                               'show xlate', 'show access-list',
                                               'show crypto', 'show nat',
                                               'ping', 'traceroute',
                                               'show logging', 'write memory',
                                               'write', 'wr', 'show version'):
            pass  # execモードではshow/ping系のみ許可（他はconfig必要）

        # ── show コマンド ──
        if c == 'show version' or c == 'sh ver':
            return self._asa_show_version(state)

        if re.match(r'^show\s+interface', c):
            m = re.match(r'^show\s+interface\s+(\S+)', c)
            target = m.group(1) if m else None
            return self._asa_show_interface(state, target)

        if re.match(r'^show\s+(ip\s+)?route', c):
            return self._asa_show_route(state)

        if re.match(r'^show\s+running-config|^show\s+run|^sh\s+run', c):
            return self._asa_show_running(state)

        if re.match(r'^show\s+access-list', c):
            return self._asa_show_access_list(state)

        if re.match(r'^show\s+conn', c):
            return self._asa_show_conn(state)

        if re.match(r'^show\s+xlate', c):
            return self._asa_show_xlate(state)

        if re.match(r'^show\s+nat', c):
            return self._asa_show_nat(state)

        if re.match(r'^show\s+crypto\s+isakmp\s+sa', c):
            return self._asa_show_crypto_isakmp_sa(state)

        if re.match(r'^show\s+crypto\s+ipsec\s+sa', c):
            return self._asa_show_crypto_ipsec_sa(state)

        if re.match(r'^show\s+service-policy', c):
            return self._asa_show_service_policy(state)

        if re.match(r'^show\s+logging', c):
            return ('Syslog logging: enabled\n'
                    f'    Console logging: level {state.logging_level}\n'
                    '    Trap logging: level informational, facility 20\n'
                    + (''.join(f'    Logging to {s["host"]} port {s["port"]}\n'
                               for s in state.syslog_servers)))

        # ── インタフェース設定 ──
        m_if = re.match(r'^interface\s+(\S+)', c)
        if m_if and state.mode == 'config':
            state.current_if = m_if.group(1)
            state.mode = 'config-if'
            return ''

        if state.mode == 'config-if':
            iface = state.current_if or ''
            iinfo = state.interfaces.get(iface, {})

            m_ip = re.match(r'^ip\s+address\s+([\d.]+)\s+([\d.]+)', c)
            if m_ip:
                ip = m_ip.group(1)
                prefix = sum(bin(int(o)).count('1') for o in m_ip.group(2).split('.'))
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['ip'] = ip
                state.interfaces[iface]['prefix'] = prefix
                return ''

            m_nameif = re.match(r'^nameif\s+(\S+)', c)
            if m_nameif:
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['nameif'] = m_nameif.group(1)
                return ''

            m_sec = re.match(r'^security-level\s+(\d+)', c)
            if m_sec:
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['security_level'] = int(m_sec.group(1))
                return ''

            if c in ('no shutdown', 'no shut'):
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['status'] = 'up'
                return ''
            if c == 'shutdown':
                state.interfaces.setdefault(iface, {})
                state.interfaces[iface]['status'] = 'down'
                return ''

        # ── ACL設定 ──
        # access-list OUTSIDE_IN extended permit tcp any host 192.168.1.10 eq 80
        m_acl = re.match(
            r'^access-list\s+(\S+)\s+(extended|standard)\s+(permit|deny)\s+'
            r'(ip|tcp|udp|icmp|ospf|any)\s+(.+)', orig := cmd.strip(), re.I)
        if m_acl and state.mode == 'config':
            name   = m_acl.group(1)
            action = m_acl.group(3).lower()
            proto  = m_acl.group(4).lower()
            rest   = m_acl.group(5)
            entry = {'action': action, 'proto': proto, 'raw': rest,
                     'line': len(state.acls.get(name, [])) + 1}
            state.acls.setdefault(name, []).append(entry)
            return ''

        # access-list NAME remark TEXT
        m_remark = re.match(r'^access-list\s+(\S+)\s+remark\s+(.+)', cmd.strip(), re.I)
        if m_remark and state.mode == 'config':
            name = m_remark.group(1)
            state.acls.setdefault(name, []).append(
                {'action': 'remark', 'proto': '', 'raw': m_remark.group(2), 'line': 0})
            return ''

        # access-group NAME in|out interface IFACE
        m_ag = re.match(r'^access-group\s+(\S+)\s+(in|out)\s+interface\s+(\S+)', c)
        if m_ag and state.mode == 'config':
            name, direction, iface = m_ag.group(1), m_ag.group(2), m_ag.group(3)
            for ifname, iinfo in state.interfaces.items():
                if iinfo.get('nameif', '').lower() == iface.lower() or ifname.lower() == iface.lower():
                    state.interfaces[ifname]['acl_' + direction] = name
            return ''

        # ── NAT設定 ──
        # object network OBJ_INSIDE
        m_obj = re.match(r'^object\s+network\s+(\S+)', c)
        if m_obj and state.mode == 'config':
            state._current_obj = m_obj.group(1)
            state.mode = 'config'
            return ''

        # nat (inside,outside) dynamic interface
        m_nat = re.match(r'^nat\s+\((\S+),(\S+)\)\s+(dynamic|static)\s+(.+)', c)
        if m_nat:
            real_iface   = m_nat.group(1)
            mapped_iface = m_nat.group(2)
            nat_type     = m_nat.group(3)
            mapped       = m_nat.group(4).strip()
            state.nat_rules.append({
                'type': nat_type, 'real': real_iface,
                'mapped': mapped_iface, 'mapped_addr': mapped,
            })
            return ''

        # ── ルーティング ──
        m_route = re.match(r'^route\s+(\S+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\d+))?', c)
        if m_route and state.mode == 'config':
            iface    = m_route.group(1)
            network  = m_route.group(2)
            mask     = m_route.group(3)
            nexthop  = m_route.group(4)
            metric   = int(m_route.group(5)) if m_route.group(5) else 1
            state.routes.append({
                'iface': iface, 'network': network, 'mask': mask,
                'nexthop': nexthop, 'metric': metric,
            })
            return ''

        # ── ステートフルインスペクション（Modular Policy Framework）──
        # policy-map global_policy
        m_pmap = re.match(r'^policy-map\s+(\S+)', c)
        if m_pmap and state.mode == 'config':
            state._current_pmap = m_pmap.group(1)
            state.mode = 'config-policy-map'
            return ''

        if state.mode == 'config-policy-map':
            m_class = re.match(r'^class\s+(\S+)', c)
            if m_class:
                state._current_class = m_class.group(1)
                state.mode = 'config-pmap-c'
                return ''

        if state.mode == 'config-pmap-c':
            m_inspect = re.match(r'^inspect\s+(\S+)', c)
            if m_inspect:
                proto = m_inspect.group(1).lower()
                if proto not in state.inspect_protocols:
                    state.inspect_protocols.append(proto)
                return ''

        # class-map CMAP
        m_cmap = re.match(r'^class-map\s+(\S+)', c)
        if m_cmap and state.mode == 'config':
            state._current_cmap = m_cmap.group(1)
            state.mode = 'config-class-map'
            return ''

        # service-policy POLICY global|interface IFACE
        m_sp = re.match(r'^service-policy\s+(\S+)\s+(global|interface\s+\S+)', c)
        if m_sp and state.mode == 'config':
            state.service_policies.append({
                'policy': m_sp.group(1), 'scope': m_sp.group(2)})
            return ''

        # ── IPsec / IKE設定（Cisco IOS / ASA 共通） ──
        # crypto isakmp key <key> address <peer>  (IOS)
        m_ios_psk = re.match(r'^crypto\s+isakmp\s+key\s+(\S+)\s+address\s+([\d.]+)', c)
        if m_ios_psk and state.device_type in ('cisco', 'catalyst', 'srs'):
            key  = m_ios_psk.group(1)
            peer = m_ios_psk.group(2)
            state.ipsec_crypto.setdefault('isakmp_keys', {})[peer] = key
            return ''

        # crypto isakmp policy 10
        m_isakmp = re.match(r'^crypto\s+isakmp\s+policy\s+(\d+)', c)
        if m_isakmp and state.mode == 'config':
            state._ike_policy_num = int(m_isakmp.group(1))
            state.ipsec_crypto.setdefault('isakmp_policies', {})[state._ike_policy_num] = {
                'encryption': 'aes-256', 'hash': 'sha256',
                'authentication': 'pre-share', 'group': 14, 'lifetime': 86400,
            }
            return ''

        if hasattr(state, '_ike_policy_num') and state.mode == 'config':
            num = state._ike_policy_num
            pol = state.ipsec_crypto.get('isakmp_policies', {}).get(num, {})
            m_enc = re.match(r'^encryption\s+(.+)', c)
            if m_enc: pol['encryption'] = m_enc.group(1).strip(); return ''
            m_hash = re.match(r'^hash\s+(\S+)', c)
            if m_hash: pol['hash'] = m_hash.group(1); return ''
            m_auth = re.match(r'^authentication\s+(\S+)', c)
            if m_auth: pol['authentication'] = m_auth.group(1); return ''
            m_grp = re.match(r'^group\s+(\d+)', c)
            if m_grp: pol['group'] = int(m_grp.group(1)); return ''
            m_lt = re.match(r'^lifetime\s+(\d+)', c)
            if m_lt: pol['lifetime'] = int(m_lt.group(1)); return ''

        # crypto ipsec transform-set NAME esp-aes-256 esp-sha-hmac
        m_ts = re.match(r'^crypto\s+ipsec\s+transform-set\s+(\S+)\s+(.+)', c)
        if m_ts and state.mode == 'config':
            ts_name = m_ts.group(1)
            state.ipsec_crypto.setdefault('transform_sets', {})[ts_name] = {
                'transforms': m_ts.group(2).split()
            }
            return ''

        # tunnel-group X.X.X.X type ipsec-l2l
        m_tg = re.match(r'^tunnel-group\s+([\d.]+)\s+type\s+(\S+)', c)
        if m_tg and state.mode == 'config':
            peer = m_tg.group(1)
            state.ipsec_peers.setdefault(peer, {})['type'] = m_tg.group(2)
            state._current_tg = peer
            return ''

        # tunnel-group X.X.X.X ipsec-attributes
        m_tg_attr = re.match(r'^tunnel-group\s+([\d.]+)\s+ipsec-attributes', c)
        if m_tg_attr and state.mode == 'config':
            state._current_tg = m_tg_attr.group(1)
            state._tg_attr_mode = True
            return ''

        if getattr(state, '_tg_attr_mode', False):
            m_psk = re.match(r'^(?:ikev1\s+)?pre-shared-key\s+(\S+)', c)
            if m_psk:
                peer = getattr(state, '_current_tg', '')
                if peer:
                    state.ipsec_peers.setdefault(peer, {})['preshared'] = m_psk.group(1)
                return ''

        # crypto map OUTSIDE_MAP 10 match address ACL
        m_cmap_match = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+match\s+address\s+(\S+)', c)
        if m_cmap_match and state.mode == 'config':
            mapname = m_cmap_match.group(1)
            seq = int(m_cmap_match.group(2))
            acl = m_cmap_match.group(3)
            state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {})[seq] = {'acl': acl}
            return ''

        # crypto map OUTSIDE_MAP 10 set peer X.X.X.X
        m_cmap_peer = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+set\s+peer\s+([\d.]+)', c)
        if m_cmap_peer and state.mode == 'config':
            mapname, seq, peer = m_cmap_peer.group(1), int(m_cmap_peer.group(2)), m_cmap_peer.group(3)
            state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {}).setdefault(seq, {})['peer'] = peer
            return ''

        # crypto map OUTSIDE_MAP 10 set transform-set TS_NAME
        m_cmap_ts = re.match(r'^crypto\s+map\s+(\S+)\s+(\d+)\s+set\s+transform-set\s+(\S+)', c)
        if m_cmap_ts and state.mode == 'config':
            mapname, seq, ts = m_cmap_ts.group(1), int(m_cmap_ts.group(2)), m_cmap_ts.group(3)
            state.ipsec_crypto.setdefault('crypto_maps', {}).setdefault(mapname, {}).setdefault(seq, {})['transform_set'] = ts
            return ''

        # crypto map OUTSIDE_MAP interface outside
        m_cmap_if = re.match(r'^crypto\s+map\s+(\S+)\s+interface\s+(\S+)', c)
        if m_cmap_if and state.mode == 'config':
            state.ipsec_crypto['crypto_map_interface'] = {
                'name': m_cmap_if.group(1), 'interface': m_cmap_if.group(2)}
            return ''

        # crypto isakmp enable outside
        m_ike_enable = re.match(r'^crypto\s+isakmp\s+enable\s+(\S+)', c)
        if m_ike_enable and state.mode == 'config':
            state.ipsec_crypto['isakmp_enabled'] = m_ike_enable.group(1)
            return ''

        # ── ロギング ──
        m_log = re.match(r'^logging\s+(?:host|server)\s+([\d.]+)', c)
        if m_log and state.mode == 'config':
            ip = m_log.group(1)
            if not any(s['host'] == ip for s in state.syslog_servers):
                state.syslog_servers.append({'host': ip, 'port': 514, 'facility': 'local7', 'level': 'informational'})
            return ''

        # write memory / write / wr
        if c in ('write memory', 'write', 'wr', 'copy running-config startup-config'):
            return ('Building configuration...\n'
                    f'Cryptochecksum: a1b2c3d4 e5f60718 90ab1234 cdef5678\n'
                    f'[OK]\n'
                    f'{state.hostname}# ')

        # ── hostname ──
        m_hn = re.match(r'^hostname\s+(\S+)', c)
        if m_hn and state.mode == 'config':
            state.hostname = m_hn.group(1)
            return ''

        # ── domain-name ──
        if re.match(r'^domain-name\s+', c):
            return ''

        # ── ping ──
        m_ping = re.match(r'^ping\s+([\d.]+)', c)
        if m_ping:
            dest = m_ping.group(1)
            return (f'Type escape sequence to abort.\n'
                    f'Sending 5, 100-byte ICMP Echos to {dest}, timeout is 2 seconds:\n'
                    f'!!!!!\n'
                    f'Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/5 ms')

        # ── help ──
        if c in ('?', 'help'):
            return self._asa_help()

        # 不明コマンド
        if cmd.strip():
            return f'ERROR: % Invalid input detected at \'^\'  marker.\n{cmd}\n      ^'
        return ''

    def _asa_show_version(self, state: DeviceState) -> str:
        up = state.uptime_str()
        return f"""Cisco Adaptive Security Appliance Software Version 9.16(4)
Device Manager Version 7.18(1)

Compiled on Tue 25-Jan-22 17:42 PST by builders
System image file is "disk0:/asa9164-smp-k8.bin"
Config file at boot was "startup-config"

{state.hostname} up {up}

Hardware:   ASA5525-X, 8192 MB RAM, CPU Lynnfield 2394 MHz, 1 CPU (8 cores)
Internal ATA Compact Flash, 8192MB
BIOS Flash M25P64 @ 0xfed01000, 16384KB

Encryption hardware device : Cisco ASA Crypto on-board accelerator
                             (revision 0x1) with 4096KB, HW_DES, HW_3DES, HW_AES, HW_AESCCM
                             HW_AAD, HW_HMACSHA1, HW_RANDOM

 0: Ext: GigabitEthernet0/0  : address is 0000.dead.1001, irq 11
 1: Ext: GigabitEthernet0/1  : address is 0000.dead.1002, irq 11
 2: Ext: GigabitEthernet0/2  : address is 0000.dead.1003, irq 11
 3: Ext: Management0/0       : address is 0000.dead.1004, irq 11

Licensed features for this platform:
Maximum Physical Interfaces       : Unlimited      perpetual
Maximum VLANs                     : 150            perpetual
Inside Hosts                      : Unlimited      perpetual
Failover                          : Active/Active  perpetual
Encryption-DES                    : Enabled        perpetual
Encryption-3DES-AES               : Enabled        perpetual
Security Contexts                 : 2              perpetual
GTP/GPRS                          : Disabled       perpetual
AnyConnect Premium Peers          : 750            perpetual
AnyConnect Essentials             : Disabled       perpetual
Other VPN Peers                   : 750            perpetual
Total VPN Peers                   : 750            perpetual
Shared License                    : Disabled       perpetual
AnyConnect for Mobile             : Disabled       perpetual
AnyConnect for Cisco VPN Phone    : Disabled       perpetual
Advanced Endpoint Assessment      : Disabled       perpetual
UC Phone Proxy Sessions           : 2              perpetual
Total UC Proxy Sessions           : 2              perpetual
Botnet Traffic Filter             : Disabled       perpetual
Intercompany Media Engine         : Disabled       perpetual

This platform has an ASA 5525-X with SW, 8GE Data, 1GE Mgmt license.

Serial Number: FTX1234A00B
Running Permanent Activation Key: 0x01234567 0x89abcdef 0xfedcba98 0x76543210 0x01234567
Configuration register is 0x1
Image type          : Release
Key Version         : A
"""

    def _asa_show_interface(self, state: DeviceState, target: str = None) -> str:
        lines = []
        for ifname, info in state.interfaces.items():
            if target and target.lower() not in ifname.lower():
                continue
            status  = info.get('status', 'up')
            nameif  = info.get('nameif', 'N/A')
            sec     = info.get('security_level', 0)
            ip      = info.get('ip', 'unassigned')
            prefix  = info.get('prefix', 0)
            mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix else 0
            mask = (f'{(mask_int>>24)&0xff}.{(mask_int>>16)&0xff}.'
                    f'{(mask_int>>8)&0xff}.{mask_int&0xff}')
            line_proto = 'up' if status == 'up' else 'down'
            lines.append(f'Interface {ifname} "{nameif}", is {status}, line protocol is {line_proto}')
            lines.append(f'  Security level is {sec}, Link is up (Connected)')
            if ip and ip != 'unassigned':
                lines.append(f'  IP address {ip}, subnet mask {mask}')
            else:
                lines.append(f'  IP address unassigned')
            lines.append(f'  MAC address 0000.dead.{abs(hash(ifname)) % 0xffff:04x}, MTU 1500')
            lines.append(f'  Input: 1234567 packets, 123456789 bytes; Errors: 0')
            lines.append(f'  Output: 987654 packets, 98765432 bytes; Errors: 0')
            lines.append('')
        return '\n'.join(lines).rstrip()

    def _asa_show_route(self, state: DeviceState) -> str:
        lines = [
            'Codes: C - connected, S - static, I - IGRP, R - RIP, M - mobile, B - BGP',
            '       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area',
            '       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2',
            '       E1 - OSPF external type 1, E2 - OSPF external type 2',
            '       i - IS-IS, L1 - IS-IS level-1, L2 - IS-IS level-2, ia - IS-IS inter area',
            '       * - candidate default, U - per-user static route, o - ODR',
            '',
            'Gateway of last resort is not set',
            '',
        ]
        for ifname, info in state.interfaces.items():
            ip = info.get('ip', '')
            prefix = info.get('prefix', 24)
            if not ip:
                continue
            # 直結ネットワーク
            try:
                octets = [int(x) for x in ip.split('.')]
                ip_int = (octets[0]<<24)|(octets[1]<<16)|(octets[2]<<8)|octets[3]
                mask = (0xffffffff << (32 - prefix)) & 0xffffffff
                net_int = ip_int & mask
                net = (f'{(net_int>>24)&0xff}.{(net_int>>16)&0xff}.'
                       f'{(net_int>>8)&0xff}.{net_int&0xff}')
                lines.append(f'C        {net}/{prefix} is directly connected, {info.get("nameif","?")}')
            except Exception:
                pass
        for r in state.routes:
            net = r.get('network', '')
            mask_str = r.get('mask', '255.255.255.0')
            prefix = sum(bin(int(o)).count('1') for o in mask_str.split('.'))
            nh = r.get('nexthop', '0.0.0.0')
            iface = r.get('iface', '')
            metric = r.get('metric', 1)
            lines.append(f'S        {net}/{prefix} [1/{metric}] via {nh}, {iface}')
        return '\n'.join(lines)

    def _asa_show_access_list(self, state: DeviceState) -> str:
        if not state.acls:
            return 'access-list: No access lists configured.'
        lines = []
        for name, entries in state.acls.items():
            for i, e in enumerate(entries):
                if e['action'] == 'remark':
                    lines.append(f'access-list {name} remark {e["raw"]}')
                else:
                    lines.append(f'access-list {name} line {i+1} extended '
                                 f'{e["action"]} {e["proto"]} {e["raw"]} '
                                 f'(hitcnt={random.randint(0,999)}) 0x{abs(hash(e["raw"]))%0xffffffff:08x}')
        return '\n'.join(lines)

    def _asa_show_conn(self, state: DeviceState) -> str:
        lines = [
            f'{random.randint(50,200)} in use, {random.randint(1,50)} most used',
            'Flags: A - awaiting inside ACK to SYN, a - awaiting outside ACK to SYN,',
            '       B - initial SYN from outside, C - CTIQBE media, D - DNS, d - dump,',
            '       E - outside back connection, F - outside FIN, f - inside FIN,',
            '       G - group, g - MGCP, H - H.323, h - H.225.0, I - inbound data,',
            '       i - incomplete, J - GTP, j - GTP data, K - GTP t3-response',
            '',
            'TCP inside 192.168.1.100:54321 outside 203.0.113.10:80, idle 0:00:03, bytes 1234, flags UIO',
            'TCP inside 192.168.1.101:55555 outside 8.8.8.8:443, idle 0:00:01, bytes 5678, flags UIO',
            'UDP inside 192.168.1.100:12345 outside 8.8.8.8:53, idle 0:00:00, bytes 128, flags -',
        ]
        return '\n'.join(lines)

    def _asa_show_xlate(self, state: DeviceState) -> str:
        nat_count = len(state.nat_rules)
        lines = [
            f'{nat_count} in use, {nat_count} most used',
            'Flags: D - DNS, e - extended, I - identity, i - dynamic, r - portmap,',
            '       s - static, T - twice, N - net-to-net',
        ]
        if state.nat_rules:
            for r in state.nat_rules:
                lines.append(f'NAT from {r["real"]}:any to {r["mapped"]}:{r.get("mapped_addr","interface")} flags si')
        else:
            lines.append('(empty)')
        return '\n'.join(lines)

    def _asa_show_nat(self, state: DeviceState) -> str:
        lines = ['Auto NAT Policies (Section 2)', '']
        if not state.nat_rules:
            lines.append('  (No NAT rules configured)')
        for i, r in enumerate(state.nat_rules, 1):
            lines.append(f'{i} (inside) to (outside) source {r.get("type","dynamic")} '
                         f'any interface')
        lines += ['', 'Manual NAT Policies (Section 3)', '  (empty)']
        return '\n'.join(lines)

    def _asa_show_crypto_isakmp_sa(self, state: DeviceState) -> str:
        if not state.ipsec_peers:
            return 'There are no IKEv1 SAs\nThere are no IKEv2 SAs'
        lines = ['IKEv1 SAs:', '', '   Active SA: 1', '    Rekey SA: 0 (A tunnel will start rekey 30 seconds before the lifetime expires.)',
                 '    Total IKE SA: 1', '',
                 '1   IKE Peer: ' + list(state.ipsec_peers.keys())[0]]
        lines += ['    Type    : L2L             Role    : initiator',
                  '    Rekey   : no              State   : MM_ACTIVE']
        return '\n'.join(lines)

    def _asa_show_crypto_ipsec_sa(self, state: DeviceState) -> str:
        if not state.ipsec_peers:
            return 'There are no ipsec sas.'
        peer = list(state.ipsec_peers.keys())[0]
        lines = [
            f'interface: outside',
            f'    Crypto map tag: OUTSIDE_MAP, seq num: 10, local addr: 203.0.113.1',
            f'',
            f'      local  ident  (addr/mask/prot/port): (192.168.1.0/255.255.255.0/0/0)',
            f'      remote ident (addr/mask/prot/port): (10.1.1.0/255.255.255.0/0/0)',
            f'      current_peer: {peer}',
            f'',
            f'      #pkts encaps: 12345, #pkts encrypt: 12345, #pkts digest: 12345',
            f'      #pkts decaps: 12200, #pkts decrypt: 12200, #pkts verify: 12200',
            f'      #pkts compressed: 0, #pkts decompressed: 0',
            f'      #pkts not compressed: 12345, #pkts comp failed: 0, #pkts decomp failed: 0',
            f'      #pre-frag successes: 0, #pre-frag failures: 0, #fragments created: 0',
            f'      #PMTUs sent: 0, #PMTUs rcvd: 0, #decapsulated frgs needing reassembly: 0',
            f'      #TFC rcvd: 0, #TFC sent: 0',
            f'      #Valid ICMP Errors rcvd: 0, #Invalid ICMP Errors rcvd: 0',
            f'      #send errors: 0, #recv errors: 0',
            f'',
            f'      local crypto endpt.: 203.0.113.1/0, remote crypto endpt.: {peer}/0',
            f'      path mtu 1500, ipsec overhead 74(44), media mtu 1500',
            f'      PMTU time remaining (sec): 0, DF policy: copy-df',
            f'      ICMP error validation: disabled, TFC packets: disabled',
            f'      current outbound spi: 0xA1B2C3D4',
            f'      current inbound spi : 0xD4C3B2A1',
            f'',
            f'    inbound esp sas:',
            f'      spi: 0xD4C3B2A1 (3569939105)',
            f'        transform: esp-aes-256 esp-sha-hmac no compression',
            f'        in use settings =={{Tunnel, TFC pessimize}}',
            f'        slot: 0, conn_id: 12288, crypto-map: OUTSIDE_MAP',
            f'        sa timing: remaining key lifetime (kB/sec): (4374000/28799)',
            f'',
            f'    outbound esp sas:',
            f'      spi: 0xA1B2C3D4 (2712847316)',
            f'        transform: esp-aes-256 esp-sha-hmac no compression',
            f'        in use settings =={{Tunnel, TFC pessimize}}',
            f'        slot: 0, conn_id: 12288, crypto-map: OUTSIDE_MAP',
            f'        sa timing: remaining key lifetime (kB/sec): (4374000/28799)',
        ]
        return '\n'.join(lines)

    def _asa_show_service_policy(self, state: DeviceState) -> str:
        lines = ['Global policy:',
                 '  Service-policy: global_policy',
                 '    Class-map: inspection_default',
                 '      Inspect:']
        for proto in state.inspect_protocols:
            lines.append(f'        {proto}, packet 0, lock fail 0, drop 0, reset-drop 0, 5-min-pkt-rate 0 pkts/sec, v6-fail-close 0 sctp-drop-override 0')
        return '\n'.join(lines)

    def _asa_show_running(self, state: DeviceState) -> str:
        lines = [
            ': Saved',
            ':',
            f'ASA Version 9.16(4)',
            '!',
            f'hostname {state.hostname}',
            'enable password *** encrypted',
            'passwd *** encrypted',
            'names',
            '!',
        ]
        # インタフェース
        for ifname, info in state.interfaces.items():
            lines.append(f'interface {ifname}')
            nameif = info.get('nameif', '')
            if nameif:
                lines.append(f' nameif {nameif}')
                lines.append(f' security-level {info.get("security_level", 0)}')
            ip = info.get('ip', '')
            if ip:
                prefix = info.get('prefix', 24)
                mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff
                mask = (f'{(mask_int>>24)&0xff}.{(mask_int>>16)&0xff}.'
                        f'{(mask_int>>8)&0xff}.{mask_int&0xff}')
                lines.append(f' ip address {ip} {mask}')
            if info.get('status', 'up') != 'down':
                lines.append(' no shutdown')
            lines.append('!')
        # ACL
        for name, entries in state.acls.items():
            for e in entries:
                if e['action'] == 'remark':
                    lines.append(f'access-list {name} remark {e["raw"]}')
                else:
                    lines.append(f'access-list {name} extended {e["action"]} {e["proto"]} {e["raw"]}')
        # NAT
        for r in state.nat_rules:
            lines.append(f'nat ({r["real"]},{r["mapped"]}) {r["type"]} {r.get("mapped_addr","interface")}')
        # ルート
        for r in state.routes:
            lines.append(f'route {r["iface"]} {r["network"]} {r["mask"]} {r["nexthop"]} {r.get("metric",1)}')
        # IKE/IPsec
        for num, pol in state.ipsec_crypto.get('isakmp_policies', {}).items():
            lines += [
                f'crypto isakmp policy {num}',
                f' authentication {pol.get("authentication","pre-share")}',
                f' encryption {pol.get("encryption","aes-256")}',
                f' hash {pol.get("hash","sha256")}',
                f' group {pol.get("group",14)}',
                f' lifetime {pol.get("lifetime",86400)}',
            ]
        for ts_name, ts in state.ipsec_crypto.get('transform_sets', {}).items():
            lines.append(f'crypto ipsec transform-set {ts_name} ' + ' '.join(ts.get('transforms', [])))
        for mapname, seqs in state.ipsec_crypto.get('crypto_maps', {}).items():
            for seq, entry in seqs.items():
                if entry.get('acl'):
                    lines.append(f'crypto map {mapname} {seq} match address {entry["acl"]}')
                if entry.get('peer'):
                    lines.append(f'crypto map {mapname} {seq} set peer {entry["peer"]}')
                if entry.get('transform_set'):
                    lines.append(f'crypto map {mapname} {seq} set transform-set {entry["transform_set"]}')
        cm_if = state.ipsec_crypto.get('crypto_map_interface', {})
        if cm_if:
            lines.append(f'crypto map {cm_if["name"]} interface {cm_if["interface"]}')
        ike_if = state.ipsec_crypto.get('isakmp_enabled', '')
        if ike_if:
            lines.append(f'crypto isakmp enable {ike_if}')
        for peer, pinfo in state.ipsec_peers.items():
            lines.append(f'tunnel-group {peer} type {pinfo.get("type","ipsec-l2l")}')
            lines.append(f'tunnel-group {peer} ipsec-attributes')
            psk = pinfo.get('preshared', '')
            if psk:
                lines.append(f' ikev1 pre-shared-key {psk}')
        # サービスポリシー
        lines += [
            '!',
            'class-map inspection_default',
            ' match default-inspection-traffic',
            '!',
            'policy-map global_policy',
            ' class inspection_default',
        ]
        for proto in state.inspect_protocols:
            lines.append(f'  inspect {proto}')
        for sp in state.service_policies:
            lines.append(f'service-policy {sp["policy"]} {sp["scope"]}')
        # syslog
        for s in state.syslog_servers:
            lines.append(f'logging host {s["host"]}')
        lines += ['!', 'end']
        return '\n'.join(lines)

    def _asa_help(self) -> str:
        return (
            'Available commands (Cisco ASA 9.x シングルコンテキスト):\n'
            '\n[設定モード]\n'
            '  configure terminal            -- 設定モードへ移行\n'
            '  interface <if>                -- インタフェース設定モード\n'
            '    nameif <name>               -- インタフェース名前付け\n'
            '    security-level <0-100>      -- セキュリティレベル設定\n'
            '    ip address <ip> <mask>      -- IPアドレス設定\n'
            '    [no] shutdown               -- ポートUP/DOWN\n'
            '\n[ACL]\n'
            '  access-list <name> extended permit|deny <proto> <src> <dst> [eq <port>]\n'
            '  access-list <name> remark <text>\n'
            '  access-group <name> in|out interface <nameif>\n'
            '\n[NAT]\n'
            '  nat (inside,outside) dynamic interface\n'
            '  nat (inside,outside) static <ip>\n'
            '\n[ルーティング]\n'
            '  route <nameif> <net> <mask> <nexthop> [metric]\n'
            '\n[ステートフルインスペクション (MPF)]\n'
            '  class-map inspection_default\n'
            '    match default-inspection-traffic\n'
            '  policy-map global_policy\n'
            '    class inspection_default\n'
            '      inspect ftp|http|https|smtp|dns|h323|sip|skinny\n'
            '  service-policy global_policy global\n'
            '\n[IPsec/IKE]\n'
            '  crypto isakmp policy <num>\n'
            '    authentication pre-share\n'
            '    encryption aes-256\n'
            '    hash sha256\n'
            '    group 14\n'
            '    lifetime 86400\n'
            '  crypto ipsec transform-set <name> esp-aes-256 esp-sha-hmac\n'
            '  crypto map <name> <seq> match address <acl>\n'
            '  crypto map <name> <seq> set peer <ip>\n'
            '  crypto map <name> <seq> set transform-set <ts>\n'
            '  crypto map <name> interface outside\n'
            '  crypto isakmp enable outside\n'
            '  tunnel-group <peer-ip> type ipsec-l2l\n'
            '  tunnel-group <peer-ip> ipsec-attributes\n'
            '    ikev1 pre-shared-key <key>\n'
            '\n[確認]\n'
            '  show version\n'
            '  show interface\n'
            '  show ip route\n'
            '  show access-list\n'
            '  show conn\n'
            '  show xlate / show nat\n'
            '  show crypto isakmp sa\n'
            '  show crypto ipsec sa\n'
            '  show service-policy\n'
            '  show running-config\n'
        )

    # ─── PC コマンドエンジン（Linux汎用エンドポイント）─────────
    def _pc_process(self, cmd: str, c: str, state: DeviceState) -> str:
        """PCのLinuxライクなコマンドを処理する"""

        # ping / traceroute はapp.pyのhandle_icmpで処理されるが、
        # ここでは到達前にフォールスルーしないよう念のためNoneを示す
        # （実際は呼ばれない）

        # ping
        if re.match(r'^ping\s+', c):
            return self._pc_ping(cmd, c, state)

        # traceroute / tracepath
        if re.match(r'^traceroute\s+|^tracepath\s+', c):
            return self._pc_traceroute(cmd, c, state)

        # nc / netcat  (TCP接続テスト)
        if re.match(r'^nc\s+|^netcat\s+', c):
            return self._pc_nc(cmd, c, state)

        # telnet (TCP ポート確認)
        if re.match(r'^telnet\s+', c):
            return self._pc_telnet(cmd, c, state)

        # curl
        if re.match(r'^curl\s+', c):
            return self._pc_curl(cmd, c, state)

        # wget
        if re.match(r'^wget\s+', c):
            return self._pc_wget(cmd, c, state)

        # ssh
        if re.match(r'^ssh\s+', c):
            return self._pc_ssh(cmd, c, state)

        # hostname
        if c == 'hostname':
            return state.hostname

        # uname
        if c.startswith('uname'):
            if '-a' in c:
                return f'Linux {state.hostname} 5.15.0 #1 SMP x86_64 GNU/Linux'
            return 'Linux'

        # whoami / id
        if c in ('whoami', 'id'):
            return 'root' if c == 'whoami' else 'uid=0(root) gid=0(root) groups=0(root)'

        # ifconfig
        m_ifconfig = re.match(r'^ifconfig(?:\s+(\S+))?', c)
        if m_ifconfig:
            target = m_ifconfig.group(1)
            return self._pc_ifconfig(state, target)

        # ip addr / ip address
        if re.match(r'^ip\s+addr(?:ess)?(?:\s+show)?', c) or c == 'ip a':
            return self._pc_ip_addr(state)

        # ip route / route / netstat -r
        if re.match(r'^ip\s+route(?:\s+show)?', c) or c == 'ip r':
            return self._pc_ip_route(state)
        if re.match(r'^(?:route|netstat\s+-r)', c):
            return self._pc_route_table(state)

        # ip link
        if re.match(r'^ip\s+link(?:\s+show)?', c) or c == 'ip l':
            return self._pc_ip_link(state)

        # arp -n
        if re.match(r'^arp', c):
            eth0 = state.interfaces.get('eth0', {})
            gw = state.gateway
            lines = ['Address                  HWtype  HWaddress           Flags Mask            Iface']
            lines.append(f'{gw:<25}ether   aa:bb:cc:00:11:22   C                     eth0')
            return '\n'.join(lines)

        # cat /etc/hosts
        if 'cat' in c and '/etc/hosts' in c:
            return f'127.0.0.1   localhost\n127.0.1.1   {state.hostname}'

        # cat /etc/resolv.conf
        if 'cat' in c and 'resolv.conf' in c:
            return 'nameserver 8.8.8.8\nnameserver 1.1.1.1'

        # ss -tuln / netstat -tuln
        if re.match(r'^(?:ss|netstat)\s+-', c):
            return ('Netid  State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port\n'
                    'tcp    LISTEN  0       128     0.0.0.0:22          0.0.0.0:*')

        # ps aux / ps
        if re.match(r'^ps', c):
            return (f'  PID TTY          TIME CMD\n'
                    f'    1 ?        00:00:01 init\n'
                    f'  100 ?        00:00:00 sshd\n'
                    f'  200 pts/0    00:00:00 bash\n'
                    f'  201 pts/0    00:00:00 ps')

        # uptime
        if c == 'uptime':
            delta = datetime.now() - state.startup_time
            h = int(delta.total_seconds() // 3600)
            return f' {datetime.now().strftime("%H:%M:%S")} up {h}:00,  1 user,  load average: 0.00, 0.00, 0.00'

        # date
        if c == 'date':
            return datetime.now().strftime('%a %b %d %H:%M:%S JST %Y')

        # clear
        if c in ('clear', 'cls'):
            return '\x0c'

        # help / ?
        if c in ('help', '?', 'man'):
            return (
                'Available commands:\n'
                '  ifconfig / ip addr      -- ネットワークインタフェース表示\n'
                '  ip route / route        -- ルーティングテーブル表示\n'
                '  ip link                 -- リンク状態表示\n'
                '  ping <IP>               -- 到達性確認 (ICMPエコー)\n'
                '  traceroute <IP>         -- 経路トレース\n'
                '  nc -zv <IP> <PORT>      -- TCP ポート接続テスト\n'
                '  nc -zv <IP> <PORT1>-<PORT2>  -- TCP ポートレンジスキャン\n'
                '  telnet <IP> <PORT>      -- TCP Telnet 接続テスト\n'
                '  curl http://<IP>/       -- HTTP GET リクエスト\n'
                '  curl -v http://<IP>/    -- HTTP GET 詳細表示\n'
                '  wget http://<IP>/       -- HTTP ファイル取得\n'
                '  ssh user@<IP>           -- SSH 接続テスト\n'
                '  arp -n                  -- ARPテーブル表示\n'
                '  hostname                -- ホスト名表示\n'
                '  uname -a                -- OS情報表示\n'
                '  uptime                  -- 稼働時間表示\n'
                '  ps                      -- プロセス一覧\n'
                '  ss -tuln                -- ソケット一覧\n'
            )

        # 不明コマンド
        prog = cmd.split()[0] if cmd.split() else cmd
        return f'-bash: {prog}: command not found'

    # ── PC 通信テスト系コマンド ──────────────────────────────────

    def _pc_my_ip(self, state) -> str:
        """PC の eth0 IP を返す"""
        return state.interfaces.get('eth0', {}).get('ip', '192.168.1.10')

    def _pc_ping(self, cmd, c, state) -> str:
        m = re.match(r'^ping\s+(?:-c\s*(\d+)\s+)?([\d.]+)', c)
        if not m:
            return "Usage: ping [-c count] <destination>"
        count = int(m.group(1)) if m.group(1) else 5
        dest  = m.group(2)
        my_ip = self._pc_my_ip(state)
        rtt_base = round(random.uniform(0.5, 3.0), 3)
        lines = [f"PING {dest} ({dest}) 56(84) bytes of data."]
        for i in range(count):
            rtt = round(rtt_base + random.uniform(-0.2, 0.5), 3)
            lines.append(f"64 bytes from {dest}: icmp_seq={i+1} ttl=64 time={rtt} ms")
        rtts = [rtt_base + random.uniform(-0.2, 0.5) for _ in range(count)]
        lines += [
            "",
            f"--- {dest} ping statistics ---",
            f"{count} packets transmitted, {count} received, 0% packet loss, time {count*1000}ms",
            f"rtt min/avg/max/mdev = {min(rtts):.3f}/{sum(rtts)/len(rtts):.3f}/{max(rtts):.3f}/0.100 ms",
        ]
        return "\n".join(lines)

    def _pc_traceroute(self, cmd, c, state) -> str:
        m = re.match(r'^trace(?:route|path)\s+([\d.]+)', c)
        if not m:
            return "Usage: traceroute <destination>"
        dest  = m.group(1)
        gw    = state.gateway
        lines = [f"traceroute to {dest} ({dest}), 30 hops max, 60 byte packets"]
        hops  = [(1, gw, round(random.uniform(0.5, 2.0), 3)),
                 (2, "203.0.113.254", round(random.uniform(5, 15), 3)),
                 (3, dest, round(random.uniform(8, 25), 3))]
        for n, ip, rtt in hops:
            r2, r3 = round(rtt + random.uniform(0, 1), 3), round(rtt + random.uniform(0, 1), 3)
            lines.append(f" {n}  {ip}  {rtt} ms  {r2} ms  {r3} ms")
        return "\n".join(lines)

    def _pc_nc(self, cmd, c, state) -> str:
        """nc -zv <host> <port> または nc -zv <host> <port1>-<port2>"""
        m_range = re.match(r'^nc\s+.*?(-z)?.*?([\d.]+)\s+(\d+)-(\d+)', c)
        m_single = re.match(r'^nc\s+.*?([\d.]+)\s+(\d+)', c)
        my_ip = self._pc_my_ip(state)
        if m_range:
            host  = m_range.group(2)
            p_lo  = int(m_range.group(3))
            p_hi  = int(m_range.group(4))
            lines = []
            for port in range(p_lo, min(p_hi + 1, p_lo + 20)):
                rtt = round(random.uniform(0.3, 5.0), 2)
                lines.append(f"Connection to {host} {port} port [tcp/*] succeeded!  ({rtt} ms)")
            return "\n".join(lines) if lines else f"nc: connect to {host} port {p_lo} failed"
        elif m_single:
            host = m_single.group(1)
            port = int(m_single.group(2))
            rtt  = round(random.uniform(0.3, 5.0), 2)
            lines = [
                f"Connection to {host} {port} port [tcp/*] succeeded!",
                f"  Local:  {my_ip}:{random.randint(40000,60000)}",
                f"  Remote: {host}:{port}",
                f"  RTT:    {rtt} ms",
                f"  State:  ESTABLISHED",
            ]
            return "\n".join(lines)
        return "Usage: nc -zv <host> <port>"

    def _pc_telnet(self, cmd, c, state) -> str:
        m = re.match(r'^telnet\s+([\d.]+)(?:\s+(\d+))?', c)
        if not m:
            return "Usage: telnet <host> [port]"
        host = m.group(1)
        port = int(m.group(2)) if m.group(2) else 23
        my_ip = self._pc_my_ip(state)
        rtt = round(random.uniform(0.5, 8.0), 2)
        if port == 23:
            return (f"Trying {host}...\n"
                    f"Connected to {host}.\n"
                    f"Escape character is '^]'.\n"
                    f"\n"
                    f"login: ")
        else:
            return (f"Trying {host}...\n"
                    f"Connected to {host} port {port}.\n"
                    f"Escape character is '^]'.\n"
                    f"  TCP connection established in {rtt} ms\n"
                    f"  Local:  {my_ip}:{random.randint(40000,60000)}\n"
                    f"  Remote: {host}:{port}")

    def _pc_curl(self, cmd, c, state) -> str:
        verbose = '-v' in c or '--verbose' in c
        m = re.match(r'^curl\s+(?:-\S+\s+)*(?:http://|https://)?([\d.]+\S*)', c)
        if not m:
            return "curl: try 'curl http://<IP>/'"
        url_part = m.group(1)
        host = url_part.split('/')[0]
        path = '/' + '/'.join(url_part.split('/')[1:]) if '/' in url_part else '/'
        my_ip = self._pc_my_ip(state)
        rtt = round(random.uniform(1.0, 15.0), 3)
        if verbose:
            lines = [
                f"*   Trying {host}:80...",
                f"* Connected to {host} ({host}) port 80 (#0)",
                f"> GET {path} HTTP/1.1",
                f"> Host: {host}",
                f"> User-Agent: curl/7.88.1",
                f"> Accept: */*",
                f">",
                f"< HTTP/1.1 200 OK",
                f"< Content-Type: text/html",
                f"< Content-Length: 612",
                f"< Connection: keep-alive",
                f"<",
                f"<!DOCTYPE html>",
                f"<html><head><title>Network Lab Test Page</title></head>",
                f"<body><h1>TCP Connection OK</h1>",
                f"<p>Server: {host} | Client: {my_ip} | RTT: {rtt}ms</p>",
                f"</body></html>",
                f"* Connection #0 to host {host} left intact",
            ]
        else:
            lines = [
                f"<!DOCTYPE html>",
                f"<html><head><title>Network Lab Test Page</title></head>",
                f"<body><h1>TCP Connection OK</h1>",
                f"<p>Server: {host} | Client: {my_ip} | RTT: {rtt}ms</p>",
                f"</body></html>",
            ]
        return "\n".join(lines)

    def _pc_wget(self, cmd, c, state) -> str:
        m = re.match(r'^wget\s+(?:-\S+\s+)*(?:http://|https://)?([\d.]+\S*)', c)
        if not m:
            return "wget: missing URL"
        url_part = m.group(1)
        host = url_part.split('/')[0]
        rtt = round(random.uniform(1.0, 15.0), 3)
        size = random.randint(200, 800)
        lines = [
            f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--  http://{url_part}",
            f"Resolving {host}... {host}",
            f"Connecting to {host}|{host}|:80... connected.",
            f"HTTP request sent, awaiting response... 200 OK",
            f"Length: {size} [text/html]",
            f"Saving to: 'index.html'",
            f"",
            f"index.html        100%[==========>]  {size}  --.-KB/s    in {rtt}s",
            f"",
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({size/rtt:.0f} B/s) - 'index.html' saved [{size}/{size}]",
        ]
        return "\n".join(lines)

    def _pc_ssh(self, cmd, c, state) -> str:
        m = re.match(r'^ssh\s+(?:-\S+\s+)*(?:(\S+)@)?([\d.]+)', c)
        if not m:
            return "Usage: ssh [user@]<host>"
        user = m.group(1) or 'root'
        host = m.group(2)
        return (f"The authenticity of host '{host} ({host})' can't be established.\n"
                f"ED25519 key fingerprint is SHA256:abc123xyz/networklab.\n"
                f"Are you sure you want to continue connecting (yes/no/[fingerprint])? yes\n"
                f"Warning: Permanently added '{host}' (ED25519) to the list of known hosts.\n"
                f"{user}@{host}'s password: \n"
                f"Last login: {datetime.now().strftime('%a %b %d %H:%M:%S %Y')} from {self._pc_my_ip(state)}\n"
                f"[{user}@{host} ~]$ ")

    def _pc_ifconfig(self, state: DeviceState, target: str = None) -> str:
        lines = []
        for ifname, info in state.interfaces.items():
            if target and ifname != target:
                continue
            ip = info.get('ip', '')
            prefix = info.get('prefix', 24)
            mac = info.get('mac', '00:00:00:00:00:00')
            status = info.get('status', 'up')
            flags = 'UP,BROADCAST,RUNNING,MULTICAST' if status == 'up' else 'DOWN'
            if ifname == 'lo':
                flags = 'UP,LOOPBACK,RUNNING'
            # サブネットマスク計算
            mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff
            mask = f'{(mask_int>>24)&0xff}.{(mask_int>>16)&0xff}.{(mask_int>>8)&0xff}.{mask_int&0xff}'
            lines.append(f'{ifname}: flags={flags}  mtu 1500')
            if ip:
                lines.append(f'        inet {ip}  netmask {mask}  broadcast {self._broadcast(ip, prefix)}')
            lines.append(f'        ether {mac}  txqueuelen 1000  (Ethernet)')
            lines.append(f'        RX packets 1234  bytes 567890 (567.8 KB)')
            lines.append(f'        TX packets 456   bytes 123456 (123.4 KB)')
            lines.append('')
        return '\n'.join(lines).rstrip()

    def _pc_ip_addr(self, state: DeviceState) -> str:
        lines = []
        idx = 1
        for ifname, info in state.interfaces.items():
            ip = info.get('ip', '')
            prefix = info.get('prefix', 24)
            mac = info.get('mac', '00:00:00:00:00:00')
            status = info.get('status', 'up')
            state_str = 'UP' if status == 'up' else 'DOWN'
            flags = 'LOOPBACK,UP,LOWER_UP' if ifname == 'lo' else f'BROADCAST,MULTICAST,{state_str},LOWER_UP'
            lines.append(f'{idx}: {ifname}: <{flags}> mtu 1500 qdisc pfifo_fast state {state_str}')
            lines.append(f'    link/ether {mac} brd ff:ff:ff:ff:ff:ff')
            if ip:
                bcast = self._broadcast(ip, prefix)
                lines.append(f'    inet {ip}/{prefix} brd {bcast} scope global {ifname}')
                lines.append(f'       valid_lft forever preferred_lft forever')
            idx += 1
        return '\n'.join(lines)

    def _pc_ip_route(self, state: DeviceState) -> str:
        lines = []
        for r in state.routes:
            dest = r['dest']
            gw = r['gw']
            iface = r['iface']
            metric = r.get('metric', 0)
            if dest == '0.0.0.0/0':
                lines.append(f'default via {gw} dev {iface} proto static metric {metric}')
            elif gw == '0.0.0.0':
                lines.append(f'{dest} dev {iface} proto kernel scope link src {state.interfaces.get(iface,{}).get("ip","?")} metric {metric}')
            else:
                lines.append(f'{dest} via {gw} dev {iface} metric {metric}')
        return '\n'.join(lines)

    def _pc_route_table(self, state: DeviceState) -> str:
        lines = ['Kernel IP routing table',
                 'Destination     Gateway         Genmask         Flags Metric Ref    Use Iface']
        for r in state.routes:
            dest_net = r['dest'].split('/')[0]
            prefix = int(r['dest'].split('/')[1]) if '/' in r['dest'] else 0
            mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix else 0
            mask = f'{(mask_int>>24)&0xff}.{(mask_int>>16)&0xff}.{(mask_int>>8)&0xff}.{mask_int&0xff}'
            gw = r['gw'] if r['gw'] != '0.0.0.0' else '0.0.0.0'
            flag = 'UG' if r['gw'] != '0.0.0.0' else 'U'
            dest_disp = '0.0.0.0' if r['dest'] == '0.0.0.0/0' else dest_net
            lines.append(f'{dest_disp:<16}{gw:<16}{mask:<16}{flag}     {r.get("metric",0):<7}0      0 {r["iface"]}')
        return '\n'.join(lines)

    def _pc_ip_link(self, state: DeviceState) -> str:
        lines = []
        idx = 1
        for ifname, info in state.interfaces.items():
            mac = info.get('mac', '00:00:00:00:00:00')
            status = info.get('status', 'up')
            state_str = 'UP' if status == 'up' else 'DOWN'
            flags = 'LOOPBACK,UP,LOWER_UP' if ifname == 'lo' else f'BROADCAST,MULTICAST,{state_str},LOWER_UP'
            lines.append(f'{idx}: {ifname}: <{flags}> mtu 1500 qdisc pfifo_fast state {state_str} mode DEFAULT group default')
            lines.append(f'    link/ether {mac} brd ff:ff:ff:ff:ff:ff')
            idx += 1
        return '\n'.join(lines)

    @staticmethod
    def _broadcast(ip: str, prefix: int) -> str:
        try:
            octets = [int(x) for x in ip.split('.')]
            ip_int = (octets[0]<<24)|(octets[1]<<16)|(octets[2]<<8)|octets[3]
            mask = (0xffffffff << (32 - prefix)) & 0xffffffff
            bcast_int = (ip_int & mask) | (~mask & 0xffffffff)
            return f'{(bcast_int>>24)&0xff}.{(bcast_int>>16)&0xff}.{(bcast_int>>8)&0xff}.{bcast_int&0xff}'
        except Exception:
            return '255.255.255.255'

    # ─── APRESIA コマンドエンジン（ApresiaLightGM200マニュアル準拠）─
    def _apresia_process(self, cmd: str, c: str, state: DeviceState) -> str:
        """APRESIAのCLIコマンドを処理する"""

        # モード遷移
        if c in ('exit', 'end', 'quit'):
            if state.mode == 'config-if':
                state.mode = 'config'
            elif state.mode in ('config-router', 'config-vlan'):
                state.mode = 'config'
            elif state.mode == 'config':
                state.mode = 'exec'
            return ''
        if c in ('configure terminal', 'conf t', 'configure', 'conf', 'config'):
            state.mode = 'config'
            return 'Enter configuration commands, one per line.  End with CNTL/Z.'
        if c == 'enable':
            return ''
        if c == 'disable':
            return ''

        # ── インターフェース移行 ──
        # "interface port 1/0/1" または "interface vlan 10"
        m_if = re.match(r'^interface\s+(port\s+[\d/]+|vlan\s+\d+)', c)
        if m_if:
            state.mode = 'config-if'
            itype = m_if.group(1)
            if 'port' in itype:
                port = itype.replace('port', '').strip()
                state.current_if = f'Port{port}'
            else:
                vid = itype.replace('vlan', '').strip()
                state.current_if = f'Vlan{vid}'
            return ''

        # ── show コマンド群 ──
        if re.match(r'^show\s+version', c):
            return self._apresia_show_version(state)
        if re.match(r'^show\s+unit', c):
            return self._apresia_show_unit(state)
        if re.match(r'^show\s+environment', c):
            return self._apresia_show_environment(state)
        if re.match(r'^show\s+cpu\s+utilization', c):
            return (f'CPU Utilization Statistics\n'
                    f'  5 sec: {random.randint(2,15)}%   '
                    f'1 min: {random.randint(3,12)}%   '
                    f'5 min: {random.randint(3,10)}%')
        if re.match(r'^show\s+(running-config|startup-config)', c):
            return self._apresia_show_running(state)
        if re.match(r'^show\s+port\s+\S+', c):
            m = re.match(r'^show\s+port\s+(\S+)', c)
            return self._apresia_show_port(m.group(1), state)
        if re.match(r'^show\s+port$', c):
            return self._apresia_show_port('all', state)
        if re.match(r'^show\s+vlan\s+detail', c):
            return self._apresia_show_vlan_detail(state)
        if re.match(r'^show\s+vlan\s+interface', c):
            return self._apresia_show_vlan_interface(state)
        if re.match(r'^show\s+vlan', c):
            return self._apresia_show_vlan(state)
        if re.match(r'^show\s+ipif', c):
            return self._apresia_show_ipif(state)
        if re.match(r'^show\s+arp', c):
            return self._apresia_show_arp(state)
        if re.match(r'^show\s+mac\s+address-table', c) or re.match(r'^show\s+fdb', c):
            return self._apresia_show_fdb(state)
        if re.match(r'^show\s+spanning-tree', c) or re.match(r'^show\s+stp', c):
            return self._apresia_show_stp(state)
        if re.match(r'^show\s+loopdetect', c):
            return self._apresia_show_loopdetect(state)
        if re.match(r'^show\s+lldp\s+neighbors', c):
            return self._apresia_show_lldp(state)
        if re.match(r'^show\s+interfaces?\s+status', c):
            return self._apresia_show_port('all', state)
        if re.match(r'^show\s+(interfaces?|port\s+description)', c):
            return self._apresia_show_port('all', state)
        if re.match(r'^show\s+history', c):
            return 'show version\nshow unit\nshow port\nshow vlan'
        if re.match(r'^show\s+boot', c):
            return ('Current firmware image  : image1 (active)\n'
                    'Next boot firmware image: image1\n'
                    'Configuration file      : primary-config')

        # ── ping / traceroute ──
        if c.startswith('ping '):
            return self._cmd_ping(cmd, state)
        if c.startswith('traceroute '):
            return self._cmd_traceroute(cmd, state)

        # ── 設定コマンド（config mode）──
        if state.mode == 'config':
            # hostname
            m_hn = re.match(r'^hostname\s+(\S+)', c)
            if m_hn:
                state.hostname = m_hn.group(1)
                return ''
            # VLAN作成: "vlan <id>"
            m_vl = re.match(r'^vlan\s+(\d+)', c)
            if m_vl:
                state.mode = 'config-vlan'
                return ''
            # loopdetect
            if re.match(r'^(enable|disable)\s+loopdetect', c):
                return ''
            # OSPF
            if re.match(r'^router\s+ospf', c):
                state.mode = 'config-router'
                state._routing_mode = 'ospf'
                return ''
            # RIP
            if re.match(r'^router\s+rip', c):
                state.mode = 'config-router'
                state._routing_mode = 'rip'
                return ''
            # 管理IP: "config ipif System 192.168.1.1/24"
            m_ip = re.match(r'^config\s+ipif\s+\w+\s+([\d.]+)/(\d+)', c)
            if m_ip:
                ip, prefix = m_ip.group(1), int(m_ip.group(2))
                if 'Vlan10' in state.interfaces:
                    state.interfaces['Vlan10']['ip'] = ip
                    state.interfaces['Vlan10']['prefix'] = prefix
                return ''
            return ''

        if state.mode == 'config-if' and state.current_if:
            iface = state.current_if
            if re.match(r'^switchport\s+mode\s+access', c):
                if iface in state.interfaces:
                    state.interfaces[iface]['vlan_mode'] = 'access'
                return ''
            if re.match(r'^switchport\s+mode\s+trunk', c):
                if iface in state.interfaces:
                    state.interfaces[iface]['vlan'] = 'trunk'
                return ''
            m_vlan = re.match(r'^switchport\s+access\s+vlan\s+(\d+)', c)
            if m_vlan:
                if iface in state.interfaces:
                    state.interfaces[iface]['vlan'] = m_vlan.group(1)
                return ''
            m_desc = re.match(r'^description\s+(.+)', cmd)
            if m_desc:
                if iface in state.interfaces:
                    state.interfaces[iface]['desc'] = m_desc.group(1)
                return ''
            if re.match(r'^(no\s+)?shutdown', c):
                if iface in state.interfaces:
                    state.interfaces[iface]['status'] = (
                        'notconnect' if c.startswith('no') else 'connected')
                return ''
            return ''

        # ── write（設定保存）──
        if c in ('write', 'write memory', 'save'):
            return 'Building configuration...\n[OK]'
        if c in ('reboot',):
            return 'Are you sure you want to reboot the device? (y/n): y\nRebooting...'

        # ── 不明コマンド ──
        return f"% Unknown command."

    def _apresia_show_version(self, state: DeviceState) -> str:
        now = datetime.now()
        up = state.uptime_str()
        return (
            f'{state.hostname}\n'
            f' ApresiaLightGM200シリーズ  Version 2.03\n'
            f' Copyright (C) 2024 APRESIA Systems, Ltd.\n'
            f'\n'
            f' APLGM200-24GT\n'
            f' Serial Number   : APLGM200001\n'
            f' Firmware Version: 2.03\n'
            f' Boot Version    : 1.02\n'
            f' Hardware Version: Rev.A\n'
            f' MAC Address     : 00:1a:2b:3c:4d:00\n'
            f' System Up Time  : {up}\n'
            f' Current Time    : {now.strftime("%Y/%m/%d %H:%M:%S")}\n'
        )

    def _apresia_show_unit(self, state: DeviceState) -> str:
        return (
            f' Unit  : 1\n'
            f' Model : APLGM200-24GT\n'
            f' Status: OK\n'
            f' Ports : 24 x 1000BASE-T + 4 x SFP\n'
            f' MAC   : 00:1a:2b:3c:4d:00\n'
        )

    def _apresia_show_environment(self, state: DeviceState) -> str:
        return (
            f' Fan Status   : OK (Fan1: Running, Fan2: Running)\n'
            f' Temperature  : 42°C (Threshold: 70°C)\n'
            f' Power Status : OK (AC Power)\n'
            f' Voltage      : Normal\n'
        )

    def _apresia_show_port(self, portspec: str, state: DeviceState) -> str:
        lines = [
            f' Port       Status     Vlan  Duplex  Speed   Desc',
            f' ---------- ---------- ----- ------- ------- ----------------',
        ]
        for name, iface in state.interfaces.items():
            if not name.startswith('Port'):
                continue
            # portspecでフィルタ
            if portspec != 'all' and portspec not in name.lower():
                continue
            short = name  # Port1/0/1
            status = iface.get('status', 'notconnect')
            vlan = iface.get('vlan', '1')
            duplex = 'Full' if iface.get('duplex') == 'full' else 'Auto'
            speed = iface.get('speed', 'auto')
            speed_str = f'{speed}M' if speed.isdigit() else 'Auto'
            desc = iface.get('desc', '')[:16]
            lines.append(
                f' {short:<11}{status:<11}{vlan:<6}{duplex:<8}{speed_str:<8}{desc}'
            )
        return '\n'.join(lines)

    def _apresia_show_vlan(self, state: DeviceState) -> str:
        lines = [
            ' VLAN  Name                Status    Ports',
            ' ----- ------------------- --------- ---------------------------',
        ]
        for vid, vinfo in state.vlans.items():
            name = vinfo.get('name', f'VLAN{vid}')[:19]
            ports = ' '.join(vinfo.get('ports', []))[:27]
            lines.append(f' {vid:<6}{name:<20}{"active":<10}{ports}')
        return '\n'.join(lines)

    def _apresia_show_vlan_detail(self, state: DeviceState) -> str:
        lines = []
        for vid, vinfo in state.vlans.items():
            lines.append(f' VLAN ID : {vid}')
            lines.append(f'  Name   : {vinfo.get("name", f"VLAN{vid}")}')
            lines.append(f'  Status : {vinfo.get("status","active")}')
            lines.append(f'  Ports  : {", ".join(vinfo.get("ports",[]))}')
            lines.append('')
        return '\n'.join(lines)

    def _apresia_show_vlan_interface(self, state: DeviceState) -> str:
        lines = [
            ' Interface   Mode     PVID  Allowed VLAN',
            ' ----------- -------- ----- ---------------------------',
        ]
        for name, iface in state.interfaces.items():
            if not name.startswith('Port'):
                continue
            vlan = iface.get('vlan', '1')
            mode = 'trunk' if vlan == 'trunk' else 'access'
            pvid = '1' if vlan == 'trunk' else vlan
            allowed = iface.get('allowed_vlan', '1-4094') if vlan == 'trunk' else pvid
            lines.append(f' {name:<13}{mode:<9}{pvid:<6}{allowed}')
        return '\n'.join(lines)

    def _apresia_show_ipif(self, state: DeviceState) -> str:
        lines = [' IP Interface Table',
                 ' Interface  IP Address       Prefix  Status',
                 ' ---------- ---------------- ------- -------']
        for name, iface in state.interfaces.items():
            if name.startswith('Vlan') and iface.get('ip'):
                lines.append(
                    f' {name:<12}{iface["ip"]:<17}/{iface["prefix"]:<8}'
                    f'{"up" if iface.get("status")=="up" else "down"}'
                )
        if len(lines) == 3:
            lines.append(' (管理IPが設定されていません。config ipif System X.X.X.X/Y で設定してください)')
        return '\n'.join(lines)

    def _apresia_show_arp(self, state: DeviceState) -> str:
        lines = [' ARP Table',
                 ' IP Address       MAC Address        Type     Interface',
                 ' ---------------- ------------------ -------- ---------']
        for e in state.arp_table:
            lines.append(
                f' {e["ip"]:<17}{e["mac"]:<19}{"Dynamic":<9}{e["iface"]}'
            )
        return '\n'.join(lines)

    def _apresia_show_fdb(self, state: DeviceState) -> str:
        lines = [' FDB (Forwarding Database)',
                 ' VLAN  MAC Address          Type     Port',
                 ' ----- -------------------- -------- -----------']
        macs = [
            ('10', '00:1a:2b:3c:4d:5e', 'Dynamic', 'Port1/0/1'),
            ('10', '00:2b:3c:4d:5e:6f', 'Dynamic', 'Port1/0/2'),
            ('1',  'ff:ff:ff:ff:ff:ff', 'Static',  'CPU'),
        ]
        for v, m, t, p in macs:
            lines.append(f' {v:<6}{m:<21}{t:<9}{p}')
        return '\n'.join(lines)

    def _apresia_show_stp(self, state: DeviceState) -> str:
        return (
            f' Spanning Tree: Enabled (RSTP)\n'
            f'\n'
            f' Bridge ID     : Priority 32768  Address 00:1a:2b:3c:4d:00\n'
            f' Root Bridge   : Priority 8192   Address 00:0e:0e:f1:00:01\n'
            f' Root Port     : Port1/0/24   Cost: 4   Hello: 2  MaxAge: 20  FwdDly: 15\n'
            f'\n'
            f' Port        Role       State      Cost   Prio  Type\n'
            f' ----------- ---------- ---------- ------ ----- ----------\n'
            f' Port1/0/1   Designated Forwarding 20000  128   P2P\n'
            f' Port1/0/2   Designated Forwarding 20000  128   P2P\n'
            f' Port1/0/24  Root       Forwarding 20000  128   P2P\n'
        )

    def _apresia_show_loopdetect(self, state: DeviceState) -> str:
        lines = [' Loopback Detection Status: Enabled',
                 '',
                 ' Port        Status     Action',
                 ' ----------- ---------- -------']
        for name, iface in state.interfaces.items():
            if name.startswith('Port') and iface.get('status') == 'connected':
                lines.append(f' {name:<13}{"Normal":<11}Block')
        return '\n'.join(lines)

    def _apresia_show_lldp(self, state: DeviceState) -> str:
        return (
            f' LLDP Neighbor Information\n'
            f'\n'
            f' Port        Chassis ID         System Name    Port Descr\n'
            f' ----------- ------------------ -------------- --------------------\n'
            f' Port1/0/24  00:0e:0e:f1:00:01  Dist-SW        GigabitEthernet1/0/1\n'
        )

    def _apresia_show_running(self, state: DeviceState) -> str:
        lines = [
            f'!',
            f'! ApresiaLightGM200シリーズ  Version 2.03',
            f'!',
            f'hostname {state.hostname}',
            f'!',
        ]
        # 管理IP
        for name, iface in state.interfaces.items():
            if name.startswith('Vlan') and iface.get('ip'):
                lines.append(f'config ipif System {iface["ip"]}/{iface["prefix"]}')
        lines.append('!')
        # ポート設定
        for name, iface in state.interfaces.items():
            if not name.startswith('Port'):
                continue
            vlan = iface.get('vlan', '1')
            desc = iface.get('desc', '')
            lines.append(f'interface {name.lower()}')
            if desc:
                lines.append(f' description {desc}')
            if vlan == 'trunk':
                lines.append(f' switchport mode trunk')
                if iface.get('allowed_vlan'):
                    lines.append(f' switchport trunk allowed vlan {iface["allowed_vlan"]}')
            else:
                lines.append(f' switchport mode access')
                lines.append(f' switchport access vlan {vlan}')
            lines.append(f'!')
        # VLAN
        for vid in state.vlans:
            if vid != 1:
                lines.append(f'vlan {vid}')
                lines.append(f' name {state.vlans[vid].get("name", f"VLAN{vid}")}')
                lines.append(f'!')
        lines.append('end')
        return '\n'.join(lines)

    # ─── save / commit ────────────────────────
    def _cmd_save(self, state):
        if state.device_type == "sir":
            return "設定を保存しました。"
        return "Building configuration...\n[OK]"

    # ─── Stack（3.1章）────────────────────────
    def _show_switch(self, state):
        """show switch — スタック状態表示"""
        lines = [
            "Switch/Stack Mac Address : 0057.d2d4.4d00 - Local Mac Address",
            "Mac persistency wait time: Indefinite",
            "",
            "                                             H/W   Current",
            "Switch#   Role    Mac Address     Priority Version  State",
            "------------------------------------------------------------",
            "*1       Active   0057.d2d4.4d00     15      V01    Ready",
            " 2       Standby  0057.d2d4.4d01      14     V01    Ready",
        ]
        return "\n".join(lines)

    def _show_switch_detail(self, state):
        """show switch detail"""
        lines = [
            "Switch/Stack Mac Address : 0057.d2d4.4d00",
            "",
            "                                             H/W   Current",
            "Switch#   Role    Mac Address     Priority Version  State",
            "------------------------------------------------------------",
            "*1       Active   0057.d2d4.4d00     15      V01    Ready",
            " 2       Standby  0057.d2d4.4d01      14     V01    Ready",
            "",
            "         Stack Port Status             Neighbors",
            "Switch#  Port 1     Port 2           Port 1   Port 2",
            "  1      OK         OK                2        2",
            " 2       OK         OK                1        1",
        ]
        return "\n".join(lines)

    # ─── IOS Management（3.2章）──────────────
    def _show_ntp_status(self, state):
        return ("Clock is synchronized, stratum 3, reference is 192.168.1.1\n"
                "nominal freq is 250.0000 Hz, actual freq is 250.0001 Hz, precision is 2**10\n"
                "ntp uptime is 3600 (1/100 of seconds), resolution is 4004\n"
                "reference time is E3A4B2C1.D5E6F7A8 (10:30:00.835 UTC Mon Jun 13 2026)\n"
                "clock offset is 0.5000 msec, root delay is 1.00 msec\n"
                "root dispersion is 5.00 msec, peer dispersion is 1.75 msec\n"
                "loopfilter state is 'CTRL' (Normal Controlled Loop), drift is 0.000000001 s/s")

    def _show_ntp_associations(self, state):
        lines = [
            "  address         ref clock       st   when   poll reach  delay  offset   disp",
            "*~192.168.1.1    .GPS.           1   58     64  377     1.0    0.50    1.75",
            " ~192.168.2.1    192.168.1.1     2   12    128  377     2.0    0.80    2.50",
            "* sys.peer, # selected, + candidate, - outlyer, x falseticker, ~ configured",
        ]
        return "\n".join(lines)

    def _show_logging(self, state):
        return ("Syslog logging: enabled (0 messages dropped, 0 messages rate-limited,\n"
                "                0 flushes, 0 overruns, xml disabled, filtering disabled)\n\n"
                "No Active Message Discriminator.\n\n"
                "No Inactive Message Discriminator.\n\n"
                "    Console logging: level debugging, 42 messages logged, xml disabled,\n"
                "                     filtering disabled\n"
                "    Monitor logging: level debugging, 0 messages logged, xml disabled,\n"
                "                     filtering disabled\n"
                "    Buffer logging:  level debugging, 42 messages logged, xml disabled,\n"
                "                    filtering disabled\n"
                "    Logging Exception size (8192 bytes)\n"
                "    Count and timestamp logging messages: disabled\n"
                "    Persistent logging: disabled\n\n"
                "Log Buffer (8192 bytes):\n"
                f"*Jun 13 10:30:00.001: %LINK-3-UPDOWN: GigabitEthernet1/0/1, changed state to up\n"
                f"*Jun 13 10:30:01.002: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/1, changed state to up")

    def _show_snmp(self, state):
        return ("Chassis: " + state.hostname + "\n"
                "Contact: admin@example.com\n"
                "Location: Server Room Floor1\n"
                "SNMP agent enabled\n"
                "Community: public (read-only)\n"
                "Community: private (read-write)\n"
                "SNMP v2c: enabled")

    # ─── Security（3.5章）────────────────────
    def _show_port_security(self, state):
        """show port-security"""
        lines = [
            "Secure Port  MaxSecureAddr  CurrentAddr  SecurityViolation  Security Action",
            "               (Count)       (Count)          (Count)",
            "---------------------------------------------------------------------------",
        ]
        for name, iface in state.interfaces.items():
            if name.startswith("GigabitEthernet") and iface.get("status") == "connected":
                short = name.replace("GigabitEthernet","Gi")
                lines.append(f"{short:<13}{'1':<15}{'1':<13}{'0':<19}Restrict")
        lines.append("---------------------------------------------------------------------------")
        lines.append(f"Total Addresses in System (excluding one mac per port)     : 0")
        lines.append(f"Max Addresses limit in System (excluding one mac per port) : 4096")
        return "\n".join(lines)

    def _show_port_security_interface(self, ifname, state):
        """show port-security interface Gi1/0/1"""
        return (f"Port Security              : Enabled\n"
                f"Port Status                : Secure-up\n"
                f"Violation Mode             : Restrict\n"
                f"Aging Time                 : 0 mins\n"
                f"Aging Type                 : Absolute\n"
                f"SecureStatic Address Aging : Disabled\n"
                f"Maximum MAC Addresses      : 1\n"
                f"Total MAC Addresses        : 1\n"
                f"Configured MAC Addresses   : 0\n"
                f"Sticky MAC Addresses       : 0\n"
                f"Last Source Address:Vlan   : 00:1a:2b:3c:4d:5e:10\n"
                f"Security Violation Count   : 0")

    def _show_dhcp_snooping(self, state):
        """show ip dhcp snooping"""
        lines = [
            "Switch DHCP snooping is enabled",
            "Switch DHCP gleaning is disabled",
            "DHCP snooping is configured on following VLANs:",
            "10",
            "DHCP snooping is operational on following VLANs:",
            "10",
            "DHCP snooping is configured on the following L3 Interfaces:",
            "",
            "Insertion of option 82 is enabled",
            "   circuit-id default format: vlan-mod-port",
            "   remote-id: 0057.d2d4.4d00 (MAC)",
            "Option 82 on untrusted port is not allowed",
            "Verification of hwaddr field is enabled",
            "Verification of giaddr field is enabled",
            "DHCP snooping trust/rate is configured on the following Interfaces:",
            "",
            "Interface          Trusted    Allow option    Rate limit (pps)",
            "-----------  -------  ------------    ----------------",
            "Gi1/0/24      yes        yes             unlimited",
        ]
        return "\n".join(lines)

    def _show_dai(self, state):
        """show ip arp inspection — Dynamic ARP Inspection"""
        lines = [
            " Vlan        Configuration    Operation   ACL Match          Static ACL",
            " ----        -------------    ---------   ---------          ----------",
            "   10        Enabled          Active      Implicit Deny      No",
            "",
            " Vlan        ACL Logging      DHCP Logging      Probe Logging",
            " ----        -----------      ------------      -------------",
            "   10        Deny             Deny              Off",
            "",
            " Vlan      Forwarded        Dropped     DHCP Drops      ACL Drops",
            " ----      ---------        -------     ----------      ---------",
            "   10              0              0              0              0",
        ]
        return "\n".join(lines)

    def _validate_command(self, cmd: str, c: str, state) -> str:
        """
        コマンドを検証してエラーメッセージを返す。
        問題なければNoneを返す。

        検証項目:
        1. Ambiguous: 前方一致が複数（"s" → show/spanning-tree/standby...）
        2. Incomplete: 必須パラメータが不足
        3. Mode: 現在のモードでは実行不可
        4. Range: 数値が許容範囲外
        5. 存在しないコマンド（後続のprocess()ロジックに任せるためNone）
        """
        dt = state.device_type
        mode = state.mode
        tokens = c.split()
        if not tokens:
            return None
        first = tokens[0]

        # ── Ambiguous チェック（先頭トークンが複数候補にマッチ） ──
        if dt in ('catalyst', 'cisco', 'srs', 'nexus'):
            # execモードでの先頭コマンド候補
            exec_cmds = ['show','ping','traceroute','clear','debug','undebug',
                         'configure','enable','disable','write','copy','reload',
                         'logout','exit','end','interface','router','vlan',
                         'spanning-tree','ip','no','do','clock','feature',
                         'vpc','snmp-server','logging','ntp','banner','service',
                         'hostname','crypto','aaa','username','line','access-list',
                         'standby','vrrp','channel-group']
            if mode == 'exec':
                candidates = [k for k in exec_cmds if k.startswith(first) and k != first]
                # 展開されていない（略称のまま残った）場合はambiguous
                if len(candidates) >= 2 and first not in exec_cmds:
                    return self._ambiguous_error(cmd, state, candidates)

        # ── Incomplete チェック（必須パラメータなし） ──
        incomplete_rules = {
            # (先頭コマンド, モード): 必要な最低トークン数
            ('interface', 'config'): 2,      # interface GigabitEthernet...
            ('interface', 'exec'):   2,
            ('ip address', 'config-if'): 4,  # ip address x.x.x.x mask
            ('ip route', 'config'): 4,       # ip route net mask gw
            ('vlan', 'config'): 2,           # vlan 10
            ('neighbor', 'config-router'): 3, # neighbor x.x.x.x remote-as N
            ('network', 'config-router'): 3,  # network x.x.x.x x.x.x.x area N (OSPF)
            ('username', 'config'): 3,        # username xxx privilege N
        }
        # 先頭2トークンのコンテキストチェック
        ctx = ' '.join(tokens[:2]) if len(tokens) >= 2 else tokens[0]
        for (req_cmd, req_mode), min_toks in incomplete_rules.items():
            if (c.startswith(req_cmd) and
                    (req_mode == mode or req_mode == '*') and
                    len(tokens) < min_toks):
                return self._incomplete_error(cmd, state)

        # ── Mode チェック（execモードでconfig専用コマンドを打った場合） ──
        config_only_cmds = [
            'hostname','interface','router ospf','router rip','router bgp',
            'spanning-tree mode','vlan','ip route','snmp-server','logging host',
            'ntp server','banner','service','username','line','access-list',
            'crypto','aaa','standby','vrrp','channel-group','switchport',
            'shutdown','no shutdown','ip address','channel-group',
            'spanning-tree portfast','spanning-tree bpduguard',
            'spanning-tree guard','vpc domain','feature',
        ]
        if mode == 'exec' and dt in ('catalyst', 'cisco', 'srs', 'nexus'):
            for cc in config_only_cmds:
                if c.startswith(cc) and cc not in ['ip', 'no']:
                    # exitやendは除外
                    if c not in ('exit','end','quit'):
                        return self._mode_error(cmd, state, 'config')

        # ── Range チェック（数値範囲）──
        if dt in ('catalyst', 'cisco', 'srs', 'nexus'):
            # vlan <id>: 1-4094
            import re
            m_vlan = re.match(r'^vlan\s+(\d+)', c)
            if m_vlan:
                vid = int(m_vlan.group(1))
                if not (1 <= vid <= 4094):
                    return self._range_error(cmd, state, 'VLAN ID', 1, 4094, vid)

            # spanning-tree vlan <id> priority <value>: 0-61440, 4096の倍数のみ
            m_stp_pri = re.match(r'^spanning-tree\s+vlan\s+\d+\s+priority\s+(\d+)', c)
            if m_stp_pri:
                pri = int(m_stp_pri.group(1))
                if not (0 <= pri <= 61440) or pri % 4096 != 0:
                    return self._range_error(
                        cmd, state,
                        'STP priority', '0', '61440 (must be multiple of 4096)', pri
                    )

            # spanning-tree vlan <id> root primary|secondary
            m_stp_root = re.match(r'^spanning-tree\s+vlan\s+(\d+)\s+root\s+(primary|secondary)', c)
            # OK

            # ip ospf priority <0-255>
            m_ospf_pri = re.match(r'^ip\s+ospf\s+priority\s+(\d+)', c)
            if m_ospf_pri:
                pri = int(m_ospf_pri.group(1))
                if not (0 <= pri <= 255):
                    return self._range_error(cmd, state, 'OSPF priority', 0, 255, pri)

            # router bgp <AS>: 1-65535
            m_bgp_as = re.match(r'^router\s+bgp\s+(\d+)', c)
            if m_bgp_as:
                asn = int(m_bgp_as.group(1))
                if not (1 <= asn <= 4294967295):
                    return self._range_error(cmd, state, 'AS number', 1, 4294967295, asn)

            # channel-group <1-48> mode active|passive|on
            m_ch = re.match(r'^channel-group\s+(\d+)', c)
            if m_ch:
                ch_id = int(m_ch.group(1))
                if not (1 <= ch_id <= 48):
                    return self._range_error(cmd, state, 'channel-group', 1, 48, ch_id)

            # vrrp <1-255>
            m_vrrp = re.match(r'^vrrp\s+(\d+)', c)
            if m_vrrp:
                gid = int(m_vrrp.group(1))
                if not (1 <= gid <= 255):
                    return self._range_error(cmd, state, 'VRRP group', 1, 255, gid)

            # standby <0-255>
            m_hsrp = re.match(r'^standby\s+(\d+)', c)
            if m_hsrp:
                gid = int(m_hsrp.group(1))
                if not (0 <= gid <= 255):
                    return self._range_error(cmd, state, 'HSRP group', 0, 255, gid)

        return None  # エラーなし

    def _validate_command_sir(self, cmd: str, c: str, state) -> str:
        """Si-R固有のバリデーション"""
        tokens = c.split()
        if not tokens:
            return None
        # ip rip network にはアドレスが必要
        import re
        if re.match(r'^ip\s+rip\s+network$', c):
            return self._incomplete_error(cmd, state)
        return None

    
    def _unknown(self, cmd: str, state) -> str:
        """不明コマンドの汎用フォールバック → _cmd_error へ委譲"""
        return self._cmd_error(cmd, state, reason='invalid')

    def _cmd_error(self, cmd: str, state, reason: str = 'invalid',
                   position: int = None, value: str = None) -> str:
        """
        ベンダー別CLIエラーメッセージを生成する。

        reason:
          'invalid'    → コマンド不明 / タイプミス
          'ambiguous'  → 前方一致が複数
          'incomplete' → パラメータ不足
          'mode'       → 現在のモードでは使用不可
          'range'      → 値が範囲外
          'exists'     → 既に存在する
          'notfound'   → 対象が見つからない
          'permission' → 権限不足
        """
        dt = state.device_type

        # ── Cisco IOS / Catalyst / SR-S ──────────────────
        if dt in ('catalyst', 'cisco', 'srs'):
            if reason == 'invalid':
                # タイプミスの場合: どこでエラーになったかを ^ で示す
                pos = position if position is not None else len(cmd)
                spaces = ' ' * (2 + pos)
                return (
                    f'% Invalid input detected at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'{spaces}^'
                )
            elif reason == 'ambiguous':
                tok = cmd.strip().split()[0] if cmd.strip() else cmd
                return f'% Ambiguous command: "{tok}"'
            elif reason == 'incomplete':
                return '% Incomplete command.'
            elif reason == 'mode':
                return (
                    f'% Invalid input detected at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'  ^'
                    f'\n% (Command not available in current mode)'
                )
            elif reason == 'range':
                return f'% Value out of range. {value or ""}'
            elif reason == 'exists':
                return f'% {value or "Object already exists."}'
            elif reason == 'notfound':
                return f'% {value or "Object not found."}'
            elif reason == 'permission':
                return '% Authorization failed.'
            else:
                return (
                    f'% Invalid input detected at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'  ^'
                )

        # ── NX-OS ────────────────────────────────────────
        elif dt == 'nexus':
            if reason == 'invalid':
                pos = position if position is not None else len(cmd)
                spaces = ' ' * (2 + pos)
                return (
                    f'% Invalid command at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'{spaces}^'
                )
            elif reason == 'ambiguous':
                tok = cmd.strip().split()[0] if cmd.strip() else cmd
                return f'% Ambiguous command: "{tok}"'
            elif reason == 'incomplete':
                return '% Incomplete command at \'^\' marker.'
            elif reason == 'mode':
                return (
                    f'% Invalid command at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'  ^\n'
                    f'% Command not available in current mode'
                )
            elif reason == 'range':
                return f'% Invalid value: {value or "out of range"}'
            elif reason == 'permission':
                return '% Permission denied.'
            else:
                return (
                    f'% Invalid command at \'^\' marker.\n'
                    f'  {cmd}\n'
                    f'  ^'
                )

        # ── Si-R ─────────────────────────────────────────
        elif dt == 'sir':
            if reason == 'invalid':
                return '% Unknown command.'
            elif reason == 'ambiguous':
                return '% Ambiguous command.'
            elif reason == 'incomplete':
                return '% Incomplete command.'
            elif reason == 'mode':
                return '% Command not available in this mode.'
            elif reason == 'range':
                return f'% Invalid value. {value or ""}'
            elif reason == 'notfound':
                return f'% Not found. {value or ""}'
            else:
                return '% Unknown command.'

        # ── SR-S ─────────────────────────────────────────
        elif dt == 'srs':
            if reason == 'invalid':
                return f'% Unknown command: {cmd.split()[0] if cmd.strip() else cmd}'
            elif reason == 'incomplete':
                return '% Incomplete command.'
            elif reason == 'range':
                return f'% Value out of range. {value or ""}'
            else:
                return f'% Unknown command: {cmd.split()[0] if cmd.strip() else cmd}'

        # ── APRESIA ──────────────────────────────────────
        elif dt == 'apresia':
            if reason == 'invalid':
                return f'% Error: Unknown command - {cmd.split()[0] if cmd.strip() else cmd}'
            elif reason == 'ambiguous':
                return '% Error: Ambiguous command.'
            elif reason == 'incomplete':
                return '% Error: Incomplete command.'
            elif reason == 'mode':
                return '% Error: Command not available in current mode.'
            elif reason == 'range':
                return f'% Error: Value out of range. {value or ""}'
            else:
                return f'% Error: Unknown command.'

        # フォールバック
        return f'% Unknown command.'

    def _ambiguous_error(self, cmd: str, state, candidates: list) -> str:
        """候補が複数ある曖昧なコマンドエラー（実機表示準拠）"""
        dt = state.device_type
        tok = cmd.strip().split()[0] if cmd.strip() else cmd
        if dt in ('catalyst', 'cisco', 'srs', 'nexus'):
            lines = [f'% Ambiguous command: "{tok}"']
            for c in candidates[:6]:
                lines.append(f'  {c}')
            return '\n'.join(lines)
        elif dt == 'sir':
            return '% Ambiguous command.'
        elif dt == 'apresia':
            return '% Error: Ambiguous command.'
        return '% Ambiguous command.'

    def _incomplete_error(self, cmd: str, state) -> str:
        """パラメータ不足エラー"""
        return self._cmd_error(cmd, state, reason='incomplete')

    def _range_error(self, cmd: str, state, field: str, min_v, max_v, got=None) -> str:
        """値範囲エラー"""
        detail = f'{field}: allowed {min_v}-{max_v}'
        if got is not None:
            detail += f', got {got}'
        return self._cmd_error(cmd, state, reason='range', value=detail)

    def _mode_error(self, cmd: str, state, required_mode: str = '') -> str:
        """モードエラー（configモードで実行が必要など）"""
        dt = state.device_type
        if dt in ('catalyst', 'cisco', 'srs', 'nexus'):
            hint = f' Use in {required_mode} mode.' if required_mode else ''
            return (
                f'% Invalid input detected at \'^\' marker.\n'
                f'  {cmd}\n'
                f'  ^'
            )
        return self._cmd_error(cmd, state, reason='mode')


# ══════════════════════════════════════════
# CLI補完・ヘルプエンジン（機種別）
# ══════════════════════════════════════════
class CliCompletion:
    """
    実機準拠の CLI 補完・ヘルプ

    Cisco IOS/NX-OS:
      SW# show ?         → showのサブコマンド一覧
      SW# show ip ?      → show ipのサブコマンド一覧
      SW# sh<Tab>        → show に補完
      SW(config)# rout?  → router に補完

    Si-R:
      Router# ip rip ?   → ip ripのサブコマンド一覧
      Router# ?          → 全コマンド一覧

    APRESIA:
      AP# config ?       → configのサブコマンド一覧
    """

    # ── Cisco IOS/Catalyst コマンド定義 ──────────────────
    IOS_EXEC = {
        'show': {
            '_desc': 'Show running system information',
            'version':          'System hardware and software status',
            'running-config':   'Current operating configuration',
            'startup-config':   'Contents of startup configuration',
            'ip': {
                '_desc': 'IP information',
                'route':        'IP routing table',
                'interface':    'IP interface status and configuration',
                'ospf': {
                    '_desc': 'OSPF information',
                    'neighbor':   'Neighbor list',
                    'database':   'OSPF database summary',
                    'interface':  'Interface information',
                    'route':      'OSPF local routing table',
                    'border-routers': 'ABR and ASBR information',
                },
                'rip': {
                    '_desc': 'RIP routing protocol information',
                    'neighbor': 'RIP neighbor list',
                    'route':    'RIP routing table',
                    'status':   'RIP protocol status',
                },
                'bgp': {
                    '_desc': 'BGP information',
                    'summary':  'Summary of BGP neighbor status',
                    'neighbor': 'Detailed information on TCP and BGP connections',
                    'table':    'BGP table',
                },
                'arp':          'IP ARP table',
                'access-lists': 'List IP access lists',
                'prefix-list':  'List prefix lists',
            },
            'interfaces':       'Interface status and configuration',
            'vlan':             'VTP VLAN status',
            'spanning-tree':    'Spanning tree topology',
            'etherchannel': {
                '_desc':        'EtherChannel information',
                'summary':      'One-line summary per channel-group',
                'detail':       'Detailed information of channel-groups',
                'port-channel': 'Port-channel information',
                'port':         'EtherChannel port information',
            },
            'vrrp': {
                '_desc': 'VRRP information',
                'brief': 'Brief status of VRRP groups',
                'all':   'All VRRP groups',
            },
            'standby': {
                '_desc': 'HSRP information',
                'brief': 'Brief status of Hot standby groups',
                'all':   'All standby group information',
            },
            'vpc': {
                '_desc':              'vPC information (NX-OS)',
                'brief':              'vPC brief status',
                'peer-keepalive':     'vPC peer-keepalive status',
                'role':               'vPC role information',
                'consistency-parameters': 'vPC consistency check',
            },
            'mac': {
                '_desc': 'MAC address',
                'address-table': 'MAC address table',
            },
            'arp':              'ARP table',
            'cdp':              'CDP information',
            'lldp':             'LLDP information',
            'clock':            'Display the system clock',
            'logging':          'Show the contents of logging buffers',
            'processes':        'Active process statistics',
            'ntp':              'NTP associations',
        },
        'ping':         'Send echo messages',
        'traceroute':   'Trace route to destination',
        'telnet':       'Open a telnet connection',
        'ssh':          'Open a secure shell client connection',
        'clear': {
            '_desc': 'Reset functions',
            'ip': {
                '_desc': 'IP functions',
                'route':        'Delete route table entries',
                'ospf':         'OSPF clear commands',
                'bgp':          'Clear BGP connections',
                'arp':          'Delete ARP table entries',
            },
            'arp':              'Delete entries from the ARP table',
            'mac':              'MAC address table information',
            'spanning-tree':    'Clear spanning-tree counters',
            'counters':         'Clear counters on one or all interfaces',
        },
        'debug': {
            '_desc': 'Debugging functions (see also \'undebug\')',
            'ip': {
                '_desc': 'IP information',
                'ospf':     'OSPF information',
                'rip':      'RIP protocol transactions',
                'bgp':      'BGP information',
                'routing':  'Routing table events',
            },
            'spanning-tree': 'Spanning tree information',
        },
        'undebug':      'Disable debugging functions',
        'configure': {
            '_desc':    'Enter configuration mode',
            'terminal': 'Enter configuration mode (interactive)',
        },
        'enable':       'Turn on privileged commands',
        'disable':      'Turn off privileged commands',
        'write': {
            '_desc':    'Write running configuration to memory or network',
            'memory':   'Write to NV memory',
            'terminal': 'Write to terminal',
            'erase':    'Erase startup configuration',
        },
        'copy':         'Copy from one file to another',
        'reload':       'Halt and perform a cold restart',
        'logout':       'Exit from the EXEC',
        'exit':         'Exit from the EXEC',
        'end':          'Return to privileged EXEC mode',
    }

    IOS_CONFIG = {
        'hostname':     'Set system\'s network name',
        'interface': {
            '_desc': 'Select an interface to configure',
            'GigabitEthernet': 'GigabitEthernet IEEE 802.3z',
            'Port-channel':    'Ethernet Channel of interfaces',
            'Vlan':            'Catalyst Vlans',
            'Loopback':        'Loopback interface',
            'Tunnel':          'Tunnel interface',
        },
        'router': {
            '_desc': 'Enable a routing process',
            'ospf':   'Open Shortest Path First (OSPF)',
            'rip':    'Routing Information Protocol (RIP)',
            'bgp':    'Border Gateway Protocol (BGP)',
            'eigrp':  'Enhanced Interior Gateway Routing Protocol (EIGRP)',
        },
        'ip': {
            '_desc': 'Global IP configuration subcommands',
            'route':              'Establish static routes',
            'routing':            'Enable IP routing',
            'access-list':        'Named access-list',
            'prefix-list':        'Build a prefix list',
            'ospf':               'OSPF configuration',
            'rip':                'RIP configuration',
            'name-server':        'Specify address of name server to use',
            'default-gateway':    'Specify default gateway (if not routing IP)',
            'ssh':                'Configure ssh options',
            'http':               'HTTP server configuration',
        },
        'vlan':                   'Add, delete, or modify values associated with a VLAN',
        'spanning-tree': {
            '_desc': 'Spanning Tree Subsystem',
            'mode':             'Spanning tree operating mode',
            'vlan':             'VLAN Switch Spanning Tree',
            'portfast':         'Spanning tree portfast options',
            'guard':            'Change an interface\'s spanning tree guard mode',
        },
        'access-list':            'Add an access list entry',
        'line': {
            '_desc': 'Configure a terminal line',
            'con':  'Primary terminal line',
            'vty':  'Virtual terminal',
            'aux':  'Auxiliary line',
        },
        'username':               'Establish User Name Authentication',
        'enable':                 'Modify enable password parameters',
        'service':                'Set a microcode service',
        'logging': {
            '_desc': 'Modify message logging facilities',
            'host':         'Set syslog server IP',
            'trap':         'Logging trap (severity)',
            'buffered':     'Set buffered logging parameters',
            'console':      'Set console logging parameters',
        },
        'snmp-server': {
            '_desc': 'Modify SNMP parameters',
            'community':    'Enable SNMP; set community string and access privs',
            'host':         'Specify hosts to receive SNMP notifications',
            'location':     'Text for mib object sysLocation',
            'contact':      'Text for mib object sysContact',
        },
        'ntp': {
            '_desc': 'Configure NTP',
            'server':       'Configure NTP server',
            'authenticate': 'Authenticate time sources',
        },
        'banner':                 'Define a login banner',
        'crypto':                 'Encryption module',
        'aaa':                    'Authentication, Authorization and Accounting',
        'no':                     'Negate a command or set its defaults',
        'do':                     'To run exec commands in config mode',
        'exit':                   'Exit from configure mode',
        'end':                    'Exit to privileged EXEC mode',
    }

    IOS_CONFIG_IF = {
        'ip': {
            '_desc': 'Interface Internet Protocol config commands',
            'address':          'Set the IP address of an interface',
            'ospf': {
                '_desc': 'OSPF interface commands',
                'priority':     'Router priority',
                'cost':         'Interface cost',
                'hello-interval': 'Time between HELLO packets',
                'dead-interval':  'Interval after which a neighbor is dead',
                'network':      'Network type',
                'passive':      'Suppress routing updates on an interface',
            },
            'rip':              'Router Information Protocol',
            'access-group':     'Specify access control for packets',
        },
        'switchport': {
            '_desc': 'Set switching mode characteristics of the Layer2 interface',
            'mode': {
                '_desc': 'Set trunking mode of the interface',
                'access':   'Set trunking mode to ACCESS unconditionally',
                'trunk':    'Set trunking mode to TRUNK unconditionally',
            },
            'access': {
                '_desc': 'Set access mode characteristics of the interface',
                'vlan':     'Set VLAN when interface is in access mode',
            },
            'trunk': {
                '_desc': 'Set trunk characteristics when interface is in trunking mode',
                'allowed': {
                    '_desc': 'Set allowed VLANs when interface is in trunking mode',
                    'vlan':     'Set allowed VLANs',
                },
                'native': {
                    '_desc': 'Set trunking native characteristics',
                    'vlan': 'Set native VLAN when interface is in trunking mode',
                },
                'encapsulation': 'Set encapsulation type for an interface',
            },
        },
        'channel-group': {
            '_desc': 'Etherchannel/port bundling function',
            '<1-48>': 'Channel group number',
        },
        'spanning-tree': {
            '_desc': 'Spanning Tree Subsystem',
            'portfast':     'Spanning tree portfast options',
            'bpduguard':    'Don\'t accept BPDUs on this interface',
            'guard': {
                '_desc': 'Change an interface\'s spanning tree guard mode',
                'root': 'Enable root guard on this interface',
            },
            'cost':         'Change an interface\'s spanning tree port path cost',
            'priority':     'Change an interface\'s spanning tree port priority',
        },
        'vrrp': {
            '_desc': 'VRRP interface configuration',
            '<1-255>': 'Group number',
        },
        'standby': {
            '_desc': 'HSRP interface configuration',
            '<0-255>': 'Group number',
        },
        'description':    'Interface specific description',
        'shutdown':       'Shutdown the selected interface',
        'no':             'Negate a command or set its defaults',
        'duplex':         'Configure duplex operation',
        'speed':          'Configure speed operation',
        'mtu':            'Set the interface Maximum Transmission Unit (MTU)',
        'exit':           'Exit from interface configuration mode',
        'end':            'Return to privileged EXEC mode',
    }

    # ── NX-OS 追加コマンド ──────────────────────────
    NXOS_EXTRA_EXEC = {
        'show': {
            'vpc':          'Virtual Port Channel information',
            'feature':      'Show feature status',
            'module':       'Show module information',
            'topology':     'Show topology information',
            'port-channel': 'Port-channel information',
        },
        'feature':  'Enable/disable features',
    }

    NXOS_EXTRA_CONFIG = {
        'feature': {
            '_desc': 'Enable a feature',
            'vpc':      'Enable Virtual Port Channel (vPC)',
            'lacp':     'Enable Link Aggregation Control Protocol',
            'ospf':     'Enable OSPF routing protocol',
            'bgp':      'Enable BGP routing protocol',
            'bfd':      'Enable Bidirectional Forwarding Detection',
            'lldp':     'Enable LLDP',
            'dhcp':     'Enable DHCP snooping',
        },
        'vpc': {
            '_desc': 'Configure vPC',
            'domain':    'Configure vPC domain',
            'peer-link': 'Configure vPC peer-link (in interface mode)',
            '<1-4096>':  'Configure a vPC',
        },
    }

    NXOS_CONFIG_VPC = {
        'role':             'Configure vPC role priority',
        'peer-keepalive':   'Configure vPC peer-keepalive link',
        'peer-gateway':     'Enable vPC peer-gateway',
        'peer-switch':      'Enable vPC peer-switch',
        'auto-recovery':    'Enable vPC auto-recovery',
        'system-priority':  'Configure vPC system priority',
        'system-mac':       'Configure vPC system MAC address',
        'ip':               'IP configuration for vPC keepalive',
        'delay':            'Configure delay for restoring vPCs',
        'exit':             'Exit from vpc-domain configuration mode',
    }

    # ── Si-R コマンド定義 ─────────────────────────────
    SIR_EXEC = {
        'show': {
            '_desc': 'Show current system status',
            'ip': {
                '_desc': 'IP configuration',
                'route':        'Routing table',
                'rip': {
                    '_desc': 'RIP status',
                    'route':    'RIP routing table',
                    'neighbor': 'RIP neighbor list',
                    'status':   'RIP protocol status',
                },
                'ospf': {
                    '_desc': 'OSPF status',
                    'neighbor':   'OSPF neighbor list',
                    'database':   'OSPF database',
                    'interface':  'OSPF interface status',
                    'route':      'OSPF routing table',
                },
                'bgp': {
                    '_desc': 'BGP status',
                    'summary':  'BGP session summary',
                    'neighbor': 'BGP neighbor detail',
                },
            },
            'vrrp':     'VRRP status',
            'version':  'Show version',
        },
        'ip': {
            '_desc': 'IP configuration commands',
            'rip': {
                '_desc': 'RIP configuration',
                'use':      'Enable RIP (ip rip use use)',
                'network':  'Specify RIP network (ip rip network <net>/<prefix>)',
                'neighbor': 'Specify RIP neighbor',
                'version':  'RIP version',
            },
            'ospf': {
                '_desc': 'OSPF interface commands',
                'priority': 'OSPF priority',
                'cost':     'OSPF cost',
            },
            'route':        'Add static route (ip route <dest>/<prefix> <gw>)',
        },
        'ping':         'Send echo request',
        'traceroute':   'Trace route',
        'configure': {
            '_desc':    'Enter configuration mode',
            'terminal': 'Enter configuration mode (interactive)',
        },
        'conf':         'Enter configuration mode (short)',
        'exit':         'Exit',
        'save':         'Save configuration',
    }

    SIR_CONFIG = {
        'hostname':     'Set system hostname',
        'lan': {
            '_desc': 'LAN interface configuration',
            '<0-3>': 'LAN port number',
        },
        'wan': {
            '_desc': 'WAN interface configuration',
            '<0-3>': 'WAN port number',
        },
        'ip': {
            '_desc': 'IP configuration',
            'route':            'Set static route',
            'rip': {
                '_desc': 'RIP configuration',
                'use':          'Enable RIP (ip rip use use)',
                'network':      'Set RIP network',
                'neighbor':     'Set RIP neighbor',
                'version':      'Set RIP version',
            },
            'ospf': {
                '_desc': 'OSPF configuration',
                'priority': 'Set OSPF priority',
                'cost':     'Set OSPF cost',
            },
        },
        'ospf': {
            '_desc': 'OSPF global configuration',
            'use':          'Enable OSPF',
            'router-id':    'Set OSPF router ID',
        },
        'router': {
            '_desc': 'Routing protocol configuration',
            'ospf': 'Configure OSPF',
            'bgp':  'Configure BGP',
            'rip':  'Configure RIP',
        },
        'vrrp': {
            '_desc': 'VRRP configuration',
            'use': 'Enable VRRP',
        },
        'syslog':       'Syslog configuration',
        'consoleinfo':  'Console configuration',
        'telnetinfo':   'Telnet server configuration',
        'exit':         'Exit configuration mode',
        'save':         'Save configuration',
    }

    # ── APRESIA コマンド定義 ──────────────────────────
    APRESIA_EXEC = {
        'show': {
            '_desc': 'Show system status',
            'ipif':         'Show IP interface',
            'iproute':      'Show IP routing table',
            'vlan':         'Show VLAN information',
            'fdb':          'Show forwarding database',
            'stp':          'Show spanning tree status',
            'ports':        'Show port status',
            'switch':       'Show switch information',
            'lacp':         'Show LACP status',
            'management':   'Show management information',
        },
        'ping':             'Send ping',
        'tracert':          'Trace route',
        'config':           'Enter configuration mode',
        'configure':        'Enter configuration mode',
        'save':             'Save configuration',
        'logout':           'Logout',
    }

    # APRESIA config <subcommand> ヘルプ（exec mode で config ? と打った場合）
    APRESIA_CONFIG_SUBCMDS = {
        'ipif': {
            '_desc': 'Configure IP interface',
            'System': 'Configure system IP address (config ipif System <ip>/<prefix>)',
        },
        'iproute':      'Configure static IP route',
        'vlan':         'Configure VLAN settings',
        'stp': {
            '_desc': 'Configure spanning tree',
            'ports':    'STP port configuration',
        },
        'lacp':         'Configure LACP',
        'ports': {
            '_desc': 'Configure port settings',
            '<port>':   'Port number (e.g. 1/0/1)',
        },
        'snmp':         'Configure SNMP parameters',
        'syslog':       'Configure syslog parameters',
        'management':   'Configure management IP address',
    }

    APRESIA_CONFIG = {
        'config': {
            '_desc': 'System configuration',
            'ipif':         'Configure IP interface',
            'iproute':      'Configure static route',
            'vlan':         'Configure VLAN',
            'stp':          'Configure spanning tree',
            'lacp':         'Configure LACP',
            'ports':        'Configure port settings',
            'snmp':         'Configure SNMP',
            'syslog':       'Configure syslog',
            'management':   'Configure management IP',
        },
        'create': {
            '_desc': 'Create a configuration item',
            'vlan':         'Create VLAN',
            'iproute':      'Create static route',
            'account':      'Create user account',
        },
        'delete': {
            '_desc': 'Delete a configuration item',
            'vlan':         'Delete VLAN',
            'iproute':      'Delete static route',
        },
        'enable':           'Enable a feature',
        'disable':          'Disable a feature',
        'exit':             'Exit configuration mode',
        'save':             'Save configuration',
    }

    def get_help(self, cmd: str, state: DeviceState) -> str:
        """? を押したときのヘルプ表示（実機Cisco準拠）"""
        device_type = state.device_type
        mode = state.mode
        tree = self._get_tree(device_type, mode)
        if not tree:
            return ''
        # ?を除去してトークン分割
        query = cmd.rstrip('?').strip()
        tokens = query.split() if query else []
        # サブツリーを辿る（前方一致）
        node = tree
        for tok in tokens:
            matched = self._find_match(node, tok)
            if matched:
                if isinstance(node[matched], dict):
                    node = node[matched]
                else:
                    return f'  {matched:<22}  {node[matched]}'
            else:
                break
        return self._format_help(node, device_type, tokens)

    def complete(self, partial: str, state: DeviceState) -> str:
        """Tab補完: 一意に決まる場合のみ補完"""
        device_type = state.device_type
        mode = state.mode
        tree = self._get_tree(device_type, mode)
        if not tree:
            return partial
        tokens = partial.strip().split()
        if not tokens:
            return partial
        completing = tokens[-1].lower()
        prefix_tokens = tokens[:-1]
        # 前のトークンでサブツリーを辿る
        node = tree
        resolved = []
        for tok in prefix_tokens:
            matched = self._find_match(node, tok)
            if matched:
                resolved.append(matched)
                if isinstance(node[matched], dict):
                    node = node[matched]
                else:
                    return partial
            else:
                resolved.append(tok)
        # 候補を探す
        candidates = [k for k in node if not k.startswith('_') and k.lower().startswith(completing)]
        if len(candidates) == 1:
            return ' '.join(resolved + [candidates[0]]) + ' '
        return partial

    def _find_match(self, node: dict, tok: str) -> str:
        tok_lower = tok.lower()
        for key in node:
            if key.startswith('_'):
                continue
            if key.lower() == tok_lower or key.lower().startswith(tok_lower):
                return key
        return ''

    def _format_help(self, node: dict, device_type: str, context: list) -> str:
        """ヘルプ一覧を実機形式でフォーマット"""
        if not isinstance(node, dict):
            return ''
        lines = []
        for key, val in sorted(node.items()):
            if key.startswith('_'):
                continue
            if isinstance(val, dict):
                desc = val.get('_desc', key)
            else:
                desc = val if isinstance(val, str) else key
            lines.append(f'  {key:<22}  {desc}')
        if not lines:
            return '% No completions available'
        return '\n'.join(lines)

    def _get_tree(self, device_type: str, mode: str) -> dict:
        """機種・モード別のコマンドツリーを返す"""
        if device_type in ('catalyst', 'cisco', 'srs', 'nexus'):
            if mode == 'exec':
                base = dict(self.IOS_EXEC)
                if device_type == 'nexus':
                    for k, v in self.NXOS_EXTRA_EXEC.items():
                        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                            base[k] = {**base[k], **v}
                        else:
                            base[k] = v
                return base
            elif mode == 'config':
                base = dict(self.IOS_CONFIG)
                if device_type == 'nexus':
                    base.update(self.NXOS_EXTRA_CONFIG)
                return base
            elif mode == 'config-if':
                return dict(self.IOS_CONFIG_IF)
            elif mode == 'config-vpc-domain':
                return dict(self.NXOS_CONFIG_VPC)
            elif mode in ('config-router', 'config-vlan'):
                return {
                    'network':          'Specify a network to announce via routing protocol',
                    'neighbor':         'Specify a neighbor router',
                    'passive-interface':'Suppress routing updates on an interface',
                    'redistribute':     'Redistribute information from another routing protocol',
                    'default-information': 'Control distribution of default information',
                    'area':             'OSPF area parameters',
                    'version':          'Set routing protocol version',
                    'no':               'Negate a command or set its defaults',
                    'exit':             'Exit from routing protocol configuration mode',
                }
        elif device_type == 'sir':
            if mode == 'exec':
                return dict(self.SIR_EXEC)
            else:
                return dict(self.SIR_CONFIG)
        elif device_type == 'apresia':
            if mode == 'exec':
                tree = dict(self.APRESIA_EXEC)
                # config ? のサブコマンドも exec から辿れるように
                tree['config'] = dict(self.APRESIA_CONFIG_SUBCMDS)
                return tree
            else:
                return dict(self.APRESIA_CONFIG)
        return {}


cli_completion = CliCompletion()
