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
