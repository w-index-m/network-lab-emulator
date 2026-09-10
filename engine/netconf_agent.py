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
}

SERVER_CAPABILITIES = [
    'urn:ietf:params:netconf:base:1.0',
    'urn:ietf:params:netconf:base:1.1',
    'urn:ietf:params:netconf:capability:writable-running:1.0',
    'urn:ietf:params:netconf:capability:xpath:1.0',
    'urn:ietf:params:xml:ns:yang:ietf-interfaces?module=ietf-interfaces&revision=2014-05-08',
    'urn:ietf:params:xml:ns:yang:ietf-ip?module=ietf-ip&revision=2014-06-16',
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
# RPC処理
# ══════════════════════════════════════════
def handle_rpc(state, rpc_xml: str, on_change=None) -> str:
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
        # subtreeフィルタ: ietf-interfaces以外が指定されたら空を返す
        filt = _child(req, 'filter')
        if filt is not None and len(filt) > 0:
            if _child(filt, 'interfaces') is None:
                return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}">'
                        f'<data/></rpc-reply>')
        return (f'<rpc-reply xmlns="{NS["nc"]}" message-id="{msg_id}"><data>'
                f'{build_interfaces_xml(state)}</data></rpc-reply>')

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

    def check_auth_password(self, username, password):
        if self.valid_users.get(username) == password:
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
        if isinstance(u, dict) and u.get('name'):
            users[u['name']] = u.get('password', 'admin')
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

                reply = handle_rpc(state, msg, on_change=on_change)
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
            self._session_id += 1
            threading.Thread(
                target=serve_connection,
                args=(client, self.state, self.host_key, self._session_id,
                      self.on_change),
                daemon=True).start()

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
