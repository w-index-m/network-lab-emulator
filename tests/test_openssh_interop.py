"""
OpenSSHクライアントとの相互接続 — 実機の `ssh`/`sshpass` で確認する

`tests/test_ssh_cli_server.py` は paramiko のクライアントで確認して
いるが、paramiko はサーバ側と同じライブラリなので、**もう少し厳しい
実装であるOpenSSHのクライアントと繋がるか**は別に確かめる価値がある。

このテストで実際に見つけて直した不具合:

    `_run_shell` が `chan.recv(1024)` でチャンク読みしていたため、
    `ssh host <<EOF ... EOF` のようにヒアドキュメントで複数行を
    まとめて流し込む接続（OpenSSHクライアントがまさにそうする）だと、
    "enable" の行と次の行（パスワード）が**同じTCPセグメントに
    乗って届く**ことがあった。外側の読み取りループがそのチャンクを
    丸ごと読み切ってしまうため、`enable` の処理から呼ばれる
    `_read_password()` 側の recv() には何も残っておらず、パスワード
    入力が空振りしてすぐ次のプロンプトに戻ってしまい、本来
    パスワードのはずだった行が**次のコマンドとして誤実行**された。
    1バイトずつ読むよう修正した（`telnet_cli_agent.py` の
    `_readline` も元から同じ理由で1バイト読みだった）。

`ssh`/`sshpass` が入っていない環境ではスキップする
（未検証だった経緯は docs/ssh-cli-server.md §5 参照）。
"""

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

if not (shutil.which('ssh') and shutil.which('sshpass')):
    pytest.skip('ssh/sshpass が入っていない環境ではスキップ',
               allow_module_level=True)

PORT = 8126
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'openssh-interop'
DEV_IP = '10.227.0.1'
USER, PASSWORD = 'netadmin', 'Str0ngP@ss'
LOWUSER, LOWPASS = 'lowpriv', 'LowP@ss'
ENABLE_SECRET = 'En@bleSecret1'


def _api(method, path, body=None):
    import json
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return __import__('json').loads(raw) if raw else None


def _cli(cmd):
    return _api('POST', '/api/cli', {'device_id': DEV, 'command': cmd})['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-openssh') / 'server.log'
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


@pytest.fixture
def device(server):
    try:
        _api('DELETE', f'/api/device/{DEV}')
    except Exception:
        pass
    _api('POST', '/api/device',
        {'id': DEV, 'type': 'catalyst', 'hostname': 'OSSH-SW'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', f'ip address {DEV_IP} 255.255.255.0',
              'no shutdown', 'exit',
              f'username {USER} privilege 15 secret {PASSWORD}',
              f'username {LOWUSER} privilege 1 secret {LOWPASS}',
              f'enable secret {ENABLE_SECRET}',
              'crypto key generate rsa modulus 2048', 'end'):
        _cli(c)
    time.sleep(2)
    yield
    try:
        _api('DELETE', f'/api/device/{DEV}')
    except Exception:
        pass


_SSH_OPTS = ['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'ConnectTimeout=10']


def _ssh(user, password, remote_cmd=None, stdin_script=None, timeout=20):
    """本物の `ssh` バイナリを実行する。

    remote_cmd を渡せば execチャンネル（`ssh host "cmd"`）、
    stdin_script を渡せば `-tt` の対話シェルにヒアドキュメントを流す。
    """
    args = ['sshpass', '-p', password, 'ssh'] + _SSH_OPTS
    if stdin_script is not None:
        args += ['-tt', f'{user}@{DEV_IP}']
        return subprocess.run(args, input=stdin_script, capture_output=True,
                              text=True, timeout=timeout)
    args += [f'{user}@{DEV_IP}', remote_cmd]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _ssh_key(keyfile, user, remote_cmd=None, stdin_script=None, timeout=20):
    args = ['ssh', '-i', keyfile, '-o', 'IdentitiesOnly=yes'] + _SSH_OPTS
    if stdin_script is not None:
        args += ['-tt', '-o', 'BatchMode=yes', f'{user}@{DEV_IP}']
        return subprocess.run(args, input=stdin_script, capture_output=True,
                              text=True, timeout=timeout)
    args += ['-o', 'BatchMode=yes', f'{user}@{DEV_IP}', remote_cmd]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# ══════════════════════════════════════════
def test_password_auth_exec_channel(device):
    r = _ssh(USER, PASSWORD, remote_cmd='show ip interface brief')
    assert r.returncode == 0, r.stderr
    assert DEV_IP in r.stdout


def test_wrong_password_is_rejected(device):
    r = _ssh(USER, 'definitely-wrong', remote_cmd='show version')
    assert r.returncode != 0
    assert 'Permission denied' in r.stderr


def test_interactive_shell_changes_persist(device):
    """対話シェルでの変更が /api/cli 側からも見えること"""
    r = _ssh(USER, PASSWORD, stdin_script=(
        'configure terminal\nhostname RENAMED-BY-OPENSSH\nend\nexit\n'))
    assert 'RENAMED-BY-OPENSSH' in r.stdout
    assert 'hostname RENAMED-BY-OPENSSH' in _cli('show running-config')


def test_public_key_login(device, tmp_path):
    keyfile = str(tmp_path / 'id_rsa')
    subprocess.run(['ssh-keygen', '-t', 'rsa', '-b', '2048', '-f', keyfile,
                   '-N', '', '-q'], check=True, timeout=30)
    pub = open(keyfile + '.pub').read().split()[1]
    cmds = ['configure terminal', 'ip ssh pubkey-chain', f'username {USER}',
           'key-string']
    for i in range(0, len(pub), 64):
        cmds.append(pub[i:i + 64])
    cmds += ['exit', 'exit', 'exit', 'end']
    for c in cmds:
        _cli(c)

    r = _ssh_key(keyfile, USER, remote_cmd='show ip interface brief')
    assert r.returncode == 0, r.stderr
    assert DEV_IP in r.stdout


def test_unregistered_key_is_rejected(device, tmp_path):
    keyfile = str(tmp_path / 'id_rsa_unreg')
    subprocess.run(['ssh-keygen', '-t', 'rsa', '-b', '2048', '-f', keyfile,
                   '-N', '', '-q'], check=True, timeout=30)
    r = _ssh_key(keyfile, USER, remote_cmd='show version')
    assert r.returncode != 0
    assert 'Permission denied' in r.stderr


# ══════════════════════════════════════════
# enable — ヒアドキュメントで複数行を一気に流し込んだときの回帰
# ══════════════════════════════════════════
def test_enable_elevation_survives_a_heredoc_burst(device):
    """`enable` の直後にパスワードを含む複数行を一気に流し込んでも
    正しく処理されること（見つけた不具合そのものの再現）"""
    script = ('show ip interface brief\n'
             'enable\n'
             f'{ENABLE_SECRET}\n'
             'configure terminal\n'
             'hostname FIXED-BY-OPENSSH\n'
             'end\n'
             'disable\n'
             'exit\n')
    r = _ssh(LOWUSER, LOWPASS, stdin_script=script)
    out = r.stdout
    assert 'Password:' in out
    # 修正前はここで enable のパスワード行が空振りし、次のコマンドとして
    # 誤実行されて "% Invalid input detected" になっていた
    assert 'Invalid input' not in out
    assert '(config)#' in out
    assert 'FIXED-BY-OPENSSH(config)# end' in out
    assert 'hostname FIXED-BY-OPENSSH' in _cli('show running-config')


def test_wrong_enable_password_in_a_heredoc_burst(device):
    script = 'enable\nWrongPassword\nshow version\nconfigure terminal\nexit\n'
    r = _ssh(LOWUSER, LOWPASS, stdin_script=script)
    out = r.stdout
    assert 'Access denied' in out
    # 拒否された後もセッションは生きていて、通常コマンドは通ること
    assert 'Cisco IOS' in out
    # まだ昇格していないので configure は依然として弾かれる
    assert out.count("% Invalid input detected") >= 1


def test_user_exec_restriction_over_a_real_openssh_session(device):
    r = _ssh(LOWUSER, LOWPASS, remote_cmd='show running-config')
    assert 'Invalid input' in r.stdout
