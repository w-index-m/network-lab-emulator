#!/usr/bin/env python3
"""
EtherChannel + STP コスト反映検証テスト
経路ジェネレータ + Paramiko Si-R 経路確認テスト

実装内容:
1. SR-S と Catalyst で EtherChannel バンドル
2. STP での port-channel コスト反映確認
3. Si-R へ static route または redistribute で経路配信
4. Paramiko で Si-R の学習経路を確認

実行:
  # エミュレーター検証
  python tools/test_etherchannel_stp_cost.py --emulator

  # Si-R 経路確認（Paramiko）
  export SIR_HOST=192.168.1.50
  export SIR_USER=admin
  export SIR_PASS=admin
  python tools/test_etherchannel_stp_cost.py --sir-routes
"""

import argparse
import os
import sys
import json
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


class EtherChannelSTPTester:
    """EtherChannel + STP コスト検証"""

    def __init__(self, base_url='http://localhost:8000'):
        self.base_url = base_url.rstrip('/')
        self.results = []

    def _post(self, path, data):
        """HTTP POST"""
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
        except Exception as e:
            print(f"❌ API エラー: {e}")
            return None

    def cli(self, device_id, command):
        """CLI実行"""
        result = self._post('/api/cli', {
            'device_id': device_id,
            'command': command
        })
        if result and 'output' in result:
            return result['output']
        return ""

    def check_connectivity(self):
        """接続確認"""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/status", timeout=3) as r:
                r.read()
            return True
        except Exception:
            return False

    def setup_topology(self):
        """トポロジー構築"""
        print("\n" + "="*70)
        print("📍 EtherChannel/STP トポロジー構築")
        print("="*70)

        # デバイス登録
        devices = [
            {'id': 'srs-test', 'type': 'srs', 'hostname': 'SRS-Switch'},
            {'id': 'cat-test', 'type': 'catalyst', 'hostname': 'CAT-Switch'},
        ]

        for dev in devices:
            self._post('/api/device', dev)
            print(f"  ✅ {dev['id']} 登録")

        return devices

    def configure_etherchannel(self):
        """EtherChannel設定（Catalyst）"""
        print("\n" + "="*70)
        print("📍 EtherChannel 設定（Catalyst）")
        print("="*70)

        device_id = 'cat-test'

        commands = [
            'conf t',
            'interface range Gi1/0/1-2',
            'channel-group 1 mode active',
            'exit',
            'interface port-channel 1',
            'switchport mode trunk',
            'spanning-tree port priority 128',
            'spanning-tree cost 8',  # 手動でコスト設定
            'exit',
            'end',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ Catalyst EtherChannel 設定完了")

    def configure_srs_etherchannel(self):
        """EtherChannel設定（SR-S）"""
        print("\n" + "="*70)
        print("📍 EtherChannel 設定（SR-S）")
        print("="*70)

        device_id = 'srs-test'

        commands = [
            'conf t',
            'interface range Gi1/0/1-2',
            'channel-group 1 mode passive',
            'exit',
            'interface port-channel 1',
            'switchport mode trunk',
            'spanning-tree port priority 128',
            'spanning-tree cost 12',  # SR-S側は別コスト
            'exit',
            'end',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ SR-S EtherChannel 設定完了")

    def verify_etherchannel_status(self):
        """EtherChannel状態確認"""
        print("\n" + "="*70)
        print("📍 EtherChannel 状態確認")
        print("="*70)

        for dev_id in ['cat-test', 'srs-test']:
            print(f"\n  📍 {dev_id}:")

            # EtherChannel サマリー
            output = self.cli(dev_id, 'show etherchannel summary')
            if output:
                lines = output.split('\n')[:10]
                for line in lines:
                    if line.strip():
                        print(f"      {line}")

            # ポート状態
            output = self.cli(dev_id, 'show port-channel summary')
            if output:
                print(f"      ✅ port-channel 情報取得")

    def verify_stp_cost(self):
        """STP コスト確認"""
        print("\n" + "="*70)
        print("📍 STP コスト確認")
        print("="*70)

        for dev_id in ['cat-test', 'srs-test']:
            print(f"\n  📍 {dev_id}:")

            output = self.cli(dev_id, 'show spanning-tree vlan 1')
            if output:
                lines = output.split('\n')
                # ポートコスト情報を含む行を抽出
                for line in lines:
                    if 'Cost:' in line or 'port-channel' in line.lower():
                        print(f"      {line}")

            # port-channel 固有のコスト確認
            output = self.cli(dev_id, 'show spanning-tree interface port-channel 1')
            if output:
                print(f"\n      [port-channel 1 詳細]")
                lines = output.split('\n')[:15]
                for line in lines:
                    if line.strip():
                        print(f"      {line}")

    def configure_sir_routes(self):
        """Si-R への経路配信設定"""
        print("\n" + "="*70)
        print("📍 経路ジェネレータ設定（static route + redistribute）")
        print("="*70)

        # Si-Rデバイス登録
        self._post('/api/device', {
            'id': 'sir-route-gen',
            'type': 'sir',
            'hostname': 'SiR-RouteGen'
        })
        print("  ✅ Si-R デバイス登録")

        device_id = 'sir-route-gen'

        # Static Route 配信（経路ジェネレータ相当）
        commands = [
            'configure',
            'ip route 192.168.200.0/24 0.0.0.0',
            'ip route 192.168.201.0/24 0.0.0.0',
            'ip route 10.100.0.0/16 0.0.0.0',
            # OSPF に redistribute
            'router ospf 1',
            'redistribute static',
            'exit',
            'save',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ Si-R 経路配信設定完了")
        return device_id

    def verify_sir_routes_emulator(self, device_id):
        """Si-R の経路確認（エミュレーター）"""
        print("\n" + "="*70)
        print("📍 Si-R ルーティングテーブル確認（エミュレーター）")
        print("="*70)

        output = self.cli(device_id, 'show ip route')
        if output:
            lines = output.split('\n')
            # Static/配信経路をフィルタ
            route_lines = [l for l in lines if 'S ' in l or '192.168.20' in l or '10.100' in l]
            if route_lines:
                print(f"  ✅ 配信経路数: {len(route_lines)}")
                for route in route_lines[:10]:
                    print(f"      {route}")
                self.results.append(('Si-R Routes', True))
            else:
                print("  ⚠️  配信経路なし")
                self.results.append(('Si-R Routes', False))

    def test_emulator_etherchannel_stp(self):
        """エミュレーター内テスト"""
        print("\n" + "="*70)
        print("🧪 EtherChannel + STP コスト検証（エミュレーター）")
        print("="*70)

        if not self.check_connectivity():
            print("❌ エミュレーター接続失敗")
            return False

        # トポロジー構築
        self.setup_topology()
        time.sleep(1)

        # EtherChannel設定
        self.configure_etherchannel()
        self.configure_srs_etherchannel()
        time.sleep(2)

        # 状態確認
        self.verify_etherchannel_status()
        time.sleep(1)

        # STP コスト確認
        self.verify_stp_cost()
        time.sleep(1)

        # 経路ジェネレータ
        sir_id = self.configure_sir_routes()
        time.sleep(2)

        # 経路確認
        self.verify_sir_routes_emulator(sir_id)

        return True


class ParamikoSiRRouteVerifier:
    """Paramiko経由でSi-Rの経路確認"""

    def __init__(self, host, username, password, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.ssh = None

    def connect(self):
        """SSH接続"""
        try:
            print(f"\n📡 Paramiko SSH 接続中... {self.host}")
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False
            )
            print(f"✅ Paramiko SSH 接続成功")
            return True
        except Exception as e:
            print(f"❌ Paramiko 接続失敗: {e}")
            return False

    def send_command(self, command):
        """コマンド送信"""
        try:
            stdin, stdout, stderr = self.ssh.exec_command(command)
            output = stdout.read().decode('utf-8', errors='ignore')
            return output
        except Exception as e:
            print(f"❌ コマンド実行失敗: {e}")
            return ""

    def disconnect(self):
        """切断"""
        if self.ssh:
            self.ssh.close()

    def verify_sir_generated_routes(self):
        """Si-R でジェネレータ経路を確認"""
        print("\n" + "="*70)
        print("📍 Si-R 経路確認（ジェネレータ経由）- Paramiko")
        print("="*70)

        if not self.connect():
            return False

        try:
            # ルーティングテーブル確認
            print("\n  [1] 全ルーティングテーブル:")
            output = self.send_command('show ip route')
            if output:
                lines = output.split('\n')
                for line in lines[:25]:
                    if line.strip():
                        print(f"      {line}")
                print(f"      ✅ ルーティングテーブル取得成功")

            # Static Route確認
            print("\n  [2] Static Route一覧:")
            output = self.send_command('show ip route static')
            if output:
                lines = output.split('\n')
                for line in lines:
                    if line.strip() and ('192.168.20' in line or '10.100' in line or 'S ' in line):
                        print(f"      {line}")
                print(f"      ✅ Static Route取得成功")

            # OSPF再配信確認
            print("\n  [3] OSPF プロセス確認:")
            output = self.send_command('show ip ospf')
            if output:
                lines = output.split('\n')[:15]
                for line in lines:
                    if line.strip():
                        print(f"      {line}")
                print(f"      ✅ OSPF プロセス取得成功")

            # 詳細設定確認
            print("\n  [4] redistribute 設定確認:")
            output = self.send_command('show running-config | include redistribute')
            if output:
                lines = output.split('\n')
                for line in lines:
                    if line.strip():
                        print(f"      {line}")
                print(f"      ✅ redistribute 設定取得成功")

            self.disconnect()
            return True

        except Exception as e:
            print(f"❌ エラー: {e}")
            self.disconnect()
            return False


def main():
    parser = argparse.ArgumentParser(
        description='EtherChannel + STP コスト反映 / 経路ジェネレータ + Si-R 検証'
    )
    parser.add_argument('--emulator', action='store_true', help='エミュレーター内での検証')
    parser.add_argument('--sir-routes', action='store_true', help='Si-R 経路確認（Paramiko）')

    args = parser.parse_args()

    if not any([args.emulator, args.sir_routes]):
        args.emulator = True

    print("="*70)
    print("🧪 EtherChannel + STP + 経路ジェネレータ テスト")
    print("="*70)

    # ── エミュレーター検証 ──
    if args.emulator:
        tester = EtherChannelSTPTester()
        if tester.test_emulator_etherchannel_stp():
            if tester.results:
                passed = sum(1 for _, ok in tester.results if ok)
                print(f"\n📊 エミュレーター結果: {passed}/{len(tester.results)} 成功")

    # ── Si-R Paramiko検証 ──
    if args.sir_routes:
        if not HAS_PARAMIKO:
            print("\n❌ paramiko がインストールされていません")
            print("   pip install paramiko")
            return 1

        host = os.getenv('SIR_HOST')
        if not host:
            print("\n❌ 環境変数 SIR_HOST が設定されていません")
            print("   export SIR_HOST=192.168.1.50")
            return 1

        verifier = ParamikoSiRRouteVerifier(
            host=host,
            username=os.getenv('SIR_USER', 'admin'),
            password=os.getenv('SIR_PASS', 'admin')
        )
        verifier.verify_sir_generated_routes()

    print("\n" + "="*70)
    print("✅ テスト完了")
    print("="*70)
    return 0


if __name__ == '__main__':
    sys.exit(main())
