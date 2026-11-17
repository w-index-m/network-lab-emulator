"""
SNMPv3 の USM(User-based Security Model)とメッセージの組み立て・分解

RFC 3412(v3 メッセージ)・RFC 3414(USM、鍵の作り方・HMAC-96・DES)・RFC 3826(AES-128)・
RFC 7860(HMAC-SHA-2)。AES-192/256 は RFC が無く、鍵の伸ばし方が2通りある:
  - aes192 / aes256   … Blumenthal 方式(net-snmp の既定 -x AES192 / AES256)
  - aes192c / aes256c … Reeder 方式(Cisco IOS と同じ。net-snmp では -x AES192C / AES256C)

エミュレータの SNMP エージェント(engine/snmp_v3.py)と、ニルヴァーナ単体版のクライアント
(cyber-view-standalone/cvs/snmp_client.py)が同じファイルを使う(単体版の cvs/snmp_usm.py は
このファイルのコピー。ずれは tests/test_cyber_view_standalone.py が検出する)。

暗号は cryptography パッケージ(AES / DES)。認証だけなら標準ライブラリで動く。
"""
import hashlib
import hmac
import os

# ── 認証・暗号の種類 ─────────────────────────────────
# 認証: (ハッシュ関数, msgAuthenticationParameters の長さ)
AUTH = {
    'md5': (hashlib.md5, 12),          # usmHMACMD5AuthProtocol(HMAC-96)
    'sha': (hashlib.sha1, 12),         # usmHMACSHAAuthProtocol(HMAC-96)
    'sha224': (hashlib.sha224, 16),    # RFC 7860 usmHMAC128SHA224AuthProtocol
    'sha256': (hashlib.sha256, 24),    # usmHMAC192SHA256AuthProtocol
    'sha384': (hashlib.sha384, 32),    # usmHMAC256SHA384AuthProtocol
    'sha512': (hashlib.sha512, 48),    # usmHMAC384SHA512AuthProtocol
}
# 暗号: 必要な鍵の長さ(DES は鍵8 + pre-IV 8)
PRIV = {'des': 16, 'aes': 16, 'aes128': 16, 'aes192': 24, 'aes256': 32, 'aes192c': 24, 'aes256c': 32}

FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 0x01, 0x02, 0x04
SEC_MODEL_USM = 3

# usmStats(Report PDU で返すカウンタ)
OID_UNSUPPORTED_SEC_LEVELS = '1.3.6.1.6.3.15.1.1.1.0'
OID_NOT_IN_TIME_WINDOWS = '1.3.6.1.6.3.15.1.1.2.0'
OID_UNKNOWN_USER_NAMES = '1.3.6.1.6.3.15.1.1.3.0'
OID_UNKNOWN_ENGINE_IDS = '1.3.6.1.6.3.15.1.1.4.0'
OID_WRONG_DIGESTS = '1.3.6.1.6.3.15.1.1.5.0'
OID_DECRYPTION_ERRORS = '1.3.6.1.6.3.15.1.1.6.0'
REPORT_NAMES = {
    OID_UNSUPPORTED_SEC_LEVELS: 'unsupportedSecLevels', OID_NOT_IN_TIME_WINDOWS: 'notInTimeWindows',
    OID_UNKNOWN_USER_NAMES: 'unknownUserNames', OID_UNKNOWN_ENGINE_IDS: 'unknownEngineIDs',
    OID_WRONG_DIGESTS: 'wrongDigests', OID_DECRYPTION_ERRORS: 'decryptionErrors',
}
TIME_WINDOW = 150   # 秒(RFC 3414 3.2 7)


class UsmError(Exception):
    """report: Report PDU で返すべき usmStats の OID(None なら黙って捨てる)"""

    def __init__(self, msg, report=None, parsed=None):
        super().__init__(msg)
        self.report = report
        self.parsed = parsed


def norm_auth(name):
    n = (name or '').lower().replace('-', '').replace('_', '')
    n = {'hmacmd5': 'md5', 'hmacsha': 'sha', 'sha1': 'sha', 'sha2256': 'sha256', 'sha2384': 'sha384',
         'sha2512': 'sha512', 'sha2224': 'sha224'}.get(n, n)
    if n not in AUTH:
        raise ValueError(f'認証方式 {name} は使えません({", ".join(AUTH)})')
    return n


def norm_priv(name):
    n = (name or '').lower().replace('-', '').replace('_', '')
    n = {'cfb128aes128': 'aes128', 'aes': 'aes128', 'cbcdes': 'des'}.get(n, n)   # 3DES-EDE は未対応
    if n not in PRIV:
        raise ValueError(f'暗号方式 {name} は使えません({", ".join(PRIV)})')
    return n


# ── 鍵(RFC 3414 A.2)──────────────────────────────────
def password_to_key(auth, password):
    """パスワードを 1MB 分くり返して H を取る(Ku)"""
    h = AUTH[norm_auth(auth)][0]
    pw = password.encode() if isinstance(password, str) else bytes(password)
    if not pw:
        raise ValueError('パスワードが空です')
    count = 1048576
    data = (pw * (count // len(pw) + 1))[:count]
    return h(data).digest()


def localize_key(auth, ku, engine_id):
    """Kul = H(Ku || engineID || Ku)"""
    h = AUTH[norm_auth(auth)][0]
    return h(ku + engine_id + ku).digest()


def auth_key(auth, password, engine_id):
    return localize_key(auth, password_to_key(auth, password), engine_id)


def priv_key(auth, priv, password, engine_id):
    """暗号用の鍵(認証方式のハッシュで作り、足りなければ伸ばす)"""
    auth, priv = norm_auth(auth), norm_priv(priv)
    need = PRIV[priv]
    kul = localize_key(auth, password_to_key(auth, password), engine_id)
    h = AUTH[auth][0]
    if priv.endswith('c'):                       # Reeder(Cisco): 前の鍵をパスワードにして作り直す
        cur = kul
        while len(kul) < need:
            cur = localize_key(auth, password_to_key(auth, cur), engine_id)
            kul += cur
    else:                                        # Blumenthal: 今までの鍵全体の H を足す
        while len(kul) < need:
            kul += h(kul).digest()
    return kul[:need]


def hmac_digest(auth, key, msg):
    h, n = AUTH[norm_auth(auth)]
    return hmac.new(key, msg, h).digest()[:n]


# ── 暗号(DES-CBC / AES-CFB128)──────────────────────────
def _cipher(priv, key, iv):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if priv == 'des':
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            alg = algorithms.TripleDES(key[:8])     # 8 バイト鍵の TripleDES = 単 DES
        return Cipher(alg, modes.CBC(iv))
    return Cipher(algorithms.AES(key), modes.CFB(iv))


def encrypt(priv, key, boots, etime, plaintext, salt=None):
    """→ (暗号文, msgPrivacyParameters)"""
    priv = norm_priv(priv)
    if priv == 'des':
        salt = salt or (boots & 0xFFFFFFFF).to_bytes(4, 'big') + os.urandom(4)
        iv = bytes(a ^ b for a, b in zip(key[8:16], salt))
        pad = (-len(plaintext)) % 8
        e = _cipher(priv, key[:8], iv).encryptor()
        return e.update(plaintext + b'\x00' * pad) + e.finalize(), salt
    salt = salt or os.urandom(8)
    iv = (boots & 0xFFFFFFFF).to_bytes(4, 'big') + (etime & 0xFFFFFFFF).to_bytes(4, 'big') + salt
    e = _cipher(priv, key[:PRIV[priv]], iv).encryptor()
    return e.update(plaintext) + e.finalize(), salt


def decrypt(priv, key, boots, etime, salt, ciphertext):
    priv = norm_priv(priv)
    if len(salt) != 8:
        raise ValueError('msgPrivacyParameters が8バイトではありません')
    if priv == 'des':
        if len(ciphertext) % 8:
            raise ValueError('DES の暗号文が8の倍数ではありません')
        iv = bytes(a ^ b for a, b in zip(key[8:16], salt))
        d = _cipher(priv, key[:8], iv).decryptor()
        return d.update(ciphertext) + d.finalize()
    iv = (boots & 0xFFFFFFFF).to_bytes(4, 'big') + (etime & 0xFFFFFFFF).to_bytes(4, 'big') + salt
    d = _cipher(priv, key[:PRIV[priv]], iv).decryptor()
    return d.update(ciphertext) + d.finalize()


# ── BER(このファイルだけで完結させる)──────────────────
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(b)]) + b


def tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def ber_int(v):
    n = max(1, (v.bit_length() + 8) // 8) if v >= 0 else max(1, (v + 1).bit_length() // 8 + 1)
    return tlv(0x02, v.to_bytes(n, 'big', signed=True))


def octets(b):
    return tlv(0x04, b)


def read_tlv(buf, i):
    """→ (tag, 値の開始位置, 値の終わり)"""
    if i + 2 > len(buf):
        raise ValueError('短すぎる')
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[i:i + k], 'big')
        i += k
    if i + ln > len(buf):
        raise ValueError('長さが合わない')
    return tag, i, i + ln


def _int_at(buf, s, e):
    return int.from_bytes(buf[s:e], 'big', signed=True) if e > s else 0


# ── メッセージ ───────────────────────────────────────
def is_v3(data):
    try:
        _t, s, _e = read_tlv(data, 0)
        t, vs, ve = read_tlv(data, s)
        return t == 0x02 and _int_at(data, vs, ve) == 3
    except Exception:
        return False


def parse_message(data):
    """v3 メッセージを分解する(認証・復号はしない)。
    → dict(msg_id, max_size, flags, sec_model, engine_id, boots, time, user, auth_params,
           auth_offset, priv_params, msg_data(平文なら scopedPDU の SEQUENCE 全体 / 暗号なら暗号文), encrypted)"""
    t, s, e = read_tlv(data, 0)
    if t != 0x30:
        raise ValueError('SEQUENCE ではない')
    t, vs, ve = read_tlv(data, s)
    if _int_at(data, vs, ve) != 3:
        raise ValueError('v3 ではない')
    # msgGlobalData
    t, gs, ge = read_tlv(data, ve)
    t, a, b = read_tlv(data, gs)
    msg_id = _int_at(data, a, b)
    t, a, b = read_tlv(data, b)
    max_size = _int_at(data, a, b)
    t, a, b = read_tlv(data, b)
    flags = data[a] if b > a else 0
    t, a, b = read_tlv(data, b)
    sec_model = _int_at(data, a, b)
    # msgSecurityParameters(OCTET STRING の中に SEQUENCE)
    t, ss, se = read_tlv(data, ge)
    if t != 0x04:
        raise ValueError('msgSecurityParameters が OCTET STRING ではない')
    t, ps, pe = read_tlv(data, ss)
    t, a, b = read_tlv(data, ps)
    engine_id = data[a:b]
    t, a, b = read_tlv(data, b)
    boots = _int_at(data, a, b)
    t, a, b = read_tlv(data, b)
    etime = _int_at(data, a, b)
    t, a, b = read_tlv(data, b)
    user = data[a:b]
    t, aa, ab = read_tlv(data, b)
    auth_params = data[aa:ab]
    t, a, b = read_tlv(data, ab)
    priv_params = data[a:b]
    # msgData
    t, ds, de = read_tlv(data, se)
    if t == 0x04:
        msg_data, encrypted = data[ds:de], True
    else:
        msg_data, encrypted = data[se:de], False
    return {'msg_id': msg_id, 'max_size': max_size, 'flags': flags, 'sec_model': sec_model,
            'engine_id': engine_id, 'boots': boots, 'time': etime, 'user': user,
            'auth_params': auth_params, 'auth_offset': aa, 'priv_params': priv_params,
            'msg_data': msg_data, 'encrypted': encrypted}


def verify(data, parsed, auth, key):
    """受け取ったメッセージの HMAC を確かめる"""
    n = AUTH[norm_auth(auth)][1]
    if len(parsed['auth_params']) != n:
        return False
    off = parsed['auth_offset']
    blanked = data[:off] + b'\x00' * n + data[off + n:]
    return hmac.compare_digest(hmac_digest(auth, key, blanked), parsed['auth_params'])


def parse_scoped_pdu(sp):
    """scopedPDU → (contextEngineID, contextName, pdu_tag, pdu の中身 bytes)"""
    t, s, e = read_tlv(sp, 0)
    if t != 0x30:
        raise ValueError('scopedPDU が SEQUENCE ではない(復号に失敗?)')
    t, a, b = read_tlv(sp, s)
    ceid = sp[a:b]
    t, a, b = read_tlv(sp, b)
    cname = sp[a:b]
    t, a, b = read_tlv(sp, b)
    return ceid, cname, t, sp[a:b]


def scoped_pdu(context_engine_id, context_name, pdu_bytes):
    return tlv(0x30, octets(context_engine_id) + octets(context_name) + pdu_bytes)


def build_message(msg_id, flags, engine_id, boots, etime, user, scoped, auth=None, akey=None,
                  priv=None, pkey=None, max_size=65507, salt=None):
    """v3 メッセージを組み立てる。flags の auth/priv に合わせて署名・暗号化する"""
    priv_params = b''
    if flags & FLAG_PRIV:
        data_part, priv_params = encrypt(priv, pkey, boots, etime, scoped, salt=salt)
        msg_data = octets(data_part)
    else:
        msg_data = scoped
    n = AUTH[norm_auth(auth)][1] if flags & FLAG_AUTH else 0
    sp_pre = octets(engine_id) + ber_int(boots) + ber_int(etime) + octets(user)
    auth_tlv = octets(b'\x00' * n)
    sec = tlv(0x30, sp_pre + auth_tlv + octets(priv_params))
    glob = tlv(0x30, ber_int(msg_id) + ber_int(max_size) + octets(bytes([flags])) + ber_int(SEC_MODEL_USM))
    whole = tlv(0x30, ber_int(3) + glob + octets(sec) + msg_data)
    if n:
        pos = whole.find(sp_pre + auth_tlv)
        off = pos + len(sp_pre) + 2
        whole = whole[:off] + hmac_digest(auth, akey, whole) + whole[off + n:]
    return whole


# ── 送る側(クライアント)の手順 ──────────────────────────
class V3User:
    """クライアント側の資格情報。鍵は相手の engineID が分かってから作る"""

    def __init__(self, user, auth=None, auth_password=None, priv=None, priv_password=None):
        self.user = user
        self.auth = norm_auth(auth) if auth else None
        self.auth_password = auth_password
        self.priv = norm_priv(priv) if priv else None
        self.priv_password = priv_password
        if self.priv and not self.auth:
            raise ValueError('暗号(priv)には認証(auth)が必要です')
        self._keys = {}

    @property
    def flags(self):
        return (FLAG_AUTH if self.auth else 0) | (FLAG_PRIV if self.priv else 0)

    @property
    def level(self):
        return 'authPriv' if self.priv else ('authNoPriv' if self.auth else 'noAuthNoPriv')

    def keys(self, engine_id):
        if engine_id not in self._keys:
            ak = auth_key(self.auth, self.auth_password, engine_id) if self.auth else None
            pk = priv_key(self.auth, self.priv, self.priv_password, engine_id) if self.priv else None
            self._keys[engine_id] = (ak, pk)
        return self._keys[engine_id]


def discovery_message(msg_id, request_id):
    """engineID を知るための最初の要求(空の GET、reportable)"""
    pdu = tlv(0xA0, ber_int(request_id) + ber_int(0) + ber_int(0) + tlv(0x30, b''))
    return build_message(msg_id, FLAG_REPORTABLE, b'', 0, 0, b'', scoped_pdu(b'', b'', pdu))


def open_message(data, user):
    """受け取った応答を確かめて復号する → (parsed, scopedPDU bytes)。
    鍵は応答の engineID で作る(認証済みなら必ず確かめる)"""
    p = parse_message(data)
    if p['flags'] & FLAG_AUTH:
        if not user.auth:
            raise UsmError('応答が認証つきですが、こちらに認証の設定がありません')
        ak, pk = user.keys(p['engine_id'])
        if not verify(data, p, user.auth, ak):
            raise UsmError('応答の認証(HMAC)が合いません')
        if p['flags'] & FLAG_PRIV:
            sp = decrypt(user.priv, pk, p['boots'], p['time'], p['priv_params'], p['msg_data'])
            return p, sp
    return p, p['msg_data']
