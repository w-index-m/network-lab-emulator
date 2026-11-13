"""
本物のRADIUS(RFC 2865 認証 / RFC 2866 アカウンティング)サーバとクライアント。

NetAttest EPS風の`netattest`装置が、自分のIPのUDP 1812/1813で実際に
待ち受ける。パケットは本物のRADIUS形式なので、FreeRADIUSの
`radtest`/`radclient`等の実クライアントからもそのまま問い合わせられる。

対応する認証方式:
  - PAP(User-Password属性の秘匿化 RFC 2865 5.2)
  - CHAP(CHAP-Password / CHAP-Challenge RFC 2865 5.3)
  - EAP-MD5(RFC 3748/3579。EAP-Message + Message-Authenticator +
    State によるAccess-Challengeの往復)

対応外(実機との差):
  - PEAP/EAP-TTLS/EAP-TLS(TLSトンネル)とMS-CHAPv2
  - Proxy-State・RADIUS over TLS(RadSec)・CoA(RFC 5176)

エミュレータ内の各装置(SSHログインのAAA・802.1X認証スイッチ・
無線コントローラ)は radius_request() で本物のUDPパケットを
自分の装置IPから送信し、ここのサーバ(またはサンドボックス外の
任意のRADIUSサーバ)に問い合わせる。
"""

import hashlib
import hmac
import os
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

from engine.loopback_alias import ensure_loopback_alias

AUTH_PORT = 1812
ACCT_PORT = 1813

ACCESS_REQUEST = 1
ACCESS_ACCEPT = 2
ACCESS_REJECT = 3
ACCOUNTING_REQUEST = 4
ACCOUNTING_RESPONSE = 5
ACCESS_CHALLENGE = 11

CODE_NAMES = {1: 'Access-Request', 2: 'Access-Accept', 3: 'Access-Reject',
              4: 'Accounting-Request', 5: 'Accounting-Response',
              11: 'Access-Challenge'}

# 属性番号
USER_NAME = 1
USER_PASSWORD = 2
CHAP_PASSWORD = 3
NAS_IP_ADDRESS = 4
NAS_PORT = 5
SERVICE_TYPE = 6
REPLY_MESSAGE = 18
STATE = 24
CLASS = 25
VENDOR_SPECIFIC = 26
SESSION_TIMEOUT = 27
CALLED_STATION_ID = 30
CALLING_STATION_ID = 31
NAS_IDENTIFIER = 32
ACCT_STATUS_TYPE = 40
ACCT_SESSION_ID = 44
ACCT_SESSION_TIME = 46
CHAP_CHALLENGE = 60
NAS_PORT_TYPE = 61
TUNNEL_TYPE = 64
TUNNEL_MEDIUM_TYPE = 65
EAP_MESSAGE = 79
MESSAGE_AUTHENTICATOR = 80
TUNNEL_PRIVATE_GROUP_ID = 81
NAS_PORT_ID = 87

SERVICE_TYPE_LOGIN = 1
SERVICE_TYPE_FRAMED = 2
SERVICE_TYPE_ADMINISTRATIVE = 6

CISCO_VENDOR_ID = 9
CISCO_AVPAIR = 1

ACCT_START = 1
ACCT_STOP = 2
ACCT_INTERIM = 3

EAP_REQUEST = 1
EAP_RESPONSE = 2
EAP_SUCCESS = 3
EAP_FAILURE = 4
EAP_TYPE_IDENTITY = 1
EAP_TYPE_MD5 = 4


# ── パケットのエンコード/デコード ─────────────────────────────

def encode_attrs(attrs: List[Tuple[int, bytes]]) -> bytes:
    out = b''
    for t, v in attrs:
        # RADIUS属性は最大253オクテット。EAP-Messageは分割して詰める
        for i in range(0, max(len(v), 1), 253):
            chunk = v[i:i + 253]
            out += struct.pack('!BB', t, len(chunk) + 2) + chunk
            if len(v) <= 253:
                break
    return out


def decode_attrs(data: bytes) -> List[Tuple[int, bytes]]:
    attrs = []
    i = 0
    while i + 2 <= len(data):
        t, ln = data[i], data[i + 1]
        if ln < 2 or i + ln > len(data):
            break
        attrs.append((t, data[i + 2:i + ln]))
        i += ln
    return attrs


def attr_get(attrs, t) -> Optional[bytes]:
    for at, v in attrs:
        if at == t:
            return v
    return None


def attr_join(attrs, t) -> bytes:
    """EAP-Messageのように複数属性に分割されたものを連結して返す。"""
    return b''.join(v for at, v in attrs if at == t)


def build_packet(code: int, ident: int, authenticator: bytes,
                 attrs: List[Tuple[int, bytes]]) -> bytes:
    body = encode_attrs(attrs)
    return struct.pack('!BBH', code, ident, 20 + len(body)) + authenticator + body


def parse_packet(data: bytes):
    if len(data) < 20:
        raise ValueError('RADIUS packet too short')
    code, ident, length = struct.unpack('!BBH', data[:4])
    if length > len(data) or length < 20:
        raise ValueError('bad RADIUS length')
    return code, ident, data[4:20], decode_attrs(data[20:length])


def encrypt_password(password: bytes, secret: bytes, authenticator: bytes) -> bytes:
    """RFC 2865 5.2 User-Passwordの秘匿化。"""
    if len(password) % 16:
        password += b'\x00' * (16 - len(password) % 16)
    out = b''
    last = authenticator
    for i in range(0, len(password), 16):
        b = hashlib.md5(secret + last).digest()
        c = bytes(x ^ y for x, y in zip(password[i:i + 16], b))
        out += c
        last = c
    return out


def decrypt_password(hidden: bytes, secret: bytes, authenticator: bytes) -> bytes:
    out = b''
    last = authenticator
    for i in range(0, len(hidden), 16):
        b = hashlib.md5(secret + last).digest()
        out += bytes(x ^ y for x, y in zip(hidden[i:i + 16], b))
        last = hidden[i:i + 16]
    return out.rstrip(b'\x00')


def response_authenticator(code, ident, req_auth, attr_bytes, secret) -> bytes:
    length = 20 + len(attr_bytes)
    return hashlib.md5(struct.pack('!BBH', code, ident, length) + req_auth
                       + attr_bytes + secret).digest()


def _with_message_authenticator(code, ident, auth_field, attrs, secret) -> List[Tuple[int, bytes]]:
    """Message-Authenticator(RFC 3579 3.2)を付けた属性列を返す。
    auth_field はRequestならRequest Authenticator、Responseなら
    対応するRequestのRequest Authenticator。"""
    attrs = [a for a in attrs if a[0] != MESSAGE_AUTHENTICATOR]
    attrs.append((MESSAGE_AUTHENTICATOR, b'\x00' * 16))
    pkt = build_packet(code, ident, auth_field, attrs)
    mac = hmac.new(secret, pkt, hashlib.md5).digest()
    attrs[-1] = (MESSAGE_AUTHENTICATOR, mac)
    return attrs


def verify_message_authenticator(data: bytes, secret: bytes,
                                 auth_field: Optional[bytes] = None) -> bool:
    code, ident, auth, attrs = parse_packet(data)
    got = attr_get(attrs, MESSAGE_AUTHENTICATOR)
    if got is None:
        return True
    zeroed = [(t, b'\x00' * 16 if t == MESSAGE_AUTHENTICATOR else v) for t, v in attrs]
    pkt = build_packet(code, ident, auth_field if auth_field is not None else auth, zeroed)
    return hmac.compare_digest(hmac.new(secret, pkt, hashlib.md5).digest(), got)


def vsa_cisco_avpair(value: str) -> bytes:
    sub = struct.pack('!BB', CISCO_AVPAIR, len(value) + 2) + value.encode()
    return struct.pack('!I', CISCO_VENDOR_ID) + sub


def parse_cisco_avpairs(attrs) -> List[str]:
    out = []
    for t, v in attrs:
        if t != VENDOR_SPECIFIC or len(v) < 6:
            continue
        if struct.unpack('!I', v[:4])[0] != CISCO_VENDOR_ID:
            continue
        i = 4
        while i + 2 <= len(v):
            st, sl = v[i], v[i + 1]
            if st == CISCO_AVPAIR:
                out.append(v[i + 2:i + sl].decode(errors='replace'))
            i += max(sl, 2)
    return out


def eap_packet(code: int, ident: int, etype: Optional[int] = None, data: bytes = b'') -> bytes:
    if etype is None:
        return struct.pack('!BBH', code, ident, 4)
    return struct.pack('!BBHB', code, ident, 5 + len(data), etype) + data


def parse_eap(data: bytes):
    if len(data) < 4:
        return None
    code, ident, length = struct.unpack('!BBH', data[:4])
    etype = data[4] if length > 4 and len(data) > 4 else None
    return code, ident, etype, data[5:length]


def _int_attr(n: int) -> bytes:
    return struct.pack('!I', n)


def _tagged_int(tag: int, n: int) -> bytes:
    # Tunnel-Type/Tunnel-Medium-Type はタグ1オクテット+値3オクテット(RFC 2868)
    return struct.pack('!B', tag) + struct.pack('!I', n)[1:]


# ── サーバ ─────────────────────────────────────────────────────

class RadiusServer:
    """装置1台分のRADIUSサーバ(NetAttest EPS相当)。

    clients/users の実体は装置側(state.netattest)の辞書をそのまま
    参照する(複製しない)。ここは受信・応答と、ランタイムの
    ログ/EAPセッション状態だけを持つ。
    """

    def __init__(self, device_id: str, state, ip: str):
        self.device_id = device_id
        self.state = state
        self.ip = ip
        self._socks: List[socket.socket] = []
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._eap_sessions: Dict[bytes, dict] = {}
        self.stats = {'access_requests': 0, 'access_accepts': 0,
                      'access_rejects': 0, 'access_challenges': 0,
                      'accounting_requests': 0, 'dropped_unknown_client': 0,
                      'bad_authenticator': 0}

    @property
    def cfg(self) -> dict:
        return self.state.netattest

    def start(self) -> None:
        ensure_loopback_alias(self.ip)
        for port, handler in ((AUTH_PORT, self._handle_auth), (ACCT_PORT, self._handle_acct)):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.ip, port))
            s.settimeout(0.5)
            self._socks.append(s)
            t = threading.Thread(target=self._loop, args=(s, handler),
                                 name=f'radius-{self.device_id}-{port}', daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass

    def _loop(self, sock, handler):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                reply = handler(data, addr)
            except Exception as e:  # 壊れたパケットでサーバを落とさない
                self._log('-', addr[0], 'error', f'malformed packet: {e}')
                continue
            if reply:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass

    def _client_secret(self, src_ip: str) -> Optional[bytes]:
        c = self.cfg.get('clients', {}).get(src_ip)
        return c['secret'].encode() if c else None

    def _log(self, user, nas, result, reason, method=''):
        logs = self.cfg.setdefault('auth_log', [])
        logs.append({'time': time.strftime('%Y/%m/%d %H:%M:%S'), 'user': user,
                     'nas': nas, 'method': method, 'result': result, 'reason': reason})
        del logs[:-200]

    # 認証(1812)
    def _handle_auth(self, data: bytes, addr) -> Optional[bytes]:
        code, ident, req_auth, attrs = parse_packet(data)
        if code != ACCESS_REQUEST:
            return None
        secret = self._client_secret(addr[0])
        if secret is None:
            # 実機同様、未登録クライアントからの要求は黙って捨てる
            self.stats['dropped_unknown_client'] += 1
            self._log('-', addr[0], 'drop', 'unknown RADIUS client')
            return None
        if not verify_message_authenticator(data, secret):
            self.stats['bad_authenticator'] += 1
            self._log('-', addr[0], 'drop', 'invalid Message-Authenticator')
            return None
        self.stats['access_requests'] += 1
        username = (attr_get(attrs, USER_NAME) or b'').decode(errors='replace')
        eap = attr_join(attrs, EAP_MESSAGE)
        if eap:
            return self._handle_eap(ident, req_auth, attrs, eap, secret, addr[0])

        user = self.cfg.get('users', {}).get(username)
        pw_attr = attr_get(attrs, USER_PASSWORD)
        chap = attr_get(attrs, CHAP_PASSWORD)
        method = 'PAP' if pw_attr is not None else ('CHAP' if chap is not None else '-')
        ok = False
        reason = ''
        if user is None:
            reason = 'user not found'
        elif user.get('disabled'):
            reason = 'user disabled'
        elif pw_attr is not None:
            ok = decrypt_password(pw_attr, secret, req_auth) == user['password'].encode()
            reason = '' if ok else 'password mismatch'
        elif chap is not None:
            challenge = attr_get(attrs, CHAP_CHALLENGE) or req_auth
            expect = hashlib.md5(chap[:1] + user['password'].encode() + challenge).digest()
            ok = hmac.compare_digest(expect, chap[1:])
            reason = '' if ok else 'password mismatch'
        else:
            reason = 'no credentials'
        return self._final(ok, ident, req_auth, secret, username, addr[0], method, reason, user)

    def _accept_attrs(self, user: dict) -> List[Tuple[int, bytes]]:
        attrs: List[Tuple[int, bytes]] = []
        priv = user.get('privilege')
        if priv is not None:
            attrs.append((SERVICE_TYPE, _int_attr(
                SERVICE_TYPE_ADMINISTRATIVE if int(priv) >= 15 else SERVICE_TYPE_LOGIN)))
            attrs.append((VENDOR_SPECIFIC, vsa_cisco_avpair(f'shell:priv-lvl={int(priv)}')))
        vlan = user.get('vlan')
        if vlan:
            # 動的VLAN割り当て(RFC 3580): Tunnel-Type=VLAN(13),
            # Tunnel-Medium-Type=IEEE-802(6), Tunnel-Private-Group-ID=<vlan>
            attrs.append((TUNNEL_TYPE, _tagged_int(0, 13)))
            attrs.append((TUNNEL_MEDIUM_TYPE, _tagged_int(0, 6)))
            attrs.append((TUNNEL_PRIVATE_GROUP_ID, str(vlan).encode()))
        if user.get('group'):
            attrs.append((CLASS, f'group={user["group"]}'.encode()))
        if user.get('session_timeout'):
            attrs.append((SESSION_TIMEOUT, _int_attr(int(user['session_timeout']))))
        return attrs

    def _final(self, ok, ident, req_auth, secret, username, nas, method, reason,
               user, extra: Optional[List[Tuple[int, bytes]]] = None, eap: bool = False) -> bytes:
        if ok:
            self.stats['access_accepts'] += 1
            code = ACCESS_ACCEPT
            attrs = self._accept_attrs(user)
            self._log(username, nas, 'accept', '', method)
        else:
            self.stats['access_rejects'] += 1
            code = ACCESS_REJECT
            attrs = [(REPLY_MESSAGE, b'Authentication failed')]
            self._log(username, nas, 'reject', reason, method)
        attrs += extra or []
        if eap:
            attrs = _with_message_authenticator(code, ident, req_auth, attrs, secret)
        body = encode_attrs(attrs)
        return build_packet(code, ident,
                            response_authenticator(code, ident, req_auth, body, secret), attrs)

    def _handle_eap(self, ident, req_auth, attrs, eap_data, secret, nas) -> bytes:
        parsed = parse_eap(eap_data)
        state_attr = attr_get(attrs, STATE)
        username = (attr_get(attrs, USER_NAME) or b'').decode(errors='replace')
        if not parsed:
            return self._final(False, ident, req_auth, secret, username, nas, 'EAP',
                               'malformed EAP', None, eap=True)
        code, eid, etype, edata = parsed
        if code == EAP_RESPONSE and etype == EAP_TYPE_IDENTITY:
            identity = edata.decode(errors='replace') or username
            challenge = os.urandom(16)
            st = os.urandom(16)
            self._eap_sessions[st] = {'identity': identity, 'challenge': challenge,
                                      'eap_id': (eid + 1) & 0xff, 'ts': time.time()}
            req = eap_packet(EAP_REQUEST, (eid + 1) & 0xff, EAP_TYPE_MD5,
                             bytes([len(challenge)]) + challenge)
            out = [(EAP_MESSAGE, req), (STATE, st)]
            out = _with_message_authenticator(ACCESS_CHALLENGE, ident, req_auth, out, secret)
            body = encode_attrs(out)
            self.stats['access_challenges'] += 1
            return build_packet(ACCESS_CHALLENGE, ident,
                                response_authenticator(ACCESS_CHALLENGE, ident, req_auth, body, secret),
                                out)
        sess = self._eap_sessions.pop(state_attr, None) if state_attr else None
        if code == EAP_RESPONSE and etype == EAP_TYPE_MD5 and sess:
            identity = sess['identity']
            user = self.cfg.get('users', {}).get(identity)
            vlen = edata[0] if edata else 0
            got = edata[1:1 + vlen]
            ok, reason = False, ''
            if user is None:
                reason = 'user not found'
            elif user.get('disabled'):
                reason = 'user disabled'
            else:
                expect = hashlib.md5(bytes([eid]) + user['password'].encode()
                                     + sess['challenge']).digest()
                ok = hmac.compare_digest(expect, got)
                reason = '' if ok else 'password mismatch'
            result = eap_packet(EAP_SUCCESS if ok else EAP_FAILURE, eid)
            return self._final(ok, ident, req_auth, secret, identity, nas, 'EAP-MD5',
                               reason, user, extra=[(EAP_MESSAGE, result)], eap=True)
        # 未対応のEAPタイプ(PEAP/TLS等)へのNAKや、State無しの要求
        reason = 'unsupported EAP type' if etype not in (EAP_TYPE_MD5, EAP_TYPE_IDENTITY) \
            else 'EAP session state not found'
        return self._final(False, ident, req_auth, secret, username, nas, 'EAP', reason, None,
                           extra=[(EAP_MESSAGE, eap_packet(EAP_FAILURE, eid))], eap=True)

    # アカウンティング(1813)
    def _handle_acct(self, data: bytes, addr) -> Optional[bytes]:
        code, ident, req_auth, attrs = parse_packet(data)
        if code != ACCOUNTING_REQUEST:
            return None
        secret = self._client_secret(addr[0])
        if secret is None:
            self.stats['dropped_unknown_client'] += 1
            return None
        expect = hashlib.md5(data[:4] + b'\x00' * 16 + data[20:] + secret).digest()
        if not hmac.compare_digest(expect, req_auth):
            self.stats['bad_authenticator'] += 1
            return None
        self.stats['accounting_requests'] += 1
        st = attr_get(attrs, ACCT_STATUS_TYPE)
        st_name = {1: 'Start', 2: 'Stop', 3: 'Interim-Update'}.get(
            struct.unpack('!I', st)[0] if st and len(st) == 4 else 0, 'Unknown')
        sess_time = attr_get(attrs, ACCT_SESSION_TIME)
        recs = self.cfg.setdefault('acct_log', [])
        recs.append({
            'time': time.strftime('%Y/%m/%d %H:%M:%S'),
            'status': st_name,
            'user': (attr_get(attrs, USER_NAME) or b'').decode(errors='replace'),
            'nas': addr[0],
            'session_id': (attr_get(attrs, ACCT_SESSION_ID) or b'').decode(errors='replace'),
            'nas_port_id': (attr_get(attrs, NAS_PORT_ID) or b'').decode(errors='replace'),
            'calling_station': (attr_get(attrs, CALLING_STATION_ID) or b'').decode(errors='replace'),
            'session_time': struct.unpack('!I', sess_time)[0] if sess_time and len(sess_time) == 4 else None,
        })
        del recs[:-200]
        return build_packet(ACCOUNTING_RESPONSE, ident,
                            response_authenticator(ACCOUNTING_RESPONSE, ident, req_auth, b'', secret), [])


_servers: Dict[str, RadiusServer] = {}
_lock = threading.Lock()


def radius_server_ip(state) -> Optional[str]:
    for name in ('port1', 'mgmt'):
        info = state.interfaces.get(name)
        if info and info.get('ip') and info.get('status', 'up') != 'down':
            return info['ip']
    return None


def ensure_radius_server(device_id: str, state) -> Optional[RadiusServer]:
    """`service radius`有効な装置のサーバを(必要なら再)起動する。
    IPが変わっていれば張り直す。無効なら停止する。"""
    with _lock:
        cur = _servers.get(device_id)
        want = getattr(state, 'netattest', {}).get('service_enabled') and radius_server_ip(state)
        if cur and (not want or cur.ip != want or cur.state is not state):
            cur.stop()
            _servers.pop(device_id, None)
            cur = None
        if want and not cur:
            srv = RadiusServer(device_id, state, want)
            try:
                srv.start()
            except OSError as e:
                srv.stop()
                print(f'[RADIUS] {device_id} ({want}:{AUTH_PORT}) 起動失敗: {e}')
                return None
            _servers[device_id] = srv
            print(f'[RADIUS] {device_id} ({want}:{AUTH_PORT}/{ACCT_PORT}) 実UDPサーバ起動')
            cur = srv
        return cur


def stop_radius_server(device_id: str) -> None:
    with _lock:
        srv = _servers.pop(device_id, None)
    if srv:
        srv.stop()


def get_radius_server(device_id: str) -> Optional[RadiusServer]:
    return _servers.get(device_id)


# ── クライアント(NAS側) ────────────────────────────────────────

class RadiusResult:
    def __init__(self, status: str, attrs=None, detail: str = ''):
        self.status = status          # 'accept' | 'reject' | 'timeout' | 'error'
        self.attrs = attrs or []
        self.detail = detail

    @property
    def accepted(self) -> bool:
        return self.status == 'accept'

    @property
    def vlan(self) -> Optional[str]:
        v = attr_get(self.attrs, TUNNEL_PRIVATE_GROUP_ID)
        if not v:
            return None
        # タグ付き(先頭0x01-0x1F)の場合は先頭1オクテットがタグ
        if v[:1] and v[0] < 0x20:
            v = v[1:]
        return v.decode(errors='replace')

    @property
    def privilege(self) -> Optional[int]:
        for av in parse_cisco_avpairs(self.attrs):
            if av.startswith('shell:priv-lvl='):
                try:
                    return int(av.split('=', 1)[1])
                except ValueError:
                    pass
        st = attr_get(self.attrs, SERVICE_TYPE)
        if st and len(st) == 4 and struct.unpack('!I', st)[0] == SERVICE_TYPE_ADMINISTRATIVE:
            return 15
        return None

    @property
    def reply_message(self) -> str:
        v = attr_get(self.attrs, REPLY_MESSAGE)
        return v.decode(errors='replace') if v else ''


def _bind_socket(source_ip: Optional[str], timeout: float) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    if source_ip:
        ensure_loopback_alias(source_ip)
        try:
            s.bind((source_ip, 0))
        except OSError:
            pass  # 送信元を固定できなくても送れはする(サーバ側で未登録扱いになる)
    return s


def _transact(server: str, port: int, secret: bytes, code: int, attrs, source_ip,
              timeout: float, retries: int, eap: bool = False):
    s = _bind_socket(source_ip, timeout)
    try:
        ident = os.urandom(1)[0]
        req_auth = os.urandom(16)
        if eap:
            attrs = _with_message_authenticator(code, ident, req_auth, attrs, secret)
        pkt = build_packet(code, ident, req_auth, attrs)
        for _ in range(retries + 1):
            try:
                s.sendto(pkt, (server, port))
                while True:
                    data, _addr = s.recvfrom(4096)
                    rcode, rid, rauth, rattrs = parse_packet(data)
                    if rid != ident:
                        continue
                    expect = response_authenticator(rcode, rid, req_auth, data[20:], secret)
                    if not hmac.compare_digest(expect, rauth):
                        return None, 'response authenticator mismatch (shared secret?)'
                    if eap and not verify_message_authenticator(data, secret, req_auth):
                        return None, 'bad Message-Authenticator'
                    return (rcode, rattrs), ''
            except socket.timeout:
                continue
            except OSError as e:
                return None, str(e)
        return None, 'timeout'
    finally:
        s.close()


def _nas_attrs(nas_ip, nas_port_id, calling_station, nas_identifier):
    out = []
    if nas_ip:
        out.append((NAS_IP_ADDRESS, socket.inet_aton(nas_ip)))
    if nas_identifier:
        out.append((NAS_IDENTIFIER, nas_identifier.encode()))
    if nas_port_id:
        out.append((NAS_PORT_ID, nas_port_id.encode()))
    if calling_station:
        out.append((CALLING_STATION_ID, calling_station.encode()))
    return out


def radius_authenticate(server: str, secret: str, username: str, password: str,
                        method: str = 'pap', port: int = AUTH_PORT,
                        source_ip: Optional[str] = None, nas_port_id: str = '',
                        calling_station: str = '', nas_identifier: str = '',
                        service_type: Optional[int] = None,
                        timeout: float = 2.0, retries: int = 1) -> RadiusResult:
    """本物のRADIUS問い合わせを1回行う。method: 'pap' | 'chap' | 'eap-md5'。"""
    sec = secret.encode()
    base = [(USER_NAME, username.encode())]
    base += _nas_attrs(source_ip, nas_port_id, calling_station, nas_identifier)
    if service_type:
        base.append((SERVICE_TYPE, _int_attr(service_type)))

    if method == 'eap-md5':
        eid = 1
        attrs = base + [(EAP_MESSAGE, eap_packet(EAP_RESPONSE, eid, EAP_TYPE_IDENTITY,
                                                  username.encode()))]
        res, err = _transact(server, port, sec, ACCESS_REQUEST, attrs, source_ip,
                             timeout, retries, eap=True)
        if res is None:
            return RadiusResult('timeout' if err == 'timeout' else 'error', detail=err)
        code, rattrs = res
        if code != ACCESS_CHALLENGE:
            return RadiusResult('accept' if code == ACCESS_ACCEPT else 'reject', rattrs,
                                'no EAP challenge')
        parsed = parse_eap(attr_join(rattrs, EAP_MESSAGE))
        st = attr_get(rattrs, STATE)
        if not parsed or parsed[2] != EAP_TYPE_MD5:
            return RadiusResult('reject', rattrs, 'server did not offer EAP-MD5')
        _c, chal_id, _t, edata = parsed
        challenge = edata[1:1 + edata[0]]
        digest = hashlib.md5(bytes([chal_id]) + password.encode() + challenge).digest()
        attrs = base + [(EAP_MESSAGE, eap_packet(EAP_RESPONSE, chal_id, EAP_TYPE_MD5,
                                                  bytes([16]) + digest))]
        if st:
            attrs.append((STATE, st))
        res, err = _transact(server, port, sec, ACCESS_REQUEST, attrs, source_ip,
                             timeout, retries, eap=True)
        if res is None:
            return RadiusResult('timeout' if err == 'timeout' else 'error', detail=err)
        code, rattrs = res
        return RadiusResult('accept' if code == ACCESS_ACCEPT else 'reject', rattrs)

    # PAPはRequest Authenticatorで秘匿化するため_transactは使わず自前で組み立てる
    s = _bind_socket(source_ip, timeout)
    try:
        ident = os.urandom(1)[0]
        req_auth = os.urandom(16)
        attrs = list(base)
        if method == 'chap':
            chap_id = os.urandom(1)
            challenge = os.urandom(16)
            attrs.append((CHAP_PASSWORD, chap_id + hashlib.md5(
                chap_id + password.encode() + challenge).digest()))
            attrs.append((CHAP_CHALLENGE, challenge))
        else:
            attrs.append((USER_PASSWORD, encrypt_password(password.encode(), sec, req_auth)))
        pkt = build_packet(ACCESS_REQUEST, ident, req_auth, attrs)
        for _ in range(retries + 1):
            try:
                s.sendto(pkt, (server, port))
                while True:
                    data, _addr = s.recvfrom(4096)
                    rcode, rid, rauth, rattrs = parse_packet(data)
                    if rid != ident:
                        continue
                    expect = response_authenticator(rcode, rid, req_auth, data[20:], sec)
                    if not hmac.compare_digest(expect, rauth):
                        return RadiusResult('error', detail='response authenticator mismatch (shared secret?)')
                    return RadiusResult('accept' if rcode == ACCESS_ACCEPT else 'reject', rattrs)
            except socket.timeout:
                continue
            except OSError as e:
                return RadiusResult('error', detail=str(e))
        return RadiusResult('timeout', detail='timeout')
    finally:
        s.close()


def radius_accounting(server: str, secret: str, status: int, username: str,
                      session_id: str, port: int = ACCT_PORT,
                      source_ip: Optional[str] = None, nas_port_id: str = '',
                      calling_station: str = '', session_time: Optional[int] = None,
                      timeout: float = 2.0, retries: int = 1) -> str:
    """Accounting-Request(Start/Stop)を送る。'ok' | 'timeout' | エラー文字列。"""
    sec = secret.encode()
    attrs = [(ACCT_STATUS_TYPE, _int_attr(status)), (USER_NAME, username.encode()),
             (ACCT_SESSION_ID, session_id.encode())]
    attrs += _nas_attrs(source_ip, nas_port_id, calling_station, '')
    if session_time is not None:
        attrs.append((ACCT_SESSION_TIME, _int_attr(int(session_time))))
    s = _bind_socket(source_ip, timeout)
    try:
        ident = os.urandom(1)[0]
        body = encode_attrs(attrs)
        hdr = struct.pack('!BBH', ACCOUNTING_REQUEST, ident, 20 + len(body))
        req_auth = hashlib.md5(hdr + b'\x00' * 16 + body + sec).digest()
        pkt = hdr + req_auth + body
        for _ in range(retries + 1):
            try:
                s.sendto(pkt, (server, port))
                while True:
                    data, _addr = s.recvfrom(4096)
                    rcode, rid, rauth, _r = parse_packet(data)
                    if rid != ident:
                        continue
                    if rcode == ACCOUNTING_RESPONSE and hmac.compare_digest(
                            response_authenticator(rcode, rid, req_auth, data[20:], sec), rauth):
                        return 'ok'
                    return 'bad response'
            except socket.timeout:
                continue
            except OSError as e:
                return str(e)
        return 'timeout'
    finally:
        s.close()
