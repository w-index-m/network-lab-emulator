"""
Catalyst/Cisco (IOS-XE) の RESTCONF API。

実機の代表的な有効化手順:
    ip http secure-server
    restconf

有効化後、実機は https://<device-ip>/restconf/data/... でietf-interfaces
等のYANGモデルをJSONで返す。このエミュレータは1プロセスで複数装置を
扱うため、device_idをURLに含める形にしている:
    /restconf/{device_id}/data/ietf-interfaces:interfaces

認証は他のAPIと違いHTTP Basic認証（実機のRESTCONFと同じ方式）を
使う実装だが、認証まわりの実挙動（401 / WWW-Authenticateヘッダー等）は
pytestの同一プロセス内でapp.pyがNETLAB_AUTH_DISABLE=1で読み込まれる
（他のテストファイルとモジュールを共有するため）都合上ここでは検証せず、
実際にuvicornを起動してcurlで確認した内容を
docs/restconf-catalyst.md に記録する。ここではAPIのデータモデル部分
（RESTCONF有効/無効時の応答、ietf-interfacesの内容、PUTでの書き換え）を
確認する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


RESTCONF_SETUP = [
    'configure terminal',
    'ip http secure-server',
    'restconf',
    'interface GigabitEthernet0/1',
    'ip address 10.5.0.1 255.255.255.0',
    'no shutdown',
    'end',
]


class TestRestconfEnable:
    def test_show_restconf_reports_enabled(self):
        _dev('rc-r1')
        _run('rc-r1', RESTCONF_SETUP)
        out = _cli('rc-r1', 'show restconf')
        assert 'Enabled' in out

    def test_running_config_reflects_restconf(self):
        _dev('rc-r2')
        _run('rc-r2', RESTCONF_SETUP)
        out = _cli('rc-r2', 'show running-config')
        assert 'ip http secure-server' in out
        assert 'restconf' in out

    def test_restconf_disabled_by_default(self):
        _dev('rc-r3')
        out = _cli('rc-r3', 'show restconf')
        assert 'Disabled' in out


class TestRestconfApiEnabled:
    def test_get_interfaces_returns_ietf_interfaces_model(self):
        _dev('rc-r5')
        _run('rc-r5', RESTCONF_SETUP)
        r = client.get('/restconf/rc-r5/data/ietf-interfaces:interfaces')
        assert r.status_code == 200
        data = r.json()
        ifaces = data['ietf-interfaces:interfaces']['interface']
        gi01 = next(i for i in ifaces if i['name'] == 'GigabitEthernet0/1')
        assert gi01['enabled'] is True
        assert gi01['ietf-ip:ipv4']['address'][0]['ip'] == '10.5.0.1'

    def test_get_single_interface(self):
        _dev('rc-r6')
        _run('rc-r6', RESTCONF_SETUP)
        r = client.get(
            '/restconf/rc-r6/data/ietf-interfaces:interfaces/interface=GigabitEthernet0/1')
        assert r.status_code == 200
        entry = r.json()['ietf-interfaces:interface']
        assert entry['name'] == 'GigabitEthernet0/1'
        assert entry['enabled'] is True

    def test_get_nonexistent_interface_404(self):
        _dev('rc-r7')
        _run('rc-r7', RESTCONF_SETUP)
        r = client.get(
            '/restconf/rc-r7/data/ietf-interfaces:interfaces/interface=GigabitEthernet9/9')
        assert r.status_code == 404

    def test_put_disables_interface(self):
        _dev('rc-r8')
        _run('rc-r8', RESTCONF_SETUP)
        r = client.put(
            '/restconf/rc-r8/data/ietf-interfaces:interfaces/interface=GigabitEthernet0/1',
            json={'ietf-interfaces:interface': {'enabled': False}})
        assert r.status_code == 200
        assert r.json()['ietf-interfaces:interface']['enabled'] is False
        # CLI側にも反映されていること
        out = _cli('rc-r8', 'show ip interface brief')
        assert 'administratively down' in out.lower()


class TestRestconfNotEnabled:
    def test_api_404_when_restconf_not_configured(self):
        _dev('rc-r9')
        r = client.get('/restconf/rc-r9/data/ietf-interfaces:interfaces')
        assert r.status_code == 404
        assert 'ietf-restconf:errors' in r.json()

    def test_api_404_for_non_cisco_device(self):
        _dev('rc-r10', type_='sir')
        r = client.get('/restconf/rc-r10/data/ietf-interfaces:interfaces')
        assert r.status_code == 404


class TestRestconfDashboardApi:
    """RESTCONFヘルスダッシュボード（/api/restconf/dashboard）"""

    def test_restconf_ready_device_appears_with_counts(self):
        _dev('rc-dash-1')
        _run('rc-dash-1', RESTCONF_SETUP)
        r = client.get('/api/restconf/dashboard')
        assert r.status_code == 200
        data = r.json()
        entry = next(d for d in data['devices'] if d['device_id'] == 'rc-dash-1')
        assert entry['restconf_enabled'] is True
        assert entry['http_secure_server'] is True
        assert entry['interface_with_ip'] >= 1
        assert entry['interface_up'] + entry['interface_down'] == entry['interface_count']

    def test_non_restconf_device_marked_disabled(self):
        _dev('rc-dash-2')
        r = client.get('/api/restconf/dashboard')
        entry = next(d for d in r.json()['devices'] if d['device_id'] == 'rc-dash-2')
        assert entry['restconf_enabled'] is False

    def test_non_cisco_device_excluded(self):
        _dev('rc-dash-3', type_='sir')
        r = client.get('/api/restconf/dashboard')
        ids = [d['device_id'] for d in r.json()['devices']]
        assert 'rc-dash-3' not in ids

    def test_summary_counts_match_device_list(self):
        r = client.get('/api/restconf/dashboard')
        data = r.json()
        assert data['summary']['device_count'] == len(data['devices'])
        assert data['summary']['restconf_ready_count'] == sum(
            1 for d in data['devices'] if d['restconf_enabled'])
