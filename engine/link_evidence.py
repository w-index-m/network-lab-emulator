"""
リンク断の「検知経路」をまとめる — /cyber-view の LINK DOWN 表示用

同じ link down でも、運用ではどの経路で気づくかが環境によって違う:

- STATE  : エミュレータ自身が持つ IF 状態(shutdown・リンク断)。ラボ内の装置だけ
- SYSLOG : `%LINK-3-UPDOWN: Interface X, changed state to down` / Si-R の `X Link Down`
           (LOKI_URL があれば Loki、無ければエミュレータ内の記録)
- TRAP   : SNMP linkDown / linkUp trap(SNMP_TRAP_LISTEN で受信)
- POLL   : SNMP の定期収集で得た ifOperStatus(PROMETHEUS_URL の snmp_exporter)

どれか1つでも「最後に見た状態が down」ならそのリンク端は断とみなし、
どの経路が断を示しているかを画面に出す。実機監視で trap だけ/syslog だけの
環境でも、同じ LINK DOWN 表示になるようにするためのもの。
"""

import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

TRAP_OID_LINKDOWN = '1.3.6.1.6.3.1.1.5.3'
TRAP_OID_LINKUP = '1.3.6.1.6.3.1.1.5.4'
OID_SYSDESCR = '1.3.6.1.2.1.1.1.0'
OID_IFDESCR = '1.3.6.1.2.1.2.2.1.2.'
OID_IFNAME = '1.3.6.1.2.1.31.1.1.1.1.'

_ABBR = (('TenGigabitEthernet', 'te'), ('GigabitEthernet', 'gi'), ('FastEthernet', 'fa'),
         ('Ethernet', 'eth'), ('Port-channel', 'po'), ('Vlan', 'vl'), ('Loopback', 'lo'))


def norm_if(name):
    """IF名を比較用に正規化(GigabitEthernet0/0/2 と Gi0/0/2 を同じにする)"""
    n = (name or '').strip()
    for long, short in _ABBR:
        if n.lower().startswith(long.lower()):
            return short + n[len(long):].lower()
    low = n.lower()
    for long, short in _ABBR:   # 既に短縮形(Gi0/0/2 など)
        if low.startswith(short) and len(low) > len(short) and (low[len(short)].isdigit()):
            return low
    return low


_LOG_PATTERNS = (
    # Cisco IOS/NX-OS 系(LINEPROTO も同じ形)
    re.compile(r'Interface\s+([\w/.:\-]+),\s+changed state to\s+(administratively down|down|up)', re.I),
    # Si-R / SR-S
    re.compile(r'\b([\w/.:\-]+)\s+Link\s+(Down|Up)\b', re.I),
)


def parse_log_link_event(msg):
    """ログ1行から (IF名, 'down'|'up') を取り出す。該当しなければ None"""
    for p in _LOG_PATTERNS:
        m = p.search(msg or '')
        if m:
            return m.group(1), ('up' if m.group(2).lower() == 'up' else 'down')
    return None


# ── SNMP trap の受信 ─────────────────────────────────
def _read_tlv(data, pos):
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        n = length & 0x7f
        length = int.from_bytes(data[pos:pos + n], 'big')
        pos += n
    return tag, data[pos:pos + length], pos + length


def _oid(value):
    if not value:
        return ''
    parts = [str(value[0] // 40), str(value[0] % 40)]
    n = 0
    for b in value[1:]:
        n = (n << 7) | (b & 0x7f)
        if not b & 0x80:
            parts.append(str(n))
            n = 0
    return '.'.join(parts)


def decode_trap(data):
    """SNMPv2c trap から trap OID・sysDescr・ifDescr/ifName を取り出す(失敗時 None)"""
    try:
        tag, body, _ = _read_tlv(data, 0)
        if tag != 0x30:
            return None
        _, _, pos = _read_tlv(body, 0)                 # version
        _, comm, pos = _read_tlv(body, pos)            # community
        ptag, pdu, _ = _read_tlv(body, pos)
        if ptag != 0xa7:
            return None
        p = 0
        for _ in range(3):                             # request-id / error-status / error-index
            _, _, p = _read_tlv(pdu, p)
        _, vbl, _ = _read_tlv(pdu, p)
        out = {'community': comm.decode('utf-8', 'replace'), 'trap_oid': '', 'descr': '', 'ifname': ''}
        q = 0
        while q < len(vbl):
            _, vb, q = _read_tlv(vbl, q)
            _, o, r = _read_tlv(vb, 0)
            _, v, _ = _read_tlv(vb, r)
            oid = _oid(o)
            if oid == '1.3.6.1.6.3.1.1.4.1.0':
                out['trap_oid'] = _oid(v)
            elif oid == OID_SYSDESCR:
                out['descr'] = v.decode('utf-8', 'replace')
            elif oid.startswith(OID_IFDESCR) or oid.startswith(OID_IFNAME):
                out['ifname'] = v.decode('utf-8', 'replace')
        return out if out['trap_oid'] else None
    except Exception:
        return None


class TrapStore:
    """受信した linkDown/linkUp trap を時刻付きで保持する"""

    def __init__(self, maxlen=2000):
        self._lock = threading.Lock()
        self.events = deque(maxlen=maxlen)
        self._sock = None
        self.listen = None

    def record(self, src_ip, info, now=None):
        if info['trap_oid'] not in (TRAP_OID_LINKDOWN, TRAP_OID_LINKUP):
            return None
        descr = info.get('descr', '')
        host, _, rest = descr.partition(': ')
        if not rest:
            host, rest = '', descr
        iface = info.get('ifname') or (rest.split()[0] if rest.split() else '')
        ev = {'t': time.time() if now is None else now, 'src_ip': src_ip, 'host': host,
              'iface': iface, 'state': 'down' if info['trap_oid'] == TRAP_OID_LINKDOWN else 'up',
              'descr': descr}
        with self._lock:
            self.events.append(ev)
        return ev

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def start(self, spec):
        """spec = "host:port"。二重起動はしない。失敗したら例外"""
        if self._sock is not None:
            return self.listen
        host, _, port = spec.rpartition(':')
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host or '0.0.0.0', int(port)))
        self._sock = s
        self.listen = f'{host or "0.0.0.0"}:{s.getsockname()[1]}'

        def loop():
            while True:
                try:
                    data, addr = s.recvfrom(65535)
                except OSError:
                    return
                info = decode_trap(data)
                if info:
                    self.record(addr[0], info)
        threading.Thread(target=loop, daemon=True, name='snmp-trap-listener').start()
        return self.listen

    def stop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self.listen = None

    def clear(self):
        with self._lock:
            self.events.clear()


traps = TrapStore()


def prometheus_oper_status(url, timeout=3.0):
    """{(instance, ifName): 'up'|'down'} を snmp_exporter の ifOperStatus から取る"""
    q = 'ifOperStatus{job="cisco-snmp"}'
    full = url.rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': q})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(full, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    out = {}
    for row in data.get('data', {}).get('result', []):
        m = row.get('metric', {})
        if 'instance' in m and 'ifName' in m:
            v = int(float(row['value'][1]))
            # IF-MIB: 1=up 2=down 7=lowerLayerDown。testing/unknown/dormant 等は判定に使わない
            st = 'up' if v == 1 else 'down' if v in (2, 7) else None
            if st:
                out[(m['instance'].split(':')[0], norm_if(m['ifName']))] = st
    return out
