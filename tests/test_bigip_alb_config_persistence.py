"""
F5 BIG-IP(LTM) / AWS ALB の設定永続化テスト。

「今までの検証を色々やってみましたが自分が依頼したものが多いので
どうかなと...自動検証のパターン出しとかお願いしたい」への対応として、
ユーザー指定の検証対象ではなく自発的にコードを調査して見つけたギャップ:
`app.py`の`_save_config()`/`_load_config()`(`saved_config.json`への
保存・60秒ごとの自動保存・起動時の復元)は、Nexus TACACS+/AAA・
Si-R IPsec等は個別に対応済みだったが、このセッションで追加された
BIG-IPのLTM設定(pool/virtual/monitor/persistence/snatpool/net vlan/
net self)とALBのtarget-group/listener/ruleは一度も対応されておらず、
**アプリを再起動するたびに全て消え、実リスナー
(VirtualServerProxy/AlbListenerProxy)も再起動されないまま失われる**
という実バグだった。

`tests/test_nexus_tacacs_persistence.py`と同じ
隔離パターン(SAVED_CONFIG_PATHをtmp_pathへ、device_sessionsを空の辞書へ
monkeypatch)で、(1)辞書状態そのものの保存・復元に加えて、(2)復元後に
実TCPリスナーが実際に上がりトラフィックを中継すること——辞書を復元
しただけでは実ソケットは上がらないため、ここまで確認しないと
「直った」とは言えない——の両方を検証する。
"""

import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from fastapi.testclient import TestClient
import app as app_module

client = TestClient(app_module.app)


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


class _FakeBackend:
    """接続されたら固定の1行を返すだけの簡易TCPバックエンド。"""

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
        self._thread.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass


class TestBigipLtmConfigSurvivesSaveAndLoad:
    def test_bigip_ltm_state_round_trips_through_save_and_load(self, tmp_path, monkeypatch):
        from engine.bigip_lb import stop_virtual_server_proxy

        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        backend = _FakeBackend(b'BIGIP-BACKEND\n')
        try:
            app_module.device_sessions['bp-persist-1'] = app_module.DeviceState('bigip', 'bp-persist-1')
            _cli('bp-persist-1', 'create net vlan internal { interfaces add { 1.2 } }')
            _cli('bp-persist-1', 'create net self my_self address 10.97.90.245/24 vlan internal')
            _cli('bp-persist-1',
                 f'create ltm pool persistpool1 {{ members add {{ 127.0.0.1:{backend.port} }} '
                 f'monitor tcp }}')
            _cli('bp-persist-1', 'create ltm persistence source-addr sp1 { timeout 60 }')
            _cli('bp-persist-1',
                 'create ltm virtual persistvs1 { destination 10.97.90.1:9910 pool persistpool1 '
                 'persist replace-all-with { sp1 } }')

            # 保存前に、辞書状態が実際に設定されていることを確認しておく
            state_before = app_module.device_sessions['bp-persist-1']
            assert 'persistpool1' in state_before.f5_pools
            assert 'persistvs1' in state_before.f5_virtuals
            assert 'my_self' in state_before.f5_selfips

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            dev = saved['devices']['bp-persist-1']
            assert 'persistpool1' in dev['f5_pools']
            assert 'persistvs1' in dev['f5_virtuals']
            assert dev['f5_selfips']['my_self']['address'] == '10.97.90.245'
            assert 'sp1' in dev['f5_persistence_profiles']

            # 「再起動」を模して device_sessions を空にしてから再ロード
            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            restored = app_module.device_sessions['bp-persist-1']
            assert restored.f5_pools['persistpool1']['members'][0]['port'] == backend.port
            assert restored.f5_virtuals['persistvs1']['destination'] == '10.97.90.1:9910'
            assert restored.f5_selfips['my_self']['address'] == '10.97.90.245'
            assert restored.f5_persistence_profiles['sp1']['timeout'] == 60

            # 辞書が復元されただけでは実ソケットは上がらない。_load_config()が
            # VirtualServerProxyを実際に再起動していることを、本物のTCP接続で確認する。
            deadline = time.time() + 3
            last_err = None
            while time.time() < deadline:
                try:
                    with socket.create_connection(('10.97.90.1', 9910), timeout=1) as s:
                        s.settimeout(1)
                        data = s.recv(32)
                    assert data == b'BIGIP-BACKEND\n', (
                        '復元後、実リスナーがバックエンドへ実際に中継していない')
                    break
                except OSError as e:
                    last_err = e
                    time.sleep(0.2)
            else:
                raise AssertionError(
                    f'復元後、VIP 10.97.90.1:9910 への実接続に失敗した: {last_err}')
        finally:
            backend.stop()
            stop_virtual_server_proxy('bp-persist-1', 'persistvs1')

    def test_non_bigip_device_has_no_ltm_keys_in_save(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        app_module.device_sessions['r1'] = app_module.DeviceState('cisco', 'r1')
        app_module._save_config()

        saved = json.loads(cfg_path.read_text(encoding='utf-8'))
        r1_data = saved['devices']['r1']
        for key in ('f5_pools', 'f5_virtuals', 'f5_monitors', 'f5_persistence_profiles',
                    'f5_snatpools', 'f5_vlans', 'f5_selfips'):
            assert key not in r1_data


class TestAlbConfigSurvivesSaveAndLoad:
    def test_alb_state_round_trips_through_save_and_load(self, tmp_path, monkeypatch):
        from engine.alb_lb import stop_alb_listener_proxy

        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        backend = _FakeBackend(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')
        try:
            app_module.device_sessions['alb-persist-1'] = app_module.DeviceState('alb', 'alb-persist-1')
            _cli('alb-persist-1', 'aws elbv2 create-load-balancer --name apersist1 --type application')
            _cli('alb-persist-1', 'aws elbv2 create-target-group --name tgpersist1 --protocol HTTP '
                                  '--port 8000')
            _cli('alb-persist-1', f'aws elbv2 register-targets --target-group-arn tgpersist1 '
                                  f'--targets Id=127.0.0.1,Port={backend.port}')
            _cli('alb-persist-1', 'aws elbv2 create-listener --load-balancer-arn apersist1 '
                                  '--protocol HTTP --port 9180 '
                                  '--default-actions Type=forward,TargetGroupArn=tgpersist1')

            state_before = app_module.device_sessions['alb-persist-1']
            assert state_before.alb_listeners
            listener_arn = list(state_before.alb_listeners.keys())[0]
            tg_arn = list(state_before.alb_target_groups.keys())[0]
            assert state_before.alb_target_groups[tg_arn]['name'] == 'tgpersist1'

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            dev = saved['devices']['alb-persist-1']
            assert dev['alb_target_groups'][tg_arn]['name'] == 'tgpersist1'
            assert listener_arn in dev['alb_listeners']

            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            restored = app_module.device_sessions['alb-persist-1']
            assert restored.alb_target_groups[tg_arn]['name'] == 'tgpersist1'
            assert listener_arn in restored.alb_listeners

            # 復元後、実リスナー(AlbListenerProxy)がVIP:9180で実際に
            # HTTPを中継していることを本物のHTTPクライアントで確認する。
            import urllib.request
            deadline = time.time() + 3
            last_err = None
            body = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen('http://172.31.0.10:9180/', timeout=1) as r:
                        body = r.read()
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.2)
            assert body == b'OK', f'復元後、ALB実リスナーへのHTTP到達に失敗した: {last_err}'
        finally:
            backend.stop()
            state = app_module.device_sessions.get('alb-persist-1')
            if state:
                for arn in list(state.alb_listeners.keys()):
                    stop_alb_listener_proxy('alb-persist-1', arn)

    def test_non_alb_device_has_no_alb_keys_in_save(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        app_module.device_sessions['r2'] = app_module.DeviceState('cisco', 'r2')
        app_module._save_config()

        saved = json.loads(cfg_path.read_text(encoding='utf-8'))
        r2_data = saved['devices']['r2']
        for key in ('alb_load_balancers', 'alb_target_groups', 'alb_listeners', 'alb_rules'):
            assert key not in r2_data
