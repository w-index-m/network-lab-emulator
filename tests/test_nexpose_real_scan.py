"""
Nexpose の実ポートスキャン — 本物の uvicorn サーバに対して確認する

tests/test_nexpose_api.py は TestClient を使っているが、TestClient は
**リクエストを処理している間しかイベントループを回さない**。SNMP UDP
エージェントは同じループ上の asyncio DatagramProtocol なので、
TestClient 環境ではリクエスト外に届いたパケットが処理されず、
実プローブだと 161 が常に閉じて見える（ソケットは bind 済みで
Recv-Q にパケットが溜まったままになる）。

そこで、ここだけは本物のサーバをサブプロセスで立てて、
外から本物のソケットで叩く。確認するのは:

  - 設定した管理サービスが、本当にそのIP:ポートで待ち受けていること
  - スキャナがモデルではなく現物を見ていること
    （リスナーを止めれば検出結果からも消える）
  - `no netconf-yang` がポートを実際に解放すること
  - 装置を削除したらポートが閉じること

実際にこの3つは、実プローブを入れて初めて壊れているのが分かった。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

PORT = 8123
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'rs-a'
DEV_IP = '10.216.0.1'


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:                  # pragma: no cover
        raise AssertionError(f'{method} {path} -> {e.code}: '
                             f'{e.read()[:200]!r}') from None
    return json.loads(raw) if raw else None


def _cli(cmd):
    return _req('POST', '/api/cli', {'device_id': DEV, 'command': cmd})['output']


def _tcp_open(ip, port, timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    # 標準出力はファイルへ。PIPE にするとこのアプリの大量のログで
    # パイプバッファが埋まり、サーバ側が書き込みでブロックして
    # 起動したまま応答しなくなる。
    log = tmp_path_factory.mktemp('netlab') / 'server.log'
    env = dict(os.environ, NETLAB_AUTH_DISABLE='1')
    with open(log, 'wb') as fh:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app:app',
             '--host', '127.0.0.1', '--port', str(PORT)],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    _req('GET', '/api/3')
                    break
                except Exception:
                    if proc.poll() is not None:          # pragma: no cover
                        pytest.fail('サーバが起動しませんでした:\n'
                                    + log.read_text(errors='replace')[-2000:])
                    time.sleep(0.5)
            else:                                        # pragma: no cover
                pytest.fail('サーバが時間内に起動しませんでした:\n'
                            + log.read_text(errors='replace')[-2000:])
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:            # pragma: no cover
                proc.kill()


@pytest.fixture
def device(server):
    _req('DELETE', f'/api/device/{DEV}')
    _req('POST', '/api/device',
         {'id': DEV, 'type': 'catalyst', 'hostname': 'SCAN-TARGET'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit', 'snmp-server community public ro',
              'netconf-yang', 'gnxi', 'gnxi server', 'end'):
        _cli(c)
    time.sleep(3)                # リスナーが上がるのを待つ
    yield
    _req('DELETE', f'/api/device/{DEV}')


@pytest.fixture
def site(device):
    sid = _req('POST', '/api/3/sites', {
        'name': 'Real Scan',
        'scan': {'assets': {'includedTargets': {'addresses': [DEV_IP]}}}})['id']
    yield sid


def _scan(sid, **body):
    _req('POST', f'/api/3/sites/{sid}/scans', body)
    return _req('GET', f'/api/3/sites/{sid}/assets')['resources'][0]


def _ports(asset):
    return {(s['protocol'], s['port']) for s in asset['services']}


# ══════════════════════════════════════════
def test_configured_services_really_listen(device):
    """設定したサービスが本当にそのIP:ポートで待ち受けていること

    NETCONF/gNMI は装置IPに bind できず起動に失敗していた
    （SNMPだけがループバックへのエイリアス追加をしていたため）。
    """
    assert _tcp_open(DEV_IP, 830), 'NETCONF(830) が待ち受けていない'
    assert _tcp_open(DEV_IP, 50052), 'gNMI(50052) が待ち受けていない'


def test_scan_confirms_services_with_real_sockets(site):
    a = _scan(site)
    assert _ports(a) == {('udp', 161), ('tcp', 830), ('tcp', 50052)}
    how = {s['port']: s['detectedBy'] for s in a['services']}
    assert how[161] == 'snmp-get'          # 本物のSNMP GETで確認している
    assert how[830] == 'tcp-connect'
    assert how[50052] == 'tcp-connect'


def test_stopping_a_listener_removes_it_from_the_findings(site):
    """モデルではなく現物を見ていること"""
    before = _scan(site)
    assert ('tcp', 50052) in _ports(before)

    for c in ('configure terminal', 'no gnxi server', 'end'):
        _cli(c)
    time.sleep(1.5)

    after = _scan(site)
    assert ('tcp', 50052) not in _ports(after)
    assert after['riskScore'] < before['riskScore']
    assert 'netlab-grpc-no-tls' not in after['_vuln_ids']


def test_no_netconf_yang_actually_releases_the_port(site):
    """`no netconf-yang` でポートが解放され、再有効化できること

    stop() が close() しかしておらず、accept() でブロックしている
    スレッドが起きないため fd が残り、TCP/830 が LISTEN のままだった。
    その状態で再度 netconf-yang を打つと Address already in use で
    起動に失敗していた。
    """
    assert _tcp_open(DEV_IP, 830)

    for c in ('configure terminal', 'no netconf-yang', 'end'):
        _cli(c)
    time.sleep(1.5)
    assert not _tcp_open(DEV_IP, 830), '830 が開いたまま残っている'
    assert ('tcp', 830) not in _ports(_scan(site))

    for c in ('configure terminal', 'netconf-yang', 'end'):
        _cli(c)
    time.sleep(2.5)
    assert _tcp_open(DEV_IP, 830), '再度有効にしたのに待ち受けていない'
    assert ('tcp', 830) in _ports(_scan(site))


def test_deleting_the_device_closes_its_ports(device):
    """装置を消したらポートも閉じること

    DELETE /api/device は各エンジンの登録を消すだけで実リスナーを
    止めておらず、消したはずの装置のポートが開いたままだった。
    """
    assert _tcp_open(DEV_IP, 830)
    _req('DELETE', f'/api/device/{DEV}')
    time.sleep(1.5)
    assert not _tcp_open(DEV_IP, 830)
    assert not _tcp_open(DEV_IP, 50052)


def test_scanner_finds_a_port_the_configuration_says_nothing_about(site):
    """装置の設定に無いポートでも、開いていれば見つけること

    候補ポートだけを確認する作りだと「設定は消えたのにリスナーが
    残っている」「勝手に何かが上がっている」を見逃す。
    スキャナなのだから設定ではなく現物を見る。

    ここでは装置の設定と無関係に、テスト側で 22/tcp を開けて確かめる。
    """
    a = _scan(site)
    assert ('tcp', 22) not in _ports(a)

    rogue = socket.socket()
    rogue.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rogue.bind((DEV_IP, 22))
    rogue.listen(5)
    try:
        found = _scan(site)
        assert ('tcp', 22) in _ports(found), \
            '設定に無いが開いているポートを見逃している'
        svc = next(s for s in found['services'] if s['port'] == 22)
        assert svc['detectedBy'] == 'tcp-connect'
    finally:
        rogue.close()

    assert ('tcp', 22) not in _ports(_scan(site))     # 閉じたら消える


def test_probe_false_falls_back_to_reading_the_configuration(site):
    a = _scan(site, probe=False)
    assert all(s['detectedBy'] == 'configuration' for s in a['services'])


# ══════════════════════════════════════════
# 資格情報を本当に試す
# ══════════════════════════════════════════
def _cred(sid, name, account):
    return _req('POST', f'/api/3/sites/{sid}/site_credentials',
                {'name': name, 'account': account})['id']


def _detail(asset, name):
    return next(d for d in asset['credentials'] if d['name'] == name)


def test_no_credentials(site):
    a = _scan(site)
    assert a['credentialStatus'] == 'no-credentials-supplied'
    assert a['credentials'] == []


def test_wrong_ssh_password_does_not_authenticate_the_scan(site):
    """でたらめなパスワードで認証スキャンにならないこと

    以前は「登録されていれば認証成功」とみなしていたので、
    どんなパスワードでも requires_auth の所見まで出ていた。
    """
    _cred(site, 'bad-ssh', {'service': 'ssh', 'username': 'admin',
                            'password': 'definitely-wrong'})
    a = _scan(site)
    assert a['credentialStatus'] == 'credential-status-login-failed'
    assert _detail(a, 'bad-ssh')['verified'] is False
    assert 'netlab-no-aaa-authentication' not in a['_vuln_ids']


def test_correct_ssh_password_really_logs_in(site):
    """本物のSSH認証が通ったときだけ認証スキャンになること

    この装置はローカルユーザを定義していないので、NETCONFのSSHサーバは
    admin/admin にフォールバックする（engine/netconf_agent.py）。
    """
    _cred(site, 'good-ssh', {'service': 'ssh', 'username': 'admin',
                             'password': 'admin'})
    a = _scan(site)
    assert a['credentialStatus'] == 'credential-status-success'
    d = _detail(a, 'good-ssh')
    assert d['verified'] is True
    assert d['note'] == 'authenticated on tcp/830'
    assert 'netlab-no-aaa-authentication' in a['_vuln_ids']


def test_credential_follows_the_device_password(site):
    """装置側のパスワードを変えたら、同じ資格情報が通らなくなること"""
    _cred(site, 'ssh', {'service': 'ssh', 'username': 'admin',
                        'password': 'admin'})
    assert _scan(site)['credentialStatus'] == 'credential-status-success'

    # ローカルユーザを作ると admin/admin のフォールバックは無くなる
    for c in ('configure terminal',
              'username netadmin privilege 15 secret An0therP@ss', 'end'):
        _cli(c)
    time.sleep(1)
    assert _scan(site)['credentialStatus'] == 'credential-status-login-failed'

    _cred(site, 'ssh2', {'service': 'ssh', 'username': 'netadmin',
                         'password': 'An0therP@ss'})
    a = _scan(site)
    assert a['credentialStatus'] == 'credential-status-success'
    assert _detail(a, 'ssh2')['verified'] is True
    assert _detail(a, 'ssh')['verified'] is False


def test_snmp_credential_is_checked_against_the_real_agent(site):
    """SNMPコミュニティも本物のGETで確かめること"""
    _cred(site, 'bad-snmp', {'service': 'snmp', 'community': 'not-configured'})
    a = _scan(site)
    assert _detail(a, 'bad-snmp')['verified'] is False
    assert a['credentialStatus'] == 'credential-status-login-failed'

    _cred(site, 'good-snmp', {'service': 'snmp', 'community': 'public'})
    a = _scan(site)
    assert _detail(a, 'good-snmp')['verified'] is True
    assert a['credentialStatus'] == 'credential-status-success'


def test_credential_for_a_service_that_is_not_running(site):
    for c in ('configure terminal', 'no netconf-yang', 'end'):
        _cli(c)
    time.sleep(1.5)
    _cred(site, 'ssh', {'service': 'ssh', 'username': 'admin',
                        'password': 'admin'})
    a = _scan(site)
    assert a['credentialStatus'] == 'credential-status-service-not-found'
    assert _detail(a, 'ssh')['note'] == 'no SSH service found'


# ══════════════════════════════════════════
# SNMPコミュニティの照合（実機と同じ「黙って捨てる」）
# ══════════════════════════════════════════
def _snmp_get(ip, community, oid='1.3.6.1.2.1.1.5.0', timeout=1.5):
    from engine.nexpose import probe_snmp
    return probe_snmp(ip, 161, community, timeout=timeout)


def test_configured_community_is_required(device):
    """設定したコミュニティだけが通ること

    以前は `_auth` が設定に関わらず 'public' を常に許していたため、
    コミュニティを変えても public で読めてしまっていた。
    """
    for c in ('configure terminal', 'no snmp-server community public',
              'snmp-server community s3cret-only ro', 'end'):
        _cli(c)
    time.sleep(1)
    assert _snmp_get(DEV_IP, 's3cret-only') is True
    assert _snmp_get(DEV_IP, 'public') is False
    assert _snmp_get(DEV_IP, 'wrong') is False


def test_walk_does_not_bypass_the_community_check(device):
    """GETNEXT/WALK がコミュニティ照合を素通りしないこと

    以前 `getnext()` はコミュニティ引数すら受け取っておらず、
    でたらめなコミュニティで snmpwalk するとMIBが丸ごと読めた。
    """
    import subprocess
    for c in ('configure terminal', 'no snmp-server community public',
              'snmp-server community s3cret-only ro', 'end'):
        _cli(c)
    time.sleep(1)

    def walk(community):
        r = subprocess.run(
            ['snmpwalk', '-v2c', '-c', community, '-t', '2', '-r', '0',
             DEV_IP, '1.3.6.1.2.1.1'],
            capture_output=True, text=True, timeout=30)
        return r.stdout

    assert 'SCAN-TARGET' in walk('s3cret-only')
    assert 'SCAN-TARGET' not in walk('wrong-community')
    assert 'SCAN-TARGET' not in walk('public')


def test_multiple_communities_all_work(device):
    """コミュニティを複数設定したらどれでも読めること

    エージェント側が1つしか保持できず、2つ目以降で読めなかった。
    """
    for c in ('configure terminal', 'snmp-server community alpha ro',
              'snmp-server community bravo rw', 'end'):
        _cli(c)
    time.sleep(1)
    assert _snmp_get(DEV_IP, 'alpha') is True
    assert _snmp_get(DEV_IP, 'bravo') is True       # RWでも読み取りはできる
    assert _snmp_get(DEV_IP, 'charlie') is False
