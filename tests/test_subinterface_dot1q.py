"""
サブインタフェース（ルータ・オン・ア・スティック）の 802.1Q 対応。

以前は `interface GigabitEthernet0/0.10` を作って IP を付けることはできたが、
`encapsulation dot1Q 10` が黙って捨てられていた。その結果
  - どのVLANに属するのかが装置内に一切残らない
  - running-config にも出ない
  - 両端でタグ番号が食い違っていても隣接が成立してしまう
という、実機では起きない状態になっていた。
"""

import os
import sys
import time

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _link(a, b, ifa, ifb):
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': ifa, 'iface_b': ifb})


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


def _wait(fn, timeout=12.0, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


class TestEncapsulationCommand:
    def test_encapsulation_is_stored_and_shown_in_running_config(self):
        _dev('sub-1')
        _run('sub-1', [
            'conf t', 'interface GigabitEthernet0/0.10',
            'encapsulation dot1Q 10',
            'ip address 10.10.10.1 255.255.255.0', 'no shutdown', 'end',
        ])
        cfg = _cli('sub-1', 'show running-config')
        assert 'interface GigabitEthernet0/0.10' in cfg
        assert 'encapsulation dot1Q 10' in cfg, cfg
        # encapsulation は ip address より前に出る（実機と同じ順）
        block = cfg.split('interface GigabitEthernet0/0.10')[1]
        assert block.index('encapsulation') < block.index('ip address')

    def test_native_keyword_is_preserved(self):
        _dev('sub-2')
        _run('sub-2', [
            'conf t', 'interface GigabitEthernet0/0.1',
            'encapsulation dot1Q 1 native',
            'ip address 10.10.1.1 255.255.255.0', 'end',
        ])
        assert 'encapsulation dot1Q 1 native' in _cli('sub-2', 'show running-config')

    def test_rejected_on_physical_interface(self):
        # 実機は物理インタフェースでは受け付けない
        _dev('sub-3')
        out = _run('sub-3', ['conf t', 'interface GigabitEthernet0/0',
                             'encapsulation dot1Q 10'])
        assert 'サブインタフェース' in out or 'Incomplete' in out, out

    def test_invalid_vlan_id_rejected(self):
        _dev('sub-4')
        out = _run('sub-4', ['conf t', 'interface GigabitEthernet0/0.99',
                             'encapsulation dot1Q 5000'])
        assert 'Invalid VLAN' in out, out

    def test_no_encapsulation_removes_it(self):
        _dev('sub-5')
        _run('sub-5', [
            'conf t', 'interface GigabitEthernet0/0.20',
            'encapsulation dot1Q 20',
            'ip address 10.10.20.1 255.255.255.0', 'end',
        ])
        assert 'encapsulation dot1Q 20' in _cli('sub-5', 'show running-config')
        _run('sub-5', ['conf t', 'interface GigabitEthernet0/0.20',
                       'no encapsulation dot1Q', 'end'])
        assert 'encapsulation dot1Q 20' not in _cli('sub-5', 'show running-config')


class TestShowVlans:
    def test_show_vlans_lists_subinterfaces(self):
        _dev('sub-v1')
        _run('sub-v1', [
            'conf t',
            'interface GigabitEthernet0/0.10', 'encapsulation dot1Q 10',
            'ip address 10.11.10.1 255.255.255.0', 'exit',
            'interface GigabitEthernet0/0.20', 'encapsulation dot1Q 20',
            'ip address 10.11.20.1 255.255.255.0', 'end',
        ])
        out = _cli('sub-v1', 'show vlans')
        assert 'Virtual LAN ID:  10' in out, out
        assert 'Virtual LAN ID:  20' in out, out
        assert 'GigabitEthernet0/0.10' in out
        assert '10.11.10.1' in out

    def test_show_vlans_when_none_configured(self):
        _dev('sub-v2')
        assert 'No Virtual LANs configured' in _cli('sub-v2', 'show vlans')


class TestVlanTagMismatch:
    """両端のタグ番号が一致していなければ隣接してはいけない。

    判定はエミュレータの仮想ネットワーク上の隣接（ospf_engine の
    neighbors は device_id をキーにする）で行う。同じ装置には
    engine/real_ospf_agent.py の実パケットリスナーも動いていて、
    そちらは loopback 上で router-id をキーにした隣接を作る。
    実リスナーは仮想トポロジのVLANタグとは無関係に動くので、
    出力文字列だけを見ると両者が混ざって判定を誤る。
    """

    def _build(self, a, b, vlan_a, vlan_b, net):
        """サブインタフェース上でEIGRPを動かす2台構成。

        検証にEIGRPを使うのは、OSPFだと実リスナー側の隣接が混ざって
        仮想パスの成否を判定できないため。
        """
        _dev(a)
        _dev(b)
        _link(a, b, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        for dev, ip, vid in ((a, f'{net}.1', vlan_a), (b, f'{net}.2', vlan_b)):
            _run(dev, [
                'conf t', 'interface GigabitEthernet0/0', 'no shutdown', 'exit',
                f'interface GigabitEthernet0/0.{vid}',
                f'encapsulation dot1Q {vid}',
                f'ip address {ip} 255.255.255.0', 'no shutdown', 'exit',
                'router eigrp 100', f'network {net}.0 0.0.0.255', 'end',
            ])

    def test_matching_tags_form_adjacency(self):
        from engine.protocols import eigrp_engine
        self._build('sub-m1', 'sub-m2', 10, 10, '10.12.1')
        assert _wait(lambda: 'sub-m2' in eigrp_engine.nodes
                     .get('sub-m1', {}).get('neighbors', {})), \
            ("タグ一致なのに隣接しない: "
             f"{list(eigrp_engine.nodes.get('sub-m1', {}).get('neighbors', {}))}")

    def test_mismatched_tags_do_not_form_adjacency(self):
        from engine.protocols import eigrp_engine, vnet
        self._build('sub-x1', 'sub-x2', 10, 20, '10.12.2')
        assert not vnet.vlan_tags_compatible('sub-x1', 'sub-x2')
        time.sleep(4)
        nbrs = eigrp_engine.nodes.get('sub-x1', {}).get('neighbors', {})
        assert 'sub-x2' not in nbrs, f'タグ不一致なのに隣接した: {list(nbrs)}'

    def test_same_tag_on_both_sides_is_compatible(self):
        from engine.protocols import vnet
        self._build('sub-y1', 'sub-y2', 30, 30, '10.12.3')
        assert vnet.vlan_tags_compatible('sub-y1', 'sub-y2')

    def test_one_side_untagged_is_not_judged_here(self):
        # 片側だけタグ付き（タグ付き対アクセス）は物理側の設定次第なので
        # ここでは不一致と判断しない
        from engine.protocols import vnet
        _dev('sub-z1')
        _dev('sub-z2')
        _link('sub-z1', 'sub-z2', 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        _run('sub-z1', ['conf t', 'interface GigabitEthernet0/0.40',
                        'encapsulation dot1Q 40',
                        'ip address 10.12.4.1 255.255.255.0', 'end'])
        _run('sub-z2', ['conf t', 'interface GigabitEthernet0/0',
                        'ip address 10.12.4.2 255.255.255.0', 'no shutdown', 'end'])
        assert vnet.vlan_tags_compatible('sub-z1', 'sub-z2')
