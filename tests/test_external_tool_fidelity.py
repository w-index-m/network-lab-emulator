"""
pyATS/Genie・Batfish をエミュレータに当てて見つかった忠実度バグの回帰テスト
(外部ツール自体には依存しない。詳細は docs/external-tools-pyats-gobgp-gnmic-batfish.md)

pyATS/Genie で発見:
- `show interfaces`(引数なし)がCisco/CatalystでもSi-R形式を返し、Genieが空結果
- `show inventory` が全機種で WS-C3650 スタック(show version の ISR4321/C9300 と矛盾)
- `show ip ssh` 未実装
- running-config の `version 17.3` が show version(17.9.1)と矛盾
- OSPF Dead Time が「最終Hello受信から」ではなく不規則に0〜40秒を回る
- インタフェースshutdownでそのセグメントがOSPFから撤回されず対向に経路が残る

Batfish(設定から計算した経路との差分検証)で発見:
- `ip access-group` が常に'lan0'に紐付き running-config に出ない
- 1台に複数エリア(ABR)を設定すると全IFが最後のエリアになり area 0 側の隣接が張れない
- running-config の `network ... area` がnetworkごとのエリア/入力表記を反映しない
- エリア間経路が `O IA` ではなく `IA` と表示される
"""

import os
import re
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


# 自動保存(saved_config.json)から前回実行の同名装置が復元されて状態が
# 混ざらないよう、装置IDは実行ごとにユニークにする
_RUN = uuid.uuid4().hex[:6]


def _id(dev_id):
    return f'{dev_id}-{_RUN}'


def _dev(c, dev_id, dtype, hostname):
    c.post('/api/device', json={'id': _id(dev_id), 'type': dtype, 'hostname': hostname})


def _cli(c, dev_id, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': _id(dev_id), 'command': cmd}).json()['output']
    return out


def _wait(pred, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.5)
    return False


class TestShowCommandFidelity:
    def test_show_interfaces_is_ios_format_on_cisco(self, client):
        _dev(client, 'fid-r1', 'cisco', 'FID-R1')
        out = _cli(client, 'fid-r1', 'show interfaces')
        assert 'IP address/masklen' not in out
        assert 'GigabitEthernet0/0/0 is up, line protocol is up' in out
        assert out.count('line protocol is') == 3

    def test_show_inventory_matches_show_version_model(self, client):
        _dev(client, 'fid-r1', 'cisco', 'FID-R1')
        _dev(client, 'fid-sw1', 'catalyst', 'FID-SW1')
        inv_r = _cli(client, 'fid-r1', 'show inventory')
        assert 'ISR4321/K9' in inv_r and 'WS-C3650' not in inv_r
        assert 'FLM2xxx0001' in inv_r and 'FLM2xxx0001' in _cli(client, 'fid-r1', 'show version')
        # catalyst は実機WS-C3650の採取どおり(test_show_output_fidelity.py)
        assert 'WS-C3650-24TD-E' in _cli(client, 'fid-sw1', 'show inventory')

    def test_show_ip_ssh(self, client):
        _dev(client, 'fid-ssh', 'cisco', 'FID-SSH')
        assert _cli(client, 'fid-ssh', 'show ip ssh').startswith('SSH Disabled')
        _cli(client, 'fid-ssh', 'configure terminal',
             'interface GigabitEthernet0/0/1', 'ip address 10.229.9.1 255.255.255.0', 'exit',
             'crypto key generate rsa modulus 2048', 'end')
        assert _cli(client, 'fid-ssh', 'show ip ssh').startswith('SSH Enabled - version 2.0')

    def test_running_config_version_matches_show_version(self, client):
        _dev(client, 'fid-r1', 'cisco', 'FID-R1')
        assert 'version 17.9' in _cli(client, 'fid-r1', 'show running-config')
        assert 'Version 17.9.1' in _cli(client, 'fid-r1', 'show version')


class TestAccessGroupBinding:
    def test_access_group_bound_to_current_interface_and_rendered(self, client):
        _dev(client, 'fid-acl', 'cisco', 'FID-ACL')
        _cli(client, 'fid-acl', 'configure terminal',
             'ip access-list extended BLOCK', 'permit tcp any any eq 22', 'deny ip any any', 'exit',
             'interface GigabitEthernet0/0/1', 'ip address 10.229.8.1 255.255.255.0',
             'ip access-group BLOCK in', 'end')
        rc = _cli(client, 'fid-acl', 'show running-config')
        block = rc.split('interface GigabitEthernet0/0/1')[1].split('!')[0]
        assert 'ip access-group BLOCK in' in block
        assert 'ip access-group' not in rc.split('interface GigabitEthernet0/0/0')[1].split('!')[0]
        from engine.protocols import ipfilter_engine
        assert ipfilter_engine.applied[_id('fid-acl')][('GigabitEthernet0/0/1', 'in')] == 'BLOCK'


class TestOspfMultiAreaAbr:
    """r1(area 0のみ) -- r2(ABR: area 0 + area 1)"""

    # conftestのautouse _cleanup がテストごとにOSPFを止めるため毎回組み直す
    # (NETLAB_FAST_TIMERS下なので収束は数秒)
    @pytest.fixture
    def lab(self, client, request):
        # テストごとに別の装置IDにする(同じIDを使い回すと前テストのOSPF状態が残る)
        o1, o2 = f'fid-o1-{request.node.name[-12:]}', f'fid-o2-{request.node.name[-12:]}'
        self.o1, self.o2 = o1, o2
        for d, h in ((o1, 'FID-O1'), (o2, 'FID-O2')):
            _dev(client, d, 'cisco', h)
        client.post('/api/link', json={'a': _id(o1), 'b': _id(o2),
                                       'iface_a': 'GigabitEthernet0/0/0',
                                       'iface_b': 'GigabitEthernet0/0/0'})
        _cli(client, o1, 'configure terminal',
             'interface GigabitEthernet0/0/0', 'ip address 10.229.12.1 255.255.255.252', 'no shutdown', 'exit',
             'interface GigabitEthernet0/0/1', 'ip address 10.229.1.1 255.255.255.0', 'no shutdown', 'exit',
             'router ospf 1', 'network 10.229.12.0 0.0.0.3 area 0', 'network 10.229.1.0 0.0.0.255 area 0', 'end')
        _cli(client, o2, 'configure terminal',
             'interface GigabitEthernet0/0/0', 'ip address 10.229.12.2 255.255.255.252', 'no shutdown', 'exit',
             'interface GigabitEthernet0/0/1', 'ip address 10.229.2.1 255.255.255.0', 'no shutdown', 'exit',
             'router ospf 1', 'network 10.229.12.0 0.0.0.3 area 0', 'network 10.229.2.0 0.0.0.255 area 1', 'end')
        ok = _wait(lambda: '10.229.2.0/24' in _cli(client, o1, 'show ip route'))
        assert ok, _cli(client, o1, 'show ip ospf neighbor')
        return client

    def test_abr_adjacency_on_backbone_link(self, lab):
        o1, o2 = self.o1, self.o2
        out = _cli(lab, o1, 'show ip ospf neighbor')
        assert re.search(r'10\.229\.12\.2\s+1\s+FULL|10\.229\.12\.2\s+1\s+Full', out)

    def test_inter_area_route_displayed_as_o_ia(self, lab):
        o1, o2 = self.o1, self.o2
        out = _cli(lab, o1, 'show ip route')
        assert re.search(r'^O IA\s+10\.229\.2\.0/24 \[110/\d+\] via 10\.229\.12\.2', out, re.M)
        out2 = _cli(lab, o2, 'show ip route')
        assert re.search(r'^O\s+10\.229\.1\.0/24', out2, re.M)

    def test_show_ip_ospf_interface_area_per_interface(self, lab):
        o1, o2 = self.o1, self.o2
        out = _cli(lab, o2, 'show ip ospf interface brief')
        assert 'Internet Address 10.229.12.2/30, Area 0.0.0.0' in out
        assert 'Internet Address 10.229.2.1/24, Area 0.0.0.1' in out

    def test_running_config_area_per_network_as_entered(self, lab):
        o1, o2 = self.o1, self.o2
        rc = _cli(lab, o2, 'show running-config')
        assert 'network 10.229.12.0 0.0.0.3 area 0\n' in rc + '\n'
        assert 'network 10.229.2.0 0.0.0.255 area 1' in rc
        assert 'area 0.0.0.0' not in rc

    def test_dead_time_counts_down_from_last_hello(self, lab):
        o1, o2 = self.o1, self.o2
        out = _cli(lab, o1, 'show ip ospf neighbor')
        m = re.search(r'10\.229\.12\.2\s+\d+\s+\S+\s+(\d\d):(\d\d):(\d\d)', out)
        assert m, out
        left = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        from engine.protocols import ospf_engine
        n = ospf_engine.nodes[_id(o1)]
        # 最終Hello受信からのカウントダウンなので dead - hello 以上(端数で-1)
        assert n['dead_interval'] - n['hello_interval'] - 1 <= left <= n['dead_interval'], out

    def test_shutdown_withdraws_stub_and_no_shutdown_restores(self, lab):
        o1, o2 = self.o1, self.o2
        _cli(lab, o2, 'configure terminal', 'interface GigabitEthernet0/0/1', 'shutdown', 'end')
        assert _wait(lambda: '10.229.2.0/24' not in _cli(lab, o1, 'show ip route'), 15)
        _cli(lab, o2, 'configure terminal', 'interface GigabitEthernet0/0/1', 'no shutdown', 'end')
        assert _wait(lambda: '10.229.2.0/24' in _cli(lab, o1, 'show ip route'), 15)
