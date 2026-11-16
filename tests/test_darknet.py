"""
ダークネット観測(NICTER のエッセンス、/cyber-view の DARKNET)

- 1パケットで決まる分類(DDoS の跳ね返り・リフレクション攻撃の準備)と、数で決まる分類(スキャン・設定ミス)
- 日ごとの統計(観測パケット数・送信元IP数・1IPあたり)
- lo の擬似イーサネットフレームの読み取り
- API: 帯の設定(装置が使っている帯・広すぎる帯は拒否)、ラボの ping がダークネット宛なら記録、警報(花びら DARKNET)
- 本物のパケット: root なら帯を lo に引き込み、試験ツールのスキャンが本当に届いて分類されること
"""
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as app_module  # noqa: E402
from engine import darknet as D  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset():
    D.darknet.set_prefixes([])
    D.darknet.clear()
    yield
    if D.darknet.sensor:
        D.darknet.sensor.stop()
        D.darknet.sensor = None
    D.darknet.set_prefixes([])
    D.darknet.clear()


class TestClassify:
    def test_single_packet_rules(self):
        assert D.classify_packet('tcp', 80, 40000, D.TCP_SYN | D.TCP_ACK) == 'backscatter'
        assert D.classify_packet('tcp', 80, 40000, D.TCP_RST) == 'backscatter'
        assert D.classify_packet('tcp', 40000, 445, D.TCP_SYN) is None
        assert D.classify_packet('icmp', 0, 0, icmp_type=0) == 'backscatter'
        assert D.classify_packet('icmp', 0, 0, icmp_type=8) is None
        assert D.classify_packet('udp', 5000, 53) == 'reflection'
        assert D.classify_packet('udp', 123, 40000) == 'backscatter'
        assert D.classify_packet('udp', 5000, 514) is None

    def test_scan_misconfig_and_stats(self):
        fired = []
        dn = D.Darknet(fire=lambda *a: fired.append(a))
        dn.set_prefixes(['10.249.1.0/24'])
        assert dn.observe('1.2.3.4', '192.168.0.1', 'tcp') is None          # 帯の外は記録しない
        cats = [dn.observe('198.51.100.9', f'10.249.1.{i}', 'tcp', 40000, 445, D.TCP_SYN, now=100 + i)['cat']
                for i in range(1, 8)]
        assert cats[:4] == ['other'] * 4 and cats[4:] == ['scan'] * 3     # 5アドレス目からスキャン
        mc = [dn.observe('172.31.9.9', '10.249.1.200', 'udp', 5000, 514, now=200 + i)['cat'] for i in range(6)]
        assert mc[-1] == 'misconfig' and mc[0] == 'other'
        dn.observe('203.0.113.80', '10.249.1.50', 'tcp', 80, 41000, D.TCP_SYN | D.TCP_ACK, now=300)
        dn.observe('192.0.2.44', '10.249.1.60', 'udp', 5000, 123, now=301)
        rules = [f[0] for f in fired]
        assert {'darknet_scan', 'darknet_misconfig', 'darknet_backscatter', 'darknet_reflection'} <= set(rules)
        snap = dn.snapshot()
        day = snap['days'][0]
        assert day['packets'] == 15 and day['srcs'] == 4
        assert day['per_ip'] == round(15 / 256, 2)
        assert snap['size'] == 256 and len(snap['events']) == 15
        assert 'DDoS の跳ね返り' in [e for e in snap['events'] if e['cat'] == 'backscatter'][0]['text']

    def test_prefix_validation(self):
        dn = D.Darknet()
        with pytest.raises(ValueError):
            dn.set_prefixes(['10.0.0.0/8'])
        with pytest.raises(ValueError):
            dn.set_prefixes(['2001:db8::/64'])


def test_parse_frame():
    tcp = struct.pack('!HHIIBBHHH', 80, 41000, 1, 2, 5 << 4, 0x12, 1000, 0, 0)
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0,
                     socket.inet_aton('203.0.113.80'), socket.inet_aton('10.249.1.5'))
    frame = b'\0' * 12 + b'\x08\x00' + ip + tcp
    assert D.parse_frame(frame) == ('203.0.113.80', '10.249.1.5', 'tcp', 80, 41000, 0x12, None, 40)
    assert D.parse_frame(b'\0' * 12 + b'\x86\xdd' + b'\0' * 40) is None


def test_api_config_lab_ping_and_alert(client):
    app_module._darknet_init()
    a, b = f'dk-a-{_RUN}', f'dk-b-{_RUN}'
    for dev in (a, b):
        client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': dev.upper()})
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    for dev, ip in ((a, '10.249.2.1'), (b, '10.249.2.2')):
        for cmd in ('configure terminal', 'interface GigabitEthernet0/0/0', f'ip address {ip} 255.255.255.0',
                    'no shutdown', 'end'):
            client.post('/api/cli', json={'device_id': dev, 'command': cmd})
    # 装置が使っている帯・広すぎる帯は拒否
    r = client.post('/api/darknet/config', json={'prefixes': ['10.249.2.0/24'], 'sensor': False})
    assert r.status_code == 400 and '使っています' in r.json()['error']
    assert client.post('/api/darknet/config', json={'prefixes': ['10.0.0.0/8'], 'sensor': False}).status_code == 400
    r = client.post('/api/darknet/config', json={'prefixes': ['10.249.3.0/24'], 'sensor': False})
    assert r.status_code == 200 and r.json()['prefixes'] == ['10.249.3.0/24']
    client.post('/api/cli', json={'device_id': a, 'command': 'configure terminal'})
    client.post('/api/cli', json={'device_id': a, 'command': 'ip route 10.249.3.0 255.255.255.0 10.249.2.2'})
    client.post('/api/cli', json={'device_id': a, 'command': 'end'})
    for i in range(1, 7):
        client.post('/api/cli', json={'device_id': a, 'command': f'ping 10.249.3.{i}'})
    d = client.get('/api/darknet').json()
    assert d['days'][0]['packets'] == 30                                  # 6アドレス × 5発
    assert all(e['proto'] == 'icmp' and e['via'] == 'lab' and e['src'] == '10.249.2.1' for e in d['events'])
    assert d['hour']['cats']['scan'] > 0
    al = [x for x in client.get('/api/traffic/alerts').json()['alerts'] if x['rule'] == 'darknet_scan']
    assert al and al[-1]['source'] == 'DARKNET' and al[-1]['severity'] == 'high'
    assert client.post('/api/darknet/clear').json()['ok']
    assert client.get('/api/darknet').json()['days'] == []


def _can_sniff():
    if os.geteuid() != 0 or not shutil.which('ip') or not shutil.which('iptables'):
        return False
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)).close()
    except (OSError, AttributeError):
        return False
    return subprocess.run(['iptables', '-S', 'INPUT'], capture_output=True).returncode == 0


@pytest.mark.skipif(not _can_sniff(), reason='root・AF_PACKET・iptables が必要')
def test_real_packets_from_the_traffic_tool(client):
    app_module._darknet_init()
    r = client.post('/api/darknet/config', json={'prefixes': ['10.249.250.0/24'], 'sensor': True})
    assert r.status_code == 200, r.text
    s = r.json()['sensor']
    assert s['running'] and s['passive'], s
    route = subprocess.run(['ip', 'route', 'get', '10.249.250.9'], capture_output=True, text=True).stdout
    assert 'local' in route
    for kind in ('scan', 'backscatter', 'reflection'):
        subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'darknet_traffic.py'),
                        '--prefix', '10.249.250.0/24', kind], check=True, capture_output=True, timeout=60)
    time.sleep(1)
    d = client.get('/api/darknet').json()
    cats = d['hour']['cats']
    assert cats['scan'] > 0 and cats['backscatter'] > 0 and cats['reflection'] > 0
    assert any(e['src'] == '127.0.0.66' and e['dport'] in (445, 23) for e in d['events'])
    assert any(e['src'] == '203.0.113.80' and e['proto'] == 'tcp' for e in d['events'])
    # 観測をやめると引き込みも外れる
    client.post('/api/darknet/config', json={'prefixes': []})
    route = subprocess.run(['ip', 'route', 'get', '10.249.250.9'], capture_output=True, text=True).stdout
    assert 'local' not in route
    assert '10.249.250.0/24' not in subprocess.run(['iptables', '-S', 'INPUT'], capture_output=True, text=True).stdout
