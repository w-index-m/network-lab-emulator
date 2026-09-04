"""
実RIP(UDP/520, v1/v2)リスナー

engine/protocols.py の RipEngine（内部シミュレーション）を、外部ツール
(tools/route_injector_cli.py 等)から本物のRIP Responseパケットを受信
できるようにラップする。engine/snmp_udp_agent.py と同じパターンで、
各装置のmanagement IPをループバックエイリアスとして追加し、そのIPの
UDP/520で待ち受ける。

受信したRTE(Route Table Entry)は RipEngine.receive() にそのまま渡し、
内部のBellman-Ford更新ロジック（distribute-list、ポイズンリバース等）を
そのまま再利用する。送信元は装置IDを持たない外部ホストなので、
src_id にはピアIPアドレス文字列をそのまま使う。
"""

import asyncio
import struct
from typing import Optional

from engine.snmp_udp_agent import _ensure_loopback_alias, _pick_management_ip

RIP_COMMAND_REQUEST = 1
RIP_COMMAND_RESPONSE = 2
RIP_HEADER_FMT = '!BBH'
RIP_ENTRY_FMT = '!HH4s4s4sI'  # afi, route_tag, address, mask, next_hop, metric


def _classful_prefix(network: str) -> int:
    """RIPv1にはサブネットマスク欄が無いため、アドレスクラスから
    プレフィックス長を導出する（RFC 1058）。
    クラスA:/8  クラスB:/16  クラスC:/24"""
    try:
        first = int(network.split('.')[0])
    except (ValueError, IndexError):
        return 24
    if first < 128:
        return 8
    if first < 192:
        return 16
    return 24


def parse_rip_packet(data: bytes) -> Optional[dict]:
    if len(data) < 4:
        return None
    command, version, _zero = struct.unpack_from(RIP_HEADER_FMT, data, 0)
    entries = []
    offset = 4
    while offset + 20 <= len(data):
        afi, route_tag, addr, mask, next_hop, metric = struct.unpack_from(
            RIP_ENTRY_FMT, data, offset)
        offset += 20
        if afi != 2:  # AF_INET以外(認証エントリ afi=0xFFFF等)はスキップ
            continue
        network = '.'.join(str(b) for b in addr)
        mask_int = struct.unpack('!I', mask)[0]
        if mask_int:
            prefix = bin(mask_int).count('1')
        else:
            # RIPv1（マスク欄が常に0）や、RIPv2でもマスク未指定の場合は
            # クラスフルマスクとして解釈する。ここで0を返すと /0 の
            # 経路として学習され、デフォルトルート同然の誤った経路が
            # 入ってしまう
            prefix = _classful_prefix(network)
        entries.append({
            'network': network,
            'prefix': prefix,
            'metric': metric,
        })
    return {'command': command, 'version': version, 'entries': entries}


class RipDeviceProtocol(asyncio.DatagramProtocol):
    def __init__(self, device_id: str, rip_engine, loop):
        self.device_id = device_id
        self.rip_engine = rip_engine
        self.loop = loop
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        pkt = parse_rip_packet(data)
        if not pkt or not pkt['entries']:
            return
        src_ip = addr[0]
        msg = {
            'src_id': src_ip,
            'src_hostname': f'external({src_ip})',
            'command': 'request' if pkt['command'] == RIP_COMMAND_REQUEST else 'response',
            'entries': pkt['entries'],
        }
        self.loop.create_task(self.rip_engine.receive(self.device_id, msg))


async def start_all_rip_agents(device_sessions: dict, rip_engine, port: int = 520):
    """device_sessions内の全装置に対し、実UDP RIPリスナーを起動する。
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
                lambda dev=device_id: RipDeviceProtocol(dev, rip_engine, loop),
                local_addr=(ip, port),
            )
            started[device_id] = (transport, ip)
        except Exception as e:
            print(f"[RIP] {device_id} ({ip}:{port}) 起動失敗: {e}")
    return started
