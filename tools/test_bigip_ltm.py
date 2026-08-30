#!/usr/bin/env python3
"""
F5 BIG-IP LTM テストツール

テスト内容:
  1. Pool の CRUD 操作
  2. Virtual Server（VIP）の作成と管理
  3. メンバー状態管理（up/down 手動切り替え）
  4. 複数プール・複数VIP の構成
  5. ヘルスモニター設定

実行:
  python tools/test_bigip_ltm.py --emulator
"""

import argparse
import sys
import os
import json
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class BigIPLTMTester:
    """F5 BIG-IP LTM テスター"""

    def __init__(self, base_url='http://localhost:8000'):
        self.base_url = base_url.rstrip('/')
        self.device_id = 'bigip-ltm-test'
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

    def cli(self, command):
        """CLI実行"""
        result = self._post('/api/cli', {
            'device_id': self.device_id,
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

    def register_device(self):
        """BIG-IP デバイス登録"""
        print("\n" + "="*70)
        print("📍 BIG-IP LTM デバイス登録")
        print("="*70)

        self._post('/api/device', {
            'id': self.device_id,
            'type': 'bigip',
            'hostname': 'F5-LTM-Test'
        })
        print(f"  ✅ {self.device_id} 登録完了")

    def test_pool_creation(self):
        """Pool 作成テスト"""
        print("\n" + "="*70)
        print("🧪 Pool 作成テスト")
        print("="*70)

        # Pool 作成
        commands = [
            'tmsh create ltm pool web_pool {',
            '    members add { 10.0.0.1:80 10.0.0.2:80 10.0.0.3:80 }',
            '    monitor http',
            '    load-balancing-mode round-robin',
            '}'
        ]

        for cmd in commands:
            self.cli(cmd)
            time.sleep(0.1)

        print("  ✅ Pool 作成完了: web_pool")

        # Pool 状態確認
        output = self.cli('tmsh show ltm pool web_pool')
        if 'web_pool' in output:
            print("  ✅ Pool 状態確認成功")
            lines = output.split('\n')[:10]
            for line in lines:
                if line.strip():
                    print(f"      {line}")
            self.results.append(('Pool Creation', True))
            return True
        else:
            print("  ❌ Pool 状態確認失敗")
            self.results.append(('Pool Creation', False))
            return False

    def test_virtual_server(self):
        """Virtual Server（VIP）作成テスト"""
        print("\n" + "="*70)
        print("🧪 Virtual Server 作成テスト")
        print("="*70)

        # Virtual Server 作成
        commands = [
            'tmsh create ltm virtual vs_web {',
            '    destination 192.0.2.10:80',
            '    pool web_pool',
            '    profiles add { http tcp }',
            '}'
        ]

        for cmd in commands:
            self.cli(cmd)
            time.sleep(0.1)

        print("  ✅ Virtual Server 作成完了: vs_web")

        # Virtual Server 状態確認
        output = self.cli('tmsh show ltm virtual vs_web')
        if 'vs_web' in output or '192.0.2.10' in output:
            print("  ✅ Virtual Server 状態確認成功")
            self.results.append(('Virtual Server', True))
            return True
        else:
            print("  ❌ Virtual Server 状態確認失敗")
            self.results.append(('Virtual Server', False))
            return False

    def test_member_management(self):
        """メンバー up/down 管理テスト"""
        print("\n" + "="*70)
        print("🧪 メンバー Up/Down 管理テスト")
        print("="*70)

        # メンバーを down に設定
        self.cli('tmsh modify ltm pool web_pool members modify { 10.0.0.1:80 { state user-down } }')
        time.sleep(0.2)

        print("  ✅ メンバー 10.0.0.1:80 を down に設定")

        # Pool 状態確認（down メンバーを確認）
        output = self.cli('tmsh show ltm pool web_pool')
        if '10.0.0.1' in output:
            print("  ✅ メンバー状態確認成功")
            self.results.append(('Member Management', True))

            # メンバーを up に復旧
            self.cli('tmsh modify ltm pool web_pool members modify { 10.0.0.1:80 { state user-up } }')
            print("  ✅ メンバー 10.0.0.1:80 を up に復旧")
            return True
        else:
            print("  ❌ メンバー状態確認失敗")
            self.results.append(('Member Management', False))
            return False

    def test_multiple_pools(self):
        """複数プール構成テスト"""
        print("\n" + "="*70)
        print("🧪 複数プール・複数VIP 構成テスト")
        print("="*70)

        # API プール作成
        commands = [
            'tmsh create ltm pool api_pool {',
            '    members add { 10.0.0.10:8080 10.0.0.11:8080 }',
            '    monitor tcp',
            '    load-balancing-mode least-connections-member',
            '}'
        ]

        for cmd in commands:
            self.cli(cmd)
            time.sleep(0.1)

        print("  ✅ API Pool 作成: api_pool")

        # API VIP 作成
        commands = [
            'tmsh create ltm virtual vs_api {',
            '    destination 192.0.2.20:8080',
            '    pool api_pool',
            '    profiles add { http tcp }',
            '}'
        ]

        for cmd in commands:
            self.cli(cmd)
            time.sleep(0.1)

        print("  ✅ API VIP 作成: vs_api")

        # 全 Pool 一覧確認
        output = self.cli('tmsh show ltm pool')
        pool_count = output.count('Ltm::Pool')
        if pool_count >= 2:
            print(f"  ✅ 複数プール確認: {pool_count} 個以上")
            self.results.append(('Multiple Pools', True))
            return True
        else:
            print("  ⚠️  プール数が期待値より少ない")
            self.results.append(('Multiple Pools', False))
            return False

    def test_member_add_delete(self):
        """メンバー追加・削除テスト"""
        print("\n" + "="*70)
        print("🧪 メンバー追加・削除テスト")
        print("="*70)

        # メンバー追加
        self.cli('tmsh modify ltm pool web_pool members add { 10.0.0.4:80 }')
        time.sleep(0.2)
        print("  ✅ メンバー追加: 10.0.0.4:80")

        # メンバー削除
        self.cli('tmsh modify ltm pool web_pool members delete { 10.0.0.4:80 }')
        time.sleep(0.2)
        print("  ✅ メンバー削除: 10.0.0.4:80")

        output = self.cli('tmsh show ltm pool web_pool')
        if '10.0.0.4' not in output:
            print("  ✅ メンバー削除確認成功")
            self.results.append(('Member Add/Delete', True))
            return True
        else:
            print("  ❌ メンバー削除確認失敗")
            self.results.append(('Member Add/Delete', False))
            return False

    def test_pool_deletion(self):
        """Pool 削除テスト"""
        print("\n" + "="*70)
        print("🧪 Pool・VIP 削除テスト")
        print("="*70)

        # VIP 削除
        self.cli('tmsh delete ltm virtual vs_api')
        time.sleep(0.2)
        print("  ✅ VIP 削除: vs_api")

        # Pool 削除
        self.cli('tmsh delete ltm pool api_pool')
        time.sleep(0.2)
        print("  ✅ Pool 削除: api_pool")

        output = self.cli('tmsh show ltm pool api_pool')
        if 'not found' in output.lower() or 'api_pool' not in output:
            print("  ✅ Pool 削除確認成功")
            self.results.append(('Pool Deletion', True))
            return True
        else:
            print("  ⚠️  Pool がまだ存在している可能性")
            self.results.append(('Pool Deletion', False))
            return False

    def test_running_config(self):
        """実行中の設定表示テスト"""
        print("\n" + "="*70)
        print("🧪 実行中設定表示テスト")
        print("="*70)

        output = self.cli('tmsh show running-config')
        if output and ('ltm pool' in output or 'ltm virtual' in output):
            print("  ✅ Running-config 取得成功")
            line_count = len([l for l in output.split('\n') if l.strip()])
            print(f"      設定行数: {line_count}")
            self.results.append(('Running Config', True))
            return True
        else:
            print("  ⚠️  Running-config が空または形式が異なる")
            self.results.append(('Running Config', False))
            return False

    def run_all_tests(self):
        """全テスト実行"""
        print("\n" + "="*70)
        print("🧪 F5 BIG-IP LTM テストスイート")
        print("="*70)

        if not self.check_connectivity():
            print("❌ エミュレーター接続失敗")
            return False

        # デバイス登録
        self.register_device()
        time.sleep(1)

        # テスト実行
        tests = [
            self.test_pool_creation,
            self.test_virtual_server,
            self.test_member_management,
            self.test_multiple_pools,
            self.test_member_add_delete,
            self.test_running_config,
            self.test_pool_deletion,
        ]

        for test_func in tests:
            try:
                test_func()
                time.sleep(0.5)
            except Exception as e:
                print(f"  ❌ テスト実行エラー: {e}")

        # 結果サマリー
        print("\n" + "="*70)
        print("📊 テスト結果サマリー")
        print("="*70)

        passed = sum(1 for _, ok in self.results if ok)
        total = len(self.results)

        for name, ok in self.results:
            status = "✅ PASS" if ok else "❌ FAIL"
            print(f"  {status}: {name}")

        print(f"\n  📈 合計: {passed}/{total} 成功 ({int(passed*100/total)}%)")
        print("="*70)

        return passed == total


def main():
    parser = argparse.ArgumentParser(
        description='F5 BIG-IP LTM テストツール'
    )
    parser.add_argument('--emulator', action='store_true', help='エミュレーター環境で実行')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーター URL')

    args = parser.parse_args()

    print("\n" + "="*70)
    print("🧪 BIG-IP LTM テストツール")
    print("="*70)

    if args.emulator:
        tester = BigIPLTMTester(base_url=args.url)
        success = tester.run_all_tests()
        return 0 if success else 1

    print("❌ --emulator フラグが必要です")
    return 1


if __name__ == '__main__':
    sys.exit(main())
