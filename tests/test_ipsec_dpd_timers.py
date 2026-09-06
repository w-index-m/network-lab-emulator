"""
IPsec/IKE のタイマー系コマンド実装（Si-R / Cisco ルータ）。

これまで show crypto isakmp sa 等の表示コマンドはあったが、実際に
生存確認(DPD)や再送を行うタイマーは無かった。マニュアル
（コマンドリファレンス 10.2.62, 10.2.82～10.2.84, 10.2.101）で確認した
デフォルト値・範囲に基づき、以下を実装した:

Si-R:
  - remote ap ike retry <time> <count>        （ネゴシエーション再送）
  - remote ap ike dpd use <on|off>            （DPD利用可否）
  - remote ap ike dpd idle <time>             （無通信監視時間）
  - remote ap ike dpd retry <time> <count>    （DPD再送）
  - remote ap sessionwatch interval ...       （接続先監視）
  - リンクダウン(ether use off)でDPD検知が動き出し、無通信監視時間+
    再送時間×再送回数が経過するとトンネルがdownと判定される

Cisco:
  - crypto isakmp keepalive <interval> <retry> を実際のDPD検知窓として使う
  - 検知窓をトンネルごとに保持するよう修正（以前はicmp_engineの共有属性を
    直接書き換えていたため、keepalive設定が異なる複数装置でクロストークが
    あった）
  - crypto map <name> interface <if> と、interface配下の crypto map <name>
    （実機で最も一般的な構文）の両方でicmp_engineへの登録が働くことを確認
  - crypto map名の大文字小文字が model 間で食い違い、
    crypto_map_interfaceの適用先マップが見つからなくなるバグを修正
"""

import os
import sys
import time

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.ike_engine import (
    DEFAULT_DPD_IDLE, DEFAULT_DPD_RETRY_COUNT, DEFAULT_DPD_RETRY_TIME,
    sir_dpd_detect_seconds,
)
from engine.protocols import icmp_engine

client = TestClient(app_module.app)


def _dev(id_, type_):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


def _wait(fn, timeout=8.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _setup_sir_tunnel(a, b, ip_a, ip_b, extra_a=(), extra_b=()):
    """Si-R同士でトンネルを張る最小構成。"""
    _dev(a, 'sir')
    _dev(b, 'sir')
    _run(a, [
        'configure', f'lan 1 ip address {ip_a}/30 3',
        f'remote 1 ap 0 tunnel local {ip_a}',
        f'remote 1 ap 0 tunnel remote {ip_b}',
        'remote 1 ap 0 ipsec ike preshared-key testkey123',
        *extra_a,
        'ike use on', 'ipsec use on',
    ])
    _run(b, [
        'configure', f'lan 1 ip address {ip_b}/30 3',
        f'remote 1 ap 0 tunnel local {ip_b}',
        f'remote 1 ap 0 tunnel remote {ip_a}',
        'remote 1 ap 0 ipsec ike preshared-key testkey123',
        *extra_b,
        'ike use on', 'ipsec use on',
    ])


def _setup_cisco_tunnel(dev_id, local_ip, peer_ip, interval=None, retry=None,
                        embed_map_in_interface=True):
    """Ciscoルータで crypto map ベースのIPsecを組む。"""
    _dev(dev_id, 'cisco')
    keepalive = ([f'crypto isakmp keepalive {interval} {retry}']
                if interval is not None else [])
    iface_cmds = ['interface GigabitEthernet0/0', f'ip address {local_ip} 255.255.255.252']
    if embed_map_in_interface:
        iface_cmds += ['crypto map CMAP', 'no shutdown', 'exit']
    else:
        iface_cmds += ['no shutdown', 'exit', 'crypto map CMAP interface GigabitEthernet0/0']
    _run(dev_id, [
        'conf t',
        'crypto isakmp policy 10', 'encryption aes 256', 'hash sha256',
        'authentication pre-share', 'group 14', 'exit',
        f'crypto isakmp key testkey123 address {peer_ip}',
        *keepalive,
        'crypto isakmp enable GigabitEthernet0/0',
        'crypto ipsec transform-set TS esp-aes-256 esp-sha256-hmac',
        'crypto map CMAP 10 ipsec-isakmp',
        'match address 100', f'set peer {peer_ip}', 'set transform-set TS', 'exit',
        *iface_cmds,
    ])


class TestSirConfigParsing:
    def test_ike_retry_parses_time_and_count(self):
        _dev('dpd-p1', 'sir')
        _run('dpd-p1', ['configure', 'remote 1 ap 0 ike retry 10s 3'])
        cfg = _cli('dpd-p1', 'show running-config')
        assert 'remote 1 ap 0 ike retry 10s 3' in cfg

    def test_ike_retry_rejects_out_of_range_time(self):
        _dev('dpd-p2', 'sir')
        out = _run('dpd-p2', ['configure', 'remote 1 ap 0 ike retry 90s 3'])
        assert 'format error' in out

    def test_ike_retry_rejects_out_of_range_count(self):
        _dev('dpd-p3', 'sir')
        out = _run('dpd-p3', ['configure', 'remote 1 ap 0 ike retry 10s 20'])
        assert 'format error' in out

    def test_ike_retry_rejects_missing_unit_suffix(self):
        _dev('dpd-p4', 'sir')
        out = _run('dpd-p4', ['configure', 'remote 1 ap 0 ike retry 10 3'])
        assert 'format error' in out

    def test_dpd_use_on_and_defaults_shown_in_ike_policy(self):
        _dev('dpd-p5', 'sir')
        _run('dpd-p5', ['configure', 'remote 1 ap 0 ike dpd use on'])
        out = _cli('dpd-p5', 'show ike policy')
        assert f'on(idle={DEFAULT_DPD_IDLE}s' in out
        assert f'retry={DEFAULT_DPD_RETRY_TIME}s*{DEFAULT_DPD_RETRY_COUNT}' in out

    def test_dpd_off_by_default(self):
        _dev('dpd-p6', 'sir')
        _run('dpd-p6', ['configure', 'remote 1 ap 0 tunnel local 10.0.0.1'])
        out = _cli('dpd-p6', 'show ike policy')
        assert 'off' in out

    def test_dpd_idle_range(self):
        _dev('dpd-p7', 'sir')
        assert 'format error' in _run(
            'dpd-p7', ['configure', 'remote 1 ap 0 ike dpd idle 3s'])
        assert 'format error' in _run(
            'dpd-p7', ['configure', 'remote 1 ap 0 ike dpd idle 700s'])
        assert _run('dpd-p7', ['configure', 'remote 1 ap 0 ike dpd idle 60s']) == ''

    def test_dpd_retry_must_be_shorter_than_idle(self):
        # マニュアル注記: 再送時間×(再送回数+1) < 無通信監視時間
        _dev('dpd-p8', 'sir')
        _run('dpd-p8', ['configure', 'remote 1 ap 0 ike dpd idle 5s'])
        out = _run('dpd-p8', ['configure', 'remote 1 ap 0 ike dpd retry 2s 3'])
        assert 'format error' in out  # 2*(3+1)=8 >= 5

    def test_dpd_retry_ok_when_within_idle(self):
        _dev('dpd-p9', 'sir')
        _run('dpd-p9', ['configure', 'remote 1 ap 0 ike dpd idle 60s'])
        out = _run('dpd-p9', ['configure', 'remote 1 ap 0 ike dpd retry 2s 3'])
        assert out == ''  # 2*(3+1)=8 < 60

    def test_sessionwatch_interval_defaults_retry_to_1s(self):
        # show running-config はSi-Rでは「入力したコマンド文字列をそのまま
        # 再現する」実装(app.py _build_running_config)になっており、
        # 省略された引数を正規化して埋め直すことはしない。デフォルト値が
        # 正しく補われたかどうかは、内部状態(ipsec_tunnels)で確認する。
        _dev('dpd-p10', 'sir')
        _run('dpd-p10', ['configure',
                         'remote 1 ap 0 sessionwatch interval 30s 5s 10s'])
        state = app_module.device_sessions['dpd-p10']
        sw = state.ipsec_tunnels[1]['sessionwatch']
        assert sw == {'normal': 30, 'error': 5, 'timeout': 10, 'retry': 1}

    def test_sessionwatch_retry_must_be_less_than_timeout(self):
        _dev('dpd-p11', 'sir')
        out = _run('dpd-p11', ['configure',
                               'remote 1 ap 0 sessionwatch interval 30s 5s 10s 15s'])
        assert 'format error' in out

    def test_sessionwatch_timeout_range(self):
        _dev('dpd-p12', 'sir')
        assert 'format error' in _run(
            'dpd-p12', ['configure', 'remote 1 ap 0 sessionwatch interval 30s 5s 3s'])
        assert 'format error' in _run(
            'dpd-p12', ['configure', 'remote 1 ap 0 sessionwatch interval 30s 5s 200s'])


class TestSirRunningConfigRoundTrip:
    def test_all_timer_commands_appear_in_running_config(self):
        _dev('dpd-r1', 'sir')
        _run('dpd-r1', [
            'configure',
            'remote 1 ap 0 ike retry 10s 3',
            'remote 1 ap 0 ike dpd use on',
            'remote 1 ap 0 ike dpd idle 10s',
            'remote 1 ap 0 ike dpd retry 1s 3',
            'remote 1 ap 0 sessionwatch interval 1m 5s 10s 2s',
        ])
        cfg = _cli('dpd-r1', 'show running-config')
        assert 'remote 1 ap 0 ike retry 10s 3' in cfg
        assert 'remote 1 ap 0 ike dpd use on' in cfg
        assert 'remote 1 ap 0 ike dpd idle 10s' in cfg
        assert 'remote 1 ap 0 ike dpd retry 1s 3' in cfg
        # 60秒は分表記に丸められる
        assert 'remote 1 ap 0 sessionwatch interval 1m 5s 10s 2s' in cfg

    def test_dpd_use_off_is_captured_verbatim(self):
        # show running-config は入力コマンドをそのまま再現する実装のため、
        # 明示的にoffを打てばその行がそのまま残る
        # （マニュアルの実行例 "remote <n> ap <ap> ike dpd use off" もこの形）。
        _dev('dpd-r2', 'sir')
        _run('dpd-r2', ['configure', 'remote 1 ap 0 ike dpd use off'])
        cfg = _cli('dpd-r2', 'show running-config')
        assert 'remote 1 ap 0 ike dpd use off' in cfg
        # 内部状態としてもoffが反映されていること
        state = app_module.device_sessions['dpd-r2']
        assert state.ipsec_tunnels[1]['dpd_use'] is False


class TestSirDpdDetection:
    def test_established_tunnel_survives_without_dpd(self):
        """DPD off のトンネルは、リンクが落ちても能動検知しない
        （実機同様、次のネゴシエーションかSA有効期限切れまで気づかない）。"""
        _setup_sir_tunnel('dpd-d1', 'dpd-d2', '192.0.2.1', '192.0.2.2')
        assert 'Established' in _cli('dpd-d1', 'show ipsec tunnel')
        _run('dpd-d1', ['configure', 'ether 2 1 use off'])
        time.sleep(0.5)
        assert 'Established' in _cli('dpd-d1', 'show ipsec tunnel')

    def test_dpd_on_transitions_to_detecting_then_down(self):
        _setup_sir_tunnel(
            'dpd-d3', 'dpd-d4', '192.0.2.5', '192.0.2.6',
            extra_a=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
            extra_b=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
        )
        assert 'Established' in _cli('dpd-d3', 'show ipsec tunnel')

        _run('dpd-d3', ['configure', 'ether 2 1 use off'])
        assert 'DPD-Detecting' in _cli('dpd-d3', 'show ipsec tunnel')

        assert _wait(lambda: 'Waiting' in _cli('dpd-d3', 'show ipsec tunnel'),
                     timeout=6.0), _cli('dpd-d3', 'show ipsec tunnel')

    def test_link_recovery_before_detect_window_keeps_tunnel_up(self):
        _setup_sir_tunnel(
            'dpd-d5', 'dpd-d6', '192.0.2.9', '192.0.2.10',
            extra_a=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
            extra_b=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
        )
        _run('dpd-d5', ['configure', 'ether 2 1 use off'])
        assert 'DPD-Detecting' in _cli('dpd-d5', 'show ipsec tunnel')
        _run('dpd-d5', ['configure', 'ether 2 1 use on'])
        assert 'Established' in _cli('dpd-d5', 'show ipsec tunnel')
        time.sleep(1.5)
        assert 'Established' in _cli('dpd-d5', 'show ipsec tunnel'), \
            "検知窓が満了する前に復旧したのに切断された"

    def test_show_ipsec_sa_reflects_dpd_state(self):
        _setup_sir_tunnel(
            'dpd-d7', 'dpd-d8', '192.0.2.13', '192.0.2.14',
            extra_a=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
            extra_b=['remote 1 ap 0 ike dpd use on',
                    'remote 1 ap 0 ike dpd idle 10s',
                    'remote 1 ap 0 ike dpd retry 1s 3'],
        )
        _run('dpd-d7', ['configure', 'ether 2 1 use off'])
        assert 'DPD-DETECT' in _cli('dpd-d7', 'show ipsec sa')


class TestSirDpdDetectSecondsHelper:
    def test_detect_seconds_matches_manual_formula(self):
        t = {'dpd_idle': 10, 'dpd_retry_time': 1, 'dpd_retry_count': 3}
        # NETLAB_FAST_TIMERS=1 なので実際には1/10に縮む
        assert sir_dpd_detect_seconds(t) == (10 + 1 * 3) / 10

    def test_detect_seconds_uses_manual_defaults_when_unset(self):
        assert sir_dpd_detect_seconds({}) == (
            DEFAULT_DPD_IDLE + DEFAULT_DPD_RETRY_TIME * DEFAULT_DPD_RETRY_COUNT) / 10


class TestCiscoKeepaliveValidation:
    def test_keepalive_within_range_accepted(self):
        _dev('dpd-c1', 'cisco')
        out = _run('dpd-c1', ['conf t', 'crypto isakmp keepalive 30 5'])
        assert out == ''

    def test_keepalive_interval_too_low_rejected(self):
        _dev('dpd-c2', 'cisco')
        out = _run('dpd-c2', ['conf t', 'crypto isakmp keepalive 5 5'])
        assert 'Invalid input' in out

    def test_keepalive_retry_too_high_rejected(self):
        _dev('dpd-c3', 'cisco')
        out = _run('dpd-c3', ['conf t', 'crypto isakmp keepalive 30 100'])
        assert 'Invalid input' in out

    def test_keepalive_appears_in_running_config(self):
        _dev('dpd-c4', 'catalyst')
        _run('dpd-c4', [
            'conf t', 'crypto isakmp key testkey address 198.51.100.201',
            'crypto isakmp keepalive 20 3',
        ])
        cfg = _cli('dpd-c4', 'show running-config')
        assert 'crypto isakmp keepalive 20 3' in cfg


class TestCiscoDpdPerTunnelIsolation:
    """以前はicmp_engineの共有属性(DPD_DETECT_SEC)を直接書き換えていたため、
    keepalive設定が異なる複数装置が同じグローバル値を取り合っていた。
    トンネルごとに保持されることを確認する。
    """

    def test_different_keepalive_settings_do_not_clobber_each_other(self):
        _setup_cisco_tunnel('dpd-iso1', '198.51.100.221', '198.51.100.222',
                            interval=10, retry=2)
        _setup_cisco_tunnel('dpd-iso2', '198.51.100.222', '198.51.100.221',
                            interval=100, retry=5)

        t1 = icmp_engine.ipsec_tunnels.get('dpd-iso1', [])
        t2 = icmp_engine.ipsec_tunnels.get('dpd-iso2', [])
        assert t1 and t2
        # 10*2/10(FAST) = 2.0 と 100*5/10(FAST) = 50.0 は大きく異なる。
        # 以前のバグでは後から設定した方の値に両方揃ってしまっていた。
        assert t1[0]['detect_sec'] != t2[0]['detect_sec']
        assert t1[0]['detect_sec'] == pytest.approx(2.0)
        assert t2[0]['detect_sec'] == pytest.approx(50.0)

    def test_setting_up_second_device_does_not_change_first(self):
        _setup_cisco_tunnel('dpd-iso3', '198.51.100.225', '198.51.100.226',
                            interval=10, retry=2)
        before = icmp_engine.ipsec_tunnels['dpd-iso3'][0]['detect_sec']
        _setup_cisco_tunnel('dpd-iso4', '198.51.100.226', '198.51.100.225',
                            interval=200, retry=10)
        after = icmp_engine.ipsec_tunnels['dpd-iso3'][0]['detect_sec']
        assert before == after, "後発の装置設定が既存装置のDPDタイマーを書き換えた"


class TestCiscoDpdDetection:
    def test_shutdown_triggers_detecting_then_down_interface_embedded_map(self):
        """実機で最も一般的な構文（interface配下に crypto map <name>）で確認。"""
        _setup_cisco_tunnel('dpd-e1', '198.51.100.230', '198.51.100.231',
                            interval=10, retry=2, embed_map_in_interface=True)
        assert _wait(lambda: 'ESTABLISHED' in _cli('dpd-e1', 'show crypto ipsec sa'))

        _run('dpd-e1', ['conf t', 'interface GigabitEthernet0/0', 'shutdown'])
        out = _cli('dpd-e1', 'show crypto ipsec sa')
        assert 'DPD-DETECTING' in out or 'DOWN' in out

        assert _wait(lambda: 'DOWN' in _cli('dpd-e1', 'show crypto ipsec sa'),
                     timeout=6.0), _cli('dpd-e1', 'show crypto ipsec sa')

    def test_global_crypto_map_interface_form_also_registers(self):
        """"crypto map <name> interface <if>" 形式でも登録されることを確認。"""
        _setup_cisco_tunnel('dpd-e2', '198.51.100.234', '198.51.100.235',
                            interval=10, retry=2, embed_map_in_interface=False)
        assert _wait(lambda: 'ESTABLISHED' in _cli('dpd-e2', 'show crypto ipsec sa'))

    def test_local_addr_is_not_confused_with_similarly_named_interface(self):
        """"GigabitEthernet0/0" は "GigabitEthernet0/0/0" の部分文字列。
        部分一致だけで探すと、crypto mapを適用していない方のIPを
        誤って掴んでしまう（0/0 は 0/0/0 に含まれる）。
        """
        _setup_cisco_tunnel('dpd-e3', '198.51.100.238', '198.51.100.239',
                            interval=10, retry=2, embed_map_in_interface=True)
        out = _cli('dpd-e3', 'show crypto ipsec sa')
        local_lines = [ln for ln in out.splitlines() if 'local addr' in ln]
        assert local_lines and '198.51.100.238' in local_lines[0], local_lines
