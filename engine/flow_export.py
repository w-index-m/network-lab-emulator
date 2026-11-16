"""
装置からのフロー情報の送出(sFlow v5 / NetFlow v5)— エミュレータの装置を本物のエクスポータにする

ラボの通信(ping)が装置を通るたびに、その装置の設定に従って本物の UDP を送る。
受け取る側(ニルヴァーナの FlowCollector、nfdump の sfcapd / nfcapd、ntopng 等)は実機と区別しない。

設定(実機の構文):
- NX-OS : feature sflow / sflow collector-ip <ip> vrf default / sflow collector-port <n> /
          sflow sampling-rate <n> / sflow agent-ip <ip> / sflow data-source interface <if>
- Arista: sflow destination <ip> [<port>] / sflow source <ip> / sflow sample <n> / sflow run
- IOS   : ip flow-export destination <ip> <port> / ip flow-export version 5 / ip flow-export source <if> /
          interface 配下の ip flow ingress | egress

1回の ping(count 発 × size バイト)を、sFlow は「その通信の先頭パケットのヘッダ + サンプリング率」の
フローサンプル1件、NetFlow は「パケット数・バイト数を持つフローレコード」1件として送る。
"""

import socket
import struct
import threading
import time

_seq = {}
_lock = threading.Lock()
_boot = time.time()


def _next(key):
    with _lock:
        _seq[key] = _seq.get(key, 0) + 1
        return _seq[key]


def _icmp_frame(src, dst, size):
    """Ethernet + IPv4 + ICMP echo のヘッダ(先頭 64 バイトぶん)"""
    total = 20 + 8 + max(0, size)
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, total & 0xFFFF, 0, 0, 64, 1, 0, socket.inet_aton(src), socket.inet_aton(dst))
    icmp = struct.pack('!BBHHH', 8, 0, 0, 1, 1) + b'\0' * 24
    return b'\x00\x00\x5e\x00\x01\x01' + b'\x00\x00\x5e\x00\x01\x02' + b'\x08\x00' + ip + icmp


def build_sflow_v5(agent_ip, seq, uptime_ms, samples):
    """samples: [{'src','dst','size','rate','input','output','pool'}] → sFlow v5 データグラム(flow sample + raw packet header)"""
    out = b''
    for k, s in enumerate(samples):
        hdr = _icmp_frame(s['src'], s['dst'], s['size'])
        frame_len = 14 + 20 + 8 + max(0, s['size'])
        pad = b'\0' * (-len(hdr) % 4)
        rec = struct.pack('!IIII', 1, frame_len, 4, len(hdr)) + hdr + pad
        fs = struct.pack('!IIIIIIII', seq * 100 + k, s.get('input', 1), max(1, s['rate']), s.get('pool', s['rate']), 0,
                         s.get('input', 1), s.get('output', 2), 1) + struct.pack('!II', 1, len(rec)) + rec
        out += struct.pack('!II', 1, len(fs)) + fs
    return struct.pack('!II4sIIII', 5, 1, socket.inet_aton(agent_ip), 0, seq, int(uptime_ms) & 0xFFFFFFFF, len(samples)) + out


def build_netflow_v5(seq, uptime_ms, records, now=None):
    """records: [{'src','dst','packets','bytes','proto','sport','dport','input','output'}] → NetFlow v5 パケット"""
    now = time.time() if now is None else now
    hdr = struct.pack('!HHIIIIBBH', 5, len(records), int(uptime_ms) & 0xFFFFFFFF, int(now), int((now % 1) * 1e9), seq, 0, 0, 0)
    body = b''
    for r in records:
        body += (socket.inet_aton(r['src']) + socket.inet_aton(r['dst']) + b'\0' * 4 +
                 struct.pack('!HH', r.get('input', 1), r.get('output', 2)) +
                 struct.pack('!IIII', r['packets'], r['bytes'], int(uptime_ms) & 0xFFFFFFFF, int(uptime_ms) & 0xFFFFFFFF) +
                 struct.pack('!HH', r.get('sport', 0), r.get('dport', 0)) + bytes([0, 0, r.get('proto', 1), 0]) +
                 b'\0' * 8)
    return hdr + body


def config(state):
    fe = getattr(state, 'flow_export', None)
    if fe is None:
        fe = {'sflow': {'enabled': False, 'collectors': [], 'rate': 4096, 'agent_ip': None, 'sent': 0},
              'netflow': {'collectors': [], 'version': 5, 'source': None, 'ifaces': [], 'sent': 0}}
        state.flow_export = fe
    return fe


def _send(addrs, data):
    sent = 0
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for host, port in addrs:
            try:
                s.sendto(data, (host, int(port)))
                sent += 1
            except OSError:
                pass
    finally:
        s.close()
    return sent


def export_ping(device_id, state, agent_ip, src_ip, dst_ip, count, size):
    """この装置を通った ping 1回ぶんを、設定に従って sFlow / NetFlow で送る。送ったデータグラム数を返す"""
    fe = getattr(state, 'flow_export', None)
    if not fe or not src_ip or not dst_ip:
        return 0
    up = (time.time() - _boot) * 1000
    n = 0
    sf = fe['sflow']
    if sf['enabled'] and sf['collectors']:
        rate = max(1, int(sf['rate']))
        k = max(1, -(-int(count) // rate))            # サンプル数(少なくとも1件。ラボの ping は数が少ないので)
        data = build_sflow_v5(sf.get('agent_ip') or agent_ip, _next((device_id, 'sflow')), up,
                              [{'src': src_ip, 'dst': dst_ip, 'size': int(size), 'rate': rate, 'pool': int(count)}] * min(k, 8))
        n += _send(sf['collectors'], data)
        sf['sent'] += 1
    nf = fe['netflow']
    if nf['collectors'] and nf['ifaces']:
        data = build_netflow_v5(_next((device_id, 'netflow')), up,
                                [{'src': src_ip, 'dst': dst_ip, 'packets': int(count), 'bytes': int(count) * (20 + 8 + int(size)),
                                  'proto': 1, 'dport': 0x0800}])
        n += _send(nf['collectors'], data)
        nf['sent'] += 1
    return n


def running_config_lines(state, device_type):
    fe = getattr(state, 'flow_export', None)
    if not fe:
        return []
    sf, nf, out = fe['sflow'], fe['netflow'], []
    if device_type == 'nexus' and (sf['enabled'] or sf['collectors']):
        out += ['feature sflow'] if sf['enabled'] else []
        out += [f'sflow sampling-rate {sf["rate"]}']
        if sf.get('agent_ip'):
            out.append(f'sflow agent-ip {sf["agent_ip"]}')
        for h, p in sf['collectors']:
            out.append(f'sflow collector-ip {h} vrf default')
            if int(p) != 6343:
                out.append(f'sflow collector-port {p}')
    elif device_type == 'arista' and (sf['enabled'] or sf['collectors']):
        out.append(f'sflow sample {sf["rate"]}')
        if sf.get('agent_ip'):
            out.append(f'sflow source {sf["agent_ip"]}')
        out += [f'sflow destination {h}' + ('' if int(p) == 6343 else f' {p}') for h, p in sf['collectors']]
        if sf['enabled']:
            out.append('sflow run')
    if nf['collectors']:
        out += [f'ip flow-export destination {h} {p}' for h, p in nf['collectors']]
        out.append(f'ip flow-export version {nf["version"]}')
        if nf.get('source'):
            out.append(f'ip flow-export source {nf["source"]}')
    return out


def show(state, device_type):
    fe = getattr(state, 'flow_export', None) or config(state)
    sf, nf = fe['sflow'], fe['netflow']
    if device_type in ('nexus', 'arista'):
        lines = ['sFlow Configuration:', f"  sFlow enabled        : {'Yes' if sf['enabled'] else 'No'}",
                 f"  sampling-rate        : {sf['rate']}", f"  agent-ip             : {sf.get('agent_ip') or '(管理IP)'}"]
        lines += [f'  collector            : {h} port {p}' for h, p in sf['collectors']] or ['  collector            : (なし)']
        lines.append(f"  Datagrams sent       : {sf['sent']}")
        return '\n'.join(lines)
    lines = [f'Version {nf["version"]} flow-export is ' + ('enabled' if nf['collectors'] else 'disabled')]
    lines += [f'  Exporting flows to {h} ({p})' for h, p in nf['collectors']]
    if nf.get('source'):
        lines.append(f'  Exporting using source interface {nf["source"]}')
    lines.append(f'  Flow export enabled on: {", ".join(nf["ifaces"]) or "(none)"}')
    lines.append(f'  {nf["sent"]} flows exported')
    return '\n'.join(lines)
