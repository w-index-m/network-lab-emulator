#!/usr/bin/env python3
"""
SNMP Trap Receiver → Prometheus /metrics ブリッジ

PrometheusはPull型のため、SNMP Trap（Push型）を直接受信できない。
本ツールはUDPでSNMPv2c Trapを受信し、Prometheus text exposition format
の /metrics として公開する。Prometheusがこれをスクレイプし、Alertmanagerの
アラートルールでlinkDown急増等を検知できるようにする。

本エミュレーター(engine/syslog_sender.py の send_snmp_trap_async)が送る
SNMPv2c Trap（バージョン/コミュニティ/PDU、varbindsに sysUpTime /
snmpTrapOID / sysDescr("hostname: description") を含む形式）を主な対象に
最小限のBERデコードを行う。一般的なSNMPv2c Trapの多くも同じ構造
（version, community, PDU、varbind内にsnmpTrapOID）を持つため、
汎用的なOID/コミュニティ抽出にも概ね対応する。

追加パッケージ不要（Python標準ライブラリのみ）。

使い方:
  # デフォルト: UDP 1162 でTrap受信、9162番でメトリクス公開
  # (162番は特権ポートのためroot以外は既定で1162にしている。
  #  実機同様162番で受けたい場合は --trap-port 162 を指定し、root権限で実行)
  python tools/snmp_trap_receiver.py

  # Cisco/Si-R等の "snmp-server host" にこのツールのIPと同じポートを設定
  #   snmp-server host <このツールのIP> traps public

Prometheus側の prometheus.yml には以下を追加:
  scrape_configs:
    - job_name: 'snmp-trap-receiver'
      static_configs:
        - targets: ['localhost:9162']

Alertmanagerルール例（linkDownが直近5分で1回でも発生したら通知）:
  - alert: SnmpLinkDownTrapReceived
    expr: increase(snmptrap_linkdown_total[5m]) > 0
    labels:
      severity: warning
    annotations:
      summary: "{{ $labels.source_ip }} から linkDown trap を受信"
"""

import argparse
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRAP_OID_LINKDOWN = '1.3.6.1.6.3.1.1.5.3'
TRAP_OID_LINKUP = '1.3.6.1.6.3.1.1.5.4'
TRAP_OID_COLDSTART = '1.3.6.1.6.3.1.1.5.1'
TRAP_OID_WARMSTART = '1.3.6.1.6.3.1.1.5.2'

_KNOWN_TRAP_NAMES = {
    TRAP_OID_LINKDOWN: 'linkDown',
    TRAP_OID_LINKUP: 'linkUp',
    TRAP_OID_COLDSTART: 'coldStart',
    TRAP_OID_WARMSTART: 'warmStart',
}

_lock = threading.Lock()
# (source_ip, trap_oid) -> count
_counts: dict = {}
# (source_ip, trap_oid) -> unix timestamp of last receipt
_last_seen: dict = {}
# 直近N件の生ログ（デバッグ・目視確認用）
_recent: list = []
_RECENT_MAX = 200


def _ber_read_length(data: bytes, pos: int):
    """BER長さフィールドを読む。戻り値: (length, next_pos)"""
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    n_bytes = first & 0x7f
    length = 0
    for _ in range(n_bytes):
        length = (length << 8) | data[pos]
        pos += 1
    return length, pos


def _ber_read_tlv(data: bytes, pos: int):
    """1つのTLVを読む。戻り値: (tag, value_bytes, next_pos)"""
    tag = data[pos]
    pos += 1
    length, pos = _ber_read_length(data, pos)
    value = data[pos:pos + length]
    return tag, value, pos + length


def _decode_oid(value: bytes) -> str:
    if not value:
        return ''
    first = value[0]
    parts = [str(first // 40), str(first % 40)]
    n = 0
    for b in value[1:]:
        n = (n << 7) | (b & 0x7f)
        if not (b & 0x80):
            parts.append(str(n))
            n = 0
    return '.'.join(parts)


def decode_snmp_v2c_trap(data: bytes):
    """SNMPv2c Trapを最小限デコードする。
    戻り値: dict(community, trap_oid, description) 、失敗時はNone。
    厳密なASN.1バリデーションはせず、必要なフィールドだけ拾う
    ベストエフォート実装（不正/未知の形式は静かにNoneを返す）。"""
    try:
        pos = 0
        tag, msg_body, _ = _ber_read_tlv(data, pos)
        if tag != 0x30:
            return None
        pos = 0
        # version (INTEGER)
        vtag, vval, pos = _ber_read_tlv(msg_body, pos)
        # community (OCTET STRING)
        ctag, cval, pos = _ber_read_tlv(msg_body, pos)
        community = cval.decode('utf-8', errors='replace')
        # PDU (SNMPv2c Trap = 0xa7, GetResponse等の可能性もあるが本ツールはTrap専用)
        ptag, pdu_body, _ = _ber_read_tlv(msg_body, pos)
        if ptag != 0xa7:
            return None
        ppos = 0
        _, _, ppos = _ber_read_tlv(pdu_body, ppos)  # request-id
        _, _, ppos = _ber_read_tlv(pdu_body, ppos)  # error-status
        _, _, ppos = _ber_read_tlv(pdu_body, ppos)  # error-index
        vbtag, vblist, _ = _ber_read_tlv(pdu_body, ppos)  # varbinds SEQUENCE
        if vbtag != 0x30:
            return None

        trap_oid = ''
        description = ''
        vpos = 0
        while vpos < len(vblist):
            vbtag2, vb_body, vpos = _ber_read_tlv(vblist, vpos)
            if vbtag2 != 0x30:
                continue
            inner_pos = 0
            oid_tag, oid_val, inner_pos = _ber_read_tlv(vb_body, inner_pos)
            val_tag, val_val, inner_pos = _ber_read_tlv(vb_body, inner_pos)
            oid_str = _decode_oid(oid_val)
            if oid_str == '1.3.6.1.6.3.1.1.4.1.0':  # snmpTrapOID.0
                trap_oid = _decode_oid(val_val)
            elif oid_str == '1.3.6.1.2.1.1.1.0':  # sysDescr.0
                description = val_val.decode('utf-8', errors='replace')
        if not trap_oid:
            return None
        return {'community': community, 'trap_oid': trap_oid,
                'description': description}
    except Exception:
        return None


def _record_trap(source_ip: str, community: str, trap_oid: str, description: str):
    key = (source_ip, trap_oid)
    now = time.time()
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1
        _last_seen[key] = now
        _recent.append({
            'time': now, 'source_ip': source_ip, 'community': community,
            'trap_oid': trap_oid, 'trap_name': _KNOWN_TRAP_NAMES.get(trap_oid, ''),
            'description': description,
        })
        del _recent[:-_RECENT_MAX]
    name = _KNOWN_TRAP_NAMES.get(trap_oid, trap_oid)
    print(f'[trap] {source_ip} {name} community={community} {description}')


def _udp_listener(bind_host: str, port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((bind_host, port))
    print(f'[snmp_trap_receiver] listening on udp {bind_host}:{port}')
    while True:
        data, addr = s.recvfrom(8192)
        parsed = decode_snmp_v2c_trap(data)
        if parsed:
            _record_trap(addr[0], parsed['community'],
                         parsed['trap_oid'], parsed['description'])


def _prometheus_text() -> str:
    lines = [
        '# HELP snmptrap_received_total Total SNMP traps received, by source and trap OID',
        '# TYPE snmptrap_received_total counter',
    ]
    with _lock:
        items = list(_counts.items())
        seen = dict(_last_seen)
    for (source_ip, trap_oid), count in items:
        name = _KNOWN_TRAP_NAMES.get(trap_oid, trap_oid)
        lines.append(
            f'snmptrap_received_total{{source_ip="{source_ip}",trap_oid="{trap_oid}",'
            f'trap_name="{name}"}} {count}')
    lines.append('')
    lines.append('# HELP snmptrap_last_received_timestamp_seconds Unix time of the last trap received')
    lines.append('# TYPE snmptrap_last_received_timestamp_seconds gauge')
    for (source_ip, trap_oid), ts in seen.items():
        name = _KNOWN_TRAP_NAMES.get(trap_oid, trap_oid)
        lines.append(
            f'snmptrap_last_received_timestamp_seconds{{source_ip="{source_ip}",'
            f'trap_oid="{trap_oid}",trap_name="{name}"}} {ts}')
    # linkDown/linkUp専用の集計（アラートルールを書きやすいように別名でも出す）
    linkdown_total = sum(c for (_, oid), c in items if oid == TRAP_OID_LINKDOWN)
    linkup_total = sum(c for (_, oid), c in items if oid == TRAP_OID_LINKUP)
    lines.append('')
    lines.append('# HELP snmptrap_linkdown_total Total linkDown traps received (all sources)')
    lines.append('# TYPE snmptrap_linkdown_total counter')
    lines.append(f'snmptrap_linkdown_total {linkdown_total}')
    lines.append('# HELP snmptrap_linkup_total Total linkUp traps received (all sources)')
    lines.append('# TYPE snmptrap_linkup_total counter')
    lines.append(f'snmptrap_linkup_total {linkup_total}')
    return '\n'.join(lines) + '\n'


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # アクセスログは静かに（トラップのprintのみ表示）

    def do_GET(self):
        if self.path == '/metrics':
            body = _prometheus_text().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/recent':
            import json
            with _lock:
                body = json.dumps(_recent[-50:], ensure_ascii=False, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bind', default='0.0.0.0', help='Trap受信バインドアドレス')
    ap.add_argument('--trap-port', type=int, default=1162,
                     help='SNMP Trap受信ポート(既定1162。実機同様162で受けるにはroot権限で'
                          '--trap-port 162を指定)')
    ap.add_argument('--metrics-port', type=int, default=9162,
                     help='Prometheus /metrics 公開ポート(既定9162)')
    args = ap.parse_args()

    t = threading.Thread(target=_udp_listener, args=(args.bind, args.trap_port), daemon=True)
    t.start()

    server = ThreadingHTTPServer(('0.0.0.0', args.metrics_port), _Handler)
    print(f'[snmp_trap_receiver] /metrics on http://0.0.0.0:{args.metrics_port}/metrics '
          f'(raw log: /recent)')
    server.serve_forever()


if __name__ == '__main__':
    main()
