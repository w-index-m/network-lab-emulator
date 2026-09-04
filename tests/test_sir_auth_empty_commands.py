"""
Si-R の auth/macauth/arpauth/dot1x/snmp statistics コマンドの回帰テスト。

実機 Si-R G110B（2026-09-04、factory-default状態）ではこれらのコマンドは
エラーにならず、認証設定が何もないため出力が空になることを確認した。
エミュレーターは以前、
- show auth/macauth/dot1x port ether 系: "% Invalid input" エラー
- show arpauth statistics/vlan: ダミーのARPテーブルを表示
- show snmp statistics: show snmp と同じ内容を表示
していたが、いずれも実機と食い違っていたため空出力に修正した。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _fresh_sir(device_id: str):
    client.post('/api/device', json={
        'id': device_id, 'type': 'sir', 'hostname': device_id,
    })
    def run(cmd):
        return client.post('/api/cli', json={
            'device_id': device_id, 'command': cmd,
        }).json()['output']
    return run


COMMANDS = [
    'show auth port ether',
    'show auth ethergroup',
    'show macauth port ether',
    'show macauth statistics port ether',
    'show macauth ethergroup',
    'show macauth statistics ethergroup',
    'show arpauth statistics',
    'show arpauth vlan',
    'show dot1x port ether',
    'show dot1x statistics port ether',
    'show snmp statistics',
]


def test_auth_macauth_arpauth_dot1x_snmp_statistics_are_empty_on_default():
    run = _fresh_sir('sir-authcmds-1')
    for cmd in COMMANDS:
        out = run(cmd)
        assert out == "", f"{cmd!r} should be empty, got {out!r}"
