"""
プロトコルエンジン (Python版)
RIP v2 / OSPF / BGP / STP / RSTP を実際に動かす
app.py から import して使用
"""
import asyncio
import time
import random
import math
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from collections import defaultdict

# バックグラウンドタスクの強参照を保持（GC防止）
# uvicornではこれがないとcreate_taskで作ったタスクが回収される
_background_tasks: set = set()

# タイマー間隔（テスト時に短縮可能）。環境変数 NETLAB_FAST_TIMERS=1 で高速化
import os as _os
_FAST = _os.environ.get('NETLAB_FAST_TIMERS') == '1'
RIP_UPDATE_INTERVAL = 2 if _FAST else 30      # RIP定期更新
RIP_JITTER_MAX      = 1 if _FAST else 8       # RIP起動ジッター上限
OSPF_HELLO_INTERVAL = 1 if _FAST else 10      # OSPF Hello間隔

def _spawn(coro):
    """タスクを生成して強参照を保持"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

def _normalize_area(area: str) -> str:
    """エリアIDを正規化（0 も 0.0.0.0 も同一視）"""
    area = str(area).strip()
    if '.' not in area:
        # 数値形式 → ドット形式に変換
        try:
            num = int(area)
            return f'{(num>>24)&0xff}.{(num>>16)&0xff}.{(num>>8)&0xff}.{num&0xff}'
        except ValueError:
            return area
    return area

# ══════════════════════════════════════════
# 仮想ネットワーク（装置間リンク管理）
# ══════════════════════════════════════════
class VirtualNetwork:
    """装置間の仮想リンクを管理し、パケットを転送する"""
    def __init__(self):
        self.links: Dict[str, set] = defaultdict(set)  # device_id -> {peer_ids}
        self.interface_links: Dict[str, Dict[str, str]] = defaultdict(dict)  # device_id -> {peer_id -> iface_name}
        self.down_interfaces: Dict[str, set] = defaultdict(set)  # device_id -> {shutdown中のiface名}
        self.send_callbacks: Dict[str, Callable] = {}  # device_id -> ws_send関数
        self.ws_send_callbacks: Dict[str, Callable] = {}  # フロントエンド表示用

    def add_link(self, a: str, b: str, iface_a: str = None, iface_b: str = None):
        self.links[a].add(b)
        self.links[b].add(a)
        if iface_a:
            self.interface_links[a][b] = iface_a
        if iface_b:
            self.interface_links[b][a] = iface_b

    def interface_down(self, device_id: str, iface: str):
        """インターフェースをshutdown状態にする（broadcast_to_neighborsがスキップする）"""
        self.down_interfaces[device_id].add(iface)

    def interface_up(self, device_id: str, iface: str):
        """インターフェースをno shutdown状態に戻す"""
        self.down_interfaces[device_id].discard(iface)

    def get_peers_on_interface(self, device_id: str, iface: str) -> set:
        """指定インターフェース経由で接続されているpeer device_idのセット"""
        return {peer for peer, if_name in self.interface_links.get(device_id, {}).items()
                if if_name == iface}

    def remove_link(self, a: str, b: str):
        self.links[a].discard(b)
        self.links[b].discard(a)
        # STPにポートダウンを通知（後方参照なので_spawn使用）
        try:
            _spawn(stp_engine.port_down(a, b))
            _spawn(stp_engine.port_down(b, a))
        except Exception:
            pass

    def get_neighbors(self, device_id: str) -> set:
        return self.links.get(device_id, set())

    def register(self, device_id: str, callback: Callable, hostname: str = ''):
        """フロントエンド(WebSocket)への表示用コールバックを登録"""
        self.ws_send_callbacks[device_id] = callback
        if hostname:
            self.ws_send_callbacks[f'_hostname_{device_id}'] = hostname

    def unregister(self, device_id: str):
        self.ws_send_callbacks.pop(device_id, None)
        self.send_callbacks.pop(device_id, None)
        peers = list(self.links.get(device_id, []))
        for peer in peers:
            self.links[peer].discard(device_id)
        self.links.pop(device_id, None)

    async def send_to(self, device_id: str, msg: dict):
        """
        メッセージを装置に届ける。
        - プロトコルパケット(rip_packet/ospf_hello等) → 該当エンジンのreceiveを呼ぶ
        - 表示用ログ(rip_log/rip_table等) → フロントエンドWebSocketに送る
        """
        msg_type = msg.get('type', '')

        # プロトコルパケットは該当エンジンのreceive()に自動ルーティング
        if msg_type == 'rip_packet':
            await rip_engine.receive(device_id, msg)
            return
        elif msg_type == 'ospf_hello':
            await ospf_engine.receive_hello(device_id, msg)
            return
        elif msg_type == 'stp_bpdu':
            await stp_engine.receive_bpdu(device_id, msg)
            return
        elif msg_type == 'bgp_open' or msg_type == 'bgp_update':
            # BGPはエンジン側で直接相手ノードを操作するのでスキップ
            pass

        # それ以外（表示用）はフロントエンドへ + syslog送信
        cb = self.ws_send_callbacks.get(device_id)
        if cb:
            try:
                await cb(msg)
            except Exception:
                pass

        # ── syslog / SNMP trap 送信フック ──
        log_msg = msg.get('message', '')
        if log_msg and msg_type in (
            'ospf_log', 'rip_log', 'bgp_log', 'stp_log',
            'static_log', 'snmp_cfg', 'syslog_cfg',
        ):
            sev = 'informational'
            if any(k in log_msg for k in ('DOWN', 'DEAD', 'expired', 'fail', 'ERROR')):
                sev = 'warnings'
            elif any(k in log_msg for k in ('FULL', 'Established', 'Root bridge', 'bundled')):
                sev = 'notifications'
            try:
                from engine.syslog_sender import syslog_dispatcher, snmp_dispatcher
                hostname = self.ws_send_callbacks.get(f'_hostname_{device_id}', device_id)
                # syslog送信
                await syslog_dispatcher.emit(device_id, hostname, sev, log_msg)
                # SNMP trap送信（重要イベントのみ: OSPF/BGP隣接変化、STP TC）
                if any(k in log_msg for k in ('ADJCHG','Established','FULL','topology','TCN',
                                               'err-disable','DOWN')):
                    trap_oid = '1.3.6.1.6.3.1.1.5.1'  # coldStart default
                    if 'OSPF' in log_msg or 'ospf' in log_msg:
                        trap_oid = '1.3.6.1.4.1.9.10.13.1'  # ciscoOspfTrap
                    elif 'BGP' in log_msg:
                        trap_oid = '1.3.6.1.4.1.9.10.14.1'  # ciscoBgpTrap
                    elif 'STP' in log_msg or 'TCN' in log_msg:
                        trap_oid = '1.3.6.1.2.1.17.0.1'     # newRoot
                    await snmp_dispatcher.emit(device_id, hostname, trap_oid, log_msg[:100])
            except Exception:
                pass

    @staticmethod
    def _ip_to_int(ip: str) -> int:
        try:
            parts = ip.split('.')
            return sum(int(p) << (8 * (3 - i)) for i, p in enumerate(parts))
        except Exception:
            return 0

    def _get_device_networks(self, device_id: str) -> list:
        """装置が持つ全インタフェースのネットワークアドレス一覧を返す"""
        try:
            from engine.protocols import icmp_engine
            info = icmp_engine.device_ips.get(device_id, {})
            nets = []
            for ip, prefix in info.get('ips', {}).items():
                mask = (0xffffffff << (32 - prefix)) & 0xffffffff
                net_int = self._ip_to_int(ip) & mask
                nets.append((net_int, mask, prefix))
            return nets
        except Exception:
            return []

    def _shares_segment(self, dev_a: str, dev_b: str) -> bool:
        """
        2装置が共通のIPセグメントを持つか確認。
        VLANが設定されている場合は同一VLANも確認。
        実機では同一L2セグメント（同一サブネット）内でのみ
        RIP/OSPF/STPなどのマルチキャストが届く。
        IPが未設定の場合はリンク接続だけで通信可能とみなす（初期設定段階）。
        """
        # VLAN設定がある場合はVLANチェック優先
        try:
            from engine.protocols import vlan_engine
            ports_a = vlan_engine.ports.get(dev_a, {})
            ports_b = vlan_engine.ports.get(dev_b, {})
            if ports_a and ports_b:
                # 共通VLANがあるかチェック
                vlans_a = set()
                for p in ports_a.values():
                    if p.mode == 'access':
                        vlans_a.add(p.access_vlan)
                    else:
                        vlans_a |= p.allowed_vlans
                vlans_b = set()
                for p in ports_b.values():
                    if p.mode == 'access':
                        vlans_b.add(p.access_vlan)
                    else:
                        vlans_b |= p.allowed_vlans
                if vlans_a and vlans_b:
                    return bool(vlans_a & vlans_b)
        except Exception:
            pass

        nets_a = self._get_device_networks(dev_a)
        nets_b = self._get_device_networks(dev_b)
        # どちらかにIPが未設定 → 設定中とみなして通信許可
        if not nets_a or not nets_b:
            return True
        # 共通セグメントを探す
        for net_a, mask_a, prefix_a in nets_a:
            for net_b, mask_b, prefix_b in nets_b:
                if prefix_a == prefix_b and net_a == net_b:
                    return True
                common_prefix = min(prefix_a, prefix_b)
                common_mask = (0xffffffff << (32 - common_prefix)) & 0xffffffff
                if (net_a & common_mask) == (net_b & common_mask):
                    if common_prefix >= 30 or (prefix_a == prefix_b):
                        return True
        return False

    async def broadcast_to_neighbors(self, src_id: str, msg: dict, exclude: set = None):
        msg_type = msg.get('type', '')
        down = self.down_interfaces.get(src_id, set())
        for peer_id in list(self.links.get(src_id, set())):
            if exclude and peer_id in exclude:
                continue
            # shutdownされているインターフェース経由のピアには送信しない
            if down:
                connecting_iface = self.interface_links.get(src_id, {}).get(peer_id)
                if connecting_iface and connecting_iface in down:
                    continue
            # L2マルチキャスト系プロトコル（RIP/OSPF/STP/VRRP/LACP）は
            # 同一セグメントのみ届く
            if msg_type in ('rip_update', 'rip_request', 'rip_packet',
                            'ospf_hello', 'ospf_lsa',
                            'stp_bpdu',
                            'vrrp_advert', 'hsrp_hello',
                            'lacp_pdu'):
                if not self._shares_segment(src_id, peer_id):
                    continue   # 別セグメント → 届かない
            await self.send_to(peer_id, msg)

# グローバルネットワークインスタンス
vnet = VirtualNetwork()

# ══════════════════════════════════════════
# RIP v2 エンジン
# ══════════════════════════════════════════
@dataclass
class RipRoute:
    network: str
    prefix: int
    metric: int
    next_hop: str
    learned_from: str        # 'direct' or device_id
    learned_from_hostname: str = ''
    timestamp: float = field(default_factory=time.time)
    state: str = 'valid'     # valid / invalid

class RipEngine:
    def __init__(self):
        self.nodes: Dict[str, dict] = {}

    def _node(self, device_id: str) -> dict:
        if device_id not in self.nodes:
            self.nodes[device_id] = {
                'enabled': False,
                'version': 2,
                'hostname': device_id,
                'networks': [],
                'table': [],          # List[RipRoute]
                'timer_task': None,
                'expire_tasks': {},   # "net/prefix" -> task
                'redistributed': {},  # net/prefix -> {metric, source}
                'peers': {},          # peer_id -> {hostname, last_seen, routes}
            }
        return self.nodes[device_id]

    async def start(self, device_id: str, hostname: str, networks: List[str], version: int = 2):
        n = self._node(device_id)
        n['enabled'] = True
        n['version'] = version
        n['hostname'] = hostname
        n['networks'] = networks
        n['table'] = []

        # 直接接続ネットワークをテーブルに追加
        for net in networks:
            parts = net.split('/')
            if len(parts) == 2:
                self._add_route(device_id, RipRoute(
                    network=parts[0], prefix=int(parts[1]),
                    metric=1, next_hop='0.0.0.0',
                    learned_from='direct'
                ))

        await vnet.send_to(device_id, {
            'type': 'rip_log',
            'message': vendor_log.rip_start(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), hostname, version, networks)
        })

        # タイマー開始
        if n['timer_task']:
            n['timer_task'].cancel()
        n['timer_task'] = _spawn(self._update_loop(device_id))

        # 起動直後にRequestを送信
        await asyncio.sleep(0.5)
        await self._send_request(device_id)

    async def stop(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        # ポイズンリバース
        if n['table']:
            poison = [{'network': r.network, 'prefix': r.prefix, 'metric': 16}
                      for r in n['table']]
            pkt = {'type': 'rip_packet', 'command': 'response',
                   'version': 2, 'src_id': device_id,
                   'src_hostname': n['hostname'], 'entries': poison}
            await vnet.broadcast_to_neighbors(device_id, pkt)
        if n['timer_task']:
            n['timer_task'].cancel()
        for t in n['expire_tasks'].values():
            t.cancel()
        n['enabled'] = False

    async def interface_down(self, device_id: str, peer_ids: set):
        """インターフェースダウン時: 対象ピアから学習したルートをポイズンして削除"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return
        removed = [r for r in n['table'] if r.learned_from in peer_ids]
        n['table'] = [r for r in n['table'] if r.learned_from not in peer_ids]
        # 自ノードのピアレコードから削除 + 相手ノードからも自分を削除
        for peer_id in peer_ids:
            n.get('peers', {}).pop(peer_id, None)
            peer_n = self.nodes.get(peer_id)
            if peer_n:
                peer_n.get('peers', {}).pop(device_id, None)
        if removed:
            poison = [{'network': r.network, 'prefix': r.prefix, 'metric': 16}
                      for r in removed]
            pkt = {'type': 'rip_packet', 'command': 'response', 'version': 2,
                   'src_id': device_id, 'src_hostname': n['hostname'], 'entries': poison}
            await vnet.broadcast_to_neighbors(device_id, pkt)

    async def _update_loop(self, device_id: str):
        jitter = random.uniform(1, RIP_JITTER_MAX)
        await asyncio.sleep(jitter)
        while True:
            n = self.nodes.get(device_id)
            if not n or not n['enabled']:
                break
            await self._send_update(device_id)
            await asyncio.sleep(RIP_UPDATE_INTERVAL)

    async def _send_request(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        pkt = {'type': 'rip_packet', 'command': 'request',
               'version': n['version'], 'src_id': device_id,
               'src_hostname': n['hostname'],
               'entries': [{'network': '0.0.0.0', 'prefix': 0, 'metric': 16}]}
        await vnet.broadcast_to_neighbors(device_id, pkt)

    async def _send_update(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return

        down = vnet.down_interfaces.get(device_id, set())
        for peer_id in vnet.get_neighbors(device_id):
            # shutdownインターフェース経由のピアには送信しない
            if down:
                connecting_iface = vnet.interface_links.get(device_id, {}).get(peer_id)
                if connecting_iface and connecting_iface in down:
                    continue
            # セグメントチェック: 共通IPセグメントがなければ届かない
            if not vnet._shares_segment(device_id, peer_id):
                continue
            entries = []
            # 直接ネットワーク
            for net in n['networks']:
                parts = net.split('/')
                if len(parts) == 2:
                    entries.append({'network': parts[0], 'prefix': int(parts[1]),
                                    'metric': 1, 'next_hop': '0.0.0.0'})
            # 再配信ルート（他プロトコルから注入されたもの）
            for net, info in n.get('redistributed', {}).items():
                parts = net.split('/')
                if len(parts) == 2:
                    entries.append({'network': parts[0], 'prefix': int(parts[1]),
                                    'metric': info.get('metric', 1),
                                    'next_hop': '0.0.0.0', 'redistributed': True})
            # 学習済みルート（スプリットホライズン）
            for r in n['table']:
                if r.learned_from != 'direct' and r.learned_from != peer_id and r.metric < 16:
                    entries.append({'network': r.network, 'prefix': r.prefix,
                                    'metric': r.metric, 'next_hop': r.next_hop})
            if not entries:
                continue
            # distribute-list out フィルタを適用
            entries = filter_engine.filter_routes(device_id, 'rip', 'out', entries)
            if not entries:
                continue
            pkt = {'type': 'rip_packet', 'command': 'response',
                   'version': n['version'], 'src_id': device_id,
                   'src_hostname': n['hostname'], 'entries': entries,
                   'timestamp': time.time()}
            peer_node = self.nodes.get(peer_id)
            if peer_node and peer_node.get('enabled'):
                await vnet.send_to(peer_id, pkt)

    async def receive(self, receiver_id: str, msg: dict):
        n = self.nodes.get(receiver_id)
        if not n or not n['enabled']:
            return
        src_id = msg.get('src_id')
        src_hostname = msg.get('src_hostname', src_id)
        command = msg.get('command')

        if command == 'request':
            await self._send_update(receiver_id)
            return

        # ピアとして記録（ルートの有無に関わらず）
        if src_id:
            # ピアの接続IPを検索（icmp_engineの同一セグメントIPを使用）
            peer_ip = src_id
            try:
                recv_nets = vnet._get_device_networks(receiver_id)
                for ip, prefix in icmp_engine.device_ips.get(src_id, {}).get('ips', {}).items():
                    mask = (0xffffffff << (32 - prefix)) & 0xffffffff
                    net_int = vnet._ip_to_int(ip) & mask
                    for r_net, r_mask, r_prefix in recv_nets:
                        if r_prefix == prefix and r_net == net_int:
                            peer_ip = ip
                            break
                    if peer_ip != src_id:
                        break
            except Exception:
                pass
            n.setdefault('peers', {})[src_id] = {
                'hostname': src_hostname,
                'ip': peer_ip,
                'last_seen': time.time(),
                'routes': len(msg.get('entries', [])),
            }

        # Response処理（ベルマン-フォード）
        changed = False
        for entry in msg.get('entries', []):
            net = entry.get('network')
            if not net or net == '0.0.0.0':
                continue
            prefix = entry.get('prefix', 24)
            # distribute-list in フィルタ: 拒否された経路は学習しない
            if not filter_engine.check_prefix_list(
                    receiver_id,
                    filter_engine.distribute_lists.get(receiver_id, {}).get(('rip', 'in'), ''),
                    net, int(prefix)):
                continue
            new_metric = min(entry.get('metric', 1) + 1, 16)

            if new_metric >= 16:
                # ポイズンリバース
                idx = next((i for i, r in enumerate(n['table'])
                            if r.network == net and r.prefix == prefix
                            and r.learned_from == src_id), -1)
                if idx >= 0:
                    n['table'].pop(idx)
                    changed = True
                continue

            existing = next((r for r in n['table']
                             if r.network == net and r.prefix == prefix), None)
            if not existing:
                route = RipRoute(network=net, prefix=prefix, metric=new_metric,
                                 next_hop=src_id, learned_from=src_id,
                                 learned_from_hostname=src_hostname)
                self._add_route(receiver_id, route)
                changed = True
                await vnet.send_to(receiver_id, {
                    'type': 'rip_log',
                    'message': vendor_log.rip_route_learned(vnet.ws_send_callbacks.get(f'_type_{receiver_id}','cisco'), n['hostname'], net, prefix, new_metric, src_hostname)
                })
            elif src_id == existing.learned_from:
                if existing.metric != new_metric:
                    existing.metric = new_metric
                    changed = True
                existing.timestamp = time.time()
                self._reset_expire(receiver_id, net, prefix)
            elif new_metric < existing.metric:
                existing.metric = new_metric
                existing.learned_from = src_id
                existing.learned_from_hostname = src_hostname
                existing.next_hop = src_id
                existing.timestamp = time.time()
                changed = True
                self._reset_expire(receiver_id, net, prefix)
                await vnet.send_to(receiver_id, {
                    'type': 'rip_log',
                    'message': vendor_log.rip_route_learned(vnet.ws_send_callbacks.get(f'_type_{receiver_id}','cisco'), n['hostname'], net, prefix, new_metric, src_hostname)
                })

        if changed:
            await self._push_table(receiver_id)
            # トリガードアップデート
            await asyncio.sleep(random.uniform(1, 3))
            await self._send_update(receiver_id)

    def _add_route(self, device_id: str, route: RipRoute):
        n = self._node(device_id)
        idx = next((i for i, r in enumerate(n['table'])
                    if r.network == route.network and r.prefix == route.prefix), -1)
        if idx >= 0:
            n['table'][idx] = route
        else:
            n['table'].append(route)
        if route.learned_from != 'direct':
            self._reset_expire(device_id, route.network, route.prefix)

    def _reset_expire(self, device_id: str, network: str, prefix: int):
        n = self.nodes.get(device_id)
        if not n:
            return
        key = f'{network}/{prefix}'
        old = n['expire_tasks'].get(key)
        if old:
            old.cancel()
        async def expire():
            await asyncio.sleep(180)
            nd = self.nodes.get(device_id)
            if not nd:
                return
            idx = next((i for i, r in enumerate(nd['table'])
                        if r.network == network and r.prefix == prefix), -1)
            if idx >= 0:
                nd['table'].pop(idx)
                await vnet.send_to(device_id, {
                    'type': 'rip_log',
                    'message': vendor_log.rip_route_expired(vnet.ws_send_callbacks.get(f'_type_{receiver_id}','cisco'), n['hostname'], network, prefix)
                })
                await self._push_table(device_id)
            nd['expire_tasks'].pop(key, None)
        n['expire_tasks'][key] = _spawn(expire())

    async def _push_table(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        table_data = [{'network': r.network, 'prefix': r.prefix,
                       'metric': r.metric, 'next_hop': r.next_hop,
                       'learned_from': r.learned_from,
                       'learned_from_hostname': r.learned_from_hostname,
                       'state': r.state}
                      for r in n['table']]
        await vnet.send_to(device_id, {'type': 'rip_table', 'table': table_data})

    def get_table(self, device_id: str) -> List[dict]:
        n = self.nodes.get(device_id)
        if not n:
            return []
        return [{'network': r.network, 'prefix': r.prefix,
                 'metric': r.metric, 'next_hop': r.next_hop,
                 'learned_from': r.learned_from,
                 'learned_from_hostname': r.learned_from_hostname}
                for r in n['table']]

    def format_show_rip_route(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '<ERROR> No RIP is configured.'
        lines = ['FP Destination/Mask     Gateway           Metric   Time    Interface']
        for r in n['table']:
            fp = '*C' if r.learned_from == 'direct' else '*R'
            age = 'none' if r.learned_from == 'direct' else \
                  f'{int((time.time()-r.timestamp)//60):02d}:{int((time.time()-r.timestamp)%60):02d}'
            via = '0.0.0.0' if r.learned_from == 'direct' else r.learned_from_hostname or r.next_hop
            lines.append(f'{fp:<3}{r.network}/{r.prefix:<20}{via:<18}{str(r.metric):<9}{age:<8}lan0')
        lines.append(f'The number of entries : {len(n["table"])}')
        return '\n'.join(lines)

    def format_show_rip_protocol(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '<ERROR> No RIP is configured.'
        next_due = random.randint(5, 29)
        lines = [
            f'Sending updates every 30 seconds with +/-50%, next due in {next_due} seconds',
            'Timeout after 180 seconds, garbage collect after 120 seconds',
            'Redistributing: Connected, Static',
            'Interface        Send     Recv',
            f'  lan0            {n["version"]}        1 {n["version"]}',
            'Routing Information Sources:',
        ]
        sources = set(r.learned_from_hostname for r in n['table']
                      if r.learned_from != 'direct' and r.learned_from_hostname)
        for src in sources:
            lines.append(f'  {src:<20}0              0 00:00:{random.randint(10,59):02d}')
        lines.append(f'Distance: 120')
        lines.append(f'The number of entries : {len(n["table"])}')
        return '\n'.join(lines)

    def format_show_rip_neighbor(self, device_id: str) -> str:
        """show ip rip neighbor — RIPネイバー一覧（Cisco/Si-R共通形式）"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% RIP is not configured on this device.'
        # peers dict（パケット受信時に記録）を優先。なければ学習ルートから補完
        peers = dict(n.get('peers', {}))
        for r in n['table']:
            if r.learned_from and r.learned_from != 'direct' and r.learned_from not in peers:
                peers[r.learned_from] = {
                    'hostname': r.learned_from_hostname or r.learned_from,
                    'last_seen': r.timestamp,
                    'routes': 1,
                }
        if not peers:
            return ('Index   IP Address        Last Update   Bad Pkts   Bad Routes\n'
                    '(No RIP neighbors — ルートを受信していません)')
        lines = ['Index   IP Address        Last Update   Bad Pkts   Bad Routes']
        for i, (peer_id, info) in enumerate(peers.items(), 1):
            age_sec = int(time.time() - info.get('last_seen', time.time()))
            mm, ss = divmod(age_sec, 60)
            hh, mm = divmod(mm, 60)
            upd = f'{hh:02d}:{mm:02d}:{ss:02d}'
            display_ip = info.get('ip', info.get('hostname', peer_id))
            lines.append(f'{i:<8}{display_ip:<18}{upd:<14}0          0')
        lines.append('')
        lines.append('Routing Information Sources:')
        for peer_id, info in peers.items():
            hostname = info.get('hostname', peer_id)
            display_ip = info.get('ip', hostname)
            routes = info.get('routes', 0)
            lines.append(f'  {display_ip:<20}{hostname:<16}  {routes} routes')
        return '\n'.join(lines)


# ══════════════════════════════════════════
# OSPF エンジン
# ══════════════════════════════════════════
@dataclass
class OspfNeighbor:
    neighbor_id: str
    router_id: str
    hostname: str
    state: str = 'Init'      # Init / TwoWay / ExStart / Exchange / Loading / Full
    dead_task: Optional[Any] = None
    uptime: float = field(default_factory=time.time)

@dataclass
class OspfLsa:
    ls_type: int
    ls_id: str
    adv_router: str
    seq_num: int
    checksum: int
    age: int
    links: List[dict] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

class OspfEngine:
    """
    OSPF完全実装（Cisco IOS XE / Catalyst 9300準拠）
    - DR/BDR選出（priority比較 → RouterID比較、priority=0はDR非対象）
    - コスト計算（ref-bw 100Mbps ÷ 帯域幅、デフォルト=10）
    - dead_interval = hello_interval × 4（Cisco仕様）
    - マルチエリア（area 0/1、ABRがSummary LSA生成）
    - passive-interface（Helloを送らない）
    - show: Full/DR、Full/BDR、Full/DROther 正確表示
    """
    def __init__(self):
        self.nodes: Dict[str, dict] = {}
        # device_id -> {iface -> True}  passive-interface設定
        self.passive_ifaces: Dict[str, set] = {}
        # マルチエリア: device_id -> {area_id -> [networks]}
        self.area_networks: Dict[str, Dict[str, list]] = {}

    def _node(self, device_id: str) -> dict:
        if device_id not in self.nodes:
            h = abs(hash(device_id)) % 200 + 10
            self.nodes[device_id] = {
                'enabled': False,
                'process_id': 1,
                'hostname': device_id,
                # ループバック優先・最大IP（Cisco仕様）→ 起動時に確定
                'router_id': f'10.0.{h}.1',
                'area_id': '0.0.0.0',
                'networks': [],   # [(network, wildcard, area)]
                'neighbors': {},  # device_id -> OspfNeighbor
                'lsdb': {},
                'routes': [],
                'hello_task': None,
                'seq_num': 1,
                'hello_interval': OSPF_HELLO_INTERVAL,
                'dead_interval': OSPF_HELLO_INTERVAL * 4,  # Cisco: hello×4
                'priority': 1,    # DR選出優先度（0=DR非対象）
                'dr': None,       # 現在のDR device_id
                'bdr': None,      # 現在のBDR device_id
                'interface_cost': 10,   # デフォルトコスト（100M ÷ 1G）
                'redistributed': {},
                'abr': False,     # ABR（マルチエリア）フラグ
                'summary_routes': [],  # ABRが生成するSummary LSA
            }
        return self.nodes[device_id]

    def set_priority(self, device_id: str, priority: int):
        n = self._node(device_id)
        n['priority'] = max(0, min(255, priority))
        # 既にネイバーがいればDR再選出をトリガー
        if n.get('enabled') and n.get('neighbors'):
            _spawn(self._elect_dr_bdr(device_id))

    def set_cost(self, device_id: str, cost: int):
        n = self._node(device_id)
        n['interface_cost'] = cost

    def add_passive_interface(self, device_id: str, iface: str):
        self.passive_ifaces.setdefault(device_id, set()).add(iface.lower())

    def remove_passive_interface(self, device_id: str, iface: str):
        if device_id in self.passive_ifaces:
            self.passive_ifaces[device_id].discard(iface.lower())

    async def start(self, device_id: str, hostname: str, process_id: int,
                    networks: List[str], area: str = '0.0.0.0'):
        n = self._node(device_id)
        n['enabled'] = True
        n['process_id'] = process_id
        n['hostname'] = hostname
        n['area_id'] = _normalize_area(area)
        # networks追加（重複排除）
        for net in networks:
            if net not in n['networks']:
                n['networks'].append(net)
        # マルチエリアへ登録
        self.area_networks.setdefault(device_id, {})
        self.area_networks[device_id].setdefault(n['area_id'], [])
        for net in networks:
            if net not in self.area_networks[device_id][n['area_id']]:
                self.area_networks[device_id][n['area_id']].append(net)

        await vnet.send_to(device_id, {
            'type': 'ospf_log',
            'message': vendor_log.ospf_start(
                vnet.ws_send_callbacks.get(f'_type_{device_id}', 'cisco'),
                hostname, process_id, n['router_id'], n['area_id']
            )
        })
        if n['hello_task']:
            n['hello_task'].cancel()
        n['hello_task'] = _spawn(self._hello_loop(device_id))

    def add_network(self, device_id: str, network: str, area: str):
        """追加のnetwork文でマルチエリア設定（hello再起動も行う）"""
        n = self._node(device_id)
        norm_area = _normalize_area(area)
        self.area_networks.setdefault(device_id, {})
        self.area_networks[device_id].setdefault(norm_area, [])
        if network not in self.area_networks[device_id][norm_area]:
            self.area_networks[device_id][norm_area].append(network)
        if network not in n['networks']:
            n['networks'].append(network)
        # ABRかどうか判定（複数エリアに属する）
        if len(self.area_networks.get(device_id, {})) > 1:
            n['abr'] = True

    async def stop(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        if n['hello_task']:
            n['hello_task'].cancel()
        for nbr in n['neighbors'].values():
            if nbr.dead_task:
                nbr.dead_task.cancel()
        n['enabled'] = False
        n['neighbors'] = {}
        n['dr'] = None
        n['bdr'] = None

    async def interface_down(self, device_id: str, peer_ids: set):
        """インターフェースダウン時: 対象ピアとのネイバーを即座に削除してルート再計算"""
        n = self.nodes.get(device_id)
        if not n:
            return
        removed = False
        for peer_id in list(peer_ids):
            if peer_id in n['neighbors']:
                nbr = n['neighbors'].pop(peer_id)
                if nbr.dead_task:
                    nbr.dead_task.cancel()
                await vnet.send_to(device_id, {
                    'type': 'ospf_log',
                    'message': (f'%OSPF-5-ADJCHG: Process {n["process_id"]}, '
                                f'Nbr {nbr.router_id} interface down, '
                                f'changing state from FULL to DOWN')
                })
                removed = True
        if removed:
            self._recalc_routes(device_id)

    async def _hello_loop(self, device_id: str):
        await asyncio.sleep(random.uniform(0.5, 1.5))
        while True:
            n = self.nodes.get(device_id)
            if not n or not n['enabled']:
                break
            await self._send_hello(device_id)
            await asyncio.sleep(n['hello_interval'])

    async def _send_hello(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return
        # passive-interface設定があればHello送信しない
        passive = self.passive_ifaces.get(device_id, set())
        if 'all' in passive:
            return
        pkt = {
            'type': 'ospf_hello',
            'src_id': device_id,
            'src_hostname': n['hostname'],
            'router_id': n['router_id'],
            'area_id': n['area_id'],
            'hello_interval': n['hello_interval'],
            'dead_interval': n['dead_interval'],
            'priority': n['priority'],
            'dr': n['dr'],
            'bdr': n['bdr'],
            'neighbors': list(n['neighbors'].keys()),
            'networks': n['networks'],
            'external_routes': dict(n.get('redistributed', {})),
            'cost': n['interface_cost'],
        }
        await vnet.broadcast_to_neighbors(device_id, pkt)

    async def receive_hello(self, receiver_id: str, msg: dict):
        n = self.nodes.get(receiver_id)
        if not n or not n['enabled']:
            return
        src_id = msg.get('src_id')
        src_hostname = msg.get('src_hostname', src_id)
        router_id = msg.get('router_id', src_id)
        area_id = msg.get('area_id')
        src_priority = msg.get('priority', 1)
        src_dr = msg.get('dr')
        src_bdr = msg.get('bdr')

        if _normalize_area(area_id) != n['area_id']:
            return  # エリア不一致

        # hello/dead interval確認（不一致なら隣接不可）
        if msg.get('hello_interval') and msg['hello_interval'] != n['hello_interval']:
            return

        # 外部ルート記録
        ext = msg.get('external_routes', {})
        if ext:
            n.setdefault('_learned_external', {})
            for net, info in ext.items():
                n['_learned_external'][net] = {
                    'metric': info.get('metric', 20),
                    'next_hop': src_id,
                    'src_hostname': src_hostname,
                }

        existing = n['neighbors'].get(src_id)
        if not existing:
            # 新規隣接 → Init → 2Way → ExStart → Exchange → Loading → Full
            nbr = OspfNeighbor(neighbor_id=src_id, router_id=router_id,
                                hostname=src_hostname, state='Init')
            n['neighbors'][src_id] = nbr
            self._reset_dead_timer(receiver_id, src_id)

            # DBD交換シミュレーション（高速）
            await asyncio.sleep(0.3)
            nbr.state = 'TwoWay'
            await asyncio.sleep(0.2)
            nbr.state = 'ExStart'
            await asyncio.sleep(0.2)
            nbr.state = 'Exchange'
            await asyncio.sleep(0.3)
            nbr.state = 'Loading'
            await asyncio.sleep(0.2)
            nbr.state = 'Full'
            self._generate_router_lsa(receiver_id)
            await self._exchange_lsa(receiver_id, src_id)
            self._inject_external_routes(receiver_id)

            # Full確立後にDR/BDR選出（全候補が揃った状態で実施）
            await self._elect_dr_bdr(receiver_id)
            # DR/BDR役割をshow出力用に決定
            self._update_neighbor_role(receiver_id, src_id)

            await vnet.send_to(receiver_id, {
                'type': 'ospf_log',
                'message': (f'%OSPF-5-ADJCHG: Process {n["process_id"]}, '
                            f'Nbr {router_id} on GigabitEthernet from LOADING to FULL, '
                            f'Loading Done')
            })
            await vnet.send_to(receiver_id, {'type': 'ospf_routes', 'routes': n['routes']})
        else:
            # 既存隣接 → Dead timer更新
            self._reset_dead_timer(receiver_id, src_id)
            if ext:
                self._inject_external_routes(receiver_id)

    def _update_neighbor_role(self, device_id: str, neighbor_id: str):
        """ネイバーのDR/BDR役割を更新"""
        n = self.nodes.get(device_id)
        if not n:
            return
        nbr = n['neighbors'].get(neighbor_id)
        if not nbr:
            return
        if n['dr'] == neighbor_id:
            nbr.role = 'DR'
        elif n['bdr'] == neighbor_id:
            nbr.role = 'BDR'
        else:
            nbr.role = 'DROther'

    async def _elect_dr_bdr(self, device_id: str):
        """
        DR/BDR選出（Cisco仕様準拠）:
        1. priority最大のルータがDR（priority=0は対象外）
        2. 同じpriorityならRouter IDが大きい方
        3. BDRはDR次点
        """
        n = self.nodes.get(device_id)
        if not n:
            return
        # 候補リスト: 自分 + ネイバー全員
        candidates = []
        if n['priority'] > 0:
            candidates.append({
                'device_id': device_id,
                'router_id': n['router_id'],
                'priority': n['priority'],
            })
        for nid, nbr in n['neighbors'].items():
            nbr_node = self.nodes.get(nid)
            if not nbr_node:
                continue
            pri = nbr_node.get('priority', 1)
            if pri > 0:
                candidates.append({
                    'device_id': nid,
                    'router_id': nbr.router_id,
                    'priority': pri,
                })
        if not candidates:
            return
        # priority降順 → Router ID降順でソート
        def _rid_to_int(rid: str) -> int:
            try:
                parts = rid.split('.')
                return sum(int(p) << (8 * (3 - i)) for i, p in enumerate(parts))
            except Exception:
                return abs(hash(rid))
        candidates.sort(key=lambda c: (c['priority'], _rid_to_int(c['router_id'])),
                        reverse=True)
        dr_candidate = candidates[0]
        bdr_candidate = candidates[1] if len(candidates) > 1 else None
        prev_dr = n.get('dr')
        n['dr'] = dr_candidate['device_id']
        n['bdr'] = bdr_candidate['device_id'] if bdr_candidate else None
        if prev_dr != n['dr']:
            role = 'DR' if n['dr'] == device_id else 'non-DR'
            await vnet.send_to(device_id, {
                'type': 'ospf_log',
                'message': vendor_log.ospf_dr_elected(
                    vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'),
                    n.get('hostname', device_id),
                    n.get('process_id', 1),
                    dr_candidate['router_id'],
                    dr_candidate['priority'],
                    'GigabitEthernet0/0'
                )
            })
            # DRになった場合はType2 Network LSAを生成
            if n['dr'] == device_id:
                self._generate_network_lsa(device_id)

    def _reset_dead_timer(self, device_id: str, neighbor_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        nbr = n['neighbors'].get(neighbor_id)
        if not nbr:
            return
        if nbr.dead_task:
            nbr.dead_task.cancel()
        async def dead():
            await asyncio.sleep(n['dead_interval'])
            nd = self.nodes.get(device_id)
            if not nd:
                return
            nb = nd['neighbors'].pop(neighbor_id, None)
            if nb:
                await vnet.send_to(device_id, {
                    'type': 'ospf_log',
                    'message': (f'%OSPF-5-ADJCHG: Process {nd["process_id"]}, '
                                f'Nbr {nb.router_id} from FULL to DOWN, Dead timer expired')
                })
                self._recalc_routes(device_id)
        nbr.dead_task = _spawn(dead())

    def _generate_router_lsa(self, device_id: str):
        """
        LSA Type1 (Router LSA) 生成
        - 自ルーターが直接接続するネットワークとネイバーリンクを記載
        - Stub Network: 直接接続ネットワーク（エンドユーザー側）
        - Transit Network: DR/BDRが選出されたブロードキャストネットワーク
        - Point-to-Point: ポイントツーポイントリンク
        """
        n = self.nodes.get(device_id)
        if not n:
            return
        links = []
        # ① スタブネットワーク（直接接続ネットワーク）
        for net in n['networks']:
            parts = net.split('/')
            prefix = int(parts[1]) if len(parts) > 1 else 24
            mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff
            mask = '.'.join(str((mask_int >> (8 * (3 - i))) & 0xff) for i in range(4))
            links.append({
                'type': 'stub',           # Type3: Stub Network
                'link_id': parts[0],      # ネットワークアドレス
                'link_data': mask,        # サブネットマスク
                'metric': n['interface_cost'],
                'network': net,
            })
        # ② トランジットリンク（ネイバーへのリンク）
        for nid, nbr in n['neighbors'].items():
            if nbr.state != 'Full':
                continue
            nbr_node = self.nodes.get(nid, {})
            dr = n.get('dr')
            if dr:
                # Broadcast ネットワーク → Transit リンク
                links.append({
                    'type': 'transit',    # Type2: Transit (DR接続)
                    'link_id': nbr.router_id,  # DRのルーターID
                    'link_data': n['router_id'],
                    'metric': n['interface_cost'],
                    'neighbor_id': nid,
                })
            else:
                # P2P リンク
                links.append({
                    'type': 'p2p',        # Type1: Point-to-Point
                    'link_id': nbr.router_id,
                    'link_data': '255.255.255.255',
                    'metric': n['interface_cost'],
                    'neighbor_id': nid,
                })

        key = f'{n["router_id"]}:1:{n["router_id"]}'
        n['lsdb'][key] = OspfLsa(
            ls_type=1, ls_id=n['router_id'], adv_router=n['router_id'],
            seq_num=n['seq_num'], checksum=random.randint(0x1000, 0xffff),
            age=0, links=links
        )
        n['seq_num'] += 1

    def _generate_network_lsa(self, device_id: str):
        """
        LSA Type2 (Network LSA) 生成 — DRのみ生成
        - DRがブロードキャストネットワークを代表してフラッディング
        - 接続ルーター一覧を含む
        - DR以外はType2 LSAを生成しない
        """
        n = self.nodes.get(device_id)
        if not n or n.get('dr') != device_id:
            return   # DR以外は生成しない

        attached_routers = [n['router_id']]
        for nid, nbr in n['neighbors'].items():
            if nbr.state == 'Full':
                nbr_node = self.nodes.get(nid, {})
                attached_routers.append(nbr_node.get('router_id', nid))

        # ネットワークアドレスをLink IDに使用
        net_addr = n['networks'][0].split('/')[0] if n['networks'] else n['router_id']
        prefix = int(n['networks'][0].split('/')[1]) if n['networks'] and '/' in n['networks'][0] else 24
        mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff
        mask = '.'.join(str((mask_int >> (8 * (3 - i))) & 0xff) for i in range(4))

        key = f'{n["router_id"]}:2:{net_addr}'
        n['lsdb'][key] = OspfLsa(
            ls_type=2, ls_id=net_addr, adv_router=n['router_id'],
            seq_num=n['seq_num'], checksum=random.randint(0x1000, 0xffff),
            age=0,
            links=[{
                'type': 'network',
                'network_mask': mask,
                'attached_routers': attached_routers,
                'dr_router_id': n['router_id'],
            }]
        )
        n['seq_num'] += 1

    def _generate_summary_lsa(self, device_id: str):
        """
        LSA Type3 (Summary LSA / Network Summary LSA) 生成 — ABRのみ生成
        - ABRがエリア内ネットワーク情報をバックボーン（Area0）に広告
        - また他エリアのネットワーク情報を各スタブエリアに広告
        - metric = 広告元エリア内のコスト
        """
        n = self.nodes.get(device_id)
        if not n or not n.get('abr'):
            return
        areas = self.area_networks.get(device_id, {})
        all_area_nets = []
        for area_id, nets in areas.items():
            for net in nets:
                all_area_nets.append((area_id, net))

        # 全エリアのネットワークをLSDB内にType3 LSAとして格納
        for area_id, net in all_area_nets:
            parts = net.split('/')
            if len(parts) != 2:
                continue
            prefix = int(parts[1])
            mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff
            mask = '.'.join(str((mask_int >> (8 * (3 - i))) & 0xff) for i in range(4))
            key = f'{n["router_id"]}:3:{parts[0]}'
            n['lsdb'][key] = OspfLsa(
                ls_type=3, ls_id=parts[0], adv_router=n['router_id'],
                seq_num=n['seq_num'], checksum=random.randint(0x1000, 0xffff),
                age=0, links=[{
                    'type': 'summary',
                    'network': net,
                    'network_mask': mask,
                    'metric': n['interface_cost'],
                    'area_id': area_id,
                }]
            )
            n['seq_num'] += 1

    def _recalc_routes(self, device_id: str):
        """
        Dijkstra SPF計算（RFC 2328準拠）
        - Type1 LSA: ルーター間リンクの探索（transit/p2p）
        - Type2 LSA: ブロードキャストネットワーク経由の探索
        - Type3 LSA: ABRからのエリア間経路（O IA）
        - Type5 LSA: ASBRからの外部経路（O E1/E2）
        """
        n = self.nodes.get(device_id)
        if not n:
            return

        routes = []
        my_rid = n['router_id']

        # SPF候補ツリー
        # {router_id: (cost, via_device, next_hop)}
        dist = {my_rid: (0, 'direct', '')}
        visited = set()
        pq = [(0, my_rid, 'direct', '')]  # (cost, router_id, via, next_hop)

        while pq:
            pq.sort(key=lambda x: x[0])
            cost, cur_rid, via, nh = pq.pop(0)
            if cur_rid in visited:
                continue
            visited.add(cur_rid)

            for lsa in n['lsdb'].values():
                if lsa.adv_router != cur_rid:
                    continue

                if lsa.ls_type == 1:
                    # Type1: Router LSA
                    for link in lsa.links:
                        ltype = link.get('type', '')
                        link_cost = cost + link.get('metric', 10)

                        if ltype == 'stub':
                            # スタブネットワーク → 経路として追加
                            net = link.get('network', '')
                            prefix = int(net.split('/')[1]) if '/' in net else 24
                            if net:
                                via_str = via if via != 'direct' else 'connected'
                                routes.append({
                                    'network': link['link_id'],
                                    'prefix': str(prefix),
                                    'metric': link_cost,
                                    'via': via_str,
                                    'next_hop': nh or cur_rid,
                                    'type': 'O',
                                    'area': n['area_id'],
                                })

                        elif ltype in ('transit', 'p2p'):
                            nbr_rid = link['link_id']
                            if nbr_rid not in visited:
                                new_nh = nh if nh else nbr_rid
                                new_via = via if via != 'direct' else nbr_rid
                                if nbr_rid not in dist or dist[nbr_rid][0] > link_cost:
                                    dist[nbr_rid] = (link_cost, new_via, new_nh)
                                    pq.append((link_cost, nbr_rid, new_via, new_nh))

                elif lsa.ls_type == 2:
                    # Type2: Network LSA（DRが生成）
                    for link in lsa.links:
                        for att_rid in link.get('attached_routers', []):
                            if att_rid != my_rid and att_rid not in visited:
                                link_cost = cost + 1
                                new_nh = nh if nh else att_rid
                                new_via = via if via != 'direct' else att_rid
                                if att_rid not in dist or dist[att_rid][0] > link_cost:
                                    dist[att_rid] = (link_cost, new_via, new_nh)
                                    pq.append((link_cost, att_rid, new_via, new_nh))

                elif lsa.ls_type == 3:
                    # Type3: Summary LSA（ABRが生成、エリア間経路 O IA）
                    for link in lsa.links:
                        net = link.get('network', '')
                        if not net:
                            continue
                        parts = net.split('/')
                        prefix = int(parts[1]) if len(parts) > 1 else 24
                        link_cost = cost + link.get('metric', 10)
                        routes.append({
                            'network': parts[0],
                            'prefix': str(prefix),
                            'metric': link_cost,
                            'via': via if via != 'direct' else lsa.adv_router,
                            'next_hop': nh or lsa.adv_router,
                            'type': 'O IA',
                            'area': link.get('area_id', '0.0.0.0'),
                        })

        # 重複除去（コスト最小を残す）
        best = {}
        for r in routes:
            key = f'{r["network"]}/{r["prefix"]}'
            if key not in best or best[key]['metric'] > r['metric']:
                best[key] = r
        n['routes'] = list(best.values())
        self._inject_external_routes(device_id)
        _spawn(vnet.send_to(device_id, {
            'type': 'ospf_log',
            'message': vendor_log.ospf_spf(
                        vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'),
                        n['hostname'], n.get('process_id',1), len(n['routes']))
        }))

    async def _exchange_lsa(self, device_id: str, neighbor_id: str):
        """
        LSA交換（DBD/LSR/LSU/LSAck相当）
        Full状態確立後にLSDBを相互マージし、SPFを再計算する。
        """
        n = self.nodes.get(device_id)
        nbr_node = self.nodes.get(neighbor_id)
        if not n or not nbr_node:
            return
        # Type1 Router LSA最新化
        self._generate_router_lsa(device_id)
        self._generate_router_lsa(neighbor_id)
        # Type2 Network LSA（DR側が生成）
        if n.get('dr') == device_id:
            self._generate_network_lsa(device_id)
        if nbr_node.get('dr') == neighbor_id:
            self._generate_network_lsa(neighbor_id)
        # Type3 Summary LSA（ABR側が生成）
        self._generate_summary_lsa(device_id)
        self._generate_summary_lsa(neighbor_id)
        # LSDB相互マージ
        for key, lsa in list(nbr_node['lsdb'].items()):
            if key not in n['lsdb'] or n['lsdb'][key].seq_num < lsa.seq_num:
                n['lsdb'][key] = OspfLsa(
                    ls_type=lsa.ls_type, ls_id=lsa.ls_id,
                    adv_router=lsa.adv_router, seq_num=lsa.seq_num,
                    checksum=lsa.checksum, age=lsa.age,
                    links=list(lsa.links),
                )
        for key, lsa in list(n['lsdb'].items()):
            if key not in nbr_node['lsdb'] or nbr_node['lsdb'][key].seq_num < lsa.seq_num:
                nbr_node['lsdb'][key] = OspfLsa(
                    ls_type=lsa.ls_type, ls_id=lsa.ls_id,
                    adv_router=lsa.adv_router, seq_num=lsa.seq_num,
                    checksum=lsa.checksum, age=lsa.age,
                    links=list(lsa.links),
                )
        # 両側でSPF再計算
        self._recalc_routes(device_id)
        self._recalc_routes(neighbor_id)
        await vnet.send_to(neighbor_id, {
            'type': 'ospf_routes', 'routes': nbr_node['routes']
        })

    def _inject_external_routes(self, device_id: str):
        """再配信ルート（O E2）をroutesに追加"""
        n = self.nodes.get(device_id)
        if not n:
            return
        learned = n.get('_learned_external', {})
        existing = {f"{r['network']}/{r['prefix']}" for r in n['routes']}
        for net, info in learned.items():
            if net in existing:
                continue
            parts = net.split('/')
            if len(parts) != 2:
                continue
            n['routes'].append({
                'network': parts[0], 'prefix': parts[1],
                'metric': info.get('metric', 20),
                'via': info.get('next_hop', ''),
                'next_hop': info.get('next_hop', ''),
                'type': 'O E2',
                'external': True,
            })

    # ── show コマンド出力 ──────────────────
    def format_show_ospf_neighbor(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% OSPF is not configured on this device.'
        lines = [
            'Neighbor ID     Pri   State           Dead Time   Address         Interface'
        ]
        if not n['neighbors']:
            lines.append('(No neighbors)')
            return '\n'.join(lines)
        for nid, nbr in n['neighbors'].items():
            nbr_node = self.nodes.get(nid)
            pri = nbr_node.get('priority', 1) if nbr_node else 1
            # DR/BDR/DROther判定
            role = getattr(nbr, 'role', None)
            if not role:
                if n.get('dr') == nid:
                    role = 'DR'
                elif n.get('bdr') == nid:
                    role = 'BDR'
                else:
                    role = 'DROTHER'
            state_str = f'{nbr.state}/{role}'
            # Dead timeカウントダウン（HH:MM:SS形式）
            elapsed = time.time() - nbr.uptime
            dead_left = max(0, n['dead_interval'] - (elapsed % n['dead_interval']))
            h = int(dead_left // 3600)
            m = int((dead_left % 3600) // 60)
            s_ = int(dead_left % 60)
            dead_str = f'{h:02d}:{m:02d}:{s_:02d}'
            # ネイバーIPは相手ノードのIPから推定（実IPがあれば使用）
            peer_node = self.nodes.get(nid, {})
            peer_ips = list(peer_node.get('_peer_ips', {}).values())
            peer_ip = peer_ips[0] if peer_ips else f'10.0.{abs(hash(nid))%200+1}.1'
            iface = nbr.iface if hasattr(nbr, 'iface') and nbr.iface else 'GigabitEthernet0/0/0'
            lines.append(
                f'{nbr.router_id:<16}{pri:<6}{state_str:<16}{dead_str:<12}'
                f'{peer_ip:<16}{iface}'
            )
        return '\n'.join(lines)

    def format_show_ospf_database(self, device_id: str) -> str:
        """show ip ospf database — Type1/2/3 LSA表示"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% OSPF is not configured on this device.'

        lines = [
            f'            OSPF Router with ID ({n["router_id"]}) (Process ID {n["process_id"]})',
            '',
        ]

        # Type1: Router LSA
        type1 = [lsa for lsa in n['lsdb'].values() if lsa.ls_type == 1]
        lines += [
            f'\t\tRouter Link States (Area {n["area_id"]})',
            '',
            'Link ID         ADV Router      Age  Seq#       Checksum Link count',
        ]
        if type1:
            for lsa in sorted(type1, key=lambda l: l.ls_id):
                age = min(3600, int(time.time() - lsa.timestamp))
                lines.append(
                    f'{lsa.ls_id:<16}{lsa.adv_router:<16}{age:<5}'
                    f'0x{lsa.seq_num:08x}  0x{lsa.checksum:04x}   {len(lsa.links)}'
                )
        else:
            lines.append('(none)')

        # Type2: Network LSA（DRが生成）
        type2 = [lsa for lsa in n['lsdb'].values() if lsa.ls_type == 2]
        if type2:
            lines += [
                '',
                f'\t\tNet Link States (Area {n["area_id"]})',
                '',
                'Link ID         ADV Router      Age  Seq#       Checksum',
            ]
            for lsa in sorted(type2, key=lambda l: l.ls_id):
                age = min(3600, int(time.time() - lsa.timestamp))
                lines.append(
                    f'{lsa.ls_id:<16}{lsa.adv_router:<16}{age:<5}'
                    f'0x{lsa.seq_num:08x}  0x{lsa.checksum:04x}'
                )
                # 接続ルーター一覧
                for link in lsa.links:
                    for att in link.get('attached_routers', []):
                        lines.append(f'   Attached Router: {att}')

        # Type3: Summary LSA（ABRが生成、エリア間）
        type3 = [lsa for lsa in n['lsdb'].values() if lsa.ls_type == 3]
        if type3:
            lines += [
                '',
                '\t\tSummary Net Link States (Area 0.0.0.0)',
                '',
                'Link ID         ADV Router      Age  Seq#       Checksum',
            ]
            for lsa in sorted(type3, key=lambda l: l.ls_id):
                age = min(3600, int(time.time() - lsa.timestamp))
                lines.append(
                    f'{lsa.ls_id:<16}{lsa.adv_router:<16}{age:<5}'
                    f'0x{lsa.seq_num:08x}  0x{lsa.checksum:04x}'
                )

        # Type5: AS External LSA（ASBRが生成）
        type5 = [lsa for lsa in n['lsdb'].values() if lsa.ls_type == 5]
        if type5:
            lines += [
                '',
                '\t\tAS External Link States',
                '',
                'Link ID         ADV Router      Age  Seq#       Checksum Tag',
            ]
            for lsa in sorted(type5, key=lambda l: l.ls_id):
                age = min(3600, int(time.time() - lsa.timestamp))
                lines.append(
                    f'{lsa.ls_id:<16}{lsa.adv_router:<16}{age:<5}'
                    f'0x{lsa.seq_num:08x}  0x{lsa.checksum:04x}  0'
                )

        # LSDB統計
        lines += [
            '',
            f'  LSA count: {len(n["lsdb"])} '
            f'(Type1:{len(type1)} Type2:{len(type2)} '
            f'Type3:{len(type3)} Type5:{len(type5)})',
        ]
        return '\n'.join(lines)

    def format_show_ospf_route(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% OSPF is not configured on this device.'
        if not n['routes']:
            return ('OSPF Router with ID (' + n['router_id'] + ')\n'
                    '(No OSPF routes)')
        lines = [
            f'OSPF Router with ID ({n["router_id"]}) (Process ID {n["process_id"]})',
            '',
            'Routing Table',
            '',
            'Destination        Cost     Type    NextHop          AdvRouter        Area',
        ]
        for r in n['routes']:
            rtype = r.get('type', 'O')
            lines.append(
                f'{r["network"]}/{r["prefix"]:<14}{str(r["metric"]):<9}'
                f'{rtype:<8}{str(r.get("next_hop","")):<17}'
                f'{str(r.get("via","")):<17}{n["area_id"]}'
            )
        ia = sum(1 for r in n['routes'] if 'IA' in r.get('type', ''))
        e2 = sum(1 for r in n['routes'] if 'E2' in r.get('type', ''))
        intra = len(n['routes']) - ia - e2
        lines.append('')
        lines.append(f'Total nets: {len(n["routes"])}')
        lines.append(f'Intra area: {intra}  Inter area: {ia}  ASE: {e2}  NSSA: 0')
        return '\n'.join(lines)

    def format_show_ospf_interface(self, device_id: str) -> str:
        """show ip ospf interface"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% OSPF is not configured on this device.'
        passive = self.passive_ifaces.get(device_id, set())
        dr_str = n['dr'] if n['dr'] else 'none'
        bdr_str = n['bdr'] if n['bdr'] else 'none'
        is_dr = (n['dr'] == device_id)
        is_bdr = (n['bdr'] == device_id)
        role = 'DR' if is_dr else ('BDR' if is_bdr else 'DROther')
        lines = [f'GigabitEthernet0/0/0 is up, line protocol is up']
        lines.append(f'  Internet Address 192.168.1.1/24, Area {n["area_id"]}')
        lines.append(f'  Process ID {n["process_id"]}, Router ID {n["router_id"]}, Network Type BROADCAST, Cost: {n["interface_cost"]}')
        lines.append(f'  Transmit Delay is 1 sec, State {role}, Priority {n["priority"]}')
        lines.append(f'  Designated Router (ID) {n["router_id"]}, Interface address 192.168.1.1')
        if is_bdr or not is_dr:
            lines.append(f'  Backup Designated router (ID) {n["router_id"]}')
        lines.append(f'  Timer intervals configured, Hello {n["hello_interval"]}, Dead {n["dead_interval"]}, Retransmit 5')
        if passive:
            lines.append(f'  No Hellos (Passive interface)')
        lines.append(f'  Neighbor Count is {len(n["neighbors"])}, Adjacent neighbor count is {len(n["neighbors"])}')
        return '\n'.join(lines)

# ══════════════════════════════════════════
# BGP エンジン
# ══════════════════════════════════════════
@dataclass
class BgpSession:
    neighbor_id: str
    hostname: str
    remote_as: int
    state: str = 'Idle'   # Idle/Active/OpenSent/OpenConfirm/Established
    uptime: Optional[float] = None
    prefixes_received: int = 0
    prefixes_sent: int = 0
    keepalive_task: Optional[Any] = None

@dataclass
class BgpRoute:
    prefix: str
    prefix_len: int
    next_hop: str
    local_pref: int = 100
    med: int = 0
    as_path: List[int] = field(default_factory=list)
    origin: str = 'i'
    learned_from: str = ''
    learned_from_hostname: str = ''

class BgpEngine:
    def __init__(self):
        self.nodes: Dict[str, dict] = {}

    def _node(self, device_id: str) -> dict:
        if device_id not in self.nodes:
            self.nodes[device_id] = {
                'enabled': False,
                'local_as': None,
                'router_id': f'10.1.0.{device_id}',
                'hostname': device_id,
                'sessions': {},    # neighbor_id -> BgpSession
                'rib_in': [],      # List[BgpRoute]
                'loc_rib': [],     # ベストパス
                'networks': [],    # 広告ネットワーク
                'keepalive_interval': 30,
            }
        return self.nodes[device_id]

    async def start(self, device_id: str, hostname: str, local_as: int):
        n = self._node(device_id)
        n['enabled'] = True
        n['local_as'] = int(local_as)
        n['hostname'] = hostname
        n['router_id'] = f'10.1.0.{abs(hash(device_id)) % 254 + 1}'
        await vnet.send_to(device_id, {
            'type': 'bgp_log',
            'message': vendor_log.bgp_start(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), n['hostname'], local_as, n['router_id'])
        })

    async def add_neighbor(self, device_id: str, neighbor_id: str,
                            neighbor_hostname: str, remote_as: int):
        n = self._node(device_id)
        if not n['enabled']:
            return
        session = BgpSession(neighbor_id=neighbor_id, hostname=neighbor_hostname,
                              remote_as=int(remote_as))
        n['sessions'][neighbor_id] = session
        await vnet.send_to(device_id, {
            'type': 'bgp_log',
            'message': vendor_log.bgp_neighbor_change(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), n['hostname'], neighbor_ip if 'neighbor_ip' in dir() else neighbor_hostname, remote_as, 'Idle')
        })
        await self._open_session(device_id, neighbor_id)

    async def _open_session(self, device_id: str, neighbor_id: str):
        n = self.nodes.get(device_id)
        session = n['sessions'].get(neighbor_id) if n else None
        if not session:
            return
        nbr_node = self.nodes.get(neighbor_id)
        if not nbr_node or not nbr_node.get('enabled'):
            session.state = 'Active'
            return

        session.state = 'OpenSent'
        await vnet.send_to(device_id, {
            'type': 'bgp_log',
            'message': vendor_log.bgp_neighbor_change(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), n['hostname'], session.hostname, session.remote_as, 'Established')
        })

        await asyncio.sleep(1.5)

        session.state = 'Established'
        session.uptime = time.time()
        await vnet.send_to(device_id, {
            'type': 'bgp_state', 'neighbor_id': neighbor_id, 'state': 'Established',
            'message': f'%BGP-5-ADJCHANGE: neighbor {session.hostname} Up'
        })
        # 相手側にも通知
        nbr_session = nbr_node['sessions'].get(device_id)
        if nbr_session:
            nbr_session.state = 'Established'
            nbr_session.uptime = time.time()
            await vnet.send_to(neighbor_id, {
                'type': 'bgp_state', 'neighbor_id': device_id, 'state': 'Established',
                'message': f'%BGP-5-ADJCHANGE: neighbor {n["hostname"]} Up'
            })

        # UPDATE送信（双方向で全ネットワークを広告）
        await asyncio.sleep(0.3)
        await self._send_update(device_id, neighbor_id)
        await self._send_update(neighbor_id, device_id)
        # 確実性のため再送
        await asyncio.sleep(0.3)
        await self._send_update(device_id, neighbor_id)
        await self._send_update(neighbor_id, device_id)
        # Keepaliveタイマー
        self._start_keepalive(device_id, neighbor_id)
        self._start_keepalive(neighbor_id, device_id)

    def _start_keepalive(self, device_id: str, neighbor_id: str):
        n = self.nodes.get(device_id)
        session = n['sessions'].get(neighbor_id) if n else None
        if not session:
            return
        if session.keepalive_task:
            session.keepalive_task.cancel()
        async def keepalive():
            while True:
                await asyncio.sleep(n['keepalive_interval'])
                if session.state != 'Established':
                    break
        session.keepalive_task = _spawn(keepalive())

    async def _send_update(self, device_id: str, neighbor_id: str):
        n = self.nodes.get(device_id)
        nbr_node = self.nodes.get(neighbor_id)
        if not n or not nbr_node:
            return
        session = n['sessions'].get(neighbor_id)
        if not session or session.state != 'Established':
            return

        routes = [BgpRoute(prefix=net.split('/')[0],
                           prefix_len=int(net.split('/')[1]) if '/' in net else 24,
                           next_hop='0.0.0.0',
                           as_path=[n['local_as']], origin='i')
                  for net in n['networks']]
        if not routes:
            return

        for r in routes:
            key = f'{r.prefix}/{r.prefix_len}'
            existing = next((x for x in nbr_node['rib_in']
                             if x.prefix == r.prefix and x.prefix_len == r.prefix_len
                             and x.learned_from == device_id), None)
            if not existing:
                new_r = BgpRoute(prefix=r.prefix, prefix_len=r.prefix_len,
                                 next_hop=device_id, local_pref=100, med=0,
                                 as_path=r.as_path.copy(), origin=r.origin,
                                 learned_from=device_id,
                                 learned_from_hostname=n['hostname'])
                nbr_node['rib_in'].append(new_r)
                nbr_node['prefixes_received'] = len(nbr_node['rib_in'])

        session.prefixes_sent += len(routes)
        await vnet.send_to(device_id, {
            'type': 'bgp_log',
            'message': vendor_log.ios('BGP', 6, 'TABLECHG', f'Neighbor {session.hostname} UPDATE sent, {len(routes)} prefix(es)')
        })
        await vnet.send_to(neighbor_id, {
            'type': 'bgp_log',
            'message': vendor_log.ios('BGP', 6, 'TABLECHG', f'Neighbor {n["hostname"]} (AS{n["local_as"]}) UPDATE received, {len(routes)} prefix(es)')
        })
        self._recalc_best_path(neighbor_id)
        await vnet.send_to(neighbor_id, {'type': 'bgp_routes',
                                          'routes': nbr_node['loc_rib']})

    def _recalc_best_path(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        best = {}
        for r in n['rib_in']:
            key = f'{r.prefix}/{r.prefix_len}'
            if key not in best:
                best[key] = r
            else:
                existing = best[key]
                if r.local_pref > existing.local_pref:
                    best[key] = r
                elif r.local_pref == existing.local_pref and \
                        len(r.as_path) < len(existing.as_path):
                    best[key] = r
        n['loc_rib'] = [{'prefix': r.prefix, 'prefix_len': r.prefix_len,
                          'next_hop': r.next_hop, 'local_pref': r.local_pref,
                          'med': r.med, 'as_path': r.as_path, 'origin': r.origin,
                          'learned_from_hostname': r.learned_from_hostname}
                         for r in best.values()]

    async def advertise_network(self, device_id: str, network: str):
        n = self._node(device_id)
        if network not in n['networks']:
            n['networks'].append(network)
        for neighbor_id, session in n['sessions'].items():
            if session.state == 'Established':
                await self._send_update(device_id, neighbor_id)

    def format_show_bgp_summary(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% BGP is not configured.'
        lines = [
            f'BGP router identifier {n["router_id"]}, local AS number {n["local_as"]}',
            'BGP table version is 1, main routing table version 1',
            '',
            'Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd',
        ]
        for nid, session in n['sessions'].items():
            uptime = '00:00:00'
            if session.uptime:
                elapsed = int(time.time() - session.uptime)
                h, m, s = elapsed//3600, (elapsed%3600)//60, elapsed%60
                uptime = f'{h:02d}:{m:02d}:{s:02d}'
            # この neighbor から受信した prefix 数を実カウント
            pfx_count = sum(1 for r in n['rib_in'] if r.learned_from == nid)
            state_or_pfx = str(pfx_count) \
                if session.state == 'Established' else session.state
            lines.append(
                f'{"192.168.1."+str(nid):<16}4{str(session.remote_as):>6}'
                f'{random.randint(10,200):>8}{random.randint(10,200):>8}'
                f'{1:>9}{0:>5}{0:>5} {uptime:<9} {state_or_pfx}'
            )
        return '\n'.join(lines)

    def format_show_bgp_table(self, device_id: str) -> str:
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% BGP is not configured.'
        lines = [
            f'BGP table version is 1, local router ID is {n["router_id"]}',
            'Status codes: s suppressed, d damped, h history, * valid, > best, i - internal',
            'Origin codes: i - IGP, e - EGP, ? - incomplete',
            '',
            '   Network          Next Hop            Metric LocPrf Weight Path',
        ]
        for r in n['loc_rib']:
            lines.append(
                f'*> {r["prefix"]+"/"+(str(r["prefix_len"])):<18} {"0.0.0.0":<20} '
                f'{str(r["med"]):>6} {str(r["local_pref"]):>6} {32768:>6} '
                f'{" ".join(str(a) for a in r["as_path"])} {r["origin"]}'
            )
        for r in n['rib_in']:
            key = f'{r.prefix}/{r.prefix_len}'
            if not any(f'{x["prefix"]}/{x["prefix_len"]}' == key for x in n['loc_rib']):
                lines.append(
                    f'*  {key:<18} {"192.168.1."+str(r.learned_from):<20} '
                    f'{r.med:>6} {r.local_pref:>6} {0:>6} '
                    f'{" ".join(str(a) for a in r.as_path)} {r.origin}'
                )
        return '\n'.join(lines)


# ══════════════════════════════════════════
# STP / RSTP エンジン
# ══════════════════════════════════════════
PORT_STATE = {'BLOCKING': 'BLK', 'LISTENING': 'LIS', 'LEARNING': 'LRN',
              'FORWARDING': 'FWD', 'DISABLED': 'DIS'}
PORT_ROLE  = {'ROOT': 'Root', 'DESIGNATED': 'Desgn', 'ALTERNATE': 'Altn',
              'BACKUP': 'Backup', 'DISABLED': 'Disabled'}

class StpEngine:
    def __init__(self):
        self.nodes: Dict[str, dict] = {}
        # device_id -> {vlan_id -> node_dict}  (Rapid-PVST+ 独立インスタンス)
        self.pvst_nodes: Dict[str, Dict[int, dict]] = {}
        # device_id -> {port_name -> {'portfast':bool,'bpduguard':bool,'bpdufilter':bool}}
        self.port_config: Dict[str, Dict[str, dict]] = {}
        # グローバル設定
        self.portfast_default: Dict[str, bool] = {}
        self.bpduguard_default: Dict[str, bool] = {}

    def _mac(self, device_id: str) -> str:
        h = abs(hash(device_id)) % 0xffffff
        return f'00:0e:0e:{h>>16:02x}:{(h>>8)&0xff:02x}:{h&0xff:02x}'

    def _vlan_bridge_id(self, device_id: str, vlan: int, priority: int) -> str:
        """
        Rapid PVST+: Bridge ID = Priority(4bit) + SystemIDExt(12bit=VLAN ID) + MAC(48bit)
        例: priority=32768, VLAN=10 → 32778.mac
        """
        mac = self._mac(device_id)
        extended_priority = priority + vlan  # sys-id-ext にVLAN IDを加算
        return f'{extended_priority}.{mac}'

    def _make_vlan_node(self, device_id: str, vlan: int, priority: int = 32768,
                        mode: str = 'rstp') -> dict:
        """VLAN別STPインスタンスノードを作成"""
        mac = self._mac(device_id)
        bridge_id = self._vlan_bridge_id(device_id, vlan, priority)
        return {
            'enabled': True,
            'mode': mode,
            'hostname': device_id,
            'bridge_priority': priority,
            'bridge_mac': mac,
            'bridge_id': bridge_id,
            'root_bridge_id': bridge_id,   # 初期は自分がroot
            'root_path_cost': 0,
            'root_port': None,
            'ports': {},
            'vlan': vlan,
            'dr': None, 'bdr': None,
            'bpdu_task': None,
            'tc_count': 0,
            'portfast_default': False,
            'bpduguard_default': False,
        }

    def _get_vlan_node(self, device_id: str, vlan: int) -> Optional[dict]:
        """VLAN別インスタンスを取得（なければNone）"""
        return self.pvst_nodes.get(device_id, {}).get(vlan)

    def _node(self, device_id: str, vlan: int = 1) -> dict:
        """後方互換性のため残す（VLAN1のデフォルトノード）"""
        if device_id not in self.nodes:
            mac = self._mac(device_id)
            self.nodes[device_id] = {
                'enabled': False,
                'mode': 'rstp',
                'hostname': device_id,
                'bridge_priority': 32768,
                'bridge_mac': mac,
                'bridge_id': f'8000.{mac}',
                'root_bridge_id': f'8000.{mac}',
                'root_path_cost': 0,
                'root_port': None,
                'ports': {},
                'vlan': vlan,
                'vlans': {vlan: {'priority': 32768, 'root': None}},
                'bpdu_task': None,
                'tc_count': 0,
                'portfast_default': False,
                'bpduguard_default': False,
            }
        return self.nodes[device_id]

    # ── PortFast / BPDUGuard 設定 ──────────────
    def set_portfast(self, device_id: str, port_name: str, enabled: bool):
        self.port_config.setdefault(device_id, {})
        self.port_config[device_id].setdefault(port_name, {})
        self.port_config[device_id][port_name]['portfast'] = enabled
        n = self.nodes.get(device_id)
        if n and port_name in n['ports']:
            n['ports'][port_name]['portfast'] = enabled
            if enabled:
                # PortFast: 即座にFORWARDING遷移
                n['ports'][port_name]['state'] = 'FORWARDING'
                n['ports'][port_name]['role'] = 'DESIGNATED'
                _spawn(vnet.send_to(device_id, {
                    'type': 'stp_log',
                    'message': vendor_log.ios('SPANTREE', 5, 'PORTFAST', f'Interface {port_name} edge port type was enabled because the interface is spanning-tree portfast enabled')
                }))

    def set_portfast_default(self, device_id: str, enabled: bool):
        """spanning-tree portfast default — 全アクセスポートにPortFastを適用"""
        n = self._node(device_id)
        n['portfast_default'] = enabled
        self.portfast_default[device_id] = enabled
        if enabled:
            for pname, p in n['ports'].items():
                if p.get('vlan') != 'trunk':
                    p['portfast'] = True
                    p['state'] = 'FORWARDING'

    def set_bpduguard(self, device_id: str, port_name: str, enabled: bool):
        self.port_config.setdefault(device_id, {})
        self.port_config[device_id].setdefault(port_name, {})
        self.port_config[device_id][port_name]['bpduguard'] = enabled
        n = self.nodes.get(device_id)
        if n and port_name in n['ports']:
            n['ports'][port_name]['bpduguard'] = enabled

    def set_bpduguard_default(self, device_id: str, enabled: bool):
        """spanning-tree portfast bpduguard default"""
        n = self._node(device_id)
        n['bpduguard_default'] = enabled
        self.bpduguard_default[device_id] = enabled

    def _bridge_id(self, device_id: str) -> str:
        n = self._node(device_id)
        pri_hex = f'{n["bridge_priority"]:04x}'
        return f'{pri_hex}.{n["bridge_mac"]}'

    def _compare_bridge_id(self, a: str, b: str) -> int:
        """a < b なら負、a > b なら正"""
        pa, ma = a.split('.', 1)
        pb, mb = b.split('.', 1)
        pri_diff = int(pa, 16) - int(pb, 16)
        if pri_diff != 0:
            return pri_diff
        return -1 if ma < mb else (1 if ma > mb else 0)

    async def start(self, device_id: str, hostname: str, mode: str = 'rstp',
                    priority: int = 32768, vlan: int = 1):
        # 後方互換ノード
        n = self._node(device_id, vlan)
        n['enabled'] = True
        n['mode'] = mode
        n['hostname'] = hostname
        n['bridge_priority'] = priority
        n['bridge_id'] = self._vlan_bridge_id(device_id, vlan, priority)
        n['root_bridge_id'] = n['bridge_id']
        n['root_path_cost'] = 0
        n['vlan'] = vlan
        n.setdefault('vlans', {})[vlan] = {'priority': priority, 'root': None}

        # PVST+ 独立インスタンス作成
        self.pvst_nodes.setdefault(device_id, {})[vlan] = self._make_vlan_node(
            device_id, vlan, priority, mode
        )

        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': (f'%SPANTREE-5-EXTENDED_SYSID: Extended sysid enabled for type vlan\n'
                        f'%SPANTREE-5-ROOTCHANGE: Root Changed for vlan {vlan}: new Bridge ID is {n["bridge_id"]}')
        })
        self._init_ports(device_id)
        # PVST+インスタンスにもポートをコピー
        self.pvst_nodes[device_id][vlan]['ports'] = dict(n['ports'])

        if n['bpdu_task']:
            n['bpdu_task'].cancel()
        interval = 1 if mode == 'rstp' else 2
        n['bpdu_task'] = _spawn(self._bpdu_loop(device_id, interval))
        return n['bridge_id']

    def set_vlan_priority(self, device_id: str, vlan: int, priority: int):
        """spanning-tree vlan <vlan-id> priority <priority>"""
        n = self._node(device_id)
        n.setdefault('vlans', {})[vlan] = {'priority': priority, 'root': None}
        n['bridge_priority'] = priority
        n['bridge_id'] = self._vlan_bridge_id(device_id, vlan, priority)

        # PVST+ 独立インスタンスを更新/作成
        if device_id not in self.pvst_nodes:
            self.pvst_nodes[device_id] = {}
        if vlan not in self.pvst_nodes[device_id]:
            self.pvst_nodes[device_id][vlan] = self._make_vlan_node(
                device_id, vlan, priority, n.get('mode', 'rstp')
            )
            self.pvst_nodes[device_id][vlan]['ports'] = dict(n.get('ports', {}))
        else:
            vi = self.pvst_nodes[device_id][vlan]
            vi['bridge_priority'] = priority
            vi['bridge_id'] = self._vlan_bridge_id(device_id, vlan, priority)
            vi['root_bridge_id'] = vi['bridge_id']  # 再選出
            vi['root_path_cost'] = 0

        # 既に起動中なら即座にBPDU再送してルートブリッジ再選出
        if n.get('enabled'):
            n['root_bridge_id'] = n['bridge_id']
            n['root_path_cost'] = 0
            _spawn(self._send_bpdu(device_id))

        _spawn(vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': (f'%SPANTREE-5-EXTENDED_SYSID: vlan {vlan}: priority {priority} '
                        f'(sys-id-ext {vlan}) configured')
        }))

    def _init_ports(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        # 接続済みの隣接ノードからポートを生成
        for i, peer_id in enumerate(vnet.get_neighbors(device_id), 1):
            port_name = f'ether {i}'
            if port_name not in n['ports']:
                n['ports'][port_name] = {
                    'name': port_name, 'state': 'FORWARDING',
                    'role': 'DESIGNATED', 'cost': 19,
                    'priority': 128, 'connected_to': peer_id,
                    'bpdu_sent': 0, 'bpdu_received': 0,
                }

    def add_port(self, device_id: str, port_name: str, peer_id: str):
        n = self._node(device_id)
        n['ports'][port_name] = {
            'name': port_name, 'state': 'BLOCKING',
            'role': 'DESIGNATED', 'cost': 19,
            'priority': 128, 'connected_to': peer_id,
            'bpdu_sent': 0, 'bpdu_received': 0,
        }
        if n['enabled']:
            _spawn(vnet.send_to(device_id, {
                'type': 'stp_log',
                'message': vendor_log.ios('SPANTREE', 6, 'PORTDPVSTP', f'Port {port_name} instance 0 moving from INIT to BLOCKING')
            }))
            _spawn(self._elect_root(device_id))

    async def _bpdu_loop(self, device_id: str, interval: float):
        await asyncio.sleep(0.5)
        while True:
            n = self.nodes.get(device_id)
            if not n or not n['enabled']:
                break
            await self._send_bpdu(device_id)
            await asyncio.sleep(interval)

    async def _send_bpdu(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return
        pkt = {
            'type': 'stp_bpdu', 'src_id': device_id,
            'src_hostname': n['hostname'], 'mode': n['mode'],
            'root_bridge_id': n['root_bridge_id'],
            'root_path_cost': n['root_path_cost'],
            'bridge_id': n['bridge_id'],
            'hello_time': 1 if n['mode'] == 'rstp' else 2,
            'max_age': 20, 'forward_delay': 15,
        }
        await vnet.broadcast_to_neighbors(device_id, pkt)

    async def receive_bpdu(self, receiver_id: str, msg: dict):
        receiver = self.nodes.get(receiver_id)
        if not receiver or not receiver['enabled']:
            return
        src_id = msg.get('src_id')
        root_bid = msg.get('root_bridge_id')
        root_cost = msg.get('root_path_cost', 0)
        sender_bid = msg.get('bridge_id')

        # このポート（src_idに繋がるポート）で受信したBPDUを記録
        port = None
        for p in receiver['ports'].values():
            if p['connected_to'] == src_id:
                port = p
                break
        if port is None:
            return

        # ── BPDUGuard チェック ──
        # PortFastが有効なポートでBPDUを受信 → err-disabled
        port_name = port.get('name', '')
        pcfg = self.port_config.get(receiver_id, {}).get(port_name, {})
        is_portfast = port.get('portfast', False) or pcfg.get('portfast', False)
        is_bpduguard = (port.get('bpduguard', False) or pcfg.get('bpduguard', False)
                        or receiver.get('bpduguard_default', False))
        if is_portfast and is_bpduguard:
            port['state'] = 'ERR_DISABLED'
            port['role'] = 'DISABLED'
            port['err_reason'] = 'bpduguard'
            _spawn(vnet.send_to(receiver_id, {
                'type': 'stp_log',
                'message': (f'%SPANTREE-2-BLOCK_BPDUGUARD: '
                            f'Received BPDU on port {port_name} with BPDU Guard enabled. '
                            f'Disabling port.\n'
                            f'%PM-4-ERR_DISABLE: bpduguard error detected on {port_name}, '
                            f'putting {port_name} in err-disable state')
            }))
            return

        # ── Edge Port 降格（802.1w Section 9.3.4）──
        # PortFast(Edge Port)が有効なポートでBPDUを受信した場合
        # BPDUGuardが無効であれば → Edge Portを非Edgeに降格（PortFast無効化）
        if is_portfast and not is_bpduguard:
            port['portfast'] = False
            if port_name in self.port_config.get(receiver_id, {}):
                self.port_config[receiver_id][port_name]['portfast'] = False
            # Edge Port降格 → 通常のRSTPネゴシエーション開始（DISCARDING→LEARNING→FORWARDING）
            port['state'] = 'DISCARDING'
            _spawn(vnet.send_to(receiver_id, {
                'type': 'stp_log',
                'message': (f'%SPANTREE-2-PORTFAST_DISABLED: '
                            f'PortFast disabled on port {port_name} because BPDU was received. '
                            f'Port will go through normal STP transitions.')
            }))
            _spawn(self._rstp_rapid_transition(receiver_id, port_name, port))

        # ── Root Guard チェック（Cisco 802.1w準拠）──
        # Root Guardが有効なポートで上位ブリッジからBPDUを受信 → Root Inconsistent
        is_root_guard = port.get('root_guard', False) or pcfg.get('root_guard', False)
        if is_root_guard:
            # 受信したBPDUのRoot Bridge IDが自分より優先度が高い（上位ブリッジ）
            if self._compare_bridge_id(root_bid, receiver['bridge_id']) < 0:
                if port.get('state') != 'ROOT_INCONSISTENT':
                    port['state'] = 'ROOT_INCONSISTENT'
                    port['role'] = 'ALTERNATE'
                    receiver['tc_count'] = receiver.get('tc_count', 0) + 1
                    receiver['tc_last_time'] = time.time()
                    _spawn(vnet.send_to(receiver_id, {
                        'type': 'stp_log',
                        'message': (f'%SPANTREE-2-ROOTGUARD_CONFIG_CHANGE: '
                                    f'Root guard blocking port {port_name} on '
                                    f'{receiver["hostname"]}. '
                                    f'Inconsistent port. Root Bridge ID: {root_bid}')
                    }))
                return  # Root Inconsistentなので通常処理をスキップ
            else:
                # Root Guardで遮断していたポートが正常BPDUを受信 → 復旧
                if port.get('state') == 'ROOT_INCONSISTENT':
                    port['state'] = 'FORWARDING'
                    _spawn(vnet.send_to(receiver_id, {
                        'type': 'stp_log',
                        'message': (f'%SPANTREE-2-ROOTGUARD_UNBLOCK: '
                                    f'Root guard unblocking port {port_name} on '
                                    f'{receiver["hostname"]}. Consistent port.')
                    }))

        # 受信BPDU情報を保存（後でポート役割決定に使う）
        port['rx_root_bid'] = root_bid
        port['rx_root_cost'] = root_cost
        port['rx_sender_bid'] = sender_bid
        port['rx_time'] = time.time()

        # TCフラグ受信処理
        if msg.get('topology_change'):
            receiver['tc_count'] = receiver.get('tc_count', 0) + 1
            receiver['tc_last_time'] = time.time()
            _spawn(vnet.send_to(receiver_id, {
                'type': 'stp_log',
                'message': (f'%SPANTREE-6-TOPOTRAP: Topology change detected on '
                            f'{msg.get("src_hostname", src_id)} '
                            f'(累計 TC count: {receiver["tc_count"]})')
            }))

        # 自分が知っているルートより良いルートを学習
        better_root = self._compare_bridge_id(root_bid, receiver['root_bridge_id']) < 0
        same_root_better_cost = (
            root_bid == receiver['root_bridge_id'] and
            root_cost + port['cost'] < receiver['root_path_cost']
        )
        if better_root or same_root_better_cost:
            prev_root = receiver['root_bridge_id']
            receiver['root_bridge_id'] = root_bid
            receiver['root_path_cost'] = root_cost + port['cost']
            src_hostname = msg.get('src_hostname', src_id)
            if better_root:
                await vnet.send_to(receiver_id, {
                    'type': 'stp_log',
                    'message': vendor_log.stp_root_change(vnet.ws_send_callbacks.get(f'_type_{receiver_id}','cisco'), receiver['hostname'], 1, root_bid)
                })

        # 全ポートの役割を再計算（ループブロッキング含む）
        self._recompute_port_roles(receiver_id)

    def _recompute_port_roles(self, device_id: str):
        """
        STPポート役割を正しく決定（ループ防止）。
        - Root Port: ルートへの最小コストポート（1つ）
        - Designated Port: セグメントで自分が最良ブリッジ
        - Alternate Port: それ以外（Blocking）
        """
        n = self.nodes.get(device_id)
        if not n:
            return

        is_root_bridge = (n['root_bridge_id'] == n['bridge_id'])

        if is_root_bridge:
            # ルートブリッジの全ポートはDesignated/Forwarding
            for port in n['ports'].values():
                port['role'] = 'DESIGNATED'
                port['state'] = 'FORWARDING'
            _spawn(vnet.send_to(device_id, {
                'type': 'stp_ports', 'ports': list(n['ports'].values())
            }))
            return

        # 非ルートブリッジ: まずルートポートを決定
        # = ルートへの最小コストを広告しているポート
        best_port = None
        best_cost = float('inf')
        best_sender = None
        for port in n['ports'].values():
            if 'rx_root_cost' not in port:
                continue
            # このポート経由のルートまでのコスト
            path_cost = port['rx_root_cost'] + port['cost']
            sender = port.get('rx_sender_bid', '')
            # ルートBIDが一致するBPDUのみ評価
            if port.get('rx_root_bid') != n['root_bridge_id']:
                continue
            if path_cost < best_cost or (
                path_cost == best_cost and
                (best_sender is None or self._compare_bridge_id(sender, best_sender) < 0)
            ):
                best_cost = path_cost
                best_port = port
                best_sender = sender

        # 各ポートの役割決定
        for port in n['ports'].values():
            if port is best_port:
                # ルートポート
                port['role'] = 'ROOT'
                port['state'] = 'FORWARDING' if n['mode'] == 'rstp' else 'FORWARDING'
            elif 'rx_sender_bid' in port:
                # 受信BPDUがある = 対向ブリッジが存在
                # 自分のルートパスコスト vs 対向が広告するコスト を比較
                my_cost = n['root_path_cost']
                their_cost = port['rx_root_cost']
                if their_cost < my_cost:
                    # 対向の方がルートに近い → 自分はこのセグメントで非指定
                    # → Alternate (Blocking) でループ防止
                    port['role'] = 'ALTERNATE'
                    port['state'] = 'BLOCKING'
                elif their_cost == my_cost:
                    # コスト同じ → ブリッジID小さい方がDesignated
                    if self._compare_bridge_id(n['bridge_id'], port['rx_sender_bid']) < 0:
                        port['role'] = 'DESIGNATED'
                        port['state'] = 'FORWARDING'
                    else:
                        port['role'] = 'ALTERNATE'
                        port['state'] = 'BLOCKING'
                else:
                    # 自分の方がルートに近い → Designated
                    port['role'] = 'DESIGNATED'
                    port['state'] = 'FORWARDING'
            else:
                # BPDU未受信のポート → Designated（エッジポート想定）
                port['role'] = 'DESIGNATED'
                port['state'] = 'FORWARDING'

        # ブロッキングポートがあればログ
        for port in n['ports'].values():
            if port['role'] == 'ALTERNATE':
                _spawn(vnet.send_to(device_id, {
                    'type': 'stp_log',
                    'message': f'%SPANTREE-6-PORTDPVSTP: Port {port["name"]} instance 0 '
                               f'state: BLOCKING (ループ防止)'
                }))

        _spawn(vnet.send_to(device_id, {
            'type': 'stp_ports', 'ports': list(n['ports'].values())
        }))

    def _update_port_states(self, device_id: str, root_port_peer_id: str):
        # 旧メソッド互換（新ロジックを呼ぶ）
        self._recompute_port_roles(device_id)

    async def _elect_root(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        if n['root_bridge_id'] == n['bridge_id']:
            for port in n['ports'].values():
                port['role'] = 'DESIGNATED'
                port['state'] = 'FORWARDING'
            await vnet.send_to(device_id, {
                'type': 'stp_log',
                'message': vendor_log.ios('SPANTREE', 5, 'ROOTCHANGE', f'This bridge is now the root bridge')
            })
        await vnet.send_to(device_id, {
            'type': 'stp_ports', 'ports': list(n['ports'].values())
        })

    def stop(self, device_id: str):
        n = self.nodes.get(device_id)
        if not n:
            return
        if n['bpdu_task']:
            n['bpdu_task'].cancel()
        n['enabled'] = False

    async def port_down(self, device_id: str, peer_id: str):
        """
        リンク障害発生時の処理（実機STPのTCN/再収束を再現）。
        1. 障害ポートを削除
        2. TCN（Topology Change Notification）を隣接に送信
        3. ALTERNATEポートがあれば即座にFORWARDING遷移（RSTP高速収束）
        4. ルートポートが消えた場合はルートブリッジ再選出
        """
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return

        # 障害ポートを特定して削除
        down_port = None
        down_port_name = None
        for pname, p in list(n['ports'].items()):
            if p.get('connected_to') == peer_id:
                down_port = p
                down_port_name = pname
                break
        if not down_port:
            return

        was_root_port = (down_port['role'] == 'ROOT')
        was_alt_port = (down_port['role'] == 'ALTERNATE')
        del n['ports'][down_port_name]

        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': (f'%LINK-3-UPDOWN: Interface {down_port_name}, changed state to down\n'
                        f'{n["mode"].upper()}: {down_port_name} — Port Down (接続先: {peer_id})')
        })

        # TC カウントを増やす
        n['tc_count'] = n.get('tc_count', 0) + 1
        n['tc_last_time'] = time.time()

        # TCN を隣接全員に通知
        await self._send_tcn(device_id)

        if was_root_port:
            # ルートポートが消えた → ルートブリッジ再選出が必要
            await vnet.send_to(device_id, {
                'type': 'stp_log',
                'message': vendor_log.ios('SPANTREE', 5, 'ROOTCHANGE', f'Root port lost. Initiating re-election...')
            })
            # ルートを自分自身にリセットして再収束を待つ
            n['root_bridge_id'] = n['bridge_id']
            n['root_path_cost'] = 0
            n['root_port'] = None
            # 残りのALTERNATEポートをFORWARDINGに昇格（RSTP高速収束）
            for pname, p in n['ports'].items():
                if p['role'] == 'ALTERNATE':
                    await self._rstp_rapid_transition(device_id, pname, p)
            self._recompute_port_roles(device_id)
            await vnet.send_to(device_id, {
                'type': 'stp_topology_change',
                'message': f'Topology Change: Root port failed, reconverging...'
            })
        else:
            # 非ルートポートが消えた → ポート役割を再計算するだけ
            self._recompute_port_roles(device_id)

    async def port_up(self, device_id: str, peer_id: str):
        """リンク回復時の処理"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return
        # 新しいポートを追加してSTP収束させる
        port_num = len(n['ports']) + 1
        port_name = f'ether {port_num}'
        n['ports'][port_name] = {
            'name': port_name,
            'state': 'DISCARDING' if n['mode'] == 'rstp' else 'BLOCKING',
            'role': 'DESIGNATED',
            'cost': 19, 'priority': 128,
            'connected_to': peer_id,
            'bpdu_sent': 0, 'bpdu_received': 0,
        }
        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': f'%LINK-3-UPDOWN: Interface {port_name}, changed state to up (peer: {peer_id}), '
                       f'state: {"DISCARDING" if n["mode"] == "rstp" else "BLOCKING"}'
        })
        # RSTP: Proposal/Agreementで高速遷移
        if n['mode'] == 'rstp':
            await self._rstp_rapid_transition(device_id, port_name, n['ports'][port_name])

    async def _rstp_rapid_transition(self, device_id: str, port_name: str, port: dict):
        """
        RSTPのProposal/Agreement高速遷移（実機は~1秒）。
        DISCARDING → LEARNING → FORWARDING
        """
        n = self.nodes.get(device_id)
        if not n:
            return
        # DISCARDING → LEARNING
        port['state'] = 'LEARNING'
        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': f'{n["mode"].upper()}: {port_name} → LEARNING (Proposal/Agreement)'
        })
        await asyncio.sleep(0.3)  # 高速タイマーでは0.3秒
        # LEARNING → FORWARDING
        port['state'] = 'FORWARDING'
        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': vendor_log.stp_port_state(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), n.get('hostname',device_id), port_name, 'LEARNING', 'FORWARDING')
        })
        await vnet.send_to(device_id, {
            'type': 'stp_topology_change',
            'message': f'Topology Change: {port_name} transitioned to FORWARDING'
        })

    async def _send_tcn(self, device_id: str):
        """TCN（Topology Change Notification）を隣接に送信"""
        n = self.nodes.get(device_id)
        if not n:
            return
        pkt = {
            'type': 'stp_bpdu',
            'src_id': device_id,
            'src_hostname': n['hostname'],
            'mode': n['mode'],
            'root_bridge_id': n['root_bridge_id'],
            'root_path_cost': n['root_path_cost'],
            'bridge_id': n['bridge_id'],
            'hello_time': 1, 'max_age': 20, 'forward_delay': 15,
            'topology_change': True,   # TCフラグ
            'tc_count': n.get('tc_count', 0),
        }
        await vnet.broadcast_to_neighbors(device_id, pkt)
        await vnet.send_to(device_id, {
            'type': 'stp_log',
            'message': (f'%SPANTREE-6-TOPOTRAP: Topology change detected on port ion '
                        f'(TC count: {n.get("tc_count",0)})')
        })

    def format_show_spanning_tree(self, device_id: str) -> str:
        """show spanning-tree — デフォルトVLAN1のSTP状態"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% Spanning tree is not configured.'
        # VLANが設定されていればそのVLAN、なければVLAN1
        vlan = n.get('vlan', 1)
        return self.format_show_spanning_tree_vlan(device_id, vlan)

    def format_show_spanning_tree_summary(self, device_id: str) -> str:
        """show spanning-tree summary"""
        n = self.nodes.get(device_id)
        if not n or not n['enabled']:
            return '% Spanning tree is not configured.'
        mode = n['mode']
        mode_str = 'rapid-pvst' if mode == 'rstp' else mode
        pf_default = 'enabled' if n.get('portfast_default') else 'disabled'
        bg_default = 'enabled' if n.get('bpduguard_default') else 'disabled'

        # ポート集計
        blk = sum(1 for p in n['ports'].values() if p['state'] in ('BLOCKING','DISCARDING','ERR_DISABLED'))
        lis = sum(1 for p in n['ports'].values() if p['state'] == 'LISTENING')
        lrn = sum(1 for p in n['ports'].values() if p['state'] == 'LEARNING')
        fwd = sum(1 for p in n['ports'].values() if p['state'] == 'FORWARDING')
        total = len(n['ports'])

        # VLAN別インスタンス
        vlans = self.pvst_nodes.get(device_id, {1: {}})
        vlan_list = sorted(vlans.keys()) if vlans else [n.get('vlan', 1)]

        lines = [
            f'Switch is in {mode_str} mode',
            f'Root bridge for: ' + ', '.join(
                f'VLAN{v:04d}' for v in vlan_list
                if n.get('root_bridge_id') == n.get('bridge_id')
            ) or 'none',
            f'Extended system ID           is enabled',
            f'Portfast Default             is {pf_default}',
            f'PortFast BPDU Guard Default  is {bg_default}',
            f'Portfast BPDU Filter Default is disabled',
            f'Loopguard Default            is disabled',
            f'EtherChannel misconfig guard is enabled',
            f'UplinkFast                   is disabled',
            f'BackboneFast                 is disabled',
            '',
            f'Name                   Blocking Listening Learning Forwarding STP Active',
            f'---------------------- -------- --------- -------- ---------- ----------',
        ]
        for v in vlan_list:
            vname = f'VLAN{v:04d}'
            lines.append(f'{vname:<23}{blk:<9}{lis:<10}{lrn:<9}{fwd:<11}{total}')
        lines.append(f'---------------------- -------- --------- -------- ---------- ----------')
        lines.append(f'{len(vlan_list)} vlans    {blk:<9}{lis:<10}{lrn:<9}{fwd:<11}{total}')
        return '\n'.join(lines)

    def format_show_spanning_tree_vlan(self, device_id: str, vlan: int) -> str:
        """show spanning-tree vlan <id> — PVST+インスタンス別表示"""
        vi = self.pvst_nodes.get(device_id, {}).get(vlan)
        n = self.nodes.get(device_id)
        if not n or not n.get('enabled'):
            return f'% Spanning tree instance for VLAN {vlan} does not exist.'
        if not vi:
            vi = n
        is_root = vi.get('root_bridge_id') == vi.get('bridge_id')
        pri = vi.get('bridge_priority', 32768)
        mac = vi.get('bridge_mac', n.get('bridge_mac',''))
        mode = vi.get('mode', n.get('mode','rstp'))
        tc_count = vi.get('tc_count', 0)
        tc_last = vi.get('tc_last_time')
        if tc_last:
            elapsed = int(time.time() - tc_last)
            tc_time_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        else:
            tc_time_str = 'never'
        lines = [
            f'VLAN{vlan:04d}',
            f'  Spanning tree enabled protocol {mode}',
            f'  Root ID    Priority    {pri + vlan}',
            f'             Address     {mac}',
        ]
        if is_root:
            lines.append('             This bridge is the root')
        else:
            lines.append(f'             Cost        {vi.get("root_path_cost", 0)}')
            lines.append(f'             Hello Time  2 sec  Max Age 20 sec  Forward Delay 15 sec')
        lines += [
            '',
            f'  Bridge ID  Priority    {pri + vlan}  (priority {pri} sys-id-ext {vlan})',
            f'             Address     {mac}',
            f'             Hello Time  {"1" if mode == "rstp" else "2"} sec  '
            f'Max Age 20 sec  Forward Delay 15 sec',
            f'             Topology Change count  {tc_count}',
            f'             Time since topology change {tc_time_str}',
            '',
            'Interface           Role Sts Cost      Prio.Nbr Type',
            '-' * 68,
        ]
        ports = vi.get('ports', n.get('ports', {}))
        for port in ports.values():
            state_short = PORT_STATE.get(port['state'], port['state'][:3])
            if port['state'] == 'ROOT_INCONSISTENT':
                state_short = 'BKN'
            rg_str = ' RG' if port.get('root_guard') else ''
            pf_str = ' P' if port.get('portfast') else ''
            role_short = {
                'ROOT': 'Root ', 'DESIGNATED': 'Desgn', 'ALTERNATE': 'Altn ',
                'BACKUP': 'Bkup ', 'DISABLED': 'Dis  ',
            }.get(port['role'], port['role'][:5])
            lines.append(
                f'{port["name"]:<20}{role_short} {state_short} {str(port["cost"]):<10}'
                f'{str(port["priority"])+".1":<9}P2p{pf_str}{rg_str}'
            )
        return '\n'.join(lines)

@dataclass
class StaticRoute:
    network: str
    prefix: int
    next_hop: str
    ad: int = 1
    iface: str = ''
    active: bool = True
    description: str = ''

# Administrative Distance 値（Cisco IOS 準拠）
AD_VALUES = {
    'connected':  0,
    'static':     1,
    'eigrp':     90,
    'ospf':     110,
    'rip':      120,
    'bgp_ext':   20,
    'bgp_int':  200,
    'unknown':  255,
}


class RibEngine:
    """
    各装置の統合ルーティングテーブルを管理。
    スタティック/RIP/OSPF/BGP のルートをAD値で比較し、最良ルートを選択。
    フローティングスタティック（高AD値のバックアップルート）に対応。
    """
    def __init__(self):
        self.nodes: Dict[str, dict] = {}

    def _node(self, device_id: str) -> dict:
        if device_id not in self.nodes:
            self.nodes[device_id] = {
                'static_routes': [],   # List[StaticRoute]
                'hostname': device_id,
            }
        return self.nodes[device_id]

    def add_static_route(self, device_id: str, hostname: str, network: str,
                         prefix: int, next_hop: str, ad: int = 1):
        n = self._node(device_id)
        n['hostname'] = hostname
        # 同一宛先で既存ルートがあり、ADが高い（悪い）場合のみ追加
        existing = [r for r in n['static_routes']
                    if r.network == network and r.prefix == prefix]
        if existing:
            best_existing_ad = min(r.ad for r in existing)
            if ad >= best_existing_ad:
                # より良いルートが既にある → フローティングとして追加だけ
                pass
            else:
                # 既存より良いAD → 既存を置換
                n['static_routes'] = [r for r in n['static_routes']
                                      if not (r.network == network and r.prefix == prefix)]
        route = StaticRoute(network=network, prefix=prefix, next_hop=next_hop, ad=ad)
        n['static_routes'].append(route)
        return route

    def remove_static_route(self, device_id: str, network: str, prefix: int,
                            next_hop: str = None):
        n = self.nodes.get(device_id)
        if not n:
            return
        n['static_routes'] = [
            r for r in n['static_routes']
            if not (r.network == network and r.prefix == prefix
                    and (next_hop is None or r.next_hop == next_hop))
        ]

    def set_nexthop_reachable(self, device_id: str, next_hop: str, reachable: bool):
        """next_hopの到達性を変更（リンクダウン時にフローティング切替）"""
        n = self.nodes.get(device_id)
        if not n:
            return
        for r in n['static_routes']:
            if r.next_hop == next_hop:
                r.active = reachable

    def get_best_routes(self, device_id: str) -> List[dict]:
        """
        全ソース（static/rip/ospf/bgp/connected）を集めてAD比較し、
        宛先ごとに最良ルートを返す。
        """
        candidates = []  # (network, prefix, next_hop, ad, source, metric, iface)

        # スタティック（active なものだけ）
        n = self.nodes.get(device_id)
        if n:
            for r in n['static_routes']:
                if r.active:
                    candidates.append({
                        'network': r.network, 'prefix': r.prefix,
                        'next_hop': r.next_hop, 'ad': r.ad,
                        'source': 'static', 'metric': 0, 'iface': r.iface,
                    })

        # RIP
        rnode = rip_engine.nodes.get(device_id)
        if rnode and rnode.get('enabled'):
            for r in rnode['table']:
                src = 'connected' if r.learned_from == 'direct' else 'rip'
                ad = AD_VALUES['connected'] if src == 'connected' else AD_VALUES['rip']
                candidates.append({
                    'network': r.network, 'prefix': r.prefix,
                    'next_hop': r.next_hop if r.learned_from != 'direct' else '0.0.0.0',
                    'ad': ad, 'source': src, 'metric': r.metric,
                    'iface': 'lan0',
                })

        # OSPF
        onode = ospf_engine.nodes.get(device_id)
        if onode and onode.get('enabled'):
            for r in onode.get('routes', []):
                src = 'connected' if r['via'] == 'direct' else 'ospf'
                ad = AD_VALUES['connected'] if src == 'connected' else AD_VALUES['ospf']
                candidates.append({
                    'network': r['network'], 'prefix': int(r['prefix']),
                    'next_hop': r.get('next_hop', '0.0.0.0'),
                    'ad': ad, 'source': src, 'metric': r['metric'],
                    'iface': 'lan0',
                })

        # BGP
        bnode = bgp_engine.nodes.get(device_id)
        if bnode and bnode.get('enabled'):
            for r in bnode.get('loc_rib', []):
                candidates.append({
                    'network': r['prefix'], 'prefix': r['prefix_len'],
                    'next_hop': r['next_hop'], 'ad': AD_VALUES['bgp_ext'],
                    'source': 'bgp', 'metric': r.get('med', 0),
                    'iface': 'lan0',
                })

        # 宛先ごとにAD最小（同ADならmetric最小）を選択
        best = {}
        for c in candidates:
            key = f"{c['network']}/{c['prefix']}"
            if key not in best:
                best[key] = c
            else:
                cur = best[key]
                if c['ad'] < cur['ad'] or (c['ad'] == cur['ad'] and c['metric'] < cur['metric']):
                    best[key] = c
        return list(best.values())

    def format_show_ip_route(self, device_id: str, hostname: str = '') -> str:
        """統合ルーティングテーブル表示（AD/メトリック付き）"""
        routes = self.get_best_routes(device_id)
        code_map = {'connected': 'C', 'static': 'S', 'rip': 'R',
                    'ospf': 'O', 'bgp': 'B'}
        lines = [
            'Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP',
            '       * - candidate default',
            '',
        ]
        if not routes:
            lines.append('(No routes - configure routing or static routes)')
            return '\n'.join(lines)
        # connected → static → 動的の順で表示
        order = {'connected': 0, 'static': 1, 'bgp': 2, 'ospf': 3, 'rip': 4}
        for r in sorted(routes, key=lambda x: (order.get(x['source'], 9), x['network'])):
            code = code_map.get(r['source'], '?')
            net = f"{r['network']}/{r['prefix']}"
            if r['source'] == 'connected':
                lines.append(f"{code}     {net} is directly connected, {r['iface']}")
            else:
                lines.append(
                    f"{code}     {net} [{r['ad']}/{r['metric']}] via {r['next_hop']}, {r['iface']}"
                )
        return '\n'.join(lines)

    def format_show_static(self, device_id: str) -> str:
        """スタティックルート一覧（フローティング状態込み）"""
        n = self.nodes.get(device_id)
        if not n or not n['static_routes']:
            return '(No static routes configured)'
        lines = ['Destination/Mask     Next-Hop          AD    Status      Type']
        # 宛先ごとにグループ化してアクティブ/フローティング判定
        by_dest = defaultdict(list)
        for r in n['static_routes']:
            by_dest[f'{r.network}/{r.prefix}'].append(r)
        for dest, rlist in by_dest.items():
            rlist_sorted = sorted(rlist, key=lambda x: x.ad)
            # 最小ADでactiveなものが使用中
            in_use = next((r for r in rlist_sorted if r.active), None)
            for r in rlist_sorted:
                if not r.active:
                    status = 'unreachable'
                elif r is in_use:
                    status = 'active'
                else:
                    status = 'floating'   # 待機中のバックアップ
                rtype = 'primary' if r.ad <= 1 else 'floating-backup'
                lines.append(
                    f'{dest:<21}{r.next_hop:<18}{r.ad:<6}{status:<12}{rtype}'
                )
        return '\n'.join(lines)


rib_engine = RibEngine()


# ══════════════════════════════════════════
# ICMP エンジン（ping / traceroute 実到達性判定）
# ══════════════════════════════════════════
class IcmpEngine:
    """
    送信元から宛先まで実際に経路をたどって到達性を判定。
    各装置のIP・接続ネットワークを把握し、RIBを参照してホップ転送する。
    """
    # DPD タイマーデフォルト値
    DPD_DETECT_SEC  = 30   # no crypto map 後、tunnelがDOWNになるまでの秒数
    IKE_NEGO_SEC    = 3    # crypto map 再適用後、ESTABLISHEDになるまでの秒数

    def __init__(self):
        import time as _time
        self._time = _time
        self.device_ips: Dict[str, dict] = {}
        # IPsecトンネル: {device_id: [{'local': ip, 'peer': ip, 'state': str, 'since': float}]}
        # state: 'established' | 'detecting' | 'down' | 'negotiating'
        self.ipsec_tunnels: Dict[str, list] = {}

    def register_ipsec(self, device_id: str, local_ip: str, peer_ip: str):
        """IPsecトンネルを登録。既存エントリは negotiating 状態で再開"""
        now = self._time.time()
        tunnels = self.ipsec_tunnels.setdefault(device_id, [])
        for t in tunnels:
            if t['local'] == local_ip and t['peer'] == peer_ip:
                if t['state'] not in ('established',):
                    t['state'] = 'negotiating'
                    t['since'] = now
                return
        tunnels.append({'local': local_ip, 'peer': peer_ip,
                        'state': 'negotiating', 'since': now})

    def clear_ipsec(self, device_id: str):
        """crypto map 削除 / interface shutdown → detecting 状態へ遷移"""
        now = self._time.time()
        tunnels = self.ipsec_tunnels.get(device_id, [])
        for t in tunnels:
            if t['state'] == 'established':
                t['state'] = 'detecting'
                t['since'] = now
            elif t['state'] == 'negotiating':
                # ネゴ中に削除 → すぐ down
                t['state'] = 'down'
                t['since'] = now
        # established でも detecting でもない（=設定なし）ならエントリ削除
        self.ipsec_tunnels[device_id] = [
            t for t in tunnels if t['state'] in ('detecting', 'down')
        ]
        if not self.ipsec_tunnels[device_id]:
            self.ipsec_tunnels.pop(device_id, None)

    def _advance_dpd(self, t: dict) -> str:
        """タイマーを進めて現在の state を返す"""
        now = self._time.time()
        elapsed = now - t.get('since', now)
        if t['state'] == 'detecting' and elapsed >= self.DPD_DETECT_SEC:
            t['state'] = 'down'
            t['since'] = now
        elif t['state'] == 'negotiating' and elapsed >= self.IKE_NEGO_SEC:
            t['state'] = 'established'
            t['since'] = now
        return t['state']

    def _has_ipsec_to(self, device_id: str, next_device_id: str) -> bool:
        """device_id から next_device_id へのIPsecトンネルが established か"""
        tunnels = self.ipsec_tunnels.get(device_id, [])
        if not tunnels:
            return False
        peer_ips = set(self.device_ips.get(next_device_id, {}).get('ips', {}).keys())
        for t in tunnels:
            state = self._advance_dpd(t)
            if state == 'established' and t['peer'] in peer_ips:
                return True
        return False

    def get_ipsec_sa_status(self, device_id: str) -> list:
        """show crypto ipsec sa 用のステータスリストを返す"""
        import time as _t
        now = _t.time()
        result = []
        for t in self.ipsec_tunnels.get(device_id, []):
            state = self._advance_dpd(t)
            elapsed = int(now - t.get('since', now))
            if state == 'established':
                remain = None
                label = 'ESTABLISHED'
            elif state == 'detecting':
                remain = max(0, self.DPD_DETECT_SEC - elapsed)
                label = f'DPD-DETECTING (DOWN まで {remain}s)'
            elif state == 'negotiating':
                remain = max(0, self.IKE_NEGO_SEC - elapsed)
                label = f'IKE-NEGOTIATING (確立まで {remain}s)'
            else:
                remain = None
                label = 'DOWN'
            result.append({
                'local': t['local'], 'peer': t['peer'],
                'state': state, 'label': label, 'elapsed': elapsed,
            })
        return result

    def register_device(self, device_id: str, hostname: str, interfaces: dict):
        ips = {}
        for ifname, info in interfaces.items():
            ip = info.get('ip', '')
            if ip:
                ips[ip] = info.get('prefix', 24)
        self.device_ips[device_id] = {'hostname': hostname, 'ips': ips,
                                       'interfaces': interfaces}

    @staticmethod
    def _to_int(addr: str) -> int:
        o = [int(x) for x in addr.split('.')]
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _ip_in_network(self, ip: str, network: str, prefix: int) -> bool:
        try:
            mask = (0xffffffff << (32 - prefix)) & 0xffffffff
            return (self._to_int(ip) & mask) == (self._to_int(network) & mask)
        except Exception:
            return False

    def _find_device_owning_ip(self, ip: str) -> Optional[str]:
        for dev_id, info in self.device_ips.items():
            if ip in info['ips']:
                return dev_id
        return None

    def _find_device_in_network(self, ip: str) -> Optional[str]:
        for dev_id, info in self.device_ips.items():
            for dev_ip, prefix in info['ips'].items():
                if self._ip_in_network(ip, dev_ip, prefix):
                    return dev_id
        return None

    def trace_path(self, src_id: str, dest_ip: str, max_hops: int = 30):
        src_info = self.device_ips.get(src_id, {})
        if dest_ip in src_info.get('ips', {}):
            return {'reachable': True, 'hops': [], 'reason': 'self',
                    'dest_device': src_id}

        dest_device = self._find_device_owning_ip(dest_ip) or \
                      self._find_device_in_network(dest_ip)

        hops = []
        visited = set()
        current = src_id
        ttl = max_hops

        while ttl > 0:
            if current in visited:
                return {'reachable': False, 'hops': hops, 'reason': 'loop detected'}
            visited.add(current)
            cur_info = self.device_ips.get(current, {})

            # 宛先と同一直結ネットワーク → 到達（最後のホップは宛先自身なので追加しない）
            for dev_ip, prefix in cur_info.get('ips', {}).items():
                if self._ip_in_network(dest_ip, dev_ip, prefix):
                    return {'reachable': True, 'hops': hops,
                            'reason': 'connected', 'dest_device': dest_device}

            # 次ホップデバイスとIPを解決
            next_hop_device, next_hop_ip = self._resolve_next_hop_detail(current, dest_ip)
            if not next_hop_device:
                # 経路なし: 現在デバイスがタイムアウト
                cur_ip = next(iter(cur_info.get('ips', {})), '?')
                hops.append({'device': current, 'hostname': cur_info.get('hostname', current),
                             'ip': cur_ip, 'timeout': True})
                return {'reachable': False, 'hops': hops, 'reason': 'no route to host'}

            next_info = self.device_ips.get(next_hop_device, {})
            has_ipsec = self._has_ipsec_to(current, next_hop_device)

            if has_ipsec:
                # IPsecトンネル: next_hopのLAN側IP（dest方向）を表示、WAN区間は透過
                tunnel_exit_ip = self._local_ip_toward(next_hop_device, dest_ip) or \
                                 next(iter(next_info.get('ips', {})), '?')
                hops.append({'device': next_hop_device,
                             'hostname': next_info.get('hostname', next_hop_device),
                             'ip': tunnel_exit_ip,
                             'ipsec': True})
            else:
                # 通常ルーティング: next_hop_ip（受信側インターフェースIP）を表示
                hops.append({'device': next_hop_device,
                             'hostname': next_info.get('hostname', next_hop_device),
                             'ip': next_hop_ip})

            current = next_hop_device
            ttl -= 1

        return {'reachable': False, 'hops': hops, 'reason': 'TTL exceeded'}

    def _resolve_next_hop(self, device_id: str, dest_ip: str) -> Optional[str]:
        dev, _ = self._resolve_next_hop_detail(device_id, dest_ip)
        return dev

    def _resolve_next_hop_detail(self, device_id: str, dest_ip: str):
        """(next_device_id, next_hop_ip) を返す"""
        best_routes = rib_engine.get_best_routes(device_id)
        matched = None
        matched_prefix = -1
        for r in best_routes:
            if self._ip_in_network(dest_ip, r['network'], r['prefix']):
                if r['prefix'] > matched_prefix:
                    matched = r
                    matched_prefix = r['prefix']
        if not matched:
            for r in best_routes:
                if r['network'] == '0.0.0.0' and r['prefix'] == 0:
                    matched = r
                    break
        if not matched:
            return None, None
        next_hop_ip = matched['next_hop']
        if next_hop_ip in ('0.0.0.0', ''):
            dev = self._find_device_owning_ip(dest_ip) or \
                  self._find_device_in_network(dest_ip)
            return dev, dest_ip
        nh_device = self._find_device_owning_ip(next_hop_ip)
        if nh_device:
            return nh_device, next_hop_ip
        for peer in vnet.get_neighbors(device_id):
            peer_info = self.device_ips.get(peer, {})
            if next_hop_ip in peer_info.get('ips', {}):
                return peer, next_hop_ip
        neighbors = vnet.get_neighbors(device_id)
        dev = next(iter(neighbors), None) if neighbors else None
        return dev, next_hop_ip

    def _local_ip_toward(self, device_id: str, next_hop_ip: str) -> Optional[str]:
        """next_hop_ipと同一セグメントの自分のIPを返す"""
        info = self.device_ips.get(device_id, {})
        for my_ip, prefix in info.get('ips', {}).items():
            if self._ip_in_network(next_hop_ip, my_ip, prefix):
                return my_ip
        return None

    def ping(self, src_id: str, dest_ip: str, count: int = 5) -> dict:
        result = self.trace_path(src_id, dest_ip)
        reachable = result['reachable']
        hop_count = max(1, len([h for h in result['hops'] if not h.get('timeout')]))
        ttl = 64 - (hop_count - 1)
        rtts = []
        if reachable:
            base = hop_count * random.uniform(0.2, 0.8)
            for _ in range(count):
                rtts.append(round(base + random.uniform(0, 1.5), 3))
        return {'reachable': reachable, 'reason': result.get('reason', ''),
                'ttl': ttl, 'rtts': rtts, 'count': count, 'hops': result['hops']}

    def traceroute(self, src_id: str, dest_ip: str) -> dict:
        result = self.trace_path(src_id, dest_ip)
        return {'reachable': result['reachable'],
                'reason': result.get('reason', ''), 'hops': result['hops']}


icmp_engine = IcmpEngine()


# ══════════════════════════════════════════
# 経路フィルタエンジン（prefix-list / distribute-list / route-map / route-manage）
# ══════════════════════════════════════════
@dataclass
class PrefixListEntry:
    seq: int
    action: str          # 'permit' | 'deny'
    network: str
    prefix: int
    ge: Optional[int] = None   # greater-equal
    le: Optional[int] = None   # less-equal

class FilterEngine:
    """
    プレフィックスリストと配布リストを管理し、経路のフィルタリングを行う。
    - prefix-list: 名前付きの permit/deny ルール集合
    - distribute-list: プロトコルの in/out 方向に prefix-list を適用
    - route-map(簡易): 再配信時のフィルタ
    - Si-R route-manage: Si-R 形式の経路制御（内部的に同じ仕組み）
    """
    def __init__(self):
        # device_id -> {list_name -> [PrefixListEntry]}
        self.prefix_lists: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        # device_id -> {(proto, direction) -> list_name}
        # 例: ('rip','out') -> 'FILTER1'
        self.distribute_lists: Dict[str, Dict[tuple, str]] = defaultdict(dict)
        # device_id -> {(target_proto, source_proto) -> list_name}  再配信フィルタ
        self.redist_filters: Dict[str, Dict[tuple, str]] = defaultdict(dict)

    # ── prefix-list 管理 ──
    def add_prefix_list(self, device_id: str, name: str, action: str,
                        network: str, prefix: int, seq: int = None,
                        ge: int = None, le: int = None):
        entries = self.prefix_lists[device_id][name]
        if seq is None:
            seq = (max([e.seq for e in entries], default=0) + 5)
        entries.append(PrefixListEntry(seq=seq, action=action, network=network,
                                       prefix=prefix, ge=ge, le=le))
        entries.sort(key=lambda e: e.seq)

    def clear_prefix_list(self, device_id: str, name: str):
        if name in self.prefix_lists[device_id]:
            del self.prefix_lists[device_id][name]

    # ── distribute-list 管理 ──
    def set_distribute_list(self, device_id: str, proto: str, direction: str,
                            list_name: str):
        self.distribute_lists[device_id][(proto, direction)] = list_name

    def set_redist_filter(self, device_id: str, target: str, source: str,
                          list_name: str):
        self.redist_filters[device_id][(target, source)] = list_name

    # ── マッチング ──
    @staticmethod
    def _ip_to_int(ip: str) -> int:
        o = [int(x) for x in ip.split('.')]
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _match_entry(self, entry: PrefixListEntry, network: str, prefix: int) -> bool:
        """1エントリにマッチするか"""
        try:
            # ネットワークアドレス部分が一致するか（prefix長まで）
            mask = (0xffffffff << (32 - entry.prefix)) & 0xffffffff
            if (self._ip_to_int(network) & mask) != (self._ip_to_int(entry.network) & mask):
                return False
            # ge/le 指定がある場合はプレフィックス長で判定
            if entry.ge is not None and prefix < entry.ge:
                return False
            if entry.le is not None and prefix > entry.le:
                return False
            # ge/le がなければプレフィックス長完全一致
            if entry.ge is None and entry.le is None:
                return prefix == entry.prefix
            return True
        except Exception:
            return False

    def check_prefix_list(self, device_id: str, list_name: str,
                          network: str, prefix: int) -> bool:
        """
        prefix-listでルートが許可されるか判定。
        マッチした最初のエントリのaction。どれにもマッチしなければ暗黙deny。
        list_nameが存在しなければ許可（フィルタなし）。
        """
        lists = self.prefix_lists.get(device_id, {})
        if list_name not in lists:
            return True  # リスト未定義 = フィルタなし = 許可
        for entry in lists[list_name]:
            if self._match_entry(entry, network, prefix):
                return entry.action == 'permit'
        return False  # 暗黙のdeny

    def filter_routes(self, device_id: str, proto: str, direction: str,
                      routes: list) -> list:
        """
        distribute-listに基づいて経路リストをフィルタ。
        routes: [{'network':.., 'prefix':..}, ...]
        戻り値: 許可された経路のみ
        """
        list_name = self.distribute_lists.get(device_id, {}).get((proto, direction))
        if not list_name:
            return routes  # フィルタなし
        return [r for r in routes
                if self.check_prefix_list(device_id, list_name,
                                          r['network'], int(r['prefix']))]

    def filter_redist(self, device_id: str, target: str, source: str,
                      network: str, prefix: int) -> bool:
        """再配信フィルタ: このルートを再配信してよいか"""
        list_name = self.redist_filters.get(device_id, {}).get((target, source))
        if not list_name:
            return True  # フィルタなし
        return self.check_prefix_list(device_id, list_name, network, prefix)

    def format_show_prefix_list(self, device_id: str, name: str = None) -> str:
        lists = self.prefix_lists.get(device_id, {})
        if not lists:
            return '(No prefix-lists configured)'
        out = []
        for lname, entries in lists.items():
            if name and lname != name:
                continue
            out.append(f'ip prefix-list {lname}: {len(entries)} entries')
            for e in entries:
                line = f'   seq {e.seq} {e.action} {e.network}/{e.prefix}'
                if e.ge is not None:
                    line += f' ge {e.ge}'
                if e.le is not None:
                    line += f' le {e.le}'
                out.append(line)
        return '\n'.join(out) if out else f'(prefix-list {name} not found)'


filter_engine = FilterEngine()


# ══════════════════════════════════════════
# ARP エンジン（IP→MAC解決・ARPテーブル）
# ══════════════════════════════════════════
@dataclass
class ArpEntry:
    ip: str
    mac: str
    iface: str
    age: float = field(default_factory=time.time)
    entry_type: str = 'dynamic'   # dynamic | static

class ArpEngine:
    """
    ARP（Address Resolution Protocol）を実装。
    同一セグメント内でIP→MACを解決し、ARPテーブルに記録する。
    pingやIP通信の前にARP解決が行われる想定。
    """
    def __init__(self):
        # device_id -> {ip -> ArpEntry}
        self.tables: Dict[str, Dict[str, ArpEntry]] = defaultdict(dict)
        self.arp_timeout = 14400  # 4時間（Cisco default）

    def _gen_mac(self, device_id: str, ip: str) -> str:
        """装置とIPから決定的にMACを生成"""
        h = abs(hash(f'{device_id}:{ip}')) % (16**8)
        return f'00:1b:0d:{(h>>16)&0xff:02x}:{(h>>8)&0xff:02x}:{h&0xff:02x}'

    def resolve(self, device_id: str, target_ip: str) -> Optional[str]:
        """
        target_ip のMACを解決。
        同一セグメントに相手がいればARPテーブルに記録してMACを返す。
        いなければNone（解決失敗）。
        """
        # 既にテーブルにあればそれを返す
        if target_ip in self.tables[device_id]:
            entry = self.tables[device_id][target_ip]
            # タイムアウトチェック
            if time.time() - entry.age < self.arp_timeout:
                return entry.mac
            else:
                del self.tables[device_id][target_ip]

        # ICMPエンジンの装置IP情報を使って同一セグメント判定
        src_info = icmp_engine.device_ips.get(device_id, {})
        # 自分のどのインタフェースと同じネットワークか
        for my_ip, prefix in src_info.get('ips', {}).items():
            if icmp_engine._ip_in_network(target_ip, my_ip, prefix):
                # 同一セグメント → 相手装置を探す
                target_dev = icmp_engine._find_device_owning_ip(target_ip)
                if target_dev:
                    # 相手のMACを取得（相手のARPエンジン視点のMAC）
                    mac = self._gen_mac(target_dev, target_ip)
                else:
                    # IPは同セグメントだが装置不明 → 仮想ホストとしてMAC生成
                    mac = self._gen_mac('host', target_ip)
                # ARPテーブルに記録
                iface = self._iface_for_ip(device_id, my_ip)
                self.tables[device_id][target_ip] = ArpEntry(
                    ip=target_ip, mac=mac, iface=iface, entry_type='dynamic')
                return mac
        # 同一セグメントにいない → ARP解決不可（L3で別途ルーティング）
        return None

    def _iface_for_ip(self, device_id: str, ip: str) -> str:
        info = icmp_engine.device_ips.get(device_id, {})
        ifaces = info.get('interfaces', {})
        for ifname, iinfo in ifaces.items():
            if iinfo.get('ip') == ip:
                return ifname
        return 'lan0'

    def add_static(self, device_id: str, ip: str, mac: str, iface: str = 'lan0'):
        self.tables[device_id][ip] = ArpEntry(ip=ip, mac=mac, iface=iface,
                                              entry_type='static')

    def clear(self, device_id: str):
        self.tables[device_id] = {}

    def get_own_mac(self, device_id: str, ip: str) -> str:
        return self._gen_mac(device_id, ip)

    def format_show_arp(self, device_id: str, device_type: str = 'cisco') -> str:
        """show arp / show ip arp の出力"""
        table = self.tables.get(device_id, {})
        # 自分のインタフェースも表示に含める
        own = icmp_engine.device_ips.get(device_id, {})

        if device_type in ('catalyst', 'cisco'):
            lines = ['Protocol  Address          Age (min)  Hardware Addr   Type   Interface']
            # 自分のIP
            for ip, prefix in own.get('ips', {}).items():
                mac = self._gen_mac(device_id, ip)
                iface = self._iface_for_ip(device_id, ip)
                lines.append(f'Internet  {ip:<16} {"-":<10} {mac}  ARPA   {iface}')
            # 学習したエントリ
            for ip, e in table.items():
                age = int((time.time() - e.age) / 60) if e.entry_type == 'dynamic' else '-'
                lines.append(f'Internet  {ip:<16} {str(age):<10} {e.mac}  ARPA   {e.iface}')
            return '\n'.join(lines)
        else:
            # Si-R形式
            lines = ['IP Address        MAC Address        Interface    Type']
            for ip, prefix in own.get('ips', {}).items():
                mac = self._gen_mac(device_id, ip)
                iface = self._iface_for_ip(device_id, ip)
                lines.append(f'{ip:<18}{mac:<19}{iface:<13}self')
            for ip, e in table.items():
                lines.append(f'{ip:<18}{e.mac:<19}{e.iface:<13}{e.entry_type}')
            return '\n'.join(lines)


arp_engine = ArpEngine()


# ══════════════════════════════════════════
# IPフィルタ / ACL エンジン（パケットフィルタ）
# ══════════════════════════════════════════
@dataclass
class AclRule:
    seq: int
    action: str          # permit | deny
    protocol: str        # ip | tcp | udp | icmp
    src: str             # 'any' | 'host X' | 'X/prefix'
    dst: str
    src_port: Optional[str] = None
    dst_port: Optional[str] = None

class IpFilterEngine:
    """
    パケットフィルタ（ACL）を実装。
    経路フィルタ（prefix-list）と異なり、実際に流れるパケットの通過可否を制御。
    Si-R形式（acl）とCisco形式（access-list）両対応。
    """
    def __init__(self):
        # device_id -> {acl_name/number -> [AclRule]}
        self.acls: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        # device_id -> {(iface, direction) -> acl_name}
        self.applied: Dict[str, Dict[tuple, str]] = defaultdict(dict)

    def add_rule(self, device_id: str, acl_name: str, action: str,
                 protocol: str = 'ip', src: str = 'any', dst: str = 'any',
                 src_port: str = None, dst_port: str = None, seq: int = None):
        rules = self.acls[device_id][acl_name]
        if seq is None:
            seq = (max([r.seq for r in rules], default=0) + 10)
        rules.append(AclRule(seq=seq, action=action, protocol=protocol,
                             src=src, dst=dst, src_port=src_port, dst_port=dst_port))
        rules.sort(key=lambda r: r.seq)

    def apply_acl(self, device_id: str, iface: str, direction: str, acl_name: str):
        self.applied[device_id][(iface, direction)] = acl_name

    def clear_acl(self, device_id: str, acl_name: str):
        if acl_name in self.acls[device_id]:
            del self.acls[device_id][acl_name]

    @staticmethod
    def _ip_to_int(ip: str) -> int:
        o = [int(x) for x in ip.split('.')]
        return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]

    def _match_addr(self, spec: str, ip: str) -> bool:
        """アドレス指定にIPがマッチするか"""
        spec = spec.strip()
        if spec == 'any':
            return True
        if spec.startswith('host '):
            return spec.split()[1] == ip
        if '/' in spec:
            net, prefix = spec.split('/')
            prefix = int(prefix)
            try:
                mask = (0xffffffff << (32 - prefix)) & 0xffffffff
                return (self._ip_to_int(ip) & mask) == (self._ip_to_int(net) & mask)
            except Exception:
                return False
        return spec == ip

    def check_packet(self, device_id: str, iface: str, direction: str,
                     src_ip: str, dst_ip: str, protocol: str = 'ip',
                     dst_port: str = None) -> bool:
        """
        パケットが通過できるか判定。
        適用ACLがなければ通過（許可）。あればルール評価し、暗黙deny。
        """
        acl_name = self.applied.get(device_id, {}).get((iface, direction))
        if not acl_name:
            return True  # ACL未適用 = 通過
        rules = self.acls.get(device_id, {}).get(acl_name, [])
        if not rules:
            return True
        for rule in rules:
            # プロトコル一致（ipは全プロトコルにマッチ）
            if rule.protocol != 'ip' and rule.protocol != protocol:
                continue
            if not self._match_addr(rule.src, src_ip):
                continue
            if not self._match_addr(rule.dst, dst_ip):
                continue
            # ポート判定（指定があれば）
            if rule.dst_port and dst_port and rule.dst_port != dst_port:
                continue
            # マッチ → このルールのaction
            return rule.action == 'permit'
        # どのルールにもマッチしない → 暗黙deny
        return False

    def format_show_acl(self, device_id: str, device_type: str = 'cisco') -> str:
        acls = self.acls.get(device_id, {})
        if not acls:
            return '(No access lists configured)'
        lines = []
        for name, rules in acls.items():
            if device_type in ('catalyst', 'cisco'):
                # 数字なら standard/extended 判定
                kind = 'Standard' if name.isdigit() and int(name) < 100 else 'Extended'
                lines.append(f'{kind} IP access list {name}')
                for r in rules:
                    if r.protocol == 'ip' and r.dst == 'any' and not r.dst_port:
                        lines.append(f'    {r.seq} {r.action} {r.src}')
                    else:
                        portstr = f' eq {r.dst_port}' if r.dst_port else ''
                        lines.append(f'    {r.seq} {r.action} {r.protocol} '
                                     f'{r.src} {r.dst}{portstr}')
            else:
                # Si-R形式
                lines.append(f'acl {name}:')
                for r in rules:
                    portstr = f' port {r.dst_port}' if r.dst_port else ''
                    lines.append(f'   {r.seq} {r.action} {r.protocol} '
                                 f'src {r.src} dst {r.dst}{portstr}')
        # 適用状況
        applied = self.applied.get(device_id, {})
        if applied:
            lines.append('')
            lines.append('Applied:')
            for (iface, direction), aname in applied.items():
                lines.append(f'   {iface} {direction}: {aname}')
        return '\n'.join(lines)


ipfilter_engine = IpFilterEngine()


# ══════════════════════════════════════════
# pyATS / Genie 風エンジン（learn / snapshot / diff）
# ══════════════════════════════════════════
class GenieEngine:
    """
    Cisco pyATS/Genie を模した検証自動化エンジン。
    - learn(feature): プロトコルの状態を構造化データ(dict)として採取
    - snapshot: 複数featureをまとめて採取し名前付き保存
    - diff: 2つのsnapshotを比較し差分をレポート
    実機pyATSの「show出力をparseしてJSON化、before/after比較」を再現する。
    """
    def __init__(self):
        # device_id -> {snapshot_name -> {feature -> structured_data}}
        self.snapshots: Dict[str, Dict[str, dict]] = defaultdict(dict)

    # ── learn: 各featureを構造化データとして採取 ──
    def learn(self, device_id: str, feature: str) -> dict:
        feature = feature.lower()
        if feature == 'ospf':
            return self._learn_ospf(device_id)
        elif feature == 'bgp':
            return self._learn_bgp(device_id)
        elif feature == 'rip':
            return self._learn_rip(device_id)
        elif feature == 'interface':
            return self._learn_interface(device_id)
        elif feature == 'routing':
            return self._learn_routing(device_id)
        elif feature == 'stp':
            return self._learn_stp(device_id)
        elif feature == 'arp':
            return self._learn_arp(device_id)
        elif feature in ('config', 'vrf', 'vlan'):
            return {'feature': feature, 'note': 'minimal'}
        return {}

    def _learn_ospf(self, device_id: str) -> dict:
        n = ospf_engine.nodes.get(device_id)
        if not n or not n.get('enabled'):
            return {}
        neighbors = {}
        for nid, nbr in n['neighbors'].items():
            neighbors[nbr.router_id] = {
                'neighbor_router_id': nbr.router_id,
                'state': nbr.state,
                'hostname': nbr.hostname,
            }
        return {
            'router_id': n['router_id'],
            'process_id': n['process_id'],
            'area': n['area_id'],
            'neighbors': neighbors,
            'routes': [{'network': r['network'], 'prefix': r['prefix'],
                        'metric': r['metric']} for r in n.get('routes', [])],
        }

    def _learn_bgp(self, device_id: str) -> dict:
        n = bgp_engine.nodes.get(device_id)
        if not n or not n.get('enabled'):
            return {}
        sessions = {}
        for nid, s in n['sessions'].items():
            sessions[nid] = {
                'remote_as': s.remote_as,
                'state': s.state,
                'hostname': s.hostname,
            }
        return {
            'local_as': n['local_as'],
            'router_id': n['router_id'],
            'sessions': sessions,
            'prefixes': [f"{r['prefix']}/{r['prefix_len']}"
                         for r in n.get('loc_rib', [])],
        }

    def _learn_rip(self, device_id: str) -> dict:
        n = rip_engine.nodes.get(device_id)
        if not n or not n.get('enabled'):
            return {}
        routes = {}
        for r in n['table']:
            routes[f'{r.network}/{r.prefix}'] = {
                'metric': r.metric,
                'next_hop': r.next_hop,
                'source': 'connected' if r.learned_from == 'direct' else 'rip',
            }
        return {'version': n['version'], 'routes': routes}

    def _learn_interface(self, device_id: str) -> dict:
        info = icmp_engine.device_ips.get(device_id, {})
        interfaces = {}
        for ifname, iinfo in info.get('interfaces', {}).items():
            interfaces[ifname] = {
                'ip_address': iinfo.get('ip', ''),
                'prefix': iinfo.get('prefix', 24),
                'enabled': True,
                'oper_status': 'up',
            }
        return {'interfaces': interfaces}

    def _learn_routing(self, device_id: str) -> dict:
        routes = rib_engine.get_best_routes(device_id)
        table = {}
        for r in routes:
            table[f"{r['network']}/{r['prefix']}"] = {
                'source': r['source'],
                'ad': r['ad'],
                'metric': r['metric'],
                'next_hop': r['next_hop'],
            }
        return {'routes': table}

    def _learn_stp(self, device_id: str) -> dict:
        n = stp_engine.nodes.get(device_id)
        if not n or not n.get('enabled'):
            return {}
        ports = {}
        for pname, p in n['ports'].items():
            ports[pname] = {'role': p['role'], 'state': p['state']}
        return {
            'bridge_id': n['bridge_id'],
            'root_bridge_id': n['root_bridge_id'],
            'is_root': n['root_bridge_id'] == n['bridge_id'],
            'ports': ports,
        }

    def _learn_arp(self, device_id: str) -> dict:
        table = arp_engine.tables.get(device_id, {})
        entries = {}
        for ip, e in table.items():
            entries[ip] = {'mac': e.mac, 'type': e.entry_type, 'iface': e.iface}
        return {'entries': entries}

    # ── snapshot: 複数featureをまとめて採取 ──
    def take_snapshot(self, device_id: str, snapshot_name: str,
                      features: List[str]) -> dict:
        data = {}
        for f in features:
            learned = self.learn(device_id, f)
            if learned:
                data[f] = learned
        self.snapshots[device_id][snapshot_name] = data
        return data

    # ── diff: 2つのsnapshotを比較 ──
    def diff(self, device_id: str, before: str, after: str) -> dict:
        snaps = self.snapshots.get(device_id, {})
        b = snaps.get(before)
        a = snaps.get(after)
        if b is None or a is None:
            return {'error': f'snapshot not found (before={before}, after={after})'}
        diffs = {}
        all_features = set(b.keys()) | set(a.keys())
        for feature in all_features:
            fb = b.get(feature, {})
            fa = a.get(feature, {})
            fdiff = self._diff_dict(fb, fa)
            if fdiff:
                diffs[feature] = fdiff
        return {'identical': len(diffs) == 0, 'diffs': diffs}

    def _diff_dict(self, before: dict, after: dict, path: str = '') -> list:
        """再帰的にdictを比較して差分リストを返す"""
        changes = []
        all_keys = set(before.keys()) | set(after.keys())
        for k in sorted(all_keys, key=str):
            bv = before.get(k, '<MISSING>')
            av = after.get(k, '<MISSING>')
            cur_path = f'{path}.{k}' if path else str(k)
            if isinstance(bv, dict) and isinstance(av, dict):
                changes.extend(self._diff_dict(bv, av, cur_path))
            elif bv != av:
                changes.append({'path': cur_path, 'before': bv, 'after': av})
        return changes

    def format_learn(self, device_id: str, feature: str) -> str:
        """learn結果を見やすく整形（JSON風）"""
        import json
        data = self.learn(device_id, feature)
        if not data:
            return f'% {feature} is not running on this device'
        header = f'===== Genie learn("{feature}") on {device_id} =====\n'
        return header + json.dumps(data, indent=2, ensure_ascii=False)

    def format_diff(self, device_id: str, before: str, after: str) -> str:
        """diff結果をpyATS風レポートに整形"""
        result = self.diff(device_id, before, after)
        if 'error' in result:
            return f'% {result["error"]}'
        if result['identical']:
            return (f'===== Snapshot Comparison: "{before}" vs "{after}" =====\n'
                    f'  Result: PASS (no differences)\n'
                    f'  状態に差分はありません。')
        lines = [f'===== Snapshot Comparison: "{before}" vs "{after}" =====',
                 f'  Result: FAIL (differences detected)', '']
        for feature, changes in result['diffs'].items():
            lines.append(f"feature '{feature}' に差分:")
            for c in changes:
                lines.append(f'  {c["path"]}:')
                lines.append(f'    - before: {c["before"]}')
                lines.append(f'    + after:  {c["after"]}')
            lines.append('')
        return '\n'.join(lines)

    def list_snapshots(self, device_id: str) -> str:
        snaps = self.snapshots.get(device_id, {})
        if not snaps:
            return '(No snapshots saved)'
        lines = ['Saved snapshots:']
        for name, data in snaps.items():
            features = ', '.join(data.keys())
            lines.append(f'  {name}: [{features}]')
        return '\n'.join(lines)


genie_engine = GenieEngine()


# ══════════════════════════════════════════
# LACP / EtherChannel エンジン（リンクアグリゲーション）
# ══════════════════════════════════════════
@dataclass
class EtherChannelMember:
    interface: str
    mode: str             # active | passive | on
    state: str = 'down'   # down | bundled | independent

class LacpEngine:
    """
    EtherChannel（リンクアグリゲーション）をLACPネゴシエーション込みで実装。
    成立条件（実機Cisco準拠）:
      active  + active  → 成立（both initiate）
      active  + passive → 成立（active initiates, passive responds）
      passive + passive → 不成立（誰も開始しない）
      on      + on      → 成立（静的、ネゴなし）
      on      + active/passive → 不成立（モード不一致）
    """
    def __init__(self):
        # device_id -> {po_number -> {'mode':.., 'members':[EtherChannelMember], 'protocol':'lacp'/'static'}}
        self.channels: Dict[str, Dict[int, dict]] = defaultdict(dict)

    def add_member(self, device_id: str, po_number: int, interface: str, mode: str):
        """物理ポートをchannel-groupに追加"""
        ch = self.channels[device_id].get(po_number)
        protocol = 'static' if mode == 'on' else 'lacp'
        if not ch:
            ch = {'mode': mode, 'protocol': protocol, 'members': []}
            self.channels[device_id][po_number] = ch
        # 既存メンバー更新 or 追加
        existing = next((m for m in ch['members'] if m.interface == interface), None)
        if existing:
            existing.mode = mode
        else:
            ch['members'].append(EtherChannelMember(interface=interface, mode=mode))

    def _modes_compatible(self, mode_a: str, mode_b: str) -> bool:
        """2端のモードでLACPが成立するか"""
        if mode_a == 'on' and mode_b == 'on':
            return True
        if mode_a == 'on' or mode_b == 'on':
            return False  # onとactive/passiveは不一致
        # active/passiveの組み合わせ
        if mode_a == 'passive' and mode_b == 'passive':
            return False  # 両passiveは開始者なし
        return True  # active+active, active+passive

    def negotiate(self, device_id: str, po_number: int,
                  peer_device_id: str, peer_po_number: int) -> bool:
        """
        対向装置とLACPネゴシエーション。成立すればメンバーをbundled、
        不成立ならindependentにする。
        """
        ch = self.channels.get(device_id, {}).get(po_number)
        peer_ch = self.channels.get(peer_device_id, {}).get(peer_po_number)
        if not ch or not peer_ch:
            return False
        ok = self._modes_compatible(ch['mode'], peer_ch['mode'])
        new_state = 'bundled' if ok else 'independent'
        for m in ch['members']:
            m.state = new_state
        return ok

    def get_channel(self, device_id: str, po_number: int):
        return self.channels.get(device_id, {}).get(po_number)

    def format_etherchannel_summary(self, device_id: str) -> str:
        """show etherchannel summary の出力（Cisco/Catalyst用）"""
        chans = self.channels.get(device_id, {})
        if not chans:
            return ('Flags:  D - down        P - bundled in port-channel\n'
                    '        I - stand-alone s - suspended\n'
                    '\n'
                    'Number of channel-groups in use: 0\n'
                    'Number of aggregators:           0')
        lines = [
            'Flags:  D - down        P - bundled in port-channel',
            '        I - stand-alone s - suspended',
            '        R - Layer3      S - Layer2',
            '        U - in use      f - failed to allocate aggregator',
            '',
            f'Number of channel-groups in use: {len(chans)}',
            f'Number of aggregators:           {len(chans)}',
            '',
            'Group  Port-channel  Protocol    Ports',
            '------+-------------+-----------+-----------------------------------',
        ]
        for po_num, ch in sorted(chans.items()):
            any_bundled = any(m.state == 'bundled' for m in ch['members'])
            po_flag = 'SU' if any_bundled else 'SD'
            proto_str = 'LACP' if ch['protocol'] == 'lacp' else '-'
            member_strs = []
            for m in ch['members']:
                flag = 'P' if m.state == 'bundled' else ('I' if m.state == 'independent' else 'D')
                short = m.interface.replace('GigabitEthernet', 'Gi')
                member_strs.append(f'{short}({flag})')
            lines.append(f'{po_num:<7}Po{po_num}({po_flag}){"":<6}{proto_str:<12}'
                         + ' '.join(member_strs))
        return '\n'.join(lines)

    def format_show_linkaggregation(self, device_id: str, group: int = None) -> str:
        """show linkaggregation [group] — SR-S形式"""
        chans = self.channels.get(device_id, {})
        if not chans:
            return ('Link Aggregation Information\n'
                    '  No link aggregation groups configured.\n'
                    '  設定例: ether 1-2 type linkaggregation 1 1\n'
                    '          linkaggregation 1 mode active')
        lines = ['Link Aggregation Information', '']
        target = {group: chans[group]} if group and group in chans else chans
        for po_num, ch in sorted(target.items()):
            any_bundled = any(m.state == 'bundled' for m in ch['members'])
            status = 'Up' if any_bundled else 'Down'
            mode = ch['mode']
            proto = 'LACP' if ch['protocol'] == 'lacp' else 'Static'
            lines.append(f'  Group {po_num}:')
            lines.append(f'    Status   : {status}')
            lines.append(f'    Mode     : {mode} ({proto})')
            lines.append(f'    Members  :')
            for m in ch['members']:
                state_str = 'Bundled' if m.state == 'bundled' else 'Independent'
                # SR-SのポートはETHER形式で表示
                ether_name = m.interface.replace('GigabitEthernet1/0/', 'ETHER')
                lines.append(f'      {ether_name:<10} {state_str}')
            lines.append(f'    LACP PDU sent/rcvd: 42/38')
            lines.append('')
        return '\n'.join(lines)

    def get_running_config_lines(self, device_id: str, device_type: str) -> list:
        """show run用のLACP設定行を返す（機種別形式）"""
        chans = self.channels.get(device_id, {})
        lines = []
        if not chans:
            return lines
        if device_type in ('catalyst', 'cisco'):
            # Cisco形式
            for po_num, ch in sorted(chans.items()):
                lines.append('!')
                lines.append(f'interface Port-channel{po_num}')
                lines.append(f' switchport mode trunk')
                lines.append('!')
                for m in ch['members']:
                    lines.append(f'interface {m.interface}')
                    lines.append(f' channel-group {po_num} mode {m.mode}')
                    lines.append('!')
        elif device_type == 'srs':
            # SR-S形式
            for po_num, ch in sorted(chans.items()):
                anchor = ch['members'][0].interface.replace('GigabitEthernet1/0/', '') if ch['members'] else '1'
                ports = [m.interface.replace('GigabitEthernet1/0/', '') for m in ch['members']]
                if len(ports) >= 2:
                    lines.append(f'ether {ports[0]}-{ports[-1]} type linkaggregation {po_num} {anchor}')
                lines.append(f'linkaggregation {po_num} mode {ch["mode"]}')
                lines.append('!')
        return lines

    def get_show_interfaces_lines(self, device_id: str, device_type: str) -> dict:
        """show interfaces用のPort-channel情報を返す"""
        chans = self.channels.get(device_id, {})
        result = {}
        for po_num, ch in chans.items():
            any_bundled = any(m.state == 'bundled' for m in ch['members'])
            status = 'connected' if any_bundled else 'notconnect'
            members = [m.interface for m in ch['members']]
            if device_type in ('catalyst', 'cisco'):
                result[f'Port-channel{po_num}'] = {
                    'status': status, 'members': members,
                    'mode': ch['mode'], 'protocol': ch['protocol'],
                }
            elif device_type == 'srs':
                result[f'linkagg{po_num}'] = {
                    'status': status, 'members': members,
                    'mode': ch['mode'], 'protocol': ch['protocol'],
                }
        return result


lacp_engine = LacpEngine()

# グローバルインスタンス
# ══════════════════════════════════════════
rip_engine  = RipEngine()
ospf_engine = OspfEngine()
bgp_engine  = BgpEngine()
stp_engine  = StpEngine()


# ══════════════════════════════════════════
# 再配信（redistribute）コーディネーター
# ══════════════════════════════════════════
async def redistribute(device_id: str, target_proto: str, source_proto: str,
                       metric: int = None):
    """
    source_proto で学習/設定したルートを target_proto に再配信する。
    target_proto: 'rip' | 'ospf' | 'bgp'  (再配信先)
    source_proto: 'rip' | 'ospf' | 'bgp' | 'static' | 'connected'  (再配信元)
    """
    # 再配信元のルートを集める
    src_routes = []  # [(net/prefix, metric)]

    if source_proto == 'connected':
        # 直接接続（各プロトコルのnetworksから）
        for eng in (rip_engine, ospf_engine):
            node = eng.nodes.get(device_id)
            if node:
                for net in node.get('networks', []):
                    src_routes.append((net, 1))
        # ICMPエンジンの直結ネットワークも
        dev = icmp_engine.device_ips.get(device_id, {})
        for ip, prefix in dev.get('ips', {}).items():
            net = _net_addr(ip, prefix)
            src_routes.append((net, 1))

    elif source_proto == 'static':
        node = rib_engine.nodes.get(device_id)
        if node:
            for r in node['static_routes']:
                if r.active:
                    src_routes.append((f'{r.network}/{r.prefix}', 1))

    elif source_proto == 'rip':
        node = rip_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for r in node['table']:
                if r.learned_from != 'direct':
                    src_routes.append((f'{r.network}/{r.prefix}', r.metric))

    elif source_proto == 'ospf':
        node = ospf_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for r in node.get('routes', []):
                if r.get('via') != 'direct':
                    src_routes.append((f"{r['network']}/{r['prefix']}", r['metric']))

    elif source_proto == 'bgp':
        node = bgp_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for r in node.get('loc_rib', []):
                src_routes.append((f"{r['prefix']}/{r['prefix_len']}", 1))

    if not src_routes:
        return 0

    # 再配信フィルタ（route-map/prefix-list）を適用
    filtered = []
    for net, m in src_routes:
        parts = net.split('/')
        if len(parts) == 2:
            if filter_engine.filter_redist(device_id, target_proto, source_proto,
                                            parts[0], int(parts[1])):
                filtered.append((net, m))
    src_routes = filtered
    if not src_routes:
        return 0

    # 再配信先に注入
    count = 0
    if target_proto == 'rip':
        node = rip_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for net, m in src_routes:
                node['redistributed'][net] = {
                    'metric': metric if metric is not None else 1,
                    'source': source_proto,
                }
                count += 1
            await rip_engine._send_update(device_id)

    elif target_proto == 'ospf':
        node = ospf_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for net, m in src_routes:
                node['redistributed'][net] = {
                    'metric': metric if metric is not None else 20,
                    'source': source_proto,
                }
                count += 1
            await ospf_engine._send_hello(device_id)

    elif target_proto == 'bgp':
        node = bgp_engine.nodes.get(device_id)
        if node and node.get('enabled'):
            for net, m in src_routes:
                await bgp_engine.advertise_network(device_id, net)
                count += 1

    return count


def _net_addr(ip: str, prefix: int) -> str:
    """IP+prefix → ネットワークアドレス/prefix"""
    try:
        o = [int(x) for x in ip.split('.')]
        ip_int = (o[0]<<24)|(o[1]<<16)|(o[2]<<8)|o[3]
        mask = (0xffffffff << (32-prefix)) & 0xffffffff
        ni = ip_int & mask
        return f'{(ni>>24)&0xff}.{(ni>>16)&0xff}.{(ni>>8)&0xff}.{ni&0xff}/{prefix}'
    except Exception:
        return f'{ip}/{prefix}'


# ══════════════════════════════════════════
# VRRP / HSRP エンジン（ゲートウェイ冗長化）
# ══════════════════════════════════════════
@dataclass
class VrrpGroup:
    group_id: int
    vip: str                        # 仮想IPアドレス
    priority: int = 100             # デフォルト priority
    preempt: bool = True            # プリエンプト（デフォルト有効）
    state: str = 'Init'             # Init / Master / Backup
    interface: str = ''
    hello_interval: int = 1         # 高速タイマー用
    dead_interval: int = 3
    virtual_mac: str = ''
    hello_task: Optional[Any] = None
    dead_task: Optional[Any] = None
    advert_time: Optional[float] = None

@dataclass
class HsrpGroup:
    group_id: int
    vip: str
    priority: int = 100
    preempt: bool = False           # HSRPはデフォルトpreempt無効
    state: str = 'Init'             # Init / Active / Standby / Listen
    interface: str = ''
    hello_interval: int = 1
    hold_time: int = 3
    virtual_mac: str = ''
    hello_task: Optional[Any] = None
    dead_task: Optional[Any] = None


class VrrpEngine:
    """
    VRRP（RFC 5798）/ HSRP（Cisco独自）ゲートウェイ冗長化エンジン
    - VRRP: Cisco/Catalyst/Si-R/APRESIA対応
    - HSRP: Cisco/Catalyst対応
    - Master/Active選出: priority大が優先、同じならIPアドレス大が優先
    - preempt: 高priority復旧時に自動的にMaster/Activeに戻る
    - フェイルオーバー: dead_interval経過でBackup→Master昇格
    """
    def __init__(self):
        # device_id -> {group_id -> VrrpGroup}
        self.vrrp: Dict[str, Dict[int, VrrpGroup]] = defaultdict(dict)
        # device_id -> {group_id -> HsrpGroup}
        self.hsrp: Dict[str, Dict[int, HsrpGroup]] = defaultdict(dict)

    # ── VRRP ─────────────────────────────
    def _vrrp_vmac(self, vrid: int) -> str:
        """VRRP仮想MAC: 00-00-5E-00-01-{VRID}"""
        return f'00:00:5e:00:01:{vrid:02x}'

    def _hsrp_vmac(self, group: int) -> str:
        """HSRP仮想MAC: 00-00-0C-07-AC-{group}"""
        return f'00:00:0c:07:ac:{group:02x}'

    async def vrrp_start(self, device_id: str, group_id: int, vip: str,
                          priority: int = 100, preempt: bool = True,
                          interface: str = ''):
        g = VrrpGroup(
            group_id=group_id, vip=vip, priority=priority,
            preempt=preempt, state='Init', interface=interface,
            virtual_mac=self._vrrp_vmac(group_id),
        )
        self.vrrp[device_id][group_id] = g
        await vnet.send_to(device_id, {
            'type': 'vrrp_log',
            'message': vendor_log.vrrp_state(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), device_id, group_id, interface or 'GigabitEthernet0/0/0', 'Init', 'Init', vip, priority)
        })
        # Hello送信開始
        if g.hello_task:
            g.hello_task.cancel()
        g.hello_task = _spawn(self._vrrp_hello_loop(device_id, group_id))

    async def _vrrp_hello_loop(self, device_id: str, group_id: int):
        await asyncio.sleep(random.uniform(0.1, 0.5))
        while True:
            g = self.vrrp.get(device_id, {}).get(group_id)
            if not g:
                break
            await self._vrrp_send_advert(device_id, group_id)
            await asyncio.sleep(g.hello_interval)

    async def _vrrp_send_advert(self, device_id: str, group_id: int):
        g = self.vrrp.get(device_id, {}).get(group_id)
        if not g:
            return
        pkt = {
            'type': 'vrrp_advert',
            'src_id': device_id,
            'group_id': group_id,
            'vip': g.vip,
            'priority': g.priority,
            'preempt': g.preempt,
            'state': g.state,
        }
        await vnet.broadcast_to_neighbors(device_id, pkt)

    async def vrrp_receive_advert(self, receiver_id: str, msg: dict):
        group_id = msg.get('group_id')
        src_id = msg.get('src_id')
        peer_priority = msg.get('priority', 100)
        peer_state = msg.get('state', 'Init')

        g = self.vrrp.get(receiver_id, {}).get(group_id)
        if not g:
            return

        my_priority = g.priority
        prev_state = g.state

        if g.state == 'Init':
            # 初回受信→選出
            await self._vrrp_elect(receiver_id, group_id, src_id, peer_priority)

        elif g.state == 'Master':
            # 相手がより高いpriorityでAdvertを送ってきた
            if peer_priority > my_priority:
                g.state = 'Backup'
                await self._vrrp_log_change(receiver_id, group_id, 'Master', 'Backup',
                                            f'より高いpriority({peer_priority})を検出')
            elif peer_priority == my_priority:
                # IP比較（大きい方がMaster）
                my_ip = self._get_device_ip(receiver_id)
                peer_ip = self._get_device_ip(src_id)
                if peer_ip and my_ip and peer_ip > my_ip:
                    g.state = 'Backup'
                    await self._vrrp_log_change(receiver_id, group_id, 'Master', 'Backup',
                                                f'IP比較で負け({peer_ip} > {my_ip})')

        elif g.state == 'Backup':
            # Dead timerリセット
            self._reset_vrrp_dead_timer(receiver_id, group_id, src_id)
            # preemptが有効で自分のpriorityが高ければMasterに昇格
            if g.preempt and my_priority > peer_priority:
                g.state = 'Master'
                await self._vrrp_log_change(receiver_id, group_id, 'Backup', 'Master',
                                            f'preempt: priority {my_priority} > {peer_priority}')

    async def _vrrp_elect(self, device_id: str, group_id: int,
                           peer_id: str, peer_priority: int):
        g = self.vrrp[device_id][group_id]
        peer_g = self.vrrp.get(peer_id, {}).get(group_id)

        if g.priority > peer_priority:
            g.state = 'Master'
            if peer_g:
                peer_g.state = 'Backup'
        elif g.priority < peer_priority:
            g.state = 'Backup'
            if peer_g:
                peer_g.state = 'Master'
        else:
            # priority同じ → IP比較
            my_ip = self._get_device_ip(device_id)
            peer_ip = self._get_device_ip(peer_id)
            if my_ip and peer_ip:
                if my_ip > peer_ip:
                    g.state = 'Master'
                    if peer_g: peer_g.state = 'Backup'
                else:
                    g.state = 'Backup'
                    if peer_g: peer_g.state = 'Master'
            else:
                g.state = 'Master'

        await self._vrrp_log_change(device_id, group_id, 'Init', g.state,
                                    f'選出完了 priority={g.priority}')
        self._reset_vrrp_dead_timer(device_id, group_id, peer_id)

    def _reset_vrrp_dead_timer(self, device_id: str, group_id: int, peer_id: str):
        g = self.vrrp.get(device_id, {}).get(group_id)
        if not g:
            return
        if g.dead_task:
            g.dead_task.cancel()
        async def dead():
            await asyncio.sleep(g.dead_interval)
            gd = self.vrrp.get(device_id, {}).get(group_id)
            if gd and gd.state == 'Backup':
                gd.state = 'Master'
                gd.advert_time = time.time()
                await self._vrrp_log_change(device_id, group_id, 'Backup', 'Master',
                                            'Dead timer expired — Masterに昇格')
        g.dead_task = _spawn(dead())

    def _get_device_ip(self, device_id: str) -> Optional[str]:
        info = icmp_engine.device_ips.get(device_id, {})
        ips = list(info.get('ips', {}).keys())
        return sorted(ips)[-1] if ips else None

    async def _vrrp_log_change(self, device_id: str, group_id: int,
                                from_state: str, to_state: str, reason: str):
        g = self.vrrp.get(device_id, {}).get(group_id)
        if not g:
            return
        g.advert_time = time.time()
        await vnet.send_to(device_id, {
            'type': 'vrrp_log',
            'message': vendor_log.vrrp_state(
                        vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'),
                        device_id, group_id,
                        g.interface or 'GigabitEthernet0/0/0',
                        from_state, to_state, g.vip, g.priority
                    )
        })

    async def vrrp_failover(self, device_id: str, group_id: int):
        """Master障害をシミュレート → Backupが即昇格"""
        g = self.vrrp.get(device_id, {}).get(group_id)
        if not g or g.state != 'Master':
            return
        prev = g.state
        g.state = 'Init'
        if g.hello_task:
            g.hello_task.cancel()
        await vnet.send_to(device_id, {
            'type': 'vrrp_log',
            'message': vendor_log.ios('VRRP', 6, 'STATECHANGE',
                f'Group {group_id} state Master -> Init (fault simulation, priority={g.priority})')
        })
        # 隣接のBackupに通知
        for nbr_id in vnet.get_neighbors(device_id):
            nbr_g = self.vrrp.get(nbr_id, {}).get(group_id)
            if nbr_g and nbr_g.state == 'Backup':
                if nbr_g.dead_task:
                    nbr_g.dead_task.cancel()
                nbr_g.state = 'Master'
                await self._vrrp_log_change(nbr_id, group_id, 'Backup', 'Master',
                                            '対向Master障害検出 → 即昇格')

    # ── HSRP ─────────────────────────────
    async def hsrp_start(self, device_id: str, group_id: int, vip: str,
                          priority: int = 100, preempt: bool = False,
                          interface: str = ''):
        g = HsrpGroup(
            group_id=group_id, vip=vip, priority=priority,
            preempt=preempt, state='Init', interface=interface,
            virtual_mac=self._hsrp_vmac(group_id),
        )
        self.hsrp[device_id][group_id] = g
        await vnet.send_to(device_id, {
            'type': 'hsrp_log',
            'message': vendor_log.hsrp_state(vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'), device_id, group_id, interface or 'GigabitEthernet0/0/0', 'Init', 'Init')
        })
        if g.hello_task:
            g.hello_task.cancel()
        g.hello_task = _spawn(self._hsrp_hello_loop(device_id, group_id))

    async def _hsrp_hello_loop(self, device_id: str, group_id: int):
        await asyncio.sleep(random.uniform(0.1, 0.5))
        while True:
            g = self.hsrp.get(device_id, {}).get(group_id)
            if not g:
                break
            await self._hsrp_send_hello(device_id, group_id)
            await asyncio.sleep(g.hello_interval)

    async def _hsrp_send_hello(self, device_id: str, group_id: int):
        g = self.hsrp.get(device_id, {}).get(group_id)
        if not g:
            return
        pkt = {
            'type': 'hsrp_hello',
            'src_id': device_id,
            'group_id': group_id,
            'vip': g.vip,
            'priority': g.priority,
            'preempt': g.preempt,
            'state': g.state,
        }
        await vnet.broadcast_to_neighbors(device_id, pkt)

    async def hsrp_receive_hello(self, receiver_id: str, msg: dict):
        group_id = msg.get('group_id')
        src_id = msg.get('src_id')
        peer_priority = msg.get('priority', 100)

        g = self.hsrp.get(receiver_id, {}).get(group_id)
        if not g:
            return

        if g.state == 'Init':
            await self._hsrp_elect(receiver_id, group_id, src_id, peer_priority)
        elif g.state == 'Active':
            if peer_priority > g.priority or (peer_priority == g.priority and g.preempt):
                if peer_priority > g.priority:
                    g.state = 'Standby'
                    await self._hsrp_log(receiver_id, group_id, 'Active', 'Standby',
                                         f'より高いpriority({peer_priority})')
        elif g.state == 'Standby':
            self._reset_hsrp_dead_timer(receiver_id, group_id, src_id)
            if g.preempt and g.priority > peer_priority:
                g.state = 'Active'
                await self._hsrp_log(receiver_id, group_id, 'Standby', 'Active',
                                     f'preempt発動 priority={g.priority}>{peer_priority}')

    async def _hsrp_elect(self, device_id: str, group_id: int,
                           peer_id: str, peer_priority: int):
        g = self.hsrp[device_id][group_id]
        peer_g = self.hsrp.get(peer_id, {}).get(group_id)
        if g.priority >= peer_priority:
            g.state = 'Active'
            if peer_g: peer_g.state = 'Standby'
        else:
            g.state = 'Standby'
            if peer_g: peer_g.state = 'Active'
        await self._hsrp_log(device_id, group_id, 'Init', g.state,
                              f'選出完了 priority={g.priority}')
        self._reset_hsrp_dead_timer(device_id, group_id, peer_id)

    def _reset_hsrp_dead_timer(self, device_id: str, group_id: int, peer_id: str):
        g = self.hsrp.get(device_id, {}).get(group_id)
        if not g:
            return
        if g.dead_task:
            g.dead_task.cancel()
        async def dead():
            await asyncio.sleep(g.hold_time)
            gd = self.hsrp.get(device_id, {}).get(group_id)
            if gd and gd.state == 'Standby':
                gd.state = 'Active'
                await self._hsrp_log(device_id, group_id, 'Standby', 'Active',
                                     'Hold timer expired — Activeに昇格')
        g.dead_task = _spawn(dead())

    async def _hsrp_log(self, device_id: str, group_id: int,
                         from_s: str, to_s: str, reason: str):
        g = self.hsrp.get(device_id, {}).get(group_id)
        if not g:
            return
        await vnet.send_to(device_id, {
            'type': 'hsrp_log',
            'message': vendor_log.hsrp_state(
                        vnet.ws_send_callbacks.get(f'_type_{device_id}','cisco'),
                        device_id, group_id,
                        g.interface or 'GigabitEthernet0/0/0',
                        from_s, to_s
                    )
        })

    async def hsrp_failover(self, device_id: str, group_id: int):
        """Active障害をシミュレート → Standbyが即昇格"""
        g = self.hsrp.get(device_id, {}).get(group_id)
        if not g or g.state != 'Active':
            return
        g.state = 'Init'
        if g.hello_task:
            g.hello_task.cancel()
        await vnet.send_to(device_id, {
            'type': 'hsrp_log',
            'message': vendor_log.ios('HSRP', 5, 'STATECHANGE', f'Grp {group_id} state Active -> Init (fault simulation)')
        })
        for nbr_id in vnet.get_neighbors(device_id):
            nbr_g = self.hsrp.get(nbr_id, {}).get(group_id)
            if nbr_g and nbr_g.state == 'Standby':
                if nbr_g.dead_task:
                    nbr_g.dead_task.cancel()
                nbr_g.state = 'Active'
                await self._hsrp_log(nbr_id, group_id, 'Standby', 'Active',
                                     '対向Active障害 → 即昇格')

    # ── show コマンド ─────────────────────
    def format_show_vrrp(self, device_id: str) -> str:
        groups = self.vrrp.get(device_id, {})
        if not groups:
            return '% VRRP is not configured on this device.'
        lines = []
        for gid, g in sorted(groups.items()):
            elapsed = int(time.time() - g.advert_time) if g.advert_time else 0
            lines.append(f'GigabitEthernet0/0/0 - Group {gid}')
            lines.append(f'  State is {g.state}')
            lines.append(f'  Virtual IP address is {g.vip}')
            lines.append(f'  Virtual MAC address is {g.virtual_mac}')
            lines.append(f'  Advertisement interval is {g.hello_interval} sec')
            lines.append(f'  Preemption {"enabled" if g.preempt else "disabled"}')
            lines.append(f'  Priority is {g.priority}')
            if g.state == 'Master':
                lines.append(f'  Master Router is {device_id} (local), priority is {g.priority}')
                lines.append(f'  Master Advertisement interval is {g.hello_interval} sec')
                lines.append(f'  Master Down interval is {g.dead_interval} sec')
            else:
                lines.append(f'  Master Router is (unknown)')
            lines.append('')
        return '\n'.join(lines)

    def format_show_vrrp_brief(self, device_id: str) -> str:
        groups = self.vrrp.get(device_id, {})
        if not groups:
            return '% VRRP is not configured.'
        lines = ['Interface          Grp  Pri  Timer Own Pre State   Master addr     Group addr']
        for gid, g in sorted(groups.items()):
            iface = 'Gi0/0/0'
            pre = 'Y' if g.preempt else 'N'
            lines.append(
                f'{iface:<19}{gid:<5}{g.priority:<5}{g.hello_interval:<6}'
                f'{"N":<4}{pre:<4}{g.state:<8}'
                f'{"local" if g.state=="Master" else "unknown":<16}{g.vip}'
            )
        return '\n'.join(lines)

    def format_show_standby(self, device_id: str) -> str:
        """show standby (HSRP)"""
        groups = self.hsrp.get(device_id, {})
        if not groups:
            return '% HSRP is not configured on this device.'
        lines = []
        for gid, g in sorted(groups.items()):
            iface = g.interface or 'GigabitEthernet0/0/0'
            lines.append(f'{iface} - Group {gid}')
            lines.append(f'  State is {g.state}')
            lines.append(f'  Virtual IP address is {g.vip}')
            lines.append(f'  Active virtual MAC address is {g.virtual_mac}')
            lines.append(f'    Local virtual MAC address is {g.virtual_mac} (v1 default)')
            lines.append(f'  Hello time {g.hello_interval} sec, hold time {g.hold_time} sec')
            lines.append(f'  Preemption {"enabled" if g.preempt else "disabled"}')
            lines.append(f'  Priority {g.priority} (configured {g.priority})')
            if g.state == 'Active':
                lines.append(f'  Active router is local')
                lines.append(f'  Standby router is unknown')
            else:
                lines.append(f'  Active router is unknown')
                lines.append(f'  Standby router is local')
            lines.append('')
        return '\n'.join(lines)

    def format_show_standby_brief(self, device_id: str) -> str:
        groups = self.hsrp.get(device_id, {})
        if not groups:
            return '% HSRP is not configured.'
        lines = ['P indicates configured to preempt.',
                 '|',
                 'Interface   Grp  Pri P State   Active          Standby         Virtual IP']
        for gid, g in sorted(groups.items()):
            iface = (g.interface or 'Gi0/0/0').replace('GigabitEthernet','Gi')
            pre = 'P' if g.preempt else ' '
            active = 'local' if g.state == 'Active' else 'unknown'
            standby = 'local' if g.state == 'Standby' else 'unknown'
            lines.append(
                f'{iface:<12}{gid:<5}{g.priority:<4}{pre} {g.state:<8}'
                f'{active:<16}{standby:<16}{g.vip}'
            )
        return '\n'.join(lines)


vrrp_engine = VrrpEngine()


# ══════════════════════════════════════════
# VLAN エンジン（IEEE 802.1Q準拠）
# ══════════════════════════════════════════
@dataclass
class VlanPort:
    """ポートのVLAN設定"""
    mode: str = 'access'          # access / trunk
    access_vlan: int = 1          # アクセスポートのPVID
    native_vlan: int = 1          # トランクのネイティブVLAN
    allowed_vlans: set = field(default_factory=lambda: set(range(1, 4095)))
    # allowed_vlans: トランクで通過を許可するVLAN ID集合


@dataclass
class VlanInfo:
    """VLANデータベースのエントリ"""
    vlan_id: int
    name: str = ''
    state: str = 'active'         # active / suspend
    ports: list = field(default_factory=list)   # メンバーポート名


class VlanEngine:
    """
    IEEE 802.1Q TagVLAN エンジン
    - アクセスポート: 単一VLANのみ所属、タグなし送受信
    - トランクポート: 複数VLAN通過、タグ付き送受信
    - 同一VLAN間のみL2通信可能（異なるVLAN間はルーターが必要）
    - ネイティブVLAN: トランクでタグなしフレームが属するVLAN
    """
    def __init__(self):
        # device_id -> {port_name -> VlanPort}
        self.ports: Dict[str, Dict[str, VlanPort]] = defaultdict(dict)
        # device_id -> {vlan_id -> VlanInfo}
        self.vlans: Dict[str, Dict[int, VlanInfo]] = defaultdict(dict)

    # ── VLAN データベース管理 ──────────────
    def create_vlan(self, device_id: str, vlan_id: int, name: str = '') -> VlanInfo:
        if vlan_id not in self.vlans[device_id]:
            self.vlans[device_id][vlan_id] = VlanInfo(
                vlan_id=vlan_id,
                name=name or f'VLAN{vlan_id:04d}',
            )
        elif name:
            self.vlans[device_id][vlan_id].name = name
        return self.vlans[device_id][vlan_id]

    def delete_vlan(self, device_id: str, vlan_id: int):
        self.vlans[device_id].pop(vlan_id, None)
        # このVLANのポートをデフォルト(VLAN1)に戻す
        for port in self.ports[device_id].values():
            if port.access_vlan == vlan_id:
                port.access_vlan = 1

    # ── ポート設定 ──────────────────────
    def set_access(self, device_id: str, port_name: str, vlan_id: int = 1):
        """switchport mode access + switchport access vlan <id>"""
        p = self._get_or_create_port(device_id, port_name)
        p.mode = 'access'
        p.access_vlan = vlan_id
        # VLANが未作成なら自動作成
        self.create_vlan(device_id, vlan_id)
        # VLANメンバーにポートを追加
        vinfo = self.vlans[device_id][vlan_id]
        if port_name not in vinfo.ports:
            vinfo.ports.append(port_name)

    def set_trunk(self, device_id: str, port_name: str,
                  native_vlan: int = 1,
                  allowed: str = 'all'):
        """switchport mode trunk"""
        p = self._get_or_create_port(device_id, port_name)
        p.mode = 'trunk'
        p.native_vlan = native_vlan
        p.allowed_vlans = self._parse_vlan_range(allowed)

    def set_trunk_allowed(self, device_id: str, port_name: str, allowed_str: str):
        """switchport trunk allowed vlan <list>"""
        p = self._get_or_create_port(device_id, port_name)
        p.allowed_vlans = self._parse_vlan_range(allowed_str)

    def set_trunk_allowed_add(self, device_id: str, port_name: str, add_str: str):
        """switchport trunk allowed vlan add <list>"""
        p = self._get_or_create_port(device_id, port_name)
        p.allowed_vlans |= self._parse_vlan_range(add_str)

    def set_trunk_allowed_remove(self, device_id: str, port_name: str, rem_str: str):
        """switchport trunk allowed vlan remove <list>"""
        p = self._get_or_create_port(device_id, port_name)
        p.allowed_vlans -= self._parse_vlan_range(rem_str)

    def set_native_vlan(self, device_id: str, port_name: str, vlan_id: int):
        """switchport trunk native vlan <id>"""
        p = self._get_or_create_port(device_id, port_name)
        p.native_vlan = vlan_id

    # ── VLAN通信可否チェック ──────────────
    def can_communicate(self, dev_a: str, port_a: str,
                        dev_b: str, port_b: str,
                        vlan_id: int = None) -> tuple:
        """
        2ポート間でL2通信が可能かチェック（IEEE 802.1Q準拠）
        戻り値: (bool, str)  -- (通信可否, 理由)

        ルール:
        1. アクセス-アクセス: 同じアクセスVLANのみ通信可
        2. アクセス-トランク: アクセスVLANがtrunkのallowedに含まれる場合のみ
        3. トランク-トランク: 共通のallowed VLANがある場合通信可
        4. VLANが未設定の場合はVLAN1として扱う
        """
        pa = self.ports[dev_a].get(port_a, VlanPort())
        pb = self.ports[dev_b].get(port_b, VlanPort())

        if pa.mode == 'access' and pb.mode == 'access':
            if pa.access_vlan == pb.access_vlan:
                return True, f'VLAN{pa.access_vlan}で通信可'
            return False, f'異なるVLAN: VLAN{pa.access_vlan} ≠ VLAN{pb.access_vlan}'

        elif pa.mode == 'access' and pb.mode == 'trunk':
            if pa.access_vlan in pb.allowed_vlans:
                return True, f'VLAN{pa.access_vlan}がtrunkのallowedに含まれる'
            return False, f'VLAN{pa.access_vlan}がtrunkのallowedに含まれない'

        elif pa.mode == 'trunk' and pb.mode == 'access':
            if pb.access_vlan in pa.allowed_vlans:
                return True, f'VLAN{pb.access_vlan}がtrunkのallowedに含まれる'
            return False, f'VLAN{pb.access_vlan}がtrunkのallowedに含まれない'

        elif pa.mode == 'trunk' and pb.mode == 'trunk':
            common = pa.allowed_vlans & pb.allowed_vlans
            if vlan_id:
                if vlan_id in common:
                    return True, f'VLAN{vlan_id}が両トランクのallowedに含まれる'
                return False, f'VLAN{vlan_id}が片方または両方のallowedに含まれない'
            if common:
                vlans_str = self._format_vlan_range(common)
                return True, f'共通VLAN: {vlans_str}'
            return False, 'allowed VLANに共通VLANなし'

        return True, 'VLAN未設定のため通信許可'

    def can_reach_vlan(self, dev_a: str, dev_b: str, vlan_id: int) -> bool:
        """
        2装置間でVLAN通信が可能かチェック。
        双方のポート設定を確認し、同一VLANに属するか確認する。
        """
        # ポートが未設定 → 通信許可（設定中）
        if not self.ports[dev_a] or not self.ports[dev_b]:
            return True

        # dev_a側のポートでvlan_idを通せるポートがあるか
        a_ok = False
        for port_name, port in self.ports[dev_a].items():
            if port.mode == 'access' and port.access_vlan == vlan_id:
                a_ok = True; break
            if port.mode == 'trunk' and vlan_id in port.allowed_vlans:
                a_ok = True; break
        if not a_ok:
            return False

        # dev_b側も同様
        b_ok = False
        for port_name, port in self.ports[dev_b].items():
            if port.mode == 'access' and port.access_vlan == vlan_id:
                b_ok = True; break
            if port.mode == 'trunk' and vlan_id in port.allowed_vlans:
                b_ok = True; break

        return b_ok

    # ── show コマンド ──────────────────────
    def format_show_vlan(self, device_id: str, brief: bool = False) -> str:
        """show vlan [brief]"""
        vdb = dict(self.vlans[device_id])
        # VLAN1は常に存在
        if 1 not in vdb:
            vdb[1] = VlanInfo(vlan_id=1, name='default', state='active')

        lines = [
            'VLAN Name                             Status    Ports',
            '---- -------------------------------- --------- -------------------------------',
        ]
        for vid in sorted(vdb.keys()):
            v = vdb[vid]
            name = v.name[:32] if v.name else f'VLAN{vid:04d}'
            ports_str = ', '.join(v.ports[:6])
            if len(v.ports) > 6:
                ports_str += ', ...'
            lines.append(f'{vid:<5}{name:<33}{v.state:<10}{ports_str}')

        if not brief:
            lines.extend(['', 'VLAN Type  SAID       MTU   Parent RingNo BridgeNo Stp  BrdgMode Trans1 Trans2'])
            lines.append('---- ----- ---------- ----- ------ ------ -------- ---- -------- ------ ------')
            for vid in sorted(vdb.keys()):
                lines.append(f'{vid:<5}{"enet":<6}{100000+vid:<11}{"1500":<6}{"  -":<7}{"  -":<7}{"  -":<9}{"  -":<5}{"  -":<9}{"0":<7}{"0"}')
        return '\n'.join(lines)

    def format_show_vlan_id(self, device_id: str, vlan_id: int) -> str:
        """show vlan id <id>"""
        vdb = self.vlans[device_id]
        if vlan_id not in vdb:
            return f'% VLAN {vlan_id} does not exist in the current database.'
        v = vdb[vlan_id]
        lines = [
            'VLAN Name                             Status    Ports',
            '---- -------------------------------- --------- -------------------------------',
            f'{vlan_id:<5}{v.name[:32]:<33}{v.state:<10}{", ".join(v.ports)}',
            '',
            f'VLAN Type  SAID       MTU',
            f'---- ----- ---------- -----',
            f'{vlan_id:<5}{"enet":<6}{100000+vlan_id:<11}1500',
        ]
        return '\n'.join(lines)

    def format_show_interfaces_trunk(self, device_id: str) -> str:
        """show interfaces trunk"""
        trunk_ports = {p: info for p, info in self.ports[device_id].items()
                       if info.mode == 'trunk'}
        if not trunk_ports:
            return '(No trunk ports configured)'

        def short(name):
            return (name.replace('GigabitEthernet', 'Gi')
                        .replace('FastEthernet', 'Fa')
                        .replace('TenGigabitEthernet', 'Te')
                        .replace('Port-channel', 'Po'))

        sections = []
        lines1 = ['Port        Mode         Encapsulation  Status        Native vlan']
        for pname, p in trunk_ports.items():
            lines1.append(f'{short(pname):<12}{"on":<13}{"802.1q":<15}{"trunking":<14}{p.native_vlan}')
        sections.append('\n'.join(lines1))

        lines2 = ['Port        VLANs allowed on trunk']
        for pname, p in trunk_ports.items():
            lines2.append(f'{short(pname):<12}{self._format_vlan_range(p.allowed_vlans)}')
        sections.append('\n'.join(lines2))

        lines3 = ['Port        VLANs allowed and active in management domain']
        vdb = self.vlans[device_id]
        for pname, p in trunk_ports.items():
            active = {v for v in p.allowed_vlans if v in vdb and vdb[v].state == 'active'}
            active.add(p.native_vlan)
            lines3.append(f'{short(pname):<12}{self._format_vlan_range(active)}')
        sections.append('\n'.join(lines3))

        return '\n\n'.join(sections)

    # ── ユーティリティ ──────────────────
    def _get_or_create_port(self, device_id: str, port_name: str) -> VlanPort:
        if port_name not in self.ports[device_id]:
            self.ports[device_id][port_name] = VlanPort()
        return self.ports[device_id][port_name]

    @staticmethod
    def _parse_vlan_range(s: str) -> set:
        """
        "1,10,20-30,100" → {1,10,20,21,...,30,100}
        "all" → {1..4094}
        "none" → set()
        """
        if not s or s.strip() == 'all':
            return set(range(1, 4095))
        if s.strip() == 'none':
            return set()
        result = set()
        for part in s.split(','):
            part = part.strip()
            if '-' in part:
                lo, hi = part.split('-', 1)
                result.update(range(int(lo.strip()), int(hi.strip()) + 1))
            elif part.isdigit():
                result.add(int(part))
        return result

    @staticmethod
    def _format_vlan_range(vlans: set) -> str:
        """
        {1,2,3,10,11,12,20} → "1-3,10-12,20"
        """
        if not vlans:
            return 'none'
        slist = sorted(vlans)
        parts = []
        start = prev = slist[0]
        for v in slist[1:]:
            if v == prev + 1:
                prev = v
            else:
                parts.append(f'{start}-{prev}' if start != prev else str(start))
                start = prev = v
        parts.append(f'{start}-{prev}' if start != prev else str(start))
        return ','.join(parts)


vlan_engine = VlanEngine()


# ══════════════════════════════════════════
# vPC エンジン（NX-OS Cisco Nexus 9000）
# ══════════════════════════════════════════
@dataclass
class VpcMemberPort:
    """vPCメンバーポート"""
    port_channel: str       # Port-channelインターフェース名
    vpc_id: int             # vPC番号
    state: str = 'down'     # up / down / suspended
    is_peer_link: bool = False

@dataclass
class VpcDomain:
    """vPCドメイン"""
    domain_id: int
    # Keepalive設定
    keepalive_dest: str = ''
    keepalive_src: str = ''
    keepalive_vrf: str = 'management'
    keepalive_interval: int = 1000   # ms
    keepalive_timeout: int = 5
    keepalive_state: str = 'pending'  # pending / alive / dead
    # Peer-Link
    peer_link_if: str = ''
    peer_link_state: str = 'down'    # down / up
    # vPCロール
    role: str = 'none'               # none / primary / secondary
    role_priority: int = 32667
    system_mac: str = ''
    system_priority: int = 32667
    # 機能フラグ
    peer_gateway: bool = False
    peer_switch: bool = False
    auto_recovery: bool = False
    # タスク
    keepalive_task: Optional[Any] = None
    # メンバーポート
    member_ports: Dict[int, VpcMemberPort] = field(default_factory=dict)


class VpcEngine:
    """
    NX-OS vPC (Virtual Port Channel) エンジン
    
    vPCの動作概要（Cisco NX-OS 仕様準拠）:
    1. feature vpc で機能有効化
    2. vpc domain <id> でドメイン設定
    3. peer-keepalive destination <ip> でL3 Keepalive設定
    4. port-channel に vpc peer-link を割り当てでピアリンク設定
    5. port-channel に vpc <id> を割り当てでvPCメンバー設定
    
    ロール選出:
    - role priority 小さい方が Primary
    - 同じなら MAC アドレスが小さい方が Primary
    
    フェイルオーバー:
    - Keepaliveが途絶えた場合: Secondaryが Operational Primary に昇格
    - Peer-Linkが落ちた場合: Secondaryのvポートが suspend
    """
    def __init__(self):
        # device_id -> VpcDomain
        self.domains: Dict[str, VpcDomain] = {}
        # device_id -> enabled
        self.feature_enabled: Dict[str, bool] = {}
        # ピア関係 device_id -> peer_device_id
        self.peers: Dict[str, str] = {}

    def enable_feature(self, device_id: str):
        """feature vpc"""
        self.feature_enabled[device_id] = True
        _spawn(vnet.send_to(device_id, {
            'type': 'vpc_log',
            'message': 'vPC feature enabled'
        }))

    def create_domain(self, device_id: str, domain_id: int) -> VpcDomain:
        """vpc domain <id>"""
        if device_id not in self.domains:
            mac = f'00:23:04:{abs(hash(device_id))%256:02x}:{domain_id:02x}:00'
            self.domains[device_id] = VpcDomain(
                domain_id=domain_id,
                system_mac=mac,
            )
        else:
            self.domains[device_id].domain_id = domain_id
        return self.domains[device_id]

    def set_keepalive(self, device_id: str, dest: str,
                      src: str = '', vrf: str = 'management'):
        """peer-keepalive destination <ip> [source <ip>] [vrf <vrf>]"""
        d = self.domains.get(device_id)
        if not d:
            return
        d.keepalive_dest = dest
        d.keepalive_src = src
        d.keepalive_vrf = vrf
        # Keepalive開始
        if d.keepalive_task:
            d.keepalive_task.cancel()
        d.keepalive_task = _spawn(self._keepalive_loop(device_id))

    async def _keepalive_loop(self, device_id: str):
        """Peer-Keepalive送受信ループ"""
        await asyncio.sleep(0.5)
        d = self.domains.get(device_id)
        if not d:
            return
        # 対向装置を探してピア関係を確立
        peer_id = self.peers.get(device_id)
        if not peer_id:
            # Keepalive宛先IPからピアを探す
            for peer, dom in self.domains.items():
                if peer == device_id:
                    continue
                if dom.keepalive_dest == d.keepalive_src or \
                   d.keepalive_dest == dom.keepalive_src or \
                   dom.keepalive_dest and d.keepalive_dest:
                    # 同じvPCドメインIDなら自動ペアリング
                    if dom.domain_id == d.domain_id:
                        self.peers[device_id] = peer
                        self.peers[peer] = device_id
                        peer_id = peer
                        break

        if peer_id:
            d.keepalive_state = 'alive'
            peer_dom = self.domains.get(peer_id)
            if peer_dom:
                peer_dom.keepalive_state = 'alive'
            # ロール選出
            await self._elect_role(device_id, peer_id)

        while True:
            await asyncio.sleep(d.keepalive_interval / 1000.0)
            d = self.domains.get(device_id)
            if not d:
                break
            peer_id = self.peers.get(device_id)
            if peer_id and peer_id in self.domains:
                d.keepalive_state = 'alive'
                pkt = {
                    'type': 'vpc_keepalive',
                    'src_id': device_id,
                    'domain_id': d.domain_id,
                    'role': d.role,
                    'role_priority': d.role_priority,
                }
                await vnet.send_to(peer_id, pkt)

    async def _elect_role(self, dev_a: str, dev_b: str):
        """
        vPCロール選出
        role priority 小さい方が Primary
        同じなら system MAC 小さい方が Primary
        """
        da = self.domains.get(dev_a)
        db = self.domains.get(dev_b)
        if not da or not db:
            return
        # ロール選出
        if da.role_priority < db.role_priority:
            da.role = 'primary'
            db.role = 'secondary'
        elif da.role_priority > db.role_priority:
            da.role = 'secondary'
            db.role = 'primary'
        else:
            # 優先度同じ → MAC比較
            if da.system_mac < db.system_mac:
                da.role = 'primary'
                db.role = 'secondary'
            else:
                da.role = 'secondary'
                db.role = 'primary'

        for dev_id, dom in [(dev_a, da), (dev_b, db)]:
            await vnet.send_to(dev_id, {
                'type': 'vpc_log',
                'role': dom.role,
                'domain_id': dom.domain_id,
                'message': vendor_log.vpc_status(
                    vnet.ws_send_callbacks.get(f'_hostname_{dev_id}', dev_id),
                    dom.domain_id, dom.role, 'Peer keepalive status is alive'
                )
            })
        # Peer-Linkが設定済みなら上げる
        await self._bring_up_peer_link(dev_a, dev_b)

    async def _bring_up_peer_link(self, dev_a: str, dev_b: str):
        """Peer-Linkをupにする"""
        da = self.domains.get(dev_a)
        db = self.domains.get(dev_b)
        if not da or not db:
            return
        if da.peer_link_if and db.peer_link_if:
            da.peer_link_state = 'up'
            db.peer_link_state = 'up'
            # メンバーポートを全てupに
            for vpc_id, mp in da.member_ports.items():
                mp.state = 'up'
            for vpc_id, mp in db.member_ports.items():
                mp.state = 'up'
            for dev_id, dom in [(dev_a, da), (dev_b, db)]:
                await vnet.send_to(dev_id, {
                    'type': 'vpc_log',
                    'message': (f'%VPC-6-VPC_STATUS_CHANGE: '
                                f'vPC Peer-Link is up, '
                                f'vPC domain {dom.domain_id} is up')
                })

    def set_peer_link(self, device_id: str, port_channel: str):
        """interface port-channelX → vpc peer-link"""
        d = self.domains.get(device_id)
        if not d:
            return
        d.peer_link_if = port_channel
        _spawn(vnet.send_to(device_id, {
            'type': 'vpc_log',
            'message': f'vPC Peer-Link configured on {port_channel}'
        }))
        # ピアが既にKeepalive確立済みなら即座にPeer-Linkを上げる
        peer_id = self.peers.get(device_id)
        if peer_id and d.keepalive_state == 'alive':
            _spawn(self._bring_up_peer_link(device_id, peer_id))

    def add_vpc_member(self, device_id: str, port_channel: str, vpc_id: int):
        """interface port-channelX → vpc <id>"""
        d = self.domains.get(device_id)
        if not d:
            return
        mp = VpcMemberPort(
            port_channel=port_channel,
            vpc_id=vpc_id,
            state='up' if d.peer_link_state == 'up' else 'down'
        )
        d.member_ports[vpc_id] = mp
        _spawn(vnet.send_to(device_id, {
            'type': 'vpc_log',
            'message': f'vPC {vpc_id} configured on {port_channel}'
        }))

    def set_role_priority(self, device_id: str, priority: int):
        """role priority <value>"""
        d = self.domains.get(device_id)
        if d:
            d.role_priority = priority

    def set_peer_gateway(self, device_id: str, enabled: bool = True):
        """peer-gateway"""
        d = self.domains.get(device_id)
        if d:
            d.peer_gateway = enabled

    def set_peer_switch(self, device_id: str, enabled: bool = True):
        """peer-switch"""
        d = self.domains.get(device_id)
        if d:
            d.peer_switch = enabled

    async def simulate_peer_failure(self, device_id: str):
        """ピア障害シミュレーション（Keepalive途絶）"""
        d = self.domains.get(device_id)
        if not d:
            return
        if d.keepalive_task:
            d.keepalive_task.cancel()
        d.keepalive_state = 'dead'
        peer_id = self.peers.get(device_id)
        if peer_id:
            peer_dom = self.domains.get(peer_id)
            if peer_dom:
                peer_dom.keepalive_state = 'dead'
                # Secondaryが Operational Primary に昇格
                if peer_dom.role == 'secondary':
                    peer_dom.role = 'primary (oper)'
                    await vnet.send_to(peer_id, {
                        'type': 'vpc_log',
                        'message': (f'%VPC-6-VPC_STATUS_CHANGE: '
                                    f'Peer keepalive is dead, '
                                    f'vPC secondary assuming operational primary role')
                    })

    async def simulate_peer_link_failure(self, device_id: str):
        """Peer-Link障害シミュレーション"""
        d = self.domains.get(device_id)
        if not d:
            return
        d.peer_link_state = 'down'
        peer_id = self.peers.get(device_id)
        if peer_id:
            peer_dom = self.domains.get(peer_id)
            if peer_dom:
                peer_dom.peer_link_state = 'down'
                # Secondary側のvPCポートを即座にsuspend
                if 'secondary' in peer_dom.role or peer_dom.role == 'none':
                    for mp in peer_dom.member_ports.values():
                        mp.state = 'suspended'
                    await vnet.send_to(peer_id, {
                        'type': 'vpc_log',
                        'message': (f'%VPC-6-VPC_STATUS_CHANGE: '
                                    f'Peer-Link is down, '
                                    f'Secondary vPC ports suspended to avoid loop')
                    })
                # Primary側もPeer-Link downを通知
                await vnet.send_to(device_id, {
                    'type': 'vpc_log',
                    'message': f'%VPC-6-PEER_LINK_CHANGE: vPC Peer-Link is down'
                })

    # ── show コマンド ──────────────────────
    def format_show_vpc(self, device_id: str) -> str:
        """show vpc"""
        d = self.domains.get(device_id)
        if not d:
            return '% vPC not configured. Use: feature vpc → vpc domain <id>'
        if not self.feature_enabled.get(device_id):
            return '% vPC feature not enabled. Use: feature vpc'

        peer_id = self.peers.get(device_id)
        peer_role = 'N/A'
        if peer_id and peer_id in self.domains:
            peer_role = self.domains[peer_id].role

        lines = [
            f'vPC domain id                     : {d.domain_id}',
            f'Peer status                        : {d.keepalive_state}',
            f'vPC keep-alive status              : {d.keepalive_state}',
            f'Configuration consistency status   : success',
            f'Per-vlan consistency status        : success',
            f'Type-2 consistency status          : success',
            f'vPC role                           : {d.role}',
            f'Number of vPCs configured          : {len(d.member_ports)}',
            f'Peer Gateway                       : {"Enabled" if d.peer_gateway else "Disabled"}',
            f'Peer-switch                        : {"Enabled" if d.peer_switch else "Disabled"}',
            '',
            f'vPC Peer-link status',
            f'---------------------------------------------------------------------',
            f'id    Port   Status Active vlans',
            f'--    ----   ------ --------------------------------------------------',
            f'1     {d.peer_link_if or "N/A":<6} {d.peer_link_state:<6} -',
            '',
            f'vPC status',
            f'----------------------------------------------------------------------------',
            f'Id    Port          Status Consistency Reason                Active vlans',
            f'--    ------------- ------ ----------- ------                ------------',
        ]
        for vpc_id, mp in sorted(d.member_ports.items()):
            consistency = 'success' if mp.state == 'up' else 'failed'
            reason = '-' if mp.state == 'up' else 'peer-link down'
            lines.append(
                f'{vpc_id:<6}{mp.port_channel:<14}{mp.state:<7}{consistency:<12}'
                f'{reason:<22}-'
            )
        return '\n'.join(lines)

    def format_show_vpc_brief(self, device_id: str) -> str:
        """show vpc brief"""
        d = self.domains.get(device_id)
        if not d:
            return '% vPC not configured.'
        lines = [
            f'vPC domain id                     : {d.domain_id}',
            f'Peer status                        : {d.keepalive_state}',
            f'vPC role                           : {d.role}',
            f'vPC Peer-Link                      : {d.peer_link_if or "Not configured"} ({d.peer_link_state})',
            f'Number of vPCs                     : {len(d.member_ports)}',
        ]
        for vpc_id, mp in sorted(d.member_ports.items()):
            lines.append(f'  vPC {vpc_id:<4}: {mp.port_channel} ({mp.state})')
        return '\n'.join(lines)

    def format_show_vpc_keepalive(self, device_id: str) -> str:
        """show vpc peer-keepalive"""
        d = self.domains.get(device_id)
        if not d:
            return '% vPC not configured.'
        lines = [
            f'vPC keep-alive status              : {d.keepalive_state}',
            f'--Destination                      : {d.keepalive_dest or "Not configured"}',
            f'--Source                           : {d.keepalive_src or "Not configured"}',
            f'--Vrf                              : {d.keepalive_vrf}',
            f'--Keepalive interval               : {d.keepalive_interval} msec',
            f'--Keepalive timeout                : {d.keepalive_timeout} sec',
            f'--Keepalive hold timeout           : {d.keepalive_timeout * 3} sec',
        ]
        return '\n'.join(lines)

    def format_show_vpc_role(self, device_id: str) -> str:
        """show vpc role"""
        d = self.domains.get(device_id)
        if not d:
            return '% vPC not configured.'
        peer_id = self.peers.get(device_id)
        peer_dom = self.domains.get(peer_id, VpcDomain(0)) if peer_id else VpcDomain(0)
        lines = [
            f'vPC Role status',
            f'---------------------------------------------------',
            f'Configured role                    : {d.role}',
            f'Current role                       : {d.role}',
            f'Role priority                      : {d.role_priority}',
            f'Dual Active Detection Status       : 0',
            f'System-mac address                 : {d.system_mac}',
            f'Local system-mac address           : {d.system_mac}',
            f'Peer system-mac address            : {peer_dom.system_mac}',
            f'Peer fabric-path system-id         : 0',
            f'Local system-priority              : {d.system_priority}',
            f'Peer system-priority               : {peer_dom.system_priority}',
        ]
        return '\n'.join(lines)

    def format_show_vpc_consistency(self, device_id: str) -> str:
        """show vpc consistency-parameters global"""
        d = self.domains.get(device_id)
        if not d:
            return '% vPC not configured.'
        lines = [
            'Name                        Type  Local Value            Peer Value',
            '--------------------------- ----- ---------------------- --------------------',
            f'STP mode                    1     Rapid-PVST+            Rapid-PVST+',
            f'STP MST region name         1     ""                     ""',
            f'STP port type, edge         1     Normal, Disabled       Normal, Disabled',
            f'STP MST region revision     1     0                      0',
            f'Xconnect Vlans              1     -                      -',
            f'Mode                        1     VPC                    VPC',
            f'Peer-link                   1     Present                Present',
        ]
        return '\n'.join(lines)

    def get_running_config(self, device_id: str) -> list:
        """show runのvPC部分を返す"""
        lines = []
        if not self.feature_enabled.get(device_id):
            return lines
        lines.append('feature vpc')
        d = self.domains.get(device_id)
        if not d:
            return lines
        lines.append(f'vpc domain {d.domain_id}')
        if d.role_priority != 32667:
            lines.append(f'  role priority {d.role_priority}')
        if d.peer_gateway:
            lines.append('  peer-gateway')
        if d.peer_switch:
            lines.append('  peer-switch')
        if d.keepalive_dest:
            src_str = f' source {d.keepalive_src}' if d.keepalive_src else ''
            vrf_str = f' vrf {d.keepalive_vrf}' if d.keepalive_vrf != 'management' else ''
            lines.append(f'  peer-keepalive destination {d.keepalive_dest}{src_str}{vrf_str}')
        if d.auto_recovery:
            lines.append('  auto-recovery')
        lines.append('!')
        if d.peer_link_if:
            lines.append(f'interface {d.peer_link_if}')
            lines.append('  switchport mode trunk')
            lines.append('  vpc peer-link')
            lines.append('!')
        for vpc_id, mp in sorted(d.member_ports.items()):
            lines.append(f'interface {mp.port_channel}')
            lines.append(f'  vpc {vpc_id}')
            lines.append('!')
        return lines


vpc_engine = VpcEngine()


# ══════════════════════════════════════════
# ベンダーログフォーマッター
# Cisco IOS / NX-OS / Si-R 各形式のログを生成
# ══════════════════════════════════════════
from datetime import datetime

class VendorLogFormatter:
    """
    各ベンダーのsyslogメッセージ形式に変換する

    Cisco IOS (Catalyst/ISR):
      *Mar  1 00:00:01.123: %FACILITY-SEV-MNEMONIC: description

    NX-OS (Nexus 9000):
      2024 Jan 01 00:00:01.123 hostname %FACILITY-SEV-MNEMONIC: description

    Si-R (富士通):
      2024/01/01 00:00:01 hostname: [facility] description
    """

    # ── Cisco IOS形式 ──────────────────────
    @staticmethod
    def ios(facility: str, severity: int, mnemonic: str, description: str) -> str:
        """
        Cisco IOS/IOS-XE (Catalyst/ISR) 標準形式
        *Jan 01 00:00:01.123: %FACILITY-SEV-MNEMONIC: description
        """
        now = datetime.now()
        ts = now.strftime('*%b %d %H:%M:%S') + f'.{now.microsecond//1000:03d}'
        return f'{ts}: %{facility}-{severity}-{mnemonic}: {description}'

    # ── NX-OS形式 ──────────────────────────
    @staticmethod
    def nxos(hostname: str, facility: str, severity: int,
             mnemonic: str, description: str) -> str:
        """
        Cisco NX-OS (Nexus 9000) 標準形式
        2024 Jan 01 00:00:01.123 hostname %FACILITY-SEV-MNEMONIC: description
        """
        now = datetime.now()
        ts = now.strftime('%Y %b %d %H:%M:%S') + f'.{now.microsecond//1000:03d}'
        return f'{ts} {hostname} %{facility}-{severity}-{mnemonic}: {description}'

    # ── Si-R形式 ───────────────────────────
    @staticmethod
    def sir(hostname: str, facility: str, description: str,
            level: str = 'INFO') -> str:
        """
        富士通 Si-R syslog形式
        2024/01/01 00:00:01 hostname: [FACILITY] description
        """
        now = datetime.now()
        ts = now.strftime('%Y/%m/%d %H:%M:%S')
        return f'{ts} {hostname}: [{facility}] {description}'

    # ── プロトコル別ログ生成 ──────────────
    # RIP
    @staticmethod
    def rip_start(device_type: str, hostname: str, version: int, networks: list) -> str:
        nets = ', '.join(networks)
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'RIPV2', 5, 'RDBUPDATE',
                f'Sending update to 224.0.0.9 via all interfaces - v{version} {nets}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'RIP', 5, 'RDBUPDATE',
                f'RIP process started. Advertising: {nets}'
            )
        else:  # sir
            return VendorLogFormatter.sir(
                hostname, 'RIP',
                f'RIPv{version} started. Networks: {nets}'
            )

    @staticmethod
    def rip_route_learned(device_type: str, hostname: str,
                          network: str, prefix: int,
                          metric: int, via: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'RIPV2', 6, 'RDBUPDATE',
                f'Updating full routing table - {network}/{prefix} [120/{metric}] via {via}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'RIP', 6, 'RDBUPDATE',
                f'Route {network}/{prefix} learned from {via} metric {metric}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'RIP',
                f'learned: {network}/{prefix} metric={metric} via {via}'
            )

    @staticmethod
    def rip_route_expired(device_type: str, hostname: str,
                          network: str, prefix: int) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'RIPV2', 6, 'RDBUPDATE',
                f'Route {network}/{prefix} timed out'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'RIP', 6, 'RDBUPDATE',
                f'Route {network}/{prefix} expired (180 sec timeout)'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'RIP',
                f'route expired: {network}/{prefix} (180sec)'
            )

    # OSPF
    @staticmethod
    def ospf_start(device_type: str, hostname: str,
                   process_id: int, router_id: str, area: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'OSPF', 5, 'ADJCHG',
                f'Process {process_id} started. Router ID {router_id}, Area {area}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'OSPF', 5, 'ADJCHG',
                f'Process {process_id}: Router-ID {router_id} Area {area}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'OSPF',
                f'ospf use on: router-id={router_id} area={area}'
            )

    @staticmethod
    def ospf_neighbor_change(device_type: str, hostname: str,
                             process_id: int, neighbor_id: str,
                             interface: str, from_state: str, to_state: str,
                             reason: str = '') -> str:
        reason_str = f', {reason}' if reason else ''
        if device_type in ('catalyst', 'cisco', 'srs', 'nexus'):
            if device_type == 'nexus':
                return VendorLogFormatter.nxos(
                    hostname, 'OSPF', 5, 'ADJCHG',
                    f'Process {process_id}, Nbr {neighbor_id} on {interface} '
                    f'from {from_state} to {to_state}{reason_str}'
                )
            return VendorLogFormatter.ios(
                'OSPF', 5, 'ADJCHG',
                f'Process {process_id}, Nbr {neighbor_id} on {interface} '
                f'from {from_state} to {to_state}{reason_str}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'OSPF',
                f'neighbor {neighbor_id} on {interface}: {from_state} -> {to_state}{reason_str}'
            )

    @staticmethod
    def ospf_dr_elected(device_type: str, hostname: str,
                        process_id: int, dr_router_id: str,
                        priority: int, interface: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'OSPF', 6, 'DRCHG',
                f'DR Change from 0.0.0.0 to {dr_router_id} on {interface}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'OSPF', 6, 'DRCHG',
                f'Process {process_id}: DR elected {dr_router_id} '
                f'(priority {priority}) on {interface}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'OSPF',
                f'DR elected: {dr_router_id} (priority={priority}) on {interface}'
            )

    @staticmethod
    def ospf_spf(device_type: str, hostname: str,
                 process_id: int, num_routes: int) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'OSPF', 6, 'LOGADJ',
                f'Process {process_id}: SPF calculation complete, {num_routes} routes'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'OSPF', 6, 'LOGADJ',
                f'Process {process_id}: SPF calculation complete '
                f'({num_routes} routes installed)'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'OSPF',
                f'SPF: {num_routes} routes calculated'
            )

    # BGP
    @staticmethod
    def bgp_start(device_type: str, hostname: str,
                  local_as: int, router_id: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'BGP', 5, 'ADJCHANGE',
                f'Process {local_as} started. Router ID {router_id}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'BGP', 5, 'ADJCHANGE',
                f'BGP process started. AS {local_as}, Router-ID {router_id}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'BGP',
                f'BGP started: AS={local_as} router-id={router_id}'
            )

    @staticmethod
    def bgp_neighbor_change(device_type: str, hostname: str,
                            neighbor: str, remote_as: int,
                            state: str) -> str:
        direction = 'Up' if state == 'Established' else 'Down'
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'BGP', 5, 'ADJCHANGE',
                f'neighbor {neighbor} {direction}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'BGP', 5, 'ADJCHANGE',
                f'neighbor {neighbor} (AS{remote_as}) {direction} '
                f'(state: {state})'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'BGP',
                f'peer {neighbor} AS{remote_as} state: {state}'
            )

    # STP
    @staticmethod
    def stp_port_state(device_type: str, hostname: str,
                       port: str, from_state: str, to_state: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'SPANTREE', 6, 'PORTDPVSTP',
                f'Port {port} instance 0 moving from {from_state} to {to_state}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'STP', 6, 'STG_PORT_STATE',
                f'vlan 1 port {port}: {from_state} -> {to_state}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'STP',
                f'port {port}: {from_state} -> {to_state}'
            )

    @staticmethod
    def stp_root_change(device_type: str, hostname: str,
                        vlan: int, new_root: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'SPANTREE', 5, 'ROOTCHANGE',
                f'Root Changed for vlan {vlan}: new root is {new_root}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'STP', 5, 'ROOTCHANGE',
                f'vlan {vlan}: New root bridge: {new_root}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'STP',
                f'vlan {vlan}: root changed to {new_root}'
            )

    @staticmethod
    def stp_tc(device_type: str, hostname: str, src: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'SPANTREE', 6, 'TOPOTRAP',
                f'Topology change detected on {src}. '
                f'Notification sent to {hostname}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'STP', 6, 'TOPOTRAP',
                f'Topology change received from {src}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'STP',
                f'topology change received from {src}'
            )

    # LACP/EtherChannel
    @staticmethod
    def lacp_bundled(device_type: str, hostname: str,
                     channel_id: int, members: list) -> str:
        mstr = ', '.join(members[:4])
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'EC', 5, 'BUNDLE',
                f'Port-channel{channel_id} created: [{mstr}] bundled'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'ETH_PORT_CHANNEL', 5, 'FOP_CHANGED',
                f'port-channel{channel_id}: first operational port '
                f'is now {members[0] if members else "unknown"}'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'LACP',
                f'linkaggregation {channel_id} bundled: [{mstr}]'
            )

    # VRRP
    @staticmethod
    def vrrp_state(device_type: str, hostname: str,
                   group_id: int, interface: str,
                   from_state: str, to_state: str,
                   vip: str, priority: int) -> str:
        if device_type in ('catalyst', 'cisco', 'srs'):
            return VendorLogFormatter.ios(
                'VRRP', 6, 'STATECHANGE',
                f'Group {group_id} {interface} state {from_state} -> {to_state}'
            )
        elif device_type == 'nexus':
            return VendorLogFormatter.nxos(
                hostname, 'VRRP', 6, 'STATECHANGE',
                f'Group {group_id} on interface {interface}: '
                f'{from_state} -> {to_state} (VIP={vip})'
            )
        else:  # sir
            return VendorLogFormatter.sir(
                hostname, 'VRRP',
                f'VRID {group_id} on {interface}: {from_state} -> {to_state} '
                f'(vip={vip} priority={priority})'
            )

    # HSRP
    @staticmethod
    def hsrp_state(device_type: str, hostname: str,
                   group_id: int, interface: str,
                   from_state: str, to_state: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs', 'nexus'):
            if device_type == 'nexus':
                return VendorLogFormatter.nxos(
                    hostname, 'HSRP', 5, 'STATECHANGE',
                    f'{interface} Grp {group_id} state {from_state} -> {to_state}'
                )
            return VendorLogFormatter.ios(
                'HSRP', 5, 'STATECHANGE',
                f'{interface} Grp {group_id} state {from_state} -> {to_state}'
            )
        return f'HSRP: {interface} Grp {group_id} {from_state} -> {to_state}'

    # VPC (NX-OS専用)
    @staticmethod
    def vpc_status(hostname: str, domain_id: int, role: str, reason: str) -> str:
        return VendorLogFormatter.nxos(
            hostname, 'VPC', 6, 'VPC_STATUS_CHANGE',
            f'domain {domain_id}: role changed to {role.upper()}, {reason}'
        )

    @staticmethod
    def vpc_peer_link(hostname: str, domain_id: int, state: str) -> str:
        return VendorLogFormatter.nxos(
            hostname, 'VPC', 6, 'PEER_LINK_CHANGE',
            f'domain {domain_id}: Peer-Link is {state}'
        )

    # Link Up/Down
    @staticmethod
    def link_updown(device_type: str, hostname: str,
                    interface: str, state: str) -> str:
        state_str = 'up' if state == 'up' else 'down'
        if device_type in ('catalyst', 'cisco', 'srs'):
            return (
                VendorLogFormatter.ios(
                    'LINK', 3, 'UPDOWN',
                    f'Interface {interface}, changed state to {state_str}'
                ) + '\n' +
                VendorLogFormatter.ios(
                    'LINEPROTO', 5, 'UPDOWN',
                    f'Line protocol on Interface {interface}, '
                    f'changed state to {state_str}'
                )
            )
        elif device_type == 'nexus':
            return (
                VendorLogFormatter.nxos(
                    hostname, 'ETH_PORT', 5, 'IF_UP' if state=='up' else 'IF_DOWN',
                    f'Interface {interface} is {state_str} in vlan 1'
                ) + '\n' +
                VendorLogFormatter.nxos(
                    hostname, 'LINEPROTO', 5, 'UPDOWN',
                    f'Line protocol on Interface {interface}, '
                    f'changed state to {state_str}'
                )
            )
        else:  # sir
            return VendorLogFormatter.sir(
                hostname, 'LINK',
                f'interface {interface} link {state_str}'
            )

    # System/Config
    @staticmethod
    def sys_config(device_type: str, hostname: str) -> str:
        if device_type in ('catalyst', 'cisco', 'srs', 'nexus'):
            if device_type == 'nexus':
                return VendorLogFormatter.nxos(
                    hostname, 'SYSMGR', 5, 'MGMT_CONF',
                    f'Configured from console by admin on vty0'
                )
            return VendorLogFormatter.ios(
                'SYS', 5, 'CONFIG_I',
                f'Configured from console by console'
            )
        else:
            return VendorLogFormatter.sir(
                hostname, 'SYS',
                f'configuration changed from console'
            )


# グローバルインスタンス
vendor_log = VendorLogFormatter()


# ══════════════════════════════════════════
# Si-R メッセージエンジン（マニュアル準拠）
# Si-R G シリーズ メッセージ集 2026年6月版準拠
# ══════════════════════════════════════════
class SiRMessageEngine:
    """
    Si-R G シリーズ システムログメッセージ生成エンジン
    
    形式: <date> <host> <machine> : <message>
    例: 2024/01/01 00:00:01 Router-A Si-R G120 : [LINK] lan0 Link Up
    
    マニュアル「Si-R G シリーズ メッセージ集」2026年6月版準拠
    """

    MACHINE = 'Si-R G120'

    def _header(self, hostname: str) -> str:
        """ログヘッダ生成 <date> <host> <machine> :"""
        now = datetime.now()
        ts = now.strftime('%Y/%m/%d %H:%M:%S')
        return f'{ts} {hostname} {self.MACHINE} :'

    def _log(self, hostname: str, facility: str, message: str) -> str:
        return f'{self._header(hostname)} [{facility}] {message}'

    # ── 1.1 システムのメッセージ ──────────────────────────────────
    def sys_startup(self, hostname: str) -> str:
        """1.1.1 システム起動"""
        return self._log(hostname, 'SYSTEM', 'system start')

    def sys_restart(self, hostname: str, cause: str = 'operator') -> str:
        """システム再起動（saveコマンド等で発生）"""
        return self._log(hostname, 'SYSTEM', f'system restart ({cause})')

    def sys_config_changed(self, hostname: str) -> str:
        """構成定義変更（コンフィグモードで設定保存時）"""
        return self._log(hostname, 'SYSTEM', 'configuration changed')

    def sys_config_mode_enter(self, hostname: str, user: str = 'admin', via: str = 'console') -> str:
        """コンフィグモード移行（configure/conf コマンド）"""
        return self._log(hostname, 'SYSTEM', f'configuration mode entered by {user} ({via})')

    def sys_config_mode_exit(self, hostname: str, user: str = 'admin') -> str:
        """コンフィグモード終了（exit/end コマンド）"""
        return self._log(hostname, 'SYSTEM', f'configuration mode exited by {user}')

    def sys_save(self, hostname: str, user: str = 'admin') -> str:
        """saveコマンド実行"""
        return self._log(hostname, 'SYSTEM', f'configuration saved by {user}')

    # ── SSH/Telnet ログイン・ログアウト ─────────────────────────────
    def ssh_login_success(self, hostname: str, user: str = 'admin',
                          src_ip: str = '') -> str:
        """SSHログイン成功"""
        src = f' from {src_ip}' if src_ip else ''
        return self._log(hostname, 'SSH', f'login: user={user}{src}')

    def ssh_login_failed(self, hostname: str, user: str = 'admin',
                         src_ip: str = '') -> str:
        """SSHログイン失敗"""
        src = f' from {src_ip}' if src_ip else ''
        return self._log(hostname, 'SSH', f'login failed: user={user}{src}')

    def ssh_logout(self, hostname: str, user: str = 'admin') -> str:
        """SSHログアウト"""
        return self._log(hostname, 'SSH', f'logout: user={user}')

    def telnet_login_success(self, hostname: str, user: str = 'admin',
                              src_ip: str = '') -> str:
        """Telnetログイン成功"""
        src = f' from {src_ip}' if src_ip else ''
        return self._log(hostname, 'TELNET', f'login: user={user}{src}')

    def telnet_logout(self, hostname: str, user: str = 'admin') -> str:
        """Telnetログアウト"""
        return self._log(hostname, 'TELNET', f'logout: user={user}')

    # ── 1.2 構成定義 / Link UP/DOWN ──────────────────────────────
    def link_up(self, hostname: str, interface: str,
                speed: str = '1Gbps', duplex: str = 'full') -> str:
        """
        インタフェース Link UP
        1.2.3 ether duplex コマンド相当
        """
        return self._log(hostname, 'LINK',
                         f'{interface} Link Up ({speed} {duplex}-duplex)')

    def link_down(self, hostname: str, interface: str) -> str:
        """インタフェース Link DOWN"""
        return self._log(hostname, 'LINK', f'{interface} Link Down')

    def lan_ip_assigned(self, hostname: str, interface: str,
                        ip: str, prefix: int) -> str:
        """
        1.8.6 IPアドレスの割り当て
        LANインタフェースへのIPアドレス設定
        """
        return self._log(hostname, 'ROUTE',
                         f'ip address {ip}/{prefix} assigned to {interface}')

    def ip_duplicate(self, hostname: str, ip: str, interface: str,
                     mac: str = '') -> str:
        """
        1.2.25 / 1.8.7 アドレス重複 / IPアドレスの重複
        """
        mac_str = f' (MAC: {mac})' if mac else ''
        return self._log(hostname, 'ARP',
                         f'IP address {ip} conflict detected on {interface}{mac_str}')

    # ── 1.8 ルーティングマネージャ ──────────────────────────────────
    def route_add(self, hostname: str, network: str, prefix: int,
                  gateway: str, proto: str = 'static') -> str:
        """経路追加（スタティック/RIP/OSPF学習）"""
        return self._log(hostname, 'ROUTE',
                         f'add {network}/{prefix} via {gateway} [{proto}]')

    def route_delete(self, hostname: str, network: str, prefix: int,
                     gateway: str, proto: str = 'static') -> str:
        """経路削除"""
        return self._log(hostname, 'ROUTE',
                         f'delete {network}/{prefix} via {gateway} [{proto}]')

    def route_table_overflow(self, hostname: str, proto: str = 'static') -> str:
        """1.8.1 ルーティングテーブルオーバフロー"""
        return self._log(hostname, 'ROUTE',
                         f'routing table overflow ({proto}): cannot add more entries')

    # ── 1.10 RIP のメッセージ ────────────────────────────────────
    def rip_start(self, hostname: str, interface: str,
                  version: int = 2) -> str:
        """RIP 開始"""
        return self._log(hostname, 'RIP',
                         f'RIPv{version} started on {interface}')

    def rip_route_learned(self, hostname: str, network: str, prefix: int,
                          metric: int, gateway: str,
                          interface: str = 'lan0') -> str:
        """RIP 経路学習"""
        return self._log(hostname, 'RIP',
                         f'learned {network}/{prefix} metric={metric} '
                         f'via {gateway} on {interface}')

    def rip_route_deleted(self, hostname: str, network: str, prefix: int,
                          gateway: str) -> str:
        """RIP 経路削除（タイムアウト等）"""
        return self._log(hostname, 'RIP',
                         f'route {network}/{prefix} via {gateway} expired/deleted')

    def rip_route_changed(self, hostname: str, network: str, prefix: int,
                          old_metric: int, new_metric: int, gateway: str) -> str:
        """RIP 経路メトリック変更"""
        return self._log(hostname, 'RIP',
                         f'route {network}/{prefix} metric changed '
                         f'{old_metric} -> {new_metric} via {gateway}')

    def rip_packet_error(self, hostname: str, error_type: str,
                         src_ip: str = '') -> str:
        """
        1.10.1-1.10.8 RIPパケット異常各種
        error_type: 'version', 'src_port', 'src_addr', 'addr_family',
                    'route_dest', 'mask_len', 'metric', 'pkt_len'
        """
        msgs = {
            'version':    f'received packet with invalid version from {src_ip}',
            'src_port':   f'received packet from invalid source port (not UDP/520) from {src_ip}',
            'src_addr':   f'received packet from invalid source address {src_ip}',
            'addr_family':f'received packet with invalid address family from {src_ip}',
            'route_dest': f'received packet with invalid route destination from {src_ip}',
            'mask_len':   f'received packet with invalid mask length from {src_ip}',
            'metric':     f'received packet with invalid metric from {src_ip}',
            'pkt_len':    f'received packet with invalid length from {src_ip}',
        }
        return self._log(hostname, 'RIP',
                         msgs.get(error_type, f'packet error ({error_type}) from {src_ip}'))

    def rip_table_overflow(self, hostname: str) -> str:
        """1.10.9 RIP ルーティングテーブルオーバフロー"""
        return self._log(hostname, 'RIP',
                         'RIP routing table overflow: cannot add route')

    # ── 1.11 OSPF のメッセージ ───────────────────────────────────
    def ospf_start(self, hostname: str, router_id: str,
                   area: str = '0.0.0.0') -> str:
        """OSPF 開始"""
        return self._log(hostname, 'OSPF',
                         f'OSPF started: router-id={router_id} area={area}')

    def ospf_neighbor_up(self, hostname: str, neighbor_id: str,
                         interface: str, area: str = '0.0.0.0') -> str:
        """OSPFネイバー確立（Full状態）"""
        return self._log(hostname, 'OSPF',
                         f'neighbor {neighbor_id} on {interface} '
                         f'(area {area}) state: Full')

    def ospf_neighbor_down(self, hostname: str, neighbor_id: str,
                           interface: str, reason: str = 'dead-timer') -> str:
        """OSPFネイバーダウン"""
        return self._log(hostname, 'OSPF',
                         f'neighbor {neighbor_id} on {interface} '
                         f'is down ({reason})')

    def ospf_spf_calc(self, hostname: str, area: str = '0.0.0.0') -> str:
        """SPF計算実施"""
        return self._log(hostname, 'OSPF',
                         f'SPF calculation done for area {area}')

    def ospf_route_add(self, hostname: str, network: str, prefix: int,
                       gateway: str, cost: int) -> str:
        """OSPF経路追加"""
        return self._log(hostname, 'OSPF',
                         f'route add {network}/{prefix} via {gateway} cost={cost}')

    def ospf_route_delete(self, hostname: str, network: str, prefix: int) -> str:
        """OSPF経路削除"""
        return self._log(hostname, 'OSPF',
                         f'route delete {network}/{prefix}')

    def ospf_area_id_mismatch(self, hostname: str, neighbor: str,
                               local_area: str, remote_area: str,
                               interface: str) -> str:
        """1.11.1 エリアID不一致"""
        return self._log(hostname, 'OSPF',
                         f'area-id mismatch on {interface} with {neighbor}: '
                         f'local={local_area} remote={remote_area}')

    def ospf_hello_mismatch(self, hostname: str, neighbor: str,
                             local: int, remote: int,
                             interface: str) -> str:
        """1.11.6 Hello パケット送信間隔の不一致"""
        return self._log(hostname, 'OSPF',
                         f'hello-interval mismatch on {interface} with {neighbor}: '
                         f'local={local}s remote={remote}s')

    def ospf_dead_mismatch(self, hostname: str, neighbor: str,
                            local: int, remote: int,
                            interface: str) -> str:
        """1.11.7 隣接ルータ停止確認間隔の不一致"""
        return self._log(hostname, 'OSPF',
                         f'dead-interval mismatch on {interface} with {neighbor}: '
                         f'local={local}s remote={remote}s')

    def ospf_netmask_mismatch(self, hostname: str, neighbor: str,
                               interface: str) -> str:
        """1.11.5 ネットワークマスク長不一致"""
        return self._log(hostname, 'OSPF',
                         f'netmask mismatch on {interface} with {neighbor}: '
                         f'ignoring Hello packet')

    def ospf_router_id_dup(self, hostname: str, dup_id: str,
                            interface: str) -> str:
        """1.11.10 ルータIDの重複"""
        return self._log(hostname, 'OSPF',
                         f'router-id {dup_id} duplicate detected on {interface}')

    def ospf_lsa_overflow(self, hostname: str, count: int,
                           max_count: int) -> str:
        """1.11.9 LSA最大数オーバ"""
        return self._log(hostname, 'OSPF',
                         f'LSA count {count} exceeds limit {max_count}: '
                         f'cannot add more LSAs')

    # ── VRRP のメッセージ ────────────────────────────────────────
    def vrrp_master(self, hostname: str, vrid: int, vip: str,
                    priority: int, interface: str = 'lan0') -> str:
        """VRRP Master 遷移"""
        return self._log(hostname, 'VRRP',
                         f'VRID {vrid} on {interface}: '
                         f'state changed to Master (priority={priority}, VIP={vip})')

    def vrrp_backup(self, hostname: str, vrid: int, vip: str,
                    priority: int, interface: str = 'lan0') -> str:
        """VRRP Backup 遷移"""
        return self._log(hostname, 'VRRP',
                         f'VRID {vrid} on {interface}: '
                         f'state changed to Backup (priority={priority}, VIP={vip})')

    def vrrp_init(self, hostname: str, vrid: int,
                  interface: str = 'lan0') -> str:
        """VRRP Initialize 遷移（起動時）"""
        return self._log(hostname, 'VRRP',
                         f'VRID {vrid} on {interface}: Initialized (Master or Backup pending)')

    def vrrp_master_down(self, hostname: str, vrid: int,
                          interface: str = 'lan0') -> str:
        """VRRP Masterダウン検出（Backup→Master昇格契機）"""
        return self._log(hostname, 'VRRP',
                         f'VRID {vrid} on {interface}: '
                         f'Master down detected, transitioning to Master')

    def vrrp_vrid_dup(self, hostname: str, vrid: int,
                       interface: str = 'lan0') -> str:
        """1.2.40 VRID重複設定"""
        return self._log(hostname, 'VRRP',
                         f'VRID {vrid} on {interface}: '
                         f'VRID conflict detected')

    # ── BGP のメッセージ ─────────────────────────────────────────
    def bgp_start(self, hostname: str, local_as: int,
                  router_id: str) -> str:
        """BGP 開始"""
        return self._log(hostname, 'BGP',
                         f'BGP started: AS={local_as} router-id={router_id}')

    def bgp_peer_up(self, hostname: str, peer_ip: str,
                    remote_as: int) -> str:
        """BGPピアEstablished"""
        return self._log(hostname, 'BGP',
                         f'peer {peer_ip} (AS{remote_as}) state: Established')

    def bgp_peer_down(self, hostname: str, peer_ip: str,
                      remote_as: int, reason: str = 'hold-timer expired') -> str:
        """BGPピアダウン"""
        return self._log(hostname, 'BGP',
                         f'peer {peer_ip} (AS{remote_as}) is down ({reason})')

    def bgp_route_received(self, hostname: str, network: str, prefix: int,
                            peer: str) -> str:
        """BGP経路受信"""
        return self._log(hostname, 'BGP',
                         f'received route {network}/{prefix} from {peer}')

    # ── STPのメッセージ ──────────────────────────────────────────
    def stp_topology_change(self, hostname: str,
                             interface: str) -> str:
        """STP トポロジー変更"""
        return self._log(hostname, 'STP',
                         f'topology change detected on {interface}')

    def stp_root_selected(self, hostname: str, root_id: str,
                           interface: str) -> str:
        """STP ルートブリッジ選出"""
        return self._log(hostname, 'STP',
                         f'root bridge selected: {root_id} via {interface}')

    def stp_port_state(self, hostname: str, interface: str,
                        state: str) -> str:
        """STP ポート状態変化（Blocking→Forwarding等）"""
        return self._log(hostname, 'STP',
                         f'{interface}: STP state changed to {state}')

    def stp_def_mismatch(self, hostname: str, param: str) -> str:
        """1.2.50 STP定義矛盾"""
        return self._log(hostname, 'STP',
                         f'configuration conflict: {param}')

    # ── ログイン/コンソール ─────────────────────────────────────
    def login_success(self, hostname: str, user: str = 'admin',
                      via: str = 'console') -> str:
        """ログイン成功"""
        return self._log(hostname, 'LOGIN',
                         f'login successful: user={user} ({via})')

    def login_failed(self, hostname: str, user: str = 'admin',
                     via: str = 'console') -> str:
        """ログイン失敗"""
        return self._log(hostname, 'LOGIN',
                         f'login failed: user={user} ({via})')

    def logout(self, hostname: str, user: str = 'admin',
               via: str = 'console') -> str:
        """ログアウト"""
        return self._log(hostname, 'LOGIN',
                         f'logout: user={user} ({via})')

    # ── SNMP トラップ ────────────────────────────────────────────
    def snmp_auth_fail(self, hostname: str, src_ip: str) -> str:
        """SNMP認証失敗"""
        return self._log(hostname, 'SNMP',
                         f'authentication failure from {src_ip}')

    # ── show logging syslog 形式 ─────────────────────────────────
    def format_show_logging(self, hostname: str, logs: list) -> str:
        """
        show logging syslog の出力形式
        logs: [(timestamp, facility, message), ...]
        """
        if not logs:
            return f'{hostname} Si-R G120 : [SYSTEM] no log entries'
        lines = [
            f'Current time : {datetime.now().strftime("%Y/%m/%d %H:%M:%S")}',
            f'',
            f'Syslog:',
        ]
        for ts, facility, message in logs[-50:]:  # 最新50件
            lines.append(f'{ts} {hostname} {self.MACHINE} : [{facility}] {message}')
        return '\n'.join(lines)


sir_msg = SiRMessageEngine()


# ══════════════════════════════════════════
# Cisco IOS/IOS-XE メッセージエンジン
# Catalyst 9300 / ISR 4321 / NX-OS準拠
# ══════════════════════════════════════════
class CiscoMessageEngine:
    """
    Cisco IOS/IOS-XE システムログメッセージ生成エンジン
    
    形式: *Mmm DD HH:MM:SS.mmm: %FACILITY-SEVERITY-MNEMONIC: description
    例: *Jun 20 12:34:56.789: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to up
    
    Cisco IOS System Message Guide / Catalyst 9300 Configuration Guide 準拠
    
    Severity levels:
      0=Emergency, 1=Alert, 2=Critical, 3=Error, 4=Warning,
      5=Notification, 6=Informational, 7=Debug
    """

    def _ts(self) -> str:
        """Cisco IOS タイムスタンプ形式: *Mmm DD HH:MM:SS.mmm"""
        now = datetime.now()
        return f'*{now.strftime("%b %d %H:%M:%S")}.{now.microsecond//1000:03d}'

    def _fmt(self, facility: str, severity: int, mnemonic: str,
              description: str) -> str:
        """標準Cisco IOSログ形式"""
        return f'{self._ts()}: %{facility}-{severity}-{mnemonic}: {description}'

    # ── %SYS ─────────────────────────────────────────────────────
    def sys_config_i(self, via: str = 'console', src: str = 'console') -> str:
        """
        %SYS-5-CONFIG_I: Configured from console
        conf t 入力後・exit後に出力
        """
        return self._fmt('SYS', 5, 'CONFIG_I',
                         f'Configured from console by {src} ({via})')

    def sys_startup(self, version: str = '17.09.01') -> str:
        """%SYS-5-RESTART: System restarted"""
        return self._fmt('SYS', 5, 'RESTART',
                         f'System restarted --\nCisco IOS Software, Version {version}')

    def sys_reload(self, reason: str = 'Reload command') -> str:
        """%SYS-5-RELOAD: Reload requested"""
        return self._fmt('SYS', 5, 'RELOAD',
                         f'Reload requested by console. Reload Reason: {reason}.')

    def sys_hostname_changed(self, old: str, new: str) -> str:
        """ホスト名変更（内部通知）"""
        return self._fmt('SYS', 6, 'HOSTNAMECHG',
                         f'Hostname changed from "{old}" to "{new}"')

    # ── %LINK / %LINEPROTO ───────────────────────────────────────
    def link_updown(self, interface: str, state: str) -> str:
        """
        %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to up
        ポート接続時・切断時
        """
        return self._fmt('LINK', 3, 'UPDOWN',
                         f'Interface {interface}, changed state to {state}')

    def lineproto_updown(self, interface: str, state: str) -> str:
        """
        %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/1, changed state to up
        L2プロトコル確立時
        """
        return self._fmt('LINEPROTO', 5, 'UPDOWN',
                         f'Line protocol on Interface {interface}, changed state to {state}')

    def link_changed(self, interface: str, state: str) -> str:
        """LINK + LINEPROTO を一括生成"""
        return (self.link_updown(interface, state) + '\n' +
                self.lineproto_updown(interface, state))

    # ── %OSPF ────────────────────────────────────────────────────
    def ospf_adjchg(self, process: int, neighbor: str, interface: str,
                    from_state: str, to_state: str, reason: str = '') -> str:
        """
        %OSPF-5-ADJCHG: Process 1, Nbr 10.0.0.2 on GigabitEthernet1/0/1
                        from LOADING to FULL, Loading Done
        """
        reason_str = f', {reason}' if reason else ''
        return self._fmt('OSPF', 5, 'ADJCHG',
                         f'Process {process}, Nbr {neighbor} on {interface} '
                         f'from {from_state} to {to_state}{reason_str}')

    def ospf_dr_elected(self, process: int, interface: str,
                         dr: str, bdr: str) -> str:
        """DR/BDR選出"""
        return self._fmt('OSPF', 6, 'DRCHG',
                         f'Process {process}: DR {dr}, BDR {bdr} on {interface}')

    def ospf_spf(self, process: int, area: str = '0') -> str:
        """SPF計算完了"""
        return self._fmt('OSPF', 6, 'LOGADJ',
                         f'Process {process}: SPF computation completed for Area {area}')

    def ospf_started(self, process: int, router_id: str, area: str) -> str:
        """OSPF プロセス起動"""
        return self._fmt('OSPF', 5, 'ADJCHG',
                         f'Process {process} started. '
                         f'Router ID {router_id}, Area {area}')

    # ── %RIP ─────────────────────────────────────────────────────
    def rip_update_sent(self, interface: str, dest: str = '224.0.0.9') -> str:
        """RIP Update送信"""
        return self._fmt('RIPV2', 5, 'RDBUPDATE',
                         f'Sending update to {dest} via {interface}')

    def rip_route_learned(self, network: str, prefix: int,
                           metric: int, gateway: str) -> str:
        """RIP経路学習"""
        return self._fmt('RIPV2', 6, 'RDBUPDATE',
                         f'Updating full routing table - '
                         f'{network}/{prefix} [120/{metric}] via {gateway}')

    def rip_neighbor_up(self, neighbor: str, interface: str) -> str:
        """RIPネイバー確立"""
        return self._fmt('RIPV2', 5, 'RDBUPDATE',
                         f'Neighbor {neighbor} on {interface} is up')

    # ── %BGP ─────────────────────────────────────────────────────
    def bgp_adjchange(self, neighbor: str, state: str,
                       remote_as: int = 0) -> str:
        """
        %BGP-5-ADJCHANGE: neighbor 10.0.0.2 Up
        """
        asn_str = f' AS{remote_as}' if remote_as else ''
        return self._fmt('BGP', 5, 'ADJCHANGE',
                         f'neighbor {neighbor}{asn_str} {state}')

    def bgp_started(self, local_as: int, router_id: str) -> str:
        """BGP プロセス起動"""
        return self._fmt('BGP', 5, 'ADJCHANGE',
                         f'Process {local_as} started. Router ID {router_id}')

    def bgp_route_received(self, network: str, prefix: int, peer: str) -> str:
        """BGP経路受信"""
        return self._fmt('BGP', 6, 'TABLECHG',
                         f'Neighbor {peer} UPDATE received: {network}/{prefix}')

    # ── %VRRP ────────────────────────────────────────────────────
    def vrrp_statechange(self, group: int, interface: str,
                          from_state: str, to_state: str) -> str:
        """
        %VRRP-6-STATECHANGE: GigabitEthernet1/0/1 Grp 1 state Backup -> Master
        """
        return self._fmt('VRRP', 6, 'STATECHANGE',
                         f'{interface} Grp {group} state {from_state} -> {to_state}')

    # ── %HSRP ────────────────────────────────────────────────────
    def hsrp_statechange(self, group: int, interface: str,
                          from_state: str, to_state: str) -> str:
        """
        %HSRP-5-STATECHANGE: GigabitEthernet1/0/1 Grp 1 state Standby -> Active
        """
        return self._fmt('HSRP', 5, 'STATECHANGE',
                         f'{interface} Grp {group} state {from_state} -> {to_state}')

    # ── %SPANTREE ─────────────────────────────────────────────────
    def stp_portdpvstp(self, interface: str, vlan: int,
                        from_state: str, to_state: str) -> str:
        """
        %SPANTREE-6-PORTDPVSTP: Port GigabitEthernet1/0/1 instance 0
                                  moving from Listening to Forwarding
        """
        return self._fmt('SPANTREE', 6, 'PORTDPVSTP',
                         f'Port {interface} instance {vlan} '
                         f'moving from {from_state} to {to_state}')

    def stp_rootchange(self, vlan: int, new_root: str) -> str:
        """
        %SPANTREE-5-ROOTCHANGE: Root Changed for vlan 1: new root is 4096.xx:xx:xx
        """
        return self._fmt('SPANTREE', 5, 'ROOTCHANGE',
                         f'Root Changed for vlan {vlan}: new root is {new_root}')

    def stp_topology_change(self, vlan: int, src: str) -> str:
        """
        %SPANTREE-6-TOPOTRAP: Topology change detected
        """
        return self._fmt('SPANTREE', 6, 'TOPOTRAP',
                         f'Topology change detected on {src}. '
                         f'Notification sent to vlan {vlan}')

    def stp_portfast(self, interface: str) -> str:
        """
        %SPANTREE-5-PORTFAST: Interface GigabitEthernet1/0/1 edge port type
                               was enabled because spanning-tree portfast configured
        """
        return self._fmt('SPANTREE', 5, 'PORTFAST',
                         f'Interface {interface} edge port type was enabled '
                         f'because the interface is spanning-tree portfast enabled')

    def stp_bpduguard(self, interface: str) -> str:
        """
        %SPANTREE-2-BLOCK_BPDUGUARD: Received BPDU on port with BPDU Guard enabled
        """
        return (
            self._fmt('SPANTREE', 2, 'BLOCK_BPDUGUARD',
                      f'Received BPDU on port {interface} with BPDU Guard enabled. '
                      f'Disabling port.') + '\n' +
            self._fmt('PM', 4, 'ERR_DISABLE',
                      f'bpduguard error detected on {interface}, '
                      f'putting {interface} in err-disable state')
        )

    def stp_root_guard(self, interface: str) -> str:
        """
        %SPANTREE-2-ROOTGUARD_CONFIG_CHANGE: Root guard blocking port
        """
        return self._fmt('SPANTREE', 2, 'ROOTGUARD_CONFIG_CHANGE',
                         f'Root guard blocking port {interface}. Inconsistent port.')

    # ── %EC (EtherChannel/LACP) ──────────────────────────────────
    def lacp_bundle(self, channel_id: int, members: list) -> str:
        """
        %EC-5-BUNDLE: Interface GigabitEthernet1/0/1 joined port-channel1
        """
        lines = []
        for m in members:
            lines.append(self._fmt('EC', 5, 'BUNDLE',
                                   f'Interface {m} joined port-channel{channel_id}'))
        lines.append(self._fmt('LINEPROTO', 5, 'UPDOWN',
                               f'Line protocol on Interface Port-channel{channel_id}, '
                               f'changed state to up'))
        return '\n'.join(lines)

    def lacp_unbundle(self, channel_id: int, interface: str,
                       reason: str = 'Partner timeout') -> str:
        """%EC-5-UNBUNDLE: Interface removed from port-channel"""
        return self._fmt('EC', 5, 'UNBUNDLE',
                         f'Interface {interface} left port-channel{channel_id} ({reason})')

    # ── %SSH / %LINE ─────────────────────────────────────────────
    def ssh_open(self, user: str = 'admin', src_ip: str = '192.168.1.100',
                 version: str = '2.0') -> str:
        """
        %SSH-5-SSH2_SESSION: SSH2 Session request from 192.168.1.100 (TTY=1)
                             user 'admin' authentication accepted
        """
        return self._fmt('SSH', 5, 'SSH2_SESSION',
                         f'SSH2 Session request from {src_ip} (TTY=1) '
                         f'user \'{user}\' authentication accepted')

    def ssh_close(self, user: str = 'admin', src_ip: str = '192.168.1.100') -> str:
        """SSH セッション終了"""
        return self._fmt('SSH', 5, 'SSH2_CLOSE',
                         f'SSH2 Session from {src_ip} by user \'{user}\' disconnected')

    def ssh_auth_failed(self, user: str, src_ip: str) -> str:
        """SSH 認証失敗"""
        return self._fmt('SSH', 3, 'SSH2_UNEXPECTED_MSG',
                         f'SSH2 Authentication failed for user \'{user}\' from {src_ip}')

    def telnet_open(self, user: str = 'admin',
                    src_ip: str = '192.168.1.100') -> str:
        """Telnet 接続"""
        return self._fmt('LINE', 5, 'CONSOLE',
                         f'User \'{user}\' connected via Telnet from {src_ip}')

    # ── %LOGIN / %AAA ─────────────────────────────────────────────
    def login_success(self, user: str = 'admin', via: str = 'console') -> str:
        """ログイン成功"""
        return self._fmt('LOGIN', 5, 'LOGIN_SUCCESS',
                         f'Login Success [user: {user}] [Source: {via}] [localport: 0]')

    def login_failed(self, user: str = 'admin', via: str = 'console') -> str:
        """ログイン失敗"""
        return self._fmt('LOGIN', 4, 'LOGIN_FAILED',
                         f'Login Failed [user: {user}] [Source: {via}] [localport: 0]')

    # ── %IP ───────────────────────────────────────────────────────
    def ip_dup_address(self, interface: str, ip: str, mac: str) -> str:
        """
        %IP-4-DUPADDR: Duplicate address 192.168.1.1 on GigabitEthernet1/0/1,
                       sourced by aabb.cc00.0100
        """
        return self._fmt('IP', 4, 'DUPADDR',
                         f'Duplicate address {ip} on {interface}, sourced by {mac}')

    # ── %PM (Port Manager) ────────────────────────────────────────
    def pm_err_disable(self, interface: str, reason: str) -> str:
        """
        %PM-4-ERR_DISABLE: bpduguard error detected on GigabitEthernet1/0/1
        """
        return self._fmt('PM', 4, 'ERR_DISABLE',
                         f'{reason} error detected on {interface}, '
                         f'putting {interface} in err-disable state')

    def pm_err_recover(self, interface: str) -> str:
        """err-disable 回復"""
        return self._fmt('PM', 6, 'ERR_RECOVER',
                         f'Attempting to recover from {interface} err-disable state')

    # ── show logging バッファ形式 ─────────────────────────────────
    def format_show_logging(self, logs: list, hostname: str = '') -> str:
        """
        show logging の出力形式（Catalyst 9300準拠）
        """
        lines = [
            'Syslog logging: enabled (0 messages dropped, 0 messages rate-limited,',
            '                0 flushes, 0 overruns, xml disabled, filtering disabled)',
            '',
            'No Active Message Discriminator.',
            '',
            'No Inactive Message Discriminator.',
            '',
            '',
            '    Console logging: level debugging, 0 messages logged, xml disabled,',
            '                     filtering disabled',
            '    Monitor logging: level debugging, 0 messages logged, xml disabled,',
            '                     filtering disabled',
            '    Buffer logging:  level debugging, messages logged, xml disabled,',
            '                     filtering disabled',
            '    Logging Exception size (4096 bytes)',
            f'    Count and timestamp logging messages: disabled',
            '    Persistent logging: disabled',
            '',
            f'Log Buffer (4096 bytes):',
        ]
        if not logs:
            lines.append('(No log messages)')
        else:
            for log in logs[-50:]:  # 最新50件
                msg = log.get('message', '') if isinstance(log, dict) else str(log)
                lines.append(msg)
        return '\n'.join(lines)


cisco_msg = CiscoMessageEngine()


# ══════════════════════════════════════════
# NX-OS メッセージエンジン（Nexus 9000準拠）
# Cisco NX-OS 10.2(x)/10.6(x) System Messages Reference準拠
#
# 形式: YYYY MMM DD HH:MM:SS.mmm hostname %FACILITY-SEV-MNEMONIC: description
# 例:   2026 Jun 20 12:34:56.789 Nexus-A %ETH_PORT-5-IF_UP: Interface Ethernet1/1 is up
# ══════════════════════════════════════════
class NxosMessageEngine:

    def _ts(self, hostname: str) -> str:
        """NX-OS タイムスタンプ: YYYY Mmm DD HH:MM:SS.mmm hostname"""
        now = datetime.now()
        return (f'{now.year} {now.strftime("%b %d %H:%M:%S")}'
                f'.{now.microsecond//1000:03d} {hostname}')

    def _fmt(self, hostname: str, facility: str, severity: int,
              mnemonic: str, description: str) -> str:
        return f'{self._ts(hostname)} %{facility}-{severity}-{mnemonic}: {description}'

    # ── ETH_PORT / LINEPROTO ─────────────────────────────────────
    def if_up(self, hostname: str, interface: str, reason: str = '') -> str:
        """
        %ETH_PORT-5-IF_UP: Interface Ethernet1/1 is up in mode access
        ポートが接続状態になった
        """
        mode_str = f' in mode {reason}' if reason else ''
        return self._fmt(hostname, 'ETH_PORT', 5, 'IF_UP',
                         f'Interface {interface} is up{mode_str}')

    def if_down(self, hostname: str, interface: str, reason: str = 'Link not connected') -> str:
        """
        %ETH_PORT-5-IF_DOWN_LINK_FAILURE: Interface Ethernet1/1 is down (Link not connected)
        """
        return self._fmt(hostname, 'ETH_PORT', 5, 'IF_DOWN_LINK_FAILURE',
                         f'Interface {interface} is down ({reason})')

    def lineproto_up(self, hostname: str, interface: str) -> str:
        """%LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet1/1, changed state to up"""
        return self._fmt(hostname, 'LINEPROTO', 5, 'UPDOWN',
                         f'Line protocol on Interface {interface}, changed state to up')

    def lineproto_down(self, hostname: str, interface: str) -> str:
        """%LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet1/1, changed state to down"""
        return self._fmt(hostname, 'LINEPROTO', 5, 'UPDOWN',
                         f'Line protocol on Interface {interface}, changed state to down')

    def if_updown(self, hostname: str, interface: str, state: str) -> str:
        """IF UP/DOWN を一括生成"""
        if state == 'up':
            return (self.if_up(hostname, interface) + '\n' +
                    self.lineproto_up(hostname, interface))
        else:
            return (self.if_down(hostname, interface) + '\n' +
                    self.lineproto_down(hostname, interface))

    # ── SYS / SYSMGR ─────────────────────────────────────────────
    def config_saved(self, hostname: str, user: str = 'admin') -> str:
        """%SYSMGR-5-MGMT_CONF: User admin modified the running-config"""
        return self._fmt(hostname, 'SYSMGR', 5, 'MGMT_CONF',
                         f'User {user} modified the running-config')

    def sys_reload(self, hostname: str, reason: str = 'Reload command') -> str:
        """%SYSMGR-3-BASIC_SYSCALL_FAILED: System reloading"""
        return self._fmt(hostname, 'SYSMGR', 5, 'BASIC_SYSCALL_FAILED',
                         f'System reload requested: {reason}')

    def hostname_changed(self, hostname: str, old: str, new: str) -> str:
        return self._fmt(hostname, 'SYSMGR', 6, 'HOSTNAME_CHANGE',
                         f'Hostname changed from {old} to {new}')

    # ── VPC ──────────────────────────────────────────────────────
    def vpc_status_change(self, hostname: str, domain_id: int,
                           role: str, reason: str = '') -> str:
        """
        %VPC-6-VPC_STATUS_CHANGE: vPC domain 1: role changed to PRIMARY
        """
        r = f', {reason}' if reason else ''
        return self._fmt(hostname, 'VPC', 6, 'VPC_STATUS_CHANGE',
                         f'vPC domain {domain_id}: role changed to {role.upper()}{r}')

    def vpc_peer_link_up(self, hostname: str, domain_id: int, interface: str) -> str:
        """%VPC-6-PEER_LINK_CHANGE: vPC peer-link is up"""
        return self._fmt(hostname, 'VPC', 6, 'PEER_LINK_CHANGE',
                         f'vPC domain {domain_id}: Peer-link {interface} is up')

    def vpc_peer_link_down(self, hostname: str, domain_id: int, interface: str) -> str:
        """%VPC-6-PEER_LINK_CHANGE: vPC peer-link is down"""
        return self._fmt(hostname, 'VPC', 6, 'PEER_LINK_CHANGE',
                         f'vPC domain {domain_id}: Peer-link {interface} is down')

    def vpc_keepalive_alive(self, hostname: str, domain_id: int,
                             peer_ip: str) -> str:
        """%VPC-6-PEER_KEEPALIVE_RECV_SUCCESS: Received peer keepalive"""
        return self._fmt(hostname, 'VPC', 6, 'PEER_KEEPALIVE_RECV_SUCCESS',
                         f'vPC domain {domain_id}: Peer keepalive received from {peer_ip}')

    def vpc_keepalive_timeout(self, hostname: str, domain_id: int) -> str:
        """%VPC-3-PEER_KEEPALIVE_RECV_FAIL: Peer keepalive is not received"""
        return self._fmt(hostname, 'VPC', 3, 'PEER_KEEPALIVE_RECV_FAIL',
                         f'vPC domain {domain_id}: Peer keepalive is not received')

    def vpc_member_up(self, hostname: str, vpc_id: int, interface: str) -> str:
        """%VPC-6-VPC_PORT_STATE_CHANGE: vPC port is up"""
        return self._fmt(hostname, 'VPC', 6, 'VPC_PORT_STATE_CHANGE',
                         f'vPC {vpc_id} on {interface} is up')

    def vpc_member_suspended(self, hostname: str, vpc_id: int,
                              interface: str, reason: str = 'peer-link down') -> str:
        """%VPC-2-VPC_SUSP_ALL_VPC: vPC suspended due to peer-link failure"""
        return self._fmt(hostname, 'VPC', 2, 'VPC_SUSP_ALL_VPC',
                         f'vPC {vpc_id} on {interface} suspended ({reason})')

    # ── OSPF ─────────────────────────────────────────────────────
    def ospf_adjchg(self, hostname: str, process: int, neighbor: str,
                     interface: str, from_state: str, to_state: str) -> str:
        """
        %OSPF-5-ADJCHG: Process 1, Nbr 10.0.0.2 on Ethernet1/1
                         from LOADING to FULL, Loading Done
        """
        return self._fmt(hostname, 'OSPF', 5, 'ADJCHG',
                         f'Process {process}, Nbr {neighbor} on {interface} '
                         f'from {from_state} to {to_state}')

    def ospf_dr_elected(self, hostname: str, process: int,
                         interface: str, dr: str, bdr: str) -> str:
        """%OSPF-6-DRCHG: Process 1: DR elected"""
        return self._fmt(hostname, 'OSPF', 6, 'DRCHG',
                         f'Process {process}: DR {dr}, BDR {bdr} elected on {interface}')

    def ospf_started(self, hostname: str, process: int, router_id: str) -> str:
        """%OSPF-5-ADJCHG: Process 1 started"""
        return self._fmt(hostname, 'OSPF', 5, 'ADJCHG',
                         f'Process {process} started, Router-ID {router_id}')

    # ── BGP ──────────────────────────────────────────────────────
    def bgp_adjchange(self, hostname: str, neighbor: str, remote_as: int,
                       state: str) -> str:
        """
        %BGP-5-ADJCHANGE: bgp-65001 [default] neighbor 10.0.0.2 (AS 65002) Up
        """
        local_as = 0
        return self._fmt(hostname, 'BGP', 5, 'ADJCHANGE',
                         f'neighbor {neighbor} (AS {remote_as}) {state}')

    def bgp_started(self, hostname: str, local_as: int) -> str:
        """%BGP-5-BGP_PROTOCOL_STARTED: bgp-65001 started"""
        return self._fmt(hostname, 'BGP', 5, 'BGP_PROTOCOL_STARTED',
                         f'bgp-{local_as} started')

    # ── LACP / ETH_PORT_CHANNEL ──────────────────────────────────
    def lacp_bundle(self, hostname: str, channel_id: int, members: list) -> str:
        """
        %ETH_PORT_CHANNEL-5-FOP_CHANGED: port-channel1: first operational port is Ethernet1/1
        """
        first = members[0] if members else 'Ethernet1/1'
        lines = [
            self._fmt(hostname, 'ETH_PORT_CHANNEL', 5, 'FOP_CHANGED',
                      f'port-channel{channel_id}: first operational port is now {first}')
        ]
        for m in members:
            lines.append(self._fmt(hostname, 'ETH_PORT_CHANNEL', 5, 'CHANNEL_COMPAT',
                                   f'{m} added to port-channel{channel_id}'))
        return '\n'.join(lines)

    # ── STP ──────────────────────────────────────────────────────
    def stp_port_state(self, hostname: str, vlan: int, interface: str,
                        from_state: str, to_state: str) -> str:
        """
        %STP-6-PORT_STATE: vlan 1 port Ethernet1/1: BLK -> FWD
        """
        return self._fmt(hostname, 'STP', 6, 'PORT_STATE',
                         f'vlan {vlan} port {interface}: {from_state} -> {to_state}')

    def stp_root_change(self, hostname: str, vlan: int, new_root: str) -> str:
        """%STP-5-TOPO_CHANGE: Root bridge changed for vlan"""
        return self._fmt(hostname, 'STP', 5, 'TOPO_CHANGE',
                         f'vlan {vlan}: Root bridge changed to {new_root}')

    def stp_tc(self, hostname: str, vlan: int, interface: str) -> str:
        """%STP-6-TOPOLOGY_CHANGE: topology change detected"""
        return self._fmt(hostname, 'STP', 6, 'TOPOLOGY_CHANGE',
                         f'vlan {vlan}: topology change detected on {interface}')

    # ── VRRP ─────────────────────────────────────────────────────
    def vrrp_state(self, hostname: str, group: int, interface: str,
                    from_state: str, to_state: str) -> str:
        """%VRRP-6-STATECHANGE: Ethernet1/1 Grp 1 state Backup -> Master"""
        return self._fmt(hostname, 'VRRP', 6, 'STATECHANGE',
                         f'{interface} Grp {group} state {from_state} -> {to_state}')

    # ── SSH / AAA ─────────────────────────────────────────────────
    def ssh_login(self, hostname: str, user: str = 'admin',
                  src_ip: str = '192.168.1.1') -> str:
        """%DCOS_SSHD-6-AUTHENTICATION_SUCCESS: Authentication succeeded for admin"""
        return self._fmt(hostname, 'DCOS_SSHD', 6, 'AUTHENTICATION_SUCCESS',
                         f'Authentication succeeded for user {user} from {src_ip}')

    def ssh_login_failed(self, hostname: str, user: str = 'admin',
                          src_ip: str = '192.168.1.1') -> str:
        """%DCOS_SSHD-3-AUTHENTICATION_FAILED: Authentication failed"""
        return self._fmt(hostname, 'DCOS_SSHD', 3, 'AUTHENTICATION_FAILED',
                         f'Authentication failed for user {user} from {src_ip}')

    def ssh_logout(self, hostname: str, user: str = 'admin') -> str:
        """%DCOS_SSHD-6-DISCONNECT: User disconnected"""
        return self._fmt(hostname, 'DCOS_SSHD', 6, 'DISCONNECT',
                         f'User {user} disconnected')

    # ── show logging 形式 ─────────────────────────────────────────
    def format_show_logging(self, hostname: str, logs: list) -> str:
        """
        show logging の出力（Nexus 9000 NX-OS 10.2準拠）
        """
        lines = [
            f'Logging console:                 5 (Notifications)',
            f'Logging monitor:                 5 (Notifications)',
            f'Logging linecard:                5 (Notifications)',
            f'Logging timestamp:               Microseconds',
            f'Logging server:                  enabled',
            f'    192.168.1.100               6 (Informational), link : up',
            f'',
            f'Log file (/log/messages):',
        ]
        if not logs:
            lines.append('(no log messages)')
        else:
            for log in logs[-50:]:
                msg = log.get('message', '') if isinstance(log, dict) else str(log)
                lines.append(msg)
        return '\n'.join(lines)


nxos_msg = NxosMessageEngine()


# ══════════════════════════════════════════
# APRESIA ApresiaLight メッセージエンジン
# ApresiaLightGM200 準拠
#
# 形式（装置内）: YYYY/MM/DD HH:MM:SS: LEVEL: MESSAGE
# syslogレベル: informational / warning / critical
# ══════════════════════════════════════════
class ApresiaMessageEngine:
    """
    APRESIA ApresiaLight GM/FM シリーズ システムログ
    
    装置内ログ形式:
      2026/06/20 12:34:56: informational: Port 1/0/1 Link Up (100M full-duplex)
      2026/06/20 12:34:57: warning:       Authentication failure from 192.168.1.100
      2026/06/20 12:34:58: critical:      Port 1/0/1 STP BPDU Guard activated
    
    syslogサーバ転送形式（RFC3164）:
      <facility.severity> hostname: MESSAGE
    """

    def _ts(self) -> str:
        return datetime.now().strftime('%Y/%m/%d %H:%M:%S')

    def _log(self, level: str, message: str) -> str:
        """装置内ログ形式"""
        return f'{self._ts()}: {level}: {message}'

    def _info(self, message: str) -> str:
        return self._log('informational', message)

    def _warn(self, message: str) -> str:
        return self._log('warning', message)

    def _crit(self, message: str) -> str:
        return self._log('critical', message)

    # ── ポート Link UP/DOWN ───────────────────────────────────────
    def port_up(self, port: str, speed: str = '1G', duplex: str = 'full') -> str:
        """Port Link Up"""
        return self._info(f'Port {port} Link Up ({speed} {duplex}-duplex)')

    def port_down(self, port: str) -> str:
        """Port Link Down"""
        return self._info(f'Port {port} Link Down')

    # ── システム ─────────────────────────────────────────────────
    def sys_startup(self) -> str:
        """システム起動"""
        return self._info('System started')

    def sys_config_saved(self, user: str = 'admin') -> str:
        """設定保存"""
        return self._info(f'Configuration saved by {user}')

    def sys_config_changed(self, user: str = 'admin') -> str:
        """設定変更"""
        return self._info(f'Configuration changed by {user}')

    # ── ログイン ─────────────────────────────────────────────────
    def login_success(self, user: str = 'admin',
                      src: str = 'console', via: str = 'console') -> str:
        """ログイン成功"""
        return self._info(f'Login: user={user} from={src} via={via}')

    def login_failed(self, user: str = 'admin', src: str = '') -> str:
        """ログイン失敗"""
        src_str = f' from {src}' if src else ''
        return self._warn(f'Authentication failure: user={user}{src_str}')

    def logout(self, user: str = 'admin') -> str:
        """ログアウト"""
        return self._info(f'Logout: user={user}')

    # ── VLAN ─────────────────────────────────────────────────────
    def vlan_created(self, vlan_id: int, name: str = '') -> str:
        name_str = f' name={name}' if name else ''
        return self._info(f'VLAN {vlan_id} created{name_str}')

    def vlan_deleted(self, vlan_id: int) -> str:
        return self._info(f'VLAN {vlan_id} deleted')

    def vlan_port_assigned(self, port: str, vlan_id: int, mode: str = 'access') -> str:
        return self._info(f'Port {port} assigned to VLAN {vlan_id} ({mode})')

    # ── STP ──────────────────────────────────────────────────────
    def stp_tc(self, port: str) -> str:
        """Topology Change Notification"""
        return self._warn(f'STP: Topology change detected on port {port}')

    def stp_bpduguard(self, port: str) -> str:
        """BPDU Guard発動 → ポート停止"""
        return self._crit(f'Port {port} STP BPDU Guard activated: port disabled')

    def stp_root_changed(self, vlan_id: int, new_root: str) -> str:
        """ルートブリッジ変更"""
        return self._warn(f'STP: Root bridge changed for VLAN {vlan_id}: {new_root}')

    def stp_port_state(self, port: str, state: str) -> str:
        """ポート状態変化"""
        return self._info(f'STP: Port {port} state changed to {state}')

    # ── LACP ─────────────────────────────────────────────────────
    def lacp_link_up(self, port: str, lag_id: int) -> str:
        return self._info(f'LACP: Port {port} bundled into LAG {lag_id}')

    def lacp_link_down(self, port: str, lag_id: int) -> str:
        return self._warn(f'LACP: Port {port} removed from LAG {lag_id}')

    # ── SNMP ─────────────────────────────────────────────────────
    def snmp_auth_fail(self, src_ip: str) -> str:
        return self._warn(f'SNMP: Authentication failure from {src_ip}')

    # ── show logging 形式 ─────────────────────────────────────────
    def format_show_logging(self, hostname: str, logs: list) -> str:
        """
        show logging の出力（APRESIA ApresiaLight準拠）
        """
        lines = [
            f'System Log:',
            f'  Hostname : {hostname}',
            f'  Time     : {datetime.now().strftime("%Y/%m/%d %H:%M:%S")}',
            f'',
            f'Log entries:',
        ]
        if not logs:
            lines.append('  (no log messages)')
        else:
            for log in logs[-50:]:
                msg = log.get('message', '') if isinstance(log, dict) else str(log)
                lines.append(f'  {msg}')
        return '\n'.join(lines)


apresia_msg = ApresiaMessageEngine()
