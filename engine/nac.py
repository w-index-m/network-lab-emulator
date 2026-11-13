"""
ネットワークアクセス制御(NAC): 有線802.1X(Catalyst)と無線(C9800 WLC)、
そしてPC側のサプリカント(NetworkManagerの`nmcli`)。

認証の実体は本物のRADIUS(engine.radius_server)。スイッチ/WLCがNASとして
EAP-MD5のAccess-Request/Access-Challengeを自分の装置IPから実際に送り、
NetAttest等のRADIUSサーバの応答(Accept/Reject・動的VLAN属性)で
ポート/クライアントの状態を決める。

実機との差(スコープ外):
  - サプリカント⇔オーセンティケータ間のEAPOLフレームは実L2では送らない
    (vnetのリンクで「どのポートに繋がっているか」を解決する。PPPoEの
    PppoeEngineと同じ方式)。オーセンティケータ⇔RADIUSサーバ間は本物のUDP。
  - EAP-PEAP/EAP-TTLSはTLSトンネルを実装しておらず、内側の資格情報は
    EAP-MD5で検証する(show出力にその旨を明記する)。EAP-TLSは非対応。
  - 802.11の暗号化(4-way handshake/PTK)は扱わない。
"""

import hashlib
import os
import re
import time
from typing import Optional

from engine import aaa_radius
from engine.radius_server import ACCT_START, ACCT_STOP

DOT1X_DEVICE_TYPES = ('cisco', 'catalyst')
WLC_DEVICE_TYPES = ('wlc',)


# ── 共通ヘルパ ────────────────────────────────────────────────

def _dot1x(state) -> dict:
    if not hasattr(state, 'dot1x') or not isinstance(state.dot1x, dict):
        state.dot1x = {'system_auth_control': False, 'authc_lists': {},
                       'authz_network': '', 'acct': '', 'ports': {}, 'sessions': {},
                       'blocked': []}
    return state.dot1x


def _mac_for(device_id: str, state, ifname: str) -> str:
    info = state.interfaces.get(ifname, {})
    mac = info.get('mac', '')
    if mac and mac not in ('aa:bb:cc:dd:ee:01', '00:00:00:00:00:00'):
        return mac
    h = hashlib.md5(f'{device_id}/{ifname}'.encode()).hexdigest()
    return ':'.join(['02'] + [h[i:i + 2] for i in range(0, 10, 2)])


def _cisco_mac(mac: str) -> str:
    h = mac.replace(':', '').replace('-', '').lower()
    return f'{h[0:4]}.{h[4:8]}.{h[8:12]}'


def _calling_station(mac: str) -> str:
    return mac.replace(':', '-').upper()


def _abbrev(ifname: str) -> str:
    m = re.match(r'^(GigabitEthernet|TenGigabitEthernet|FastEthernet)(.*)$', ifname)
    if not m:
        return ifname
    return {'GigabitEthernet': 'Gi', 'TenGigabitEthernet': 'Te', 'FastEthernet': 'Fa'}[m.group(1)] + m.group(2)


def _resolve_if(state, name: str) -> Optional[str]:
    n = name.lower()
    for full in state.interfaces:
        if full.lower() == n or _abbrev(full).lower() == n:
            return full
    m = re.match(r'^(gi|gig|gigabitethernet|te|fa)\s*(\S+)$', n)
    if m:
        pref = {'gi': 'GigabitEthernet', 'gig': 'GigabitEthernet', 'gigabitethernet': 'GigabitEthernet',
                'te': 'TenGigabitEthernet', 'fa': 'FastEthernet'}[m.group(1)]
        cand = pref + m.group(2)
        if cand in state.interfaces:
            return cand
    return None


def _eap_wire_method(eap: str) -> tuple:
    """(RADIUSに載せるEAP方式, 表示用の説明)"""
    eap = (eap or 'md5').lower()
    if eap == 'md5':
        return 'eap-md5', 'EAP-MD5'
    if eap in ('peap', 'ttls'):
        return 'eap-md5', (f'EAP-{eap.upper()} (emulated: no TLS tunnel, '
                           'inner credentials verified via EAP-MD5)')
    return None, f'EAP-{eap.upper()}'


# ── スイッチ側(有線802.1X)の設定・表示 ─────────────────────────

def handle_switch(state, cmd: str) -> Optional[str]:
    orig = cmd.strip()
    c = orig.lower()
    d = _dot1x(state)

    if state.mode == 'config':
        if c == 'dot1x system-auth-control':
            d['system_auth_control'] = True
            return ''
        if c == 'no dot1x system-auth-control':
            d['system_auth_control'] = False
            return ''
        m = re.match(r'^aaa\s+authentication\s+dot1x\s+(\S+)\s+group\s+(\S+)$', orig, re.I)
        if m:
            d['authc_lists'][m.group(1)] = m.group(2)
            return ''
        m = re.match(r'^no\s+aaa\s+authentication\s+dot1x\s+(\S+)', orig, re.I)
        if m:
            d['authc_lists'].pop(m.group(1), None)
            return ''
        if re.match(r'^no\s+aaa\s+authorization\s+network\b', c):
            d['authz_network'] = ''
            return ''
        if re.match(r'^no\s+aaa\s+accounting\s+(dot1x|network|identity)\b', c):
            d['acct'] = ''
            return ''
        m = re.match(r'^aaa\s+authorization\s+network\s+(\S+)\s+group\s+(\S+)$', orig, re.I)
        if m:
            d['authz_network'] = m.group(2)
            return ''
        m = re.match(r'^aaa\s+accounting\s+(dot1x|network|identity)\s+(\S+)\s+start-stop\s+group\s+(\S+)$',
                     orig, re.I)
        if m:
            d['acct_kw'] = m.group(1).lower()
            d['acct'] = m.group(3)
            return ''

    if state.mode == 'config-if':
        ifname = getattr(state, 'current_if', '') or ''
        if ifname and ifname in state.interfaces:
            p = d['ports'].setdefault(ifname, {'port_control': '', 'pae': False})
            if c in ('authentication port-control auto', 'access-session port-control auto'):
                p['port_control'] = 'auto'
                return ''
            if c in ('no authentication port-control auto', 'no access-session port-control auto',
                     'no authentication port-control', 'no access-session port-control'):
                p['port_control'] = ''
                return ''
            if c == 'dot1x pae authenticator':
                p['pae'] = True
                return ''
            if c == 'no dot1x pae authenticator':
                p['pae'] = False
                return ''

    if state.mode.startswith('config'):
        return None
    m = re.match(r'^clear\s+(?:access-session|authentication\s+sessions)\s+interface\s+(\S+)$', c)
    if m:
        ifname = _resolve_if(state, m.group(1))
        if ifname:
            d['sessions'].pop(ifname, None)
        return ''
    m = re.match(r'^show\s+(access-session|authentication\s+sessions)'
                 r'(?:\s+interface\s+(\S+)(\s+details?)?)?$', c)
    if m:
        if m.group(2):
            ifname = _resolve_if(state, m.group(2))
            if not ifname:
                return '% Invalid interface'
            if m.group(3):
                return _show_session_detail(state, ifname)
            return _show_sessions(state, only=ifname)
        return _show_sessions(state)
    if re.match(r'^show\s+dot1x(\s+all)?(\s+summary)?$', c):
        return _show_dot1x(state)
    return None


def _show_sessions(state, only: Optional[str] = None) -> str:
    sessions = _dot1x(state)['sessions']
    rows = [(i, s) for i, s in sessions.items() if only is None or i == only]
    if not rows:
        return 'No sessions match supplied criteria.'
    out = ['Interface                MAC Address    Method  Domain  Status Fg  Session ID',
           '--------------------------------------------------------------------------------------------']
    for ifname, s in rows:
        st = 'Auth' if s['status'] == 'Authorized' else 'Unauth'
        out.append(f'{_abbrev(ifname):<24} {_cisco_mac(s["mac"]):<14} dot1x   DATA    {st:<6}     {s["session_id"]}')
    out += ['', f'Session count = {len(rows)}']
    return '\n'.join(out)


def _show_session_detail(state, ifname: str) -> str:
    s = _dot1x(state)['sessions'].get(ifname)
    if not s:
        return 'No sessions match supplied criteria.'
    vlan_line = (f'            Vlan Group:  Vlan: {s["vlan"]}' if s.get('vlan_assigned')
                 else '')
    out = [f'            Interface:  {ifname}',
           f'          MAC Address:  {_cisco_mac(s["mac"])}',
           f'         IPv4 Address:  {s.get("ip") or "Unknown"}',
           f'            User-Name:  {s["user"]}',
           f'               Status:  {s["status"]}',
           f'               Domain:  DATA',
           f'       Oper host mode:  single-host',
           f'     Oper control dir:  both',
           f'      Session timeout:  N/A',
           f'    Common Session ID:  {s["session_id"]}',
           f'      Acct Session ID:  {s.get("acct_session_id") or "Unknown"}',
           f'           EAP Method:  {s.get("eap_desc", "")}',
           '', 'Server Policies:']
    if vlan_line:
        out.append(vlan_line)
    out += ['', 'Method status list:', '       Method           State',
            f'        dot1x           {"Authc Success" if s["status"] == "Authorized" else "Authc Failed"}']
    return '\n'.join(out)


def _show_dot1x(state) -> str:
    d = _dot1x(state)
    out = [f'Sysauthcontrol              {"Enabled" if d["system_auth_control"] else "Disabled"}',
           'Dot1x Protocol Version            3', '']
    for ifname, p in d['ports'].items():
        if not (p.get('pae') or p.get('port_control')):
            continue
        s = d['sessions'].get(ifname)
        out += [f'Dot1x Info for {ifname}', '-----------------------------------',
                f'PAE                       = {"AUTHENTICATOR" if p.get("pae") else "NONE"}',
                f'PortControl               = {(p.get("port_control") or "force-authorized").upper()}',
                f'ControlDirection          = Both',
                f'HostMode                  = SINGLE_HOST',
                f'Status                    = {s["status"].upper() if s else "UNAUTHORIZED"}', '']
    return '\n'.join(out).rstrip()


def switch_running_config_global(state) -> list:
    d = getattr(state, 'dot1x', None)
    if not d:
        return []
    out = [f'aaa authentication dot1x {name} group {grp}' for name, grp in d['authc_lists'].items()]
    if d.get('authz_network'):
        out.append(f'aaa authorization network default group {d["authz_network"]}')
    if d.get('acct'):
        out.append(f'aaa accounting {d.get("acct_kw", "dot1x")} default start-stop group {d["acct"]}')
    if d.get('system_auth_control'):
        out.append('dot1x system-auth-control')
    return out


def switch_running_config_interface(state, ifname: str) -> list:
    d = getattr(state, 'dot1x', None)
    p = (d or {}).get('ports', {}).get(ifname)
    if not p:
        return []
    out = []
    if p.get('port_control'):
        out.append(f' authentication port-control {p["port_control"]}')
    if p.get('pae'):
        out.append(' dot1x pae authenticator')
    return out


def reconcile_ports(device_id: str, state) -> None:
    """802.1Xポートのデータプレーン制御。認証されていないポートは
    EAPOL以外を通さない(実機)ので、vnetの上ではdown扱いにして疎通を
    止める。自分が止めたポートだけを戻す(shutdownとは区別する)。"""
    from engine.protocols import vnet
    d = getattr(state, 'dot1x', None)
    if not d:
        return
    blocked = set(d.get('blocked', []))
    want = set()
    if d.get('system_auth_control'):
        for ifname, p in d['ports'].items():
            if p.get('port_control') == 'auto':
                s = d['sessions'].get(ifname)
                if not s or s['status'] != 'Authorized':
                    want.add(ifname)
    # 毎回入れ直す(no shutdown等でvnet側のdownが外されることがあるため)
    for ifname in want:
        vnet.interface_down(device_id, ifname)
    for ifname in blocked - want:
        vnet.interface_up(device_id, ifname)
    d['blocked'] = sorted(want)


# ── WLC(Catalyst 9800)側の設定・表示 ───────────────────────────

def _wlc(state) -> dict:
    if not hasattr(state, 'wireless') or not isinstance(state.wireless, dict):
        state.wireless = {'wlans': {}, 'policy_profiles': {
            'default-policy-profile': {'vlan': '1', 'aaa_override': False, 'shutdown': False}},
            'policy_tags': {}, 'clients': {}}
    return state.wireless


def _wlan_policy(state, wlan_profile: str) -> dict:
    w = _wlc(state)
    for tag in w['policy_tags'].values():
        pol = tag.get(wlan_profile)
        if pol and pol in w['policy_profiles']:
            return w['policy_profiles'][pol] | {'name': pol}
    return w['policy_profiles']['default-policy-profile'] | {'name': 'default-policy-profile'}


def handle_wlc(state, cmd: str) -> Optional[str]:
    orig = cmd.strip()
    c = orig.lower()
    w = _wlc(state)

    if state.mode == 'config':
        m = re.match(r'^wlan\s+(\S+)\s+(\d+)\s+(\S+)$', orig, re.I)
        if m:
            name, wid, ssid = m.groups()
            w['wlans'].setdefault(name, {'id': int(wid), 'ssid': ssid, 'shutdown': True,
                                         'akm': 'dot1x', 'psk': '', 'auth_list': ''})
            state._wlan_profile = name
            state.mode = 'config-wlan'
            return ''
        m = re.match(r'^wlan\s+(\S+)$', orig, re.I)
        if m and m.group(1) in w['wlans']:
            state._wlan_profile = m.group(1)
            state.mode = 'config-wlan'
            return ''
        m = re.match(r'^no\s+wlan\s+(\S+)', orig, re.I)
        if m:
            w['wlans'].pop(m.group(1), None)
            return ''
        m = re.match(r'^wireless\s+profile\s+policy\s+(\S+)$', orig, re.I)
        if m:
            w['policy_profiles'].setdefault(m.group(1), {'vlan': '1', 'aaa_override': False,
                                                         'shutdown': True})
            state._wireless_policy = m.group(1)
            state.mode = 'config-wireless-policy'
            return ''
        m = re.match(r'^wireless\s+tag\s+policy\s+(\S+)$', orig, re.I)
        if m:
            w['policy_tags'].setdefault(m.group(1), {})
            state._wireless_tag = m.group(1)
            state.mode = 'config-policy-tag'
            return ''

    if state.mode == 'config-wlan':
        wl = w['wlans'].get(getattr(state, '_wlan_profile', ''))
        if wl is None:
            return None
        if c in ('shutdown', 'no shutdown'):
            wl['shutdown'] = c == 'shutdown'
            return ''
        if c in ('security wpa akm psk', 'no security wpa akm dot1x'):
            if c == 'security wpa akm psk':
                wl['akm'] = 'psk'
            return ''
        if c in ('security wpa akm dot1x', 'no security wpa akm psk'):
            if c == 'security wpa akm dot1x':
                wl['akm'] = 'dot1x'
            return ''
        m = re.match(r'^security\s+wpa\s+psk\s+set-key\s+ascii\s+[08]\s+(\S+)$', orig, re.I)
        if m:
            wl['psk'] = m.group(1)
            return ''
        m = re.match(r'^security\s+dot1x\s+authentication-list\s+(\S+)$', orig, re.I)
        if m:
            wl['auth_list'] = m.group(1)
            return ''
        if c.startswith('security wpa') or c.startswith('security ft'):
            return ''
        return None

    if state.mode == 'config-wireless-policy':
        pol = w['policy_profiles'].get(getattr(state, '_wireless_policy', ''))
        if pol is None:
            return None
        m = re.match(r'^vlan\s+(\S+)$', c)
        if m:
            pol['vlan'] = m.group(1)
            return ''
        if c in ('aaa-override', 'no aaa-override'):
            pol['aaa_override'] = c == 'aaa-override'
            return ''
        if c in ('shutdown', 'no shutdown'):
            pol['shutdown'] = c == 'shutdown'
            return ''
        return None

    if state.mode == 'config-policy-tag':
        tag = w['policy_tags'].get(getattr(state, '_wireless_tag', ''))
        if tag is None:
            return None
        m = re.match(r'^wlan\s+(\S+)\s+policy\s+(\S+)$', orig, re.I)
        if m:
            tag[m.group(1)] = m.group(2)
            return ''
        return None

    if state.mode.startswith('config'):
        return None
    if re.match(r'^show\s+wlan\s+summary$', c):
        return _show_wlan_summary(state)
    if re.match(r'^show\s+wireless\s+client\s+summary$', c):
        return _show_client_summary(state)
    m = re.match(r'^show\s+wireless\s+client\s+mac-address\s+(\S+)\s+detail$', c)
    if m:
        return _show_client_detail(state, m.group(1))
    if re.match(r'^show\s+wireless\s+profile\s+policy\s+summary$', c):
        out = ['Number of Policy Profiles: ' + str(len(w['policy_profiles'])), '',
               'Profile Name                     Description  VLAN   AAA-Override  Status',
               '-----------------------------------------------------------------------------']
        for name, p in w['policy_profiles'].items():
            out.append(f'{name:<32} {"":<12} {p["vlan"]:<6} {"Yes" if p["aaa_override"] else "No":<13} '
                       f'{"DOWN" if p["shutdown"] else "UP"}')
        return '\n'.join(out)
    return None


def _show_wlan_summary(state) -> str:
    w = _wlc(state)
    out = [f'Number of WLANs: {len(w["wlans"])}', '',
           'ID   Profile Name                     SSID                             Status Security',
           '-------------------------------------------------------------------------------------------------------']
    for name, wl in sorted(w['wlans'].items(), key=lambda kv: kv[1]['id']):
        sec = '[WPA2][802.1x][AES]' if wl['akm'] == 'dot1x' else '[WPA2][PSK][AES]'
        out.append(f'{wl["id"]:<4} {name:<32} {wl["ssid"]:<32} {"DOWN" if wl["shutdown"] else "UP":<6} {sec}')
    return '\n'.join(out)


def _ap_name(state) -> str:
    return f'{state.hostname}-AP1'


def _show_client_summary(state) -> str:
    clients = _wlc(state)['clients']
    out = [f'Number of Clients: {len(clients)}', '',
           'MAC Address    AP Name                          Type ID   State             Protocol Method     Role',
           '-----------------------------------------------------------------------------------------------------------']
    for mac, cl in clients.items():
        out.append(f'{_cisco_mac(mac):<14} {_ap_name(state):<32} WLAN {cl["wlan_id"]:<4} '
                   f'{cl["state"]:<17} 11ac     {cl["method"]:<10} Local')
    return '\n'.join(out)


def _show_client_detail(state, mac_q: str) -> str:
    q = mac_q.replace('.', '').replace(':', '').replace('-', '').lower()
    for mac, cl in _wlc(state)['clients'].items():
        if mac.replace(':', '').lower() == q:
            return '\n'.join([
                f'Client MAC Address : {_cisco_mac(mac)}',
                f'Client Username : {cl.get("user") or "N/A"}',
                f'AP Name: {_ap_name(state)}',
                f'Wireless LAN Id: {cl["wlan_id"]}',
                f'WLAN Profile Name: {cl["wlan"]}',
                f'Wireless LAN Network Name (SSID): {cl["ssid"]}',
                f'Policy Profile : {cl["policy"]}',
                f'Client State : Associated',
                f'Policy Manager State: {cl["state"]}',
                f'VLAN : {cl["vlan"]}',
                f'Policy Type : WPA2',
                f'Encryption Cipher : CCMP (AES)',
                f'Authentication Key Management : {"802.1x" if cl["method"] == "Dot1x" else "PSK"}',
                f'EAP Type : {cl.get("eap_desc") or "Not Applicable"}',
                f'AAA Override Passed : {"Yes" if cl.get("aaa_override_applied") else "No"}',
            ])
    return '% Client not found'


def wlc_running_config(state) -> list:
    w = getattr(state, 'wireless', None)
    if not w:
        return []
    out = []
    for name, p in w['policy_profiles'].items():
        if name == 'default-policy-profile' and p == {'vlan': '1', 'aaa_override': False, 'shutdown': False}:
            continue
        out.append(f'wireless profile policy {name}')
        if p['aaa_override']:
            out.append(' aaa-override')
        out.append(f' vlan {p["vlan"]}')
        out.append(' shutdown' if p['shutdown'] else ' no shutdown')
    for tname, tag in w['policy_tags'].items():
        out.append(f'wireless tag policy {tname}')
        for wl, pol in tag.items():
            out.append(f' wlan {wl} policy {pol}')
    for name, wl in sorted(w['wlans'].items(), key=lambda kv: kv[1]['id']):
        out.append(f'wlan {name} {wl["id"]} {wl["ssid"]}')
        if wl['akm'] == 'psk':
            out.append(' no security wpa akm dot1x')
            out.append(' security wpa akm psk')
            if wl['psk']:
                out.append(f' security wpa psk set-key ascii 0 {wl["psk"]}')
        elif wl['auth_list']:
            out.append(f' security dot1x authentication-list {wl["auth_list"]}')
        out.append(' shutdown' if wl['shutdown'] else ' no shutdown')
    return out


# ── PC側サプリカント(nmcli) ────────────────────────────────────

def _nm(state) -> dict:
    if not hasattr(state, 'nm') or not isinstance(state.nm, dict):
        state.nm = {'connections': {}, 'active': {}}
    return state.nm


def _ensure_wlan0(device_id, state):
    if 'wlan0' not in state.interfaces:
        state.interfaces['wlan0'] = {'ip': '', 'prefix': 0, 'status': 'down',
                                     'mac': _mac_for(device_id, state, 'wlan0'),
                                     'speed': '', 'duplex': ''}


def _link_peer(device_id: str, ifname: str, device_sessions: dict, want_types):
    from engine.protocols import vnet
    for peer in vnet.links.get(device_id, set()):
        if vnet.interface_links.get(device_id, {}).get(peer) != ifname:
            continue
        pstate = device_sessions.get(peer)
        if pstate is not None and pstate.device_type in want_types:
            return peer, pstate, vnet.interface_links.get(peer, {}).get(device_id)
    return None, None, None


def _uuid(name: str) -> str:
    h = hashlib.md5(name.encode()).hexdigest()
    return f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}'


_NM_OPT = re.compile(r'(\S+)\s+("[^"]*"|\S+)')


def handle_pc(device_id: str, state, cmd: str, device_sessions: dict) -> Optional[str]:
    orig = cmd.strip()
    m = re.match(r'^nmcli\s+(?:-\S+\s+)*(connection|con|c|device|dev|d|general|g|radio|r)\b\s*(.*)$',
                 orig, re.I)
    if not m:
        if orig.lower() == 'nmcli':
            return _nm_device_status(device_id, state)
        return None
    obj, rest = m.group(1).lower(), m.group(2).strip()
    nm = _nm(state)
    if obj in ('general', 'g'):
        return 'STATE      CONNECTIVITY  WIFI-HW  WIFI     WWAN-HW  WWAN\nconnected  full          enabled  enabled  missing  enabled'
    if obj in ('radio', 'r'):
        return 'WIFI-HW  WIFI     WWAN-HW  WWAN\nenabled  enabled  missing  enabled'

    if obj in ('device', 'dev', 'd'):
        if rest in ('', 'status'):
            return _nm_device_status(device_id, state)
        if re.match(r'^wifi(\s+list)?(\s+ifname\s+\S+)?$', rest, re.I):
            _ensure_wlan0(device_id, state)
            return _nm_wifi_list(device_id, state, device_sessions)
        mc = re.match(r'^wifi\s+connect\s+("[^"]+"|\S+)(?:\s+password\s+("[^"]+"|\S+))?(?:\s+ifname\s+(\S+))?$',
                      rest, re.I)
        if mc:
            ssid = mc.group(1).strip('"')
            _ensure_wlan0(device_id, state)
            prof = {'type': 'wifi', 'ifname': mc.group(3) or 'wlan0', 'ssid': ssid,
                    'key_mgmt': 'wpa-psk' if mc.group(2) else 'none',
                    'psk': (mc.group(2) or '').strip('"')}
            ap = _find_ssid(device_id, prof['ifname'], ssid, device_sessions)
            if not ap:
                return f"Error: No network with SSID '{ssid}' found."
            if ap[2]['akm'] == 'dot1x':
                return 'Error: Connection activation failed: 802-1x.identity: property is missing.'
            nm['connections'][ssid] = prof
            return _nm_up(device_id, state, ssid, device_sessions, from_connect=True)
        return f'Error: argument \'{rest.split()[0] if rest else ""}\' not understood.'

    # connection
    if rest in ('', 'show'):
        return _nm_conn_show(state, active_only=False)
    if re.match(r'^show\s+--active$', rest) or rest == 'show -a':
        return _nm_conn_show(state, active_only=True)
    ma = re.match(r'^add\s+(.*)$', rest, re.I)
    if ma:
        opts = {k.lower(): v.strip('"') for k, v in _NM_OPT.findall(ma.group(1))}
        ctype = opts.get('type', '')
        if ctype not in ('ethernet', 'wifi', '802-3-ethernet', '802-11-wireless'):
            return "Error: invalid or not allowed setting 'connection': 'type' must be specified."
        name = opts.get('con-name') or opts.get('connection.id')
        if not name:
            return 'Error: con-name is required.'
        wifi = ctype in ('wifi', '802-11-wireless')
        prof = {'type': 'wifi' if wifi else 'ethernet',
                'ifname': opts.get('ifname', 'wlan0' if wifi else 'eth0'),
                'ssid': opts.get('ssid', ''),
                'key_mgmt': opts.get('wifi-sec.key-mgmt', 'wpa-eap' if opts.get('802-1x.eap') else 'none'),
                'psk': opts.get('wifi-sec.psk', ''),
                'eap': opts.get('802-1x.eap', ''),
                'identity': opts.get('802-1x.identity', ''),
                'password': opts.get('802-1x.password', ''),
                'phase2': opts.get('802-1x.phase2-auth', '')}
        if wifi and not prof['ssid']:
            return "Error: Failed to add '%s' connection: 802-11-wireless.ssid: property is missing" % name
        if prof['eap'] and not prof['identity']:
            return "Error: Failed to add '%s' connection: 802-1x.identity: property is missing" % name
        if wifi:
            _ensure_wlan0(device_id, state)
        nm['connections'][name] = prof
        return f"Connection '{name}' ({_uuid(name)}) successfully added."
    mu = re.match(r'^(up|down|delete|del)\s+(?:id\s+)?("[^"]+"|\S+)$', rest, re.I)
    if mu:
        action, name = mu.group(1).lower(), mu.group(2).strip('"')
        if name not in nm['connections']:
            return f"Error: unknown connection '{name}'."
        if action == 'up':
            return _nm_up(device_id, state, name, device_sessions)
        if action == 'down':
            if name not in nm['active'].values():
                return f"Error: '{name}' is not an active connection.\nError: no active connection provided."
            _nm_down(device_id, state, name, device_sessions)
            return (f"Connection '{name}' successfully deactivated "
                    f"(D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/1)")
        if name in nm['active'].values():
            _nm_down(device_id, state, name, device_sessions)
        nm['connections'].pop(name)
        return f"Connection '{name}' successfully deleted."
    return f"Error: argument '{rest.split()[0]}' not understood."


def _nm_device_status(device_id, state) -> str:
    nm = _nm(state)
    out = ['DEVICE  TYPE      STATE         CONNECTION']
    for ifname in sorted(state.interfaces):
        if ifname == 'lo':
            continue
        typ = 'wifi' if ifname.startswith('wlan') else 'ethernet'
        con = nm['active'].get(ifname)
        st = 'connected' if con else ('disconnected' if typ == 'wifi' or state.interfaces[ifname].get('ip')
                                      else 'unavailable')
        if typ == 'ethernet' and not con and state.interfaces[ifname].get('ip'):
            st, con = 'connected', f'Wired connection 1'
        out.append(f'{ifname:<7} {typ:<9} {st:<13} {con or "--"}')
    out.append(f'{"lo":<7} {"loopback":<9} {"unmanaged":<13} --')
    return '\n'.join(out)


def _nm_conn_show(state, active_only: bool) -> str:
    nm = _nm(state)
    out = ['NAME                UUID                                  TYPE      DEVICE']
    for name, prof in nm['connections'].items():
        dev = next((i for i, n in nm['active'].items() if n == name), '--')
        if active_only and dev == '--':
            continue
        out.append(f'{name:<19} {_uuid(name)}  {"wifi" if prof["type"] == "wifi" else "ethernet":<9} {dev}')
    return '\n'.join(out)


def _wlc_peers(device_id, ifname, device_sessions):
    from engine.protocols import vnet
    for peer in vnet.links.get(device_id, set()):
        if vnet.interface_links.get(device_id, {}).get(peer) != ifname:
            continue
        ps = device_sessions.get(peer)
        if ps is not None and ps.device_type in WLC_DEVICE_TYPES:
            yield peer, ps


def _find_ssid(device_id, ifname, ssid, device_sessions):
    for peer, ps in _wlc_peers(device_id, ifname, device_sessions):
        for name, wl in _wlc(ps)['wlans'].items():
            if wl['ssid'] == ssid and not wl['shutdown']:
                return peer, ps, wl, name
    return None


def _nm_wifi_list(device_id, state, device_sessions) -> str:
    nm = _nm(state)
    out = ['IN-USE  BSSID              SSID              MODE   CHAN  RATE        SIGNAL  BARS  SECURITY']
    active_ssid = None
    if nm['active'].get('wlan0'):
        active_ssid = nm['connections'].get(nm['active']['wlan0'], {}).get('ssid')
    for peer, ps in _wlc_peers(device_id, 'wlan0', device_sessions):
        for name, wl in sorted(_wlc(ps)['wlans'].items(), key=lambda kv: kv[1]['id']):
            if wl['shutdown']:
                continue
            h = hashlib.md5(f'{peer}/{wl["id"]}'.encode()).hexdigest()
            bssid = ':'.join(h[i:i + 2] for i in range(0, 12, 2)).upper()
            sec = 'WPA2 802.1X' if wl['akm'] == 'dot1x' else 'WPA2'
            inuse = '*' if wl['ssid'] == active_ssid else ' '
            out.append(f'{inuse:<7} {bssid}  {wl["ssid"]:<17} Infra  36    540 Mbit/s  82      ▂▄▆█  {sec}')
    return '\n'.join(out)


def _activated() -> str:
    return 'Connection successfully activated (D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/2)'


def _nm_up(device_id, state, name, device_sessions, from_connect=False) -> str:
    nm = _nm(state)
    prof = nm['connections'][name]
    ifname = prof['ifname']
    if ifname not in state.interfaces:
        return f"Error: Connection activation failed: No suitable device found for this connection (device {ifname} not available)."
    prev = nm['active'].get(ifname)
    if prev and prev != name:
        _nm_down(device_id, state, prev, device_sessions)
    if prof['type'] == 'wifi':
        res = _wifi_associate(device_id, state, prof, device_sessions)
    else:
        res = _wired_8021x(device_id, state, prof, device_sessions)
    if res is not None:
        return res
    nm['active'][ifname] = name
    state.interfaces[ifname]['status'] = 'up'
    if from_connect:
        return f"Device '{ifname}' successfully activated with '{_uuid(name)}'."
    return _activated()


def _nm_down(device_id, state, name, device_sessions) -> None:
    nm = _nm(state)
    prof = nm['connections'].get(name, {})
    ifname = prof.get('ifname')
    if nm['active'].get(ifname) != name:
        return
    nm['active'].pop(ifname, None)
    if prof.get('type') == 'wifi':
        for peer, ps in _wlc_peers(device_id, ifname, device_sessions):
            mac = _mac_for(device_id, state, ifname)
            cl = _wlc(ps)['clients'].pop(mac, None)
            if cl:
                _acct(ps, cl, ACCT_STOP, f'capwap_{_ap_name(ps)}')
        state.interfaces[ifname]['status'] = 'down'
        return
    peer, ps, port = _link_peer(device_id, ifname, device_sessions, DOT1X_DEVICE_TYPES)
    if ps is None or not port:
        return
    d = _dot1x(ps)
    s = d['sessions'].pop(port, None)
    if s:
        if s.get('orig_vlan') is not None:
            ps.interfaces[port]['vlan'] = s['orig_vlan']
        _acct(ps, s, ACCT_STOP, port)
    reconcile_ports(peer, ps)


def _acct(nas_state, sess: dict, status: int, port: str) -> None:
    d = getattr(nas_state, 'dot1x', None) or {}
    group = d.get('acct')
    if not group or not sess.get('user'):
        return
    st = None if status == ACCT_START else int(time.time() - sess.get('start', time.time()))
    aaa_radius.account(nas_state, group, status, sess['user'], sess['acct_session_id'],
                       nas_port_id=port, calling_station=_calling_station(sess['mac']),
                       session_time=st)


def _wired_8021x(device_id, state, prof, device_sessions) -> Optional[str]:
    ifname = prof['ifname']
    if not prof.get('eap'):
        return None
    peer, ps, port = _link_peer(device_id, ifname, device_sessions, DOT1X_DEVICE_TYPES)
    timeout_msg = 'Error: Connection activation failed: 802.1X supplicant took too long to authenticate'
    if ps is None or not port:
        return timeout_msg
    d = _dot1x(ps)
    pcfg = d['ports'].get(port, {})
    if not (d['system_auth_control'] and pcfg.get('port_control') == 'auto' and pcfg.get('pae')):
        # オーセンティケータがEAPOLに応答しない(実機ではEAPOL-Startが無視される)
        return timeout_msg
    wire, desc = _eap_wire_method(prof['eap'])
    mac = _mac_for(device_id, state, ifname)
    sess = {'user': prof['identity'], 'mac': mac, 'status': 'Unauthorized',
            'session_id': os.urandom(12).hex().upper(), 'acct_session_id': f'0x{os.urandom(4).hex()}',
            'pc': device_id, 'ip': state.interfaces.get(ifname, {}).get('ip', ''),
            'eap_desc': desc, 'vlan': ps.interfaces.get(port, {}).get('vlan'), 'start': time.time()}
    d['sessions'][port] = sess
    if wire is None:
        reconcile_ports(peer, ps)
        return f'Error: Connection activation failed: 802.1X supplicant failed ({desc} is not supported by this emulator)'
    group = d['authc_lists'].get('default')
    if not group:
        reconcile_ports(peer, ps)
        return 'Error: Connection activation failed: 802.1X supplicant failed'
    res = aaa_radius.authenticate(ps, group, prof['identity'], prof['password'], method=wire,
                                  nas_port_id=port, calling_station=_calling_station(mac),
                                  service_type=2)
    if res.status in ('timeout', 'error'):
        reconcile_ports(peer, ps)
        return timeout_msg
    if not res.accepted:
        reconcile_ports(peer, ps)
        return 'Error: Connection activation failed: 802.1X supplicant failed'
    sess['status'] = 'Authorized'
    if d.get('authz_network') and res.vlan:
        sess['orig_vlan'] = ps.interfaces[port].get('vlan')
        ps.interfaces[port]['vlan'] = res.vlan
        sess['vlan'] = res.vlan
        sess['vlan_assigned'] = True
    _acct(ps, sess, ACCT_START, port)
    reconcile_ports(peer, ps)
    return None


def _wifi_associate(device_id, state, prof, device_sessions) -> Optional[str]:
    found = _find_ssid(device_id, prof['ifname'], prof['ssid'], device_sessions)
    if not found:
        return 'Error: Connection activation failed: The Wi-Fi network could not be found'
    peer, ps, wl, wlan_name = found
    mac = _mac_for(device_id, state, prof['ifname'])
    policy = _wlan_policy(ps, wlan_name)
    cl = {'wlan': wlan_name, 'wlan_id': wl['id'], 'ssid': wl['ssid'], 'policy': policy['name'],
          'vlan': policy['vlan'], 'mac': mac, 'start': time.time(),
          'acct_session_id': f'0x{os.urandom(4).hex()}', 'user': ''}
    if wl['akm'] == 'psk':
        if prof['key_mgmt'] != 'wpa-psk' or prof['psk'] != wl['psk']:
            return 'Error: Connection activation failed: (7) Secrets were required, but not provided.'
        cl.update({'state': 'Run', 'method': 'PSK'})
        _wlc(ps)['clients'][mac] = cl
        return None
    if prof['key_mgmt'] != 'wpa-eap' or not prof.get('eap'):
        return 'Error: Connection activation failed: (7) Secrets were required, but not provided.'
    wire, desc = _eap_wire_method(prof['eap'])
    if wire is None:
        return f'Error: Connection activation failed: 802.1X supplicant failed ({desc} is not supported by this emulator)'
    group = _dot1x(ps)['authc_lists'].get(wl['auth_list'] or 'default')
    if not group:
        return 'Error: Connection activation failed: 802.1X supplicant failed'
    res = aaa_radius.authenticate(ps, group, prof['identity'], prof['password'], method=wire,
                                  nas_port_id=f'capwap_{_ap_name(ps)}', calling_station=_calling_station(mac),
                                  service_type=2)
    if res.status in ('timeout', 'error'):
        return 'Error: Connection activation failed: 802.1X supplicant took too long to authenticate'
    if not res.accepted:
        return 'Error: Connection activation failed: 802.1X supplicant failed'
    cl.update({'state': 'Run', 'method': 'Dot1x', 'user': prof['identity'], 'eap_desc': desc})
    if policy.get('aaa_override') and res.vlan:
        cl['vlan'] = res.vlan
        cl['aaa_override_applied'] = True
    _wlc(ps)['clients'][mac] = cl
    _acct(ps, cl, ACCT_START, f'capwap_{_ap_name(ps)}')
    return None


def handle(device_id: str, state, cmd: str, device_sessions: dict) -> Optional[str]:
    if state.device_type == 'pc':
        return handle_pc(device_id, state, cmd, device_sessions)
    out = None
    if state.device_type in DOT1X_DEVICE_TYPES + WLC_DEVICE_TYPES:
        out = handle_switch(state, cmd)
    if out is None and state.device_type in WLC_DEVICE_TYPES:
        out = handle_wlc(state, cmd)
    if out is not None and state.device_type in DOT1X_DEVICE_TYPES:
        reconcile_ports(device_id, state)
    return out
