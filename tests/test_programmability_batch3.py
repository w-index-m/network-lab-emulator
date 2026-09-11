"""
プログラマビリティ第3弾のテスト
  - EEM（Embedded Event Manager）
  - アプリケーションホスティング（IOx / Docker）
  - OpenFlow（faucetパイプライン）

EEMの `action N cli command "..."` は表示用のモックではなく、実際に
ルールエンジンへコマンドを流して装置の設定を変える。ここではそれが
本当に効いていることまで確認する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient        # noqa: E402

import app as app_module                         # noqa: E402
from engine.programmability import (             # noqa: E402
    app_hosting_engine, eem_engine, openflow_engine,
)

client = TestClient(app_module.app)


def _mk(dev, hostname=None):
    app_module.device_sessions.pop(dev, None)
    eem_engine.applets.pop(dev, None)
    eem_engine.policies.pop(dev, None)
    eem_engine.history.pop(dev, None)
    app_hosting_engine.apps.pop(dev, None)
    app_hosting_engine.iox.pop(dev, None)
    openflow_engine.nodes.pop(dev, None)
    client.post('/api/device', json={'id': dev, 'type': 'catalyst',
                                     'hostname': hostname or dev})
    return dev


def _cli(dev, cmd):
    return client.post('/api/cli',
                       json={'device_id': dev, 'command': cmd}).json()['output']


def _run(dev, cmds):
    for c in cmds:
        _cli(dev, c)


# ══════════════════════════════════════════
# EEM
# ══════════════════════════════════════════
def _applet(dev='t-eem'):
    _mk(dev, 'EEMSW')
    _run(dev, ['configure terminal', 'event manager applet TEST', 'event none',
               'action 1.0 syslog msg "applet fired"',
               'action 2.0 cli command "hostname RENAMED-BY-EEM"',
               'action 3.0 puts "done"', 'end'])
    return dev


def test_applet_events_and_actions_are_stored():
    dev = _applet()
    ap = eem_engine.applets[dev]['TEST']
    assert [e['type'] for e in ap['events']] == ['none']
    assert [a['type'] for a in ap['actions']] == ['syslog', 'cli', 'puts']


def test_actions_execute_in_sequence_number_order():
    """投入順ではなくシーケンス番号順に実行される"""
    dev = _mk('t-eem-order')
    _run(dev, ['configure terminal', 'event manager applet ORD', 'event none',
               'action 3.0 puts "third"', 'action 1.0 puts "first"',
               'action 2.0 puts "second"', 'end'])
    out = _cli(dev, 'event manager run ORD')
    assert out.splitlines() == ['first', 'second', 'third']


def test_run_applet_actually_changes_device_config():
    """action cli command が本当に装置の設定を変えること"""
    dev = _applet()
    out = _cli(dev, 'event manager run TEST')
    assert '%HA_EM-6-LOG: TEST: applet fired' in out
    assert 'done' in out
    assert app_module.device_sessions[dev].hostname == 'RENAMED-BY-EEM'
    assert 'hostname RENAMED-BY-EEM' in _cli(dev, 'show running-config')


def test_applet_without_none_event_cannot_be_run_manually():
    """event none 以外のappletは手動実行できない（実機同様）"""
    dev = _mk('t-eem-nonone')
    _run(dev, ['configure terminal', 'event manager applet SYS',
               'event syslog pattern "LINK-3-UPDOWN"',
               'action 1.0 puts "x"', 'end'])
    out = _cli(dev, 'event manager run SYS')
    assert 'does not have a "none" event detector' in out


def test_run_unknown_applet():
    dev = _mk('t-eem-unknown')
    assert 'not registered' in _cli(dev, 'event manager run NOPE')


def test_syslog_event_triggers_applet_and_runs_cli():
    dev = _mk('t-eem-syslog', 'SYSLOGSW')
    _run(dev, ['configure terminal', 'event manager applet ONLINK',
               'event syslog pattern "LINK-3-UPDOWN"',
               'action 1.0 cli command "hostname FIRED"', 'end'])
    st = app_module.device_sessions[dev]
    fired = eem_engine.notify_syslog(
        dev, '%LINK-3-UPDOWN: Interface Gi1/0/1, changed state to down',
        cli_exec=lambda cmd: app_module.rule_engine.process(cmd, st))
    assert fired == ['ONLINK']
    assert st.hostname == 'FIRED'


def test_syslog_event_does_not_fire_on_unrelated_message():
    dev = _mk('t-eem-nofire')
    _run(dev, ['configure terminal', 'event manager applet ONLINK',
               'event syslog pattern "LINK-3-UPDOWN"',
               'action 1.0 puts "x"', 'end'])
    assert eem_engine.notify_syslog(dev, '%SYS-5-CONFIG_I: Configured') == []


def test_applet_can_be_removed():
    dev = _applet()
    _run(dev, ['configure terminal', 'no event manager applet TEST', 'end'])
    assert 'TEST' not in eem_engine.applets[dev]


def test_show_policy_registered_lists_applet_and_actions():
    dev = _applet()
    out = _cli(dev, 'show event manager policy registered')
    assert 'applet    user    none' in out
    assert 'TEST' in out
    assert 'syslog msg "applet fired"' in out
    assert 'cli command "hostname RENAMED-BY-EEM"' in out


def test_history_records_run():
    dev = _applet()
    _cli(dev, 'event manager run TEST')
    out = _cli(dev, 'show event manager history events')
    assert 'applet: TEST' in out


def test_python_policy_registration():
    dev = _mk('t-eem-py')
    _run(dev, ['configure terminal',
               'event manager directory user policy flash:/',
               'event manager policy eem_script.py type user', 'end'])
    out = _cli(dev, 'show event manager policy registered')
    assert 'eem_script.py' in out
    assert 'script' in out
    assert 'flash:/' in _cli(dev, 'show event manager directory user')
    avail = _cli(dev, 'show event manager policy available')
    assert 'eem_script.py' in avail


# ══════════════════════════════════════════
# アプリケーションホスティング
# ══════════════════════════════════════════
def _apphost(dev='t-apphost'):
    _mk(dev, 'C9300')
    _run(dev, ['configure terminal', 'iox', 'app-hosting appid syslogng',
               'app-vnic AppGigabitEthernet trunk',
               'vlan 46 guest-interface 0',
               'guest-ipaddress 10.1.1.9 netmask 255.255.255.0',
               'app-default-gateway 10.1.1.3 guest-interface 0',
               'app-resource docker', 'app-resource profile custom',
               'cpu 3700', 'memory 1792', 'persist-disk 200', 'vcpu 1',
               'end'])
    return dev


PKG = 'usbflash1:syslogng/syslogng.tar'


def test_install_requires_iox():
    dev = _mk('t-apphost-noiox')
    out = _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    assert 'IOx is not enabled' in out


def test_lifecycle_deployed_activated_running():
    dev = _apphost()
    assert 'DEPLOYED' in _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    assert 'DEPLOYED' in _cli(dev, 'show app-hosting list')
    assert 'ACTIVATED' in _cli(dev, 'app-hosting activate appid syslogng')
    assert 'RUNNING' in _cli(dev, 'app-hosting start appid syslogng')
    assert 'RUNNING' in _cli(dev, 'show app-hosting list')


def test_lifecycle_reverse():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    _cli(dev, 'app-hosting activate appid syslogng')
    _cli(dev, 'app-hosting start appid syslogng')
    assert 'ACTIVATED' in _cli(dev, 'app-hosting stop appid syslogng')
    assert 'DEPLOYED' in _cli(dev, 'app-hosting deactivate appid syslogng')
    assert 'uninstalled' in _cli(dev, 'app-hosting uninstall appid syslogng')
    assert 'no applications installed' in _cli(dev, 'show app-hosting list')


def test_activate_before_install_is_refused():
    dev = _apphost()
    assert 'not installed' in _cli(dev, 'app-hosting activate appid syslogng')


def test_start_before_activate_is_refused():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    out = _cli(dev, 'app-hosting start appid syslogng')
    assert 'cannot start' in out


def test_uninstall_while_running_is_refused():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    _cli(dev, 'app-hosting activate appid syslogng')
    _cli(dev, 'app-hosting start appid syslogng')
    out = _cli(dev, 'app-hosting uninstall appid syslogng')
    assert 'Deactivate it before uninstalling' in out


def test_install_twice_is_refused():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    assert 'already installed' in _cli(
        dev, f'app-hosting install appid syslogng package {PKG}')


def test_detail_shows_resources_and_network():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    _cli(dev, 'app-hosting activate appid syslogng')
    out = _cli(dev, 'show app-hosting detail appid syslogng')
    assert 'App id                 : syslogng' in out
    assert 'State                  : ACTIVATED' in out
    assert 'Memory               : 1792 MB' in out
    assert 'CPU                  : 3700 units' in out
    assert 'IPv4 address        : 10.1.1.9' in out
    assert 'Network name        : vlan46' in out
    assert 'Docker' in out


def test_detail_of_uninstalled_app():
    dev = _apphost()
    assert 'not installed' in _cli(
        dev, 'show app-hosting detail appid syslogng')


def test_resource_accounting_subtracts_running_apps():
    dev = _apphost()
    _cli(dev, f'app-hosting install appid syslogng package {PKG}')
    _cli(dev, 'app-hosting activate appid syslogng')
    out = _cli(dev, 'show app-hosting resource')
    assert 'Available: 3700(Units)' in out       # 7400 - 3700
    assert 'Available: 256(MB)' in out           # 2048 - 1792


def test_guest_ipaddress_requires_vlan_first():
    dev = _mk('t-apphost-order')
    _run(dev, ['configure terminal', 'iox', 'app-hosting appid x',
               'app-vnic AppGigabitEthernet trunk'])
    out = _cli(dev, 'guest-ipaddress 10.1.1.9 netmask 255.255.255.0')
    assert 'Configure "vlan' in out


# ══════════════════════════════════════════
# OpenFlow
# ══════════════════════════════════════════
def _openflow(dev='t-of'):
    _mk(dev, 'SW-OF')
    _run(dev, ['configure terminal', 'boot mode openflow', 'feature openflow',
               'openflow', 'switch 1 pipeline 1',
               'controller ipv4 192.168.0.91 port 6653 vrf Mgmt-vrf security none',
               'controller ipv4 192.168.0.91 port 6654 vrf Mgmt-vrf security none',
               'datapath-id 0xabcdef1234', 'exit', 'exit', 'end'])
    return dev


def test_feature_requires_openflow_boot_mode():
    dev = _mk('t-of-boot')
    _cli(dev, 'configure terminal')
    out = _cli(dev, 'feature openflow')
    assert 'OpenFlow boot mode' in out
    assert not openflow_engine.feature_enabled(dev)


def test_boot_mode_openflow_asks_for_reload():
    dev = _mk('t-of-reload')
    _cli(dev, 'configure terminal')
    out = _cli(dev, 'boot mode openflow')
    assert 'Reload the switch' in out
    assert openflow_engine.boot_mode(dev) == 'openflow'


def test_openflow_submode_requires_feature():
    dev = _mk('t-of-nofeature')
    _run(dev, ['configure terminal', 'boot mode openflow'])
    assert 'Enable "feature openflow" first' in _cli(dev, 'openflow')


def test_switch_and_controllers_are_configured():
    dev = _openflow()
    sw = openflow_engine.nodes[dev]['switches'][1]
    assert sw['pipeline'] == 1
    assert sw['dpid'] == '0xabcdef1234'
    assert len(sw['controllers']) == 2
    assert sw['controllers'][0]['vrf'] == 'Mgmt-vrf'


def test_exit_from_switch_submode_returns_to_openflow():
    """入れ子サブモードは一段だけ戻る"""
    dev = _mk('t-of-exit')
    _run(dev, ['configure terminal', 'boot mode openflow', 'feature openflow',
               'openflow', 'switch 1 pipeline 1'])
    assert app_module.device_sessions[dev].mode == 'config-openflow-switch'
    _cli(dev, 'exit')
    assert app_module.device_sessions[dev].mode == 'config-openflow'
    _cli(dev, 'exit')
    assert app_module.device_sessions[dev].mode == 'config'


def test_show_openflow_switch():
    dev = _openflow()
    out = _cli(dev, 'show openflow switch 1')
    assert 'Logical Switch Context' in out
    assert 'DPID: 0xabcdef1234' in out
    assert 'Pipeline id: 1' in out
    assert 'Controllers: 2' in out


def test_show_openflow_controllers():
    dev = _openflow()
    out = _cli(dev, 'show openflow switch 1 controllers')
    assert 'Total Controllers: 2' in out
    assert '192.168.0.91:6653' in out
    assert '192.168.0.91:6654' in out
    assert 'Negotiated Protocol Version: OpenFlow 1.3' in out


def test_show_openflow_flows_has_table_miss():
    dev = _openflow()
    out = _cli(dev, 'show openflow switch 1 flows')
    assert 'Total flows: 1' in out
    assert 'Match: any Actions: drop' in out


def test_show_openflow_ports_reflects_interface_state():
    dev = _openflow()
    _run(dev, ['configure terminal', 'interface GigabitEthernet1/0/1',
               'shutdown', 'end'])
    out = _cli(dev, 'show openflow switch 1 ports')
    assert out.startswith('Port    Interface Name')
    gi1 = next(x for x in out.splitlines() if x.split()[1:2] == ['Gi1/0/1'])
    assert 'PORT_DOWN' in gi1 and 'LINK_DOWN' in gi1
    gi2 = next(x for x in out.splitlines() if x.split()[1:2] == ['Gi1/0/2'])
    assert 'PORT_UP' in gi2 and 'LINK_UP' in gi2


def test_show_unknown_openflow_switch():
    dev = _openflow()
    assert 'not configured' in _cli(dev, 'show openflow switch 9')


def test_controller_can_be_removed():
    dev = _openflow()
    _run(dev, ['configure terminal', 'openflow', 'switch 1 pipeline 1',
               'no controller ipv4 192.168.0.91 port 6654', 'end'])
    assert len(openflow_engine.nodes[dev]['switches'][1]['controllers']) == 1


# ══════════════════════════════════════════
# running-config への出力（そのまま投入し直せること）
# ══════════════════════════════════════════
def test_running_config_emits_eem_applet():
    dev = _applet('t-rc-eem')
    out = _cli(dev, 'show running-config')
    assert 'event manager applet TEST' in out
    assert ' event none' in out
    assert ' action 1.0 syslog msg "applet fired"' in out
    assert ' action 2.0 cli command "hostname RENAMED-BY-EEM"' in out


def test_running_config_emits_app_hosting():
    dev = _apphost('t-rc-app')
    out = _cli(dev, 'show running-config')
    assert 'iox' in out
    assert 'app-hosting appid syslogng' in out
    assert ' app-vnic AppGigabitEthernet trunk' in out
    assert '  vlan 46 guest-interface 0' in out
    assert '   guest-ipaddress 10.1.1.9 netmask 255.255.255.0' in out
    assert ' app-default-gateway 10.1.1.3 guest-interface 0' in out
    assert ' app-resource profile custom' in out
    assert '  memory 1792' in out


def test_running_config_emits_openflow():
    dev = _openflow('t-rc-of')
    out = _cli(dev, 'show running-config')
    assert 'feature openflow' in out
    assert 'openflow' in out
    assert ' switch 1 pipeline 1' in out
    assert ('  controller ipv4 192.168.0.91 port 6653 vrf Mgmt-vrf '
            'security none') in out
    assert '  datapath-id 0xabcdef1234' in out
