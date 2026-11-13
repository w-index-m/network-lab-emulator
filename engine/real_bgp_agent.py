"""
実BGP(TCP/179)パッシブリスナー

engine/protocols.py の BgpEngine（内部シミュレーション、sessions/loc_rib
既存実装あり）を、外部の実BGP実装(GoBGP・FRR・tools/route_injector_cli.py
のBGPSpeaker等)と本物のBGPワイヤプロトコル(RFC 4271)で接続できるように
ラップする。

- OPENでCapability(MP-BGP IPv4 unicast / Route Refresh / 4-octet AS)を
  広告し、相手のOPENと突き合わせて4-octet ASを使うかを決める
  (両者が広告したときだけAS_PATHを4バイトで読み書きする)。
- 受信UPDATEのNLRI/WITHDRAWN/AS_PATH(+AS4_PATH)/NEXT_HOP等をBgpEngineの
  loc_rib/rib_inへ反映。セッション断時は学習経路を撤回する。
- 自分の経路(`network`で広告するもの・学習済みベストパス)を相手へUPDATEで
  広告し、変化があれば追加/撤回を送る(eBGPは自ASをprepend、next-hop-self)。
- 装置に`neighbor <ip> remote-as <as>`が1つでも設定されていれば、未設定の
  相手はCease/Connection Rejected、AS不一致はOPEN Error/Bad Peer ASで拒否
  する(実機同様)。neighbor未設定の装置は従来通りどの相手も受け入れる
  (tools/route_injector_cli.pyでの経路注入用途との互換)。
- `router bgp`投入時にensure_bgp_agent()でリスナーを起動する(起動時に
  存在した装置だけでなく、後から追加した装置でも使える)。
"""

import asyncio
import socket
import struct
from typing import Dict, Optional, Tuple

from engine.snmp_udp_agent import _ensure_loopback_alias, _pick_management_ip

BGP_MARKER = b"\xff" * 16
BGP_PORT = 179
AS_TRANS = 23456
LOCAL_HOLD_TIME = 90


def _build_header(msg_type: int, body: bytes) -> bytes:
    length = 19 + len(body)
    return BGP_MARKER + struct.pack("!HB", length, msg_type) + body


def _capabilities(local_as: int) -> bytes:
    caps = (struct.pack("!BB", 1, 4) + struct.pack("!HBB", 1, 0, 1)    # MP-BGP IPv4 unicast
            + struct.pack("!BB", 2, 0)                                 # Route Refresh
            + struct.pack("!BB", 65, 4) + struct.pack("!I", local_as))  # 4-octet AS
    return struct.pack("!BB", 2, len(caps)) + caps


def _build_open(local_as: int, hold_time: int, router_id: str, with_caps: bool = True) -> bytes:
    my_as_field = AS_TRANS if local_as > 0xFFFF else local_as
    opt = _capabilities(local_as) if with_caps else b""
    body = struct.pack("!BHH4sB", 4, my_as_field, hold_time,
                       socket.inet_aton(router_id), len(opt)) + opt
    return _build_header(1, body)


def _build_keepalive() -> bytes:
    return _build_header(4, b"")


def _build_notification(code: int, subcode: int) -> bytes:
    return _build_header(3, struct.pack("!BB", code, subcode))


def _parse_open(body: bytes) -> Optional[dict]:
    if len(body) < 10:
        return None
    ver, peer_as, hold, bgpid, opt_len = struct.unpack("!BHH4sB", body[:10])
    caps = {}
    pos, end = 10, 10 + opt_len
    while pos + 2 <= min(end, len(body)):
        ptype, plen = body[pos], body[pos + 1]
        pval = body[pos + 2:pos + 2 + plen]
        pos += 2 + plen
        if ptype != 2:
            continue
        cpos = 0
        while cpos + 2 <= len(pval):
            code, clen = pval[cpos], pval[cpos + 1]
            caps[code] = pval[cpos + 2:cpos + 2 + clen]
            cpos += 2 + clen
    real_as = peer_as
    if 65 in caps and len(caps[65]) == 4:
        real_as = struct.unpack("!I", caps[65])[0]
    return {'version': ver, 'as': real_as, 'hold': hold,
            'router_id': socket.inet_ntoa(bgpid), 'as4': 65 in caps}


def _parse_prefixes(raw: bytes):
    out = []
    pos = 0
    while pos < len(raw):
        plen = raw[pos]
        pos += 1
        nbytes = (plen + 7) // 8
        out.append((socket.inet_ntoa(raw[pos:pos + nbytes].ljust(4, b'\x00')), plen))
        pos += nbytes
    return out


def _parse_as_path(value: bytes, width: int):
    path = []
    vpos = 0
    fmt = "!I" if width == 4 else "!H"
    while vpos + 2 <= len(value):
        _seg_type, seg_len = value[vpos], value[vpos + 1]
        vpos += 2
        for _ in range(seg_len):
            path.append(struct.unpack_from(fmt, value, vpos)[0])
            vpos += width
    return path


def _parse_update(body: bytes, as4: bool = True) -> Optional[dict]:
    """as4: 4-octet ASをネゴシエートしたセッションか(AS_PATHの幅が変わる)。"""
    if len(body) < 4:
        return None
    offset = 0
    withdrawn_len = struct.unpack_from("!H", body, offset)[0]
    offset += 2
    withdrawn = _parse_prefixes(body[offset:offset + withdrawn_len])
    offset += withdrawn_len
    if offset + 2 > len(body):
        return None
    attrs_len = struct.unpack_from("!H", body, offset)[0]
    offset += 2
    attrs_end = offset + attrs_len
    if attrs_end > len(body):
        return None

    next_hop = None
    as_path = []
    as4_path = None
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
            as_path = _parse_as_path(value, 4 if as4 else 2)
        elif type_code == 17:  # AS4_PATH(4-octet AS非対応セッションでの真のAS列)
            as4_path = _parse_as_path(value, 4)
        elif type_code == 3 and len(value) == 4:  # NEXT_HOP
            next_hop = socket.inet_ntoa(value)
        elif type_code == 4 and len(value) == 4:  # MED
            med = struct.unpack("!I", value)[0]
        elif type_code == 5 and len(value) == 4:  # LOCAL_PREF
            local_pref = struct.unpack("!I", value)[0]

    if not as4 and as4_path:
        # RFC 6793 4.2.3: AS_PATHの末尾をAS4_PATHで置き換える
        keep = max(len(as_path) - len(as4_path), 0)
        as_path = as_path[:keep] + as4_path

    return {
        'next_hop': next_hop,
        'as_path': as_path,
        'origin': origin,
        'med': med,
        'local_pref': local_pref,
        'prefixes': _parse_prefixes(body[attrs_end:]),
        'withdrawn': withdrawn,
    }


def _encode_prefix(network: str, plen: int) -> bytes:
    return bytes([plen]) + socket.inet_aton(network)[:(plen + 7) // 8]


def _build_update(prefix: Tuple[str, int], as_path, next_hop: str, as4: bool,
                  ibgp: bool, origin: str = 'i', med: int = 0, local_pref: int = 100) -> bytes:
    width = "!I" if as4 else "!H"
    seg = b''
    if as_path:
        seg = struct.pack("!BB", 2, len(as_path)) + b''.join(
            struct.pack(width, a if as4 or a <= 0xFFFF else AS_TRANS) for a in as_path)
    attrs = struct.pack("!BBB", 0x40, 1, 1) + bytes([{'i': 0, 'e': 1, '?': 2}.get(origin, 0)])
    attrs += struct.pack("!BBB", 0x40, 2, len(seg)) + seg
    attrs += struct.pack("!BBB", 0x40, 3, 4) + socket.inet_aton(next_hop)
    if med:
        attrs += struct.pack("!BBB", 0x80, 4, 4) + struct.pack("!I", med)
    if ibgp:
        attrs += struct.pack("!BBB", 0x40, 5, 4) + struct.pack("!I", local_pref)
    nlri = _encode_prefix(*prefix)
    body = struct.pack("!H", 0) + struct.pack("!H", len(attrs)) + attrs + nlri
    return _build_header(2, body)


def _build_withdraw(prefixes) -> bytes:
    w = b''.join(_encode_prefix(n, l) for n, l in prefixes)
    return _build_header(2, struct.pack("!H", len(w)) + w + struct.pack("!H", 0))


class BgpSessionHandler:
    def __init__(self, device_id: str, bgp_engine, reader, writer, device_sessions=None,
                 local_ip: Optional[str] = None):
        self.device_id = device_id
        self.bgp_engine = bgp_engine
        self.reader = reader
        self.writer = writer
        self.device_sessions = device_sessions or {}
        self.peer_addr = writer.get_extra_info('peername')
        sock = writer.get_extra_info('sockname')
        self.local_ip = local_ip or (sock[0] if sock else '0.0.0.0')
        self.state = 'Idle'
        # bgp_engine.nodes[...]['sessions'] のキー。内部シミュレーションでは
        # device_id を使うが、外部ピアは装置IDを持たないのでIPをキーにする
        self.peer_key = self.peer_addr[0] if self.peer_addr else 'external'
        self.peer_as = None
        self.as4 = False
        self.hold = LOCAL_HOLD_TIME
        self.advertised: Dict[Tuple[str, int], tuple] = {}
        self._tasks = []

    async def _recv_exact(self, n: int) -> bytes:
        return await self.reader.readexactly(n)

    def _configured_neighbors(self) -> dict:
        st = self.device_sessions.get(self.device_id)
        return dict(getattr(st, '_bgp_configured_neighbors', None) or {})

    async def _send(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

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
                    op = _parse_open(body)
                    if op:
                        self.peer_as = op['as']
                    nbrs = self._configured_neighbors()
                    if nbrs:
                        expected = nbrs.get(self.peer_key)
                        if expected is None:
                            # 未設定の相手: Cease / Connection Rejected
                            await self._send(_build_notification(6, 5))
                            self._log_reject('not a configured neighbor')
                            break
                        if op and int(expected) != op['as']:
                            # OPEN Message Error / Bad Peer AS
                            await self._send(_build_notification(2, 2))
                            self._log_reject(f'bad peer AS {op["as"]} (expected {expected})')
                            break
                    self.as4 = bool(op and op['as4'])
                    if op and op['hold']:
                        self.hold = min(LOCAL_HOLD_TIME, op['hold'])
                    # 実機同様に OpenSent → OpenConfirm と段階を踏む
                    self.state = 'OpenSent'
                    self._sync_session('OpenSent')
                    await self._send(_build_open(local_as, LOCAL_HOLD_TIME, router_id))
                    await self._send(_build_keepalive())
                    self.state = 'OpenConfirm'
                    self._sync_session('OpenConfirm')
                elif mtype == 4:  # KEEPALIVE
                    if self.state == 'OpenConfirm':
                        self.state = 'Established'
                        self._sync_session('Established')
                        self._tasks = [asyncio.ensure_future(self._keepalive_loop()),
                                       asyncio.ensure_future(self._advertise_loop(local_as))]
                elif mtype == 2:  # UPDATE
                    parsed = _parse_update(body, as4=self.as4)
                    if parsed:
                        if parsed['withdrawn']:
                            self._withdraw(parsed['withdrawn'])
                        if parsed['prefixes']:
                            self._apply_update(parsed)
                elif mtype == 3:  # NOTIFICATION
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            for t in self._tasks:
                t.cancel()
            # セッション断: 学習経路を撤回し、状態もIdleへ(Establishedのまま残さない)
            self._withdraw(None)
            self._sync_session('Idle')
            try:
                self.writer.close()
            except Exception:
                pass

    def _log_reject(self, reason: str):
        n = self.bgp_engine._node(self.device_id)
        n.setdefault('external_rejects', []).append({'peer': self.peer_key, 'reason': reason})
        del n['external_rejects'][:-20]

    async def _keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(max(self.hold // 3, 1))
                await self._send(_build_keepalive())
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass

    def _desired_routes(self, local_as: int) -> Dict[Tuple[str, int], tuple]:
        """この相手へ広告すべき経路 {(prefix,len): (as_path, origin, med, local_pref)}。"""
        n = self.bgp_engine._node(self.device_id)
        ibgp = self.peer_as == local_as
        out = {}
        for net in n.get('networks', []):
            pfx, _, ln = net.partition('/')
            out[(pfx, int(ln or 24))] = (() if ibgp else (local_as,), 'i', 0, 100)
        for r in n.get('loc_rib', []):
            src = r.get('learned_from', '')
            if not src or src == self.peer_key:
                continue
            # iBGPで学んだ経路はiBGPへ再広告しない(split horizon)
            src_is_ibgp = src in n.get('sessions', {}) and \
                getattr(n['sessions'][src], 'remote_as', None) == local_as
            if ibgp and src_is_ibgp:
                continue
            path = tuple(r.get('as_path') or ())
            if not ibgp:
                path = (local_as,) + path
            if self.peer_as in path:
                continue  # 相手自身のASを含む経路は送らない(相手側でループ破棄される)
            key = (r['prefix'], int(r['prefix_len']))
            out.setdefault(key, (path, r.get('origin', 'i'), r.get('med', 0), r.get('local_pref', 100)))
        return out

    async def _advertise_loop(self, local_as: int):
        try:
            while True:
                want = self._desired_routes(local_as)
                gone = [k for k in self.advertised if k not in want]
                if gone:
                    await self._send(_build_withdraw(gone))
                    for k in gone:
                        self.advertised.pop(k, None)
                ibgp = self.peer_as == local_as
                for key, (path, origin, med, lp) in want.items():
                    if self.advertised.get(key) == (path, origin, med, lp):
                        continue
                    await self._send(_build_update(key, list(path), self.local_ip, self.as4, ibgp,
                                                   origin, med, lp))
                    self.advertised[key] = (path, origin, med, lp)
                n = self.bgp_engine._node(self.device_id)
                sess = n.get('sessions', {}).get(self.peer_key)
                if sess is not None and hasattr(sess, 'prefixes_sent'):
                    sess.prefixes_sent = len(self.advertised)
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, ConnectionError, OSError):
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

    def _withdraw(self, prefixes):
        """prefixes=None なら、この相手から学んだ経路をすべて撤回する。"""
        n = self.bgp_engine._node(self.device_id)
        peer_ip = self.peer_key
        drop = None if prefixes is None else {(p, int(l)) for p, l in prefixes}

        def hit(prefix, plen, src):
            return src == peer_ip and (drop is None or (prefix, int(plen)) in drop)

        n['loc_rib'] = [r for r in n.get('loc_rib', [])
                        if not hit(r['prefix'], r['prefix_len'], r.get('learned_from'))]
        n['rib_in'] = [r for r in n.get('rib_in', [])
                       if not hit(r.prefix, r.prefix_len, r.learned_from)]
        sess = n.get('sessions', {}).get(self.peer_key)
        if sess is not None:
            sess.prefixes_received = sum(1 for r in n.get('rib_in', []) if r.learned_from == peer_ip)

    def _apply_update(self, parsed: dict):
        # loc_rib は BgpRoute ではなく dict のリストとして扱われる
        # （BgpEngine内部実装に合わせる。show ip route 等はキーアクセス
        #   r['prefix'] で読む）。一方 rib_in は BgpRoute のリストで属性
        # アクセスされるため、同じ経路でも入れる型が異なる点に注意
        from engine.protocols import BgpRoute
        n = self.bgp_engine._node(self.device_id)
        peer_ip = self.peer_key
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


_servers: Dict[str, tuple] = {}


async def _start_one(device_id: str, ip: str, device_sessions: dict, bgp_engine, port: int):
    _ensure_loopback_alias(ip)

    async def handler(reader, writer):
        session = BgpSessionHandler(device_id, bgp_engine, reader, writer,
                                    device_sessions=device_sessions, local_ip=ip)
        await session.run()

    server = await asyncio.start_server(handler, host=ip, port=port)
    _servers[device_id] = (server, ip, port)
    return server


async def start_all_bgp_agents(device_sessions: dict, bgp_engine, port: int = BGP_PORT):
    """device_sessions内の全装置に対し、実TCP BGPパッシブリスナーを起動する。
    戻り値: {device_id: (server, ip)} 起動できたもののみ"""
    started = {}
    for device_id, state in device_sessions.items():
        ip = _pick_management_ip(state)
        if not ip:
            continue
        try:
            server = await _start_one(device_id, ip, device_sessions, bgp_engine, port)
            started[device_id] = (server, ip)
        except Exception as e:
            print(f"[BGP] {device_id} ({ip}:{port}) 起動失敗: {e}")
    return started


async def ensure_bgp_agent(device_id: str, device_sessions: dict, bgp_engine,
                           port: int = BGP_PORT) -> Optional[str]:
    """`router bgp`等の投入後に呼ぶ。装置の管理IPでリスナーが動いていなければ
    起動し、IPが変わっていれば張り直す。戻り値は待ち受けIP(失敗時None)。"""
    state = device_sessions.get(device_id)
    if state is None:
        return None
    ip = _pick_management_ip(state)
    if not ip:
        return None
    cur = _servers.get(device_id)
    if cur and cur[1] == ip and cur[2] == port and cur[0].is_serving():
        return ip
    if cur:
        stop_bgp_agent(device_id)
    try:
        await _start_one(device_id, ip, device_sessions, bgp_engine, port)
        print(f"[BGP] {device_id} ({ip}:{port}) 実TCPリスナーを起動しました")
        return ip
    except Exception as e:
        print(f"[BGP] {device_id} ({ip}:{port}) 起動失敗: {e}")
        return None


def stop_bgp_agent(device_id: str) -> None:
    cur = _servers.pop(device_id, None)
    if cur:
        try:
            cur[0].close()
        except Exception:
            pass
