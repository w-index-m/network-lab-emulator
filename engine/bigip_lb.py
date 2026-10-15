"""F5 BIG-IP LTM: 実TCPヘルスチェック + 実トラフィック分散(TCPパススルー)。

ユーザー依頼「実際のヘルスチェック/振り分け動作を追加(推奨)」への
対応——「ここにモックロードバランサーって存在しますか？」
→ 既存の`bigip`(tmsh系)モックに、設定を保持するだけでなく実際に
動く部分を足した。

これまでの `_bigip_health_check`(app.py)は「そのIPを持つ装置の
インタフェースがshutdownされていないか」というトポロジーベースの
疎通判定だけだった(他のengineと同じ「実状態遷移、ただしL2/パケット
そのものではない」パターンで、これ自体は嘘ではない)。ここでは
さらに2つを実際に動くようにする:

1. **実TCPヘルスチェック**: monitorの種別がtcp/http/https系なら、
   メンバーの実IP:portへ本物の`socket.create_connection`を試み、
   その結果で状態を決める(トポロジーベースの疎通判定に加えて、
   本当にそのポートがlistenしているかも見る——例えば実SSHリスナー
   が有効化されていないバックエンドは、インタフェースがupでも
   down判定になる)。monitorがgateway_icmp(実機の既定)なら従来通り
   トポロジーベースのまま(ICMP到達性はこのエミュレータのicmp_engine
   が既に担っている領域で、ここで重複実装しない)。

2. **実トラフィック分散**: 仮想サーバ(`create ltm virtual ... pool
   <name>`)のdestinationに実TCPリスナー(`VirtualServerProxy`)を
   立て、設定されたload-balancing-mode(round-robin/least-
   connections)で実際にヘルシーな(statusがupの)プールメンバーへ
   TCP接続を選択・中継する(バイト列をそのまま双方向リレーする本物の
   TCPプロキシ——シミュレーションではない)。これにより、例えば実
   SSHクライアントでVIPに繋ぐと、実際にそのときヘルシーなバック
   エンドのSSHバナーが返ってくる・バックエンドを落とすと実際に
   別のバックエンドへ振り分けが切り替わる、という挙動を素のソケット
   で確認できる。

スコープ外(実装していない):
  - httpモニターの実際のHTTPリクエスト送信/レスポンス検証(この
    エミュレータの装置は実HTTPサーバを持たないため、tcp相当の
    接続性チェックに留める——http/httpsモニターは実質tcp同等として
    扱う、と明記)
  - SSL/TLSプロファイル、iRules、persistence(セッション永続化)
  - UDPベースのロードバランシング(TCPパススルーのみ)
"""

import re
import socket
import threading


def member_tcp_reachable(ip: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def member_reachable_topology(ip: str) -> bool:
    """gateway_icmp(実機の既定モニター)相当: そのIPを持つ装置が存在し、
    当該インタフェースがdown(shutdown)でなければ到達可能とみなす
    (icmp_engineの既存のL3到達性判定を再利用、重複実装しない)。"""
    from engine.protocols import icmp_engine, vnet
    dev = icmp_engine._find_device_owning_ip(ip)
    if not dev:
        return False
    info = icmp_engine.device_ips.get(dev, {})
    down_ifaces = vnet.down_interfaces.get(dev, set())
    for ifname, iinfo in info.get('interfaces', {}).items():
        if iinfo.get('ip') == ip:
            return ifname not in down_ifaces
    return ip in info.get('ips', {})


def member_is_up(state, mtype: str, ip: str, port: int) -> bool:
    """モニター種別ごとの疎通判定(表示用の_bigip_health_checkと、
    実トラフィック分散の両方から呼ばれる単一の判定ロジック)。"""
    if mtype in ('tcp', 'http', 'https'):
        return member_tcp_reachable(ip, port)
    if mtype == 'gateway_icmp':
        return member_reachable_topology(ip)
    return True


class VirtualServerProxy:
    """1つの仮想サーバ(destination)ぶんの実TCPリスナー。
    新規接続ごとに、その時点でヘルシーなプールメンバーをロード
    バランシングモードに従って選び、本物のTCP中継を行う。"""

    def __init__(self, vip, port, get_members, get_lb_mode):
        self.vip = vip
        self.port = port
        self._get_members = get_members    # () -> [(ip, port), ...] (healthyのみ)
        self._get_lb_mode = get_lb_mode     # () -> 'round-robin' | 'least-connections' | ...
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._rr_index = -1
        self._active_conns = {}            # (ip, port) -> 現在の接続数
        self._lock = threading.Lock()

    def start(self):
        from engine.loopback_alias import ensure_loopback_alias
        ensure_loopback_alias(self.vip)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.vip, self.port))
        self._sock.listen(20)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _select_member(self):
        members = self._get_members()
        if not members:
            return None
        mode = self._get_lb_mode()
        with self._lock:
            if mode == 'least-connections':
                return min(members, key=lambda m: self._active_conns.get(m, 0))
            # round-robin(既定)
            self._rr_index = (self._rr_index + 1) % len(members)
            return members[self._rr_index]

    def _handle(self, client):
        member = self._select_member()
        if member is None:
            try:
                client.close()
            except OSError:
                pass
            return
        with self._lock:
            self._active_conns[member] = self._active_conns.get(member, 0) + 1
        try:
            backend = socket.create_connection(member, timeout=3.0)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            with self._lock:
                self._active_conns[member] -= 1
            return
        t1 = threading.Thread(target=self._pump, args=(client, backend), daemon=True)
        t2 = threading.Thread(target=self._pump, args=(backend, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (client, backend):
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            self._active_conns[member] -= 1

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


_proxies = {}  # (device_id, vs_name) -> (VirtualServerProxy, pool_name)


def _parse_destination(dest: str):
    m = re.match(r'^([\d.]+)[:.](\d+)$', dest or '')
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def ensure_virtual_server_proxy(device_id, vs_name, device_sessions) -> bool:
    """装置のf5_virtuals[vs_name]の設定(destination/pool)に基づいて
    実リスナーを起動する。既に同じ設定で起動済みならno-op、設定が
    変わっていれば張り直す。destination/poolが不足していれば何もしない。"""
    state = device_sessions.get(device_id)
    if state is None:
        return False
    v = getattr(state, 'f5_virtuals', {}).get(vs_name)
    if not v:
        return False
    vip, port = _parse_destination(v.get('destination', ''))
    pool_name = v.get('pool')
    if not vip or not port or not pool_name:
        return False

    key = (device_id, vs_name)
    existing = _proxies.get(key)
    if existing is not None:
        proxy, old_pool = existing
        if proxy.vip == vip and proxy.port == port and old_pool == pool_name:
            return True
        proxy.stop()
        _proxies.pop(key, None)

    def get_members():
        pool = state.f5_pools.get(pool_name)
        if not pool:
            return []
        mtype = monitor_type(state, pool.get('monitor', 'none'))
        out = []
        for m in pool['members']:
            if m.get('admin_down'):
                continue
            if member_is_up(state, mtype, m['node'], m['port']):
                out.append((m['node'], m['port']))
        return out

    def get_lb_mode():
        pool = state.f5_pools.get(pool_name)
        return (pool or {}).get('lb_mode', 'round-robin')

    proxy = VirtualServerProxy(vip, port, get_members, get_lb_mode)
    try:
        proxy.start()
    except OSError as e:
        print(f'[BIG-IP LB] {device_id}/{vs_name} ({vip}:{port}) 起動失敗: {e}')
        return False
    _proxies[key] = (proxy, pool_name)
    print(f'[BIG-IP LB] {device_id}/{vs_name} ({vip}:{port} -> pool {pool_name}) 実リスナーを起動しました')
    return True


def stop_virtual_server_proxy(device_id, vs_name) -> bool:
    key = (device_id, vs_name)
    existing = _proxies.pop(key, None)
    if existing is None:
        return False
    existing[0].stop()
    return True


def monitor_type(state, monitor_ref: str) -> str:
    """pool['monitor']の値(組み込み種別名 or カスタムmonitor名)から
    実際のmonitor種別('tcp'/'http'/'https'/'gateway_icmp'/'none')を
    解決する。"""
    if not monitor_ref or monitor_ref in ('none', '/Common/none'):
        return 'none'
    custom = getattr(state, 'f5_monitors', {}).get(monitor_ref)
    if custom:
        return custom.get('type', 'gateway_icmp')
    base = monitor_ref.lstrip('/').split('/')[-1]
    if base in ('tcp', 'http', 'https'):
        return base
    if base in ('gateway_icmp', 'icmp'):
        return 'gateway_icmp'
    return 'gateway_icmp'
