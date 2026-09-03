"""
実OSPF(raw IP proto 89)リスナー

tools/route_injector/network_route_injector.py の OSPFNeighborFaker
（scapyベースのP2P OSPF Hello/DBDesc/LSAck/LSUpdフルステートマシン、
tkinter非依存）をそのまま各装置の"実OSPFプロセス"として再利用する。
外部ツール(同ファイルのOSPFNeighborFaker、または他の実OSPF実装)から
Hello/DBDesc/LSUpdを受信し、Full到達後に受信したExternal-LSAを
engine/protocols.py の OspfEngine._learned_external に書き込んで
show ip route に反映させる。

tkinterはGUI専用コードでのみ使用されており、OSPFNeighborFakerクラス
自体はtkinterに依存しないため、importを満たすためだけのダミー
モジュールをsys.modulesへ注入してヘッドレスに読み込む
（tools/route_injector/network_route_injector.py の設計はそのまま）。
"""

import sys
import types
import asyncio
from typing import Optional


class _DummyModule(types.ModuleType):
    def __getattr__(self, name):
        return type(name, (object,), {})


def _stub_tkinter():
    if 'tkinter' in sys.modules and not isinstance(sys.modules['tkinter'], _DummyModule):
        return  # 既に本物のtkinterが使える環境ならそのまま
    for modname in ('tkinter.ttk', 'tkinter.scrolledtext',
                    'tkinter.messagebox', 'tkinter.font', 'tkinter.filedialog', 'tkinter'):
        m = _DummyModule(modname)
        if modname == 'tkinter':
            m.ttk = sys.modules.get('tkinter.ttk')
            m.messagebox = sys.modules.get('tkinter.messagebox')
            m.scrolledtext = sys.modules.get('tkinter.scrolledtext')
            m.filedialog = sys.modules.get('tkinter.filedialog')
        sys.modules[modname] = m


_stub_tkinter()
sys.path.insert(0, '/home/user/network-lab-emulator/tools/route_injector')
from network_route_injector import OSPFNeighborFaker, SCAPY_AVAILABLE  # noqa: E402


class DeviceOspfResponder(OSPFNeighborFaker):
    """OSPFNeighborFakerを、装置側の"実OSPFプロセス"として使うための
    サブクラス。External-LSA受信時にospf_engineへ書き込みを行う。"""

    def __init__(self, device_id: str, ospf_engine, loop, **kwargs):
        super().__init__(on_log=self._on_log, **kwargs)
        self.device_id = device_id
        self.ospf_engine = ospf_engine
        self.loop = loop

    def _on_log(self, msg: str):
        print(f"[OSPF:{self.device_id}] {msg}")

    # OSPFNeighborFakerの状態名 → OspfEngineが使う状態名
    _STATE_MAP = {
        'DOWN': 'Down', 'INIT': 'Init', '2-WAY': 'TwoWay',
        'EXSTART': 'ExStart', 'EXCHANGE': 'Exchange', 'FULL': 'Full',
    }

    def _set_state(self, new_state):
        super()._set_state(new_state)
        # 実リスナー経由のネイバーを ospf_engine 側のネイバーテーブルにも
        # 反映する。これをやらないと、実際にはFullで隣接が成立して経路も
        # 学習しているのに show ip ospf neighbor が (No neighbors) のままで、
        # 「表示は繋がっていないのに経路だけ来ている」という食い違いになる
        try:
            self._sync_neighbor_to_engine()
        except Exception as e:
            print(f"[OSPF:{self.device_id}] [WARN] ネイバー同期失敗: {e}")

    def _sync_neighbor_to_engine(self):
        if not self.peer_router_id:
            return
        from engine.protocols import OspfNeighbor
        n = self.ospf_engine._node(self.device_id)
        state = self._STATE_MAP.get(self.state, self.state)
        nbrs = n.setdefault('neighbors', {})
        nid = self.peer_router_id
        if self.state == self.STATE_DOWN:
            nbrs.pop(nid, None)
            return
        existing = nbrs.get(nid)
        if existing is not None:
            existing.state = state
        else:
            nbr = OspfNeighbor(
                neighbor_id=nid, router_id=nid,
                hostname=f'external({nid})', state=state,
            )
            nbrs[nid] = nbr
        # 表示用の付加情報。外部ピアは ospf_engine.nodes に登録が無い
        # （nodesは自装置の登録簿であり、そこへ偽のノードを足すと
        #   全ノードを走査する処理が壊れる）ため、ネイバー側に持たせる
        nbr = nbrs[nid]
        nbr.iface = self.iface
        if self.peer_ip:
            nbr.ip = self.peer_ip

    def _same_subnet(self, src_ip: str) -> bool:
        """送信元が自分と同じOSPFセグメントに居るか"""
        import ipaddress
        try:
            net = ipaddress.ip_network(f'{self.my_ip}/{self.mask}', strict=False)
            return ipaddress.ip_address(src_ip) in net
        except ValueError:
            return True

    def _on_packet(self, pkt):
        # 全装置の実リスナーが lo を共有しているため、224.0.0.5宛のHelloは
        # セグメントの違う装置にも届いてしまう。実機のOSPFは同一セグメント
        # の相手としか隣接しないので、ここで落とす。
        # これが無いと、10.20.20.0/24 の装置が 10.30.30.0/24 の装置を
        # ネイバーとして表示する（実際には繋がっていない相手が出る）。
        try:
            from scapy.all import IP
            if IP in pkt and not self._same_subnet(pkt[IP].src):
                return
        except Exception:
            pass
        try:
            super()._on_packet(pkt)
        except Exception as e:
            import traceback
            print(f"[OSPF:{self.device_id}] [ERROR] _on_packet例外: {e}")
            traceback.print_exc()

    def _handle_lsupd(self, lsupd):
        # 相手がLSUpdを送ってきたということは、相手側では既に隣接がFull。
        # 装置側のHelloは10秒間隔なので、2秒間隔の相手が先にFullへ抜けて
        # しまい、こちらが後から送ったDBDescが無視されてExStartのまま
        # 取り残されることがある。経路は学習できているのに
        # show ip ospf neighbor だけ ExStart という食い違いになるため、
        # LSAをやり取りできている時点でFullへ進める。
        if self.state not in (self.STATE_DOWN, self.STATE_FULL):
            self._set_state(self.STATE_FULL)
            self._log('LSAを受信したためFullへ遷移(相手は既にFull)')
        super()._handle_lsupd(lsupd)
        for lsa in lsupd.lsalist:
            # OSPF_External_LSA は type=5。scapyのASN1的レイヤ名を
            # クラス名で判定(直接import済みのExternal_LSA型と比較しない
            # ことで、tools側の実装変更に多少強くする)
            if lsa.__class__.__name__ == 'OSPF_External_LSA':
                self._apply_external_lsa(lsa)

    def _apply_external_lsa(self, lsa):
        network = lsa.id
        mask = lsa.mask
        prefix = sum(bin(int(b)).count('1') for b in mask.split('.'))
        metric = lsa.metric
        n = self.ospf_engine._node(self.device_id)
        n.setdefault('_learned_external', {})[f'{network}/{prefix}'] = {
            'metric': metric,
            'next_hop': self.peer_ip or self.peer_router_id,
            'src_hostname': f'external({self.peer_router_id})',
        }
        self._log(f"[OK] External経路を学習: {network}/{prefix} metric={metric}")
        if self.loop:
            self.loop.call_soon_threadsafe(
                lambda: self.ospf_engine._recalc_routes(self.device_id))


_running_agents: dict = {}


def _pick_ospf_ip(state, node) -> Optional[str]:
    """
    OSPFを喋らせるインターフェースのIPを選ぶ。

    OSPFは `network` で指定したセグメントの上で動くので、そのセグメント
    に載っているインターフェースのIPで待ち受けなければならない。
    管理IP優先の _pick_management_ip をそのまま使うと、mgmt0を持つ
    Nexusでは管理アドレスに張り付いてしまい、OSPFセグメントのHelloを
    一切受け取れずネイバーが上がらない。
    """
    from engine.snmp_udp_agent import _pick_management_ip
    import ipaddress

    ifaces = getattr(state, 'interfaces', {}) or {}
    nets = []
    for net in (node.get('networks') or []):
        try:
            nets.append(ipaddress.ip_network(net, strict=False))
        except ValueError:
            continue
    for name, info in ifaces.items():
        ip = info.get('ip') if isinstance(info, dict) else None
        if not ip:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in nets):
            return ip
    # networkが未設定など、判断できないときは従来どおり管理IP
    return _pick_management_ip(state)


def ensure_ospf_agent(device_id: str, device_sessions: dict, ospf_engine):
    """指定装置のOSPFが有効化された直後に呼び出す。既に実リスナーが
    起動済みならなにもしない（CLIで router ospf が設定された時点で
    動的に起動するための、start_all_ospf_agents以外のもう一つの入口。
    アプリ起動時に既にOSPFが有効な装置がなくても、後からCLIで
    router ospf を設定した装置に対して実リスナーを追従起動できる）。"""
    if not SCAPY_AVAILABLE:
        return
    if device_id in _running_agents:
        return
    state = device_sessions.get(device_id)
    if not state:
        return
    n = ospf_engine.nodes.get(device_id)
    if not n or not n.get('enabled'):
        return

    from engine.snmp_udp_agent import _ensure_loopback_alias
    ip = _pick_ospf_ip(state, n)
    if not ip:
        return
    _ensure_loopback_alias(ip)
    router_id = n.get('router_id') or ip
    area = n.get('area_id') or '0.0.0.0'
    try:
        loop = asyncio.get_event_loop()
        responder = DeviceOspfResponder(
            device_id=device_id, ospf_engine=ospf_engine, loop=loop,
            iface='lo', my_ip=ip, router_id=router_id, area=str(area),
            mask='255.255.255.0',
            hello_interval=n.get('hello_interval', 10),
            dead_interval=n.get('dead_interval', 40),
            debug=True,
        )
        responder.start()
        _running_agents[device_id] = responder
        print(f"[OSPF] {device_id} ({ip}) 実リスナーを動的起動しました "
              f"(router_id={router_id}, area={area})")
    except Exception as e:
        print(f"[OSPF] {device_id} ({ip}) 動的起動失敗: {e}")


def start_all_ospf_agents(device_sessions: dict, ospf_engine):
    """OSPFが有効な各装置に対し、実OSPFリスナー(scapy, raw proto 89)を
    起動する。management IPをそのままP2Pの自IPとして使う(loインターフェース
    上で複数装置を同時待受)。戻り値: {device_id: DeviceOspfResponder}"""
    if not SCAPY_AVAILABLE:
        print("[OSPF] scapyが利用できないため実OSPFリスナーは起動しません")
        return {}

    from engine.snmp_udp_agent import _ensure_loopback_alias

    loop = asyncio.get_event_loop()
    started = {}
    for device_id, state in device_sessions.items():
        n = ospf_engine.nodes.get(device_id)
        if not n or not n.get('enabled'):
            continue
        ip = _pick_ospf_ip(state, n)
        if not ip:
            continue
        _ensure_loopback_alias(ip)
        router_id = n.get('router_id') or ip
        area = n.get('area_id') or '0.0.0.0'
        try:
            responder = DeviceOspfResponder(
                device_id=device_id, ospf_engine=ospf_engine, loop=loop,
                iface='lo', my_ip=ip, router_id=router_id, area=str(area),
                mask='255.255.255.0',
                hello_interval=n.get('hello_interval', 10),
                dead_interval=n.get('dead_interval', 40),
                debug=True,
            )
            responder.start()
            started[device_id] = responder
            _running_agents[device_id] = responder
        except Exception as e:
            print(f"[OSPF] {device_id} ({ip}) 起動失敗: {e}")
    return started
