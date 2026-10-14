"""
ZGrab2(ZMapプロジェクト製のL7アプリケーション層スキャナ)を使った
実SSHバナー取得テスト。

ユーザー依頼「ZGrab2: ポートスキャンはせず、指定したIP:ポート群に対して
プロトコルのハンドシェイクを最後まで実行してバナー/証明書/レスポンス
全体を構造化データ(JSON)で取得する。これもラボに入れてほしい」への
対応。nmapと違いポートスキャン自体は行わず、対象ポートに対して実際に
プロトコル(ここではSSH)のハンドシェイクを最後まで完了させ、結果を
JSONで構造化取得するのが特徴——このリポジトリの実SSHリスナー
(engine/ssh_cli_agent.py、Catalystの管理IP)に対して、本物のzgrab2
バイナリで実際にバナー/鍵交換情報を取得できることを確認する。

zgrab2バイナリ(Go製、`go install github.com/zmap/zgrab2/cmd/zgrab2@latest`
でビルドできる)がPATH上に無い環境ではこのファイル全体をスキップする
——tests/test_netmiko_catalyst.pyのnetmikoスキップパターンと同じ考え方
(回帰テスト自体はこの外部バイナリに依存しない、手動/オプションの
ライブ検証という位置づけ)。
"""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

ZGRAB2_BIN = shutil.which('zgrab2')
pytestmark = pytest.mark.skipif(
    ZGRAB2_BIN is None,
    reason='zgrab2 バイナリが見つからない(go install github.com/zmap/zgrab2/cmd/zgrab2@latest)')

PORT = 8129
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'zgrab2-catalyst'
MGMT_IP = '192.168.10.1'  # Catalystのデフォルト管理IP(Vlan10 SVI)


def _cli(cmd, dev=DEV):
    import urllib.request
    req = urllib.request.Request(
        BASE + '/api/cli', method='POST',
        data=json.dumps({'device_id': dev, 'command': cmd}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-zgrab2') / 'server.log'
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
def ssh_enabled_catalyst(server):
    import urllib.request
    req = urllib.request.Request(
        BASE + '/api/device', method='POST',
        data=json.dumps({'id': DEV, 'type': 'catalyst', 'hostname': 'ZG-SW'}).encode(),
        headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=30).read()
    # tests/test_nexpose_real_scan.py / strix-pentest.ymlと同じ有効化手順
    for cmd in ('configure terminal', 'crypto key generate rsa modulus 2048', 'end'):
        _cli(cmd)
    time.sleep(2)  # 実SSHリスナーのbind待ち
    return MGMT_IP


def _run_zgrab2_ssh(ip: str, blocklist_path: str, port: int = 22, timeout: float = 15.0) -> dict:
    """zgrab2 ssh を1ターゲットに対して実行し、結果JSON(1行)をパースして返す。
    --blocklist-fileを明示的に空ファイルに向ける——既定値(`-`)は
    `$HOME/.config/zgrab2/blocklist.conf`を指すため、未作成の環境
    (CI含む)では毎回`could not read blocklist file`で即失敗する
    (ライブ検証で最初に踏んだ不具合)。"""
    proc = subprocess.run(
        [ZGRAB2_BIN, 'ssh', '--port', str(port), '--connect-timeout', '10s',
         '--blocklist-file', blocklist_path],
        input=f'{ip}\n', capture_output=True, text=True, timeout=timeout)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith('{'):
            return json.loads(line)
    raise AssertionError(f'zgrab2の出力からJSON結果行が見つからない: '
                         f'stdout={proc.stdout!r} stderr={proc.stderr!r}')


class TestZgrab2SshBanner:
    def test_zgrab2_completes_the_ssh_handshake_and_captures_the_banner(self, ssh_enabled_catalyst, tmp_path):
        ip = ssh_enabled_catalyst
        blocklist = tmp_path / 'blocklist.conf'
        blocklist.write_text('')
        result = _run_zgrab2_ssh(ip, str(blocklist))
        assert result.get('ip') == ip
        data = result.get('data', {}).get('ssh', {})
        assert data.get('status') == 'success', f'zgrab2側がSSHハンドシェイク失敗と判定: {data}'
        banner = data.get('result', {}).get('server_id', {}).get('raw', '')
        assert banner.startswith('SSH-2.0'), f'実機風のSSHバナーが取得できていない: {banner!r}'

    def test_zgrab2_reports_failure_for_a_closed_port(self, ssh_enabled_catalyst, tmp_path):
        """念のため、SSHが上がっていないポートに対しては失敗判定になる
        ことを確認し、フェイクの成功を作っていないことを固定する。"""
        ip = ssh_enabled_catalyst
        blocklist = tmp_path / 'blocklist.conf'
        blocklist.write_text('')
        result = _run_zgrab2_ssh(ip, str(blocklist), port=2222, timeout=15.0)
        data = result.get('data', {}).get('ssh', {})
        assert data.get('status') != 'success'
