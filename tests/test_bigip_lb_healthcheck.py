"""
F5 BIG-IP(`bigip`)の実ヘルスチェック/実トラフィック分散。

「ここにモックロードバランサーって存在しますか？」(既存の`bigip`
device_typeで存在が確認された) →「実際のヘルスチェック/振り分け動作を
追加(推奨)」への対応。

これまでの`_bigip_health_check`(app.py)は「メンバーのIPを持つ装置の
インタフェースがshutdownされていないか」というトポロジーベースの
疎通判定のみだった。ここでは:

1. tcp/http/https系monitorなら、メンバーの実IP:portへ本物の
   socket.create_connectionを試みる実ヘルスチェック
   (engine.bigip_lb.member_is_up/member_tcp_reachable)
2. 仮想サーバ(destination+pool)に実TCPリスナー(VirtualServerProxy)
   を立て、ロードバランシングモード(round-robin/least-connections)
   に従って実際にヘルシーなメンバーへTCP接続を中継する実トラフィック
   分散

の両方をテストする。エンドツーエンドのテストは本物のuvicornサーバ+
実SSHリスナー(engine/ssh_cli_agent.py)を2台のバックエンドとして使い、
本物のparamikoクライアントでVIP経由のSSHセッションがround-robinで
振り分けられること、バックエンドのSSHを無効化すると実際にフェイル
オーバーすることを確認する。
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


def _dev(id_, type_='bigip'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


# ── 簡易TCPバックエンド(テスト用): connectされたら固定の1行を送るだけ ──

class _FakeBackend:
    def __init__(self, reply: bytes):
        self.reply = reply
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
                conn.sendall(self.reply)
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


class TestBigipConfigCli:
    def test_create_pool_with_members_and_monitor(self):
        _dev('bp-cli-1')
        out = _cli('bp-cli-1', 'create ltm pool p1 { members add { 10.1.1.1:80 10.1.1.2:80 } '
                               'monitor tcp load-balancing-mode round-robin }')
        assert out == ''
        state = app_module.device_sessions['bp-cli-1']
        pool = state.f5_pools['p1']
        assert len(pool['members']) == 2
        assert pool['monitor'] == 'tcp'
        assert pool['lb_mode'] == 'round-robin'

    def test_create_virtual_references_pool(self):
        _dev('bp-cli-2')
        _cli('bp-cli-2', 'create ltm pool p2 { members add { 10.1.1.1:80 } monitor tcp }')
        _cli('bp-cli-2', 'create ltm virtual v2 { destination 10.9.9.9:8080 pool p2 }')
        state = app_module.device_sessions['bp-cli-2']
        v = state.f5_virtuals['v2']
        assert v['destination'] == '10.9.9.9:8080'
        assert v['pool'] == 'p2'


class TestBigipRealTcpHealthCheck:
    def test_member_up_when_backend_listening(self):
        backend = _FakeBackend(b'hello\n')
        try:
            _dev('bp-hc-1')
            _cli('bp-hc-1', f'create ltm pool hp1 {{ members add {{ 127.0.0.1:{backend.port} }} '
                            f'monitor tcp }}')
            out = _cli('bp-hc-1', 'show ltm pool hp1')
            assert 'up: 1, down: 0' in out
            assert 'available (green)' in out
        finally:
            backend.stop()

    def test_member_down_when_port_closed(self):
        # どのプロセスも listen していないポート番号を使う
        closed_port = 59999
        _dev('bp-hc-2')
        _cli('bp-hc-2', f'create ltm pool hp2 {{ members add {{ 127.0.0.1:{closed_port} }} '
                        f'monitor tcp }}')
        out = _cli('bp-hc-2', 'show ltm pool hp2')
        assert 'up: 0, down: 1' in out
        assert 'offline (red)' in out

    def test_monitor_none_always_reports_up_without_probing(self):
        """monitor未設定(none)なら実際に到達不可でもup扱い(実機同様、
        モニター無しのプールは常に利用可能とみなす)。"""
        closed_port = 59998
        _dev('bp-hc-3')
        _cli('bp-hc-3', f'create ltm pool hp3 {{ members add {{ 127.0.0.1:{closed_port} }} }}')
        out = _cli('bp-hc-3', 'show ltm pool hp3')
        assert 'up: 1, down: 0' in out


class TestVirtualServerProxyDirect:
    """engine.bigip_lb.VirtualServerProxyそのものの振り分けロジック
    (round-robin/least-connections/down除外)を、簡易バックエンドで
    直接固定する。"""

    def test_round_robin_alternates_between_members(self):
        from engine.bigip_lb import VirtualServerProxy
        b1 = _FakeBackend(b'B1\n')
        b2 = _FakeBackend(b'B2\n')
        members = [('127.0.0.1', b1.port), ('127.0.0.1', b2.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin')
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            results = []
            for _ in range(4):
                with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                    s.settimeout(3)
                    results.append(s.recv(16))
            assert results == [b'B1\n', b'B2\n', b'B1\n', b'B2\n']
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_down_member_is_skipped(self):
        from engine.bigip_lb import VirtualServerProxy
        b1 = _FakeBackend(b'B1\n')
        b2 = _FakeBackend(b'B2\n')
        # b1は「down」扱いにしてget_membersから除外する(実際にはプール
        # のmonitor判定がこれをやるが、ここではProxy自体のロジックのみ
        # を見るためget_membersを直接差し替える)
        members = [('127.0.0.1', b2.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin')
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            for _ in range(3):
                with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                    s.settimeout(3)
                    assert s.recv(16) == b'B2\n'
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_least_connections_prefers_idle_member(self):
        from engine.bigip_lb import VirtualServerProxy

        # 遅いバックエンド(接続を長く保持する)と速いバックエンドを用意し、
        # least-connectionsなら「今アクティブな接続が少ない方」が選ばれる
        # ことを確認する。
        class _SlowBackend(_FakeBackend):
            def _loop(self):
                while not self._stop:
                    try:
                        self.sock.settimeout(0.5)
                        conn, _ = self.sock.accept()
                    except (socket.timeout, OSError):
                        continue
                    time.sleep(1.5)
                    try:
                        conn.sendall(self.reply)
                    except OSError:
                        pass
                    finally:
                        try:
                            conn.close()
                        except OSError:
                            pass

        slow = _SlowBackend(b'SLOW\n')
        fast = _FakeBackend(b'FAST\n')
        members = [('127.0.0.1', slow.port), ('127.0.0.1', fast.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'least-connections')
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]

            # slowへの接続を1本張ったまま保持する(アクティブ接続カウント
            # を1にする)
            holder = socket.create_connection(('127.0.0.1', vip_port), timeout=3)
            time.sleep(0.3)  # 接続確立・カウント反映待ち

            with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                s.settimeout(3)
                data = s.recv(16)
            assert data == b'FAST\n', 'アクティブ接続が少ないfastが選ばれるはず'
            holder.close()
        finally:
            proxy.stop()
            slow.stop()
            fast.stop()


# ── エンドツーエンド: 実SSHリスナーを持つ2台のバックエンドへの
#    実round-robin振り分け + 実フェイルオーバー ──

PORT = 8131
BASE = f'http://127.0.0.1:{PORT}'
BE1, BE2, LB = 'lbe2e-be1', 'lbe2e-be2', 'lbe2e-lb1'
BE1_IP, BE2_IP = '192.168.10.1', '203.0.113.2'  # catalyst/ciscoの既定管理IP
VIP_IP, VIP_PORT = '10.251.0.1', 2223


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
    log = tmp_path_factory.mktemp('netlab-lb') / 'server.log'
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
def lb_with_ssh_backends(server):
    import urllib.request

    def _mkdev(id_, type_):
        req = urllib.request.Request(
            BASE + '/api/device', method='POST',
            data=json.dumps({'id': id_, 'type': type_, 'hostname': id_}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=30).read()

    _mkdev(BE1, 'catalyst')
    _mkdev(BE2, 'cisco')
    for be in (BE1, BE2):
        for cmd in ('configure terminal', 'crypto key generate rsa modulus 2048', 'end'):
            _live_cli(cmd, be)
    time.sleep(2)

    _mkdev(LB, 'bigip')
    _live_cli(f'create ltm pool e2e_pool {{ members add {{ {BE1_IP}:22 {BE2_IP}:22 }} '
             f'monitor tcp load-balancing-mode round-robin }}', LB)
    _live_cli(f'create ltm virtual e2e_vs {{ destination {VIP_IP}:{VIP_PORT} pool e2e_pool }}', LB)
    time.sleep(2)
    yield


class TestBigipLiveLoadBalancing:
    def test_pool_shows_both_backends_up(self, lb_with_ssh_backends):
        out = _live_cli('show ltm pool e2e_pool', LB)
        assert 'up: 2, down: 0' in out

    def test_real_ssh_sessions_alternate_round_robin(self, lb_with_ssh_backends):
        paramiko = pytest.importorskip('paramiko')
        hostnames = []
        for _ in range(4):
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(VIP_IP, port=VIP_PORT, username='admin', password='admin',
                       timeout=10, look_for_keys=False, allow_agent=False)
            chan = cli.invoke_shell()
            time.sleep(0.5)
            banner = chan.recv(2000).decode(errors='replace')
            lines = [l for l in banner.strip().splitlines() if l.strip()]
            hostnames.append(lines[-1].rstrip('#').strip() if lines else '')
            cli.close()
        assert set(hostnames) == {BE1, BE2}, f'両方のバックエンドに振り分けられるはず: {hostnames}'
        assert hostnames[0] != hostnames[1], 'round-robinなので連続する接続は別バックエンドのはず'

    def test_disabling_backend_ssh_triggers_real_failover(self, lb_with_ssh_backends):
        paramiko = pytest.importorskip('paramiko')
        _live_cli('configure terminal', BE1)
        _live_cli('crypto key zeroize rsa', BE1)
        _live_cli('end', BE1)
        time.sleep(1)

        out = _live_cli('show ltm pool e2e_pool', LB)
        assert 'up: 1, down: 1' in out

        hostnames = []
        for _ in range(3):
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(VIP_IP, port=VIP_PORT, username='admin', password='admin',
                       timeout=10, look_for_keys=False, allow_agent=False)
            chan = cli.invoke_shell()
            time.sleep(0.5)
            banner = chan.recv(2000).decode(errors='replace')
            lines = [l for l in banner.strip().splitlines() if l.strip()]
            hostnames.append(lines[-1].rstrip('#').strip() if lines else '')
            cli.close()
        assert hostnames == [BE2, BE2, BE2], \
            f'落としたbackendには二度と振り分けられないはず: {hostnames}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
