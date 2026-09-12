"""
Catalyst/Cisco の snmp-server community 設定 — 追加・権限変更・削除

Nexposeエミュレーションの「設定を直して再スキャンすると所見が消える」
という流れを試していて見つかった穴を塞ぐためのテスト。

見つかった不具合:
  1. `no snmp-server community <name>` が未実装で、一度設定した
     コミュニティを消す手段が無かった（no snmp-server host だけ実装済み）
  2. 既存の名前を再指定しても権限が更新されなかった
     （`if not any(name==...)` で丸ごと無視していた）。実機は上書きする
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient       # noqa: E402

import app as app_module                        # noqa: E402

client = TestClient(app_module.app)
DEV = 't-snmp-comm'


def _cli(*cmds):
    out = ''
    for c in cmds:
        out = client.post('/api/cli',
                          json={'device_id': DEV, 'command': c}).json()['output']
    return out


@pytest.fixture
def state():
    app_module.device_sessions.pop(DEV, None)
    client.post('/api/device',
                json={'id': DEV, 'type': 'catalyst', 'hostname': 'SW1'})
    _cli('configure terminal')
    yield app_module.device_sessions[DEV]
    app_module.device_sessions.pop(DEV, None)


def _names(state):
    return [(c['name'], c['perm']) for c in state.snmp_community]


def test_community_is_added_with_its_permission(state):
    _cli('snmp-server community public ro', 'snmp-server community wr1te rw')
    assert _names(state) == [('public', 'ro'), ('wr1te', 'rw')]


def test_read_only_and_read_write_spellings_are_normalised(state):
    _cli('snmp-server community a read-only', 'snmp-server community b read-write')
    assert _names(state) == [('a', 'ro'), ('b', 'rw')]


def test_case_of_the_community_name_is_preserved(state):
    _cli('snmp-server community MixedCase ro')
    assert _names(state) == [('MixedCase', 'ro')]


def test_reissuing_a_community_updates_its_permission(state):
    """実機は同じ名前を再指定すると権限を上書きする"""
    _cli('snmp-server community public ro')
    _cli('snmp-server community public rw')
    assert _names(state) == [('public', 'rw')]      # 増えず、書き換わる


def test_named_removal(state):
    _cli('snmp-server community public ro', 'snmp-server community wr1te rw')
    _cli('no snmp-server community wr1te')
    assert _names(state) == [('public', 'ro')]


def test_removal_preserves_case(state):
    _cli('snmp-server community MixedCase ro')
    _cli('no snmp-server community MixedCase')
    assert _names(state) == []


def test_removal_with_the_permission_suffix(state):
    _cli('snmp-server community public ro')
    _cli('no snmp-server community public ro')
    assert _names(state) == []


def test_bare_no_removes_every_community(state):
    _cli('snmp-server community a ro', 'snmp-server community b rw')
    _cli('no snmp-server community')
    assert _names(state) == []


def test_removing_an_unknown_community_is_a_no_op(state):
    _cli('snmp-server community public ro')
    _cli('no snmp-server community nope')
    assert _names(state) == [('public', 'ro')]


def test_running_config_follows_the_removal(state):
    _cli('snmp-server community public ro', 'snmp-server community wr1te rw')
    _cli('no snmp-server community wr1te')
    cfg = _cli('end', 'show running-config')
    lines = [l.strip() for l in cfg.splitlines()
             if 'snmp-server community' in l]
    assert any('public' in l for l in lines)
    assert not any('wr1te' in l for l in lines)
