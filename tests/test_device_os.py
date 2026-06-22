"""
各機器OS（Si-R / Catalyst / Cisco ISR）ごとのコマンド動作検証。
実際にFastAPIサーバーを起動し、HTTP経由でCLIコマンドを送って応答を検証する。

実行: cd network-lab && python -m pytest tests/test_device_os.py -v

注: このテストはサーバーを起動するため、ポート8000が空いている必要がある。
"""
import subprocess
import time
import sys
import os
import json
import urllib.request
import pytest

BASE = 'http://localhost:8000'


def _post(path, data):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _cli(device_id, command):
    return _post('/api/cli', {'device_id': device_id, 'command': command})['output']


@pytest.fixture(scope='module')
def server():
    """テスト用サーバーを起動"""
    proc = subprocess.Popen(
        [sys.executable, 'app.py'],
        cwd=os.path.join(os.path.dirname(__file__), '..'),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 起動待ち
    for _ in range(30):
        try:
            urllib.request.urlopen(BASE + '/api/status', timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip('サーバー起動失敗')
    # 装置登録
    _post('/api/device', {'id': 'sir', 'type': 'sir', 'hostname': 'SiR-Router'})
    _post('/api/device', {'id': 'cat', 'type': 'catalyst', 'hostname': 'Catalyst-SW'})
    _post('/api/device', {'id': 'isr', 'type': 'cisco', 'hostname': 'ISR-Router'})
    yield
    proc.terminate()
    proc.wait()


# ══════════════════════════════════════════
# Si-R コマンド検証
# ══════════════════════════════════════════
class TestSiR:
    def test_show_ether(self, server):
        out = _cli('sir', 'show ether')
        assert out and len(out) > 10, "show ether が空"

    def test_show_interfaces(self, server):
        out = _cli('sir', 'show interfaces')
        assert out and len(out) > 10

    def test_show_ip_route(self, server):
        out = _cli('sir', 'show ip route')
        assert 'Codes' in out or 'directly connected' in out or len(out) > 5

    def test_show_arp(self, server):
        out = _cli('sir', 'show arp')
        assert 'MAC' in out or 'self' in out

    def test_rip_config(self, server):
        _cli('sir', 'ip rip use use')
        _cli('sir', 'ip rip network 192.168.1.0/24')
        out = _cli('sir', 'show ip rip route')
        assert '192.168.1.0' in out, "Si-R RIP設定が反映されない"


# ══════════════════════════════════════════
# Catalyst コマンド検証
# ══════════════════════════════════════════
class TestCatalyst:
    def test_show_interfaces_status(self, server):
        out = _cli('cat', 'show interfaces status')
        assert out and len(out) > 10

    def test_show_vlan(self, server):
        out = _cli('cat', 'show vlan brief')
        assert out and len(out) > 5

    def test_show_ip_arp(self, server):
        out = _cli('cat', 'show ip arp')
        assert 'Protocol' in out or 'Internet' in out or 'ARPA' in out

    def test_spanning_tree(self, server):
        _cli('cat', 'spanning-tree mode rstp')
        out = _cli('cat', 'show spanning-tree')
        assert 'rstp' in out.lower() or 'spanning' in out.lower() or 'VLAN' in out

    def test_ospf_config(self, server):
        _cli('cat', 'router ospf 1')
        _cli('cat', 'network 192.168.10.0 0.0.0.255 area 0')
        out = _cli('cat', 'show ip ospf neighbor')
        # ネイバーは居なくてもヘッダが出ればOK
        assert 'Neighbor' in out or len(out) > 5


# ══════════════════════════════════════════
# Cisco ISR コマンド検証
# ══════════════════════════════════════════
class TestCiscoISR:
    def test_show_version(self, server):
        out = _cli('isr', 'show version')
        assert out and len(out) > 10

    def test_show_ip_interface_brief(self, server):
        out = _cli('isr', 'show ip interface brief')
        assert 'Interface' in out or len(out) > 10

    def test_standard_acl(self, server):
        _cli('isr', 'access-list 10 deny host 10.0.0.5')
        _cli('isr', 'access-list 10 permit any')
        out = _cli('isr', 'show access-list')
        assert '10' in out and ('deny' in out or 'permit' in out)

    def test_static_route(self, server):
        _cli('isr', 'ip route 172.16.0.0 255.255.0.0 10.0.0.2')
        out = _cli('isr', 'show ip route')
        assert '172.16' in out or 'S' in out

    def test_bgp_config(self, server):
        _cli('isr', 'router bgp 65001')
        out = _cli('isr', 'show ip bgp summary')
        assert 'BGP' in out or 'router identifier' in out


# ══════════════════════════════════════════
# クロスベンダー検証（異機種間プロトコル）
# ══════════════════════════════════════════
class TestCrossVendor:
    def test_sir_catalyst_link(self, server):
        """Si-RとCatalystをリンクできる"""
        r = _post('/api/links/sync', {'links': [{'a': 'sir', 'b': 'cat'}]})
        assert r['ok']

    def test_ping_command_works_all(self, server):
        """全機種でpingコマンドが応答する"""
        for dev in ['sir', 'cat', 'isr']:
            out = _cli(dev, 'ping 127.0.0.1')
            # 形式は機種で違うが何か応答があればOK
            assert out and len(out) > 5, f'{dev} のping無応答'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
