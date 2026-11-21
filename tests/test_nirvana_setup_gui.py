"""
gNMI 設定画面(cyber-view-standalone/setup_gui.py)

- 入力の確かめ: IP 以外・空のパスワード・おかしなポート名を断る(コマンドに混ぜ込ませない)
- Windows では WSL の中で quickstart を動かす: 改行を LF にした本文を標準入力で渡し、パスワードは WSLENV で渡す
- ニルヴァーナの設定: 選んだポートの先に相手を置いてリンクにし、そのまま読める
- 開始の流れ: quickstart が失敗したらニルヴァーナは起動しない、ポートの取り合いには案内を足す、パスワードは保存しない
ネットワークや本物の gnmic には出ない(本物での確認は docs/nirvana-gnmi-via-gnmic.md)。
"""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
import setup_gui as G  # noqa: E402
from cvs import config as C  # noqa: E402


def test_parse_form_accepts_defaults_and_rejects_bad_input():
    f = G.parse_form({'target': '192.168.1.1', 'password': 'pw'})
    assert f == {'targets': ['192.168.1.1'], 'ports': ['Gi1/0/1'], 'user': 'admin', 'password': 'pw'}
    assert G.parse_form({'target': '10.0.0.1, 10.0.0.2:57400', 'ports': 'Gi1/0/1,Te1/1/1', 'password': 'x'})['targets'] \
        == ['10.0.0.1', '10.0.0.2:57400']
    for bad in ({'target': '', 'password': 'x'}, {'target': 'sw1', 'password': 'x'},
                {'target': '10.0.0.1; rm -rf /', 'password': 'x'}, {'target': '10.0.0.1', 'password': ''},
                {'target': '10.0.0.1:99999', 'password': 'x'}, {'target': '10.0.0.1', 'ports': 'Gi1/0/1;id', 'password': 'x'},
                {'target': '10.0.0.1', 'user': 'a b', 'password': 'x'}):
        with pytest.raises(ValueError):
            G.parse_form(bad)


def test_quickstart_cmd_linux_and_windows(monkeypatch):
    form = G.parse_form({'target': '192.168.1.1', 'password': 'secret'})
    monkeypatch.setattr(G, 'on_windows', lambda: False)
    argv, stdin, env = G.quickstart_cmd('up', form)
    assert argv[:2] == ['bash', str(G.QUICKSTART)] and stdin is None
    assert 'secret' not in argv and env['GNMI_PASSWORD'] == 'secret' and env['GNMI_FROM_GUI'] == '1'
    monkeypatch.setattr(G, 'on_windows', lambda: True)
    monkeypatch.setenv('WSLENV', 'FOO')
    argv, stdin, env = G.quickstart_cmd('up', form)
    assert argv[:7] == ['wsl', '--cd', '~', '-e', 'bash', '-s', '--'] and argv[7] == 'up'
    assert isinstance(stdin, bytes) and b'\r' not in stdin and stdin.startswith(b'#!/usr/bin/env bash')
    assert env['WSLENV'].split(':') == ['FOO', 'GNMI_FROM_GUI/u', 'GNMI_PASSWORD/u'] and 'secret' not in argv


def test_nirvana_config_links_each_port_and_loads(tmp_path):
    form = G.parse_form({'target': '192.168.1.1', 'ports': 'Gi1/0/1,Gi1/0/2', 'password': 'x'})
    p = tmp_path / 'n.yaml'
    p.write_text(G.nirvana_config(form), encoding='utf-8')
    cfg = C.load(str(p))
    assert cfg['sources']['prometheus']['flavor'] == 'gnmic'
    sw = next(d for d in cfg['devices'] if d['id'] == 'sw1')
    assert sw['mgmt_ip'] == '192.168.1.1' and [i['name'] for i in sw['interfaces']] == \
        ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2']
    assert [(ln['a'], ln['a_if'], ln['b']) for ln in cfg['links']] == \
        [('sw1', 'GigabitEthernet1/0/1', 'sw1p1'), ('sw1', 'GigabitEthernet1/0/2', 'sw1p2')]
    assert all(d.get('snmp') is False for d in cfg['devices'] if d['id'] != 'sw1')


def test_do_up_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(G, 'STATE_DIR', tmp_path)
    calls = []
    monkeypatch.setattr(G, 'start_nirvana', lambda form: (calls.append('nirvana'), (True, 'started'))[1])
    monkeypatch.setattr(G, 'run_quickstart', lambda a, f=None: (False, 'NG ポート 9090 はすでに使われています'))
    r = G.do_up({'target': '192.168.1.1', 'password': 'pw'})
    assert r['ok'] is False and '手で起動したものを止める' in r['log'] and calls == []
    saved = json.loads((tmp_path / 'last.json').read_text(encoding='utf-8'))
    assert saved == {'target': '192.168.1.1', 'ports': 'Gi1/0/1', 'user': 'admin'}       # パスワードは保存しない
    monkeypatch.setattr(G, 'run_quickstart', lambda a, f=None: (True, 'OK'))
    r = G.do_up({'target': '192.168.1.1', 'password': 'pw'})
    assert r['ok'] and calls == ['nirvana'] and r['nirvana_url'].endswith(':8091/?view=links')
    assert G.do_up({'target': 'x', 'password': 'pw'})['ok'] is False


def test_page_and_launcher(tmp_path):
    html = G.page({'target': '192.168.1.1"><script>', 'ports': 'Gi1/0/1'})
    assert 'id="target"' in html and 'type="password"' in html and '<script>"' not in html
    bat = open(os.path.join(ROOT, 'cyber-view-standalone', 'start-nirvana-gnmi.bat'), 'rb').read()
    assert b'\r\n' in bat and b'python setup_gui.py' in bat and bat.isascii()
    ga = open(os.path.join(ROOT, '.gitattributes'), encoding='utf-8').read()
    assert '*.sh text eol=lf' in ga
