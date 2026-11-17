"""SNMPv3(USM + 簡易 VACM): 鍵の作り方・暗号・設定コマンド・show・エージェントの応答・保存・単体版クライアント

実ソケットは使わず、engine.snmp_v3.process() に本物の v3 メッセージ(engine.snmp_usm で組み立てたもの)を
直接渡す。本物の net-snmp(snmpget -v3)・snmp_exporter・snmpd との相互接続は docs/snmpv3.md に記録。
"""
import os
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as A  # noqa: E402
from engine import snmp_udp_agent, snmp_v3  # noqa: E402
from engine import snmp_usm as usm  # noqa: E402
from engine.protocols import snmp_agent  # noqa: E402

SYSNAME = '1.3.6.1.2.1.1.5.0'


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def _dev(client, typ='cisco'):
    did = f'v3-{typ}-{uuid.uuid4().hex[:6]}'
    client.post('/api/device', json={'id': did, 'type': typ, 'hostname': did.upper()})
    return did


def _cli(client, did, *cmds):
    out = ''
    for c in cmds:
        out = client.post('/api/cli', json={'device_id': did, 'command': c}).json()['output']
    return out


# ── 小さなクライアント(テスト用) ────────────────────────
def _pdu(tag, rid, oids, a2=0, a3=0):
    vbs = b''.join(usm.tlv(0x30, snmp_udp_agent._encode_oid(o) + b'\x05\x00') for o in oids)
    return usm.tlv(tag, usm.ber_int(rid) + usm.ber_int(a2) + usm.ber_int(a3) + usm.tlv(0x30, vbs))


def _ask(did, data):
    return snmp_v3.process(data, did, snmp_agent, snmp_udp_agent)


def _engine(did):
    rep = usm.parse_message(_ask(did, usm.discovery_message(1, 1)))
    return rep['engine_id'], rep['boots'], rep['time']


def _get(did, user, oids, tag=0xA0, a2=0, a3=0, etime=None, boots=None):
    eid, b, t = _engine(did)
    ak, pk = user.keys(eid)
    msg = usm.build_message(7, user.flags | usm.FLAG_REPORTABLE, eid, b if boots is None else boots,
                            t if etime is None else etime, user.user.encode(),
                            usm.scoped_pdu(eid, b'', _pdu(tag, 42, oids, a2, a3)),
                            auth=user.auth, akey=ak, priv=user.priv, pkey=pk)
    data = _ask(did, msg)
    p, sp = usm.open_message(data, user)
    _c, _n, tag, body = usm.parse_scoped_pdu(sp)
    return p, tag, body


def _report_oid(body):
    _t, s, e = usm.read_tlv(body, 0)
    _t, s, e = usm.read_tlv(body, e)
    _t, s, e = usm.read_tlv(body, e)
    _t, vs, ve = usm.read_tlv(body, e)
    _t, bs, be = usm.read_tlv(body, vs)
    _t, os_, oe = usm.read_tlv(body, bs)
    return snmp_v3._dec_oid(body[os_:oe])


def _err(body):
    _t, s, e = usm.read_tlv(body, 0)
    _t, s, e2 = usm.read_tlv(body, e)
    return int.from_bytes(body[s:e2], 'big')


# ── 鍵・暗号 ─────────────────────────────────────────
def test_rfc3414_key_vectors():
    eid = bytes.fromhex('000000000000000000000002')
    assert usm.password_to_key('md5', 'maplesyrup').hex() == '9faf3283884e92834ebc9847d8edd963'
    assert usm.auth_key('md5', 'maplesyrup', eid).hex() == '526f5eed9fcce26f8964c2930787d82b'
    assert usm.auth_key('sha', 'maplesyrup', eid).hex() == '6695febc9288e36282235fc7151f128497b38f3f'


@pytest.mark.parametrize('priv', ['des', 'aes128', 'aes192', 'aes256', 'aes192c', 'aes256c'])
def test_encrypt_roundtrip(priv):
    eid = bytes.fromhex('800000090300AABBCCDDEEFF')
    k = usm.priv_key('sha', priv, 'Priv-Pass1', eid)
    assert len(k) == usm.PRIV[priv]
    plain = b'\x30\x05scoped-pdu-bytes'
    ct, salt = usm.encrypt(priv, k, 3, 1234, plain)
    assert ct != plain
    assert usm.decrypt(priv, k, 3, 1234, salt, ct)[:len(plain)] == plain


def test_reeder_and_blumenthal_differ():
    """AES-256 の鍵の伸ばし方は2通り(Cisco = Reeder、net-snmp 既定 = Blumenthal)"""
    eid = bytes.fromhex('800000090300AABBCCDDEEFF')
    a = usm.priv_key('sha', 'aes256', 'Priv-Pass1', eid)
    c = usm.priv_key('sha', 'aes256c', 'Priv-Pass1', eid)
    assert a[:20] == c[:20] and a != c


# ── 設定・show ────────────────────────────────────────
def test_ios_cli_show_and_running_config(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'snmp-server view NMSVIEW iso included',
         'snmp-server view NMSVIEW 1.3.6.1.2.1.4 excluded', 'snmp-server group NMS v3 priv read NMSVIEW',
         'snmp-server user nms NMS v3 auth sha Auth-Pass1 priv aes 128 Priv-Pass1', 'end')
    st = A.device_sessions[did]
    u = st.snmp_v3['users']['nms']
    assert u['auth'] == 'sha' and u['priv'] == 'aes128' and len(u['auth_key']) == 40
    assert 'Auth-Pass1' not in repr(st.snmp_v3)                 # パスワードは保存しない
    out = _cli(client, did, 'show snmp user')
    assert 'User name: nms' in out and 'Authentication Protocol: SHA' in out and 'Privacy Protocol: AES128' in out
    assert 'security model:v3 priv' in _cli(client, did, 'show snmp group')
    assert _cli(client, did, 'show snmp engineID').startswith('Local SNMP engineID: 80000009')
    rc = _cli(client, did, 'show running-config')
    assert 'snmp-server group NMS v3 priv read NMSVIEW' in rc
    assert 'snmp-server view NMSVIEW 1.3.6.1.2.1.4 excluded' in rc
    assert 'snmp-server user' not in rc                            # IOS は user を running-config に出さない


def test_ios_rejects_3des_and_warns_missing_group(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal')
    assert '3DES' in _cli(client, did, 'snmp-server user a G v3 auth sha Auth-Pass1 priv 3des Priv-Pass1')
    assert 'Warning' in _cli(client, did, 'snmp-server user a NOGROUP v3 auth sha Auth-Pass1')


def test_nxos_user_localizedkey_roundtrip(client):
    did = _dev(client, 'nexus')
    _cli(client, did, 'configure terminal',
         'snmp-server user admin network-admin auth sha-256 Auth-Pass1 priv aes-128 Priv-Pass1', 'end')
    rc = _cli(client, did, 'show running-config')
    line = next(ln for ln in rc.splitlines() if ln.startswith('snmp-server user admin'))
    assert ' auth sha-256 0x' in line and ' priv aes-128 0x' in line and line.endswith('localizedkey')
    keys = dict(A.device_sessions[did].snmp_v3['users']['admin'])
    # running-config の行をそのまま入れ直しても同じ鍵になる(実機の localizedkey と同じ扱い)
    _cli(client, did, 'configure terminal', 'no snmp-server user admin', line, 'end')
    again = A.device_sessions[did].snmp_v3['users']['admin']
    assert again['auth_key'] == keys['auth_key'] and again['priv_key'] == keys['priv_key']
    assert 'sha256' in _cli(client, did, 'show snmp user')


# ── エージェントの応答 ─────────────────────────────────
@pytest.fixture(scope='module')
def ios(client):
    did = _dev(client)
    _cli(client, did, 'configure terminal', 'snmp-server view NOIP iso included',
         'snmp-server view NOIP 1.3.6.1.2.1.4 excluded', 'snmp-server group NMS v3 priv read NOIP',
         'snmp-server group RO v3 auth',
         'snmp-server user nms NMS v3 auth sha Auth-Pass1 priv aes 128 Priv-Pass1',
         'snmp-server user c256 NMS v3 auth sha Auth-Pass1 priv aes 256 Priv-Pass1',
         'snmp-server user d NMS v3 auth md5 Auth-Pass1 priv des Priv-Pass1',
         'snmp-server user ro RO v3 auth sha Auth-Pass1', 'end')
    return did


def test_discovery_report_gives_engine_id(ios):
    eid, boots, _t = _engine(ios)
    assert eid.hex().upper() == A.device_sessions[ios].snmp_v3['engine_id'] and boots == 1


@pytest.mark.parametrize('user', [
    usm.V3User('nms', 'sha', 'Auth-Pass1', 'aes', 'Priv-Pass1'),
    usm.V3User('c256', 'sha', 'Auth-Pass1', 'aes256c', 'Priv-Pass1'),
    usm.V3User('d', 'md5', 'Auth-Pass1', 'des', 'Priv-Pass1'),
    usm.V3User('ro', 'sha', 'Auth-Pass1'),
])
def test_get_sysname(ios, user):
    p, tag, body = _get(ios, user, [SYSNAME])
    assert tag == 0xA2 and _err(body) == 0
    assert A.device_sessions[ios].hostname.encode() in body
    assert p['flags'] & usm.FLAG_AUTH and bool(p['flags'] & usm.FLAG_PRIV) == bool(user.priv)


def test_wrong_password_unknown_user_and_level(ios):
    _p, tag, body = _get(ios, usm.V3User('nobody', 'sha', 'Auth-Pass1'), [SYSNAME])
    assert tag == 0xA8 and _report_oid(body) == usm.OID_UNKNOWN_USER_NAMES
    # 誤ったパスワード: wrongDigests の Report(認証なしで返る)
    p, tag, body = _get(ios, usm.V3User('nms', 'sha', 'Wrong-Pass1', 'aes', 'Priv-Pass1'), [SYSNAME])
    assert tag == 0xA8 and _report_oid(body) == usm.OID_WRONG_DIGESTS and not p['flags'] & usm.FLAG_AUTH
    _p, tag, body = _get(ios, usm.V3User('ro', 'sha', 'Auth-Pass1', 'aes', 'Priv-Pass1'), [SYSNAME])
    assert tag == 0xA8 and _report_oid(body) == usm.OID_UNSUPPORTED_SEC_LEVELS
    # priv のグループに auth だけで来たら authorizationError(16)
    _p, tag, body = _get(ios, usm.V3User('nms', 'sha', 'Auth-Pass1'), [SYSNAME])
    assert tag == 0xA2 and _err(body) == 16


def test_time_window_report_is_authenticated(ios):
    user = usm.V3User('nms', 'sha', 'Auth-Pass1', 'aes', 'Priv-Pass1')
    p, tag, body = _get(ios, user, [SYSNAME], etime=10 ** 6)
    assert tag == 0xA8 and _report_oid(body) == usm.OID_NOT_IN_TIME_WINDOWS
    assert p['flags'] & usm.FLAG_AUTH                    # 時刻合わせに使えるよう認証つき


def test_view_excludes_ip_subtree(ios):
    user = usm.V3User('nms', 'sha', 'Auth-Pass1', 'aes', 'Priv-Pass1')
    _p, tag, body = _get(ios, user, ['1.3.6.1.2.1.4.20.1.1'], tag=0xA5, a3=30)
    assert b'\x2b\x06\x01\x02\x01\x04' not in body            # ip(1.3.6.1.2.1.4)以下は返らない
    _p, tag, body = _get(ios, usm.V3User('ro', 'sha', 'Auth-Pass1'), ['1.3.6.1.2.1.4.20.1.1'], tag=0xA1)
    assert b'\x2b\x06\x01\x02\x01\x04' in body                 # view 指定なしのグループは読める


def test_v2c_still_works_beside_v3(ios):
    assert snmp_agent.get(ios, SYSNAME, 'public')[2] == A.device_sessions[ios].hostname


def test_persistence_roundtrip(client, ios, tmp_path, monkeypatch):
    path = tmp_path / 'saved.json'
    monkeypatch.setattr(A, 'SAVED_CONFIG_PATH', path)
    A._save_config()
    text = path.read_text()
    assert 'Auth-Pass1' not in text and 'snmp_v3' in text
    saved = {k: v for k, v in A.device_sessions.items()}
    monkeypatch.setattr(A, 'device_sessions', {})
    A._load_config()
    assert A.device_sessions[ios].snmp_v3['users']['nms']['auth_key'] == saved[ios].snmp_v3['users']['nms']['auth_key']


# ── 単体版クライアント ─────────────────────────────────
def test_standalone_client_v3_against_agent(ios, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
    from cvs import snmp_client as SC
    did = ios

    class FakeSock:
        def __init__(self, *a):
            self.q = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def sendto(self, msg, addr):
            r = _ask(did, msg)
            if r:
                self.q.append(r)

        def recvfrom(self, n):
            import socket
            if not self.q:
                raise socket.timeout()
            return self.q.pop(0), ('x', 161)

    monkeypatch.setattr(SC.socket, 'socket', FakeSock)
    SC._ENGINES.clear()
    u = SC.v3_user({'user': 'nms', 'auth': 'sha', 'auth_password': 'Auth-Pass1', 'priv': 'aes', 'priv_password': 'Priv-Pass1'})
    assert SC.get('192.0.2.1', u, [SYSNAME])[SYSNAME] == A.device_sessions[did].hostname
    assert SC.walk('192.0.2.1', u, '1.3.6.1.2.1.31.1.1.1.1')
    bad = SC.v3_user({'user': 'nms', 'auth': 'sha', 'auth_password': 'Bad-Pass1', 'priv': 'aes', 'priv_password': 'x' * 8})
    with pytest.raises(SC.SnmpError):
        SC.get('192.0.2.1', bad, [SYSNAME])
