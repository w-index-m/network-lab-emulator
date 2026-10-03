"""
VRRP unicast-peer（keepalivedの`unicast_peer`相当）。

https://github.com/vincentbernat/network-lab の`lab-keepalived-unicast`を
見て持ち込んだ機能(ユーザーが「両方」実装してほしいと指定した2機能の1つ)。

**実装前の確認で見つかった制約**: 既存の`VrrpEngine`はVRRP
advertisementを`vnet.broadcast_to_neighbors`(= multicast相当)でしか
送らず、`app.py`側のVRRP設定コマンドも選出の即時同期を
`vnet.get_neighbors(device_id)`(= 直結隣接)だけに頼っていた。つまり
**VRRPピア同士が`vnet`上で直結(同一リンク)でなければ、そもそも
advertisementが届かず永久にInitのまま選出されない**制約がある。
これはkeepalivedのunicast_peerが実運用で解決する課題そのもの
(multicastが届かない/フィルタされる環境でVRRPを使うため、相手の
IPへ明示的にユニキャストで送る)なので、このエミュレータでも同じ
制約を同じ方法で解決する形にした。

実装: `VrrpGroup`に`unicast_peers: List[str]`を追加、
`VrrpEngine._vrrp_send_advert`はbroadcast_to_neighborsに加えて
`unicast_peers`に設定されたIPの装置へ`vnet.send_to()`で直接
advertisementを送る(`icmp_engine._find_device_owning_ip()`でIPから
装置を解決。vnetの直結隣接である必要はない)。CLI:
`vrrp <gid> unicast-peer <ip>`(Cisco/Catalyst形式に相乗り。実際の
Cisco IOSにはこのコマンドは無い — unicast VRRPはkeepalived固有の
機能だが、このエミュレータにkeepalived相当の専用device_typeは無いため、
既存のpragmatic判断(EOSでcrypto key generate rsa流用等)と同じ考え方で
Cisco形式のVRRP配下にそのまま追加した)。設定直後にも
`vnet.get_neighbors`経由の既存パターンと同じ即時ネゴシエーションを行う。

Live-verified: R1 -- MID -- R2 (R1/R2は直結せず、中間L3装置MID経由)で
`vrrp <gid> ip <vip>`だけでは両方Initのまま選出されないことを確認し、
`vrrp <gid> unicast-peer <相手IP>`を両側に設定した途端に正しく
Master/Backupへ選出されることを確認(`show vrrp`/`show running-config`
への反映も含む)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _link(a, b, iface_a, iface_b):
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': iface_a, 'iface_b': iface_b})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


def _out(id_, cmd):
    return _cli(id_, cmd)['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _out(id_, cmd)
    return out


_subnet_counter = [0]


def _setup_non_adjacent_vrrp(suffix, vip, pri1=150, pri2=100):
    """R1 -- MID -- R2 の3台を作り、R1/R2にVRRPだけ設定する
    (unicast-peerは呼び出し側で設定する)。R1とR2はvnet上で直結しない。

    各呼び出しでユニークな/24サブネットを使う — 全体テストスイート実行
    時、別のテストケース(suffix違い)が同じIPを使うと
    icmp_engine._find_device_owning_ip()がどちらの装置を指すか曖昧になり、
    unicast-peer解決が別の試験のデバイスに化けてしまう事故を実際に
    起こしたため(フルスイートでのみ再現、単体では再現しなかった)。"""
    _subnet_counter[0] += 1
    octet = 60 + _subnet_counter[0]
    r1, r2, mid = f'vr1-{suffix}', f'vr2-{suffix}', f'vmid-{suffix}'
    _dev(r1); _dev(r2); _dev(mid)
    _link(r1, mid, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
    _link(mid, r2, 'GigabitEthernet0/1', 'GigabitEthernet0/0')

    _run(r1, ['configure terminal', 'interface GigabitEthernet0/0',
              f'ip address 192.168.{octet}.1 255.255.255.0', 'no shutdown', 'exit'])
    _run(r2, ['configure terminal', 'interface GigabitEthernet0/0',
              f'ip address 192.168.{octet}.2 255.255.255.0', 'no shutdown', 'exit'])

    _run(r1, ['interface GigabitEthernet0/0', f'vrrp 10 ip {vip}',
              f'vrrp 10 priority {pri1}'])
    _run(r2, ['interface GigabitEthernet0/0', f'vrrp 10 ip {vip}',
              f'vrrp 10 priority {pri2}'])
    return r1, r2, mid


class TestVrrpUnicastPeer:
    def test_non_adjacent_peers_never_elect_without_unicast_peer(self):
        """regression防止: vnet上で直結していないVRRPピアは、
        unicast-peerを設定しない限り選出されずInitのまま
        (= この制約自体が実在することの固定)。"""
        r1, r2, _mid = _setup_non_adjacent_vrrp('noucast1', '192.168.50.254')
        assert 'State is Init' in _out(r1, 'show vrrp')
        assert 'State is Init' in _out(r2, 'show vrrp')

    def test_unicast_peer_lets_non_adjacent_peers_elect(self):
        r1, r2, _mid = _setup_non_adjacent_vrrp('ucast1', '192.168.51.254', pri1=150, pri2=100)
        _run(r1, [f'vrrp 10 unicast-peer 192.168.50.2'.replace('192.168.50.2', _peer_ip(r2))])
        _run(r2, [f'vrrp 10 unicast-peer {_peer_ip(r1)}'])

        out1 = _out(r1, 'show vrrp')
        out2 = _out(r2, 'show vrrp')
        assert 'State is Master' in out1
        assert f'Master Router is {r1} (local)' in out1
        assert 'State is Backup' in out2
        assert 'Master Router is' in out2 and r1 not in out2.split('Master Router is')[0]

    def test_unicast_peer_reflected_in_running_config(self):
        r1, r2, _mid = _setup_non_adjacent_vrrp('ucast2', '192.168.52.254')
        peer_ip = _peer_ip(r2)
        _run(r1, [f'vrrp 10 unicast-peer {peer_ip}'])
        rc = _out(r1, 'show running-config')
        assert f'vrrp 10 unicast-peer {peer_ip}' in rc

    def test_show_vrrp_lists_configured_unicast_peers(self):
        r1, r2, _mid = _setup_non_adjacent_vrrp('ucast3', '192.168.53.254')
        peer_ip = _peer_ip(r2)
        _run(r1, [f'vrrp 10 unicast-peer {peer_ip}'])
        out1 = _out(r1, 'show vrrp')
        assert f'VRRP Unicast Peer(s): {peer_ip}' in out1

    def test_lower_priority_side_becomes_backup_once_unicast_configured(self):
        r1, r2, _mid = _setup_non_adjacent_vrrp('ucast4', '192.168.54.254', pri1=90, pri2=200)
        _run(r1, [f'vrrp 10 unicast-peer {_peer_ip(r2)}'])
        _run(r2, [f'vrrp 10 unicast-peer {_peer_ip(r1)}'])
        assert 'State is Backup' in _out(r1, 'show vrrp')
        assert 'State is Master' in _out(r2, 'show vrrp')


def _peer_ip(device_id: str) -> str:
    """セットアップでGigabitEthernet0/0/0に明示的に振ったIPを取得する
    (装置には既定のGigabitEthernet0/0/0が別IPで事前登録されているため、
    辞書の最初の値を拾うと誤った既定IPを返してしまう)。"""
    st = app_module.device_sessions[device_id]
    info = st.interfaces.get('GigabitEthernet0/0')
    ip = info.get('ip') if isinstance(info, dict) else None
    if not ip:
        raise AssertionError(f'{device_id} has no GigabitEthernet0/0 IP')
    return ip
