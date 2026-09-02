"""
実SNMP(UDP/161, v2c)エージェント

engine/protocols.py の SnmpAgent（内部シミュレーション、get/getnext/walk済み
実装あり）を、本物のSNMP v2cワイヤプロトコル(BER符号化)でUDP応答できるように
ラップする。net-snmpの snmpget/snmpwalk や Prometheus snmp_exporter から
実際にポーリングできる。

各装置のmanagement IP（interfaces中で最初に見つかったIP）をループバックの
エイリアスIPとして追加し、そのIP:161でリッスンする（同一プロセス内で複数
装置を実際に別IPとして区別するため）。
"""

import asyncio
import struct
import subprocess
from typing import Optional

# ── BER (Basic Encoding Rules) ──────────────────────────────
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2
PDU_SET = 0xA3
PDU_GETBULK = 0xA5


def _encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_length(len(value)) + value


def _encode_int(n: int, tag: int = TAG_INTEGER) -> bytes:
    if n == 0:
        body = b'\x00'
    else:
        # 符号無し値(Counter32等)も含め、最上位ビットが立つ場合は0x00でパディング
        nbytes = max(1, (n.bit_length() + 8) // 8)
        body = n.to_bytes(nbytes, 'big', signed=False)
        if body[0] & 0x80 and tag == TAG_INTEGER:
            body = b'\x00' + body
    return _tlv(tag, body)


def _encode_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip('.').split('.') if p != '']
    if len(parts) < 2:
        parts = (parts + [0, 0])[:2]
    first = parts[0] * 40 + parts[1]
    out = bytearray([first])
    for p in parts[2:]:
        if p == 0:
            out.append(0)
            continue
        chunk = []
        v = p
        while v:
            chunk.insert(0, v & 0x7F)
            v >>= 7
        for i in range(len(chunk) - 1):
            out.append(chunk[i] | 0x80)
        out.append(chunk[-1])
    return _tlv(TAG_OID, bytes(out))


def _encode_ipaddress(ip: str) -> bytes:
    parts = [int(x) for x in ip.split('.')]
    return _tlv(TAG_IPADDRESS, bytes(parts))


def _encode_value(vtype: str, value) -> bytes:
    if vtype == 'INTEGER':
        return _encode_int(int(value))
    if vtype == 'STRING':
        return _tlv(TAG_OCTET_STRING, str(value).encode('utf-8'))
    if vtype == 'OID':
        return _encode_oid(str(value))
    if vtype == 'Timeticks':
        return _encode_int(int(value), TAG_TIMETICKS)
    if vtype == 'Counter32':
        return _encode_int(int(value), TAG_COUNTER32)
    if vtype == 'Gauge32':
        return _encode_int(int(value), TAG_GAUGE32)
    if vtype == 'IpAddress':
        return _encode_ipaddress(str(value))
    # 未知の型は文字列として返す
    return _tlv(TAG_OCTET_STRING, str(value).encode('utf-8'))


class _BerReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_tlv(self):
        tag = self.data[self.pos]
        self.pos += 1
        ln = self.data[self.pos]
        self.pos += 1
        if ln & 0x80:
            nbytes = ln & 0x7F
            ln = int.from_bytes(self.data[self.pos:self.pos + nbytes], 'big')
            self.pos += nbytes
        value = self.data[self.pos:self.pos + ln]
        self.pos += ln
        return tag, value

    def eof(self):
        return self.pos >= len(self.data)


def _decode_int(value: bytes) -> int:
    return int.from_bytes(value, 'big', signed=True) if value else 0


def _decode_oid(value: bytes) -> str:
    if not value:
        return ''
    first = value[0]
    parts = [first // 40, first % 40]
    i = 1
    cur = 0
    while i < len(value):
        b = value[i]
        cur = (cur << 7) | (b & 0x7F)
        if not (b & 0x80):
            parts.append(cur)
            cur = 0
        i += 1
    return '.'.join(str(p) for p in parts)


def decode_request(data: bytes):
    """SNMPv2cリクエストをパースし、
    (version, community, pdu_type, request_id, param2, param3, oids[]) を返す。
    GetBulkRequestの場合 param2=non-repeaters, param3=max-repetitions。
    それ以外は param2=error-status, param3=error-index（リクエストでは常に0）。
    """
    r = _BerReader(data)
    tag, seq = r.read_tlv()
    if tag != TAG_SEQUENCE:
        raise ValueError('not a SEQUENCE')
    rr = _BerReader(seq)
    _, ver_b = rr.read_tlv()
    version = _decode_int(ver_b)
    _, comm_b = rr.read_tlv()
    community = comm_b.decode('utf-8', errors='replace')
    pdu_tag, pdu_body = rr.read_tlv()

    pr = _BerReader(pdu_body)
    _, reqid_b = pr.read_tlv()
    request_id = _decode_int(reqid_b)
    _, p2_b = pr.read_tlv()
    param2 = _decode_int(p2_b)
    _, p3_b = pr.read_tlv()
    param3 = _decode_int(p3_b)
    _, vbl_b = pr.read_tlv()

    oids = []
    vr = _BerReader(vbl_b)
    while not vr.eof():
        _, vb_b = vr.read_tlv()
        vbr = _BerReader(vb_b)
        _, oid_b = vbr.read_tlv()
        oids.append(_decode_oid(oid_b))

    return version, community, pdu_tag, request_id, param2, param3, oids


def encode_response(version: int, community: str, request_id: int,
                     varbinds: list, error_status: int = 0, error_index: int = 0) -> bytes:
    """varbinds: [(oid, type_or_None, value_or_None)]
    type_or_None が None の場合は noSuchObject を返す。"""
    vb_items = []
    for oid, vtype, value in varbinds:
        oid_bytes = _encode_oid(oid)
        if vtype is None:
            val_bytes = _tlv(TAG_NO_SUCH_OBJECT, b'')
        elif vtype == 'ENDOFMIBVIEW':
            val_bytes = _tlv(TAG_END_OF_MIB_VIEW, b'')
        else:
            val_bytes = _encode_value(vtype, value)
        vb_items.append(_tlv(TAG_SEQUENCE, oid_bytes + val_bytes))
    vbl = _tlv(TAG_SEQUENCE, b''.join(vb_items))

    pdu_body = (
        _encode_int(request_id) +
        _encode_int(error_status) +
        _encode_int(error_index) +
        vbl
    )
    pdu = _tlv(PDU_RESPONSE, pdu_body)

    msg_body = (
        _encode_int(version) +
        _tlv(TAG_OCTET_STRING, community.encode('utf-8')) +
        pdu
    )
    return _tlv(TAG_SEQUENCE, msg_body)


# ── UDP エージェント本体 ──────────────────────────────────
class SnmpDeviceProtocol(asyncio.DatagramProtocol):
    def __init__(self, device_id: str, snmp_agent):
        self.device_id = device_id
        self.snmp_agent = snmp_agent
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            version, community, pdu_tag, request_id, p2, p3, oids = decode_request(data)
        except Exception:
            return

        varbinds = []
        if pdu_tag == PDU_GET:
            for oid in oids:
                result = self.snmp_agent.get(self.device_id, oid, community)
                if result in (None, 'AUTH_FAIL'):
                    varbinds.append((oid, None, None))
                else:
                    o, t, v = result
                    varbinds.append((o, t, v))
        elif pdu_tag == PDU_GETNEXT:
            for oid in oids:
                result = self.snmp_agent.getnext(self.device_id, oid)
                if result is None:
                    varbinds.append((oid, 'ENDOFMIBVIEW', None))
                else:
                    o, t, v = result
                    varbinds.append((o, t, v))
        elif pdu_tag == PDU_GETBULK:
            max_reps = max(1, p3)
            for oid in oids:
                cur = oid
                for _ in range(max_reps):
                    result = self.snmp_agent.getnext(self.device_id, cur)
                    if result is None:
                        varbinds.append((cur, 'ENDOFMIBVIEW', None))
                        break
                    o, t, v = result
                    varbinds.append((o, t, v))
                    cur = o
        else:
            return  # SetRequest等は未対応

        response = encode_response(version, community, request_id, varbinds)
        if self.transport:
            self.transport.sendto(response, addr)


def _ensure_loopback_alias(ip: str):
    """SNMPをそのIPで待ち受けられるよう、ループバックにエイリアスIPを追加する。
    既に存在する場合は何もしない（失敗は無視）。"""
    if ip in ('', '127.0.0.1'):
        return
    try:
        subprocess.run(
            ['ip', 'addr', 'add', f'{ip}/32', 'dev', 'lo', 'scope', 'host'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _pick_management_ip(state) -> Optional[str]:
    """装置のinterfacesから、SNMP待ち受けに使うIPを1つ選ぶ。
    mgmt系インターフェース(mgmt0等)があれば優先、無ければ最初に見つかったIP。"""
    ifaces = getattr(state, 'interfaces', {})
    mgmt = None
    first = None
    for name, info in ifaces.items():
        ip = info.get('ip') if isinstance(info, dict) else None
        if not ip:
            continue
        if first is None:
            first = ip
        if 'mgmt' in name.lower():
            mgmt = ip
    return mgmt or first


async def start_all_snmp_agents(device_sessions: dict, snmp_agent, port: int = 161):
    """device_sessions内の全装置に対し、実UDP SNMPエージェントを起動する。
    戻り値: {device_id: (transport, ip)} 起動できたもののみ"""
    loop = asyncio.get_event_loop()
    started = {}
    for device_id, state in device_sessions.items():
        ip = _pick_management_ip(state)
        if not ip:
            continue
        _ensure_loopback_alias(ip)
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda dev=device_id: SnmpDeviceProtocol(dev, snmp_agent),
                local_addr=(ip, port),
            )
            started[device_id] = (transport, ip)
        except Exception as e:
            print(f'[SNMP] {device_id} ({ip}:{port}) の起動失敗: {e}')
    return started
