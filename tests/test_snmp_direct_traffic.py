"""
スタンドアロン版: SNMP 直接の流量取得(sources.snmp.traffic)と、応答しない装置を外す devices[].snmp: false

- ifHCIn/OutOctets の差から bps を出す(1回目は基準だけ・64bit が減ったらリセットとして捨てる・
  64bit が無ければ 32bit で折り返しを補正)
- opt-in(traffic を書いた時だけ動く)、snmp: false の装置は問い合わせない
- 続けて 2 回取れなければ「応答なし」(古い値は出さない)、取れたら戻る
- Collector: Prometheus / Zabbix が無くても SNMP の値でリンクの流量・LINK DOWN(POLL)が出る。
  PC のように片側にカウンタが無いリンクは、相手側のカウンタを使う
- エミュレータの本物の SNMP(UDP)から実際に流量が読めること
"""
import asyncio
import os
import sys
import threading
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SA = os.path.join(ROOT, 'cyber-view-standalone')
sys.path.insert(0, ROOT)
sys.path.insert(0, SA)

from cvs import config as sa_config  # noqa: E402
from cvs import snmp_client as SC  # noqa: E402
from cvs import snmp_poll as SP  # noqa: E402
from cvs.collector import Collector  # noqa: E402
from cvs.link_evidence import norm_if  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


def _cfg(traffic=True, extra_devs=(), links=()):
    devs = [{'id': 'cat', 'hostname': 'CAT-01', 'type': 'catalyst', 'mgmt_ip': '192.168.1.254',
             'interfaces': [{'name': 'Vlan263', 'ip': '192.168.1.254', 'prefix': 24}]}] + list(extra_devs)
    snmp = {'community': 'x'}
    if traffic:
        snmp['traffic'] = traffic
    return sa_config.validate({'devices': devs, 'links': list(links), 'sources': {'snmp': snmp}})


class _Fake:
    """SC.walk の代わり。rows[root] = [(oid, 値)]。fail=True なら応答なし(例外)"""

    def __init__(self):
        self.rows = {}
        self.fail = False

    def walk(self, host, comm, root, **kw):
        if self.fail:
            raise SC.SnmpError('timeout')
        return self.rows.get(root, [])

    def set(self, idx_name, cin, cout, oper=1, hc=True):
        """idx_name: {ifIndex: 名前}、cin/cout: {ifIndex: カウンタ}"""
        rin, rout = (SP.IF_HC_IN, SP.IF_HC_OUT) if hc else (SP.IF_IN, SP.IF_OUT)
        other = (SP.IF_IN, SP.IF_OUT) if hc else (SP.IF_HC_IN, SP.IF_HC_OUT)
        self.rows[SP.IFNAME] = [(f'{SP.IFNAME}.{i}', n) for i, n in idx_name.items()]
        self.rows[rin] = [(f'{rin}.{i}', v) for i, v in cin.items()]
        self.rows[rout] = [(f'{rout}.{i}', v) for i, v in cout.items()]
        self.rows[other[0]] = []
        self.rows[other[1]] = []
        self.rows[SP.IF_OPER] = [(f'{SP.IF_OPER}.{i}', oper if isinstance(oper, int) else oper[i]) for i in idx_name]


@pytest.fixture
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(SP.SC, 'walk', f.walk)
    return f


def _poller(**kw):
    cfg = _cfg(**kw)
    return SP.SnmpPoller(cfg, None, {}), cfg


# ───────────────────────── 流量の計算 ─────────────────────────
def test_traffic_is_opt_in_and_interval_is_configurable():
    p, _ = _poller(traffic=False)
    assert p.traffic_on is False
    p, _ = _poller(traffic=True)
    assert p.traffic_on is True and p.traffic_interval == 10
    p, _ = _poller(traffic={'interval': 5})
    assert p.traffic_on is True and p.traffic_interval == 5


def test_rate_from_counter_delta_first_sample_is_baseline_only(fake):
    p, _ = _poller()
    names = {1: 'Gi0/4', 2: 'Vl263'}
    fake.set(names, {1: 1000, 2: 0}, {1: 2000, 2: 0})
    p.traffic_tick(now=100)
    assert p.traffic_snapshot()['rates'] == {}                     # 1回目は基準だけ(でたらめな値を出さない)
    fake.set(names, {1: 1000 + 1_250_000, 2: 0}, {1: 2000 + 625_000, 2: 0})
    p.traffic_tick(now=110)
    snap = p.traffic_snapshot()
    ib, ob = snap['rates'][('cat', norm_if('Gi0/4'))]
    assert ib == pytest.approx(1_000_000) and ob == pytest.approx(500_000)            # bytes×8÷10秒
    assert snap['counters'][('cat', norm_if('Gi0/4'))] == (1_251_000, 627_000)
    assert snap['oper'][('cat', norm_if('Gi0/4'))] == 'up'


def test_counter_reset_is_dropped_for_64bit_and_wrap_is_fixed_for_32bit(fake):
    p, _ = _poller()
    names = {1: 'Gi0/4'}
    key = ('cat', norm_if('Gi0/4'))
    fake.set(names, {1: 10_000_000}, {1: 10_000_000})
    p.traffic_tick(now=0)
    fake.set(names, {1: 100}, {1: 100})                              # 64bit が減った = 装置の再起動
    p.traffic_tick(now=10)
    assert p.traffic_snapshot()['rates'][key] == (0, 0)             # 巨大な値や負の値にならない(その回は捨てる)
    fake.set(names, {1: 100 + 1_250_000}, {1: 100})
    p.traffic_tick(now=20)                                           # 再起動をまたいでも、その後の増分は正しく出る
    assert p.traffic_snapshot()['rates'][key][0] == pytest.approx(1_250_000 * 8 / 20)

    p, _ = _poller()
    fake.set(names, {1: 2 ** 32 - 500}, {1: 2 ** 32 - 500}, hc=False)    # 32bit だけの機種
    p.traffic_tick(now=0)
    fake.set(names, {1: 500}, {1: 1500}, hc=False)                       # 折り返した
    p.traffic_tick(now=10)
    ib, ob = p.traffic_snapshot()['rates'][key]
    assert ib == pytest.approx(1000 * 8 / 10) and ob == pytest.approx(2000 * 8 / 10)


def test_rate_is_averaged_over_the_requested_window(fake):
    """ping のような一瞬の通信が、取得の瞬間によって 0 と山を行き来しない(Prometheus の rate と同じ窓の平均)"""
    p, _ = _poller()
    names = {1: 'Gi0/4'}
    key = ('cat', norm_if('Gi0/4'))
    total = 0
    for now, add in ((0, 0), (10, 1_250_000), (20, 0), (30, 0)):       # 10〜20 秒の間だけ通信があった
        total += add
        fake.set(names, {1: total}, {1: 0})
        p.traffic_tick(now=now)
    assert p.traffic_snapshot(window=10)['rates'][key][0] == 0                                  # 直近 10 秒は静か
    assert p.traffic_snapshot(window=60)['rates'][key][0] == pytest.approx(1_250_000 * 8 / 30)  # 窓全体の平均
    assert p.traffic_snapshot(window=1)['rates'][key][0] == 0                                   # 窓が取得間隔より短くても出る


def test_oper_status_maps_like_prometheus(fake):
    p, _ = _poller()
    names = {1: 'Gi0/1', 2: 'Gi0/2', 3: 'Gi0/3', 4: 'Gi0/4'}
    fake.set(names, {i: 0 for i in names}, {i: 0 for i in names}, oper={1: 1, 2: 2, 3: 7, 4: 5})
    p.traffic_tick(now=0)
    o = p.traffic_snapshot()['oper']
    assert o[('cat', 'gi0/1')] == 'up' and o[('cat', 'gi0/2')] == 'down' and o[('cat', 'gi0/3')] == 'down'
    assert ('cat', 'gi0/4') not in o                                       # dormant 等は判定に使わない


# ───────────────────────── 応答しない装置 ─────────────────────────
def test_snmp_false_device_is_not_polled(fake):
    pc = {'id': 'pc', 'hostname': 'MY-PC', 'type': 'pc', 'snmp': False,
          'interfaces': [{'name': 'Ethernet', 'ip': '192.168.1.10', 'prefix': 24}]}
    p, cfg = _poller(extra_devs=[pc])
    assert [d['id'] for d in p.devs] == ['cat']                      # PC は問い合わせ対象から外れる
    assert {d['id'] for d in cfg['devices']} == {'cat', 'pc'}        # 画面には載る
    fake.set({1: 'Gi0/4'}, {1: 0}, {1: 0})
    p.traffic_tick(now=0)
    assert not [k for k in p.errors if 'pc' in k]


def test_lost_after_two_failures_and_recovers(fake):
    p, _ = _poller()
    names = {1: 'Gi0/4'}
    fake.set(names, {1: 0}, {1: 0})
    p.traffic_tick(now=0)
    fake.set(names, {1: 1_000_000}, {1: 0})
    p.traffic_tick(now=10)
    assert ('cat', 'gi0/4') in p.traffic_snapshot()['rates']
    fake.fail = True
    p.traffic_tick(now=20)
    assert p.traffic_snapshot()['lost'] == {}                         # 1回の取りこぼしでは応答なしにしない
    p.traffic_tick(now=30)
    snap = p.traffic_snapshot()
    assert snap['lost'] == {'cat': '192.168.1.254'} and snap['rates'] == {}      # 古い値は出さない
    assert 'snmp-traffic:cat' in p.errors
    fake.fail = False
    fake.set(names, {1: 2_000_000}, {1: 0})
    p.traffic_tick(now=40)
    assert p.traffic_snapshot()['lost'] == {} and 'snmp-traffic:cat' not in p.errors


# ───────────────────────── Collector(画面に出る値) ─────────────────────────
def _lab_cfg():
    pc = {'id': 'pc', 'hostname': 'MY-PC', 'type': 'pc', 'snmp': False,
          'interfaces': [{'name': 'Ethernet', 'ip': '192.168.1.10', 'prefix': 24}]}
    link = {'a': 'pc', 'a_if': 'Ethernet', 'b': 'cat', 'b_if': 'GigabitEthernet0/4', 'speed': 1_000_000_000}
    return _cfg(extra_devs=[pc], links=[link])


def test_collector_uses_snmp_for_link_rate_and_pc_end_uses_peer_counters(fake):
    c = Collector(_lab_cfg())
    names = {1: 'Gi0/4', 2: 'Vl263'}
    fake.set(names, {1: 0, 2: 0}, {1: 0, 2: 0})
    c.snmp.traffic_tick(now=0)
    fake.set(names, {1: 1_250_000, 2: 0}, {1: 625_000, 2: 0})
    c.snmp.traffic_tick(now=10)
    snap = c._build(60)
    ln = snap['links'][0]
    # a=PC, b=Catalyst。PC 側にカウンタが無いので、Catalyst の in(PC→Catalyst)と out(Catalyst→PC)を使う
    assert ln['ab_bps'] == 1_000_000 and ln['ba_bps'] == 500_000
    assert ln['source'] == 'snmp' and snap['sources_used'] == ['snmp']
    assert ln['down'] is False and ln['state'] == 'ok'
    h = c.health()
    assert h['sources']['metrics'] == 'snmp'
    assert '取得元 snmp' in [x for x in h['categories'] if x['key'] == 'traffic'][0]['summary']


def test_collector_link_down_from_snmp_oper_status(fake):
    c = Collector(_lab_cfg())
    fake.set({1: 'Gi0/4'}, {1: 0}, {1: 0}, oper=2)                   # shutdown / ケーブル抜け
    c.snmp.traffic_tick(now=0)
    ln = c._build(60)['links'][0]
    assert ln['down'] is True and ln['down_sources'] == ['POLL'] and ln['state'] == 'down'


def test_collector_marks_unreachable_after_snmp_failures_and_hosts_shows_pc(fake):
    c = Collector(_lab_cfg())
    fake.fail = True
    c.snmp.traffic_tick(now=0)
    c.snmp.traffic_tick(now=10)
    snap = c._build(60)
    assert [u['id'] for u in snap['unreachable']] == ['cat'] and snap['links'][0]['state'] == 'lost'
    hosts = {h['id']: h for h in c.hosts()}
    assert hosts['pc']['interfaces'][0]['ip'] == '192.168.1.10'      # PC もアドレス空間に載る
    assert hosts['pc']['peers'] == ['cat']


def test_collector_without_traffic_option_is_unchanged(fake):
    cfg = _cfg(traffic=False)
    c = Collector(cfg)
    c.snmp.traffic_tick(now=0)                                          # 呼ばれても画面には出ない
    snap = c._build(60)
    assert snap['sources_used'] == [] and snap['unreachable'] == []


# ───────────────────────── エミュレータの本物の SNMP ─────────────────────────
@pytest.fixture(scope='module')
def client():
    from fastapi.testclient import TestClient
    import app as app_module
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def test_real_snmp_traffic_rate_from_emulator(client):
    """エミュレータの SNMP(UDP)から ifHC カウンタを読み、ping の分だけ流量が出る"""
    from engine.protocols import snmp_agent
    from engine.snmp_udp_agent import SnmpDeviceProtocol
    a, b = f'tr-a-{_RUN}', f'tr-b-{_RUN}'
    for dev, host in ((a, 'TR-A'), (b, 'TR-B')):
        client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': host})
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    _cli(client, a, 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.247.40.1 255.255.255.252',
         'no shutdown', 'exit', 'snmp-server community rt-ro RO', 'end')
    _cli(client, b, 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.247.40.2 255.255.255.252',
         'no shutdown', 'exit', 'snmp-server community rt-ro RO', 'end')
    loop = asyncio.new_event_loop()
    tr, _p = loop.run_until_complete(loop.create_datagram_endpoint(
        lambda: SnmpDeviceProtocol(a, snmp_agent), local_addr=('127.0.0.1', 0)))
    port = tr.get_extra_info('sockname')[1]
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    try:
        cfg = {'devices': [{'id': a, 'hostname': 'TR-A', 'mgmt_ip': '127.0.0.1'}],
               'sources': {'snmp': {'community': 'rt-ro', 'port': port, 'timeout': 1, 'traffic': True}}}
        p = SP.SnmpPoller(cfg, None, {})
        p.traffic_tick(now=1000)                                           # 基準
        assert p.errors == {}
        out = _cli(client, a, 'ping 10.247.40.2 repeat 200 size 1000')
        assert '100 percent' in out
        p.traffic_tick(now=1010)
        snap = p.traffic_snapshot()
        ib, ob = snap['rates'][(a, 'gi0/0/0')]
        assert ob and ob > 200 * 1000 * 8 / 10 * 0.9                         # 送った分(200×1000B)が出る
        assert snap['oper'][(a, 'gi0/0/0')] == 'up'
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)
        tr.close()
