"""
Netmiko を使用した Catalyst 設定変更・状態確認テスト

実行:
  python -m pytest tests/test_netmiko_catalyst.py -v -s

またはスタンドアロン実行:
  python tests/test_netmiko_catalyst.py

注: netmiko実機接続が可能な場合と、HTTPエミュレーターAPIどちらにも対応
"""
import subprocess
import time
import sys
import os
import json
import urllib.request
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

BASE = 'http://localhost:8000'


def _post(path, data):
    """HTTPエミュレーター API呼び出し"""
    try:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(data).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"API呼び出し失敗: {e}")
        return None


def _cli(device_id, command):
    """CLIコマンド実行（エミュレーター）"""
    result = _post('/api/cli', {'device_id': device_id, 'command': command})
    if result and 'output' in result:
        return result['output']
    return ""


@pytest.fixture(scope='module')
def server():
    """テスト用サーバーを起動"""
    env = os.environ.copy()
    env['NETLAB_AUTH_DISABLE'] = '1'
    proc = subprocess.Popen(
        [sys.executable, 'app.py'],
        cwd=os.path.join(os.path.dirname(__file__), '..'),
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # サーバー起動待機
    for _ in range(30):
        try:
            urllib.request.urlopen(BASE + '/api/status', timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip('サーバー起動失敗')

    # テスト用デバイス登録
    _post('/api/device', {'id': 'catalyst-test', 'type': 'catalyst', 'hostname': 'Cat-Test'})
    _post('/api/device', {'id': 'cisco-test', 'type': 'cisco', 'hostname': 'Cisco-Test'})

    yield
    proc.terminate()
    proc.wait()


class TestCatalystNetmikoStyle:
    """
    Netmiko類似のインターフェースで Catalyst の設定変更・状態確認をテスト
    実装: HTTP APIを通じてエミュレーターを制御
    """

    def test_catalyst_interface_config_and_verify(self, server):
        """Catalyst インターフェース設定投入・状態確認"""
        dev_id = 'catalyst-test'

        # 1. 設定投入（CLIコマンド）
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'interface Gi1/0/1')
        out = _cli(dev_id, 'ip address 10.0.1.1 255.255.255.0')

        # 2. 設定確認
        out = _cli(dev_id, 'show running-config interface Gi1/0/1')
        assert '10.0.1.1' in out, "Catalystに IP アドレスが設定されていない"
        assert '255.255.255.0' in out, "サブネットマスクが反映されていない"

        # 3. インターフェース状態確認
        out = _cli(dev_id, 'show interfaces Gi1/0/1')
        assert 'GigabitEthernet1/0/1' in out or 'Gi1/0/1' in out, "インターフェース情報が取得できない"

    def test_catalyst_ospf_config_netmiko_style(self, server):
        """OSPF設定の投入と隣接状態確認"""
        dev_id = 'catalyst-test'

        # OSPF プロセス設定
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'router ospf 1')
        _cli(dev_id, 'network 10.0.1.0 0.0.0.255 area 0')
        _cli(dev_id, 'exit')

        # OSPF設定確認
        out = _cli(dev_id, 'show running-config | include ospf')
        assert 'ospf 1' in out.lower(), "OSPF設定が反映されていない"

        out = _cli(dev_id, 'show ip ospf')
        assert '1' in out, "OSPF プロセスID が表示されていない"

    def test_catalyst_bgp_config_verification(self, server):
        """BGP設定投入と確認"""
        dev_id = 'catalyst-test'

        # BGP設定投入
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'router bgp 65001')
        _cli(dev_id, 'neighbor 192.168.1.1 remote-as 65002')
        _cli(dev_id, 'address-family ipv4')
        _cli(dev_id, 'network 172.16.1.0 mask 255.255.255.0')
        _cli(dev_id, 'exit-address-family')
        _cli(dev_id, 'exit')

        # BGP設定確認
        out = _cli(dev_id, 'show running-config | include bgp')
        assert 'bgp 65001' in out.lower(), "BGP AS が設定されていない"
        assert '65002' in out, "BGP neighbor が設定されていない"

        # BGP プロセス確認
        out = _cli(dev_id, 'show ip bgp summary')
        assert '65001' in out, "BGP AS番号が表示されていない"

    def test_catalyst_acl_config_and_verification(self, server):
        """ACL設定投入・確認"""
        dev_id = 'catalyst-test'

        # ACL投入
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'ip access-list extended TEST_ACL')
        _cli(dev_id, 'permit ip 10.0.0.0 0.0.0.255 any')
        _cli(dev_id, 'deny ip any any')
        _cli(dev_id, 'exit')

        # ACL確認
        out = _cli(dev_id, 'show access-lists TEST_ACL')
        assert 'TEST_ACL' in out or '10.0.0.0' in out, "ACL が設定されていない"
        assert 'permit' in out.lower(), "permit ルールが表示されていない"

    def test_catalyst_vlan_config(self, server):
        """VLAN設定投入・確認"""
        dev_id = 'catalyst-test'

        # VLAN設定
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'vlan 100')
        _cli(dev_id, 'name TEST-VLAN')
        _cli(dev_id, 'exit')

        # VLAN確認
        out = _cli(dev_id, 'show vlan id 100')
        assert '100' in out, "VLAN ID が表示されていない"
        assert 'TEST-VLAN' in out or 'test' in out.lower(), "VLAN 名が反映されていない"

    def test_catalyst_save_config(self, server):
        """設定保存の確認"""
        dev_id = 'catalyst-test'

        # 設定投入
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'hostname CAT-MODIFIED')
        _cli(dev_id, 'exit')

        # save/write memory 相当
        out = _cli(dev_id, 'write memory')

        # 保存確認
        out = _cli(dev_id, 'show running-config | include hostname')
        assert 'CAT-MODIFIED' in out, "ホスト名設定が保存されていない"

    def test_cisco_router_config(self, server):
        """Cisco ISR ルーターの設定変更・確認"""
        dev_id = 'cisco-test'

        # インターフェース設定
        _cli(dev_id, 'conf t')
        _cli(dev_id, 'interface GigabitEthernet0/0/1')
        _cli(dev_id, 'ip address 10.1.1.1 255.255.255.0')
        _cli(dev_id, 'no shutdown')
        _cli(dev_id, 'exit')

        # 設定確認
        out = _cli(dev_id, 'show running-config interface GigabitEthernet0/0/1')
        assert '10.1.1.1' in out, "Cisco ルーターに IP アドレスが設定されていない"


class TestNetmikoRealDeviceStub:
    """
    実機Catalyst/Cisco接続時のテストスタブ
    実装例: 実機接続情報が環境変数で与えられた場合に実行
    """

    @pytest.mark.skipif(
        not os.getenv('NETMIKO_CATALYST_HOST'),
        reason="実機Catalyst接続情報なし（NETMIKO_CATALYST_HOST環境変数で設定）"
    )
    def test_real_catalyst_connection_and_config(self):
        """実機Catalyst への netmiko 接続テスト"""
        try:
            from netmiko import ConnectHandler
        except ImportError:
            pytest.skip("netmiko がインストールされていない")

        device = {
            'device_type': 'cisco_ios',
            'host': os.getenv('NETMIKO_CATALYST_HOST'),
            'username': os.getenv('NETMIKO_USERNAME', 'admin'),
            'password': os.getenv('NETMIKO_PASSWORD', 'admin'),
            'secret': os.getenv('NETMIKO_SECRET', 'admin'),
            'fast_cli': False,
        }

        try:
            net_connect = ConnectHandler(**device)

            # 1. 既存設定確認
            output = net_connect.send_command('show running-config | include hostname')
            assert output, "ホスト名情報が取得できない"
            print(f"\n[実機] 現在のホスト名: {output}")

            # 2. インターフェース一覧取得
            output = net_connect.send_command('show ip interface brief')
            assert 'Interface' in output or 'IP-Address' in output, "インターフェース情報が取得できない"
            print(f"\n[実機] インターフェース:\n{output}")

            # 3. OSPF 隣接状態確認
            output = net_connect.send_command('show ip ospf neighbor')
            print(f"\n[実機] OSPF 隣接:\n{output}")

            # 4. BGP ルート確認
            output = net_connect.send_command('show ip bgp summary')
            print(f"\n[実機] BGP サマリ:\n{output}")

            net_connect.disconnect()
            print("\n✅ 実機Catalyst接続テスト成功")

        except Exception as e:
            pytest.fail(f"実機接続失敗: {e}")


def run_standalone():
    """スタンドアロン実行モード"""
    print("=" * 70)
    print("🧪 Netmiko Catalyst テスト（エミュレーター版）")
    print("=" * 70)

    # サーバー起動
    print("\n[1] FastAPI サーバーを起動中...")
    proc = subprocess.Popen(
        [sys.executable, 'app.py'],
        cwd=os.path.join(os.path.dirname(__file__), '..'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    for i in range(120):  # 60秒待機
        try:
            urllib.request.urlopen(BASE + '/api/status', timeout=1)
            print("✅ サーバー起動完了")
            break
        except Exception:
            time.sleep(0.5)
            if (i + 1) % 20 == 0:
                print(f"  待機中... ({i//2}秒)")
            if i == 119:
                print("❌ サーバー起動失敗")
                proc.terminate()
                proc.wait()
                return False

    # デバイス登録
    print("\n[2] テスト用デバイスを登録中...")
    _post('/api/device', {'id': 'catalyst-test', 'type': 'catalyst', 'hostname': 'Cat-Test'})
    _post('/api/device', {'id': 'cisco-test', 'type': 'cisco', 'hostname': 'Cisco-Test'})
    print("✅ デバイス登録完了")

    try:
        # テスト実行
        print("\n[3] Catalyst テストを実行中...")

        # Test 1: インターフェース設定
        print("\n  📍 Test 1: インターフェース設定投入・確認")
        _cli('catalyst-test', 'conf t')
        _cli('catalyst-test', 'interface Gi1/0/1')
        _cli('catalyst-test', 'ip address 10.0.1.1 255.255.255.0')
        _cli('catalyst-test', 'exit')
        out = _cli('catalyst-test', 'show running-config interface Gi1/0/1')
        if '10.0.1.1' in out:
            print("    ✅ インターフェース設定成功")
        else:
            print("    ❌ インターフェース設定失敗")

        # Test 2: OSPF設定
        print("\n  📍 Test 2: OSPF設定投入・確認")
        _cli('catalyst-test', 'conf t')
        _cli('catalyst-test', 'router ospf 1')
        _cli('catalyst-test', 'network 10.0.1.0 0.0.0.255 area 0')
        _cli('catalyst-test', 'exit')
        out = _cli('catalyst-test', 'show ip ospf')
        if '1' in out:
            print("    ✅ OSPF設定成功")
        else:
            print("    ❌ OSPF設定失敗")

        # Test 3: BGP設定
        print("\n  📍 Test 3: BGP設定投入・確認")
        _cli('catalyst-test', 'conf t')
        _cli('catalyst-test', 'router bgp 65001')
        _cli('catalyst-test', 'neighbor 192.168.1.1 remote-as 65002')
        _cli('catalyst-test', 'address-family ipv4')
        _cli('catalyst-test', 'network 172.16.1.0 mask 255.255.255.0')
        _cli('catalyst-test', 'exit-address-family')
        _cli('catalyst-test', 'exit')
        out = _cli('catalyst-test', 'show ip bgp summary')
        if '65001' in out:
            print("    ✅ BGP設定成功")
        else:
            print("    ❌ BGP設定失敗")

        # Test 4: VLAN設定
        print("\n  📍 Test 4: VLAN設定投入・確認")
        _cli('catalyst-test', 'conf t')
        _cli('catalyst-test', 'vlan 100')
        _cli('catalyst-test', 'name TEST-VLAN')
        _cli('catalyst-test', 'exit')
        out = _cli('catalyst-test', 'show vlan id 100')
        if '100' in out:
            print("    ✅ VLAN設定成功")
        else:
            print("    ❌ VLAN設定失敗")

        # Test 5: Cisco ISR設定
        print("\n  📍 Test 5: Cisco ISR ルーター設定")
        _cli('cisco-test', 'conf t')
        _cli('cisco-test', 'interface GigabitEthernet0/0/1')
        _cli('cisco-test', 'ip address 10.1.1.1 255.255.255.0')
        _cli('cisco-test', 'no shutdown')
        _cli('cisco-test', 'exit')
        out = _cli('cisco-test', 'show ip interface brief')
        if 'GigabitEthernet' in out or 'Gi0/0' in out:
            print("    ✅ Cisco ルーター設定成功")
        else:
            print("    ❌ Cisco ルーター設定失敗")

        print("\n" + "=" * 70)
        print("✅ テスト完了")
        print("=" * 70)
        return True

    finally:
        proc.terminate()
        proc.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        success = run_standalone()
        sys.exit(0 if success else 1)
    else:
        pytest.main([__file__, '-v', '-s'])
