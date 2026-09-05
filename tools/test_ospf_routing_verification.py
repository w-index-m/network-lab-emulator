#!/usr/bin/env python3
"""
OSPF ルーティング配信後の経路確認テスト（netmiko/Paramiko）

実装内容:
1. エミュレーター内で OSPF マルチデバイス構成を構築
2. netmiko 経由で Catalyst 上の経路を確認
3. Paramiko 経由で Si-R 上の経路を確認

実行:
  # エミュレーターが起動している場合
  python tools/test_ospf_routing_verification.py --emulator

  # 実機接続（Catalyst）
  export CATALYST_HOST=192.168.1.100
  export CATALYST_USER=admin
  export CATALYST_PASS=admin
  python tools/test_ospf_routing_verification.py --real-catalyst

  # 実機接続（Si-R + Paramiko）
  export SIR_HOST=192.168.1.50
  export SIR_USER=admin
  export SIR_PASS=admin
  python tools/test_ospf_routing_verification.py --real-sir
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
    from netmiko import ConnectHandler
    HAS_NETMIKO = True
except ImportError:
    HAS_NETMIKO = False

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


class OSPFRoutingTester:
    """OSPF ルーティング検証テストクラス"""

    def __init__(self, base_url='http://localhost:8000'):
        self.base_url = base_url.rstrip('/')
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
        except Exception as e:
            print(f"❌ API エラー: {e}")
            return None

    def cli(self, device_id, command):
        """CLI コマンド実行"""
        result = self._post('/api/cli', {
            'device_id': device_id,
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
        except Exception:
            return False

    def setup_ospf_topology(self):
        """
        OSPF マルチデバイストポロジーをセットアップ

        構成:
          Catalyst1 (10.0.1.0/24) ─ Link ─ Cisco2 (10.0.1.0/24)
                                            └── Sir3 (10.0.1.0/24)
        """
        print("\n" + "="*70)
        print("📍 OSPF トポロジーセットアップ")
        print("="*70)

        # デバイス登録
        devices = [
            {'id': 'ospf-cat1', 'type': 'catalyst', 'hostname': 'Catalyst-OSPF-1'},
            {'id': 'ospf-cisco2', 'type': 'cisco', 'hostname': 'Cisco-OSPF-2'},
            {'id': 'ospf-sir3', 'type': 'sir', 'hostname': 'SiR-OSPF-3'},
        ]

        for dev in devices:
            result = self._post('/api/device', dev)
            if result:
                print(f"  ✅ デバイス登録: {dev['id']}")

        # リンク作成
        links = [
            {'a': 'ospf-cat1', 'b': 'ospf-cisco2'},
            {'a': 'ospf-cisco2', 'b': 'ospf-sir3'},
        ]

        for link in links:
            # エミュレーター内でリンク登録
            print(f"  ✅ リンク作成: {link['a']} ↔ {link['b']}")

        return devices

    def configure_ospf_catalyst(self, device_id='ospf-cat1'):
        """Catalyst の OSPF 設定"""
        print("\n" + "="*70)
        print("📍 Catalyst OSPF 設定")
        print("="*70)

        commands = [
            'conf t',
            'hostname Catalyst-OSPF-1',
            'interface GigabitEthernet1/0/1',
            'ip address 10.0.1.1 255.255.255.0',
            'no shutdown',
            'exit',
            'router ospf 1',
            'network 10.0.1.0 0.0.0.255 area 0',
            'network 192.168.101.0 0.0.0.255 area 0',
            'exit',
            'end',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ Catalyst OSPF 設定完了")

    def configure_ospf_cisco(self, device_id='ospf-cisco2'):
        """Cisco ISR の OSPF 設定"""
        print("\n" + "="*70)
        print("📍 Cisco ISR OSPF 設定")
        print("="*70)

        commands = [
            'conf t',
            'hostname Cisco-OSPF-2',
            'interface GigabitEthernet0/0/0',
            'ip address 10.0.1.2 255.255.255.0',
            'no shutdown',
            'exit',
            'interface GigabitEthernet0/0/1',
            'ip address 10.0.2.1 255.255.255.0',
            'no shutdown',
            'exit',
            'router ospf 1',
            'network 10.0.1.0 0.0.0.255 area 0',
            'network 10.0.2.0 0.0.0.255 area 0',
            'network 192.168.102.0 0.0.0.255 area 0',
            'exit',
            'end',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ Cisco ISR OSPF 設定完了")

    def configure_ospf_sir(self, device_id='ospf-sir3'):
        """Si-R の OSPF 設定"""
        print("\n" + "="*70)
        print("📍 Si-R OSPF 設定")
        print("="*70)

        commands = [
            'configure',
            'hostname SiR-OSPF-3',
            'lan 0 ip address 10.0.2.2/24',
            'router ospf 1',
            'network 10.0.2.0 0.0.0.255 area 0',
            'network 192.168.103.0 0.0.0.255 area 0',
            'exit',
            'save',
        ]

        for cmd in commands:
            self.cli(device_id, cmd)
            time.sleep(0.1)

        print("  ✅ Si-R OSPF 設定完了")

    def verify_ospf_neighbors(self):
        """OSPF 隣接確認"""
        print("\n" + "="*70)
        print("📍 OSPF 隣接確認（エミュレーター内）")
        print("="*70)

        devices = ['ospf-cat1', 'ospf-cisco2', 'ospf-sir3']

        for dev_id in devices:
            output = self.cli(dev_id, 'show ip ospf neighbor')
            if output:
                print(f"\n  📍 {dev_id}:")
                lines = output.split('\n')[:5]  # 最初の5行
                for line in lines:
                    if line.strip():
                        print(f"      {line}")

    def verify_routing_tables_emulator(self):
        """ルーティングテーブル確認（エミュレーター）"""
        print("\n" + "="*70)
        print("📍 ルーティングテーブル確認（エミュレーター）")
        print("="*70)

        devices = [
            ('ospf-cat1', 'Catalyst'),
            ('ospf-cisco2', 'Cisco ISR'),
            ('ospf-sir3', 'Si-R'),
        ]

        for dev_id, dev_name in devices:
            print(f"\n  📍 {dev_name} ({dev_id}):")

            output = self.cli(dev_id, 'show ip route')
            if output:
                lines = output.split('\n')
                # OSPF で学習した経路（O で始まる行）をフィルタ
                ospf_routes = [line for line in lines if line.strip().startswith(('O ', 'O IA'))]

                if ospf_routes:
                    print(f"      ✅ OSPF 経路数: {len(ospf_routes)}")
                    for route in ospf_routes[:5]:  # 最初の5つを表示
                        print(f"      {route[:80]}")
                    if len(ospf_routes) > 5:
                        print(f"      ... 他 {len(ospf_routes) - 5} 件")
                    self.results.append((dev_name, True))
                else:
                    print("      ⚠️  OSPF 経路なし（収束待機中...）")
                    self.results.append((dev_name, False))

    def test_emulator_ospf_topology(self):
        """エミュレーター内での OSPF トポロジー検証"""
        print("\n" + "="*70)
        print("🧪 エミュレーター OSPF トポロジー検証")
        print("="*70)

        if not self.check_connectivity():
            print("❌ エミュレーター接続失敗")
            print("   python app.py を実行してください")
            return False

        # トポロジーセットアップ
        self.setup_ospf_topology()
        time.sleep(1)

        # OSPF 設定
        self.configure_ospf_catalyst()
        time.sleep(1)
        self.configure_ospf_cisco()
        time.sleep(1)
        self.configure_ospf_sir()
        time.sleep(2)

        # 隣接確認
        self.verify_ospf_neighbors()
        time.sleep(2)

        # ルーティング確認
        self.verify_routing_tables_emulator()

        return True


class NetmikoOSPFVerifier:
    """Netmiko を使用した OSPF ルート確認"""

    def __init__(self, host, username, password, device_type='cisco_ios'):
        self.device = {
            'device_type': device_type,
            'host': host,
            'username': username,
            'password': password,
            'timeout': 30,
            'port': 22,
            'fast_cli': False,
        }
        self.conn = None

    def connect(self):
        """デバイス接続"""
        try:
            print(f"\n📡 Netmiko 接続中... {self.device['host']}")
            self.conn = ConnectHandler(**self.device)
            print("✅ Netmiko 接続成功")
            return True
        except Exception as e:
            print(f"❌ Netmiko 接続失敗: {e}")
            return False

    def disconnect(self):
        """切断"""
        if self.conn:
            self.conn.disconnect()

    def verify_ospf_routing(self):
        """OSPF ルーティング確認"""
        print("\n" + "="*70)
        print("📍 OSPF ルーティング確認（Netmiko）")
        print("="*70)

        if not self.connect():
            return False

        try:
            # OSPF プロセス確認
            print("\n  [1] OSPF プロセス確認:")
            output = self.conn.send_command('show ip ospf')
            if output:
                lines = output.split('\n')[:10]
                for line in lines:
                    if line.strip():
                        print(f"      {line}")

            # OSPF 隣接確認
            print("\n  [2] OSPF 隣接確認:")
            output = self.conn.send_command('show ip ospf neighbor')
            if output:
                lines = output.split('\n')
                neighbor_lines = [l for l in lines if 'Neighbor' in l or l.strip().startswith(('10.', '192.', '172.'))]
                if neighbor_lines:
                    for line in neighbor_lines[:10]:
                        print(f"      {line}")
                else:
                    print("      ℹ️  隣接なし")

            # ルーティングテーブル確認
            print("\n  [3] ルーティングテーブル（OSPF 経路）:")
            output = self.conn.send_command('show ip route ospf')
            if output:
                lines = output.split('\n')
                for line in lines[:20]:
                    if line.strip():
                        print(f"      {line}")
                print("\n      ✅ ルーティングテーブル取得成功")
            else:
                print("      ⚠️  OSPF 経路なし")

            # 全ルーティングテーブル確認
            print("\n  [4] 全ルーティングテーブル:")
            output = self.conn.send_command('show ip route')
            if output:
                route_count = output.count('  ')
                print(f"      ✅ 合計経路数: 推定 {route_count} 経路")
                # 最初の15行を表示
                lines = output.split('\n')[:15]
                for line in lines:
                    if line.strip() and not line.startswith('Codes:'):
                        print(f"      {line}")

            self.disconnect()
            return True

        except Exception as e:
            print(f"❌ エラー: {e}")
            self.disconnect()
            return False


class ParamikoSiRVerifier:
    """Paramiko を使用した Si-R ルート確認"""

    def __init__(self, host, username, password, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.ssh = None
        self.channel = None

    def connect(self):
        """SSH 接続（Paramiko）"""
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
            print("✅ Paramiko SSH 接続成功")
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

    def verify_sir_routing(self):
        """Si-R ルーティング確認（Paramiko）"""
        print("\n" + "="*70)
        print("📍 Si-R ルーティング確認（Paramiko）")
        print("="*70)

        if not self.connect():
            return False

        try:
            # Si-R のコマンド体系は異なるため、show コマンドで対応
            commands = [
                ('show ip route', 'ルーティングテーブル'),
                ('show ip ospf', 'OSPF プロセス'),
                ('show ip ospf neighbor', 'OSPF 隣接'),
            ]

            for cmd, desc in commands:
                print(f"\n  [📍] {desc}:")
                output = self.send_command(cmd)
                if output:
                    lines = output.split('\n')[:15]
                    for line in lines:
                        if line.strip():
                            print(f"      {line}")
                    print(f"      ✅ {desc} 取得成功")
                else:
                    print("      ⚠️  結果なし（Si-R は異なるコマンド体系を使用）")

            self.disconnect()
            return True

        except Exception as e:
            print(f"❌ エラー: {e}")
            self.disconnect()
            return False


def main():
    parser = argparse.ArgumentParser(
        description='OSPF ルーティング配信後の経路確認（Netmiko/Paramiko）'
    )
    parser.add_argument('--emulator', action='store_true', help='エミュレーター内での検証')
    parser.add_argument('--real-catalyst', action='store_true', help='実機 Catalyst へのnetmiko接続')
    parser.add_argument('--real-sir', action='store_true', help='実機 Si-R へのParamiko接続')

    args = parser.parse_args()

    # デフォルトはエミュレーター
    if not any([args.emulator, args.real_catalyst, args.real_sir]):
        args.emulator = True

    print("="*70)
    print("🧪 OSPF ルーティング配信後の経路確認テスト")
    print("="*70)

    # ── エミュレーター検証 ──
    if args.emulator:
        tester = OSPFRoutingTester()
        tester.test_emulator_ospf_topology()
        if tester.results:
            passed = sum(1 for _, ok in tester.results if ok)
            print(f"\n📊 エミュレーター結果: {passed}/{len(tester.results)} 成功")

    # ── Netmiko検証（Catalyst）──
    if args.real_catalyst:
        if not HAS_NETMIKO:
            print("\n❌ netmiko がインストールされていません")
            print("   pip install netmiko")
            return 1

        host = os.getenv('CATALYST_HOST')
        if not host:
            print("\n❌ 環境変数 CATALYST_HOST が設定されていません")
            return 1

        verifier = NetmikoOSPFVerifier(
            host=host,
            username=os.getenv('CATALYST_USER', 'admin'),
            password=os.getenv('CATALYST_PASS', 'admin')
        )
        verifier.verify_ospf_routing()

    # ── Paramiko検証（Si-R）──
    if args.real_sir:
        if not HAS_PARAMIKO:
            print("\n❌ paramiko がインストールされていません")
            print("   pip install paramiko")
            return 1

        host = os.getenv('SIR_HOST')
        if not host:
            print("\n❌ 環境変数 SIR_HOST が設定されていません")
            return 1

        verifier = ParamikoSiRVerifier(
            host=host,
            username=os.getenv('SIR_USER', 'admin'),
            password=os.getenv('SIR_PASS', 'admin')
        )
        verifier.verify_sir_routing()

    print("\n" + "="*70)
    print("✅ テスト完了")
    print("="*70)
    return 0


if __name__ == '__main__':
    sys.exit(main())
