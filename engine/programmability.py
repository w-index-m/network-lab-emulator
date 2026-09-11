"""
プログラマビリティ第3弾: EEM / アプリケーションホスティング / OpenFlow

- EemEngine        … Embedded Event Manager（applet / Pythonポリシー）
- AppHostingEngine … Catalyst 9000 のアプリケーションホスティング（IOx/Docker）
- OpenflowEngine   … OpenFlow（faucetパイプライン）

いずれも実機のコマンド体系・状態遷移・検証（前提条件を満たさない操作の
拒否）を再現する。EEMの `action N cli command "..."` だけは表示用の
モックではなく、**実際にルールエンジンへコマンドを流して実行する**ため、
applet から装置の設定を変えられる。

出典: cisco.com はegressプロキシでブロックされているため、コマンド構文と
show出力の書式は二次情報（各docs/*.md の「参考」を参照）で確認している。
"""

import re
import time
from collections import OrderedDict


# ══════════════════════════════════════════
# EEM（Embedded Event Manager）
# ══════════════════════════════════════════
class EemEngine:
    """event manager applet / policy

    applet は「イベント検出子(event)」と「アクション(action)」の組。
    `event none` のappletは `event manager run <name>` で手動実行できる
    （実機でもappletのテストに使う定石）。
    """

    def __init__(self):
        # device_id -> {applet名 -> dict}
        self.applets = {}
        # device_id -> {ファイル名 -> dict}
        self.policies = {}
        # device_id -> [履歴]
        self.history = {}
        # device_id -> ユーザポリシーディレクトリ
        self.policy_dir = {}

    def _dev(self, device_id):
        self.applets.setdefault(device_id, OrderedDict())
        self.policies.setdefault(device_id, OrderedDict())
        self.history.setdefault(device_id, [])
        return self.applets[device_id]

    # ── 設定 ──────────────────────────────
    def add_applet(self, device_id, name, authorization_bypass=False):
        applets = self._dev(device_id)
        applets.setdefault(name, {
            'name': name, 'events': [], 'actions': [],
            'authorization_bypass': authorization_bypass,
            'registered': True, 'hits': 0,
        })
        if authorization_bypass:
            applets[name]['authorization_bypass'] = True
        return applets[name]

    def remove_applet(self, device_id, name):
        return self._dev(device_id).pop(name, None) is not None

    def add_event(self, device_id, name, ev_type, detail):
        ap = self._dev(device_id).get(name)
        if ap is None:
            return False
        ap['events'] = [e for e in ap['events'] if e['type'] != ev_type]
        ap['events'].append({'type': ev_type, **detail})
        return True

    def add_action(self, device_id, name, seq, act_type, arg):
        ap = self._dev(device_id).get(name)
        if ap is None:
            return False
        ap['actions'] = [a for a in ap['actions'] if a['seq'] != seq]
        ap['actions'].append({'seq': seq, 'type': act_type, 'arg': arg})
        # 実機はシーケンス番号順に実行する（投入順ではない）
        ap['actions'].sort(key=lambda a: _seq_key(a['seq']))
        return True

    def register_policy(self, device_id, filename, ptype='user'):
        self._dev(device_id)
        self.policies[device_id][filename] = {
            'name': filename, 'type': ptype,
            'class': 'applet' if filename.endswith('.tcl') else 'script',
        }
        return True

    def unregister_policy(self, device_id, filename):
        self._dev(device_id)
        return self.policies[device_id].pop(filename, None) is not None

    # ── 実行 ──────────────────────────────
    def run_applet(self, device_id, name, cli_exec=None):
        """applet を手動実行する。

        cli_exec は "action N cli command" を実際に流すためのコールバック。
        渡さなければコマンドは実行されず、実行ログだけを返す。
        戻り値: (出力文字列, 見つかったかbool)
        """
        ap = self._dev(device_id).get(name)
        if ap is None:
            return f'% Applet {name} is not registered', False
        # 実機は event none のappletだけが手動実行できる
        if not any(e['type'] == 'none' for e in ap['events']):
            return (f'% Applet {name} does not have a "none" event detector. '
                    'Only applets registered with "event none" can be run '
                    'manually.'), False
        ap['hits'] += 1
        out = []
        for act in ap['actions']:
            if act['type'] == 'syslog':
                msg = f'%HA_EM-6-LOG: {name}: {act["arg"]}'
                out.append(msg)
                self._log(device_id, name, msg)
            elif act['type'] == 'puts':
                out.append(act['arg'])
            elif act['type'] == 'cli':
                if cli_exec is not None:
                    res = cli_exec(act['arg'])
                    if res:
                        out.append(res if isinstance(res, str) else str(res))
                else:
                    out.append(f'(cli) {act["arg"]}')
        self.history[device_id].append({
            'time': time.time(), 'name': name, 'event': 'none',
        })
        return '\n'.join(out), True

    def notify_syslog(self, device_id, message, cli_exec=None):
        """syslogメッセージでトリガされるappletを起動する。
        戻り値: 起動したapplet名のリスト"""
        fired = []
        for name, ap in self._dev(device_id).items():
            for ev in ap['events']:
                if ev['type'] != 'syslog':
                    continue
                try:
                    if re.search(ev.get('pattern', ''), message):
                        ap['hits'] += 1
                        self.history[device_id].append({
                            'time': time.time(), 'name': name,
                            'event': 'syslog'})
                        for act in ap['actions']:
                            if act['type'] == 'cli' and cli_exec is not None:
                                cli_exec(act['arg'])
                        fired.append(name)
                        break
                except re.error:
                    continue
        return fired

    def _log(self, device_id, name, msg):
        pass

    # ── 表示 ──────────────────────────────
    def format_policy_registered(self, device_id):
        self._dev(device_id)
        applets = self.applets[device_id]
        policies = self.policies[device_id]
        if not applets and not policies:
            return 'No EEM policies registered'
        lines = ['No.  Class     Type    Event Type          Trap  Time Registered           Name']
        n = 0
        for name, ap in applets.items():
            n += 1
            ev = ap['events'][0]['type'] if ap['events'] else 'none'
            lines.append(f'{n:<5}applet    user    {ev:<20}Off   '
                         f'{_ts()}  {name}')
            for act in ap['actions']:
                lines.append(f' {act["seq"]} {_action_text(act)}')
        for fname, p in policies.items():
            n += 1
            lines.append(f'{n:<5}script    {p["type"]:<8}{"":<20}Off   '
                         f'{_ts()}  {fname}')
        return '\n'.join(lines)

    def format_policy_available(self, device_id):
        self._dev(device_id)
        d = self.policy_dir.get(device_id, '')
        policies = self.policies[device_id]
        if not policies:
            return ('No EEM policies available'
                    + (f' in {d}' if d else ''))
        lines = ['No.  Type    Time Created                     Name']
        for i, fname in enumerate(policies, 1):
            lines.append(f'{i:<5}{policies[fname]["type"]:<8}{_ts():<33}{fname}')
        return '\n'.join(lines)

    def format_history_events(self, device_id):
        self._dev(device_id)
        h = self.history[device_id]
        if not h:
            return 'No EEM events in history'
        lines = ['No.  Job Id Proc Status   Time of Event             Event Type    Name']
        for i, e in enumerate(h, 1):
            lines.append(f'{i:<5}{i:<7}Actv success  '
                         f'{time.strftime("%a %b%d %H:%M:%S %Y", time.localtime(e["time"])):<26}'
                         f'{e["event"]:<14}applet: {e["name"]}')
        return '\n'.join(lines)

    def format_statistics(self, device_id):
        self._dev(device_id)
        applets = self.applets[device_id]
        lines = ['                                              average     maximum',
                 'Description                    value     max   run time    run time',
                 '-' * 72]
        for name, ap in applets.items():
            lines.append(f'{name:<31}{ap["hits"]:<10}{"-":<8}{"0.000":<12}{"0.000"}')
        if not applets:
            lines.append('(no applets registered)')
        return '\n'.join(lines)

    def format_directory_user(self, device_id):
        d = self.policy_dir.get(device_id, '')
        if not d:
            return 'EEM user policy directory is not configured'
        return f'EEM user policy directory: {d}'


def _seq_key(seq):
    """action のシーケンス番号は 1.0 / 2.5 のような小数表記が使える"""
    try:
        return float(seq)
    except (TypeError, ValueError):
        return 0.0


def _action_text(act):
    if act['type'] == 'syslog':
        return f'syslog msg "{act["arg"]}"'
    if act['type'] == 'cli':
        return f'cli command "{act["arg"]}"'
    return f'{act["type"]} "{act["arg"]}"'


def _ts():
    return time.strftime('%a %b%d %H:%M:%S %Y')


# ══════════════════════════════════════════
# アプリケーションホスティング（IOx / Docker）
# ══════════════════════════════════════════
class AppHostingEngine:
    """Catalyst 9000 の app-hosting

    状態遷移（実機どおり）:
        (未導入) --install--> DEPLOYED --activate--> ACTIVATED
                                 ^                      |
                                 |                    start
                            deactivate                  v
                                 +-------- stop ---- RUNNING
        uninstall は DEPLOYED からのみ
    """

    STATES = ('DEPLOYED', 'ACTIVATED', 'RUNNING')

    def __init__(self):
        self.apps = {}       # device_id -> {appid -> dict}
        self.iox = {}        # device_id -> bool

    def _dev(self, device_id):
        self.apps.setdefault(device_id, OrderedDict())
        return self.apps[device_id]

    def set_iox(self, device_id, enabled: bool):
        self.iox[device_id] = enabled

    def iox_enabled(self, device_id) -> bool:
        return bool(self.iox.get(device_id))

    def config_app(self, device_id, appid):
        apps = self._dev(device_id)
        apps.setdefault(appid, {
            'appid': appid, 'state': None, 'owner': 'iox',
            'type': 'docker', 'name': appid, 'version': 'v1',
            'description': '', 'path': '', 'profile': 'default',
            'cpu': 0, 'memory': 0, 'disk': 0, 'vcpu': 0,
            'vnics': [], 'gateway': '', 'gateway_gi': '',
            'nameserver': '', 'run_opts': '', 'auto_start': False,
        })
        return apps[appid]

    def remove_app(self, device_id, appid):
        return self._dev(device_id).pop(appid, None) is not None

    # ── ライフサイクル ─────────────────────
    def install(self, device_id, appid, package):
        if not self.iox_enabled(device_id):
            return ('% IOx is not enabled. Configure "iox" first.', False)
        app = self.config_app(device_id, appid)
        if app['state'] is not None:
            return (f'% App {appid} is already installed '
                    f'(state {app["state"]})', False)
        app['state'] = 'DEPLOYED'
        app['path'] = package
        return (f'Installing package \'{package}\' for \'{appid}\'. '
                'Use \'show app-hosting list\' for progress.\n'
                f'{appid} installed successfully\n'
                f'Current state is DEPLOYED', True)

    def activate(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] is None:
            return (f'% App {appid} is not installed', False)
        if app['state'] != 'DEPLOYED':
            return (f'% App {appid} is in state {app["state"]}, '
                    'cannot activate', False)
        # 実機はリソースプロファイルが装置の空きを超えると失敗する
        app['state'] = 'ACTIVATED'
        return (f'{appid} activated successfully\n'
                'Current state is ACTIVATED', True)

    def start(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] is None:
            return (f'% App {appid} is not installed', False)
        if app['state'] != 'ACTIVATED':
            return (f'% App {appid} is in state {app["state"]}, '
                    'cannot start', False)
        app['state'] = 'RUNNING'
        return (f'{appid} started successfully\n'
                'Current state is RUNNING', True)

    def stop(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] != 'RUNNING':
            return (f'% App {appid} is not running', False)
        app['state'] = 'ACTIVATED'
        return (f'{appid} stopped successfully\n'
                'Current state is ACTIVATED', True)

    def deactivate(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] not in ('ACTIVATED',):
            return (f'% App {appid} is not activated', False)
        app['state'] = 'DEPLOYED'
        return (f'{appid} deactivated successfully\n'
                'Current state is DEPLOYED', True)

    def uninstall(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] is None:
            return (f'% App {appid} is not installed', False)
        if app['state'] != 'DEPLOYED':
            return (f'% App {appid} is in state {app["state"]}. '
                    'Deactivate it before uninstalling.', False)
        app['state'] = None
        app['path'] = ''
        return (f'{appid} uninstalled successfully\n'
                'Current state is not installed', True)

    # ── 表示 ──────────────────────────────
    def format_list(self, device_id):
        apps = {k: v for k, v in self._dev(device_id).items()
                if v['state'] is not None}
        lines = ['App id                                   State',
                 '-' * 57]
        for appid, app in apps.items():
            lines.append(f'{appid:<41}{app["state"]}')
        if not apps:
            lines.append('(no applications installed)')
        return '\n'.join(lines)

    def format_detail(self, device_id, appid):
        app = self._dev(device_id).get(appid)
        if app is None or app['state'] is None:
            return f'% App {appid} is not installed'
        lines = [
            f'App id                 : {app["appid"]}',
            f'Owner                  : {app["owner"]}',
            f'State                  : {app["state"]}',
            'Application',
            f'  Type                 : {app["type"]}',
            f'  Name                 : {app["name"]}',
            f'  Version              : {app["version"]}',
            f'  Description          : {app["description"]}',
            f'  Path                 : {app["path"]}',
            '  URL Path             :',
            f'Activated profile name : {app["profile"]}',
            '',
            'Resource reservation',
            f'  Memory               : {app["memory"]} MB',
            f'  Disk                 : {app["disk"]} MB',
            f'  CPU                  : {app["cpu"]} units',
            f'  VCPU                 : {app["vcpu"]}',
            '',
            'Attached devices',
            '  Type              Name               Alias',
            '  ' + '-' * 45,
            '  serial/shell     iox_console_shell   serial0',
            '  serial/aux       iox_console_aux     serial1',
            '  serial/syslog    iox_syslog          serial2',
            '  serial/trace     iox_trace           serial3',
        ]
        if app['vnics']:
            lines += ['', 'Network interfaces', '   ' + '-' * 39]
            for v in app['vnics']:
                lines.append(f'eth{v["guest_interface"]}:')
                lines.append(f'   MAC address         : '
                             f'52:54:dd:{v["guest_interface"]:02x}:'
                             f'{v["vlan"] >> 8 & 0xff:02x}:{v["vlan"] & 0xff:02x}')
                if v.get('ip'):
                    lines.append(f'   IPv4 address        : {v["ip"]}')
                lines.append(f'   Network name        : vlan{v["vlan"]}')
        if app['type'] == 'docker':
            lines += ['', 'Docker', '------', 'Run-time information',
                      '  Command              :',
                      '  Entry-point          :',
                      f'  Run options in use   : {app["run_opts"]}',
                      '  Package run options  :',
                      'Application health information',
                      '  Status               : 0',
                      '  Last probe error     :',
                      '  Last probe output    :']
        return '\n'.join(lines)

    def format_resource(self, device_id):
        apps = [a for a in self._dev(device_id).values()
                if a['state'] in ('ACTIVATED', 'RUNNING')]
        used_cpu = sum(a['cpu'] for a in apps)
        used_mem = sum(a['memory'] for a in apps)
        used_disk = sum(a['disk'] for a in apps)
        return '\n'.join([
            'CPU:',
            '  Quota: 7400(Units)',
            f'  Available: {max(0, 7400 - used_cpu)}(Units)',
            '  Quota: 100(Percentage)',
            f'  Available: {max(0, 100 - int(used_cpu / 74))}(Percentage)',
            '',
            'Memory:',
            '  Quota: 2048(MB)',
            f'  Available: {max(0, 2048 - used_mem)}(MB)',
            '',
            'Storage device: bootflash',
            '  Quota: 1000(MB)',
            f'  Available: {max(0, 1000 - used_disk)}(MB)',
        ])


# ══════════════════════════════════════════
# OpenFlow（faucetパイプライン）
# ══════════════════════════════════════════
class OpenflowEngine:
    """Catalyst 9000 の OpenFlow

    実機は `boot mode openflow` でリロードして初めてOpenFlowモードになる。
    通常のスイッチングモードのままでは `feature openflow` が通らない。
    """

    def __init__(self):
        self.nodes = {}      # device_id -> dict

    def _node(self, device_id):
        self.nodes.setdefault(device_id, {
            'boot_mode': 'normal', 'feature': False,
            'switches': OrderedDict(),
        })
        return self.nodes[device_id]

    def set_boot_mode(self, device_id, mode):
        self._node(device_id)['boot_mode'] = mode

    def boot_mode(self, device_id):
        return self._node(device_id)['boot_mode']

    def enable_feature(self, device_id, enabled=True):
        n = self._node(device_id)
        if enabled and n['boot_mode'] != 'openflow':
            return False
        n['feature'] = enabled
        if not enabled:
            n['switches'].clear()
        return True

    def feature_enabled(self, device_id):
        return self._node(device_id)['feature']

    def add_switch(self, device_id, sid, pipeline):
        n = self._node(device_id)
        sw = n['switches'].setdefault(sid, {
            'id': sid, 'pipeline': pipeline, 'dpid': f'0x{sid:016x}',
            'controllers': [], 'probe_interval': 5, 'tls_trustpoint': '',
            'logging_flow_mod': False,
        })
        sw['pipeline'] = pipeline
        return sw

    def add_controller(self, device_id, sid, ip, port, vrf='', security='none'):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return False
        sw['controllers'] = [c for c in sw['controllers']
                             if not (c['ip'] == ip and c['port'] == port)]
        sw['controllers'].append({'ip': ip, 'port': port, 'vrf': vrf,
                                  'security': security, 'connected': True})
        return True

    def remove_controller(self, device_id, sid, ip, port):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return False
        before = len(sw['controllers'])
        sw['controllers'] = [c for c in sw['controllers']
                             if not (c['ip'] == ip and c['port'] == port)]
        return len(sw['controllers']) != before

    # ── 表示 ──────────────────────────────
    def format_switch(self, device_id, sid):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return f'% OpenFlow switch {sid} is not configured'
        return '\n'.join([
            'Logical Switch Context',
            f'  Id: {sw["id"]}',
            '  Switch type: Forwarding',
            f'  Pipeline id: {sw["pipeline"]}',
            f'  Data plane: secure',
            f'  Table-Miss default: drop',
            '  Configured protocol version: Negotiate',
            '  Config state: no-shutdown',
            '  Working state: enabled',
            f'  DPID: {sw["dpid"]}',
            '  Number of tables: 9',
            '  Capabilities: FLOW_STATS TABLE_STATS PORT_STATS',
            f'  Controllers: {len(sw["controllers"])}',
        ])

    def format_controllers(self, device_id, sid):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return f'% OpenFlow switch {sid} is not configured'
        if not sw['controllers']:
            return 'Total Controllers: 0'
        lines = [f'Total Controllers: {len(sw["controllers"])}']
        for i, c in enumerate(sw['controllers'], 1):
            lines += [
                f'  Controller: {i}',
                f'    {c["ip"]}:{c["port"]}',
                f'    Protocol: tcp',
                f'    VRF: {c["vrf"] or "(global)"}',
                f'    Connected: {"Yes" if c["connected"] else "No"}',
                '    Role: Equal',
                '    Negotiated Protocol Version: OpenFlow 1.3',
            ]
        return '\n'.join(lines)

    def format_flows(self, device_id, sid):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return f'% OpenFlow switch {sid} is not configured'
        # コントローラ未接続のうちは table-miss の1件だけ
        return '\n'.join([
            'Logical Switch Id: %d' % sid,
            'Total flows: 1',
            'Flow: 1 Match: any Actions: drop, Priority: 0, Table: 0, '
            'Cookie: 0x0, Duration: 0.0s, Packets: 0, Bytes: 0',
        ])

    def format_ports(self, device_id, sid, interfaces):
        sw = self._node(device_id)['switches'].get(sid)
        if sw is None:
            return f'% OpenFlow switch {sid} is not configured'
        lines = ['Port    Interface Name   Config-State     Link-State']
        n = 0
        for name, info in (interfaces or {}).items():
            if not name.startswith('GigabitEthernet'):
                continue
            n += 1
            short = name.replace('GigabitEthernet', 'Gi')
            down = info.get('status') in ('down', 'administratively down',
                                          'notconnect', 'disabled')
            lines.append(f'{n:>4}  {short:>16}  '
                         f'{"PORT_DOWN" if down else "PORT_UP":>13}  '
                         f'{"LINK_DOWN" if down else "LINK_UP":>10}')
            if n >= 24:
                break
        return '\n'.join(lines)


eem_engine = EemEngine()
app_hosting_engine = AppHostingEngine()
openflow_engine = OpenflowEngine()
