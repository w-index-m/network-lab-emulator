"""
小さな SNMPv2c クライアント(標準ライブラリだけ。GETBULK で表を walk する / GET)

ニルヴァーナのスタンドアロン版が、実機から経路表(IP-FORWARD-MIB)と CPU(CISCO-PROCESS-MIB)を
定期的に直接取るために使う。値は Python の型(int / str / IP 文字列)で返す。
"""
import os
import socket

# ── BER ──────────────────────────────────────────
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(v):
    n = max(1, (v.bit_length() + 8) // 8)
    return _tlv(0x02, v.to_bytes(n, 'big', signed=True))


def _oid(oid):
    parts = [int(x) for x in oid.strip('.').split('.')]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(enc))
    return _tlv(0x06, body)


def _read(buf, i):
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[i:i + k], 'big')
        i += k
    return tag, buf[i:i + ln], i + ln


def _dec_oid(b):
    out = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            out.append(v)
            v = 0
    return '.'.join(map(str, out))


def _value(tag, b):
    if tag == 0x02:
        return int.from_bytes(b, 'big', signed=True)
    if tag in (0x41, 0x42, 0x43, 0x46):           # Counter32 / Gauge32 / TimeTicks / Counter64
        return int.from_bytes(b, 'big')
    if tag == 0x40:
        return '.'.join(str(x) for x in b)
    if tag == 0x04:
        try:
            return b.decode('utf-8')
        except UnicodeDecodeError:
            return b.hex()
    if tag == 0x06:
        return _dec_oid(b)
    return None                                    # NULL / noSuchObject / endOfMibView


class SnmpError(Exception):
    pass


def _request(host, community, pdu_tag, oids, port=161, timeout=2.0, retries=1, non_rep=0, max_rep=25):
    rid = int.from_bytes(os.urandom(3), 'big')
    vbs = b''.join(_tlv(0x30, _oid(o) + b'\x05\x00') for o in oids)
    pdu = _tlv(pdu_tag, _int(rid) + _int(non_rep) + _int(max_rep) + _tlv(0x30, vbs))
    msg = _tlv(0x30, _int(1) + _tlv(0x04, community.encode()) + pdu)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        for _ in range(retries + 1):
            s.sendto(msg, (host, port))
            try:
                while True:
                    data, _a = s.recvfrom(65535)
                    out = _parse(data, rid)
                    if out is not None:
                        return out
            except socket.timeout:
                continue
    raise SnmpError(f'{host}: 応答がありません(コミュニティ・ACL・UDP/{port} を確認)')


def _parse(data, rid):
    _t, body, _ = _read(data, 0)
    _t, _ver, i = _read(body, 0)
    _t, _comm, i = _read(body, i)
    _t, pdu, _ = _read(body, i)
    _t, rid_b, j = _read(pdu, 0)
    if int.from_bytes(rid_b, 'big', signed=True) != rid:
        return None
    _t, err, j = _read(pdu, j)
    _t, _eidx, j = _read(pdu, j)
    if int.from_bytes(err, 'big'):
        raise SnmpError(f'SNMP エラー {int.from_bytes(err, "big")}')
    _t, vbl, _ = _read(pdu, j)
    out, k = [], 0
    while k < len(vbl):
        _t, vb, k = _read(vbl, k)
        _t, ob, m = _read(vb, 0)
        vt, vv, _ = _read(vb, m)
        out.append((_dec_oid(ob), vt, _value(vt, vv)))
    return out


def get(host, community, oids, **kw):
    """{oid: 値}(無いものは入らない)"""
    rows = _request(host, community, 0xA0, list(oids), **kw)
    return {o: v for o, t, v in rows if t not in (0x80, 0x81, 0x82, 0x05)}


def walk(host, community, root, max_rows=20000, **kw):
    """root 以下を GETBULK でたどる → [(oid, 値)]"""
    root = root.strip('.')
    cur, out = root, []
    while len(out) < max_rows:
        rows = _request(host, community, 0xA5, [cur], **kw)
        if not rows:
            break
        done = False
        for o, t, v in rows:
            if t == 0x82 or not (o == root or o.startswith(root + '.')):
                done = True
                break
            out.append((o, v))
            cur = o
        if done:
            break
    return out


__all__ = ['get', 'walk', 'SnmpError']
