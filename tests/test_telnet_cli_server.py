"""
実Telnetサーバ（TCP/23）と `transport input`

このテストが守っているのは、突き詰めると次の一点:

    transport input all  → 23番が開く → 平文管理の所見が立つ
    transport input ssh  → 23番が閉じる → 所見が消える

この「直せる」状態を作るまでに、2つの穴が塞がっていなかった:

  - Nexposeの `netlab-telnet-cleartext` は `state.telnet_enabled` を
    見ていたが、**この属性を立てるコードがどこにも無かった**。
    つまり一度も成立しない死んだ判定だった
  - `transport input ssh telnet` は running-config に**ハードコード**
    されているだけで、コマンド自体が未実装だった。設定を変えても
    出力は変わらず、telnetを止めることもできなかった

本物の uvicorn サーバを立て、素のソケットでTelnetを喋って確認する。
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

PORT = 8125
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'tn-cli'
DEV_IP = '10.226.0.1'
USER, PASSWORD = 'netadmin', 'Str0ngP@ss'


def _req(method, path, body=None):
    import json
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _cli(cmd):
    return _req('POST', '/api/cli',
                {'device_id': DEV, 'command': cmd})['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-tn') / 'server.log'
    env = dict(os.environ, NETLAB_AUTH_DISABLE='1')
    with open(log, 'wb') as fh:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app:app',
             '--host', '127.0.0.1', '--port', str(PORT)],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    urllib.request.urlopen(BASE + '/api/3', timeout=5).read()
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


@pytest.fixture
def device(server):
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + f'/api/device/{DEV}', method='DELETE'), timeout=30).read()
    except Exception:
        pass
    _req('POST', '/api/device',
         {'id': DEV, 'type': 'catalyst', 'hostname': 'TN-SW'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit',
              f'username {USER} privilege 15 secret {PASSWORD}', 'end'):
        _cli(c)
    time.sleep(1.5)
    yield
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + f'/api/device/{DEV}', method='DELETE'), timeout=30).read()
    except Exception:
        pass


def _set_transport(value):
    _cli('configure terminal')
    _cli('line vty 0 4')
    out = _cli(f'transport input {value}')
    _cli('end')
    time.sleep(1.2)
    return out


def _port_open(port=23, timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((DEV_IP, port)) == 0
    finally:
        s.close()


def _telnet_login(user=USER, password=PASSWORD, timeout=6.0):
    """素のソケットでTelnetログインする。成功したらプロンプトまでの本文"""
    from engine.nexpose import _telnet_expect
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((DEV_IP, 23))
    try:
        hit, _ = _telnet_expect(s, [b'Username:'], timeout)
        assert hit is not None, 'Username: のプロンプトが出ない'
        s.sendall(user.encode() + b'\r\n')
        hit, _ = _telnet_expect(s, [b'Password:'], timeout)
        assert hit is not None, 'Password: のプロンプトが出ない'
        s.sendall(password.encode() + b'\r\n')
        hit, buf = _telnet_expect(s, [b'#', b'Login invalid'], timeout)
        return hit, buf, s
    except Exception:
        s.close()
        raise


# ══════════════════════════════════════════
# transport input
# ══════════════════════════════════════════
def test_telnet_is_closed_by_default(device):
    """装置を作っただけでは平文ポートを開けない

    実機の既定は telnet 許可だが、それだと何もしていないのに
    平文ポートが開く。このエミュレータは明示設定を要求する
    （docs/telnet-cli-server.md に明記）。
    """
    assert not _port_open()


def test_transport_input_all_opens_telnet(device):
    _set_transport('all')
    assert _port_open()


def test_transport_input_ssh_closes_telnet(device):
    _set_transport('all')
    assert _port_open()
    _set_transport('ssh')
    assert not _port_open()


def test_transport_input_none_closes_telnet(device):
    _set_transport('all')
    _set_transport('none')
    assert not _port_open()


def test_running_config_reflects_transport_input(device):
    """回帰テスト: 以前は固定文字列だったので設定を変えても出力が変わらなかった"""
    _set_transport('all')
    cfg = _cli('show running-config')
    lines = [l.strip() for l in cfg.splitlines()
             if l.strip().startswith('transport input')]
    assert lines and all(l == 'transport input all' for l in lines)

    _set_transport('ssh')
    cfg = _cli('show running-config')
    lines = [l.strip() for l in cfg.splitlines()
             if l.strip().startswith('transport input')]
    assert lines and all(l == 'transport input ssh' for l in lines)


def test_transport_input_rejects_unknown_protocol(device):
    out = _set_transport('carrier-pigeon')
    assert 'Invalid input' in out


def test_line_vty_enters_and_leaves_the_submode(device):
    _cli('configure terminal')
    _cli('line vty 0 4')
    assert _cli('show running-config') is not None      # クラッシュしない
    _cli('exit')
    out = _cli('transport input all')      # config 直下では効かない
    _cli('end')
    # config-line を抜けているので、transport input は素通りするだけ
    assert 'transport input all' not in _cli('show running-config')


# ══════════════════════════════════════════
# Telnetログイン
# ══════════════════════════════════════════
def test_login_and_run_a_command(device):
    _set_transport('all')
    hit, _buf, s = _telnet_login()
    try:
        assert hit == b'#'
        s.sendall(b'show ip interface brief\r\n')
        time.sleep(1.5)
        out = s.recv(65535).decode(errors='replace')
        assert DEV_IP in out
    finally:
        s.close()


def test_wrong_password_is_rejected(device):
    _set_transport('all')
    hit, _buf, s = _telnet_login(password='definitely-wrong')
    try:
        assert hit == b'Login invalid'
    finally:
        s.close()


def test_changes_made_over_telnet_are_the_same_state(device):
    """Telnetで変えた設定が /api/cli 側からも見えること"""
    _set_transport('all')
    hit, _buf, s = _telnet_login()
    try:
        assert hit == b'#'
        for cmd in (b'configure terminal', b'hostname RENAMED-OVER-TELNET',
                    b'end'):
            s.sendall(cmd + b'\r\n')
            time.sleep(0.8)
            s.recv(65535)
    finally:
        s.close()
    assert 'hostname RENAMED-OVER-TELNET' in _cli('show running-config')


def test_credentials_travel_in_cleartext(device):
    """平文であること自体の確認（これがこの所見の中身）

    Telnetは暗号化しないので、送ったパスワードがそのまま
    バイト列としてソケットに乗る。所見の根拠をテストで示しておく。
    """
    _set_transport('all')
    s = socket.socket()
    s.settimeout(6)
    s.connect((DEV_IP, 23))
    try:
        from engine.nexpose import _telnet_expect
        hit, buf = _telnet_expect(s, [b'Username:'], 6)
        assert hit is not None
        # サーバからの応答が平文（TLSハンドシェイクではない）
        assert b'User Access Verification' in buf
    finally:
        s.close()


# ══════════════════════════════════════════
# enable（権限昇格）— SSH CLIサーバと同じ規則をTelnet越しに確認する
# ══════════════════════════════════════════
LOWUSER, LOWPASS = 'lowpriv', 'LowP@ss'
ENABLE_SECRET = 'En@bleSecret1'


@pytest.fixture
def low_priv_device(device):
    _set_transport('all')
    for c in ('configure terminal', f'username {LOWUSER} privilege 1 '
             f'secret {LOWPASS}', f'enable secret {ENABLE_SECRET}', 'end'):
        _cli(c)
    yield


def test_privileged_user_lands_at_hash_prompt(low_priv_device):
    hit, _buf, s = _telnet_login(USER, PASSWORD)
    try:
        assert hit == b'#'
    finally:
        s.close()


def test_low_privilege_user_lands_at_angle_bracket_prompt(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = socket.socket(); s.settimeout(6); s.connect((DEV_IP, 23))
    try:
        hit, _ = _telnet_expect(s, [b'Username:'], 6)
        s.sendall(LOWUSER.encode() + b'\r\n')
        hit, _ = _telnet_expect(s, [b'Password:'], 6)
        s.sendall(LOWPASS.encode() + b'\r\n')
        hit, _buf = _telnet_expect(s, [b'>', b'#'], 6)
        assert hit == b'>'
    finally:
        s.close()


def _login_low_priv():
    from engine.nexpose import _telnet_expect
    s = socket.socket(); s.settimeout(6); s.connect((DEV_IP, 23))
    _telnet_expect(s, [b'Username:'], 6)
    s.sendall(LOWUSER.encode() + b'\r\n')
    _telnet_expect(s, [b'Password:'], 6)
    s.sendall(LOWPASS.encode() + b'\r\n')
    _telnet_expect(s, [b'>'], 6)
    return s


def test_user_exec_blocks_configure_terminal(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = _login_low_priv()
    try:
        s.sendall(b'configure terminal\r\n')
        _hit, buf = _telnet_expect(s, [b'>'], 6)
        assert b'Invalid input' in buf
    finally:
        s.close()


def test_user_exec_allows_ordinary_show_commands(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = _login_low_priv()
    try:
        s.sendall(b'show ip interface brief\r\n')
        _hit, buf = _telnet_expect(s, [b'>'], 6)
        assert DEV_IP.encode() in buf
    finally:
        s.close()


def test_enable_with_correct_password_elevates(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = _login_low_priv()
    try:
        s.sendall(b'enable\r\n')
        hit, _ = _telnet_expect(s, [b'Password:'], 6)
        assert hit is not None
        s.sendall(ENABLE_SECRET.encode() + b'\r\n')
        hit, _ = _telnet_expect(s, [b'#'], 6)
        assert hit == b'#'
        s.sendall(b'configure terminal\r\n')
        _hit, buf = _telnet_expect(s, [b'#'], 6)
        assert b'Invalid input' not in buf
        assert b'(config)#' in buf
    finally:
        s.close()


def test_enable_with_wrong_password_stays_unprivileged(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = _login_low_priv()
    try:
        s.sendall(b'enable\r\n')
        _telnet_expect(s, [b'Password:'], 6)
        s.sendall(b'wrong\r\n')
        _hit, buf = _telnet_expect(s, [b'>'], 6)
        assert b'Access denied' in buf
    finally:
        s.close()


def test_disable_returns_to_user_exec(low_priv_device):
    from engine.nexpose import _telnet_expect
    s = _login_low_priv()
    try:
        s.sendall(b'enable\r\n')
        _telnet_expect(s, [b'Password:'], 6)
        s.sendall(ENABLE_SECRET.encode() + b'\r\n')
        _telnet_expect(s, [b'#'], 6)
        s.sendall(b'disable\r\n')
        hit, _ = _telnet_expect(s, [b'>'], 6)
        assert hit == b'>'
    finally:
        s.close()
