"""
実gNMI（gRPC）サーバ

openconfig/gnmi の gnmi.proto 原本（engine/gnmi_proto/gnmi.proto）を
grpc_tools.protoc でコンパイルしたスタブを使い、**本物のgNMIクライアント**
（gnmic / pygnmi / gNMIc等）から Capabilities / Get / Set / Subscribe が
実行できる実サーバとして動作する。

NETCONF(engine/netconf_agent.py)・RESTCONF(app.py)と同じ
「装置のstate.interfaces」を読み書きするため、gNMIで設定した内容は
CLIの show ip interface brief からもそのまま見える。

IOS-XE 17.3以降のCLIは gnmi-yang ではなく gnxi 系:
    gnxi
    gnxi server            … 非TLS(既定ポート 50052)
    gnxi port <n>
    gnxi secure-init / secure-server / secure-port <n>  … TLS(既定 9339)
"""

import json
import os
import sys
import threading
import time
from concurrent import futures
from typing import Optional

_PROTO_DIR = os.path.join(os.path.dirname(__file__), 'gnmi_proto')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

try:
    import grpc                                  # noqa: E402
    import gnmi_pb2                              # noqa: E402
    import gnmi_pb2_grpc                         # noqa: E402
    _HAS_GRPC = True
except ImportError:                              # grpcio未導入環境
    _HAS_GRPC = False
    grpc = None
    gnmi_pb2 = None
    gnmi_pb2_grpc = None

GNMI_PORT = 50052          # IOS-XEの非TLS gNMI既定ポート
GNMI_SECURE_PORT = 9339    # TLS時の既定ポート

# Capabilities で広告するモデル。NETCONF/RESTCONFと同じ土俵に揃える。
SUPPORTED_MODELS = [
    ('Cisco-IOS-XE-native', 'Cisco Systems, Inc.', '2026-09-01'),
    ('ietf-interfaces', 'IETF', '2014-05-08'),
    ('openconfig-interfaces', 'OpenConfig working group', '2021-04-06'),
]
GNMI_VERSION = '0.8.0'


# ── パス変換 ────────────────────────────────────────────
def path_to_str(path) -> str:
    """gnmi.Path を "native:native/hostname" のような文字列にする"""
    if path is None:
        return ''
    parts = []
    for e in path.elem:
        seg = e.name
        if e.key:
            for k, v in sorted(e.key.items()):
                seg += f'[{k}={v}]'
        parts.append(seg)
    return '/'.join(parts)


def path_keys(path, elem_name: str) -> dict:
    for e in path.elem:
        if e.name == elem_name:
            return dict(e.key)
    return {}


# ── データモデル（NETCONF/RESTCONFと共有）────────────────
def _iface_json(name: str, info: dict) -> dict:
    out = {
        'name': name,
        'type': 'iana-if-type:ethernetCsmacd',
        'enabled': info.get('status') not in (
            'down', 'notconnect', 'disabled', 'administratively down'),
    }
    desc = info.get('desc') or info.get('description')
    if desc:
        out['description'] = desc
    if info.get('ip'):
        prefix = int(info.get('prefix', 24) or 24)
        mask = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix else 0
        netmask = '.'.join(str((mask >> s) & 0xff) for s in (24, 16, 8, 0))
        out['ietf-ip:ipv4'] = {
            'address': [{'ip': info['ip'], 'netmask': netmask}]}
    return out


def path_elems(path):
    """gnmi.Path を [(名前, キーdict), ...] に分解する。

    パスを "a/b[name=X]" のような文字列に潰してから "/" で分割すると、
    Cisco のインタフェース名 (GigabitEthernet1/0/1) がスラッシュで
    バラバラになって解決できない。必ずelemのまま扱う。
    """
    if path is None:
        return []
    return [(e.name, dict(e.key)) for e in path.elem]


def get_value(state, elems):
    """パス要素に対応する値を返す。未対応なら None。"""
    if not elems:
        return None
    head, _ = elems[0]
    rest = elems[1:]

    if head in ('Cisco-IOS-XE-native:native', 'native'):
        if not rest:
            return {'hostname': state.hostname}
        if [n for n, _ in rest] == ['hostname']:
            return state.hostname
        return None

    if head in ('ietf-interfaces:interfaces', 'interfaces',
                'openconfig-interfaces:interfaces'):
        ifaces = getattr(state, 'interfaces', {}) or {}
        if not rest:
            return {'interface': [_iface_json(n, i) for n, i in ifaces.items()]}
        name0, keys0 = rest[0]
        if name0 != 'interface':
            return None
        key = keys0.get('name', '')
        if not key:
            return {'interface': [_iface_json(n, i) for n, i in ifaces.items()]}
        info = ifaces.get(key)
        if info is None:
            return None
        entry = _iface_json(key, info)
        if len(rest) >= 2:
            return entry.get(rest[1][0])
        return entry
    return None


def set_value(state, elems, val, delete: bool = False):
    """パス要素に値を書き込む。(成功bool, エラーメッセージ) を返す。"""
    if not elems:
        return False, 'empty path'
    head, _ = elems[0]
    rest = elems[1:]
    names = [n for n, _ in rest]

    if head in ('Cisco-IOS-XE-native:native', 'native') and names == ['hostname']:
        if delete:
            return False, 'hostname cannot be deleted'
        state.hostname = str(val).strip('"')
        return True, ''

    if head in ('ietf-interfaces:interfaces', 'interfaces',
                'openconfig-interfaces:interfaces'):
        if not rest or rest[0][0] != 'interface':
            return False, f'unsupported path: {head}'
        key = rest[0][1].get('name', '')
        if not key:
            return False, 'interface name key is required'
        ifaces = getattr(state, 'interfaces', {}) or {}
        if key not in ifaces:
            return False, f'interface {key} does not exist'
        info = ifaces[key]
        leaf = rest[1][0] if len(rest) >= 2 else ''

        if delete:
            if leaf == 'description':
                info['desc'] = ''
            elif leaf in ('ietf-ip:ipv4', 'ipv4'):
                info['ip'] = ''
                info['prefix'] = 0
            elif not leaf:
                info['ip'] = ''
                info['prefix'] = 0
                info['desc'] = ''
            else:
                return False, f'cannot delete {leaf}'
            return True, ''

        if isinstance(val, (bytes, bytearray)):
            val = val.decode()
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (ValueError, TypeError):
                pass

        if leaf == 'description':
            info['desc'] = str(val)
            return True, ''
        if leaf == 'enabled':
            info['status'] = ('up' if val in (True, 'true', 1)
                              else 'administratively down')
            return True, ''
        if not leaf and isinstance(val, dict):
            if 'description' in val:
                info['desc'] = str(val['description'])
            if 'enabled' in val:
                info['status'] = ('up' if val['enabled'] in (True, 'true', 1)
                                  else 'administratively down')
            v4 = val.get('ietf-ip:ipv4') or val.get('ipv4')
            if isinstance(v4, dict):
                addrs = v4.get('address') or []
                if addrs and isinstance(addrs[0], dict) and addrs[0].get('ip'):
                    info['ip'] = addrs[0]['ip']
                    nm = addrs[0].get('netmask', '255.255.255.0')
                    try:
                        info['prefix'] = sum(
                            bin(int(o)).count('1') for o in nm.split('.'))
                    except ValueError:
                        info['prefix'] = 24
            return True, ''
        return False, f'unsupported leaf: {leaf or "(container)"}'
    return False, f'unsupported path: {head}'


# ── gNMI サービス実装 ───────────────────────────────────
if _HAS_GRPC:

    class GnmiServicer(gnmi_pb2_grpc.gNMIServicer):
        def __init__(self, device_id: str, state, on_change=None):
            self.device_id = device_id
            self.state = state
            self.on_change = on_change

        # -- Capabilities --------------------------------
        def Capabilities(self, request, context):
            resp = gnmi_pb2.CapabilityResponse()
            for name, org, ver in SUPPORTED_MODELS:
                m = resp.supported_models.add()
                m.name, m.organization, m.version = name, org, ver
            resp.supported_encodings.extend(
                [gnmi_pb2.JSON_IETF, gnmi_pb2.JSON])
            resp.gNMI_version = GNMI_VERSION
            return resp

        # -- Get -----------------------------------------
        def Get(self, request, context):
            resp = gnmi_pb2.GetResponse()
            now = int(time.time() * 1e9)
            for path in request.path:
                ps = path_to_str(path)
                val = get_value(self.state, path_elems(path))
                if val is None:
                    context.abort(grpc.StatusCode.NOT_FOUND,
                                  f'path not found: {ps}')
                notif = resp.notification.add()
                notif.timestamp = now
                notif.prefix.CopyFrom(request.prefix)
                upd = notif.update.add()
                upd.path.CopyFrom(path)
                upd.val.json_ietf_val = json.dumps(val).encode()
            return resp

        # -- Set -----------------------------------------
        def Set(self, request, context):
            resp = gnmi_pb2.SetResponse()
            resp.timestamp = int(time.time() * 1e9)
            changed = False

            def _record(path, op):
                r = resp.response.add()
                r.path.CopyFrom(path)
                r.op = op

            for d in request.delete:
                ok, err = set_value(self.state, path_elems(d), None, delete=True)
                if not ok:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, err)
                changed = True
                _record(d, gnmi_pb2.UpdateResult.DELETE)
            for u in request.replace:
                ok, err = set_value(self.state, path_elems(u.path),
                                    _typed_value(u.val))
                if not ok:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, err)
                changed = True
                _record(u.path, gnmi_pb2.UpdateResult.REPLACE)
            for u in request.update:
                ok, err = set_value(self.state, path_elems(u.path),
                                    _typed_value(u.val))
                if not ok:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, err)
                changed = True
                _record(u.path, gnmi_pb2.UpdateResult.UPDATE)
            if changed and self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass
            return resp

        # -- Subscribe -----------------------------------
        def Subscribe(self, request_iterator, context):
            """ONCE / POLL / STREAM(SAMPLE) に対応"""
            sub_list = None
            for req in request_iterator:
                if req.HasField('subscribe'):
                    sub_list = req.subscribe
                    mode = sub_list.mode
                    if mode == gnmi_pb2.SubscriptionList.ONCE:
                        for m in self._updates(sub_list):
                            yield m
                        yield self._sync()
                        return
                    if mode == gnmi_pb2.SubscriptionList.POLL:
                        # 最初の応答を返し、以降はpollリクエストのたびに返す
                        for m in self._updates(sub_list):
                            yield m
                        yield self._sync()
                        continue
                    # STREAM
                    for m in self._updates(sub_list):
                        yield m
                    yield self._sync()
                    interval = min(
                        (s.sample_interval for s in sub_list.subscription
                         if s.sample_interval), default=10_000_000_000)
                    interval = max(interval / 1e9, 0.1)
                    while context.is_active():
                        time.sleep(interval)
                        if not context.is_active():
                            return
                        for m in self._updates(sub_list):
                            yield m
                    return
                if req.HasField('poll'):
                    if sub_list is None:
                        context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                                      'poll received before subscribe')
                    for m in self._updates(sub_list):
                        yield m
                    yield self._sync()

        def _updates(self, sub_list):
            now = int(time.time() * 1e9)
            for sub in sub_list.subscription:
                val = get_value(self.state, path_elems(sub.path))
                if val is None:
                    continue
                resp = gnmi_pb2.SubscribeResponse()
                resp.update.timestamp = now
                u = resp.update.update.add()
                u.path.CopyFrom(sub.path)
                u.val.json_ietf_val = json.dumps(val).encode()
                yield resp

        @staticmethod
        def _sync():
            resp = gnmi_pb2.SubscribeResponse()
            resp.sync_response = True
            return resp

    def _typed_value(tv):
        """gnmi.TypedValue から素のPython値を取り出す"""
        which = tv.WhichOneof('value')
        if which is None:
            return None
        v = getattr(tv, which)
        if which in ('json_ietf_val', 'json_val'):
            try:
                return json.loads(v.decode())
            except (ValueError, AttributeError):
                return v
        return v

else:                                            # grpcio未導入時のダミー
    GnmiServicer = None

    def _typed_value(tv):
        return None


# ── サーバ管理 ──────────────────────────────────────────
_servers = {}


class GnmiServer:
    """1装置ぶんのgNMIリスナー"""

    def __init__(self, device_id: str, ip: str, state,
                 port: int = GNMI_PORT, on_change=None):
        self.device_id = device_id
        self.ip = ip
        self.port = port
        self.state = state
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        gnmi_pb2_grpc.add_gNMIServicer_to_server(
            GnmiServicer(device_id, state, on_change), self._server)
        self._server.add_insecure_port(f'{ip}:{port}')

    def start(self):
        self._server.start()

    def stop(self):
        try:
            self._server.stop(0)
        except Exception:
            pass


def ensure_gnmi_agent(device_id: str, device_sessions: dict, on_change=None):
    """gnxi server が有効な装置のgNMIリスナーを起動（起動済みなら何もしない）"""
    if not _HAS_GRPC:
        return None
    if device_id in _servers:
        return _servers[device_id]
    state = device_sessions.get(device_id)
    if state is None or not getattr(state, 'gnxi_server', False):
        return None
    if state.device_type not in ('cisco', 'catalyst'):
        return None
    ip = None
    for _n, info in (getattr(state, 'interfaces', {}) or {}).items():
        if info.get('ip') and info['ip'] != '127.0.0.1':
            ip = info['ip']
            break
    if not ip:
        return None
    port = int(getattr(state, 'gnxi_port', GNMI_PORT) or GNMI_PORT)
    try:
        srv = GnmiServer(device_id, ip, state, port=port, on_change=on_change)
        srv.start()
        _servers[device_id] = srv
        print(f'[gNMI] {device_id} ({ip}:{port}) 実リスナーを起動しました')
        return srv
    except Exception as e:
        print(f'[gNMI] {device_id} ({ip}:{port}) 起動失敗: {e}')
        return None


def stop_gnmi_agent(device_id: str):
    srv = _servers.pop(device_id, None)
    if srv is None:
        return False
    srv.stop()
    print(f'[gNMI] {device_id} 実リスナーを停止しました')
    return True


def format_show_gnxi_state(state, detail: bool = False) -> str:
    """show gnxi state [detail]

    実機の正確な桁揃えは公開情報から確認できなかったため、
    IOS-XEの一般的なstate表示に合わせた書式にしている。
    """
    enabled = getattr(state, 'gnxi_enabled', False)
    server = getattr(state, 'gnxi_server', False)
    sec_server = getattr(state, 'gnxi_secure_server', False)
    port = getattr(state, 'gnxi_port', GNMI_PORT)
    sec_port = getattr(state, 'gnxi_secure_port', GNMI_SECURE_PORT)
    if not enabled:
        return 'gNXI is not enabled'
    lines = ['Settings', '========']
    lines.append(f'  Server: {"Enabled" if server else "Disabled"}')
    lines.append(f'  Server port: {port}')
    lines.append(f'  Secure server: {"Enabled" if sec_server else "Disabled"}')
    lines.append(f'  Secure server port: {sec_port}')
    if detail:
        tp = getattr(state, 'gnxi_trustpoint', '') or 'not configured'
        lines.append(f'  Secure client authentication: '
                     f'{"Enabled" if getattr(state, "gnxi_secure_password_auth", False) else "Disabled"}')
        lines.append(f'  Secure trustpoint: {tp}')
        lines.append('')
        lines.append('GNMI')
        lines.append('====')
        lines.append(f'  Admin state: {"Enabled" if server or sec_server else "Disabled"}')
        lines.append(f'  Oper status: {"Up" if _servers.get(getattr(state, "_device_id", "")) or server else "Down"}')
        lines.append(f'  State: {"Provisioned" if sec_server else "Served"}')
        lines.append('')
        lines.append('gNMI Configuration service')
        lines.append('==========================')
        lines.append('  Admin state: Enabled')
        lines.append('  Supported encodings: JSON_IETF, JSON')
        lines.append(f'  gNMI version: {GNMI_VERSION}')
    return '\n'.join(lines)
