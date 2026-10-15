"""AWS ALB(Application Load Balancer)風のL7ロードバランサ: 実ヘルス
チェック + 実HTTPリクエストルーティング + 実トラフィック分散。

「ちなみにalbを実装することもできますか？実働様でおねがいします」
への対応。実機ALBにはSSH/コンソールログインという概念が無く、運用者が
実際に触るインタフェースはaws-cli(`aws elbv2 ...`)そのものなので、
他の装置の「実機CLI」扱いと同じ精神でaws-cli構文を部分再現する
(`engine/rules.py`の`_alb_process`)。この engine/alb_lb.py は
F5 BIG-IP実装(`engine/bigip_lb.py`)と同じ考え方で、実ソケットに
よるヘルスチェックと実トラフィック分散を担当する——ただしALBはL7
(HTTPリクエストのpath/Hostヘッダに基づくリスナールール評価)なので、
BIG-IPのL4 TCPパススルーより一段実装が多い。

1. **実ヘルスチェック**: health-check-protocolがHTTP/HTTPSなら、
   メンバーの実IP:portへ本物のHTTP GETを送り、2xx/3xxなら健全と
   みなす(`member_http_healthy`)。相手がHTTPを話さない(例えば実SSH
   リスナーのバナーをそのまま返すだけ)場合は正しく不健全判定になる
   ——フェイクの検知ではない。TCPプロトコルなら接続性のみを見る
   (`member_tcp_reachable`)。

2. **実トラフィック分散**: リスナーのdestination(ALBのVIP:port)に
   本物のTCPリスナー(`AlbListenerProxy`)を立て、新規接続ごとに
   HTTPリクエストライン+ヘッダーを実際に読み取り(本物のHTTPパース、
   フェイクではない)、リスナールールのpath-pattern/host-header条件
   (優先度順)を評価してターゲットグループを選び、その時点で健全な
   メンバーへround-robinで実TCP接続を中継する(読み取り済みのバイト
   列をそのまま転送してから双方向リレーするので、実際にHTTPを話す
   バックエンド——`engine/mock_http_server.py`でpc装置に立てた本物の
   `http.server`——が相手なら、クライアントは本物のHTTPレスポンスを
   受け取る)。

スコープ外(実装していない):
  - HTTPSのTLS終端(実ALBはここでTLS復号するが、このエミュレータは
    証明書/TLSプロファイルを持たないため、HTTPS指定でも平文TCPとして
    扱う——他の機能のスコープカットと同じ考え方)
  - header-based/query-string-based条件、重み付けターゲットグループ、
    リダイレクト/固定レスポンスアクション(forwardアクションのみ)
  - least_outstanding_requestsアルゴリズム(実ALBの既定はround robin
    であり、これのみ実装)
  - スティッキーセッション(target group stickiness)
"""

import fnmatch
import http.client
import socket
import threading


def member_tcp_reachable(ip: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def member_http_healthy(ip: str, port: int, path: str = '/', timeout: float = 2.0) -> bool:
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        try:
            conn.request('GET', path or '/', headers={'Host': ip, 'Connection': 'close'})
            resp = conn.getresponse()
            resp.read()
            return 200 <= resp.status < 400
        finally:
            conn.close()
    except Exception:
        return False


def target_is_healthy(protocol: str, ip: str, port: int, path: str = '/') -> bool:
    if protocol in ('HTTP', 'HTTPS'):
        return member_http_healthy(ip, port, path)
    return member_tcp_reachable(ip, port)


def rule_matches(conditions, path, host) -> bool:
    """ALBのリスナールール条件(path-pattern/host-header)を評価する。
    実ALBのpath-pattern(`/api/*`等)はグロブに近いためfnmatchで扱う。"""
    if not conditions:
        return False
    for cond in conditions:
        field = cond.get('field')
        values = cond.get('values') or []
        if field == 'path-pattern':
            if not any(fnmatch.fnmatch(path or '/', v) for v in values):
                return False
        elif field == 'host-header':
            if not any(fnmatch.fnmatch((host or '').lower(), v.lower()) for v in values):
                return False
        else:
            return False
    return True


class AlbListenerProxy:
    """1つのALBリスナー(destination)ぶんの実TCPリスナー。新規接続
    ごとに実際にHTTPリクエストライン+ヘッダーを読み、リスナールール
    からターゲットグループを選び、健全なメンバーへround-robinで
    実中継する。"""

    def __init__(self, vip, port, select_target_group, get_healthy_members):
        self.vip = vip
        self.port = port
        self._select_tg = select_target_group   # (path, host) -> tg_key | None
        self._get_members = get_healthy_members  # (tg_key) -> [(ip,port), ...]
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._rr_index = {}  # tg_key -> int
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

    @staticmethod
    def _parse_request(buf: bytes):
        try:
            head = buf.split(b'\r\n\r\n', 1)[0]
            lines = head.split(b'\r\n')
            request_line = lines[0].decode(errors='replace')
            parts = request_line.split(' ')
            path = parts[1] if len(parts) >= 2 else '/'
            path = path.split('?', 1)[0]
            host = ''
            for line in lines[1:]:
                if line.lower().startswith(b'host:'):
                    host = line.split(b':', 1)[1].strip().decode(errors='replace')
                    break
            return path, host
        except Exception:
            return '/', ''

    def _select_member(self, tg_key):
        members = self._get_members(tg_key)
        if not members:
            return None
        with self._lock:
            idx = self._rr_index.get(tg_key, -1)
            idx = (idx + 1) % len(members)
            self._rr_index[tg_key] = idx
            return members[idx]

    def _handle(self, client):
        client.settimeout(5.0)
        buf = b''
        try:
            while b'\r\n\r\n' not in buf and len(buf) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            buf = b''
        if not buf:
            try:
                client.close()
            except OSError:
                pass
            return
        client.settimeout(None)
        path, host = self._parse_request(buf)
        tg_key = self._select_tg(path, host)
        member = self._select_member(tg_key) if tg_key is not None else None
        if member is None:
            try:
                client.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            backend = socket.create_connection(member, timeout=3.0)
        except OSError:
            try:
                client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            backend.sendall(buf)
        except OSError:
            pass
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


_proxies = {}  # (device_id, listener_arn) -> AlbListenerProxy


def ensure_alb_listener_proxy(device_id, listener_arn, device_sessions) -> bool:
    state = device_sessions.get(device_id)
    if state is None:
        return False
    listener = getattr(state, 'alb_listeners', {}).get(listener_arn)
    if not listener:
        return False
    lb = getattr(state, 'alb_load_balancers', {}).get(listener.get('lb_arn'))
    if not lb:
        return False
    vip = lb.get('vip')
    port = listener.get('port')
    if not vip or not port:
        return False

    key = (device_id, listener_arn)
    existing = _proxies.get(key)
    if existing is not None:
        if existing.vip == vip and existing.port == port:
            return True
        existing.stop()
        _proxies.pop(key, None)

    def select_tg(path, host):
        rule_arns = sorted(
            listener.get('rules', []),
            key=lambda a: state.alb_rules.get(a, {}).get('priority', 999999))
        for ra in rule_arns:
            rule = state.alb_rules.get(ra)
            if rule and rule_matches(rule.get('conditions'), path, host):
                return rule.get('tg_arn')
        return listener.get('default_tg_arn')

    def get_healthy_members(tg_arn):
        tg = state.alb_target_groups.get(tg_arn) if tg_arn else None
        if not tg:
            return []
        hc = tg['health_check']
        out = []
        for m in tg['members']:
            if m.get('admin_down'):
                continue
            if target_is_healthy(hc['protocol'], m['ip'], m['port'], hc.get('path', '/')):
                out.append((m['ip'], m['port']))
        return out

    proxy = AlbListenerProxy(vip, port, select_tg, get_healthy_members)
    try:
        proxy.start()
    except OSError as e:
        print(f'[ALB] {device_id}/{listener_arn} ({vip}:{port}) 起動失敗: {e}')
        return False
    _proxies[key] = proxy
    print(f'[ALB] {device_id}/{listener_arn} ({vip}:{port}) 実リスナーを起動しました')
    return True


def stop_alb_listener_proxy(device_id, listener_arn) -> bool:
    key = (device_id, listener_arn)
    proxy = _proxies.pop(key, None)
    if proxy is None:
        return False
    proxy.stop()
    return True
