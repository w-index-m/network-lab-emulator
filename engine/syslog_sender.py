"""
実UDPパケット送信モジュール
- syslog  : RFC 3164 準拠、UDP 514
- SNMP trap: RFC 1157 (v1) / 2272 (v2c) 準拠、UDP 162
- NTP     : RFC 5905 準拠、UDP 123（クライアントリクエスト形式）
プロトコルイベント発生時に実際のUDPパケットを送出する。
"""
import socket
import struct
import time
import asyncio
from datetime import datetime
from typing import Optional, List

# ══════════════════════════════════════════
# Syslog（RFC 3164）
# ══════════════════════════════════════════
FACILITY = {
    'kern': 0, 'user': 1, 'mail': 2, 'daemon': 3,
    'auth': 4, 'syslog': 5, 'lpr': 6, 'news': 7,
    'local0': 16, 'local1': 17, 'local2': 18, 'local3': 19,
    'local4': 20, 'local5': 21, 'local6': 22, 'local7': 23,
}
SEVERITY = {
    'emergencies': 0, 'alerts': 1, 'critical': 2, 'errors': 3,
    'warnings': 4, 'notifications': 5, 'informational': 6, 'debugging': 7,
    'emerg': 0, 'alert': 1, 'crit': 2, 'err': 3,
    'warn': 4, 'notice': 5, 'info': 6, 'debug': 7,
}


def _build_syslog_packet(facility_name: str, severity_name: str,
                          hostname: str, msg: str) -> bytes:
    """RFC 3164形式 syslogパケット生成"""
    fac = FACILITY.get(facility_name, 23)
    sev = SEVERITY.get(severity_name, 6)
    pri = fac * 8 + sev
    now = datetime.now().strftime('%b %d %H:%M:%S')
    return f'<{pri}>{now} {hostname} {msg}'.encode('utf-8')


def send_syslog(host: str, port: int, facility: str, severity: str,
                hostname: str, msg: str) -> bool:
    """UDPでsyslogを送信"""
    try:
        pkt = _build_syslog_packet(facility, severity, hostname, msg)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.sendto(pkt, (host, port))
        return True
    except Exception:
        return False


async def send_syslog_async(host: str, port: int, facility: str, severity: str,
                             hostname: str, msg: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, send_syslog, host, port, facility, severity, hostname, msg
    )


# ══════════════════════════════════════════
# SNMP Trap（RFC 1157 v1 / RFC 2272 v2c）
# ══════════════════════════════════════════
def _ber_encode_int(val: int) -> bytes:
    """BER整数エンコード"""
    if val == 0:
        return b'\x02\x01\x00'
    data = []
    n = val
    while n:
        data.append(n & 0xff)
        n >>= 8
    data.reverse()
    if data[0] & 0x80:
        data.insert(0, 0)
    return bytes([0x02, len(data)]) + bytes(data)


def _ber_encode_string(s: str) -> bytes:
    """BER文字列エンコード"""
    b = s.encode('utf-8')
    return bytes([0x04, len(b)]) + b


def _ber_encode_oid(oid: str) -> bytes:
    """BER OIDエンコード（例: "1.3.6.1.4.1.9.9.1"）"""
    parts = [int(x) for x in oid.split('.')]
    encoded = [parts[0] * 40 + parts[1]]
    for p in parts[2:]:
        if p < 128:
            encoded.append(p)
        else:
            out = []
            while p:
                out.append(p & 0x7f)
                p >>= 7
            out.reverse()
            for i, b in enumerate(out):
                encoded.append(b | (0x80 if i < len(out) - 1 else 0))
    return bytes([0x06, len(encoded)] + encoded)


def _ber_length(data: bytes) -> bytes:
    """BER長さフィールド"""
    n = len(data)
    if n < 128:
        return bytes([n])
    elif n < 256:
        return bytes([0x81, n])
    else:
        return bytes([0x82, (n >> 8) & 0xff, n & 0xff])


def build_snmp_v2c_trap(community: str, hostname: str,
                         trap_oid: str = '1.3.6.1.6.3.1.1.5.1',
                         description: str = '') -> bytes:
    """
    SNMP v2c Trap パケット生成（RFC 2272）
    Structure: SEQUENCE { version INTEGER, community OCTET STRING, PDU }
    """
    # sysUpTime (1.3.6.1.2.1.1.3.0) = 0
    uptime_oid = _ber_encode_oid('1.3.6.1.2.1.1.3.0')
    uptime_val = b'\x43\x01\x00'  # TimeTicks = 0
    uptime_varbind = b'\x30' + _ber_length(uptime_oid + uptime_val) + uptime_oid + uptime_val

    # snmpTrapOID (1.3.6.1.6.3.1.1.4.1.0)
    trap_oid_oid = _ber_encode_oid('1.3.6.1.6.3.1.1.4.1.0')
    trap_oid_val = _ber_encode_oid(trap_oid)
    trapoid_varbind = b'\x30' + _ber_length(trap_oid_oid + trap_oid_val) + trap_oid_oid + trap_oid_val

    # sysDescr (1.3.6.1.2.1.1.1.0) = hostname + description
    desc_oid = _ber_encode_oid('1.3.6.1.2.1.1.1.0')
    desc_val = _ber_encode_string(f'{hostname}: {description}')
    desc_varbind = b'\x30' + _ber_length(desc_oid + desc_val) + desc_oid + desc_val

    varbind_list = uptime_varbind + trapoid_varbind + desc_varbind
    varbinds = b'\x30' + _ber_length(varbind_list) + varbind_list

    # SNMPv2c Trap PDU (tag=0xa7)
    req_id = _ber_encode_int(int(time.time()) & 0x7fffffff)
    error_status = _ber_encode_int(0)
    error_index = _ber_encode_int(0)
    pdu_data = req_id + error_status + error_index + varbinds
    pdu = b'\xa7' + _ber_length(pdu_data) + pdu_data

    # SNMP Message
    version = _ber_encode_int(1)  # v2c = 1
    comm = _ber_encode_string(community)
    msg_data = version + comm + pdu
    msg = b'\x30' + _ber_length(msg_data) + msg_data
    return msg


def send_snmp_trap(host: str, port: int, community: str,
                   hostname: str, trap_oid: str, description: str) -> bool:
    """UDP 162 に SNMP v2c Trap を送信"""
    try:
        pkt = build_snmp_v2c_trap(community, hostname, trap_oid, description)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.sendto(pkt, (host, port))
        return True
    except Exception:
        return False


async def send_snmp_trap_async(host: str, port: int, community: str,
                                hostname: str, trap_oid: str, description: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, send_snmp_trap, host, port, community, hostname, trap_oid, description
    )


# ══════════════════════════════════════════
# NTP（RFC 5905 クライアントリクエスト）
# ══════════════════════════════════════════
NTP_DELTA = 2208988800  # 1900-01-01 → 1970-01-01 の秒数差

def build_ntp_request() -> bytes:
    """
    NTP クライアントリクエストパケット（48バイト）
    LI=0, VN=4, Mode=3(client), Stratum=0, Poll=6, Precision=-20
    """
    li_vn_mode = (0 << 6) | (4 << 3) | 3   # LI=0, Version=4, Mode=3
    stratum = 0
    poll = 6
    precision = 0xEC  # -20 as unsigned byte
    root_delay = 0
    root_dispersion = 0
    ref_id = 0
    ref_ts = 0
    orig_ts = 0
    recv_ts = 0
    tx_ts = int(time.time()) + NTP_DELTA
    tx_ts_frac = 0

    pkt = struct.pack(
        '!B B b B I I I Q Q Q I I',
        li_vn_mode, stratum, poll, precision,
        root_delay, root_dispersion, ref_id,
        ref_ts, orig_ts, recv_ts,
        tx_ts, tx_ts_frac
    )
    return pkt


def send_ntp_request(host: str, port: int = 123) -> bool:
    """UDP 123 に NTP クライアントリクエストを送信"""
    try:
        pkt = build_ntp_request()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.sendto(pkt, (host, port))
        return True
    except Exception:
        return False


async def send_ntp_request_async(host: str, port: int = 123) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, send_ntp_request, host, port)


# ══════════════════════════════════════════
# SyslogDispatcher（既存互換）
# ══════════════════════════════════════════
class SyslogDispatcher:
    """装置ごとのsyslog送信先を管理"""
    def __init__(self):
        self._targets: dict = {}

    def register(self, device_id: str, servers: list):
        self._targets[device_id] = servers[:]

    def clear(self, device_id: str):
        self._targets.pop(device_id, None)

    def _should_send(self, target_level: str, event_severity: str) -> bool:
        tl = SEVERITY.get(target_level, 6)
        es = SEVERITY.get(event_severity, 6)
        return es <= tl

    async def emit(self, device_id: str, hostname: str, severity: str, msg: str):
        targets = self._targets.get(device_id, [])
        if not targets:
            return
        tasks = []
        for t in targets:
            if self._should_send(t.get('level', 'informational'), severity):
                tasks.append(send_syslog_async(
                    t['host'], t.get('port', 514),
                    t.get('facility', 'local7'),
                    severity, hostname, msg
                ))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ══════════════════════════════════════════
# SNMPDispatcher
# ══════════════════════════════════════════
class SnmpDispatcher:
    """装置ごとのSNMP trap送信先を管理"""
    def __init__(self):
        self._targets: dict = {}  # device_id -> [{"host","port","community"}]

    def register(self, device_id: str, hosts: list):
        self._targets[device_id] = hosts[:]

    def clear(self, device_id: str):
        self._targets.pop(device_id, None)

    async def emit(self, device_id: str, hostname: str,
                   trap_oid: str, description: str):
        """SNMP trap を全送信先に送出"""
        targets = self._targets.get(device_id, [])
        if not targets:
            return
        tasks = []
        for t in targets:
            tasks.append(send_snmp_trap_async(
                t['host'], t.get('port', 162),
                t.get('community', 'public'),
                hostname, trap_oid, description
            ))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ══════════════════════════════════════════
# NTPClient
# ══════════════════════════════════════════
class NtpClient:
    """装置ごとのNTPサーバーを管理し定期的にリクエストを送信"""
    def __init__(self):
        self._servers: dict = {}   # device_id -> [{"host","port"}]
        self._tasks: dict = {}     # device_id -> asyncio.Task

    def register(self, device_id: str, servers: list):
        self._servers[device_id] = servers[:]

    def start_polling(self, device_id: str, interval: int = 64):
        """NTP polling ループを開始（デフォルト64秒）"""
        if device_id in self._tasks:
            self._tasks[device_id].cancel()
        task = asyncio.ensure_future(self._poll_loop(device_id, interval))
        self._tasks[device_id] = task

    def stop_polling(self, device_id: str):
        t = self._tasks.pop(device_id, None)
        if t:
            t.cancel()

    async def _poll_loop(self, device_id: str, interval: int):
        while True:
            await self.poll_once(device_id)
            await asyncio.sleep(interval)

    async def poll_once(self, device_id: str) -> list:
        """NTPサーバー全員にリクエストを1回送信、結果リストを返す"""
        servers = self._servers.get(device_id, [])
        results = []
        loop = asyncio.get_event_loop()
        for s in servers:
            ok = await send_ntp_request_async(s['host'], s.get('port', 123))
            results.append({'host': s['host'], 'sent': ok})
        return results


# グローバルインスタンス
syslog_dispatcher = SyslogDispatcher()
snmp_dispatcher   = SnmpDispatcher()
ntp_client        = NtpClient()
