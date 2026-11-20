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
import time
from concurrent import futures

_PROTO_DIR = os.path.join(os.path.dirname(__file__), 'gnmi_proto')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

from engine.loopback_alias import ensure_loopback_alias

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


_IF_HEADS = ('ietf-interfaces:interfaces', 'interfaces', 'openconfig-interfaces:interfaces')
_COUNTER_KEYS = ('in-octets', 'out-octets', 'in-unicast-pkts', 'out-unicast-pkts',
                 'in-errors', 'out-errors', 'in-discards', 'out-discards')


def _bare(name: str) -> str:
    """"openconfig-interfaces:state" のようなモジュール接頭辞を外す"""
    return name.split(':', 1)[1] if ':' in name else name


def _oc_state(device_id, name: str, info: dict) -> dict:
    """OpenConfig の interfaces/interface/state(counters 含む)。

    カウンタは SNMP の IF-MIB(ifHCIn/OutOctets 等)と同じデータプレーンのカウンタ
    (engine.protocols.dp_engine)から取るので、gNMI と SNMP で値が食い違わない。
    状態も IF-MIB と同じ考え方: shutdown なら admin/oper とも DOWN、それ以外は
    リンクがつながっているか IP があれば oper UP。uint64 は RFC 7951 どおり文字列。
    """
    counters = {}
    shutdown = info.get('status') in ('down', 'notconnect', 'disabled', 'administratively down')
    linked = False
    last_change = None
    try:
        from engine.protocols import dp_engine, vnet
        norm = vnet._norm_iface
        k = norm(name)
        counters = dp_engine._zero()
        if device_id:
            per_dev = dp_engine.counters.get(device_id, {})
            key = name if name in per_dev else next((n for n in per_dev if norm(n) == k), None)
            if key is not None:
                counters = dp_engine.get_counter(device_id, key)
            shutdown = shutdown or k in {norm(x) for x in vnet.down_interfaces.get(device_id, set())}
            linked = k in {norm(x) for x in vnet.interface_links.get(device_id, {}).values()}
            last_change = dp_engine.last_change_of(device_id, name)
    except Exception:  # noqa: BLE001  (エンジンが無い単体利用でも値は返す)
        pass

    def n(key):
        return str(int(counters.get(key, 0)))
    oper_up = (not shutdown) and (linked or bool(info.get('ip')))
    out = {
        'name': name,
        'type': 'iana-if-type:ethernetCsmacd',
        'enabled': not shutdown,
        'admin-status': 'DOWN' if shutdown else 'UP',
        'oper-status': 'UP' if oper_up else 'DOWN',
        'counters': {
            'in-octets': n('in_bytes'), 'out-octets': n('out_bytes'),
            'in-pkts': n('in_pkts'), 'out-pkts': n('out_pkts'),
            'in-unicast-pkts': n('in_ucast'), 'out-unicast-pkts': n('out_ucast'),
            'in-multicast-pkts': n('in_mcast'), 'out-multicast-pkts': n('out_mcast'),
            'in-broadcast-pkts': n('in_bcast'), 'out-broadcast-pkts': n('out_bcast'),
            'in-errors': n('in_errors'), 'out-errors': n('out_errors'),
            'in-discards': n('in_discards'), 'out-discards': n('out_discards'),
            'in-fcs-errors': n('in_crc'), 'in-unknown-protos': n('in_unknown_protos'),
        },
    }
    if last_change:
        out['last-change'] = str(int(last_change * 1e9))     # OpenConfig は UNIX 時刻(ナノ秒)
    return out


def _descend(val, names):
    for n in names:
        if not isinstance(val, dict):
            return None
        val = val.get(n)
        if val is None:
            return None
    return val


def get_value(state, elems, device_id=None):
    """パス要素に対応する値を返す。未対応なら None。

    device_id を渡すと OpenConfig の state(oper-status・counters)にその装置の
    カウンタが入る(gNMI サーバは自分の装置 ID を渡す)。"""
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

    if head in _IF_HEADS:
        ifaces = getattr(state, 'interfaces', {}) or {}
        # OpenConfig で取りに来たとき(モジュール名付き、または state 以下のパス)は state を付ける
        with_state = head.startswith('openconfig') or any(_bare(n) == 'state' for n, _ in rest[1:2])

        def entry(n, i):
            e = _iface_json(n, i)
            if with_state:
                e['state'] = _oc_state(device_id, n, i)
            return e
        if not rest:
            return {'interface': [entry(n, i) for n, i in ifaces.items()]}
        name0, keys0 = rest[0]
        if _bare(name0) != 'interface':
            return None
        key = keys0.get('name', '')
        if not key or key == '*':
            if len(rest) == 1:
                return {'interface': [entry(n, i) for n, i in ifaces.items()]}
            return None           # 下の階層まで指定したワイルドカードは expand_paths で IF ごとに分ける
        info = ifaces.get(key)
        if info is None:
            return None
        return _descend(entry(key, info), [_bare(n) for n, _ in rest[1:]])
    return None


def expand_paths(state, elems, device_id=None):
    """[(具体的なパス要素, 値), ...]。

    /interfaces/interface/state/counters のように IF 名を指定せず下の階層まで取りに
    来た場合は、実機と同じく IF ごとに interface[name=...] を付けた別々の値に分ける
    (gnmic の subscribe で 1 秒ごとの IF カウンタを流す定番の使い方)。"""
    if (len(elems) >= 3 and elems[0][0] in _IF_HEADS and _bare(elems[1][0]) == 'interface'
            and elems[1][1].get('name', '*') == '*'):
        out = []
        for n in (getattr(state, 'interfaces', {}) or {}):
            e2 = [elems[0], (elems[1][0], {'name': n})] + list(elems[2:])
            v = get_value(state, e2, device_id)
            if v is not None:
                out.append((e2, v))
        return out
    v = get_value(state, elems, device_id)
    return [] if v is None else [(list(elems), v)]


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
                found = expand_paths(self.state, path_elems(path), self.device_id)
                if not found:
                    context.abort(grpc.StatusCode.NOT_FOUND,
                                  f'path not found: {ps}')
                notif = resp.notification.add()
                notif.timestamp = now
                notif.prefix.CopyFrom(request.prefix)
                for elems, val in found:
                    upd = notif.update.add()
                    upd.path.CopyFrom(_path_from(path, elems))
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
                    # STREAM: サブスクリプションごとのモードで扱う。
                    # SAMPLE は sample_interval ごとに送る(suppress_redundant なら
                    # 変化時のみ)。ON_CHANGE は値が変わった時だけ送り、
                    # heartbeat_interval があればその周期でも送る。
                    # TARGET_DEFINED はこの実装では ON_CHANGE として扱う。
                    # (以前は全部を SAMPLE 扱いにしていたため、on-change 購読が
                    # 既定10秒周期のポーリングになり変化が即時に届かなかった)
                    for m in self._updates(sub_list):
                        yield m
                    yield self._sync()
                    SM = gnmi_pb2.SubscriptionMode
                    now = time.time()
                    subs = []
                    for s in sub_list.subscription:
                        on_change = s.mode in (SM.ON_CHANGE, SM.TARGET_DEFINED)
                        if on_change:
                            period = s.heartbeat_interval / 1e9 if s.heartbeat_interval else None
                        else:
                            period = max((s.sample_interval or 10_000_000_000) / 1e9, 0.1)
                        subs.append({'sub': s, 'on_change': on_change, 'period': period,
                                     'next': now + period if period else None,
                                     'suppress': (not on_change) and s.suppress_redundant,
                                     'last': self._snapshot(s)})
                    tick = 0.2
                    while context.is_active():
                        time.sleep(tick)
                        if not context.is_active():
                            return
                        now = time.time()
                        for e in subs:
                            cur = self._snapshot(e['sub'])
                            changed = cur != e['last']
                            due = e['next'] is not None and now >= e['next']
                            send = (changed if e['on_change'] else due and (changed or not e['suppress'])) \
                                or (e['on_change'] and due)
                            if due:
                                e['next'] = now + e['period']
                            if send:
                                e['last'] = cur
                                for m in self._updates_for([e['sub']]):
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
            return self._updates_for(sub_list.subscription)

        def _snapshot(self, sub):
            return json.dumps(expand_paths(self.state, path_elems(sub.path), self.device_id))

        def _updates_for(self, subscriptions):
            now = int(time.time() * 1e9)
            for sub in subscriptions:
                found = expand_paths(self.state, path_elems(sub.path), self.device_id)
                if not found:
                    continue
                resp = gnmi_pb2.SubscribeResponse()
                resp.update.timestamp = now
                for elems, val in found:
                    u = resp.update.update.add()
                    u.path.CopyFrom(_path_from(sub.path, elems))
                    u.val.json_ietf_val = json.dumps(val).encode()
                yield resp

        @staticmethod
        def _sync():
            resp = gnmi_pb2.SubscribeResponse()
            resp.sync_response = True
            return resp

    def _path_from(orig, elems):
        """元のパス(origin・target を保つ)の要素を、IF 名を埋めた elems に置き換えたもの"""
        p = gnmi_pb2.Path()
        p.CopyFrom(orig)
        del p.elem[:]
        for name, keys in elems:
            pe = p.elem.add()
            pe.name = name
            for k, v in keys.items():
                pe.key[k] = v
        return p

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
        # 装置IPはホストに存在しないことがあるので、bind する前に
        # ループバックへ足す（SNMP/NETCONFと同じ回避）
        ensure_loopback_alias(ip)
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


def active_gnmi_device_ids() -> list:
    """実gNMIリスナーが起動中の device_id 一覧（ダッシュボード表示用）"""
    return list(_servers.keys())


def gnmi_port_for(device_id: str):
    """起動中なら実際にbindしているポート番号、未起動ならNone"""
    srv = _servers.get(device_id)
    return srv.port if srv else None


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
