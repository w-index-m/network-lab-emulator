"""
実BGP(TCP/179)パッシブリスナー

engine/protocols.py の BgpEngine（内部シミュレーション、sessions/loc_rib
既存実装あり）を、外部ツール(tools/route_injector_cli.py の BGPSpeaker等)
から本物のBGPワイヤプロトコルで接続・UPDATEできるようにラップする。

各装置ごとにmanagement IP:179でパッシブオープンし、OPEN/KEEPALIVEの
最小限のステートマシン(Idle->OpenSent->OpenConfirm->Established)を
実装、UPDATE受信時はNLRI/AS_PATH/NEXT_HOPを解釈して BgpEngine の
loc_rib に直接BgpRouteを追加する（show ip route等はloc_ribを直接参照
するため、内部sessionsのフルステート管理は不要）。
"""

import asyncio
import socket
import struct
from typing import Optional

from engine.snmp_udp_agent import _ensure_loopback_alias, _pick_management_ip

BGP_MARKER = b"\xff" * 16
BGP_PORT = 179


def _build_header(msg_type: int, body: bytes) -> bytes:
    length = 19 + len(body)
    return BGP_MARKER + struct.pack("!HB", length, msg_type) + body


def _build_open(local_as: int, hold_time: int, router_id: str) -> bytes:
    my_as_field = 23456 if local_as > 0xFFFF else local_as
    body = struct.pack("!BHH4sB", 4, my_as_field, hold_time,
                        socket.inet_aton(router_id), 0)
    return _build_header(1, body)


def _build_keepalive() -> bytes:
    return _build_header(4, b"")


def _parse_update(body: bytes) -> Optional[dict]:
    if len(body) < 4:
        return None
    offset = 0
    withdrawn_len = struct.unpack_from("!H", body, offset)[0]
    offset += 2 + withdrawn_len
    if offset + 2 > len(body):
        return None
    attrs_len = struct.unpack_from("!H", body, offset)[0]
    offset += 2
    attrs_end = offset + attrs_len
    if attrs_end > len(body):
        return None

    next_hop = None
    as_path = []
    origin = 'i'
    med = 0
    local_pref = 100

    pos = offset
    while pos < attrs_end:
        flags = body[pos]
        type_code = body[pos + 1]
        pos += 2
        if flags & 0x10:  # extended length
            attr_len = struct.unpack_from("!H", body, pos)[0]
            pos += 2
        else:
            attr_len = body[pos]
            pos += 1
        value = body[pos:pos + attr_len]
        pos += attr_len

        if type_code == 1 and value:  # ORIGIN
            origin = {0: 'i', 1: 'e', 2: '?'}.get(value[0], 'i')
        elif type_code == 2:  # AS_PATH
            vpos = 0
            while vpos < len(value):
                seg_type, seg_len = value[vpos], value[vpos + 1]
                vpos += 2
                for _ in range(seg_len):
                    asn = struct.unpack_from("!I", value, vpos)[0]
                    as_path.append(asn)
                    vpos += 4
        elif type_code == 3 and len(value) == 4:  # NEXT_HOP
            next_hop = socket.inet_ntoa(value)
        elif type_code == 4 and len(value) == 4:  # MED
            med = struct.unpack("!I", value)[0]
        elif type_code == 5 and len(value) == 4:  # LOCAL_PREF
            local_pref = struct.unpack("!I", value)[0]

    nlri_bytes = body[attrs_end:]
    prefixes = []
    pos = 0
    while pos < len(nlri_bytes):
        prefix_len = nlri_bytes[pos]
        pos += 1
        num_bytes = (prefix_len + 7) // 8
        raw = nlri_bytes[pos:pos + num_bytes].ljust(4, b'\x00')
        pos += num_bytes
        prefixes.append((socket.inet_ntoa(raw), prefix_len))

    return {
        'next_hop': next_hop,
        'as_path': as_path,
        'origin': origin,
        'med': med,
        'local_pref': local_pref,
        'prefixes': prefixes,
    }


class BgpSessionHandler:
    def __init__(self, device_id: str, bgp_engine, reader, writer):
        self.device_id = device_id
        self.bgp_engine = bgp_engine
        self.reader = reader
        self.writer = writer
        self.peer_addr = writer.get_extra_info('peername')
        self.state = 'Idle'
        # bgp_engine.nodes[...]['sessions'] のキー。内部シミュレーションでは
        # device_id を使うが、外部ピアは装置IDを持たないのでIPをキーにする
        self.peer_key = self.peer_addr[0] if self.peer_addr else 'external'
        self.peer_as = None

    async def _recv_exact(self, n: int) -> bytes:
        return await self.reader.readexactly(n)

    async def run(self):
        n = self.bgp_engine._node(self.device_id)
        local_as = n.get('local_as') or 65000
        router_id = n.get('router_id') or '10.1.0.1'
        # TCPは張れたがまだOPENを受け取っていない状態。実機のパッシブ側は
        # ここが Active（接続は来たがOPEN待ち）にあたる
        self.state = 'Active'
        self._sync_session('Active')
        try:
            while True:
                header = await self._recv_exact(19)
                _marker, length, mtype = struct.unpack("!16sHB", header)
                body_len = length - 19
                body = await self._recv_exact(body_len) if body_len > 0 else b""

                if mtype == 1:  # OPEN
                    if len(body) >= 9:
                        _ver, peer_as, _hold, _bgpid = struct.unpack("!BHH4s", body[:9])
                        self.peer_as = peer_as
                    # 実機同様に OpenSent → OpenConfirm と段階を踏む
                    # （自分のOPENを送った時点がOpenSent、KEEPALIVEも送って
                    #   相手のKEEPALIVE待ちになった時点がOpenConfirm）
                    self.state = 'OpenSent'
                    self._sync_session('OpenSent')
                    self.writer.write(_build_open(local_as, 180, router_id))
                    await self.writer.drain()
                    self.writer.write(_build_keepalive())
                    await self.writer.drain()
                    self.state = 'OpenConfirm'
                    self._sync_session('OpenConfirm')
                elif mtype == 4:  # KEEPALIVE
                    if self.state == 'OpenConfirm':
                        self.state = 'Established'
                        self._sync_session('Established')
                    self.writer.write(_build_keepalive())
                    await self.writer.drain()
                elif mtype == 2:  # UPDATE
                    parsed = _parse_update(body)
                    if parsed and parsed['prefixes']:
                        self._apply_update(parsed)
                elif mtype == 3:  # NOTIFICATION
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            # セッション断は状態に反映する（Establishedのまま残さない）
            self._sync_session('Idle')
            try:
                self.writer.close()
            except Exception:
                pass

    def _sync_session(self, state: str):
        """実TCPセッションの状態を bgp_engine の sessions に反映する。

        これをやらないと、経路は loc_rib に入って show ip route には出るのに
        show ip bgp summary にはネイバー行が1つも出ない、という食い違いに
        なる（OSPFの show ip ospf neighbor で踏んだのと同じ問題）。
        """
        from engine.protocols import BgpSession
        import time as _time
        n = self.bgp_engine._node(self.device_id)
        sessions = n.setdefault('sessions', {})
        sess = sessions.get(self.peer_key)
        if sess is None:
            sess = BgpSession(
                neighbor_id=self.peer_key,
                hostname=f'external({self.peer_key})',
                remote_as=self.peer_as or 0,
            )
            sess.neighbor_ip = self.peer_key
            sessions[self.peer_key] = sess
        if self.peer_as:
            sess.remote_as = self.peer_as
        sess.state = state
        if state == 'Established' and not sess.uptime:
            sess.uptime = _time.time()
        elif state == 'Idle':
            sess.uptime = None

    def _apply_update(self, parsed: dict):
        # loc_rib は BgpRoute ではなく dict のリストとして扱われる
        # （engine/protocols.py 2281行目のBgpEngine内部実装に合わせる。
        #   show ip route 等はキーアクセス r['prefix'] で読む）。
        # 一方 rib_in は BgpRoute のリストで属性アクセスされるため、
        # 同じ経路でも入れる型が異なる点に注意
        from engine.protocols import BgpRoute
        n = self.bgp_engine._node(self.device_id)
        peer_ip = self.peer_addr[0] if self.peer_addr else 'external'
        next_hop = parsed['next_hop'] or peer_ip
        for network, prefix_len in parsed['prefixes']:
            n['loc_rib'] = [r for r in n['loc_rib']
                             if not (r['prefix'] == network and r['prefix_len'] == prefix_len
                                     and r['learned_from'] == peer_ip)]
            n['loc_rib'].append({
                'prefix': network,
                'prefix_len': prefix_len,
                'next_hop': next_hop,
                'local_pref': parsed['local_pref'],
                'med': parsed['med'],
                'as_path': parsed['as_path'],
                'origin': parsed['origin'],
                'learned_from': peer_ip,
                'learned_from_hostname': f'external({peer_ip})',
            })
            # rib_in（受信した生の経路）にも入れる。show ip bgp summary の
            # State/PfxRcd 列は rib_in を learned_from で数えるため、
            # ここに入れないと経路を受け取っているのに 0 と表示される
            n['rib_in'] = [r for r in n.get('rib_in', [])
                           if not (r.prefix == network and r.prefix_len == prefix_len
                                   and r.learned_from == peer_ip)]
            n['rib_in'].append(BgpRoute(
                prefix=network, prefix_len=prefix_len, next_hop=next_hop,
                local_pref=parsed['local_pref'], med=parsed['med'],
                as_path=list(parsed['as_path']), origin=parsed['origin'],
                learned_from=peer_ip,
                learned_from_hostname=f'external({peer_ip})',
            ))
        sess = n.get('sessions', {}).get(self.peer_key)
        if sess is not None:
            sess.prefixes_received = sum(
                1 for r in n.get('rib_in', []) if r.learned_from == peer_ip)


async def start_all_bgp_agents(device_sessions: dict, bgp_engine, port: int = BGP_PORT):
    """device_sessions内の全装置に対し、実TCP BGPパッシブリスナーを起動する。
    戻り値: {device_id: (server, ip)} 起動できたもののみ"""
    started = {}
    for device_id, state in device_sessions.items():
        ip = _pick_management_ip(state)
        if not ip:
            continue
        _ensure_loopback_alias(ip)

        def make_handler(dev=device_id):
            async def handler(reader, writer):
                session = BgpSessionHandler(dev, bgp_engine, reader, writer)
                await session.run()
            return handler

        try:
            server = await asyncio.start_server(make_handler(), host=ip, port=port)
            started[device_id] = (server, ip)
        except Exception as e:
            print(f"[BGP] {device_id} ({ip}:{port}) 起動失敗: {e}")
    return started
