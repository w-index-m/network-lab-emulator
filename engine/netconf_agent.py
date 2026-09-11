"""
実NETCONFサーバ（RFC 6241 / RFC 6242）

Cisco IOS-XE の `netconf-yang` を有効にしたときに立ち上がる、SSH の
"netconf" サブシステム（TCP 830）を実装する。ncclient のような実物の
NETCONFクライアントから接続して get-config / get / edit-config が
実行できる。

RESTCONF実装(app.py)と同じ ietf-interfaces データモデルを共有するため、
NETCONFで edit-config した結果は show ip interface brief や RESTCONF
からも見える。

対応している範囲:
  - SSH transport + netconf subsystem（paramiko。ユーザ名/パスワードは
    装置のローカルユーザ、無ければ admin/admin）
  - <hello> 交換、base:1.0 の ]]>]]> 終端と base:1.1 のチャンク framing
  - <get-config source=running>, <get>, <edit-config target=running>,
    <close-session>, <kill-session>
  - edit-config の operation="merge"/"replace"/"delete"（ietf-interfaces
    の enabled / description / ipv4 address）

対応していない範囲（実機との差）:
  - candidate/startup データストア、confirmed-commit、validate
  - notification（telemetry）、with-defaults、XPath フィルタ
    （subtree フィルタのみ対応）
"""

import asyncio
import socket
import threading
import xml.etree.ElementTree as ET
from typing import Optional

try:
    import paramiko
    _HAS_PARAMIKO = True
except Exception:                                   # pragma: no cover
    _HAS_PARAMIKO = False

NETCONF_PORT = 830
_EOM = b']]>]]>'

NS = {
    'nc': 'urn:ietf:params:xml:ns:netconf:base:1.0',
    'if': 'urn:ietf:params:xml:ns:yang:ietf-interfaces',
    'ip': 'urn:ietf:params:xml:ns:yang:ietf-ip',
    'nacm': 'urn:ietf:params:xml:ns:yang:ietf-netconf-acm',
}

SERVER_CAPABILITIES = [
    'urn:ietf:params:netconf:base:1.0',
    'urn:ietf:params:netconf:base:1.1',
    'urn:ietf:params:netconf:capability:writable-running:1.0',
    'urn:ietf:params:netconf:capability:xpath:1.0',
    'urn:ietf:params:xml:ns:yang:ietf-interfaces?module=ietf-interfaces&revision=2014-05-08',
    'urn:ietf:params:xml:ns:yang:ietf-ip?module=ietf-ip&revision=2014-06-16',
    'urn:ietf:params:xml:ns:yang:ietf-netconf-acm'
    '?module=ietf-netconf-acm&revision=2018-02-14',
]


def _prefix_to_mask(prefix: int) -> str:
    prefix = int(prefix or 0)
    mask = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix else 0
    return '.'.join(str((mask >> s) & 0xff) for s in (24, 16, 8, 0))


def _mask_to_prefix(mask: str) -> int:
    try:
        return sum(bin(int(o)).count('1') for o in mask.split('.'))
    except Exception:
        return 24


def _iface_enabled(iinfo: dict) -> bool:
    return iinfo.get('status') not in ('down', 'notconnect', 'disabled',
                                        'administratively down')


def _child(elem, name):
    """名前空間を問わず、ローカル名で直下の子要素を探す。

    クライアントによって <config> や <interfaces> に付く名前空間が
    異なる（ncclientは渡されたXMLの宣言をそのまま使う）ため、
    名前空間付きの find だけだと取りこぼす。
    """
    if elem is None:
        return None
    for c in list(elem):
        if c.tag.split('}')[-1] == name:
            return c
    return None


def _children(elem, name):
    if elem is None:
        return []
    return [c for c in list(elem) if c.tag.split('}')[-1] == name]


def _xml_escape(s: str) -> str:
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


# ══════════════════════════════════════════
# データモデル（ietf-interfaces）
# ══════════════════════════════════════════
def build_interfaces_xml(state) -> str:
    """装置の現在のインタフェース状態を ietf-interfaces のXMLで返す"""
    out = [f'<interfaces xmlns="{NS["if"]}">']
    for ifname, iinfo in state.interfaces.items():
        out.append('<interface>')
        out.append(f'<name>{ifname}</name>')
        out.append('<type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">'
                   'ianaift:ethernetCsmacd</type>')
        out.append(f'<enabled>{"true" if _iface_enabled(iinfo) else "false"}</enabled>')
        desc = iinfo.get('desc') or iinfo.get('description')
        if desc:
            out.append(f'<description>{desc}</description>')
        if iinfo.get('ip'):
            out.append(f'<ipv4 xmlns="{NS["ip"]}"><address>')
            out.append(f'<ip>{iinfo["ip"]}</ip>')
            out.append(f'<netmask>{_prefix_to_mask(iinfo.get("prefix", 24))}</netmask>')
            out.append('</address></ipv4>')
        out.append('</interface>')
    out.append('</interfaces>')
    return ''.join(out)


def apply_edit_config(state, config_elem) -> Optional[str]:
    """<edit-config>の<config>を装置状態へ反映する。
    エラーメッセージ文字列を返した場合は rpc-error になる。"""
    op_attr = f'{{{NS["nc"]}}}operation'
    ifaces = _child(config_elem, 'interfaces')
    if ifaces is None:
        return 'only ietf-interfaces is supported by this device'
    for iface in _children(ifaces, 'interface'):
        name_el = _child(iface, 'name')
        if name_el is None or not (name_el.text or '').strip():
            return 'interface name is required'
        name = name_el.text.strip()
        if name not in state.interfaces:
            return f'interface {name} does not exist'
        info = state.interfaces[name]
        op = iface.get(op_attr, 'merge')

        if op == 'delete':
            info['ip'] = ''
            info['prefix'] = 0
            continue

        en = _child(iface, 'enabled')
        if en is not None and en.text is not None:
            info['status'] = 'up' if en.text.strip().lower() == 'true' else 'down'
        desc = _child(iface, 'description')
        if desc is not None:
            info['desc'] = (desc.text or '').strip()
        v4 = _child(iface, 'ipv4')
        if v4 is not None:
            addr = _child(v4, 'address')
            if addr is not None:
                ip_el = _child(addr, 'ip')
                nm_el = _child(addr, 'netmask')
                if ip_el is not None and ip_el.text:
                    info['ip'] = ip_el.text.strip()
                if nm_el is not None and nm_el.text:
                    info['prefix'] = _mask_to_prefix(nm_el.text.strip())
    return None


# ══════════════════════════════════════════
# NACM（RFC 8341 / ietf-netconf-acm）
#
# Cisco IOS-XE の「モデルベースAAA」の実体。NETCONF/RESTCONFからの
# 読み書き・RPC実行を、ユーザが属するグループ単位で許可/拒否する。
# 設定はCLIではなくNETCONF経由（/nacm サブツリー）で行うのが規格。
#
# 既定値はRFC 8341のYANGモジュールどおり:
#   enable-nacm=true / read-default=permit / write-default=deny
#   exec-default=permit / enable-external-groups=true
# つまり「読めるが書けない」が既定。
# ══════════════════════════════════════════
_NACM_DEFAULTS = {
    'enable-nacm': True,
    'read-default': 'permit',
    'write-default': 'deny',
    'exec-default': 'permit',
    'enable-external-groups': True,
}
# 実機同様、この名前のグループに属するユーザは無制限（復旧用）
NACM_RECOVERY_GROUPS = ('ndm-admin', 'PRIV15')
# RFC 8341 の "recovery session"。NACMを一切迂回できる管理者セッションが
# 無いと、write-default=deny（既定）のせいで誰もNACMを設定できなくなり、
# 装置を永久に締め出してしまう。IOS-XEでは privilege 15 のローカル
# ユーザがこれに当たる。このエミュレータはローカルユーザDBを持たず
# 組み込みの admin が privilege 15 相当なので、それを復旧ユーザとする。
NACM_RECOVERY_USERS = ('admin',)


def _is_recovery(state, user: str) -> bool:
    if not user:
        return False
    if _user_groups(state, user) & set(NACM_RECOVERY_GROUPS):
        return True
    local = getattr(state, 'users', None) or []
    if local:
        # ローカルユーザが定義されていれば privilege 15 のみ復旧セッション
        return any(u.get('name') == user and int(u.get('privilege', 1)) >= 15
                   for u in local)
    # ローカルユーザDBが空のときは組み込みの admin が privilege 15 相当
    return user in NACM_RECOVERY_USERS


def get_nacm(state) -> dict:
    """装置のNACM設定を取り出す（無ければ既定値で作る）"""
    nacm = getattr(state, 'nacm', None)
    if not isinstance(nacm, dict):
        nacm = dict(_NACM_DEFAULTS)
        nacm['groups'] = {}        # {group_name: [user, ...]}
        nacm['rule-lists'] = []    # [{'name':..,'groups':[..],'rules':[..]}]
        nacm['denied-operations'] = 0
        nacm['denied-data-writes'] = 0
        nacm['denied-notifications'] = 0
        state.nacm = nacm
    return nacm


def build_nacm_xml(state) -> str:
    """/nacm サブツリーを ietf-netconf-acm のXMLで返す"""
    n = get_nacm(state)
    out = [f'<nacm xmlns="{NS["nacm"]}">']
    out.append(f'<enable-nacm>{"true" if n["enable-nacm"] else "false"}</enable-nacm>')
    out.append(f'<read-default>{n["read-default"]}</read-default>')
    out.append(f'<write-default>{n["write-default"]}</write-default>')
    out.append(f'<exec-default>{n["exec-default"]}</exec-default>')
    out.append('<enable-external-groups>'
               f'{"true" if n["enable-external-groups"] else "false"}'
               '</enable-external-groups>')
    out.append(f'<denied-operations>{n["denied-operations"]}</denied-operations>')
    out.append(f'<denied-data-writes>{n["denied-data-writes"]}</denied-data-writes>')
    out.append('<denied-notifications>'
               f'{n["denied-notifications"]}</denied-notifications>')
    if n['groups']:
        out.append('<groups>')
        for gname, users in n['groups'].items():
            out.append(f'<group><name>{_xml_escape(gname)}</name>')
            for u in users:
                out.append(f'<user-name>{_xml_escape(u)}</user-name>')
            out.append('</group>')
        out.append('</groups>')
    for rl in n['rule-lists']:
        out.append(f'<rule-list><name>{_xml_escape(rl["name"])}</name>')
        for g in rl.get('groups', []):
            out.append(f'<group>{_xml_escape(g)}</group>')
        for r in rl.get('rules', []):
            out.append(f'<rule><name>{_xml_escape(r["name"])}</name>')
            out.append('<module-name>'
                       f'{_xml_escape(r.get("module-name", "*"))}</module-name>')
            if r.get('rpc-name'):
                out.append(f'<rpc-name>{_xml_escape(r["rpc-name"])}</rpc-name>')
            if r.get('path'):
                out.append(f'<path>{_xml_escape(r["path"])}</path>')
            out.append('<access-operations>'
                       f'{_xml_escape(r.get("access-operations", "*"))}'
                       '</access-operations>')
            out.append(f'<action>{r.get("action", "deny")}</action>')
            if r.get('comment'):
                out.append(f'<comment>{_xml_escape(r["comment"])}</comment>')
            out.append('</rule>')
        out.append('</rule-list>')
    out.append('</nacm>')
    return ''.join(out)


def _bool_of(elem, default):
    if elem is None or elem.text is None:
        return default
    return elem.text.strip().lower() == 'true'


def apply_nacm_edit(state, nacm_elem) -> Optional[str]:
    """<edit-config>で渡された /nacm を反映する"""
    n = get_nacm(state)
    op_attr = f'{{{NS["nc"]}}}operation'

    for leaf in ('enable-nacm', 'enable-external-groups'):
        el = _child(nacm_elem, leaf)
        if el is not None:
            n[leaf] = _bool_of(el, n[leaf])
    for leaf in ('read-default', 'write-default', 'exec-default'):
        el = _child(nacm_elem, leaf)
        if el is not None and el.text:
            val = el.text.strip()
            if val not in ('permit', 'deny'):
                return f'{leaf} must be "permit" or "deny"'
            n[leaf] = val

    groups = _child(nacm_elem, 'groups')
    if groups is not None:
        for g in _children(groups, 'group'):
            gname_el = _child(g, 'name')
            if gname_el is None or not (gname_el.text or '').strip():
                return 'group name is required'
            gname = gname_el.text.strip()
            if g.get(op_attr) == 'delete':
                n['groups'].pop(gname, None)
                continue
            n['groups'][gname] = [
                (u.text or '').strip() for u in _children(g, 'user-name')
                if (u.text or '').strip()]

    for rl in _children(nacm_elem, 'rule-list'):
        name_el = _child(rl, 'name')
        if name_el is None or not (name_el.text or '').strip():
            return 'rule-list name is required'
        rl_name = name_el.text.strip()
        existing = next((x for x in n['rule-lists'] if x['name'] == rl_name), None)
        if rl.get(op_attr) == 'delete':
            if existing:
                n['rule-lists'].remove(existing)
            continue
        entry = {'name': rl_name,
                 'groups': [(g.text or '').strip() for g in _children(rl, 'group')
                            if (g.text or '').strip()],
                 'rules': []}
        for r in _children(rl, 'rule'):
            rname_el = _child(r, 'name')
            if rname_el is None or not (rname_el.text or '').strip():
                return 'rule name is required'
            action_el = _child(r, 'action')
            action = (action_el.text or '').strip() if action_el is not None else ''
            if action not in ('permit', 'deny'):
                return 'rule action must be "permit" or "deny"'

            def _txt(tag, default=''):
                el = _child(r, tag)
                return (el.text or '').strip() if el is not None and el.text \
                    else default

            entry['rules'].append({
                'name': rname_el.text.strip(),
                'module-name': _txt('module-name', '*'),
                'rpc-name': _txt('rpc-name'),
                'path': _txt('path'),
                'access-operations': _txt('access-operations', '*'),
                'action': action,
                'comment': _txt('comment'),
            })
        if existing:
            n['rule-lists'][n['rule-lists'].index(existing)] = entry
        else:
            n['rule-lists'].append(entry)
    return None


def _user_groups(state, user: str) -> set:
    n = get_nacm(state)
    return {g for g, users in n['groups'].items() if user in users}


def nacm_check(state, user: str, operation: str,
               module: str = '*', path: str = '') -> bool:
    """NACMのアクセス判定（RFC 8341 3.4の手順）

    operation は 'read' / 'create' / 'update' / 'delete' / 'exec'。
    write系(create/update/delete)は write-default を、readは read-default を、
    execは exec-default を既定として使う。
    """
    n = get_nacm(state)
    if not n['enable-nacm']:
        return True
    # 復旧セッションはNACMを完全に迂回する（RFC 8341 3.3）
    if _is_recovery(state, user):
        return True
    groups = _user_groups(state, user)

    for rl in n['rule-lists']:
        rl_groups = rl.get('groups', [])
        if '*' not in rl_groups and not (set(rl_groups) & groups):
            continue
        for r in rl.get('rules', []):
            mod = r.get('module-name', '*')
            if mod != '*' and module != '*' and mod != module:
                continue
            ops = r.get('access-operations', '*')
            if ops != '*' and operation not in ops.split():
                continue
            rpath = r.get('path', '')
            if rpath and path and rpath not in path:
                continue
            if r.get('rpc-name') and operation != 'exec':
                continue
            return r['action'] == 'permit'

    if operation == 'read':
        allowed = n['read-default'] == 'permit'
    elif operation == 'exec':
        allowed = n['exec-default'] == 'permit'
    else:
        allowed = n['write-default'] == 'permit'
    if not allowed:
        n['denied-operations'] += 1
        if operation in ('create', 'update', 'delete'):
            n['denied-data-writes'] += 1
    return allowed


# ══════════════════════════════════════════
# RPC処理
# ══════════════════════════════════════════
def handle_rpc(state, rpc_xml: str, on_change=None, user: str = '') -> str:
    """<rpc>を処理して<rpc-reply>の文字列を返す"""
    try:
        rpc = ET.fromstring(rpc_xml)
    except ET.ParseError as e:
        return _rpc_error('unknown', 'rpc', 'malformed-message', str(e))

    msg_id = rpc.get('message-id', '1')

    if _child(rpc, 'close-session') is not None:
        return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                f'<ok/></rpc-reply>')
    if _child(rpc, 'kill-session') is not None:
        return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                f'<ok/></rpc-reply>')

    get_cfg = _child(rpc, 'get-config')
    get_op = _child(rpc, 'get')
    if get_cfg is not None or get_op is not None:
        req = get_cfg if get_cfg is not None else get_op
        if get_cfg is not None:
            src = _child(get_cfg, 'source')
            # running以外のデータストアは未対応（実機はcandidate等も持つ）
            if src is not None and _child(src, 'running') is None:
                return _rpc_error(msg_id, 'protocol', 'operation-not-supported',
                                  'only the running datastore is supported')
        # subtreeフィルタ。/nacm を明示指定されたらNACM設定を返す
        filt = _child(req, 'filter')
        want_nacm = False
        if filt is not None and len(filt) > 0:
            if _child(filt, 'nacm') is not None:
                want_nacm = True
            elif _child(filt, 'interfaces') is None:
                return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                        f'<data/></rpc-reply>')
        mod = 'ietf-netconf-acm' if want_nacm else 'ietf-interfaces'
        if not nacm_check(state, user, 'read', mod,
                          '/nacm' if want_nacm else '/interfaces'):
            return _rpc_error(msg_id, 'application', 'access-denied',
                              'access denied by NACM')
        body = build_nacm_xml(state) if want_nacm else build_interfaces_xml(state)
        return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}"><data>'
                f'{body}</data></rpc-reply>')

    edit = _child(rpc, 'edit-config')
    if edit is not None:
        tgt = _child(edit, 'target')
        if tgt is not None and _child(tgt, 'running') is None:
            return _rpc_error(msg_id, 'protocol', 'operation-not-supported',
                              'only the running datastore is writable')
        cfg = _child(edit, 'config')
        if cfg is None:
            return _rpc_error(msg_id, 'protocol', 'missing-element',
                              '<config> is required')
        # /nacm 自体の書き換えもNETCONF経由で行う（規格どおりCLIでは触らない）
        nacm_el = _child(cfg, 'nacm')
        if nacm_el is not None:
            if not nacm_check(state, user, 'update', 'ietf-netconf-acm', '/nacm'):
                return _rpc_error(msg_id, 'application', 'access-denied',
                                  'access denied by NACM')
            err = apply_nacm_edit(state, nacm_el)
            if err:
                return _rpc_error(msg_id, 'application', 'invalid-value', err)
            return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                    f'<ok/></rpc-reply>')
        if not nacm_check(state, user, 'update', 'ietf-interfaces',
                          '/interfaces'):
            return _rpc_error(msg_id, 'application', 'access-denied',
                              'access denied by NACM')
        err = apply_edit_config(state, cfg)
        if err:
            return _rpc_error(msg_id, 'application', 'invalid-value', err)
        if on_change:
            try:
                on_change()
            except Exception:
                pass
        return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                f'<ok/></rpc-reply>')

    return _rpc_error(msg_id, 'protocol', 'operation-not-supported',
                      'unsupported operation')


def _rpc_error(msg_id, err_type, err_tag, message) -> str:
    return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}"><rpc-error>'
            f'<error-type>{err_type}</error-type>'
            f'<error-tag>{err_tag}</error-tag>'
            f'<error-severity>error</error-severity>'
            f'<error-message>{_xml_escape(message)}</error-message>'
            f'</rpc-error></rpc-reply>')


# ══════════════════════════════════════════
# フレーミング（RFC 6242）
# ══════════════════════════════════════════
def server_hello(session_id: int) -> str:
    # capability URIには "?module=...&revision=..." のように & が含まれる。
    # XMLとして送るのでエスケープしないとクライアント側でパースエラーになる。
    caps = ''.join(
        '<capability>{}</capability>'.format(
            c.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        for c in SERVER_CAPABILITIES)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<hello xmlns="{NS["nc"]}"><capabilities>{caps}</capabilities>'
            f'<session-id>{session_id}</session-id></hello>')


def frame_chunked(payload: str) -> bytes:
    data = payload.encode()
    return b'\n#%d\n' % len(data) + data + b'\n##\n'


def unframe_chunked(buf: bytes):
    """チャンク framing のバッファから1メッセージ取り出す。
    戻り値: (メッセージ or None, 残りバッファ)"""
    out = b''
    pos = 0
    while True:
        if not buf[pos:].startswith(b'\n#'):
            return None, buf
        nl = buf.find(b'\n', pos + 2)
        if nl < 0:
            return None, buf
        header = buf[pos + 2:nl]
        if header == b'#':                       # 終端 \n##\n
            return out.decode(errors='replace'), buf[nl + 1:]
        try:
            size = int(header)
        except ValueError:
            return None, buf
        if len(buf) < nl + 1 + size:
            return None, buf
        out += buf[nl + 1:nl + 1 + size]
        pos = nl + 1 + size


class _NetconfSshServer(paramiko.ServerInterface if _HAS_PARAMIKO else object):
    """paramikoのSSHサーバ側実装。netconfサブシステムのみ許可する"""

    def __init__(self, valid_users):
        self.valid_users = valid_users
        self.event = threading.Event()
        self.subsystem_ok = False
        # NACMの判定に使うので、認証に成功したユーザ名を覚えておく
        self.authenticated_user = ''

    def check_auth_password(self, username, password):
        if self.valid_users.get(username) == password:
            self.authenticated_user = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_subsystem_request(self, channel, name):
        # 実機同様 "netconf" サブシステムのみ受け付ける
        if name == 'netconf':
            self.subsystem_ok = True
            self.event.set()
            return True
        return False


def _device_users(state) -> dict:
    """装置のローカルユーザ（無ければ admin/admin）"""
    users = {}
    for u in getattr(state, 'users', []) or []:
        if isinstance(u, dict) and u.get('name') and u.get('password'):
            users[u['name']] = u['password']
    if not users:
        users['admin'] = 'admin'
    return users


def serve_connection(sock, state, host_key, session_id: int, on_change=None):
    """1本のTCP接続をNETCONFセッションとして処理する（ブロッキング）"""
    transport = paramiko.Transport(sock)
    transport.add_server_key(host_key)
    server = _NetconfSshServer(_device_users(state))
    try:
        transport.start_server(server=server)
        chan = transport.accept(20)
        if chan is None:
            return
        server.event.wait(10)
        if not server.subsystem_ok:
            chan.close()
            return

        chan.sendall(server_hello(session_id).encode() + _EOM)

        buf = b''
        client_hello_done = False
        chunked = False
        while True:
            data = chan.recv(65535)
            if not data:
                break
            buf += data

            while True:
                if not client_hello_done:
                    # helloは常に ]]>]]> 終端
                    idx = buf.find(_EOM)
                    if idx < 0:
                        break
                    hello = buf[:idx].decode(errors='replace')
                    buf = buf[idx + len(_EOM):]
                    client_hello_done = True
                    chunked = 'base:1.1' in hello
                    continue

                if chunked:
                    msg, rest = unframe_chunked(buf)
                    if msg is None:
                        break
                    buf = rest
                else:
                    idx = buf.find(_EOM)
                    if idx < 0:
                        break
                    msg = buf[:idx].decode(errors='replace')
                    buf = buf[idx + len(_EOM):]

                reply = handle_rpc(state, msg, on_change=on_change,
                                   user=server.authenticated_user)
                if chunked:
                    chan.sendall(frame_chunked(reply))
                else:
                    chan.sendall(reply.encode() + _EOM)
                if '<close-session' in msg:
                    chan.close()
                    return
    except Exception:
        pass
    finally:
        try:
            transport.close()
        except Exception:
            pass


class NetconfServer:
    """1装置ぶんのNETCONFリスナー（別スレッドで待ち受ける）"""

    def __init__(self, device_id: str, ip: str, state, port: int = NETCONF_PORT,
                 on_change=None):
        self.device_id = device_id
        self.ip = ip
        self.port = port
        self.state = state
        self.on_change = on_change
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._session_id = 0
        self.host_key = paramiko.RSAKey.generate(2048)

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.ip, self.port))
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                return
            # サービスレベルACL(netconf-yang ssh ipv4 access-list name <acl>)。
            # 実機は許可されていない送信元からのTCP接続をそのまま落とすので、
            # SSHのネゴシエーションに入る前に切る。
            if not self._acl_allows(_addr[0] if _addr else ''):
                print(f'[NETCONF] {self.device_id} {_addr[0]} を '
                      f'サービスレベルACLで拒否しました')
                try:
                    client.close()
                except Exception:
                    pass
                continue
            self._session_id += 1
            threading.Thread(
                target=serve_connection,
                args=(client, self.state, self.host_key, self._session_id,
                      self.on_change),
                daemon=True).start()

    def _acl_allows(self, src_ip: str) -> bool:
        """送信元がサービスレベルACLで許可されているか"""
        acl_name = (getattr(self.state, 'netconf_service_acl', {})
                    or {}).get('ipv4')
        if not acl_name or not src_ip:
            return True
        try:
            from engine.protocols import ipfilter_engine
            return ipfilter_engine.check_source(self.device_id, acl_name, src_ip)
        except Exception:
            return True

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass


_servers = {}


def ensure_netconf_agent(device_id: str, device_sessions: dict,
                         port: int = NETCONF_PORT, on_change=None):
    """netconf-yang が有効な装置のNETCONFリスナーを起動する（起動済みなら何もしない）"""
    if not _HAS_PARAMIKO:
        return None
    if device_id in _servers:
        return _servers[device_id]
    state = device_sessions.get(device_id)
    if state is None or not getattr(state, 'netconf_enabled', False):
        return None
    if state.device_type not in ('cisco', 'catalyst'):
        return None
    ip = None
    for _ifname, info in state.interfaces.items():
        if info.get('ip') and info['ip'] != '127.0.0.1':
            ip = info['ip']
            break
    if not ip:
        return None
    # netconf-yang ssh port <n> で待ち受けポートを変更できる
    port = int(getattr(state, 'netconf_ssh_port', port) or port)
    try:
        srv = NetconfServer(device_id, ip, state, port=port, on_change=on_change)
        srv.start()
        _servers[device_id] = srv
        print(f'[NETCONF] {device_id} ({ip}:{port}) 実リスナーを起動しました')
        return srv
    except Exception as e:
        print(f'[NETCONF] {device_id} ({ip}:{port}) 起動失敗: {e}')
        return None


def stop_netconf_agent(device_id: str):
    srv = _servers.pop(device_id, None)
    if srv:
        srv.stop()
