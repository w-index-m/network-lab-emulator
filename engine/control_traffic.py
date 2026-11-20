"""
制御プレーンのマルチキャスト(実機が黙っていても送り続けているフレーム)をポートのカウンタに積む

実機の IF カウンタは、ユーザーが何もしなくても少しずつ増える。STP の BPDU、OSPF の Hello、
CDP / LLDP の広告が一定間隔で流れているから。以前のエミュレータは ping のときしかカウンタが
動かず、SNMP や gNMI で見ても「何も流れていないリンク」に見えていた。

- 送る側の out と、リンクの相手側の in に、同じ数・同じ大きさのマルチキャストとして積む
- どちらかのポートが shutdown なら積まない
- 間隔(秒)と大きさ(フレーム長、バイト)は実機の既定値

  STP BPDU   2 秒  64 B  STP が有効で、そのポートが Designated(BPDU を送る側)
  OSPF Hello hello-interval(既定 10 秒)  98 B  OSPF が有効で、相手が OSPF の隣接
  CDP        60 秒  400 B  Cisco 系(cisco / catalyst / nexus / wlc)。実機と同じく既定で有効
  LLDP       30 秒  300 B  `lldp run` 済み、または既定で有効な機種(arista / juniper)

小数ぶんは持ち越すので、呼ぶ間隔(1 秒ごと等)に関係なく平均の数は合う。
"""
import time

STP_BPDU = (2.0, 64)
OSPF_HELLO_SIZE = 98
CDP = (60.0, 400)
LLDP = (30.0, 300)
CDP_TYPES = ('cisco', 'catalyst', 'nexus', 'wlc')
LLDP_DEFAULT_ON = ('arista', 'juniper')


class ControlTraffic:
    def __init__(self):
        self._acc = {}
        self._last = None

    def reset(self):
        self._acc.clear()
        self._last = None

    @staticmethod
    def sources(a, b, states, stp_engine, ospf_engine):
        """装置 a がリンクの相手 b へ送り続けている制御フレーム: [(名前, 間隔秒, 大きさ)]"""
        out = []
        n = stp_engine.nodes.get(a)
        if n and n.get('enabled'):
            port = next((p for p in n.get('ports', {}).values() if p.get('connected_to') == b), None)
            if port is not None and port.get('role', 'DESIGNATED') == 'DESIGNATED':
                out.append(('stp', *STP_BPDU))
        on = ospf_engine.nodes.get(a)
        if on and on.get('enabled') and b in (on.get('neighbors') or {}):
            out.append(('ospf', float(on.get('hello_interval') or 10), OSPF_HELLO_SIZE))
        st = states.get(a)
        dt = getattr(st, 'device_type', '')
        if dt in CDP_TYPES and getattr(st, 'cdp_run', True):
            out.append(('cdp', *CDP))
        if getattr(st, 'lldp_enabled', False) or dt in LLDP_DEFAULT_ON:
            out.append(('lldp', *LLDP))
        return out

    def tick(self, states, vnet, dp_engine, stp_engine, ospf_engine, now=None):
        """前回から経った時間ぶんの制御フレームを積む。積んだフレームの数を返す"""
        now = time.time() if now is None else now
        if self._last is None:
            self._last = now
            return 0
        elapsed = min(max(now - self._last, 0.0), 120.0)   # 止まっていた後に一度に積みすぎない
        self._last = now
        norm = vnet._norm_iface
        total = 0
        for a, peers in list(vnet.links.items()):
            down_a = {norm(x) for x in vnet.down_interfaces.get(a, set())}
            for b in list(peers):
                ia = vnet.interface_links.get(a, {}).get(b)
                ib = vnet.interface_links.get(b, {}).get(a)
                if not ia or not ib:
                    continue
                if norm(ia) in down_a or norm(ib) in {norm(x) for x in vnet.down_interfaces.get(b, set())}:
                    continue
                for name, period, size in self.sources(a, b, states, stp_engine, ospf_engine):
                    key = (a, b, name)
                    acc = self._acc.get(key, 0.0) + elapsed / period
                    cnt = int(acc + 1e-9)          # 1/30 を 60 回足すと 1.9999… になるのを 2 と数える
                    self._acc[key] = max(acc - cnt, 0.0)
                    if cnt:
                        dp_engine.bump(a, ia, 'out', pkts=cnt, size=size, kind='mcast')
                        dp_engine.bump(b, ib, 'in', pkts=cnt, size=size, kind='mcast')
                        total += cnt
        return total


control_traffic = ControlTraffic()
