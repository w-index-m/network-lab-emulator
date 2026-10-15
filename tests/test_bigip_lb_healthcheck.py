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


class _FakeBackendReportingPeer(_FakeBackend):
    """SNAT検証用: 固定文字列ではなく、実際に見えている接続元IP
    (getpeername())を返す——VirtualServerProxyがSNATで送信元IPを
    変えていれば、ここに現れるのは実クライアントのIPではなくSNAT後
    のIPになるはず、という本物のソケットレベルの確認。"""

    def __init__(self):
        super().__init__(b'')

    def _loop(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.5)
                conn, addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.sendall(addr[0].encode() + b'\n')
            except OSError:
                pass
            finally:
                try:
                    conn.close()
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


class TestBigipPersistenceConfigCli:
    """ユーザー依頼「疑似クライアントから、パーシステンスの確認とかも
    お願いできますか？」への対応——実機の`source_addr`パーシステンス
    プロファイル(`create ltm persistence source-addr`)のCLI設定面。"""

    def test_create_persistence_profile_and_attach_to_virtual(self):
        _dev('bp-persist-cli-1')
        _cli('bp-persist-cli-1', 'create ltm persistence source-addr my_persist { timeout 60 }')
        _cli('bp-persist-cli-1', 'create ltm pool pp1 { members add { 10.1.1.1:80 } }')
        _cli('bp-persist-cli-1', 'create ltm virtual vp1 { destination 10.9.9.8:8080 pool pp1 '
                                 'persist replace-all-with { my_persist } }')
        state = app_module.device_sessions['bp-persist-cli-1']
        assert state.f5_persistence_profiles['my_persist'] == {'type': 'source-addr', 'timeout': 60}
        assert state.f5_virtuals['vp1']['persist'] == 'my_persist'

    def test_show_virtual_reflects_persistence(self):
        _dev('bp-persist-cli-2')
        _cli('bp-persist-cli-2', 'create ltm persistence source-addr sp2 { timeout 45 }')
        _cli('bp-persist-cli-2', 'create ltm pool pp2 { members add { 10.1.1.1:80 } }')
        _cli('bp-persist-cli-2', 'create ltm virtual vp2 { destination 10.9.9.7:8080 pool pp2 '
                                 'persist replace-all-with { sp2 } }')
        out = _cli('bp-persist-cli-2', 'show ltm virtual vp2')
        assert 'Persistence' in out and 'sp2' in out and '45s' in out

    def test_running_config_reflects_persistence_profile(self):
        _dev('bp-persist-cli-3')
        _cli('bp-persist-cli-3', 'create ltm persistence source-addr sp3 { timeout 90 }')
        out = _cli('bp-persist-cli-3', 'show running-config')
        assert 'ltm persistence source-addr sp3' in out
        assert 'timeout 90' in out


class TestVirtualServerProxyPersistenceDirect:
    """VirtualServerProxyの実際の送信元IPアフィニティ(source-addr
    persistence)の振る舞いを、簡易バックエンドで直接固定する。"""

    def test_same_client_sticks_to_same_member_while_round_robin_would_alternate(self):
        from engine.bigip_lb import VirtualServerProxy
        b1 = _FakeBackend(b'B1\n')
        b2 = _FakeBackend(b'B2\n')
        members = [('127.0.0.1', b1.port), ('127.0.0.1', b2.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin',
                                   get_persist_timeout=lambda: 30)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            results = []
            for _ in range(4):
                with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                    s.settimeout(3)
                    results.append(s.recv(16))
            # 送信元IPは毎回同じ(127.0.0.1)なので、persistence無しなら
            # round-robinで交互になるはずが、全部同じメンバーに固定される
            assert len(set(results)) == 1, f'同じクライアントは同じメンバーに固定されるはず: {results}'
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_falls_back_to_load_balancing_when_affinity_member_is_down(self):
        from engine.bigip_lb import VirtualServerProxy
        b1 = _FakeBackend(b'B1\n')
        b2 = _FakeBackend(b'B2\n')
        members = [('127.0.0.1', b1.port), ('127.0.0.1', b2.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin',
                                   get_persist_timeout=lambda: 30)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                s.settimeout(3)
                first = s.recv(16)
            # 固定されたメンバーを停止してget_membersから消す
            stuck_port = b1.port if first == b'B1\n' else b2.port
            if stuck_port == b1.port:
                b1.stop()
                members[:] = [('127.0.0.1', b2.port)]
            else:
                b2.stop()
                members[:] = [('127.0.0.1', b1.port)]
            with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                s.settimeout(3)
                second = s.recv(16)
            assert second != first, '固定先がdownなら別メンバーにフォールバックするはず'
        finally:
            proxy.stop()
            b1.stop()
            b2.stop()

    def test_no_persistence_configured_still_round_robins(self):
        """regressionガード: persistence未設定(get_persist_timeout=None)
        なら従来通りround-robinが効くこと。"""
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


class TestBigipSnatConfigCli:
    """ユーザー依頼「ナット...を実装して...一意性を保って、負荷分散
    してるか試験もお願いします」「ちなみにSナットの試験も含めて
    欲しい」への対応——実機のSNAT(automap/snatpool)のCLI設定面。"""

    def test_create_snatpool_and_attach_automap(self):
        _dev('bp-snat-cli-1')
        _cli('bp-snat-cli-1', 'create ltm pool pp1 { members add { 10.1.1.1:80 } }')
        _cli('bp-snat-cli-1', 'create ltm virtual vs1 { destination 10.9.9.1:8080 pool pp1 '
                              'source-address-translation { type automap } }')
        state = app_module.device_sessions['bp-snat-cli-1']
        assert state.f5_virtuals['vs1']['snat'] == {'type': 'automap', 'pool': None}

    def test_create_snatpool_and_attach_pool(self):
        _dev('bp-snat-cli-2')
        _cli('bp-snat-cli-2', 'create ltm snatpool sp1 { members add { 10.5.5.1 10.5.5.2 } }')
        _cli('bp-snat-cli-2', 'create ltm pool pp2 { members add { 10.1.1.1:80 } }')
        _cli('bp-snat-cli-2', 'create ltm virtual vs2 { destination 10.9.9.2:8080 pool pp2 '
                              'source-address-translation { type snat pool sp1 } }')
        state = app_module.device_sessions['bp-snat-cli-2']
        assert state.f5_snatpools['sp1']['members'] == ['10.5.5.1', '10.5.5.2']
        assert state.f5_virtuals['vs2']['snat'] == {'type': 'snat', 'pool': 'sp1'}

    def test_show_virtual_reflects_snat(self):
        _dev('bp-snat-cli-3')
        _cli('bp-snat-cli-3', 'create ltm snatpool sp3 { members add { 10.5.5.9 } }')
        _cli('bp-snat-cli-3', 'create ltm pool pp3 { members add { 10.1.1.1:80 } }')
        _cli('bp-snat-cli-3', 'create ltm virtual vs3 { destination 10.9.9.3:8080 pool pp3 '
                              'source-address-translation { type snat pool sp3 } }')
        out = _cli('bp-snat-cli-3', 'show ltm virtual vs3')
        assert 'SNAT' in out and 'sp3' in out and '10.5.5.9' in out

    def test_running_config_reflects_snatpool(self):
        _dev('bp-snat-cli-4')
        _cli('bp-snat-cli-4', 'create ltm snatpool sp4 { members add { 10.5.5.3 10.5.5.4 } }')
        out = _cli('bp-snat-cli-4', 'show running-config')
        assert 'ltm snatpool sp4' in out
        assert '10.5.5.3' in out and '10.5.5.4' in out


class TestVirtualServerProxySnatDirect:
    """VirtualServerProxyの実際のSNAT(automap/snatpool)の振る舞いを、
    「接続元IPをそのまま返すバックエンド」で直接固定する——本物の
    ソケットレベルで送信元IPが変わっていることを確認する。"""

    def test_automap_uses_vip_as_source(self):
        from engine.bigip_lb import VirtualServerProxy
        from engine.loopback_alias import ensure_loopback_alias
        vip = '127.0.20.1'
        ensure_loopback_alias(vip)
        backend = _FakeBackendReportingPeer()
        members = [('127.0.0.1', backend.port)]
        proxy = VirtualServerProxy(vip, 0, lambda: members, lambda: 'round-robin',
                                   get_snat_source=lambda: vip)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            with socket.create_connection((vip, vip_port), timeout=3) as s:
                s.settimeout(3)
                seen_source = s.recv(64).decode().strip()
            assert seen_source == vip, f'automapならバックエンドはVIP自身のIPを見るはず: {seen_source}'
        finally:
            proxy.stop()
            backend.stop()

    def test_snatpool_uses_configured_member_as_source(self):
        from engine.bigip_lb import VirtualServerProxy
        from engine.loopback_alias import ensure_loopback_alias
        snat_ip = '127.0.20.50'
        ensure_loopback_alias(snat_ip)
        backend = _FakeBackendReportingPeer()
        members = [('127.0.0.1', backend.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin',
                                   get_snat_source=lambda: snat_ip)
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                s.settimeout(3)
                seen_source = s.recv(64).decode().strip()
            assert seen_source == snat_ip, f'snatpoolの指定IPをバックエンドは見るはず: {seen_source}'
        finally:
            proxy.stop()
            backend.stop()

    def test_no_snat_configured_backend_sees_real_proxy_source(self):
        """regressionガード: SNAT未設定(get_snat_source=None)なら、
        バックエンドが見る送信元はOS既定(VirtualServerProxy自身の
        アウトバウンド接続元、通常127.0.0.1)のまま——明示的なSNAT
        IPには固定されない。"""
        from engine.bigip_lb import VirtualServerProxy
        backend = _FakeBackendReportingPeer()
        members = [('127.0.0.1', backend.port)]
        proxy = VirtualServerProxy('127.0.0.1', 0, lambda: members, lambda: 'round-robin')
        try:
            proxy.start()
            vip_port = proxy._sock.getsockname()[1]
            with socket.create_connection(('127.0.0.1', vip_port), timeout=3) as s:
                s.settimeout(3)
                seen_source = s.recv(64).decode().strip()
            assert seen_source == '127.0.0.1'
        finally:
            proxy.stop()
            backend.stop()


class TestBigipObjectUniqueness:
    """ユーザー依頼「一意性を保って」への対応——pool/virtual/node/
    persistence/snatpoolの名前付きオブジェクトが、再作成・メンバー
    追加の重複投入で破損/重複しないことを固定する。"""

    def test_recreating_pool_with_same_name_does_not_duplicate_members(self):
        _dev('bp-uniq-1')
        _cli('bp-uniq-1', 'create ltm pool up1 { members add { 10.1.1.1:80 } }')
        _cli('bp-uniq-1', 'create ltm pool up1 { members add { 10.1.1.1:80 } }')
        state = app_module.device_sessions['bp-uniq-1']
        assert len(state.f5_pools['up1']['members']) == 1, '同じメンバーの再投入で重複しないはず'

    def test_registering_same_snatpool_member_twice_does_not_duplicate(self):
        _dev('bp-uniq-2')
        _cli('bp-uniq-2', 'create ltm snatpool usp1 { members add { 10.5.5.1 } }')
        _cli('bp-uniq-2', 'create ltm snatpool usp1 { members add { 10.5.5.1 } }')
        state = app_module.device_sessions['bp-uniq-2']
        assert state.f5_snatpools['usp1']['members'] == ['10.5.5.1']

    def test_nodes_derived_from_pool_members_are_not_duplicated_across_pools(self):
        _dev('bp-uniq-3')
        _cli('bp-uniq-3', 'create ltm pool up3a { members add { 10.1.1.9:80 } }')
        _cli('bp-uniq-3', 'create ltm pool up3b { members add { 10.1.1.9:443 } }')
        out = _cli('bp-uniq-3', 'show ltm node')
        node_lines = [l for l in out.splitlines() if '10.1.1.9' in l]
        assert len(node_lines) == 1, f'同じノードIPは複数プールから見えても1行にまとまるはず: {out}'

    def test_distinct_pools_virtuals_and_persistence_profiles_stay_independent(self):
        _dev('bp-uniq-4')
        _cli('bp-uniq-4', 'create ltm pool up4a { members add { 10.1.1.1:80 } }')
        _cli('bp-uniq-4', 'create ltm pool up4b { members add { 10.1.1.2:80 } }')
        _cli('bp-uniq-4', 'create ltm persistence source-addr psp4a { timeout 30 }')
        _cli('bp-uniq-4', 'create ltm persistence source-addr psp4b { timeout 60 }')
        state = app_module.device_sessions['bp-uniq-4']
        assert set(state.f5_pools.keys()) >= {'up4a', 'up4b'}
        assert state.f5_pools['up4a']['members'][0]['node'] == '10.1.1.1'
        assert state.f5_pools['up4b']['members'][0]['node'] == '10.1.1.2'
        assert state.f5_persistence_profiles['psp4a']['timeout'] == 30
        assert state.f5_persistence_profiles['psp4b']['timeout'] == 60


class TestBigipMockSelfIp:
    """ユーザー依頼「モックのBIGIPなのでモックIPくらいのモック頼みます」
    への対応——これまでbigipはCisco系デフォルト(GigabitEthernet0/0/0 @
    203.0.113.2)をそのまま継承し、cisco/arista/fortigate等のデフォルトと
    衝突していた。F5実機の"slot.port"命名(1.1/1.2)+専用mgmtインタフェース
    +他機種と衝突しない固有Self IPを持つこと、および実機の"tmsh create
    net self <name> address <ip>/<mask> vlan <vlan_name>"構文(Self IPは
    インタフェースではなくVLANへ割り当てる)がCLI経由で実際に
    state.interfacesへ反映されることを固定する。"""

    def test_default_interfaces_use_f5_slot_port_naming_and_dont_collide(self):
        _dev('bp-mockip-1')
        state = app_module.device_sessions['bp-mockip-1']
        assert set(state.interfaces.keys()) == {'mgmt', '1.1', '1.2'}
        other_defaults = {'203.0.113.2', '10.0.0.1', '192.168.1.10', '192.168.10.1',
                           '192.168.100.1', '203.0.113.1'}
        bigip_ips = {v['ip'] for v in state.interfaces.values() if v['ip']}
        assert not (bigip_ips & other_defaults), (
            f'BIG-IPのデフォルトSelf IPが他機種のデフォルトと衝突している: {bigip_ips}')

    def test_create_net_vlan_and_net_self_assigns_ip_to_the_vlans_interface(self):
        _dev('bp-mockip-2')
        _cli('bp-mockip-2', 'create net vlan internal { interfaces add { 1.2 } }')
        out = _cli('bp-mockip-2',
                    'create net self internal_self address 10.1.20.246/24 vlan internal')
        assert out == ''
        state = app_module.device_sessions['bp-mockip-2']
        assert state.f5_vlans['internal']['interfaces'] == ['1.2']
        assert state.f5_selfips['internal_self'] == {
            'address': '10.1.20.246', 'prefix': 24, 'vlan': 'internal'}
        # VLANが指すインタフェース(1.2)のIPが実際に更新されること
        assert state.interfaces['1.2']['ip'] == '10.1.20.246'
        assert state.interfaces['1.2']['prefix'] == 24

    def test_show_and_list_net_self_render_the_configured_self_ip(self):
        _dev('bp-mockip-3')
        _cli('bp-mockip-3', 'create net vlan external { interfaces add { 1.1 } }')
        _cli('bp-mockip-3', 'create net self ext_self address 203.0.113.250/24 vlan external')
        show_out = _cli('bp-mockip-3', 'show net self')
        assert 'ext_self' in show_out and '203.0.113.250/24' in show_out and 'external' in show_out
        list_out = _cli('bp-mockip-3', 'list net self')
        assert 'net self ext_self' in list_out
        assert 'address 203.0.113.250/24' in list_out
        assert 'vlan external' in list_out

    def test_show_running_config_reflects_net_vlan_and_net_self(self):
        _dev('bp-mockip-4')
        _cli('bp-mockip-4', 'create net vlan internal { interfaces add { 1.2 } }')
        _cli('bp-mockip-4', 'create net self internal_self address 10.1.20.247/24 vlan internal')
        out = _cli('bp-mockip-4', 'show running-config')
        assert 'net vlan internal' in out
        assert 'interfaces { 1.2 }' in out
        assert 'net self internal_self' in out
        assert 'address 10.1.20.247/24' in out

    def test_self_ip_on_unknown_vlan_interface_does_not_touch_other_interfaces(self):
        _dev('bp-mockip-5')
        _cli('bp-mockip-5', 'create net self orphan_self address 10.9.9.9/24 vlan not_yet_created')
        state = app_module.device_sessions['bp-mockip-5']
        assert state.f5_selfips['orphan_self']['address'] == '10.9.9.9'
        # vlanがまだ存在しない(=interfacesが空)ので、既存インタフェースのIPは変化しない
        assert state.interfaces['1.1']['ip'] == '203.0.113.245'
        assert state.interfaces['1.2']['ip'] == '10.1.20.245'


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
