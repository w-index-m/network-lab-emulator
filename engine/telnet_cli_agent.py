"""
実Telnetサーバ（TCP/23）— CLIをTelnet越しに提供する

SSH CLIサーバ（`ssh_cli_agent.py`）のTelnet版。存在理由は2つ:

1. Nexposeエミュレーションの `netlab-telnet-cleartext`（平文管理プロトコル）
   という所見が、**実際には一度も成立しない死んだ判定だった**。
   検出条件が `state.telnet_enabled` を見ていたのに、この属性を
   立てるコードがどこにも無かった。
2. `transport input ssh telnet` が running-config に**ハードコード**
   されていて、コマンド自体が未実装だった。つまり「telnetを止める」
   ことができず、所見を直す手段が無かった。

実装したので、次の運用ループが本当に回る:

    transport input all   → 23番が開く → 平文管理の所見が立つ
    transport input ssh   → 23番が閉じる → 所見が消える

平文である（＝これが所見になる）ことが要点なので、暗号化はしない。

対応している範囲:
  - Username/Password のログインプロンプト（ローカルユーザ、
    無ければ admin/admin）。3回間違えると切断
  - IAC（Telnetオプション交渉）は WILL/WONT/DO/DONT を読み飛ばす。
    ECHO と SGA だけこちらから WILL を送る
  - パスワード入力中のエコー抑制
  - **`enable` による権限昇格**（SSH CLIサーバと同じ規則・同じ実装を
    `engine/ssh_cli_agent` から参照する）

対応していない範囲（実機との差）:
  - `line vty` 単位の同時接続数制限、`exec-timeout`、`access-class`
  - 端末制御（カーソル移動・履歴・TAB補完）
"""

import socket
import threading

from engine.loopback_alias import ensure_loopback_alias

TELNET_PORT = 23

IAC = 255
DONT, DO, WONT, WILL, SB, SE = 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA = 1, 3


class TelnetCliServer:
    """1装置ぶんのTelnet CLIリスナー"""

    def __init__(self, device_id, ip, state, run_command, port=TELNET_PORT):
        self.device_id = device_id
        self.ip = ip
        self.state = state
        self.run_command = run_command
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
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    # ── セッション ───────────────────────
    def _serve(self, sock):
        try:
            sock.settimeout(120)
            # ECHO と SGA はこちら（サーバ）が持つ、と宣言する
            sock.sendall(bytes([IAC, WILL, OPT_ECHO, IAC, WILL, OPT_SGA]))
            user = self._login(sock)
            if user is None:
                return
            from engine.ssh_cli_agent import initial_privilege
            self._shell(sock, initial_privilege(self.state, user))
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _login(self, sock):
        """ログインに成功したユーザ名を返す。失敗/切断なら None。"""
        from engine.netconf_agent import _device_users
        users = _device_users(self.state)
        sock.sendall(b'\r\n\r\nUser Access Verification\r\n\r\n')
        for _attempt in range(3):
            user = self._readline(sock, b'Username: ')
            if user is None:
                return None
            pw = self._readline(sock, b'Password: ', echo=False)
            if pw is None:
                return None
            if users.get(user) == pw:
                return user
            sock.sendall(b'\r\n% Login invalid\r\n\r\n')
        sock.sendall(b'% Bad passwords\r\n')
        return None

    def _shell(self, sock, privilege):
        priv = [privilege]                  # enable/disableで書き換える
        sock.sendall(b'\r\n' + _prompt(self.state, priv[0] >= 15) + b' ')
        while not self._stop.is_set():
            line = self._readline(sock, b'')
            if line is None:
                return
            if not line:
                sock.sendall(_prompt(self.state, priv[0] >= 15) + b' ')
                continue
            if line.lower() in ('exit', 'quit', 'logout') and \
                    getattr(self.state, 'mode', 'exec') == 'exec':
                return
            out = self._dispatch(sock, line, priv)
            if out:
                sock.sendall(_crlf(out) + b'\r\n')
            sock.sendall(_prompt(self.state, priv[0] >= 15) + b' ')

    def _dispatch(self, sock, line, priv):
        """1行ぶんのコマンドを、権限昇格まわりだけ横取りして実行する

        SSH CLIサーバ（`ssh_cli_agent.SshCliServer._dispatch`）と
        同じ規則・同じヘルパー関数を使う（実装を2箇所に持たない）。
        """
        from engine.ssh_cli_agent import is_privileged_only, check_enable_password
        low = line.lower()
        if low in ('enable', 'en'):
            if priv[0] >= 15:
                return self.run_command(self.device_id, line)
            pw = self._readline(sock, b'Password: ', echo=False)
            if pw is None:
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

    def _readline(self, sock, prompt, echo=True):
        """1行読む。Telnetのオプション交渉は読み飛ばす。

        戻り値は文字列。接続が切れたら None。
        """
        if prompt:
            sock.sendall(prompt)
        buf = bytearray()
        while True:
            try:
                data = sock.recv(1)
            except OSError:
                return None
            if not data:
                return None
            b = data[0]
            if b == IAC:
                if not self._skip_iac(sock):
                    return None
                continue
            if b in (13, 10):                       # CR / LF
                # CR LF / CR NUL の2バイト目を読み捨てる
                if b == 13:
                    try:
                        sock.recv(1)
                    except OSError:
                        pass
                sock.sendall(b'\r\n')
                return buf.decode(errors='replace').strip()
            if b in (8, 127):                       # Backspace
                if buf:
                    buf.pop()
                    if echo:
                        sock.sendall(b'\b \b')
                continue
            if b == 3:                              # Ctrl-C
                sock.sendall(b'^C\r\n')
                return ''
            if b < 32:
                continue
            buf.append(b)
            if echo:
                sock.sendall(bytes([b]))

    @staticmethod
    def _skip_iac(sock):
        """IAC に続くオプション交渉を読み飛ばす"""
        try:
            cmd = sock.recv(1)
            if not cmd:
                return False
            c = cmd[0]
            if c in (DO, DONT, WILL, WONT):
                sock.recv(1)                        # オプション番号
            elif c == SB:                           # サブネゴシエーション
                while True:
                    x = sock.recv(1)
                    if not x:
                        return False
                    if x[0] == IAC:
                        y = sock.recv(1)
                        if not y or y[0] == SE:
                            break
            return True
        except OSError:
            return False


def _prompt(state, privileged: bool = True):
    from engine.ssh_cli_agent import prompt_for
    return prompt_for(state, privileged).encode()


def _crlf(text):
    return str(text).replace('\r\n', '\n').replace('\n', '\r\n').encode()


# ── ライフサイクル ──────────────────────
_servers = {}


def telnet_allowed(state) -> bool:
    """vty の transport input に telnet が含まれているか

    既定は**含まれない**。実機の既定は telnet 許可だが、装置を作った
    だけで平文ポートが開くのは事故のもとなので、このエミュレータでは
    明示的に `transport input telnet` / `all` を設定したときだけ開く。
    （docs/telnet-cli-server.md に明記）
    """
    return 'telnet' in (getattr(state, 'vty_transport_input', None) or set())


def ensure_telnet_cli_agent(device_id, device_sessions, run_command,
                            port=TELNET_PORT):
    """telnet が許可されている装置のTelnetリスナーを起動する。
    許可が外れていれば止める。管理IPの変更にも追従する。"""
    state = device_sessions.get(device_id)
    running = _servers.get(device_id)

    if state is None or not telnet_allowed(state):
        if running is not None:
            stop_telnet_cli_agent(device_id)
        return False

    ip = _management_ip(state)
    if not ip:
        return False
    if running is not None:
        if running.ip == ip:
            return True
        running.stop()
        _servers.pop(device_id, None)
        print(f'[TELNET] {device_id} 管理IPが {running.ip} → {ip} に '
              f'変わったため待ち受けを張り直します')

    srv = TelnetCliServer(device_id, ip, state, run_command, port)
    try:
        srv.start()
    except OSError as e:
        print(f'[TELNET] {device_id} ({ip}:{port}) 起動失敗: {e}')
        return False
    _servers[device_id] = srv
    print(f'[TELNET] {device_id} ({ip}:{port}) 実CLIリスナーを起動しました')
    return True


def stop_telnet_cli_agent(device_id):
    srv = _servers.pop(device_id, None)
    if srv is None:
        return False
    srv.stop()
    print(f'[TELNET] {device_id} 実CLIリスナーを停止しました')
    return True


def _management_ip(state):
    for _name, info in (getattr(state, 'interfaces', {}) or {}).items():
        ip = info.get('ip') if isinstance(info, dict) else None
        if ip and ip != '127.0.0.1':
            return ip
    return ''
