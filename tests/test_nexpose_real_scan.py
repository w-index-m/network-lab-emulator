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


def _cli(cmd, dev=DEV):
    return _req('POST', '/api/cli', {'device_id': dev, 'command': cmd})['output']


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


def client_post_credential(sid, body):
    """`_req` と違い、4xx でも例外にせず {status_code, message, ...} で返す

    バリデーションエラーの中身（メッセージ文言）自体を確認したい
    テスト用。
    """
    req = urllib.request.Request(
        BASE + f'/api/3/sites/{sid}/site_credentials', method='POST',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {'status_code': r.status, **json.loads(r.read())}
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        return {'status_code': e.code, **payload}


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
# 認証後は本物のSSHで show running-config を読む
# ══════════════════════════════════════════
@pytest.fixture
def ssh_device(server):
    """SSH CLI(22) が上がっていて、直すべき設定が入っている装置"""
    _req('DELETE', f'/api/device/{DEV}')
    _req('POST', '/api/device',
         {'id': DEV, 'type': 'catalyst', 'hostname': 'SCAN-TARGET'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit',
              'snmp-server community public ro',
              'snmp-server community wr1te rw',
              'username netadmin privilege 15 secret pw',   # 8文字未満
              'crypto key generate rsa modulus 2048', 'end'):
        _cli(c)
    time.sleep(3)
    sid = _req('POST', '/api/3/sites', {
        'name': 'SSH scan',
        'scan': {'assets': {'includedTargets': {'addresses': [DEV_IP]}}}})['id']
    yield sid
    _req('DELETE', f'/api/device/{DEV}')


def test_scanner_really_reads_the_running_config_over_ssh(ssh_device):
    """認証後に `show running-config` を実際に実行していること

    以前は認証だけ本物で、読み取りは DeviceState を直接覗いていた。
    """
    sid = ssh_device
    _cred(sid, 'ssh', {'service': 'ssh', 'username': 'netadmin',
                       'password': 'pw'})
    a = _scan(sid)
    d = _detail(a, 'ssh')
    assert d['verified'] is True
    assert 'tcp/22' in d['note']
    assert 'read running-config' in d['note']       # 本当に取得した


def test_findings_come_from_the_config_that_was_read(ssh_device):
    sid = ssh_device
    _cred(sid, 'ssh', {'service': 'ssh', 'username': 'netadmin',
                       'password': 'pw'})
    ids = set(_scan(sid)['_vuln_ids'])
    assert {'netlab-no-aaa-authentication',
            'netlab-weak-local-password',
            'netlab-snmp-rw-community'} <= ids


def test_fixing_the_device_clears_the_config_based_findings(ssh_device):
    """設定を直すと、読み取った config が変わり所見も消えること"""
    sid = ssh_device
    _cred(sid, 'ssh', {'service': 'ssh', 'username': 'netadmin',
                       'password': 'pw'})
    assert 'netlab-snmp-rw-community' in _scan(sid)['_vuln_ids']

    for c in ('configure terminal', 'aaa new-model',
              'no snmp-server community wr1te',
              'username netadmin privilege 15 secret Str0ngP@ssw0rd', 'end'):
        _cli(c)
    time.sleep(1)
    # パスワードを変えたので古い資格情報はもう通らない
    _cred(sid, 'ssh2', {'service': 'ssh', 'username': 'netadmin',
                        'password': 'Str0ngP@ssw0rd'})
    a = _scan(sid)
    assert _detail(a, 'ssh')['verified'] is False
    assert _detail(a, 'ssh2')['verified'] is True
    for gone in ('netlab-no-aaa-authentication', 'netlab-weak-local-password',
                 'netlab-snmp-rw-community'):
        assert gone not in a['_vuln_ids']


def test_ssh_port_22_is_discovered_by_the_scan(ssh_device):
    a = _scan(ssh_device)
    assert ('tcp', 22) in _ports(a)
    svc = next(s for s in a['services'] if s['port'] == 22)
    assert svc['detectedBy'] == 'tcp-connect'


# ══════════════════════════════════════════
# 権限昇格（enable）を認識した資格情報照合
# ══════════════════════════════════════════
# ssh_cli_agent.py に user EXEC / privileged EXEC の区別を入れた結果、
# privilege 15 未満のローカルユーザは `show running-config` を
# 実行できなくなった。ログイン(認証)は通るが設定は読めない、という
# 状態が実際に起こるようになったので、その扱いを固定する。
@pytest.fixture
def low_priv_ssh_device(server):
    _req('DELETE', f'/api/device/{DEV}')
    _req('POST', '/api/device',
         {'id': DEV, 'type': 'catalyst', 'hostname': 'LOWPRIV-TARGET'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit',
              'snmp-server community secret-rw rw',   # state直読みで拾えるはず
              'aaa new-model',                        # ← 設定済み
              'username lowpriv privilege 1 secret LowP@ss',
              'crypto key generate rsa modulus 2048', 'end'):
        _cli(c)
    time.sleep(3)
    sid = _req('POST', '/api/3/sites', {
        'name': 'Low priv scan',
        'scan': {'assets': {'includedTargets': {'addresses': [DEV_IP]}}}})['id']
    yield sid
    _req('DELETE', f'/api/device/{DEV}')


def test_low_privilege_credential_authenticates_but_cannot_read_config(
        low_priv_ssh_device):
    """認証は通るが running-config は読めない、という状態になること

    privilege 1 のアカウントでは `show running-config` がuser EXECで
    ブロックされる（privileged EXEC専用コマンド）ので、ログインは
    verified=True でも config は取得できない。
    """
    sid = low_priv_ssh_device
    _cred(sid, 'lowpriv', {'service': 'ssh', 'username': 'lowpriv',
                           'password': 'LowP@ss'})
    a = _scan(sid)
    d = _detail(a, 'lowpriv')
    assert d['verified'] is True
    assert 'read running-config' not in d['note']
    assert d['note'] == 'authenticated on tcp/22'


def test_config_fetch_denial_falls_back_to_reading_the_device_state(
        low_priv_ssh_device):
    """config が読めなくても、判定自体はできる限り続けること

    以前は「拒否されたときの応答文字列」をそのまま config として
    扱っていたため、`aaa new-model` や `RW` を正規表現で探しても
    見つからず、実際には脆弱な状態でも所見が消えてしまっていた
    （configが読めているように見えて中身が空振り、という壊れ方）。
    ここでは config が読めない場合、DeviceState を直接見る従来の
    経路にきちんとフォールバックすることを確認する。
    """
    sid = low_priv_ssh_device
    _cred(sid, 'lowpriv', {'service': 'ssh', 'username': 'lowpriv',
                           'password': 'LowP@ss'})
    ids = set(_scan(sid)['_vuln_ids'])
    # RWコミュニティは実際に設定されているので検出されるべき
    assert 'netlab-snmp-rw-community' in ids
    # aaa new-model は設定済みなので、この所見は立たないはず
    assert 'netlab-no-aaa-authentication' not in ids


# ══════════════════════════════════════════
# permissionElevation: "privileged-exec"（Cisco enable相当）
# ══════════════════════════════════════════
ENABLE_SECRET = 'En@bleSecret1'


@pytest.fixture
def low_priv_no_aaa_device(server):
    """privilege 1 のユーザ + enable secret。aaa new-modelは未設定
    （enable昇格が効いているかを "netlab-no-aaa-authentication" の
    有無で見分けるため、わざと未設定にしている）"""
    _req('DELETE', f'/api/device/{DEV}')
    _req('POST', '/api/device',
         {'id': DEV, 'type': 'catalyst', 'hostname': 'ELEV-TARGET'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit',
              'username lowpriv privilege 1 secret LowP@ss',
              f'enable secret {ENABLE_SECRET}',
              'crypto key generate rsa modulus 2048', 'end'):
        _cli(c)
    time.sleep(3)
    sid = _req('POST', '/api/3/sites', {
        'name': 'Elevation scan',
        'scan': {'assets': {'includedTargets': {'addresses': [DEV_IP]}}}})['id']
    yield sid
    _req('DELETE', f'/api/device/{DEV}')


def _elevated_cred(sid, name, password, elevation_password,
                   username='lowpriv', service='ssh'):
    return _cred(sid, name, {
        'service': service, 'username': username, 'password': password,
        'permissionElevation': 'privileged-exec',
        'permissionElevationUsername': 'enable',
        'permissionElevationPassword': elevation_password})


def test_without_elevation_config_cannot_be_read(low_priv_no_aaa_device):
    """比較用: 昇格無しだと従来通り config は読めない"""
    sid = low_priv_no_aaa_device
    _cred(sid, 'plain', {'service': 'ssh', 'username': 'lowpriv',
                         'password': 'LowP@ss'})
    a = _scan(sid)
    d = _detail(a, 'plain')
    assert d['verified'] is True
    assert 'read running-config' not in d['note']


def test_elevation_with_the_correct_password_reads_the_real_config(
        low_priv_no_aaa_device):
    """正しい enable パスワードを持たせれば、privilege 1 でも
    running-config を実際に読めること"""
    sid = low_priv_no_aaa_device
    _elevated_cred(sid, 'elevated', 'LowP@ss', ENABLE_SECRET)
    a = _scan(sid)
    d = _detail(a, 'elevated')
    assert d['verified'] is True
    assert 'elevated via enable' in d['note']
    assert 'read running-config' in d['note']
    # aaa new-model が本当に未設定なので、昇格後に読んだconfigから
    # この所見が立つはず（DeviceState直読みのフォールバックでも
    # 同じ結論になるが、ここでは実際に読めていることを確かめたい）
    assert 'netlab-no-aaa-authentication' in a['_vuln_ids']


def test_elevation_with_the_wrong_password_does_not_read_config(
        low_priv_no_aaa_device):
    sid = low_priv_no_aaa_device
    _elevated_cred(sid, 'bad-elev', 'LowP@ss', 'WrongEnablePassword')
    a = _scan(sid)
    d = _detail(a, 'bad-elev')
    assert d['verified'] is True           # SSHログイン自体は通っている
    assert 'enable failed' in d['note']
    assert 'read running-config' not in d['note']


def test_elevation_password_is_never_returned_by_the_api(
        low_priv_no_aaa_device):
    sid = low_priv_no_aaa_device
    _elevated_cred(sid, 'elevated', 'LowP@ss', ENABLE_SECRET)
    rows = _req('GET', f'/api/3/sites/{sid}/site_credentials')['resources']
    row = next(r for r in rows if r['name'] == 'elevated')
    assert row['account']['permissionElevation'] == 'privileged-exec'
    assert row['account']['permissionElevationUsername'] == 'enable'
    assert 'permissionElevationPassword' not in row['account']
    assert 'password' not in row['account']


def test_unsupported_permission_elevation_is_rejected(low_priv_no_aaa_device):
    sid = low_priv_no_aaa_device
    r = client_post_credential(sid, {
        'name': 'bad', 'account': {'service': 'ssh', 'username': 'x',
                                   'password': 'y',
                                   'permissionElevation': 'made-up-value'}})
    assert r['status_code'] == 400
    assert 'unsupported permissionElevation' in r['message']


def test_permission_elevation_requires_username_and_password(
        low_priv_no_aaa_device):
    sid = low_priv_no_aaa_device
    r = client_post_credential(sid, {
        'name': 'bad', 'account': {
            'service': 'ssh', 'username': 'x', 'password': 'y',
            'permissionElevation': 'privileged-exec',
            'permissionElevationUsername': 'enable'}})
    assert r['status_code'] == 400
    assert 'permissionElevationPassword is required' in r['message']

    r2 = client_post_credential(sid, {
        'name': 'bad2', 'account': {
            'service': 'ssh', 'username': 'x', 'password': 'y',
            'permissionElevation': 'privileged-exec',
            'permissionElevationPassword': 'secret'}})
    assert r2['status_code'] == 400
    assert 'permissionElevationUsername is required' in r2['message']


def test_none_and_pbrun_do_not_require_elevation_credentials(
        low_priv_no_aaa_device):
    """none/pbrun は例外的にユーザ名・パスワードが無くても通ること
    （実機の規則）"""
    sid = low_priv_no_aaa_device
    assert client_post_credential(sid, {
        'name': 'ok-none', 'account': {
            'service': 'ssh', 'username': 'x', 'password': 'y',
            'permissionElevation': 'none'}})['status_code'] == 201
    assert client_post_credential(sid, {
        'name': 'ok-pbrun', 'account': {
            'service': 'ssh', 'username': 'x', 'password': 'y',
            'permissionElevation': 'pbrun'}})['status_code'] == 201


def test_unimplemented_elevation_types_are_accepted_but_do_nothing(
        low_priv_no_aaa_device):
    """su/sudo/sudosu はUNIX系向けでこのエミュレータに実体が無いので、
    受理はするが機能的には素通しになること（昇格を試みない）"""
    sid = low_priv_no_aaa_device
    _cred(sid, 'sudo-cred', {
        'service': 'ssh', 'username': 'lowpriv', 'password': 'LowP@ss',
        'permissionElevation': 'sudo',
        'permissionElevationUsername': 'root',
        'permissionElevationPassword': 'whatever'})
    a = _scan(sid)
    d = _detail(a, 'sudo-cred')
    assert d['verified'] is True
    assert 'elevated' not in d['note']
    assert 'read running-config' not in d['note']


# ══════════════════════════════════════════
# Telnet（平文管理）
# ══════════════════════════════════════════
def _telnet_on():
    for c in ('configure terminal', 'line vty 0 4', 'transport input all',
              'end'):
        _cli(c)
    time.sleep(1.2)


def _telnet_off():
    for c in ('configure terminal', 'line vty 0 4', 'transport input ssh',
              'end'):
        _cli(c)
    time.sleep(1.2)


def test_cleartext_finding_only_fires_when_telnet_is_really_open(site):
    """回帰テスト: この所見は一度も成立しない死んだ判定だった

    検出条件が `state.telnet_enabled` を見ていたのに、その属性を
    立てるコードがどこにも無かった。
    """
    assert 'netlab-telnet-cleartext' not in _scan(site)['_vuln_ids']

    _telnet_on()
    a = _scan(site)
    assert ('tcp', 23) in _ports(a)
    assert 'netlab-telnet-cleartext' in a['_vuln_ids']

    _telnet_off()
    a = _scan(site)
    assert ('tcp', 23) not in _ports(a)
    assert 'netlab-telnet-cleartext' not in a['_vuln_ids']


def test_telnet_credentials_are_really_tried(site):
    _telnet_on()
    _cred(site, 'bad-tn', {'service': 'telnet', 'username': 'admin',
                           'password': 'definitely-wrong'})
    a = _scan(site)
    assert _detail(a, 'bad-tn')['verified'] is False
    assert a['credentialStatus'] == 'credential-status-login-failed'

    _cred(site, 'good-tn', {'service': 'telnet', 'username': 'admin',
                            'password': 'admin'})
    a = _scan(site)
    d = _detail(a, 'good-tn')
    assert d['verified'] is True
    assert 'cleartext' in d['note']
    assert a['credentialStatus'] == 'credential-status-success'


def test_telnet_credential_reads_the_config_and_drives_findings(site):
    """Telnetでも `show running-config` を実際に読むこと"""
    _telnet_on()
    _cred(site, 'tn', {'service': 'telnet', 'username': 'admin',
                       'password': 'admin'})
    a = _scan(site)
    assert 'read running-config' in _detail(a, 'tn')['note']
    assert 'netlab-no-aaa-authentication' in a['_vuln_ids']


def test_telnet_credential_when_the_service_is_closed(site):
    _telnet_off()
    _cred(site, 'tn', {'service': 'telnet', 'username': 'admin',
                       'password': 'admin'})
    a = _scan(site)
    assert _detail(a, 'tn')['note'] == 'no Telnet service found'
    assert a['credentialStatus'] == 'credential-status-service-not-found'


def test_https_credentials_are_reported_as_unverified(site):
    """RESTCONFは装置ごとの:443では提供していないので試しようがない

    以前は candidate_services が 443 を挙げていたが、そのアドレスでは
    誰も待ち受けておらず、実プローブでは絶対に確認できない候補だった。
    """
    _cred(site, 'web', {'service': 'https', 'username': 'admin',
                        'password': 'admin'})
    a = _scan(site)
    d = _detail(a, 'web')
    assert d['verified'] is False
    assert 'not implemented' in d['note']
    assert ('tcp', 443) not in _ports(a)


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


def test_renaming_the_community_clears_the_finding_under_a_real_scan(site):
    """コミュニティ名を変えると所見が消えること（実プローブ版）

    同名のテストが test_nexpose_api.py にもあるが、あちらは
    probe=False。こちらは本物のSNMP GETでポートを確認したうえで
    判定している。
    """
    a = _scan(site)
    assert ('udp', 161) in _ports(a)
    assert 'netlab-snmp-default-community' in a['_vuln_ids']

    for c in ('configure terminal', 'no snmp-server community public',
              'snmp-server community n0t-public ro', 'end'):
        _cli(c)
    time.sleep(1)

    a = _scan(site)
    assert 'netlab-snmp-default-community' not in a['_vuln_ids']
    # ポート自体は開いたまま（設定を消したわけではない）
    assert ('udp', 161) in _ports(a)


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


# ══════════════════════════════════════════
# 非同期スキャン（body: {"async": true}）
# ══════════════════════════════════════════
# 既定のスキャンは（既存の呼び出し側・テストを一切変えずに済むよう）
# 今も同期的に完了する。`async: true` を指定した場合だけ、実際に
# バックグラウンドで進み、pause/resume/stopがその場で意味を持つ
# ようになる。実機のAPI仕様には無いエミュレータ独自の拡張
# （docs/nexpose-api.md 参照）。
DEV2 = 'rs-b'
DEV2_IP = '10.217.9.1'


@pytest.fixture
def two_device_site(server):
    """2台の装置を持つサイト。pause/resumeを「装置と装置の間」で
    観測できるだけの実時間が要る。"""
    for d, ip, host in ((DEV, DEV_IP, 'SCAN-TARGET-1'),
                        (DEV2, DEV2_IP, 'SCAN-TARGET-2')):
        _req('DELETE', f'/api/device/{d}')
        _req('POST', '/api/device', {'id': d, 'type': 'catalyst',
                                     'hostname': host})
        for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
                  'no switchport', f'ip address {ip} 255.255.255.0',
                  'no shutdown', 'end'):
            _cli(c, dev=d)
    time.sleep(2)
    sid = _req('POST', '/api/3/sites', {
        'name': 'Async scan',
        'scan': {'assets': {'includedTargets': {'addresses': [DEV_IP, DEV2_IP]}}}
    })['id']
    yield sid
    for d in (DEV, DEV2):
        _req('DELETE', f'/api/device/{d}')


def _wait_for_scan_status(scan_id, statuses, timeout=20, interval=0.2):
    """scanが指定した状態のどれかになるまで待つ。最後に見た値を返す。"""
    end = time.time() + timeout
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    while time.time() < end and scan['status'] not in statuses:
        time.sleep(interval)
        scan = _req('GET', f'/api/3/scans/{scan_id}')
    return scan


def test_async_scan_starts_running_and_reaches_finished(two_device_site):
    sid = two_device_site
    scan_id = _req('POST', f'/api/3/sites/{sid}/scans',
                   {'async': True})['id']
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    # 呼び出し元にすぐ返る。まだ終わっていない可能性が高い
    assert scan['status'] in ('running', 'finished')

    final = _wait_for_scan_status(scan_id, {'finished'})
    assert final['status'] == 'finished'
    assert final['assets'] == 2
    assert final['endTime'] is not None


def test_async_scan_can_really_be_paused_and_resumed(two_device_site):
    """本当にバックグラウンドで進んでいるスキャンを、実際に
    pause/resumeできること（テストが状態を手で書き換えるのではなく、
    バックグラウンドスレッドが実際にその状態で止まる/動き出す）"""
    sid = two_device_site
    scan_id = _req('POST', f'/api/3/sites/{sid}/scans',
                   {'async': True})['id']

    # すぐpauseする。装置1台の評価には現実の秒数がかかるので、
    # まだ running のうちにpauseが間に合う可能性が高い
    _req('POST', f'/api/3/scans/{scan_id}/pause')
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    if scan['status'] == 'finished':
        pytest.skip('スキャンがpauseより先に終わってしまった（環境依存）')
    assert scan['status'] == 'paused'

    # paused のまま、しばらく待っても2台目までは進んでいないこと
    time.sleep(2)
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    assert scan['status'] == 'paused'
    assert scan['assets'] < 2

    # resumeすれば最後まで進む
    _req('POST', f'/api/3/scans/{scan_id}/resume')
    final = _wait_for_scan_status(scan_id, {'finished'})
    assert final['status'] == 'finished', 'resumeしても終わらなかった'
    assert final['assets'] == 2


def test_async_scan_can_really_be_stopped(two_device_site):
    """止めたスキャンは、それ以降の装置を評価しないこと"""
    sid = two_device_site
    scan_id = _req('POST', f'/api/3/sites/{sid}/scans',
                   {'async': True})['id']

    _req('POST', f'/api/3/scans/{scan_id}/stop')
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    if scan['status'] == 'finished':
        pytest.skip('スキャンがstopより先に終わってしまった（環境依存）')

    final = _wait_for_scan_status(scan_id, {'stopped'}, timeout=10)
    assert final['status'] == 'stopped', 'stopしても止まらなかった'
    assert final['assets'] < 2                  # 2台目はもう評価していない
    assert final['endTime'] is not None


def test_pause_and_stop_are_rejected_once_a_scan_has_finished(
        two_device_site):
    """終わったスキャンをpause/stopしようとしたら、実機同様400"""
    sid = two_device_site
    scan_id = _req('POST', f'/api/3/sites/{sid}/scans', {})['id']  # 同期実行
    assert _req('GET', f'/api/3/scans/{scan_id}')['status'] == 'finished'

    for action in ('pause', 'resume', 'stop'):
        try:
            _req('POST', f'/api/3/scans/{scan_id}/{action}')
            pytest.fail(f'{action} が finished のスキャンに対して通ってしまった')
        except AssertionError as e:
            assert 'finished' in str(e) or 'expected one of' in str(e)


def test_default_sync_scan_behaviour_is_unchanged(two_device_site):
    """async を指定しない既定の呼び出しは、これまでどおり
    戻ってきた時点で完全に終わっていること（既存の呼び出し側を
    壊さないための後方互換性の確認）"""
    sid = two_device_site
    scan_id = _req('POST', f'/api/3/sites/{sid}/scans', {})['id']
    scan = _req('GET', f'/api/3/scans/{scan_id}')
    assert scan['status'] == 'finished'
    assert scan['assets'] == 2
    assert scan['endTime'] is not None
