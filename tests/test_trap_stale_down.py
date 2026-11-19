"""
スタンドアロン版: linkUp の trap を取りこぼしても、TRAP の「down」が残り続けない

trap は UDP なので、linkUp が届かないことがある。以前は、受け取った linkDown が期限なしで残り、
実際は復旧していてもリンクが LINK DOWN のままになった。

- linkDown から stale_after 秒(既定 90)以上たっても POLL(いまの状態の読み取り)が up なら、その down は捨てる
- 新しい linkDown(POLL の更新が追いついていない間)は、POLL が up でも down のまま
- POLL も down、または POLL が無い(読み取りをしていない)ときは捨てない
- 普通の linkDown → linkUp、stale_after の設定、本物の UDP で受けた linkDown trap
"""
import os
import sys
import time

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))

from cvs import config as sa_config  # noqa: E402
from cvs import snmp_client as SC  # noqa: E402
from cvs import snmp_poll as SP  # noqa: E402
from cvs.collector import Collector  # noqa: E402
from cvs.link_evidence import TRAP_OID_LINKDOWN, TRAP_OID_LINKUP, traps  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_traps():
    traps.clear()
    yield
    traps.stop()
    traps.clear()


class _Fake:
    """SC.walk の代わり。Catalyst の Gi0/4 だけを返す。oper: 1=up / 2=down"""

    def __init__(self):
        self.oper = 1

    def walk(self, host, comm, root, **kw):
        idx = 1
        rows = {
            SP.IFNAME: 'Gi0/4',
            SP.IF_HC_IN: 0, SP.IF_HC_OUT: 0,
            SP.IF_OPER: self.oper,
        }
        return [(f'{root}.{idx}', rows[root])] if root in rows else []


@pytest.fixture
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(SP.SC, 'walk', f.walk)
    return f


def _collector(stale_after=None):
    sources = {'snmp': {'community': 'x', 'traffic': True}}
    if stale_after is not None:
        sources['snmp_trap'] = {'stale_after': stale_after}
    cfg = sa_config.validate({
        'devices': [{'id': 'cat', 'hostname': 'CAT-01', 'type': 'catalyst', 'mgmt_ip': '192.168.1.254'},
                    {'id': 'pc', 'hostname': 'MY-PC', 'type': 'pc', 'snmp': False}],
        'links': [{'a': 'pc', 'a_if': 'Ethernet', 'b': 'cat', 'b_if': 'GigabitEthernet0/4'}],
        'sources': sources})
    return Collector(cfg)


def _trap(state, age, ifname='GigabitEthernet0/4', src='192.168.1.254'):
    """age 秒前に届いた linkDown / linkUp trap(Catalyst の送信元 IP = mgmt_ip で装置を特定する実機と同じ形)"""
    oid = TRAP_OID_LINKDOWN if state == 'down' else TRAP_OID_LINKUP
    traps.record(src, {'trap_oid': oid, 'descr': '', 'ifname': ifname}, now=time.time() - age)


def _link(c, fake=None, oper=None, poll=True):
    if poll:
        fake.oper = oper
        c.snmp.traffic_tick(now=time.time())
    return c._build(60)['links'][0]


def test_fresh_trap_down_stays_down_even_if_poll_still_says_up(fake):
    c = _collector()
    _trap('down', age=10)                                   # POLL の更新が追いついていないだけかもしれない
    ln = _link(c, fake, oper=1)
    assert ln['down'] is True and ln['down_sources'] == ['TRAP']


def test_old_trap_down_is_dropped_when_poll_says_up(fake):
    """linkUp の trap を取りこぼした: 200 秒前の linkDown が残っていても、POLL が up なら正常に戻す"""
    c = _collector()
    _trap('down', age=200)
    ln = _link(c, fake, oper=1)
    assert ln['down'] is False and ln['down_sources'] == [] and ln['evidence']['TRAP'] is None
    assert ln['evidence']['POLL'] == 'up' and ln['state'] == 'ok'


def test_old_trap_down_is_kept_when_poll_also_says_down(fake):
    c = _collector()
    _trap('down', age=200)
    ln = _link(c, fake, oper=2)
    assert ln['down'] is True and ln['down_sources'] == ['TRAP', 'POLL']


def test_old_trap_down_is_kept_when_there_is_no_poll_at_all():
    """POLL(いまの状態の読み取り)が無いのに、trap の down を捨てる根拠は無い"""
    c = _collector()
    _trap('down', age=100000)
    ln = _link(c, poll=False)
    assert ln['down'] is True and ln['down_sources'] == ['TRAP']


def test_normal_linkdown_then_linkup_clears_without_poll():
    c = _collector()
    _trap('down', age=30)
    _trap('up', age=10)
    ln = _link(c, poll=False)
    assert ln['down'] is False and ln['evidence']['TRAP'] == 'up'


def test_stale_after_is_configurable(fake):
    c = _collector(stale_after=5)
    assert c.trap_stale_after == 5
    _trap('down', age=10)
    assert _link(c, fake, oper=1)['down'] is False           # 5 秒より古いので捨てる

    traps.clear()
    c = _collector(stale_after=1000)
    _trap('down', age=200)
    assert _link(c, fake, oper=1)['down'] is True            # 1000 秒には届いていないので残す
    assert _collector().trap_stale_after == 90               # 既定


def test_other_interface_trap_is_not_affected_by_polling_a_different_interface(fake):
    c = _collector()
    _trap('down', age=200, ifname='GigabitEthernet0/9')      # links に無い IF
    ln = _link(c, fake, oper=1)
    assert ln['down'] is False and ln['evidence']['TRAP'] is None


def test_real_udp_linkdown_trap_marks_the_link_down():
    """本物の UDP で linkDown trap(エミュレータが作る v2c の trap と同じ形式)を受けて、リンクが down になる"""
    from engine.syslog_sender import build_snmp_v2c_trap
    import socket
    spec = traps.start('127.0.0.1:0')
    host, port = spec.rsplit(':', 1)
    pkt = build_snmp_v2c_trap('public', 'CAT-01', TRAP_OID_LINKDOWN, 'GigabitEthernet0/4 down')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(pkt, (host, int(port)))
    finally:
        s.close()
    for _ in range(40):
        if traps.snapshot():
            break
        time.sleep(0.05)
    ev = traps.snapshot()
    assert len(ev) == 1 and ev[0]['state'] == 'down' and ev[0]['iface'] == 'GigabitEthernet0/4'
    ln = _link(_collector(), poll=False)
    assert ln['down'] is True and ln['down_sources'] == ['TRAP']
    assert SC is not None        # (snmp_client を import したままにして ruff の未使用判定を避ける)
