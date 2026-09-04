"""
engine/snmp_udp_agent.py テスト

BER符号化/復号ロジック（SNMPv2c GET/GETNEXT/GETBULKリクエストの解析、
レスポンスの組み立て）を検証する。実UDPソケットは使わず、
エンコード→デコードの往復とバイト列の妥当性を確認する。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.snmp_udp_agent import (
    _encode_oid, _decode_oid, _encode_int, _decode_int,
    _encode_value, decode_request, encode_response,
    PDU_GET, PDU_GETNEXT, PDU_GETBULK, TAG_NO_SUCH_OBJECT, TAG_END_OF_MIB_VIEW,
)


def test_oid_encode_decode_roundtrip():
    for oid in ['1.3.6.1.2.1.1.1.0', '1.3.6.1.4.1.9.9.109.1.1.1.1.3.1', '1.3.6.1.2.1.2.2.1.10.1']:
        encoded = _encode_oid(oid)
        # encoded = tag(1) + len(1) + content
        content = encoded[2:]
        decoded = _decode_oid(content)
        assert decoded == oid


def test_int_encode_decode_roundtrip():
    for n in [0, 1, 127, 128, 255, 256, 65535, 100000]:
        encoded = _encode_int(n)
        content = encoded[2:]
        assert _decode_int(content) == n


def test_encode_value_string():
    v = _encode_value('STRING', 'Dist-SW')
    assert v[0] == 0x04  # OCTET STRING tag
    assert v[2:] == b'Dist-SW'


def test_encode_value_gauge32():
    v = _encode_value('Gauge32', 42)
    assert v[0] == 0x42  # Gauge32 application tag


def _build_get_request(oid: str, request_id: int = 1) -> bytes:
    """テスト用に簡易的なSNMPv2c GetRequestパケットを手で組み立てる"""
    from engine.snmp_udp_agent import _tlv, TAG_SEQUENCE, TAG_INTEGER, TAG_OCTET_STRING, TAG_NULL

    oid_bytes = _encode_oid(oid)
    null_bytes = _tlv(TAG_NULL, b'')
    varbind = _tlv(TAG_SEQUENCE, oid_bytes + null_bytes)
    vbl = _tlv(TAG_SEQUENCE, varbind)
    pdu_body = _encode_int(request_id) + _encode_int(0) + _encode_int(0) + vbl
    pdu = _tlv(PDU_GET, pdu_body)
    msg_body = _encode_int(1) + _tlv(TAG_OCTET_STRING, b'public') + pdu
    return _tlv(TAG_SEQUENCE, msg_body)


def test_decode_request_parses_get():
    pkt = _build_get_request('1.3.6.1.2.1.1.1.0', request_id=42)
    version, community, pdu_tag, request_id, p2, p3, oids = decode_request(pkt)
    assert version == 1  # SNMPv2c
    assert community == 'public'
    assert pdu_tag == PDU_GET
    assert request_id == 42
    assert oids == ['1.3.6.1.2.1.1.1.0']


def test_encode_response_contains_value():
    response = encode_response(
        version=1, community='public', request_id=1,
        varbinds=[('1.3.6.1.2.1.1.1.0', 'STRING', 'Cisco IOS XE Software')],
    )
    assert response[0] == 0x30  # outer SEQUENCE
    assert b'Cisco IOS XE Software' in response


def test_encode_response_no_such_object_for_missing_oid():
    response = encode_response(
        version=1, community='public', request_id=1,
        varbinds=[('1.3.6.1.2.1.1.99.0', None, None)],
    )
    assert bytes([TAG_NO_SUCH_OBJECT]) in response


def test_encode_response_end_of_mib_view():
    response = encode_response(
        version=1, community='public', request_id=1,
        varbinds=[('1.3.6.1.2.1.1.99.0', 'ENDOFMIBVIEW', None)],
    )
    assert bytes([TAG_END_OF_MIB_VIEW]) in response
