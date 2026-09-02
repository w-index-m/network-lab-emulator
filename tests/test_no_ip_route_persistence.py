"""
"no ip route" が rib_engine 側だけでなく state.static_routes 側からも
正しく削除されることのテスト（インターフェースイベント等で
rib_engineへ再同期された際に消したはずの経路が復活するバグの回帰テスト）。
"""

import os
import sys
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client():
    return TestClient(app_module.app)


def _cli(client, device_id, command):
    r = client.post('/api/cli', json={'device_id': device_id, 'command': command})
    assert r.status_code == 200
    return r.json()


def test_no_ip_route_removes_from_state_static_routes_too(client):
    _cli(client, 'catalyst', 'configure terminal')
    _cli(client, 'catalyst', 'ip route 10.77.0.0 255.255.255.0 10.9.9.2')
    _cli(client, 'catalyst', 'end')

    state = app_module.device_sessions['catalyst']
    assert any(r.get('dest') == '10.77.0.0' for r in state.static_routes)

    _cli(client, 'catalyst', 'configure terminal')
    _cli(client, 'catalyst', 'no ip route 10.77.0.0 255.255.255.0 10.9.9.2')
    _cli(client, 'catalyst', 'end')

    # rib_engine側から消えていること
    out = _cli(client, 'catalyst', 'show ip route static')['output']
    assert '10.77.0.0' not in out

    # state.static_routes 側（rules.py が使う別リスト）からも消えていないと
    # インターフェースイベント等の再登録処理でrib_engineに復活してしまう
    assert not any(r.get('dest') == '10.77.0.0' for r in state.static_routes)


def test_no_ip_route_does_not_resurrect_after_interface_event(client):
    """no ip route削除後にinterfaceコマンド(再登録トリガー)を挟んでも
    経路が復活しないことを確認する（実際のバグ再現シナリオ）"""
    _cli(client, 'catalyst', 'configure terminal')
    _cli(client, 'catalyst', 'ip route 10.78.0.0 255.255.255.0 10.9.9.2')
    _cli(client, 'catalyst', 'no ip route 10.78.0.0 255.255.255.0 10.9.9.2')
    # インターフェースへの出入りが _register_icmp 相当の再登録処理を誘発する
    _cli(client, 'catalyst', 'interface GigabitEthernet1/0/1')
    _cli(client, 'catalyst', 'no shutdown')
    _cli(client, 'catalyst', 'end')

    out = _cli(client, 'catalyst', 'show ip route static')['output']
    assert '10.78.0.0' not in out
