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
  - **公開鍵認証**（`ip ssh pubkey-chain` で登録した鍵。RSA/Ed25519/
    ECDSA。DSSはクラス自体はあるがparamiko 4.0でサポートが落ちている）。
    装置側の CLI で登録した鍵しか通らない
  - **`enable` による権限昇格**。ログインしたローカルユーザの
    `privilege` が15未満なら user EXEC(`>`) で始まり、`enable secret`/
    `enable password` と一致すれば privileged EXEC(`#`) に上がる
  - shell チャンネル（対話）と exec チャンネル（`ssh host "show ..."`）
  - プロンプト、モード遷移、Ctrl-C / Ctrl-D、`exit` での切断

対応していない範囲（実機との差）:
  - AAA連携（TACACS+/RADIUSでの `enable` 認証）
  - user EXEC で塞ぐコマンドは代表的なもの（`configure terminal`・
    `show running/startup-config`・`write`/`copy`/`reload`/`debug`・
    `crypto`・`ip ssh pubkey-chain`）だけで、実機のコマンド単位の
    privilege-level割り当て表（多くのshowはlevel 0/1で見られる等）を
    忠実には再現していない
  - 端末制御（カーソル移動・履歴・TAB補完）。行単位で読むだけ
  - ホスト鍵は**プロセス内で1本を共有**する（装置ごとに2048bitの鍵を
    生成すると装置作成が目に見えて遅くなるため。実機は装置ごとに別鍵）
"""

import re
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


def prompt_for(state, privileged: bool = True) -> str:
    """実機と同じ形のプロンプトを組み立てる。

    privileged=False（user EXEC）なら `>`。config系のモードは
    privilege 15 でなければ入れない（`_is_privileged_only` が
    `configure terminal` 自体を塞ぐ）ので、config-* の間は常に `#`。
    """
    host = getattr(state, 'hostname', 'Router')
    mode = getattr(state, 'mode', 'exec')
    if mode == 'exec':
        return f'{host}#' if privileged else f'{host}>'
    if mode == 'config':
        return f'{host}(config)#'
    if mode.startswith('config-'):
        return f'{host}({mode})#'
    return f'{host}#' if privileged else f'{host}>'


# ── enable（権限昇格）─────────────────────
# 実機はVTY経由のログインだと既定で user EXEC(>) から始まり、
# privilege 15 未満のローカルユーザはそのまま。ローカルユーザの
# privilege が15（ローカルユーザ未設定時の admin/admin フォールバック
# 含む）なら最初から privileged EXEC(#) に入る。
#
# 「昇格すると何ができるようになるか」を実機の個々のコマンドの
# privilege-level割り当てに忠実に合わせるのは大掛かりなので、
# 代表的な「これが素通りしたらラボとして意味が無い」もの
# （設定投入・running-configの閲覧・reload等）だけを
# user EXEC でブロックする、という割り切り。
_PRIVILEGED_ONLY_PATTERNS = [re.compile(p, re.I) for p in (
    r'^conf(ig(ure)?)?(\s+t(erm(inal)?)?)?\s*$',
    r'^sh(ow)?\s+run(ning-config)?\b',
    r'^sh(ow)?\s+start(up-config)?\b',
    r'^write\b', r'^copy\b', r'^reload\b', r'^debug\b',
    r'^crypto\b', r'^ip\s+ssh\s+pubkey-chain\b',
)]


def is_privileged_only(command: str) -> bool:
    c = (command or '').strip()
    return any(p.match(c) for p in _PRIVILEGED_ONLY_PATTERNS)


def initial_privilege(state, username: str) -> int:
    """ログイン直後の権限レベル。

    ローカルユーザの `privilege` を見る。ローカルユーザが1つも
    無い装置は admin/admin にフォールバックする仕様
    （`_device_users` 参照）なので、その場合は特権15として扱う。
    """
    users = getattr(state, 'users', None) or []
    for u in users:
        if u.get('name') == username:
            return int(u.get('privilege', 1) or 1)
    if not users:
        return 15
    return 1


def check_enable_password(state, password: str):
    """enable secret/password と照合する。

    戻り値: True=一致 / False=不一致 / None=どちらも未設定
    （実機の "% No password set." に相当。呼び出し側で判定する）。
    secret が設定されていれば実機同様 secret を優先する。
    """
    secret = getattr(state, 'enable_secret', None)
    if secret:
        return password == secret
    plain = getattr(state, 'enable_password', None)
    if plain:
        return password == plain
    return None


class _CliSshServer(paramiko.ServerInterface if _HAS_PARAMIKO else object):
    def __init__(self, valid_users, authorized_keys=None):
        self.valid_users = valid_users
        # {username: ["ssh-rsa AAAA...", ...]}。`ip ssh pubkey-chain` で
        # 登録された鍵のみ（`engine/ssh_cli_agent._device_authorized_keys`）。
        self.authorized_keys = authorized_keys or {}
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

    def check_auth_publickey(self, username, key):
        """`ip ssh pubkey-chain` で登録された鍵とだけ照合する。

        paramikoの2段階公開鍵認証（まず「この鍵で通るか」を問い合わせ、
        通ればクライアントが署名して再送する）どちらの段階でもここが
        呼ばれる。署名の正当性自体はTransport側が検証するので、ここは
        「そのユーザ名にその鍵が登録されているか」だけを見ればよい。
        """
        wanted = f'{key.get_name()} {key.get_base64()}'
        if wanted in self.authorized_keys.get(username, []):
            self.authenticated_user = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        if self.authorized_keys.get(username):
            return 'publickey,password'
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
        server = _CliSshServer(_device_users(self.state),
                               _device_authorized_keys(self.state))
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
            privilege = initial_privilege(self.state, server.authenticated_user)
            if kind == 'exec':
                self._run_exec(chan, payload, privilege)
            else:
                self._run_shell(chan, privilege)
        except Exception:
            try:
                chan.close()
            except Exception:
                pass

    def _run_exec(self, chan, command, privilege):
        """ssh host "show version" — 1コマンド実行して切る

        execチャンネルは1発勝負で対話プロンプトを出せないので、
        privilege 15 未満なら特権専用コマンドをその場で拒否する
        （`enable` で昇格する余地が無いのは実機のvty exec-channelと
        同じ制約）。
        """
        try:
            if privilege < 15 and is_privileged_only(command):
                out = (f"% Invalid input detected at '^' marker.\n"
                       f'  {command}\n  ^')
            else:
                out = self.run_command(self.device_id, command)
            chan.sendall(_crlf(out) + b'\r\n')
            chan.send_exit_status(0)
        finally:
            chan.close()

    def _run_shell(self, chan, privilege):
        """対話シェル。1バイトずつ読む（下の注意点を参照）。

        `enable` はパスワードを訊くために、この関数の中から
        `_read_password()` でチャンネルをもう一度読む。以前は
        ここを `chan.recv(1024)` のチャンク読みにしていたところ、
        `ssh host` にヒアドキュメントで複数行を一気に流し込むような
        接続（実際 OpenSSH クライアントで再現した）だと、
        「enable」の行と次のパスワードの行が**同じチャンクに乗って
        届く**ことがあり、外側のループがチャンクごと読み切って
        しまうため、`_read_password` 側の recv() には何も残っておらず
        パスワード入力が空振り→次のプロンプトへ、という壊れ方をした。
        1バイトずつ読めば「今読むべき分だけ読む」が保証されるので、
        ネストした recv() 呼び出しがあっても取りこぼさない
        （`telnet_cli_agent.py` の `_readline` も同じ理由で1バイト読み）。
        """
        priv = [privilege]                  # enable/disableで書き換える
        chan.sendall(b'\r\n')
        chan.sendall(prompt_for(self.state, priv[0] >= 15).encode() + b' ')
        buf = b''
        while not self._stop.is_set():
            ch = self._recv_byte(chan)
            if ch is None:
                return
            if ch in (b'\r', b'\n'):
                chan.sendall(b'\r\n')
                line = buf.decode(errors='replace').strip()
                buf = b''
                if line.lower() in ('exit', 'quit', 'logout') and \
                        getattr(self.state, 'mode', 'exec') == 'exec':
                    chan.close()
                    return
                if line:
                    out = self._dispatch(chan, line, priv)
                    if out:
                        chan.sendall(_crlf(out) + b'\r\n')
                chan.sendall(
                    prompt_for(self.state, priv[0] >= 15).encode() + b' ')
            elif ch == b'\x7f':                     # Backspace
                if buf:
                    buf = buf[:-1]
                    chan.sendall(b'\b \b')
            elif ch == b'\x03':                     # Ctrl-C
                buf = b''
                chan.sendall(b'^C\r\n')
                chan.sendall(
                    prompt_for(self.state, priv[0] >= 15).encode() + b' ')
            elif ch == b'\x04':                     # Ctrl-D
                chan.close()
                return
            else:
                buf += ch
                chan.sendall(ch)                    # エコー

    @staticmethod
    def _recv_byte(chan):
        """1バイト読む。切断されたら None。"""
        try:
            data = chan.recv(1)
        except Exception:
            return None
        return data or None

    def _dispatch(self, chan, line, priv):
        """1行ぶんのコマンドを、権限昇格まわりだけ横取りして実行する"""
        low = line.lower()
        if low in ('enable', 'en'):
            if priv[0] >= 15:
                return self.run_command(self.device_id, line)   # 実機同様素通し
            pw = self._read_password(chan, 'Password: ')
            if pw is None:                  # 接続が切れた
                return ''
            ok = check_enable_password(self.state, pw)
            if ok is None:
                return '% No password set.'
            if ok:
                priv[0] = 15
                return ''
            return '% Access denied.'
        if low == 'disable':
            if getattr(self.state, 'mode', 'exec') == 'exec':
                priv[0] = 1
            return ''
        if priv[0] < 15 and is_privileged_only(line):
            return (f"% Invalid input detected at '^' marker.\n"
                   f'  {line}\n  ^')
        return self.run_command(self.device_id, line)

    def _read_password(self, chan, prompt):
        """`Password: ` プロンプトを出し、エコーせずに1行読む。

        1バイトずつ読む理由は `_run_shell` のdocstringを参照
        （チャンク読みだと同じTCPセグメントに乗った次の行を
        呼び出し元の recv() ごと横取りされ、パスワードが空振りする）。
        """
        chan.sendall(prompt.encode())
        buf = b''
        while not self._stop.is_set():
            ch = self._recv_byte(chan)
            if ch is None:
                return None
            if ch in (b'\r', b'\n'):
                chan.sendall(b'\r\n')
                return buf.decode(errors='replace')
            if ch == b'\x7f':
                buf = buf[:-1]
            elif ch == b'\x03':
                chan.sendall(b'^C\r\n')
                return ''
            else:
                buf += ch          # パスワードなのでエコーしない


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


def _device_authorized_keys(state) -> dict:
    """`ip ssh pubkey-chain` で登録された公開鍵（無ければ空）

    形は {username: ["ssh-rsa AAAA...", ...]}。app.py の
    `_finalize_ssh_pubkey` がこの形で state.ssh_pubkeys に積む。
    """
    return getattr(state, 'ssh_pubkeys', None) or {}


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
