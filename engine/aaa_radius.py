"""
NAS(認証を依頼する側)のRADIUS設定とAAA判定。cisco/catalyst/nexus/wlc共通。

設定コマンド(実機構文):
  IOS-XE:  radius server <name>               → config-radius-server
             address ipv4 <ip> [auth-port <n>] [acct-port <n>]
             key [0|7] <secret>
           aaa group server radius <name>      → config-sg-radius
             server name <name>   (IOS-XE)  /  server <ip>  (NX-OS・旧IOS)
           ip radius source-interface <if>
  旧IOS/NX-OS:
           radius-server host <ip> [auth-port <n>] [acct-port <n>] key [0|7] <secret>

ログイン判定はIOSのメソッドリストの意味論に従う:
  サーバがRejectを返したら次のメソッド(local)には進まない。
  タイムアウト/エラーのときだけ次のサーバ→次のメソッドへ進む。
"""

import re
import time
from typing import List, Optional, Tuple

from engine.radius_server import (AUTH_PORT, ACCT_PORT, RadiusResult,
                                  radius_accounting, radius_authenticate)

RADIUS_DEVICE_TYPES = ('cisco', 'catalyst', 'nexus', 'wlc')


def _servers(state) -> dict:
    if not hasattr(state, 'radius_servers'):
        state.radius_servers = {}
    return state.radius_servers


def _groups(state) -> dict:
    if not hasattr(state, 'radius_groups'):
        state.radius_groups = {}
    return state.radius_groups


def _new_server(ip='', auth=AUTH_PORT, acct=ACCT_PORT, key='') -> dict:
    return {'address': ip, 'auth_port': auth, 'acct_port': acct, 'key': key,
            'legacy': False, 'stats': {'requests': 0, 'accepts': 0, 'rejects': 0,
                                       'timeouts': 0, 'acct_requests': 0}}


def handle_config(state, cmd: str) -> Optional[str]:
    """RADIUS関連の設定/exec/showコマンド。対象外ならNone。"""
    if state.device_type not in RADIUS_DEVICE_TYPES:
        return None
    orig = cmd.strip()
    c = orig.lower()
    servers, groups = _servers(state), _groups(state)
    in_cfg = state.mode.startswith('config')

    if in_cfg:
        m = re.match(r'^radius\s+server\s+(\S+)$', orig, re.I)
        if m:
            servers.setdefault(m.group(1), _new_server())
            state._radius_server_name = m.group(1)
            state.mode = 'config-radius-server'
            return ''
        m = re.match(r'^no\s+radius\s+server\s+(\S+)$', orig, re.I)
        if m:
            servers.pop(m.group(1), None)
            return ''
        m = re.match(r'^radius-server\s+host\s+([\d.]+)((?:\s+(?:auth-port|acct-port)\s+\d+)*)'
                     r'(?:\s+key\s+(?:[07]\s+)?(\S+))?', orig, re.I)
        if m:
            ip, ports, key = m.groups()
            s = servers.setdefault(ip, _new_server(ip))
            s.update({'address': ip, 'legacy': True})
            for k, v in re.findall(r'(auth-port|acct-port)\s+(\d+)', ports or '', re.I):
                s['auth_port' if k.lower() == 'auth-port' else 'acct_port'] = int(v)
            if key:
                s['key'] = key
            return ''
        m = re.match(r'^no\s+radius-server\s+host\s+([\d.]+)', c)
        if m:
            servers.pop(m.group(1), None)
            return ''
        m = re.match(r'^aaa\s+group\s+server\s+radius\s+(\S+)$', orig, re.I)
        if m:
            groups.setdefault(m.group(1), {'servers': [], 'source_interface': ''})
            state._radius_group_name = m.group(1)
            state.mode = 'config-sg-radius'
            return ''
        m = re.match(r'^ip\s+radius\s+source-interface\s+(\S+)', orig, re.I)
        if m and state.mode == 'config':
            state.radius_source_interface = m.group(1)
            return ''

    if state.mode == 'config-radius-server':
        s = servers.get(getattr(state, '_radius_server_name', ''))
        if s is None:
            return None
        m = re.match(r'^address\s+ipv4\s+([\d.]+)((?:\s+(?:auth-port|acct-port)\s+\d+)*)$', c)
        if m:
            s['address'] = m.group(1)
            for k, v in re.findall(r'(auth-port|acct-port)\s+(\d+)', m.group(2) or ''):
                s['auth_port' if k == 'auth-port' else 'acct_port'] = int(v)
            return ''
        m = re.match(r'^key\s+(?:[07]\s+)?(\S+)$', orig, re.I)
        if m:
            s['key'] = m.group(1)
            return ''
        return None

    if state.mode == 'config-sg-radius':
        g = groups.get(getattr(state, '_radius_group_name', ''))
        if g is None:
            return None
        m = re.match(r'^server\s+(?:name\s+)?(\S+)$', orig, re.I)
        if m:
            if m.group(1) not in g['servers']:
                g['servers'].append(m.group(1))
            return ''
        m = re.match(r'^(?:ip\s+radius\s+)?source-interface\s+(\S+)$', orig, re.I)
        if m:
            g['source_interface'] = m.group(1)
            return ''
        if c.startswith('use-vrf'):
            return ''
        return None

    m = re.match(r'^test\s+aaa\s+group\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+legacy)?(?:\s+new-code)?$', orig, re.I)
    if m and not in_cfg:
        grp, user, pw = m.groups()
        res = authenticate(state, grp, user, pw, method='pap')
        if state.device_type == 'nexus':
            return {'accept': 'user has been authenticated',
                    'reject': 'user has failed authentication'}.get(
                        res.status, 'error: no response from any server')
        head = f'Attempting authentication test to server-group {grp} using radius'
        body = {'accept': 'User was successfully authenticated.',
                'reject': 'User authentication request was rejected by server.'}.get(
                    res.status, 'No authoritative response from any server.')
        return f'{head}\n{body}'

    if re.match(r'^show\s+aaa\s+servers$', c) or re.match(r'^show\s+radius-server(\s+statistics)?$', c):
        return show_servers(state)
    if re.match(r'^show\s+radius\s+server-group\s+(all|\S+)$', c):
        return show_groups(state)
    return None


def running_config_lines(state, nxos: bool = False) -> List[str]:
    lines: List[str] = []
    servers, groups = _servers(state), _groups(state)
    for name, s in servers.items():
        if s.get('legacy') or nxos:
            ports = ''
            if s['auth_port'] != AUTH_PORT:
                ports += f' auth-port {s["auth_port"]}'
            if s['acct_port'] != ACCT_PORT:
                ports += f' acct-port {s["acct_port"]}'
            key = f' key 7 "{s["key"]}"' if nxos and s['key'] else (f' key {s["key"]}' if s['key'] else '')
            lines.append(f'radius-server host {s["address"]}{ports}{key}')
        else:
            lines.append(f'radius server {name}')
            if s['address']:
                lines.append(f' address ipv4 {s["address"]} auth-port {s["auth_port"]} '
                             f'acct-port {s["acct_port"]}')
            if s['key']:
                lines.append(f' key {s["key"]}')
            lines.append('!')
    for name, g in groups.items():
        lines.append(f'aaa group server radius {name}')
        ind = '    ' if nxos else ' '
        for srv in g['servers']:
            srv_def = servers.get(srv)
            named = srv_def is not None and not srv_def.get('legacy') and not nxos
            lines.append(f'{ind}server {"name " if named else ""}{srv}')
        if g.get('source_interface'):
            lines.append(f'{ind}{"" if nxos else "ip radius "}source-interface {g["source_interface"]}')
    src = getattr(state, 'radius_source_interface', '')
    if src:
        lines.append(f'ip radius source-interface {src}')
    return lines


def _resolve(state, group: str) -> List[Tuple[str, dict]]:
    """メソッドの`group <name>`を、問い合わせるサーバの順序付きリストに解決する。"""
    servers, groups = _servers(state), _groups(state)
    if group.lower() == 'radius':
        return [(n, s) for n, s in servers.items() if s.get('address')]
    g = groups.get(group)
    if not g:
        return []
    out = []
    for ref in g['servers']:
        s = servers.get(ref)
        if s is None:
            s = next((v for v in servers.values() if v.get('address') == ref), None)
        if s and s.get('address'):
            out.append((ref, s))
    return out


def is_radius_group(state, group: str) -> bool:
    return bool(group) and (group.lower() == 'radius' or group in _groups(state))


def _iface_ip(state, ifname: str) -> Optional[str]:
    if not ifname:
        return None
    for name, info in state.interfaces.items():
        if name.lower() == ifname.lower() and info.get('ip'):
            return info['ip']
    return None


def nas_ip(state, group: str = '', fallback: Optional[str] = None) -> Optional[str]:
    """送信元(=NAS)IP。グループのsource-interface → ip radius source-interface
    → 呼び出し元の指定 → 最初にIPを持つインタフェース、の順。"""
    g = _groups(state).get(group) or {}
    ip = _iface_ip(state, g.get('source_interface', '')) or \
        _iface_ip(state, getattr(state, 'radius_source_interface', ''))
    if ip:
        return ip
    if fallback:
        return fallback
    for info in state.interfaces.values():
        if info.get('ip') and info.get('status') not in ('down', 'admin down'):
            return info['ip']
    return None


def authenticate(state, group: str, username: str, password: str, method: str = 'pap',
                 source_ip: Optional[str] = None, **kw) -> RadiusResult:
    """グループ内のサーバを順に試す。Accept/Rejectで確定、timeout/errorなら次へ。"""
    targets = _resolve(state, group)
    if not targets:
        return RadiusResult('error', detail=f'no RADIUS servers in group {group}')
    src = nas_ip(state, group, source_ip)
    last = RadiusResult('error', detail='no response')
    for _name, s in targets:
        s['stats']['requests'] += 1
        res = radius_authenticate(s['address'], s['key'], username, password, method=method,
                                  port=s['auth_port'], source_ip=src, timeout=1.5, retries=1, **kw)
        if res.status in ('accept', 'reject'):
            s['stats']['accepts' if res.accepted else 'rejects'] += 1
            return res
        s['stats']['timeouts'] += 1
        last = res
    return last


def account(state, group: str, status: int, username: str, session_id: str,
            source_ip: Optional[str] = None, **kw) -> str:
    targets = _resolve(state, group)
    src = nas_ip(state, group, source_ip)
    for _name, s in targets:
        s['stats']['acct_requests'] += 1
        r = radius_accounting(s['address'], s['key'], status, username, session_id,
                              port=s['acct_port'], source_ip=src, timeout=1.5, retries=1, **kw)
        if r == 'ok':
            return r
    return 'no server'


def login_method_list(state) -> Optional[dict]:
    """vtyの`login authentication <name>`、無ければdefaultのメソッドリスト。
    IOS系は`aaa new-model`が無ければAAA自体が無効(ローカル認証)。"""
    if state.device_type in ('cisco', 'catalyst', 'wlc') and not getattr(state, 'aaa_new_model', False):
        return None
    name = getattr(state, 'line_vty_login_authentication', None) or 'default'
    lists = getattr(state, 'aaa_authentication_login_lists', None) or {}
    entry = lists.get(name)
    if entry is None and name == 'default':
        entry = getattr(state, 'aaa_authentication_login', None)
    return entry


def login(state, username: str, password: str, nas_source_ip: Optional[str] = None):
    """SSH/Telnetログインの判定。

    戻り値: ('accept', privilege) / ('reject', None) / None
    None は「RADIUSを使う設定ではない、またはRADIUSが無応答でlocalへ
    フォールバックする」ことを示し、呼び出し側が従来のローカル認証を行う。
    ('reject', None) はlocalにも進ませない確定拒否。
    """
    entry = login_method_list(state)
    if not entry or not is_radius_group(state, entry.get('group', '')):
        return None
    res = authenticate(state, entry['group'], username, password, method='pap',
                       source_ip=nas_source_ip, service_type=1)
    log = getattr(state, 'aaa_login_log', None)
    if log is None:
        log = state.aaa_login_log = []
    log.append({'time': time.strftime('%H:%M:%S'), 'user': username, 'result': res.status})
    del log[:-50]
    if res.accepted:
        return 'accept', res.privilege if res.privilege is not None else 1
    if res.status == 'reject':
        return 'reject', None
    if entry.get('local_fallback'):
        return None
    return 'reject', None


def show_servers(state) -> str:
    servers = _servers(state)
    if not servers:
        return 'No RADIUS servers configured.'
    out = []
    for name, s in servers.items():
        st = s['stats']
        out += [f'RADIUS: id {list(servers).index(name) + 1}, priority 1, host {s["address"]}, '
                f'auth-port {s["auth_port"]}, acct-port {s["acct_port"]}',
                f'     Server name: {name}',
                f'     State: current UP, duration 0s',
                f'     Authen: request {st["requests"]}, timeouts {st["timeouts"]}, '
                f'failover 0, retransmission 0',
                f'             Response: accept {st["accepts"]}, reject {st["rejects"]}, '
                f'challenge 0',
                f'     Account: request {st["acct_requests"]}, timeouts 0',
                '']
    return '\n'.join(out).rstrip()


def show_groups(state) -> str:
    out = []
    for name, g in [('radius', {'servers': list(_servers(state))})] + list(_groups(state).items()):
        out.append(f'Server group {name}')
        out.append(f'    Sharecount = 1  sg_unconfigured = FALSE')
        for srv in g['servers']:
            s = _servers(state).get(srv) or {}
            out.append(f'    Server({s.get("address", srv)}:{s.get("auth_port", AUTH_PORT)},'
                       f'{s.get("acct_port", ACCT_PORT)}) Transactions:')
        out.append('')
    return '\n'.join(out).rstrip()
