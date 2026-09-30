"""
実SSHサーバ（TCP/22）— 本物のSSHクライアントでCLIを叩く

これまでCLIは HTTP の `/api/cli` からしか叩けず、NETCONFサーバ(830)は
"netconf" サブシステムしか受け付けなかったため、**本物のSSHクライアントで
ログインして show コマンドを打つ手段が無かった**。

ここでは本物の uvicorn サーバを立て、paramiko のクライアントで
実際にログインして確認する。TestClient は使わない（SSHサーバは
別スレッドで動くので動きはするが、装置IPへの bind とイベントループの
都合があり、実サーバで確かめたほうが素直）。
"""

import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

paramiko = pytest.importorskip('paramiko')

PORT = 8124
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'ssh-cli'
DEV_IP = '10.224.0.1'
USER, PASSWORD = 'netadmin', 'Str0ngP@ss'


def _cli(cmd, dev=DEV):
    import urllib.request, json                          # noqa: E401
    req = urllib.request.Request(
        BASE + '/api/cli', method='POST',
        data=json.dumps({'device_id': dev, 'command': cmd}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-ssh') / 'server.log'
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


def _make_device(server, with_key=True):
    import urllib.request, json                          # noqa: E401
    req = urllib.request.Request(
        BASE + '/api/device', method='POST',
        data=json.dumps({'id': DEV, 'type': 'catalyst',
                         'hostname': 'SSH-SW'}).encode(),
        headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=30).read()
    cmds = ['configure terminal', 'interface GigabitEthernet1/0/1',
            'no switchport', f'ip address {DEV_IP} 255.255.255.0',
            'no shutdown', 'exit',
            f'username {USER} privilege 15 secret {PASSWORD}']
    if with_key:
        cmds.append('crypto key generate rsa modulus 2048')
    cmds.append('end')
    for c in cmds:
        _cli(c)
    time.sleep(2)


@pytest.fixture
def device(server):
    import urllib.request                                # noqa: E401
    req = urllib.request.Request(BASE + f'/api/device/{DEV}', method='DELETE')
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        pass
    _make_device(server)
    yield
    try:
        urllib.request.urlopen(
            urllib.request.Request(BASE + f'/api/device/{DEV}',
                                   method='DELETE'), timeout=30).read()
    except Exception:
        pass


def _connect(user=USER, password=PASSWORD, timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=user, password=password, timeout=timeout,
              allow_agent=False, look_for_keys=False)
    return c


def _shell_send(sh, cmd, wait=1.0):
    sh.send(cmd + '\n')
    time.sleep(wait)
    return sh.recv(65535).decode(errors='replace').replace('\r\n', '\n')


# ══════════════════════════════════════════
def test_ssh_is_not_listening_until_rsa_keys_exist(server):
    """実機同様、RSA鍵を作るまでSSHは上がらない"""
    import socket
    import urllib.request
    try:
        urllib.request.urlopen(
            urllib.request.Request(BASE + f'/api/device/{DEV}',
                                   method='DELETE'), timeout=30).read()
    except Exception:
        pass
    _make_device(server, with_key=False)
    s = socket.socket()
    s.settimeout(1)
    try:
        assert s.connect_ex((DEV_IP, 22)) != 0
    finally:
        s.close()


def test_login_with_the_device_local_user(device):
    c = _connect()
    try:
        assert c.get_transport().is_authenticated()
    finally:
        c.close()


def test_wrong_password_is_rejected(device):
    with pytest.raises(paramiko.AuthenticationException):
        _connect(password='definitely-wrong').close()


def test_exec_channel_runs_one_command(device):
    """ssh host "show ip interface brief" の形"""
    c = _connect()
    try:
        _in, out, _err = c.exec_command('show ip interface brief')
        text = out.read().decode()
    finally:
        c.close()
    assert 'Interface' in text
    assert DEV_IP in text


def test_interactive_shell_shows_the_prompt_and_follows_the_mode(device):
    c = _connect()
    try:
        sh = c.invoke_shell()
        time.sleep(1)
        banner = sh.recv(65535).decode(errors='replace')
        assert 'SSH-SW#' in banner

        out = _shell_send(sh, 'configure terminal')
        assert 'SSH-SW(config)#' in out
        out = _shell_send(sh, 'interface GigabitEthernet1/0/2')
        assert '(config-if)#' in out
        out = _shell_send(sh, 'end')
        assert 'SSH-SW#' in out
    finally:
        c.close()


def test_changes_made_over_ssh_are_the_same_state_as_the_web_cli(device):
    """SSHで変えた設定が、/api/cli 側からも見えること

    別実装にせず `/api/cli` と同じ経路を通しているかの確認。
    """
    c = _connect()
    try:
        sh = c.invoke_shell()
        time.sleep(1)
        sh.recv(65535)
        _shell_send(sh, 'configure terminal')
        _shell_send(sh, 'hostname RENAMED-OVER-SSH')
        _shell_send(sh, 'end')
    finally:
        c.close()
    assert 'hostname RENAMED-OVER-SSH' in _cli('show running-config')


def test_shell_and_exec_can_share_one_connection(device):
    """1本の接続で exec のあとに shell を開けること

    チャンネルを1本処理して transport ごと閉じていたため、
    2本目が "SSH session not active" で失敗していた。
    """
    c = _connect()
    try:
        _in, out, _err = c.exec_command('show ip interface brief')
        out.read()
        sh = c.invoke_shell()                    # 2本目
        time.sleep(1)
        assert 'SSH-SW#' in sh.recv(65535).decode(errors='replace')
    finally:
        c.close()


def test_exit_closes_the_session(device):
    c = _connect()
    try:
        sh = c.invoke_shell()
        time.sleep(1)
        sh.recv(65535)
        sh.send('exit\n')
        # 入力のエコーが残っているので、EOF になるまで読み切る
        deadline = time.time() + 5
        while time.time() < deadline:
            if sh.recv(65535) == b'':
                break
        else:                                    # pragma: no cover
            pytest.fail('exit でセッションが閉じない')
        assert sh.recv(65535) == b''             # EOF は戻らない
    finally:
        c.close()


def test_zeroize_stops_the_listener(device):
    import socket
    c = _connect()
    c.close()
    _cli('configure terminal')
    _cli('crypto key zeroize rsa')
    _cli('end')
    time.sleep(1.5)
    s = socket.socket()
    s.settimeout(1)
    try:
        assert s.connect_ex((DEV_IP, 22)) != 0
    finally:
        s.close()


def test_listener_follows_a_management_ip_change(device):
    """管理IPを変えたら、その新しいIPで待ち受け直すこと

    SNMPエージェントが起動時のIPに張り付いたままだった（同じ種類の
    不具合）ので、こちらは最初から追従させている。
    """
    import socket
    new_ip = '10.224.9.9'
    _cli('configure terminal')
    _cli('interface GigabitEthernet1/0/1')
    _cli(f'ip address {new_ip} 255.255.255.0')
    _cli('end')
    time.sleep(2)

    s = socket.socket()
    s.settimeout(2)
    try:
        assert s.connect_ex((new_ip, 22)) == 0, '新しいIPで待ち受けていない'
    finally:
        s.close()


def test_invalid_modulus_is_rejected(device):
    out = _cli('configure terminal') and None
    out = _cli('crypto key generate rsa modulus 99')
    assert 'Invalid modulus' in out
    _cli('end')


# ══════════════════════════════════════════
# 公開鍵認証（ip ssh pubkey-chain）
# ══════════════════════════════════════════
def _register_pubkey(username, key, wrap=64):
    """`ip ssh pubkey-chain` で公開鍵を登録する（実機と同じ手順）"""
    b64 = key.get_base64()
    cmds = ['configure terminal', 'ip ssh pubkey-chain',
           f'username {username}', 'key-string']
    for i in range(0, len(b64), wrap):
        cmds.append(b64[i:i + wrap])
    cmds += ['exit', 'exit', 'exit', 'end']
    out = ''
    for c in cmds:
        out = _cli(c) or out
    return out


def test_login_with_a_registered_public_key(device):
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                  allow_agent=False, look_for_keys=False)
        assert c.get_transport().is_authenticated()
    finally:
        c.close()


def test_an_unregistered_key_is_rejected(device):
    registered = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, registered)
    other = paramiko.RSAKey.generate(2048)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        with pytest.raises(paramiko.AuthenticationException):
            c.connect(DEV_IP, 22, username=USER, pkey=other, timeout=10,
                      allow_agent=False, look_for_keys=False)
    finally:
        c.close()


def test_password_auth_still_works_after_registering_a_key(device):
    """公開鍵を登録しても、パスワード認証が塞がれないこと"""
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)
    c = _connect()          # パスワードで接続
    try:
        assert c.get_transport().is_authenticated()
    finally:
        c.close()


def test_public_key_appears_in_running_config(device):
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)
    cfg = _cli('show running-config')
    assert 'ip ssh pubkey-chain' in cfg
    assert f' username {USER}' in cfg
    # base64本体が折り返されて出ること（連結すれば元の鍵と一致する）
    assert key.get_base64() in cfg.replace('\n', '').replace(' ', '')


def test_running_config_survives_a_key_added_over_ssh_password_login(device):
    """SSH経由で鍵を登録しても /api/cli 側の running-config に出ること

    公開鍵チェーンの設定自体は /api/cli 経由で行っているが、念のため
    別実装になっていないか running-config 側からも確かめる。
    """
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)
    # 別セッションでSSHログインして running-config を取得する
    c = _connect()
    try:
        _in, out, _err = c.exec_command('show running-config')
        cfg = out.read().decode()
    finally:
        c.close()
    assert 'ip ssh pubkey-chain' in cfg


def test_key_for_a_different_username_does_not_authenticate(device):
    """別ユーザ名に登録した鍵では、そのユーザ名でログインできないこと"""
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey('otheruser', key)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        with pytest.raises(paramiko.AuthenticationException):
            c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                      allow_agent=False, look_for_keys=False)
    finally:
        c.close()


def test_removing_the_username_revokes_its_keys(device):
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)

    for c in ('configure terminal', 'ip ssh pubkey-chain',
              f'no username {USER}', 'exit', 'end'):
        _cli(c)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        with pytest.raises(paramiko.AuthenticationException):
            c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                      allow_agent=False, look_for_keys=False)
    finally:
        c.close()


def _generate_ed25519_key():
    """paramiko.Ed25519Key に generate() が無いので cryptography 側で作る"""
    import io
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem))


def test_ed25519_keys_are_also_supported(device):
    key = _generate_ed25519_key()
    _register_pubkey(USER, key)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                  allow_agent=False, look_for_keys=False)
        assert c.get_transport().is_authenticated()
    finally:
        c.close()


def test_pasting_the_full_openssh_format_line_also_works(device):
    """`~/.ssh/id_rsa.pub` をそのまま貼っても通ること

    実機は base64本体だけを貼らせる方式だが、フルの
    "ssh-rsa AAAA... comment" 形式で貼っても受け付ける
    （utility寄りの意図的な緩和）。
    """
    key = paramiko.RSAKey.generate(2048)
    line = f'{key.get_name()} {key.get_base64()} test@laptop'
    for c in ('configure terminal', 'ip ssh pubkey-chain',
              f'username {USER}', 'key-string', line,
              'exit', 'exit', 'exit', 'end'):
        _cli(c)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                  allow_agent=False, look_for_keys=False)
        assert c.get_transport().is_authenticated()
    finally:
        c.close()


def test_no_ip_ssh_pubkey_chain_revokes_every_key(device):
    key = paramiko.RSAKey.generate(2048)
    _register_pubkey(USER, key)

    _cli('configure terminal')
    _cli('no ip ssh pubkey-chain')
    _cli('end')

    assert 'ip ssh pubkey-chain' not in _cli('show running-config')
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        with pytest.raises(paramiko.AuthenticationException):
            c.connect(DEV_IP, 22, username=USER, pkey=key, timeout=10,
                      allow_agent=False, look_for_keys=False)
    finally:
        c.close()


def test_garbage_key_data_is_rejected_with_an_error(device):
    for c in ('configure terminal', 'ip ssh pubkey-chain',
              f'username {USER}', 'key-string', 'not-a-valid-key!!'):
        _cli(c)
    out = _cli('exit')
    assert 'Invalid' in out
    _cli('exit'); _cli('exit'); _cli('end')


# ══════════════════════════════════════════
# enable（権限昇格）
# ══════════════════════════════════════════
LOWUSER, LOWPASS = 'lowpriv', 'LowP@ss'
ENABLE_SECRET = 'En@bleSecret1'


@pytest.fixture
def low_priv_device(device):
    """privilege 1 のユーザ + enable secret を足した装置

    `device` フィクスチャの USER(=netadmin) は privilege 15 のまま
    残す。既存のSSHテスト群がそれを前提にしているため。
    """
    for c in ('configure terminal', f'username {LOWUSER} privilege 1 '
             f'secret {LOWPASS}', f'enable secret {ENABLE_SECRET}', 'end'):
        _cli(c)
    yield


def _connect_shell(user, password, timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=user, password=password, timeout=timeout,
              allow_agent=False, look_for_keys=False)
    sh = c.invoke_shell()
    time.sleep(1)
    sh.recv(65535)                      # バナー/最初のプロンプトを捨てる
    return c, sh


def test_privileged_user_starts_at_hash_prompt(low_priv_device):
    """privilege 15 のユーザは最初から特権EXEC（#）"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=USER, password=PASSWORD, timeout=10,
              allow_agent=False, look_for_keys=False)
    sh = c.invoke_shell()
    time.sleep(1)
    banner = sh.recv(65535).decode(errors='replace')
    try:
        assert banner.rstrip().endswith('#')
    finally:
        c.close()


def test_low_privilege_user_starts_at_angle_bracket_prompt(low_priv_device):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=LOWUSER, password=LOWPASS, timeout=10,
              allow_agent=False, look_for_keys=False)
    sh = c.invoke_shell()
    time.sleep(1)
    banner = sh.recv(65535).decode(errors='replace')
    try:
        assert banner.rstrip().endswith('>')
    finally:
        c.close()


def test_user_exec_blocks_configure_terminal(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        out = _shell_send(sh, 'configure terminal')
        assert 'Invalid input' in out
        assert out.rstrip().endswith('>')          # モードは変わっていない
    finally:
        c.close()


def test_user_exec_blocks_show_running_config(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        out = _shell_send(sh, 'show running-config')
        assert 'Invalid input' in out
    finally:
        c.close()


def test_user_exec_still_allows_ordinary_show_commands(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        out = _shell_send(sh, 'show ip interface brief')
        assert DEV_IP in out
    finally:
        c.close()


def test_enable_with_correct_password_elevates(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        out = _shell_send(sh, 'enable')
        assert 'Password:' in out
        out = _shell_send(sh, ENABLE_SECRET)
        assert out.rstrip().endswith('#')
        # 昇格後はconfigureが通る
        out = _shell_send(sh, 'configure terminal')
        assert 'Invalid input' not in out
        assert '(config)#' in out
        _shell_send(sh, 'end')
    finally:
        c.close()


def test_enable_with_wrong_password_stays_unprivileged(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        _shell_send(sh, 'enable')
        out = _shell_send(sh, 'wrong-password')
        assert 'Access denied' in out
        assert out.rstrip().endswith('>')
        # まだ昇格していないので configure は依然として弾かれる
        out = _shell_send(sh, 'configure terminal')
        assert 'Invalid input' in out
    finally:
        c.close()


def test_disable_returns_to_user_exec(low_priv_device):
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        _shell_send(sh, 'enable')
        _shell_send(sh, ENABLE_SECRET)
        out = _shell_send(sh, 'disable')
        assert out.rstrip().endswith('>')
        out = _shell_send(sh, 'configure terminal')
        assert 'Invalid input' in out
    finally:
        c.close()


def test_enable_without_a_password_configured_is_refused(device):
    """enable secret/password のどちらも未設定なら実機同様拒否する"""
    for c in ('configure terminal', f'username {LOWUSER} privilege 1 '
             f'secret {LOWPASS}', 'end'):
        _cli(c)
    c, sh = _connect_shell(LOWUSER, LOWPASS)
    try:
        _shell_send(sh, 'enable')
        out = _shell_send(sh, 'anything')
        assert 'No password set' in out
        assert out.rstrip().endswith('>')
    finally:
        c.close()


def test_exec_channel_rejects_privileged_commands_for_low_privilege_user(
        low_priv_device):
    """execチャンネルは対話プロンプトが無いので、その場で拒否される
    （実機のvty exec-channelと同じ制約。enableで昇格する余地が無い）"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=LOWUSER, password=LOWPASS, timeout=10,
              allow_agent=False, look_for_keys=False)
    try:
        _in, out, _err = c.exec_command('show running-config')
        text = out.read().decode()
        assert 'Invalid input' in text
    finally:
        c.close()


def test_exec_channel_still_allows_ordinary_commands(low_priv_device):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(DEV_IP, 22, username=LOWUSER, password=LOWPASS, timeout=10,
              allow_agent=False, look_for_keys=False)
    try:
        _in, out, _err = c.exec_command('show ip interface brief')
        assert DEV_IP in out.read().decode()
    finally:
        c.close()


def test_no_local_users_falls_back_to_privileged_admin(server):
    """ローカルユーザが1つも無い装置は admin/admin で特権EXECに入る"""
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(
            BASE + '/api/device/priv-admin-fallback', method='DELETE'),
            timeout=30).read()
    except Exception:
        pass
    import urllib.request, json                          # noqa: E401
    urllib.request.urlopen(urllib.request.Request(
        BASE + '/api/device', method='POST',
        data=json.dumps({'id': 'priv-admin-fallback', 'type': 'catalyst',
                         'hostname': 'FALLBACK'}).encode(),
        headers={'Content-Type': 'application/json'}), timeout=30).read()
    ip = '10.224.8.8'
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {ip} 255.255.255.0',
              'no shutdown', 'exit',
              'crypto key generate rsa modulus 2048', 'end'):
        _cli(c, dev='priv-admin-fallback')
    time.sleep(2)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, 22, username='admin', password='admin', timeout=10,
                  allow_agent=False, look_for_keys=False)
        sh = c.invoke_shell()
        time.sleep(1)
        assert sh.recv(65535).decode(errors='replace').rstrip().endswith('#')
    finally:
        c.close()
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + '/api/device/priv-admin-fallback', method='DELETE'),
                timeout=30).read()
        except Exception:
            pass
