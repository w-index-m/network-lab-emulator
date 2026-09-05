"""
回帰テスト: APRESIAの "config ipif System <ip>/<prefix>" が
CLI略称展開("config"→"configure")で壊されていたバグの修正。

SNMP動的エージェント起動の検証(SR-S/APRESIAでのsnmpwalk確認)中に発見。
Cisco IOS向けの略称展開テーブルは先頭トークンの"config"を無条件で
"configure"に展開するため、APRESIA独自の"config ipif ..."コマンドの
正規表現(先頭が厳密に"config"を要求)にマッチしなくなり、管理IPが
一切設定できなくなっていた。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _fresh_apresia(device_id: str):
    client.post('/api/device', json={
        'id': device_id, 'type': 'apresia', 'hostname': device_id,
    })

    def run(cmd):
        return client.post('/api/cli', json={
            'device_id': device_id, 'command': cmd,
        }).json()['output']
    return run


def test_config_ipif_sets_the_management_ip():
    run = _fresh_apresia('apresia-ipif-1')
    run('configure terminal')
    run('config ipif System 100.64.90.1/30')
    out = run('show ip interface brief')
    assert '100.64.90.1/30' in out
    assert '192.168.10.1' not in out


def test_config_short_form_also_works():
    """_apresia_process自体は 'config'/'conf' も直接受け付けるので、
    展開をスキップしても configure terminal 相当の動作は変わらないことを確認。"""
    run = _fresh_apresia('apresia-ipif-2')
    run('config')  # "configure terminal" の省略形
    out = run('config ipif System 100.64.93.1/29')
    assert out == ''  # エラーにならない
    assert '100.64.93.1/29' in run('show ip interface brief')
