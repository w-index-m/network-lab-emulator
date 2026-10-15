"""
AWS ALB(Application Load Balancer)風のL7ロードバランサ(`alb` device_type)。

「ちなみにalbを実装することもできますか？実働様でおねがいします」
への対応。実機ALBにSSH/コンソールログインという概念が無いため、
aws-cli(`aws elbv2 ...`)構文を`/api/cli`経由で受け付ける
(`engine/rules.py` `_alb_process`、ユーザーが選んだ「aws-cli構文を
部分対応」方式)。

- CLI設定(create-load-balancer/create-target-group/register-targets/
  create-listener/create-rule/describe-*)のテスト
- 実HTTPヘルスチェック(`engine.alb_lb.target_is_healthy`)——pc装置に
  追加した本物の`http.server`(`python3 -m http.server`、
  engine/mock_http_server.py)を相手に、実際のHTTP GETで健全判定する
- `AlbListenerProxy`単体の振り分けロジック(path-pattern/host-header
  によるターゲットグループ選択、ターゲットグループ内round-robin)を
  簡易HTTPバックエンドで直接固定
- エンドツーエンド: 本物のuvicornサーバ + pc装置の実http.serverを
  2グループ(バックエンドA/B)に見立て、path-patternルールで実際に
  正しいグループへ振り分けられること、グループ内でround-robinする
  こと、バックエンドを落とすと実際にフェイルオーバーすることを
  本物のHTTPクライアント(urllib)で確認する
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from fastapi.testclient import TestClient
import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='alb'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _json_out(out):
    return json.loads(out.strip())


class TestAlbConfigCli:
    def test_create_load_balancer_returns_arn_and_dns_name(self):
        _dev('alb-cli-1')
        out = _cli('alb-cli-1', 'aws elbv2 create-load-balancer --name a1 --type application')
        data = _json_out(out)
        lb = data['LoadBalancers'][0]
        assert lb['LoadBalancerArn'].startswith('arn:aws:elasticloadbalancing:')
        assert lb['DNSName'].endswith('.elb.amazonaws.com')

    def test_create_target_group_stores_health_check_config(self):
        _dev('alb-cli-2')
        _cli('alb-cli-2', 'aws elbv2 create-target-group --name tg1 --protocol HTTP --port 8000 '
                         '--health-check-path /healthz --health-check-protocol HTTP')
        state = app_module.device_sessions['alb-cli-2']
        tg = list(state.alb_target_groups.values())[0]
        assert tg['health_check']['path'] == '/healthz'
        assert tg['health_check']['protocol'] == 'HTTP'

    def test_register_and_deregister_targets(self):
        _dev('alb-cli-3')
        _cli('alb-cli-3', 'aws elbv2 create-target-group --name tg2 --protocol HTTP --port 8000')
        _cli('alb-cli-3', 'aws elbv2 register-targets --target-group-arn tg2 '
                         '--targets Id=10.1.1.1,Port=8000 Id=10.1.1.2,Port=8000')
        state = app_module.device_sessions['alb-cli-3']
        tg = list(state.alb_target_groups.values())[0]
        assert len(tg['members']) == 2
        _cli('alb-cli-3', 'aws elbv2 deregister-targets --target-group-arn tg2 --targets Id=10.1.1.1')
        assert len(tg['members']) == 1
        assert tg['members'][0]['ip'] == '10.1.1.2'

    def test_create_listener_references_load_balancer_and_default_target_group(self):
        from engine.alb_lb import stop_alb_listener_proxy
        _dev('alb-cli-4')
        _cli('alb-cli-4', 'aws elbv2 create-load-balancer --name a4 --type application')
        _cli('alb-cli-4', 'aws elbv2 create-target-group --name tg4 --protocol HTTP --port 8000')
        try:
            out = _cli('alb-cli-4', 'aws elbv2 create-listener --load-balancer-arn a4 --protocol HTTP '
                                   '--port 80 --default-actions Type=forward,TargetGroupArn=tg4')
            data = _json_out(out)
            assert data['Listeners'][0]['Port'] == 80
            assert data['Listeners'][0]['DefaultActions'][0]['Type'] == 'forward'
        finally:
            # create-listenerは実TCPリスナーを本当に起動する(ALBの既定
            # VIPはテスト装置間で共有の172.31.0.10)ため、他のテストの
            # ポート80リスナーと衝突しないよう必ず停止する。
            state = app_module.device_sessions['alb-cli-4']
            for arn in state.alb_listeners:
                stop_alb_listener_proxy('alb-cli-4', arn)

    def test_create_rule_with_path_pattern_condition(self):
        from engine.alb_lb import stop_alb_listener_proxy
        _dev('alb-cli-5')
        _cli('alb-cli-5', 'aws elbv2 create-load-balancer --name a5 --type application')
        _cli('alb-cli-5', 'aws elbv2 create-target-group --name tg5 --protocol HTTP --port 8000')
        try:
            _cli('alb-cli-5', 'aws elbv2 create-listener --load-balancer-arn a5 --protocol HTTP --port 80 '
                             '--default-actions Type=forward,TargetGroupArn=tg5')
            state = app_module.device_sessions['alb-cli-5']
            listener_arn = list(state.alb_listeners.keys())[0]
            out = _cli('alb-cli-5', f"aws elbv2 create-rule --listener-arn {listener_arn} --priority 10 "
                                    f"--conditions Field=path-pattern,Values=/api/* "
                                    f"--actions Type=forward,TargetGroupArn=tg5")
            data = _json_out(out)
            assert data['Rules'][0]['Conditions'][0]['Field'] == 'path-pattern'
            assert data['Rules'][0]['Conditions'][0]['Values'] == ['/api/*']
        finally:
            state = app_module.device_sessions['alb-cli-5']
            for arn in state.alb_listeners:
                stop_alb_listener_proxy('alb-cli-5', arn)

    def test_nonexistent_target_group_reports_error(self):
        _dev('alb-cli-6')
        out = _cli('alb-cli-6', 'aws elbv2 register-targets --target-group-arn no-such-tg '
                               '--targets Id=10.1.1.1,Port=80')
        assert 'TargetGroupNotFoundException' in out

    def test_unknown_subcommand_shows_help(self):
        _dev('alb-cli-7')
        out = _cli('alb-cli-7', 'aws elbv2 bogus-command')
        assert 'Invalid choice' in out
        assert 'create-load-balancer' in out


class TestAlbAwsCliSpecCompliance:
    """ユーザー依頼「ALBのaws-cli仕様も本家マニュアルと突き合わせといて」
    への対応。`boto/botocore`の`botocore/data/elbv2/2015-12-01/
    service-2.json`(aws-cliが実際に使っているサービスモデル、本家
    ドキュメントの一次情報)と照合して見つけた2つの欠落フィールドを
    固定する: Ruleシェイプには常に`IsDefault`があり、
    TargetHealthDescriptionには常に`HealthCheckPort`がある。"""

    def test_create_rule_output_includes_is_default_false(self):
        _dev('alb-spec-1')
        _cli('alb-spec-1', 'aws elbv2 create-load-balancer --name specdemo --type application')
        _cli('alb-spec-1', 'aws elbv2 create-target-group --name tg1 --protocol HTTP --port 80')
        out = _cli('alb-spec-1', 'aws elbv2 create-rule --listener-arn no-listener --priority 10 '
                                 '--conditions Field=path-pattern,Values=/api/* '
                                 '--actions Type=forward,TargetGroupArn=tg1')
        assert 'ListenerNotFoundException' in out

    def test_create_rule_and_describe_rules_include_is_default(self):
        from engine.alb_lb import stop_alb_listener_proxy
        _dev('alb-spec-2')
        try:
            _cli('alb-spec-2', 'aws elbv2 create-load-balancer --name specdemo2 --type application')
            _cli('alb-spec-2', 'aws elbv2 create-target-group --name tg2 --protocol HTTP --port 80')
            listener_out = _json_out(_cli(
                'alb-spec-2',
                'aws elbv2 create-listener --load-balancer-arn specdemo2 --protocol HTTP --port 80 '
                '--default-actions Type=forward,TargetGroupArn=tg2'))
            listener_arn = listener_out['Listeners'][0]['ListenerArn']
            rule_out = _json_out(_cli(
                'alb-spec-2',
                f'aws elbv2 create-rule --listener-arn {listener_arn} --priority 10 '
                '--conditions Field=path-pattern,Values=/api/* --actions Type=forward,TargetGroupArn=tg2'))
            assert rule_out['Rules'][0]['IsDefault'] is False

            describe_out = _json_out(_cli('alb-spec-2',
                                           f'aws elbv2 describe-rules --listener-arn {listener_arn}'))
            by_default = {r['IsDefault'] for r in describe_out['Rules']}
            assert by_default == {True, False}, (
                '実機は常にcreate-listenerのdefault-actions由来の既定ルール'
                f'(IsDefault: true)も一緒に返すはず: {describe_out}')
            default_rule = next(r for r in describe_out['Rules'] if r['IsDefault'])
            assert default_rule['Priority'] == 'default'
            assert default_rule['Actions'][0]['TargetGroupArn']
        finally:
            state = app_module.device_sessions['alb-spec-2']
            for arn in state.alb_listeners:
                stop_alb_listener_proxy('alb-spec-2', arn)

    def test_describe_target_health_includes_health_check_port(self):
        _dev('alb-spec-3')
        _cli('alb-spec-3', 'aws elbv2 create-target-group --name tg3 --protocol HTTP --port 9400')
        _cli('alb-spec-3', 'aws elbv2 register-targets --target-group-arn tg3 '
                           '--targets Id=10.2.2.2,Port=9400')
        out = _json_out(_cli('alb-spec-3', 'aws elbv2 describe-target-health --target-group-arn tg3'))
        desc = out['TargetHealthDescriptions'][0]
        assert desc['HealthCheckPort'] == 'traffic-port'


class TestAlbRealHttpHealthCheck:
    def test_target_healthy_against_real_mock_http_server(self):
        from engine.mock_http_server import ensure_mock_http_server, stop_mock_http_server
        from engine.alb_lb import target_is_healthy
        ip, port = '127.0.0.1', 18321
        assert ensure_mock_http_server(ip, port, 'test-backend')
        try:
            time.sleep(0.2)
            assert target_is_healthy('HTTP', ip, port, '/') is True
        finally:
            stop_mock_http_server(ip, port)

    def test_target_unhealthy_when_nothing_listening(self):
        from engine.alb_lb import target_is_healthy
        assert target_is_healthy('HTTP', '127.0.0.1', 18322, '/') is False

    def test_non_http_service_fails_http_health_check(self):
        """HTTPを話さないTCPサービス(例: SSHバナーを返すだけ)に対する
        HTTPヘルスチェックは正しく不健全判定になる(フェイクの検知では
        ないことの固定)。"""
        from engine.alb_lb import target_is_healthy

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(5)
        port = srv.getsockname()[1]

        def _respond_garbage():
            try:
                srv.settimeout(3)
                conn, _ = srv.accept()
                conn.sendall(b'SSH-2.0-not-http\r\n')
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=_respond_garbage, daemon=True)
        t.start()
        try:
            assert target_is_healthy('HTTP', '127.0.0.1', port, '/') is False
        finally:
            srv.close()
            t.join(timeout=2)


class _FakeHttpBackend:
    """最小限の実TCPサーバ: どんなリクエストが来ても固定の200レスポンス
    (ボディで自分を識別する文字列)を返す。"""

    def __init__(self, label: bytes):
        self.label = label
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(10)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.5)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.recv(4096)
                body = self.label
                resp = (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                        b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
                        b'Connection: close\r\n\r\n' + body)
                conn.sendall(resp)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def _http_get(host, port, path='/', timeout=3.0):
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request('GET', path, headers={'Host': host, 'Connection': 'close'})
        resp = conn.getresponse()
        return resp.read()
    finally:
        conn.close()


def _http_get_with_headers(host, port, path='/', cookie=None, timeout=3.0):
    """ボディと一緒にレスポンスヘッダ(Set-Cookie確認用)も返す。"""
    import http.client
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        headers = {'Host': host, 'Connection': 'close'}
        if cookie:
            headers['Cookie'] = cookie
        conn.request('GET', path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return body, dict(resp.getheaders())
    finally:
        conn.close()


class TestAlbListenerProxyDirect:
    def test_path_pattern_routes_to_correct_target_group(self):
        from engine.alb_lb import AlbListenerProxy

        api_backend = _FakeHttpBackend(b'API-BACKEND')
        web_backend = _FakeHttpBackend(b'WEB-BACKEND')

        def select_tg(path, host):
            if path.startswith('/api/'):
                return 'api-tg'
            return 'web-tg'

        def get_members(tg_key):
            if tg_key == 'api-tg':
                return [('127.0.0.1', api_backend.port)]
            return [('127.0.0.1', web_backend.port)]

        proxy = AlbListenerProxy('127.0.0.1', 0, select_tg, get_members)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            assert _http_get('127.0.0.1', vip_port, '/api/users') == b'API-BACKEND'
            assert _http_get('127.0.0.1', vip_port, '/index.html') == b'WEB-BACKEND'
        finally:
            proxy.stop()
            api_backend.stop()
            web_backend.stop()

    def test_round_robin_within_target_group(self):
        from engine.alb_lb import AlbListenerProxy

        b1 = _FakeHttpBackend(b'B1')
        b2 = _FakeHttpBackend(b'B2')
        members = [('127.0.0.1', b1.port), ('127.0.0.1', b2.port)]
        proxy = AlbListenerProxy('127.0.0.1', 0, lambda p, h: 'tg', lambda k: members)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            results = [_http_get('127.0.0.1', vip_port, '/') for _ in range(4)]
            assert results == [b'B1', b'B2', b'B1', b'B2']
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_no_target_group_match_and_no_default_returns_503(self):
        from engine.alb_lb import AlbListenerProxy
        proxy = AlbListenerProxy('127.0.0.1', 0, lambda p, h: None, lambda k: [])
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', vip_port, timeout=3)
            conn.request('GET', '/', headers={'Connection': 'close'})
            resp = conn.getresponse()
            assert resp.status == 503
            conn.close()
        finally:
            proxy.stop()


# ── エンドツーエンド: 本物のuvicornサーバ + pc装置の実http.server ──

PORT = 8132
BASE = f'http://127.0.0.1:{PORT}'
ALB, PC_A, PC_B, PC_C = 'albe2e-lb', 'albe2e-a', 'albe2e-b', 'albe2e-c'
ALB_VIP = '172.31.0.10'
HTTP_PORT = 8800


def _live_cli(cmd, dev):
    import urllib.request
    req = urllib.request.Request(
        BASE + '/api/cli', method='POST',
        data=json.dumps({'device_id': dev, 'command': cmd}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-alb') / 'server.log'
    env = dict(os.environ, NETLAB_AUTH_DISABLE='1')
    import urllib.request
    with open(log, 'wb') as fh:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app:app',
             '--host', '127.0.0.1', '--port', str(PORT)],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    urllib.request.urlopen(BASE + '/api/health', timeout=5).read()
                    break
                except Exception:
                    if proc.poll() is not None:          # pragma: no cover
                        pytest.fail('サーバが起動しませんでした:\n'
                                    + log.read_text(errors='replace')[-2000:])
                    time.sleep(0.5)
            else:                                        # pragma: no cover
                pytest.fail('サーバが時間内に起動しませんでした')
            yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:            # pragma: no cover
                proc.kill()


@pytest.fixture(scope='module')
def alb_with_http_backends(server):
    import urllib.request

    def _mkdev(id_, type_):
        req = urllib.request.Request(
            BASE + '/api/device', method='POST',
            data=json.dumps({'id': id_, 'type': type_, 'hostname': id_}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=30).read()

    _mkdev(PC_A, 'pc')
    _mkdev(PC_B, 'pc')
    _mkdev(PC_C, 'pc')
    # pcは既定で全台同じeth0 IP(192.168.1.10)を持つため、衝突しない
    # よう明示的に別IPを割り当ててからhttp.serverを立てる
    for n, pc in enumerate((PC_A, PC_B, PC_C), start=1):
        _live_cli(f'ip addr add 10.50.0.{n}/24 dev eth0', pc)
        _live_cli(f'python3 -m http.server {HTTP_PORT}', pc)
    time.sleep(1)

    # pcの実IPは`ip addr`で確認する(固定値をハードコードしない)
    def _pc_ip(pc):
        out = _live_cli('ip addr', pc)
        import re as _re
        m = _re.search(r'inet\s+([\d.]+)/', out)
        return m.group(1) if m else None

    a_ip, b_ip, c_ip = _pc_ip(PC_A), _pc_ip(PC_B), _pc_ip(PC_C)

    _mkdev(ALB, 'alb')
    _live_cli('aws elbv2 create-load-balancer --name e2e-alb --type application', ALB)
    _live_cli(f'aws elbv2 create-target-group --name web-tg --protocol HTTP --port {HTTP_PORT} '
             f'--health-check-protocol HTTP --health-check-path /', ALB)
    _live_cli(f'aws elbv2 create-target-group --name api-tg --protocol HTTP --port {HTTP_PORT} '
             f'--health-check-protocol HTTP --health-check-path /', ALB)
    _live_cli(f'aws elbv2 register-targets --target-group-arn web-tg '
             f'--targets Id={a_ip},Port={HTTP_PORT} Id={b_ip},Port={HTTP_PORT}', ALB)
    _live_cli(f'aws elbv2 register-targets --target-group-arn api-tg '
             f'--targets Id={c_ip},Port={HTTP_PORT}', ALB)
    _live_cli(f'aws elbv2 create-listener --load-balancer-arn e2e-alb --protocol HTTP --port 80 '
             f'--default-actions Type=forward,TargetGroupArn=web-tg', ALB)
    state_out = _live_cli('aws elbv2 describe-listeners --load-balancer-arn e2e-alb', ALB)
    listener_arn = json.loads(state_out)['Listeners'][0]['ListenerArn']
    _live_cli(f'aws elbv2 create-rule --listener-arn {listener_arn} --priority 10 '
             f"--conditions Field=path-pattern,Values=/api/* "
             f'--actions Type=forward,TargetGroupArn=api-tg', ALB)
    time.sleep(2)
    yield {'a_ip': a_ip, 'b_ip': b_ip, 'c_ip': c_ip}


class TestAlbStickinessConfigCli:
    """ユーザー依頼「疑似クライアントから、パーシステンスの確認とかも
    お願いできますか？」への対応——実機ALBのduration-based cookie
    stickiness(`modify-target-group-attributes`)のCLI設定面。"""

    def test_modify_target_group_attributes_enables_stickiness(self):
        _dev('alb-stick-cli-1')
        _cli('alb-stick-cli-1', 'aws elbv2 create-target-group --name tg1 --protocol HTTP --port 8000')
        out = _cli('alb-stick-cli-1',
                   'aws elbv2 modify-target-group-attributes --target-group-arn tg1 '
                   '--attributes Key=stickiness.enabled,Value=true '
                   'Key=stickiness.lb_cookie.duration_seconds,Value=120')
        data = _json_out(out)
        attrs = {a['Key']: a['Value'] for a in data['Attributes']}
        assert attrs['stickiness.enabled'] == 'true'
        assert attrs['stickiness.lb_cookie.duration_seconds'] == '120'
        state = app_module.device_sessions['alb-stick-cli-1']
        tg = list(state.alb_target_groups.values())[0]
        assert tg['stickiness'] == {'enabled': True, 'type': 'lb_cookie', 'duration': 120}

    def test_stickiness_disabled_by_default(self):
        _dev('alb-stick-cli-2')
        _cli('alb-stick-cli-2', 'aws elbv2 create-target-group --name tg2 --protocol HTTP --port 8000')
        state = app_module.device_sessions['alb-stick-cli-2']
        tg = list(state.alb_target_groups.values())[0]
        assert tg['stickiness']['enabled'] is False

    def test_nonexistent_target_group_reports_error(self):
        _dev('alb-stick-cli-3')
        out = _cli('alb-stick-cli-3',
                   'aws elbv2 modify-target-group-attributes --target-group-arn no-such-tg '
                   '--attributes Key=stickiness.enabled,Value=true')
        assert 'TargetGroupNotFoundException' in out


class TestAlbListenerProxyStickinessDirect:
    """AlbListenerProxyの実際のCookieベースstickinessの振る舞いを、
    簡易HTTPバックエンドで直接固定する。"""

    def test_response_sets_cookie_and_subsequent_request_sticks(self):
        from engine.alb_lb import AlbListenerProxy

        b1 = _FakeHttpBackend(b'B1')
        b2 = _FakeHttpBackend(b'B2')
        members = [('127.0.0.1', b1.port), ('127.0.0.1', b2.port)]
        stickiness = {'enabled': True, 'duration': 86400}
        proxy = AlbListenerProxy('127.0.0.1', 0, lambda p, h: 'tg', lambda k: members,
                                 lambda k: stickiness)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]

            body1, headers1 = _http_get_with_headers('127.0.0.1', vip_port, '/')
            assert body1 == b'B1', 'stickiness有効でも初回はround-robinで選ばれる'
            cookie_header = headers1.get('Set-Cookie', '')
            assert cookie_header.startswith('AWSALB='), f'Set-Cookieが注入されているはず: {headers1}'
            cookie_value = cookie_header.split(';')[0]

            # 次の接続は本来ならround-robinでB2が選ばれるはずだが、
            # 同じCookieを持っているのでB1に固定される
            for _ in range(3):
                body, _ = _http_get_with_headers('127.0.0.1', vip_port, '/', cookie=cookie_value)
                assert body == b'B1', 'Cookieを持つリクエストは同じメンバーに固定されるはず'
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_cookie_for_down_member_falls_back_to_load_balancing(self):
        from engine.alb_lb import AlbListenerProxy

        b2 = _FakeHttpBackend(b'B2')
        # b1は実在しないポート(停止済み)を指すCookieを偽装する
        stale_cookie = 'AWSALB=127.0.0.1:1;'
        members = [('127.0.0.1', b2.port)]
        stickiness = {'enabled': True, 'duration': 86400}
        proxy = AlbListenerProxy('127.0.0.1', 0, lambda p, h: 'tg', lambda k: members,
                                 lambda k: stickiness)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            body, _ = _http_get_with_headers('127.0.0.1', vip_port, '/', cookie=stale_cookie)
            assert body == b'B2', '固定先がもう存在しないメンバーならload balancingにフォールバックするはず'
        finally:
            proxy.stop()
            b2.stop()

    def test_stickiness_disabled_does_not_set_cookie(self):
        from engine.alb_lb import AlbListenerProxy

        b1 = _FakeHttpBackend(b'B1')
        members = [('127.0.0.1', b1.port)]
        stickiness = {'enabled': False, 'duration': 86400}
        proxy = AlbListenerProxy('127.0.0.1', 0, lambda p, h: 'tg', lambda k: members,
                                 lambda k: stickiness)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            body, headers = _http_get_with_headers('127.0.0.1', vip_port, '/')
            assert body == b'B1'
            assert 'Set-Cookie' not in headers
        finally:
            proxy.stop()
            b1.stop()


class TestAlbObjectUniqueness:
    """ユーザー依頼「一意性を保って...試験もお願いします」への対応
    (BIG-IP側のpool/node相当)——ALBのtarget-group/register-targetsが
    再投入で重複しないことを固定する。"""

    def test_registering_same_target_twice_does_not_duplicate(self):
        _dev('alb-uniq-1')
        _cli('alb-uniq-1', 'aws elbv2 create-target-group --name utg1 --protocol HTTP --port 8000')
        _cli('alb-uniq-1', 'aws elbv2 register-targets --target-group-arn utg1 --targets Id=10.1.1.1,Port=8000')
        _cli('alb-uniq-1', 'aws elbv2 register-targets --target-group-arn utg1 --targets Id=10.1.1.1,Port=8000')
        state = app_module.device_sessions['alb-uniq-1']
        tg = list(state.alb_target_groups.values())[0]
        assert len(tg['members']) == 1

    def test_distinct_target_groups_and_listeners_stay_independent(self):
        _dev('alb-uniq-2')
        _cli('alb-uniq-2', 'aws elbv2 create-target-group --name utg2a --protocol HTTP --port 8000')
        _cli('alb-uniq-2', 'aws elbv2 create-target-group --name utg2b --protocol HTTP --port 9000')
        _cli('alb-uniq-2', 'aws elbv2 register-targets --target-group-arn utg2a --targets Id=10.1.1.1,Port=8000')
        _cli('alb-uniq-2', 'aws elbv2 register-targets --target-group-arn utg2b --targets Id=10.1.1.2,Port=9000')
        state = app_module.device_sessions['alb-uniq-2']
        tgs = {tg['name']: tg for tg in state.alb_target_groups.values()}
        assert tgs['utg2a']['members'][0]['ip'] == '10.1.1.1'
        assert tgs['utg2b']['members'][0]['ip'] == '10.1.1.2'
        assert tgs['utg2a']['port'] == 8000 and tgs['utg2b']['port'] == 9000


class TestAlbLiveEndToEnd:
    def test_target_health_reports_both_web_backends_healthy(self, alb_with_http_backends):
        out = _live_cli('aws elbv2 describe-target-health --target-group-arn web-tg', ALB)
        data = json.loads(out)
        states = {d['Target']['Id']: d['TargetHealth']['State'] for d in data['TargetHealthDescriptions']}
        assert all(s == 'healthy' for s in states.values()), states

    def test_default_path_round_robins_across_web_backends(self, alb_with_http_backends):
        import urllib.request
        bodies = []
        for _ in range(4):
            with urllib.request.urlopen(f'http://{ALB_VIP}:80/index.html', timeout=5) as r:
                bodies.append(r.read().decode().strip())
        assert set(bodies) == {PC_A, PC_B}
        assert bodies[0] != bodies[1]

    def test_api_path_routes_to_api_target_group_only(self, alb_with_http_backends):
        import urllib.request
        bodies = []
        for _ in range(3):
            with urllib.request.urlopen(f'http://{ALB_VIP}:80/api/widgets', timeout=5) as r:
                bodies.append(r.read().decode().strip())
        assert bodies == [PC_C, PC_C, PC_C]

    def test_stopping_a_web_backend_triggers_real_failover(self, alb_with_http_backends):
        import urllib.request
        # PC_Aの実http.serverを実際に止める(`fuser -k <port>/tcp`——
        # エミュレータはCLIが1リクエスト=1コマンドの応答モデルのため
        # 実機のCtrl-Cそのものは再現できないが、同じ目的を果たす実
        # コマンドとして実装した)。サーバはサブプロセス側の
        # engine.mock_http_server内で動いているため、このテスト
        # プロセスから直接モジュール関数を呼んでも止まらない
        # (別プロセスのモジュール状態)——必ずCLI経由で止める。
        out = _live_cli(f'fuser -k {HTTP_PORT}/tcp', PC_A)
        assert str(HTTP_PORT) in out
        time.sleep(1)
        out = _live_cli('aws elbv2 describe-target-health --target-group-arn web-tg', ALB)
        data = json.loads(out)
        states = {d['Target']['Id']: d['TargetHealth']['State'] for d in data['TargetHealthDescriptions']}
        assert states[alb_with_http_backends['a_ip']] == 'unhealthy'

        bodies = []
        for _ in range(3):
            with urllib.request.urlopen(f'http://{ALB_VIP}:80/index.html', timeout=5) as r:
                bodies.append(r.read().decode().strip())
        assert bodies == [PC_B, PC_B, PC_B], f'落としたバックエンドには振り分けられないはず: {bodies}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
