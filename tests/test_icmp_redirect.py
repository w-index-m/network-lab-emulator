"""ICMP Redirect(engine/icmp_redirect.py)と /cyber-view 用 API の回帰テスト。

構成: PC1・R1・R2 が同じセグメント(L2 スイッチ配下)、R2 の先にサーバ。
PC1 の既定ゲートウェイは R1、R1 はサーバのネットワークを R2(同じセグメント)へ向ける
→ R1 は PC1 に Redirect を返し、PC1 は以後 R2 へ直接送る(Linux の cache <redirected>)。
"""
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine import icmp_redirect  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _id(d):
    return f'{d}-{_RUN}'


def _cli(c, d, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': _id(d), 'command': cmd}).json()['output']
    return out


def _lab(c, tag, net):
    """net: 3オクテット目に使う番号(テストごとに別のアドレス)"""
    sw, r1, r2, pc, sv = (f'{tag}{x}' for x in ('sw', 'r1', 'r2', 'pc', 'sv'))
    for d, t in ((sw, 'catalyst'), (r1, 'cisco'), (r2, 'cisco'), (pc, 'pc'), (sv, 'pc')):
        c.post('/api/device', json={'id': _id(d), 'type': t, 'hostname': d.upper()})
    for p, (d, ifn) in enumerate(((pc, 'eth0'), (r1, 'GigabitEthernet0/0/0'), (r2, 'GigabitEthernet0/0/0')), start=1):
        c.post('/api/link', json={'a': _id(sw), 'b': _id(d), 'iface_a': f'GigabitEthernet1/0/{p}', 'iface_b': ifn})
        _cli(c, sw, 'configure terminal', f'interface GigabitEthernet1/0/{p}', 'switchport mode access',
             'switchport access vlan 50', 'no shutdown', 'end')
    c.post('/api/link', json={'a': _id(r2), 'b': _id(sv), 'iface_a': 'GigabitEthernet0/0/1', 'iface_b': 'eth0'})
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', f'ip address 10.231.{net}.1 255.255.255.0',
         'no shutdown', 'exit', f'ip route 10.232.{net}.0 255.255.255.0 10.231.{net}.2', 'end')
    _cli(c, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', f'ip address 10.231.{net}.2 255.255.255.0',
         'no shutdown', 'exit', 'interface GigabitEthernet0/0/1', f'ip address 10.232.{net}.1 255.255.255.0',
         'no shutdown', 'end')
    _cli(c, pc, f'ip addr add 10.231.{net}.10/24 dev eth0', f'ip route add default via 10.231.{net}.1')
    _cli(c, sv, f'ip addr add 10.232.{net}.10/24 dev eth0', f'ip route add default via 10.232.{net}.1')
    return sw, r1, r2, pc, sv, f'10.232.{net}.10'


def _redirects_sent(c, r):
    m = re.search(r'Sent:\s+(\d+) redirects', _cli(c, r, 'show ip traffic'))
    return int(m.group(1)) if m else -1


def _new_events(c, since):
    return c.get(f'/api/traffic/redirects?since={since}').json()['events']


def test_redirect_sent_and_host_goes_direct(client):
    sw, r1, r2, pc, sv, dest = _lab(client, 'ra', 11)
    seq0 = client.get('/api/traffic/redirects').json()['seq']
    tr = _cli(client, pc, 'traceroute ' + dest)          # 最初は R1 経由
    assert re.search(r'1\s+RA?R1|RAR1', tr.upper()), tr
    evs = [e for e in _new_events(client, seq0) if e['host'] == _id(pc)]
    assert evs and evs[0]['sent'] and evs[0]['router'] == _id(r1) and evs[0]['gw_dev'] == _id(r2)
    assert evs[0]['gw_ip'] == '10.231.11.2' and evs[0]['iface'] == 'GigabitEthernet0/0/0' and evs[0]['cached']
    # Linux と同じく経路を覚え、次からは R2 へ直接
    assert 'cache <redirected>' in _cli(client, pc, 'ip route get ' + dest)
    tr2 = _cli(client, pc, 'traceroute ' + dest)
    assert ' 1  RAR2 ' in tr2 and 'RAR1' not in tr2, tr2
    n = _redirects_sent(client, r1)
    _cli(client, pc, 'ping -c 3 ' + dest)
    assert _redirects_sent(client, r1) == n               # 直接行くので R1 はもう Redirect を出さない


def test_no_ip_redirects_suppresses(client):
    sw, r1, r2, pc, sv, dest = _lab(client, 'rb', 12)
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'no ip redirects', 'end')
    assert ' no ip redirects' in _cli(client, r1, 'show running-config')
    seq0 = client.get('/api/traffic/redirects').json()['seq']
    _cli(client, pc, 'ping -c 3 ' + dest)
    evs = [e for e in _new_events(client, seq0) if e['host'] == _id(pc)]
    assert evs and not evs[0]['sent']
    assert _redirects_sent(client, r1) == 0
    assert 'cache <redirected>' not in _cli(client, pc, 'ip route get ' + dest)
    assert ' 1  RBR1 ' in _cli(client, pc, 'traceroute ' + dest)   # R1 経由のまま
    # ip redirects で戻すと送るようになる
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip redirects', 'end')
    assert 'no ip redirects' not in _cli(client, r1, 'show running-config')
    _cli(client, pc, 'ping -c 1 ' + dest)
    assert _redirects_sent(client, r1) == 1


def test_flush_cache_and_gateway_down(client):
    sw, r1, r2, pc, sv, dest = _lab(client, 'rc', 13)
    _cli(client, pc, 'ping -c 1 ' + dest)
    assert 'cache <redirected>' in _cli(client, pc, 'ip route get ' + dest)
    _cli(client, pc, 'ip route flush cache')
    assert 'cache <redirected>' not in _cli(client, pc, 'ip route get ' + dest)
    assert icmp_redirect.cached_gateway(_id(pc), dest) is None


def test_no_redirect_when_next_hop_not_on_host_segment(client):
    """次ホップがホストと別のセグメントなら Redirect は出ない(通常の転送)"""
    sw, r1, r2, pc, sv, dest = _lab(client, 'rd', 14)
    seq0 = client.get('/api/traffic/redirects').json()['seq']
    _cli(client, pc, 'ping -c 1 10.231.14.2')              # 同じセグメントの R2 宛て: 直結なので R1 を通らない
    assert not [e for e in _new_events(client, seq0) if e['host'] == _id(pc)]


def test_cyber_view_draws_redirects():
    html = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert '/api/traffic/redirects' in html and 'function drawRedirects' in html
    assert "'ICMP REDIRECT'" in html and 'cache <redirected>' in html and 'no ip redirects' in html
