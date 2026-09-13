"""
実SSHサーバ（TCP/22）— 装置のCLIをSSH越しに提供する

これまでCLIは HTTP の `/api/cli` からしか叩けず、NETCONFサーバ(830)は
"netconf" サブシステムしか受け付けなかったため、**本物のSSHクライアントで
ログインして show コマンドを打つ手段が無かった**。

Nexposeエミュレーションの認証スキャンが「ログインは本物だが、その後の
読み取りは DeviceState を直接見る」という中途半端な状態だったのも、
ここが無いのが理由。実SSHシェルを用意すれば、スキャナは実機と同じく
「SSHでログインして `show running-config` を実行して解析する」になる。

    $ ssh admin@10.0.0.1
    SW1#show running-config

実機と同じく **RSA鍵が生成されるまでSSHは起動しない**（IOSの
`crypto key generate rsa`）。装置ごとに勝手に22番を開けたりはしない。

対応している範囲:
  - password認証（装置のローカルユーザ。無ければ admin/admin）
  - shell チャンネル（対話）と exec チャンネル（`ssh host "show ..."`）
  - プロンプト、モード遷移、Ctrl-C / Ctrl-D、`exit` での切断

対応していない範囲（実機との差）:
  - 公開鍵認証、`enable` による権限昇格、AAA連携
  - 端末制御（カーソル移動・履歴・TAB補完）。行単位で読むだけ
  - ホスト鍵は**プロセス内で1本を共有**する（装置ごとに2048bitの鍵を
    生成すると装置作成が目に見えて遅くなるため。実機は装置ごとに別鍵）
"""

import socket
import threading

from engine.loopback_alias import ensure_loopback_alias

try:
    import paramiko
    _HAS_PARAMIKO = True
except Exception:                                       # pragma: no cover
    _HAS_PARAMIKO = False

SSH_PORT = 22

# プロセス内で共有するホスト鍵（生成は重いので1度だけ）
_host_key = None
_host_key_lock = threading.Lock()


def _shared_host_key():
    global _host_key
    with _host_key_lock:
        if _host_key is None:
            _host_key = paramiko.RSAKey.generate(2048)
        return _host_key


def prompt_for(state) -> str:
    """実機と同じ形のプロンプトを組み立てる"""
    host = getattr(state, 'hostname', 'Router')
    mode = getattr(state, 'mode', 'exec')
    if mode == 'exec':
        return f'{host}#'
    if mode == 'config':
        return f'{host}(config)#'
    if mode.startswith('config-'):
        return f'{host}({mode})#'
    return f'{host}#'


class _CliSshServer(paramiko.ServerInterface if _HAS_PARAMIKO else object):
    def __init__(self, valid_users):
        self.valid_users = valid_users
        self.authenticated_user = None
        # チャンネルごとの要求を覚える。1本のSSH接続で exec と shell が
        # 続けて開かれる（paramikoのSSHClientがまさにそうする）ので、
        # サーバ単位で1つしか持てないと2本目が壊れる。
        self.requests = {}          # chanid -> ('exec', cmd) / ('shell', None)
        self.events = {}            # chanid -> Event

    def check_auth_password(self, username, password):
        if self.valid_users.get(username) == password:
            self.authenticated_user = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            self.events.setdefault(chanid, threading.Event())
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def _mark(self, channel, kind, payload=None):
        cid = channel.get_id()
        self.requests[cid] = (kind, payload)
        self.events.setdefault(cid, threading.Event()).set()

    def check_channel_shell_request(self, channel):
        self._mark(channel, 'shell')
        return True

    def check_channel_exec_request(self, channel, command):
        # ssh host "show version" の形
        cmd = command.decode(errors='replace') \
            if isinstance(command, bytes) else str(command)
        self._mark(channel, 'exec', cmd)
        return True

    def wait(self, channel, timeout=10):
        ev = self.events.setdefault(channel.get_id(), threading.Event())
        ev.wait(timeout)
        return self.requests.get(channel.get_id())

    def check_channel_pty_request(self, *a, **kw):
        return True


class SshCliServer:
    """1装置ぶんのSSH CLIリスナー"""

    def __init__(self, device_id, ip, state, run_command, port=SSH_PORT):
        self.device_id = device_id
        self.ip = ip
        self.state = state
        self.run_command = run_command      # (device_id, command) -> str
        self.port = port
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        ensure_loopback_alias(self.ip)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.ip, self.port))
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                return
            # ポートスキャンは開けてすぐ閉じる。paramikoに渡すと
            # 毎回トレースバックを吐くので、静かに落とす
            # （NETCONFサーバと同じ扱い）
            if _peer_hung_up(client):
                try:
                    client.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    def _serve(self, sock):
        transport = paramiko.Transport(sock)
        transport.add_server_key(_shared_host_key())
        server = _CliSshServer(_device_users(self.state))
        try:
            transport.start_server(server=server)
            # 1本の接続で複数チャンネルが開かれる。1本目を処理して
            # transport ごと閉じてしまうと、続く shell が
            # "SSH session not active" で失敗する。
            while transport.is_active() and not self._stop.is_set():
                chan = transport.accept(20)
                if chan is None:
                    break
                threading.Thread(target=self._serve_channel,
                                 args=(server, chan), daemon=True).start()
        except Exception:
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def _serve_channel(self, server, chan):
        try:
            req = server.wait(chan)
            if req is None:
                chan.close()
                return
            kind, payload = req
            if kind == 'exec':
                self._run_exec(chan, payload)
            else:
                self._run_shell(chan)
        except Exception:
            try:
                chan.close()
            except Exception:
                pass

    def _run_exec(self, chan, command):
        """ssh host "show version" — 1コマンド実行して切る"""
        try:
            out = self.run_command(self.device_id, command)
            chan.sendall(_crlf(out) + b'\r\n')
            chan.send_exit_status(0)
        finally:
            chan.close()

    def _run_shell(self, chan):
        chan.sendall(b'\r\n')
        chan.sendall(prompt_for(self.state).encode() + b' ')
        buf = b''
        while not self._stop.is_set():
            try:
                data = chan.recv(1024)
            except Exception:
                return
            if not data:
                return
            for b in data:
                ch = bytes([b])
                if ch in (b'\r', b'\n'):
                    chan.sendall(b'\r\n')
                    line = buf.decode(errors='replace').strip()
                    buf = b''
                    if line.lower() in ('exit', 'quit', 'logout') and \
                            getattr(self.state, 'mode', 'exec') == 'exec':
                        chan.close()
                        return
                    if line:
                        out = self.run_command(self.device_id, line)
                        if out:
                            chan.sendall(_crlf(out) + b'\r\n')
                    chan.sendall(prompt_for(self.state).encode() + b' ')
                elif ch == b'\x7f':                     # Backspace
                    if buf:
                        buf = buf[:-1]
                        chan.sendall(b'\b \b')
                elif ch == b'\x03':                     # Ctrl-C
                    buf = b''
                    chan.sendall(b'^C\r\n')
                    chan.sendall(prompt_for(self.state).encode() + b' ')
                elif ch == b'\x04':                     # Ctrl-D
                    chan.close()
                    return
                else:
                    buf += ch
                    chan.sendall(ch)                    # エコー


def _crlf(text: str) -> bytes:
    """端末向けに改行を CRLF にする（生の \n だと段付きになる）"""
    return str(text).replace('\r\n', '\n').replace('\n', '\r\n').encode()


def _peer_hung_up(sock, timeout=0.3) -> bool:
    try:
        sock.settimeout(timeout)
        return sock.recv(1, socket.MSG_PEEK) == b''
    except OSError:
        return False
    finally:
        try:
            sock.settimeout(None)
        except OSError:
            pass


def _device_users(state) -> dict:
    """装置のローカルユーザ（無ければ admin/admin）

    NETCONFサーバと同じ規則。片方だけ通る/通らないことが無いよう、
    意図的に同じ実装を参照する。
    """
    from engine.netconf_agent import _device_users as nc_users
    return nc_users(state)


# ── ライフサイクル ──────────────────────
_servers = {}


def ensure_ssh_cli_agent(device_id, device_sessions, run_command,
                         port=SSH_PORT):
    """RSA鍵が生成されている装置のSSH CLIリスナーを起動する

    管理IPが変わった場合は張り直す。SNMPエージェントが同じ理由で
    起動時のIPに張り付いたままになっていたので、同じ轍を踏まないよう
    最初からここで面倒を見る。
    """
    if not _HAS_PARAMIKO:
        return False
    state = device_sessions.get(device_id)
    if state is None or not getattr(state, 'ssh_rsa_key', False):
        return False
    ip = _management_ip(state)
    if not ip:
        return False
    running = _servers.get(device_id)
    if running is not None:
        if running.ip == ip:
            return True
        running.stop()
        _servers.pop(device_id, None)
        print(f'[SSH] {device_id} 管理IPが {running.ip} → {ip} に変わったため '
              f'待ち受けを張り直します')
    srv = SshCliServer(device_id, ip, state, run_command, port)
    try:
        srv.start()
    except OSError as e:
        print(f'[SSH] {device_id} ({ip}:{port}) 起動失敗: {e}')
        return False
    _servers[device_id] = srv
    print(f'[SSH] {device_id} ({ip}:{port}) 実CLIリスナーを起動しました')
    return True


def stop_ssh_cli_agent(device_id):
    srv = _servers.pop(device_id, None)
    if srv is None:
        return False
    srv.stop()
    print(f'[SSH] {device_id} 実CLIリスナーを停止しました')
    return True


def _management_ip(state):
    for name, info in (getattr(state, 'interfaces', {}) or {}).items():
        ip = info.get('ip') if isinstance(info, dict) else None
        if ip and ip != '127.0.0.1':
            return ip
    return ''
