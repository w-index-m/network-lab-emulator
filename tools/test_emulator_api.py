#!/usr/bin/env python3
"""
エミュレーター HTTP API を使用した Catalyst 設定変更・状態確認テスト

このツールは、Network Lab Emulator の FastAPI サーバーに対して
HTTP 経由で CLI コマンドを送信し、設定変更と状態確認を行います。

実行:
  # エミュレーターサーバー起動（別ターミナル）
  python app.py

  # テスト実行
  python tools/test_emulator_api.py --host localhost --port 8000
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import time


class EmulatorAPITester:
    """エミュレーター HTTP API テストクラス"""

    def __init__(self, base_url, device_id):
        self.base_url = base_url.rstrip('/')
        self.device_id = device_id
        self.results = []

    def _post(self, path, data):
        """HTTP POST リクエスト"""
        try:
            url = f"{self.base_url}{path}"
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            print(f"❌ HTTP エラー {e.code}: {e.reason}")
            return None
        except Exception as e:
            print(f"❌ リクエスト失敗: {e}")
            return None

    def cli_command(self, command):
        """CLI コマンド実行"""
        result = self._post('/api/cli', {
            'device_id': self.device_id,
            'command': command
        })
        if result and 'output' in result:
            return result['output']
        return ""

    def check_connectivity(self):
        """エミュレーター接続確認"""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/status", timeout=3) as r:
                r.read()
            return True
        except Exception as e:
            print(f"❌ エミュレーター接続失敗: {e}")
            return False

    def test_interface_config(self):
        """Test 1: インターフェース設定"""
        print("\n" + "="*70)
        print("📍 Test 1: インターフェース設定投入・確認")
        print("="*70)

        try:
            # 設定投入
            self.cli_command('conf t')
            self.cli_command('interface GigabitEthernet1/0/1')
            self.cli_command('ip address 10.100.1.1 255.255.255.0')
            self.cli_command('description Test-Interface')
            self.cli_command('no shutdown')
            self.cli_command('exit')

            # 設定確認
            output = self.cli_command('show running-config interface GigabitEthernet1/0/1')
            if output and '10.100.1.1' in output:
                print("✅ インターフェース設定成功")
                print(f"   IP: 10.100.1.1/24")
                self.results.append(('Interface Config', True))
            else:
                print("❌ インターフェース設定失敗")
                self.results.append(('Interface Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('Interface Config', False))

    def test_ospf_config(self):
        """Test 2: OSPF設定"""
        print("\n" + "="*70)
        print("📍 Test 2: OSPF設定投入・確認")
        print("="*70)

        try:
            # OSPF 設定投入
            self.cli_command('conf t')
            self.cli_command('router ospf 1')
            self.cli_command('network 10.0.0.0 0.255.255.255 area 0')
            self.cli_command('exit')

            # OSPF 確認
            output = self.cli_command('show ip ospf')
            if output and '1' in output:
                print("✅ OSPF設定成功")
                print(f"   プロセス ID: 1")
                self.results.append(('OSPF Config', True))
            else:
                print("❌ OSPF設定失敗")
                self.results.append(('OSPF Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('OSPF Config', False))

    def test_bgp_config(self):
        """Test 3: BGP設定"""
        print("\n" + "="*70)
        print("📍 Test 3: BGP設定投入・確認")
        print("="*70)

        try:
            # BGP 設定投入
            self.cli_command('conf t')
            self.cli_command('router bgp 65001')
            self.cli_command('address-family ipv4')
            self.cli_command('neighbor 192.168.1.2 remote-as 65002')
            self.cli_command('network 172.16.1.0 mask 255.255.255.0')
            self.cli_command('exit-address-family')
            self.cli_command('exit')

            # BGP 確認
            output = self.cli_command('show ip bgp summary')
            if output and '65001' in output:
                print("✅ BGP設定成功")
                print(f"   AS: 65001")
                self.results.append(('BGP Config', True))
            else:
                print("❌ BGP設定失敗")
                self.results.append(('BGP Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('BGP Config', False))

    def test_vlan_config(self):
        """Test 4: VLAN設定"""
        print("\n" + "="*70)
        print("📍 Test 4: VLAN設定投入・確認")
        print("="*70)

        try:
            # VLAN 設定投入
            self.cli_command('conf t')
            self.cli_command('vlan 100')
            self.cli_command('name TEST-VLAN')
            self.cli_command('exit')

            # VLAN 確認
            output = self.cli_command('show vlan id 100')
            if output and '100' in output:
                print("✅ VLAN設定成功")
                print(f"   VLAN ID: 100")
                self.results.append(('VLAN Config', True))
            else:
                print("❌ VLAN設定失敗")
                self.results.append(('VLAN Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('VLAN Config', False))

    def test_acl_config(self):
        """Test 5: ACL設定"""
        print("\n" + "="*70)
        print("📍 Test 5: ACL設定投入・確認")
        print("="*70)

        try:
            # ACL 設定投入
            self.cli_command('conf t')
            self.cli_command('ip access-list extended TEST_ACL')
            self.cli_command('permit ip 10.0.0.0 0.0.0.255 any')
            self.cli_command('deny ip any any')
            self.cli_command('exit')

            # ACL 確認
            output = self.cli_command('show access-lists TEST_ACL')
            if output and ('permit' in output.lower() or 'TEST_ACL' in output):
                print("✅ ACL設定成功")
                print(f"   ACL: TEST_ACL")
                self.results.append(('ACL Config', True))
            else:
                print("❌ ACL設定失敗")
                self.results.append(('ACL Config', False))

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('ACL Config', False))

    def test_device_state(self):
        """Test 6: デバイス状態確認"""
        print("\n" + "="*70)
        print("📍 Test 6: デバイス状態確認")
        print("="*70)

        try:
            # ホスト名
            output = self.cli_command('show running-config | include hostname')
            if output:
                print(f"✅ ホスト名取得: {output.strip()}")

            # インターフェース一覧
            output = self.cli_command('show interfaces status')
            if output:
                print(f"✅ インターフェース情報取得")
                self.results.append(('Device State', True))
            else:
                self.results.append(('Device State', False))

            # ルーティングテーブル
            output = self.cli_command('show ip route')
            if output:
                print(f"✅ ルーティングテーブル取得")

        except Exception as e:
            print(f"❌ テスト失敗: {e}")
            self.results.append(('Device State', False))

    def run_all_tests(self):
        """全テスト実行"""
        self.test_interface_config()
        self.test_ospf_config()
        self.test_bgp_config()
        self.test_vlan_config()
        self.test_acl_config()
        self.test_device_state()

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
        description='Network Lab Emulator HTTP API テスト'
    )
    parser.add_argument('--host', default='localhost', help='エミュレーターホスト')
    parser.add_argument('--port', type=int, default=8000, help='エミュレーターポート')
    parser.add_argument('--device', default='catalyst', help='テスト対象デバイスID')

    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"

    print("\n" + "="*70)
    print("🧪 Network Lab Emulator - Catalyst テスト")
    print("="*70)
    print(f"\nサーバー: {base_url}")
    print(f"デバイス: {args.device}")

    tester = EmulatorAPITester(base_url, args.device)

    # 接続確認
    print("\n⏳ エミュレーター接続確認中...")
    if not tester.check_connectivity():
        print("\n❌ エミュレーターに接続できません")
        print("\n以下のコマンドでサーバーを起動してください:")
        print(f"  python app.py")
        return 1

    print("✅ エミュレーター接続成功\n")

    # テスト実行
    try:
        tester.run_all_tests()
        success = tester.report()
        return 0 if success else 1
    except KeyboardInterrupt:
        print("\n\n⚠️  テスト中断")
        return 1
    except Exception as e:
        print(f"\n\n❌ テスト失敗: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
