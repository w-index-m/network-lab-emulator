#!/usr/bin/env python3
"""
Netmiko を使用した実機/EVE-NG 環境での Catalyst・Cisco ルーター テストツール

このツールは、EVE-NG または実機環境で Catalyst・Cisco ISR へ netmiko 経由で
以下の操作を実行します：

1. インターフェース設定・確認
2. OSPF設定・隣接確認
3. BGP設定・ネイバー確認
4. VLAN設定確認
5. ACL設定確認
6. 設定保存

使い方:
  # Catalyst への接続テスト
  python tools/test_netmiko_integration.py \\
    --host 192.168.1.100 \\
    --username admin \\
    --password admin \\
    --device-type catalyst

  # Cisco ISR ルーターへのテスト
  python tools/test_netmiko_integration.py \\
    --host 192.168.1.200 \\
    --username admin \\
    --password admin \\
    --device-type cisco \\
    --test-mode router

環境変数での設定（推奨）:
  export CATALYST_HOST=192.168.1.100
  export CATALYST_USER=admin
  export CATALYST_PASS=admin
  python tools/test_netmiko_integration.py --auto-env
"""

import argparse
import os
import sys

try:
    from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException
    HAS_NETMIKO = True
except ImportError:
    HAS_NETMIKO = False
    print("⚠️  netmiko がインストールされていません")
    print("   pip install netmiko")


class CatalystTester:
    """Catalyst スイッチテストクラス"""

    def __init__(self, device_dict):
        self.device = device_dict
        self.conn = None
        self.results = []

    def connect(self):
        """デバイスへ接続"""
        try:
            print(f"\n📡 デバイスへ接続中... {self.device['host']}")
            self.conn = ConnectHandler(**self.device)
            print(f"✅ 接続成功: {self.device['host']}")
            return True
        except NetmikoAuthenticationException as e:
            print(f"❌ 認証失敗: {e}")
            return False
        except NetmikoTimeoutException as e:
            print(f"❌ 接続タイムアウト: {e}")
            return False
        except Exception as e:
            print(f"❌ 接続エラー: {e}")
            return False

    def disconnect(self):
        """デバイスから切断"""
        if self.conn:
            self.conn.disconnect()
            print("\n🔌 切断完了")

    def send_config(self, commands):
        """複数のコンフィグコマンドを送信"""
        if isinstance(commands, str):
            commands = [commands]
        try:
            output = self.conn.send_config_set(commands)
            return output
        except Exception as e:
            print(f"❌ コンフィグ投入エラー: {e}")
            return None

    def send_command(self, command):
        """単一コマンド送信"""
        try:
            output = self.conn.send_command(command)
            return output
        except Exception as e:
            print(f"❌ コマンド実行エラー: {e}")
            return None

    def test_interface_config(self):
        """Test 1: インターフェース設定投入・確認"""
        print("\n" + "="*70)
        print("📍 Test 1: インターフェース設定投入・確認")
        print("="*70)

        try:
            # インターフェース設定投入
            commands = [
                'interface GigabitEthernet1/0/1',
                'ip address 10.100.1.1 255.255.255.0',
                'description Test-Interface',
                'no shutdown',
            ]
            output = self.send_config(commands)
            if output:
                print("✅ インターフェース設定投入成功")
                print(f"   出力: {output[:100]}...")

            # 設定確認
            output = self.send_command('show running-config interface GigabitEthernet1/0/1')
            if output and '10.100.1.1' in output:
                print("✅ 設定確認成功 - IP アドレスが設定されている")
                self.results.append(('Interface Config', True))
            else:
                print("❌ 設定確認失敗 - IP アドレスが見つからない")
                self.results.append(('Interface Config', False))

            # インターフェース状態確認
            output = self.send_command('show interfaces GigabitEthernet1/0/1')
            if output:
                print("✅ インターフェース状態取得成功")
                if 'up' in output.lower() or 'admin' in output.lower():
                    print("   状態情報を取得")

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('Interface Config', False))

    def test_ospf_config(self):
        """Test 2: OSPF設定・隣接確認"""
        print("\n" + "="*70)
        print("📍 Test 2: OSPF設定・隣接確認")
        print("="*70)

        try:
            # OSPF プロセス設定
            commands = [
                'router ospf 1',
                'network 10.0.0.0 0.255.255.255 area 0',
                'exit',
            ]
            output = self.send_config(commands)
            if output:
                print("✅ OSPF設定投入成功")

            # OSPF プロセス確認
            output = self.send_command('show ip ospf')
            if output and 'Routing Process' in output:
                print("✅ OSPF プロセス稼働確認")
                self.results.append(('OSPF Config', True))
            else:
                print("❌ OSPF プロセスが稼働していない可能性")
                self.results.append(('OSPF Config', False))

            # 隣接状態確認
            output = self.send_command('show ip ospf neighbor')
            if output:
                print("✅ OSPF 隣接情報取得")
                neighbor_count = output.count('FULL') + output.count('Full')
                print(f"   Full 隣接数: {neighbor_count}")

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('OSPF Config', False))

    def test_bgp_config(self):
        """Test 3: BGP設定・ネイバー確認"""
        print("\n" + "="*70)
        print("📍 Test 3: BGP設定・ネイバー確認")
        print("="*70)

        try:
            # BGP プロセス設定
            commands = [
                'router bgp 65001',
                'address-family ipv4',
                'neighbor 192.168.1.2 remote-as 65002',
                'network 172.16.1.0 mask 255.255.255.0',
                'exit-address-family',
                'exit',
            ]
            output = self.send_config(commands)
            if output:
                print("✅ BGP設定投入成功")

            # BGP サマリ確認
            output = self.send_command('show ip bgp summary')
            if output and '65001' in output:
                print("✅ BGP プロセス稼働確認")
                self.results.append(('BGP Config', True))
            else:
                print("❌ BGP プロセスが稼働していない可能性")
                self.results.append(('BGP Config', False))

            # ネイバー確認
            if 'Neighbor' in output or '192.168' in output:
                print("✅ BGP ネイバー情報取得")

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('BGP Config', False))

    def test_vlan_config(self):
        """Test 4: VLAN設定確認"""
        print("\n" + "="*70)
        print("📍 Test 4: VLAN設定確認")
        print("="*70)

        try:
            # VLAN 設定投入
            commands = [
                'vlan 200',
                'name TEST-VLAN-200',
                'exit',
            ]
            output = self.send_config(commands)
            if output:
                print("✅ VLAN 設定投入成功")

            # VLAN 確認
            output = self.send_command('show vlan id 200')
            if output and '200' in output:
                print("✅ VLAN 設定確認成功")
                self.results.append(('VLAN Config', True))
            else:
                print("❌ VLAN 設定が見つからない")
                self.results.append(('VLAN Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('VLAN Config', False))

    def test_acl_config(self):
        """Test 5: ACL設定確認"""
        print("\n" + "="*70)
        print("📍 Test 5: ACL設定確認")
        print("="*70)

        try:
            # ACL 設定投入
            commands = [
                'ip access-list extended TEST_ACL',
                'permit ip 10.0.0.0 0.0.0.255 any',
                'permit tcp any any eq 22',
                'deny ip any any',
                'exit',
            ]
            output = self.send_config(commands)
            if output:
                print("✅ ACL 設定投入成功")

            # ACL 確認
            output = self.send_command('show access-lists TEST_ACL')
            if output and ('permit' in output.lower() or 'TEST_ACL' in output):
                print("✅ ACL 設定確認成功")
                self.results.append(('ACL Config', True))
            else:
                print("❌ ACL が見つからない")
                self.results.append(('ACL Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('ACL Config', False))

    def test_get_state(self):
        """Test 6: デバイス状態取得"""
        print("\n" + "="*70)
        print("📍 Test 6: デバイス状態取得")
        print("="*70)

        try:
            # ホスト名確認
            output = self.send_command('show running-config | include hostname')
            if output:
                print(f"✅ ホスト名: {output.strip()}")

            # インターフェース一覧
            output = self.send_command('show ip interface brief')
            if output:
                lines = output.split('\n')
                up_count = sum(1 for line in lines if 'up' in line.lower() and 'up' in line.lower())
                print(f"✅ インターフェース情報取得（Up状態: 推定{up_count}個）")
                self.results.append(('Device State', True))
            else:
                self.results.append(('Device State', False))

            # ルーティングテーブル
            output = self.send_command('show ip route')
            if output:
                route_count = output.count('O ') + output.count('B ') + output.count('S ')
                print(f"✅ ルーティングテーブル取得（学習経路: 推定{route_count}個）")

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('Device State', False))

    def run_all_tests(self):
        """全テスト実行"""
        if not self.connect():
            return False

        try:
            self.test_interface_config()
            self.test_ospf_config()
            self.test_bgp_config()
            self.test_vlan_config()
            self.test_acl_config()
            self.test_get_state()
        finally:
            self.disconnect()

        return True

    def report(self):
        """テスト結果レポート"""
        print("\n" + "="*70)
        print("📊 テスト結果レポート")
        print("="*70)
        passed = sum(1 for _, ok in self.results if ok)
        total = len(self.results)
        print(f"\n合計: {passed}/{total} テスト成功")
        for test_name, ok in self.results:
            icon = "✅" if ok else "❌"
            print(f"  {icon} {test_name}")
        print("\n" + "="*70)
        return passed == total


def main():
    parser = argparse.ArgumentParser(
        description='Netmiko を使用した Catalyst・Cisco ルーター 実機テスト',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 環境変数で設定
  export CATALYST_HOST=192.168.1.100
  export CATALYST_USER=admin
  export CATALYST_PASS=admin
  python tools/test_netmiko_integration.py --auto-env

  # コマンドラインで指定
  python tools/test_netmiko_integration.py \\
    --host 192.168.1.100 --username admin --password admin
        """
    )
    parser.add_argument('--host', help='接続先ホスト/IP')
    parser.add_argument('--username', default='admin', help='ユーザー名（デフォルト: admin）')
    parser.add_argument('--password', default='admin', help='パスワード（デフォルト: admin）')
    parser.add_argument('--device-type', default='cisco_ios', help='Netmiko device type（デフォルト: cisco_ios）')
    parser.add_argument('--secret', help='Enable パスワード（オプション）')
    parser.add_argument('--auto-env', action='store_true', help='環境変数から自動設定')
    parser.add_argument('--port', type=int, default=22, help='SSH ポート（デフォルト: 22）')
    parser.add_argument('--timeout', type=int, default=30, help='接続タイムアウト秒数')

    args = parser.parse_args()

    if not HAS_NETMIKO:
        print("❌ netmiko がインストールされていません")
        print("   pip install netmiko")
        return 1

    # 環境変数から自動設定
    if args.auto_env:
        host = os.getenv('CATALYST_HOST') or os.getenv('CISCO_HOST')
        if not host:
            print("❌ 環境変数 CATALYST_HOST または CISCO_HOST が設定されていません")
            return 1
        args.host = host
        args.username = os.getenv('CATALYST_USER', 'admin')
        args.password = os.getenv('CATALYST_PASS', 'admin')
        args.secret = os.getenv('CATALYST_SECRET')

    if not args.host:
        parser.print_help()
        print("\n❌ --host または --auto-env の指定が必要です")
        return 1

    # Netmiko デバイス辞書作成
    device = {
        'device_type': args.device_type,
        'host': args.host,
        'username': args.username,
        'password': args.password,
        'timeout': args.timeout,
        'port': args.port,
        'fast_cli': False,
    }
    if args.secret:
        device['secret'] = args.secret

    print("\n" + "="*70)
    print("🧪 Netmiko 実機テストツール")
    print("="*70)
    print(f"\n接続先: {args.host}")
    print(f"Device Type: {args.device_type}")
    print(f"User: {args.username}")

    tester = CatalystTester(device)
    if tester.run_all_tests():
        success = tester.report()
        return 0 if success else 1
    else:
        return 1


if __name__ == '__main__':
    sys.exit(main())
