"""SNMP の TRAP 送信先(実機どおりの並び・trap-source)と、SNMP の送信元制限(community / v3 の ACL)

- `snmp-server host <ip> traps version 2c <community> udp-port <n>`(実機 IOS / NX-OS の並び)で
  送信先ポートが効く。以前は udp-port を IP の直後にしか読まず、実機どおりに書くと 162 へ送っていた
- trap-source の IF の IP から送り、linkDown に ifIndex / ifDescr を付ける(ニルヴァーナが装置と IF を見分ける)
- `snmp-server community X RO <ACL>` / NX-OS `use-ipv4acl`、v3 の `group ... access <ACL>` /
  NX-OS `snmp-server user X use-ipv4acl` で、許可していない送信元からの要求を黙って捨てる
- NX-OS の `ip access-list <名前>` が ACL の設定モードに入り、中の行が登録される
- iac/nirvana-monitoring が TRAP を ニルヴァーナ(1162)へ送り、ニルヴァーナがそれを受ける設定になっている

本物の net-snmp / snmp_exporter / docker の IaC での確認は docs/nirvana-monitoring-iac.md に記録。
"""
import os
import socket
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as A  # noqa: E402
from engine import snmp_udp_agent, snmp_v3  # noqa: E402
from engine import snmp_usm as usm  # noqa: E402
from engine.link_evidence import decode_trap  # noqa: E402
from engine.protocols import snmp_agent  # noqa: E402
from engine.syslog_sender import build_snmp_v2c_trap, send_snmp_trap  # noqa: E402

IAC = os.path.join(ROOT, 'iac', 'nirvana-monitoring')
SYSNAME = '1.3.6.1.2.1.1.5.0'


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def _dev(client, typ='cisco'):
    did = f'tacl-{typ}-{uuid.uuid4().hex[:6]}'
    client.post('/api/device', json={'id': did, 'type': typ, 'hostname': did.upper()})
    return did


def _cli(client, did, *cmds):
    out = ''
    for c in cmds:
        out = client.post('/api/cli', json={'device_id': did, 'command': c}).json()['output']
    return out


# ── TRAP の送信先 ──────────────────────────────────────
def test_trap_host_ios_order_udp_port(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal',
         'snmp-server host 10.9.9.9 traps version 2c Netlab-RO udp-port 1162',
         'snmp-server host 10.9.9.8 version 2c other',
         'snmp-server host 10.9.9.7 informs version 3 priv v3user udp-port 2162', 'end')
    hosts = {h['host']: h for h in A.device_sessions[did].snmp_hosts}
    assert hosts['10.9.9.9']['port'] == 1162 and hosts['10.9.9.9']['community'] == 'Netlab-RO'
    assert hosts['10.9.9.8']['port'] == 162 and hosts['10.9.9.8']['community'] == 'other'
    assert hosts['10.9.9.7'] | {} and hosts['10.9.9.7']['traps'] == 'informs'
    assert hosts['10.9.9.7']['version'] == '3' and hosts['10.9.9.7']['community'] == 'v3user'
    rc = _cli(client, did, 'show running-config')
    assert 'snmp-server host 10.9.9.9 traps version 2c Netlab-RO udp-port 1162' in rc
    assert 'snmp-server host 10.9.9.8 traps version 2c other\n' in rc
    assert 'snmp-server host 10.9.9.7 informs version 3 priv v3user udp-port 2162' in rc


def test_trap_host_legacy_order_still_works(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'snmp-server host 10.9.9.9 udp-port 1163 traps public', 'end')
    h = A.device_sessions[did].snmp_hosts[0]
    assert (h['port'], h['community']) == (1163, 'public')


def test_no_trap_host_stops_sending(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'snmp-server host 10.9.9.9 traps version 2c x udp-port 1162',
         'no snmp-server host 10.9.9.9', 'end')
    assert A.snmp_dispatcher._targets.get(did) == []


def test_trap_source_and_if_varbinds(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.78.0.1 255.255.255.0', 'exit',
         'snmp-server trap-source GigabitEthernet0/0/0', 'end')
    st = A.device_sessions[did]
    meta = A._trap_meta(did, st, 'GigabitEthernet0/0/1')
    assert meta['source_ip'] == '10.78.0.1'
    assert meta['iface'] == 'GigabitEthernet0/0/1' and meta['if_index'] >= 1
    assert 'snmp-server trap-source GigabitEthernet0/0/0' in _cli(client, did, 'show running-config')
    pkt = build_snmp_v2c_trap('c', st.hostname, '1.3.6.1.6.3.1.1.5.3', 'GigabitEthernet0/0/1 down',
                              meta['iface'], meta['if_index'])
    info = decode_trap(pkt)
    assert info['trap_oid'] == '1.3.6.1.6.3.1.1.5.3' and info['ifname'] == 'GigabitEthernet0/0/1'


def test_trap_sent_from_source_ip_to_udp_port():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0))
    rx.settimeout(3)
    port = rx.getsockname()[1]
    try:
        assert send_snmp_trap('127.0.0.1', port, 'netlab-ro', 'R1', '1.3.6.1.6.3.1.1.5.3', 'Gi0/0/1 down',
                              source_ip='127.0.0.2', iface='GigabitEthernet0/0/1', if_index=2)
        data, addr = rx.recvfrom(4096)
    finally:
        rx.close()
    assert addr[0] == '127.0.0.2'
    assert decode_trap(data)['descr'] == 'R1: Gi0/0/1 down'


# ── SNMP の送信元制限 ──────────────────────────────────
def _v2c_get(community, oid=SYSNAME):
    enc = snmp_udp_agent
    vb = enc._tlv(0x30, enc._encode_oid(oid) + b'\x05\x00')
    pdu = enc._tlv(0xA0, enc._encode_int(5) + enc._encode_int(0) + enc._encode_int(0) + enc._tlv(0x30, vb))
    return enc._tlv(0x30, enc._encode_int(1) + enc._tlv(0x04, community.encode()) + pdu)


class _T:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)


def _ask_v2c(did, community, src):
    p = snmp_udp_agent.SnmpDeviceProtocol(did, snmp_agent)
    p.transport = _T()
    p.datagram_received(_v2c_get(community), (src, 40000))
    return bool(p.transport.sent)


def test_v2c_community_acl_ios(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'access-list 31 permit host 10.79.0.50',
         'snmp-server community Lab-RO RO 31', 'end')
    assert 'snmp-server community Lab-RO RO 31' in _cli(client, did, 'show running-config')
    assert _ask_v2c(did, 'Lab-RO', '10.79.0.50')
    assert not _ask_v2c(did, 'Lab-RO', '10.79.0.60')
    # ACL を外せば誰からでも読める(実機と同じ)
    _cli(client, did, 'configure terminal', 'snmp-server community Lab-RO RO', 'end')
    assert _ask_v2c(did, 'Lab-RO', '10.79.0.60')


def test_v2c_acl_name_not_defined_is_permissive(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'snmp-server community Lab-RO RO 99', 'end')
    assert _ask_v2c(did, 'Lab-RO', '10.79.0.60')


def test_nxos_ip_access_list_and_use_ipv4acl(client):
    did = _dev(client, 'nexus')
    _cli(client, did, 'configure terminal', 'ip access-list SNMP-ALLOW')
    assert A.device_sessions[did].mode == 'config-ext-nacl'
    _cli(client, did, '10 permit ip 10.79.1.0/24 any', 'exit')
    assert A.device_sessions[did].mode == 'config'
    _cli(client, did, 'snmp-server community nx-ro group network-operator',
         'snmp-server community nx-ro use-ipv4acl SNMP-ALLOW', 'end')
    comm = A.device_sessions[did].snmp_community[0]
    assert comm == {'name': 'nx-ro', 'perm': 'ro', 'acl': 'SNMP-ALLOW'}
    assert _ask_v2c(did, 'nx-ro', '10.79.1.9')
    assert not _ask_v2c(did, 'nx-ro', '10.79.2.9')


def _v3_get(did, user, src):
    rep = usm.parse_message(snmp_v3.process(usm.discovery_message(1, 1), did, snmp_agent, snmp_udp_agent,
                                            src_ip=src) or b'')
    eid, b, t = rep['engine_id'], rep['boots'], rep['time']
    ak, pk = user.keys(eid)
    vb = usm.tlv(0x30, snmp_udp_agent._encode_oid(SYSNAME) + b'\x05\x00')
    pdu = usm.tlv(0xA0, usm.ber_int(9) + usm.ber_int(0) + usm.ber_int(0) + usm.tlv(0x30, vb))
    msg = usm.build_message(7, user.flags | usm.FLAG_REPORTABLE, eid, b, t, user.user.encode(),
                            usm.scoped_pdu(eid, b'', pdu), auth=user.auth, akey=ak, priv=user.priv, pkey=pk)
    return snmp_v3.process(msg, did, snmp_agent, snmp_udp_agent, src_ip=src)


def test_v3_group_access_acl_ios(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'access-list 32 permit host 10.79.3.50',
         'snmp-server group G3 v3 priv access 32',
         'snmp-server user u3 G3 v3 auth sha Auth-Pass1 priv aes 128 Priv-Pass1', 'end')
    user = usm.V3User('u3', auth='sha', auth_password='Auth-Pass1', priv='aes128', priv_password='Priv-Pass1')
    assert _v3_get(did, user, '10.79.3.50') is not None
    assert _v3_get(did, user, '10.79.3.60') is None


def test_v3_nxos_user_use_ipv4acl(client):
    did = _dev(client, 'nexus')
    _cli(client, did, 'configure terminal', 'ip access-list V3-ALLOW', '10 permit ip 10.79.4.50/32 any', 'exit',
         'snmp-server user nxu network-operator auth sha Auth-Pass1 priv aes-128 Priv-Pass1',
         'snmp-server user nxu use-ipv4acl V3-ALLOW', 'end')
    rc = _cli(client, did, 'show running-config')
    assert 'snmp-server user nxu use-ipv4acl V3-ALLOW' in rc
    assert 'ipv4:V3-ALLOW' in _cli(client, did, 'show snmp user')
    user = usm.V3User('nxu', auth='sha', auth_password='Auth-Pass1', priv='aes128', priv_password='Priv-Pass1')
    assert _v3_get(did, user, '10.79.4.50') is not None
    assert _v3_get(did, user, '10.79.4.51') is None


# ── IaC ───────────────────────────────────────────────
def test_iac_trap_path_and_acl():
    lab = open(os.path.join(IAC, 'lab', 'provision_nirvana_lab.sh'), encoding='utf-8').read()
    cfg = yaml.safe_load(open(os.path.join(IAC, 'nirvana', 'config.yaml'), encoding='utf-8'))
    port = int(cfg['sources']['snmp_trap']['listen'].rsplit(':', 1)[1])
    assert lab.count(f'traps version 2c netlab-ro udp-port {port}') == 2       # CR1/CR2 のループと NX1
    assert 'snmp-server community netlab-ro RO 20' in lab and 'use-ipv4acl SNMP-ALLOW' in lab
    dep = open(os.path.join(IAC, 'deploy.sh'), encoding='utf-8').read()
    assert '  check)' in dep and 'TRAP SYSLOG POLL' in dep
