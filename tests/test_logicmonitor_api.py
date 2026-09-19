"""
LogicMonitor REST API v3 エミュレーションのテスト

LogicMonitorは完全SaaS型でオンプレ配布物が無いため「動かす」ことは
できない。Nexpose/InsightVMと同じ考え方で、REST API v3の一部
（Device/Alert、LMv1署名認証）をこのエミュレータ内に実装している。

このテストファイルは NETLAB_AUTH_DISABLE を意図的に無効化した状態
（実際の認証ミドルウェアが動く状態）でLMv1署名の検証まで含めて確認
する。他のテストファイルの多くはNETLAB_AUTH_DISABLE=1を前提にして
いるため、モジュールレベルのfixtureで元に戻す。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

# 認証を有効なまま読み込む(他のテストがNETLAB_AUTH_DISABLE=1を
# 設定している可能性があるため、このモジュールの読み込み前に明示的に外す)
os.environ.pop('NETLAB_AUTH_DISABLE', None)

import app as app_module  # noqa: E402
from engine.logicmonitor import (  # noqa: E402
    compute_lmv1_signature, verify_lmv1_auth, parse_authorization_header,
    LM_ACCESS_ID, LM_ACCESS_KEY, logicmonitor_engine,
)

client = TestClient(app_module.app)
client.__enter__()


def _lm_headers(method, path, body_str=''):
    epoch = str(int(time.time() * 1000))
    sig = compute_lmv1_signature(LM_ACCESS_KEY, method, epoch, body_str, path)
    headers = {'Authorization': f'LMv1 {LM_ACCESS_ID}:{sig}:{epoch}'}
    if body_str:
        headers['Content-Type'] = 'application/json'
    return headers


def _setup_device(cmds=()):
    """/api/device・/api/cliは通常のセッション認証(トークン)を要求する。
    このテストファイルはLMv1認証を本物のまま動かすためNETLAB_AUTH_DISABLE
    を外しているので、装置のセットアップ操作だけ一時的に認証を無効化する。
    """
    app_module._AUTH_DISABLED = True
    try:
        client.delete('/api/device/lm-test')
        client.post('/api/device', json={'id': 'lm-test', 'type': 'catalyst',
                                         'hostname': 'LM-Test'})
        for c in cmds:
            client.post('/api/cli', json={'device_id': 'lm-test', 'command': c})
    finally:
        app_module._AUTH_DISABLED = False


@pytest.fixture(scope='module', autouse=True)
def _restore_auth_disabled_after_module():
    """app_moduleはプロセス全体で使い回される単一モジュールなので、
    ここで_AUTH_DISABLEDをFalseにしたまま終わると、後から実行される
    他のテストファイル(NETLAB_AUTH_DISABLE=1前提)まで巻き込んで
    認証エラーになってしまう。

    「モジュール読み込み時点の値を覚えておいて戻す」方式は、pytestの
    収集順序次第でこのファイルが最初にapp.pyをimportしてしまうと
    False を「元の値」として覚えてしまい機能しない
    （実際にtest_evpn_vxlan.pyと一緒に実行して再現・確認した）。
    このリポジトリの全テストファイルが前提にしている不変条件
    (NETLAB_AUTH_DISABLE有効=True)を直接復元する方が確実。
    """
    yield
    app_module._AUTH_DISABLED = True


@pytest.fixture(autouse=True)
def _clean():
    logicmonitor_engine.reset()
    app_module._AUTH_DISABLED = True
    client.delete('/api/device/lm-test')
    app_module._AUTH_DISABLED = False
    yield
    app_module._AUTH_DISABLED = True
    client.delete('/api/device/lm-test')
    app_module._AUTH_DISABLED = False


class TestLmv1SignatureVerification:
    def test_missing_header_is_rejected(self):
        ok, reason = verify_lmv1_auth('', 'GET', '', '/device/devices')
        assert not ok
        assert 'missing' in reason

    def test_malformed_header_is_rejected(self):
        ok, reason = verify_lmv1_auth('Basic dXNlcjpwYXNz', 'GET', '', '/device/devices')
        assert not ok

    def test_unknown_access_id_is_rejected(self):
        epoch = str(int(time.time() * 1000))
        sig = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', epoch, '', '/device/devices')
        ok, reason = verify_lmv1_auth(f'LMv1 someone-else:{sig}:{epoch}', 'GET', '',
                                      '/device/devices')
        assert not ok
        assert 'Access ID' in reason

    def test_wrong_signature_is_rejected(self):
        epoch = str(int(time.time() * 1000))
        ok, reason = verify_lmv1_auth(f'LMv1 {LM_ACCESS_ID}:wrong:{epoch}', 'GET', '',
                                      '/device/devices')
        assert not ok
        assert 'signature' in reason

    def test_correct_signature_is_accepted(self):
        epoch = str(int(time.time() * 1000))
        sig = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', epoch, '', '/device/devices')
        ok, reason = verify_lmv1_auth(f'LMv1 {LM_ACCESS_ID}:{sig}:{epoch}', 'GET', '',
                                      '/device/devices')
        assert ok

    def test_expired_epoch_is_rejected(self):
        old_epoch = str(int((time.time() - 600) * 1000))
        sig = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', old_epoch, '', '/device/devices')
        ok, reason = verify_lmv1_auth(f'LMv1 {LM_ACCESS_ID}:{sig}:{old_epoch}', 'GET', '',
                                      '/device/devices')
        assert not ok
        assert 'expired' in reason

    def test_signature_depends_on_method_and_path_and_body(self):
        """GETとPOST、違うパス、違うbodyでは違う署名になること
        （実機のLMv1もこれらすべてを署名対象に含める）。"""
        epoch = '1700000000000'
        sig_get = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', epoch, '', '/device/devices')
        sig_post = compute_lmv1_signature(LM_ACCESS_KEY, 'POST', epoch, '', '/device/devices')
        sig_other_path = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', epoch, '', '/alert/alerts')
        sig_with_body = compute_lmv1_signature(LM_ACCESS_KEY, 'POST', epoch, '{"a":1}',
                                               '/device/devices')
        assert len({sig_get, sig_post, sig_other_path, sig_with_body}) == 4

    def test_parse_authorization_header_rejects_wrong_part_count(self):
        assert parse_authorization_header('LMv1 onlyone') is None
        assert parse_authorization_header('LMv1 a:b:c:d') is None
        assert parse_authorization_header('') is None


class TestSantabaRestMiddleware:
    """実際にミドルウェア(session_auth_middleware)を通した確認。
    このテストファイルはNETLAB_AUTH_DISABLEを外しているので、本物の
    認証チェックが働く。"""

    def test_get_devices_without_auth_is_401(self):
        r = client.get('/santaba/rest/device/devices')
        assert r.status_code == 401

    def test_get_devices_with_bad_signature_is_401(self):
        epoch = str(int(time.time() * 1000))
        r = client.get('/santaba/rest/device/devices',
                       headers={'Authorization': f'LMv1 {LM_ACCESS_ID}:deadbeef:{epoch}'})
        assert r.status_code == 401

    def test_get_devices_with_correct_signature_succeeds(self):
        r = client.get('/santaba/rest/device/devices',
                       headers=_lm_headers('GET', '/device/devices'))
        assert r.status_code == 200
        assert r.json() == {'total': 0, 'items': []}

    def test_post_device_signature_must_cover_request_body(self):
        """bodyを含めずに署名すると、実際に送るbody付きリクエストとは
        署名が一致せず拒否されること（署名対象にbodyが入っている証拠）。"""
        body = {'name': '10.1.1.1', 'displayName': 'X'}
        import json as _json
        body_str = _json.dumps(body)
        # わざとbodyを空文字列として署名する(実際のbodyとは不一致)
        headers = _lm_headers('POST', '/device/devices', body_str='')
        r = client.post('/santaba/rest/device/devices', content=body_str, headers=headers)
        assert r.status_code == 401

    def test_post_device_with_matching_signature_succeeds(self):
        import json as _json
        body = {'name': '10.1.1.1', 'displayName': 'X'}
        body_str = _json.dumps(body)
        headers = _lm_headers('POST', '/device/devices', body_str=body_str)
        r = client.post('/santaba/rest/device/devices', content=body_str, headers=headers)
        assert r.status_code == 200
        assert r.json()['displayName'] == 'X'


class TestDeviceAndAlertLifecycle:
    def test_create_device_links_to_real_emulator_device_and_alerts_on_down_interface(self):
        _setup_device(['configure terminal', 'interface GigabitEthernet1/0/1', 'shutdown'])

        import json as _json
        body = {'name': 'lm-test-device', '_device_id': 'lm-test'}
        body_str = _json.dumps(body)
        r = client.post('/santaba/rest/device/devices', content=body_str,
                        headers=_lm_headers('POST', '/device/devices', body_str))
        assert r.status_code == 200
        lm_id = r.json()['id']

        r = client.get('/santaba/rest/alert/alerts',
                       headers=_lm_headers('GET', '/alert/alerts'))
        assert r.status_code == 200
        alerts = r.json()['items']
        assert any(a['instanceName'] == 'GigabitEthernet1/0/1'
                  and a['deviceId'] == lm_id for a in alerts)

    def test_unused_notconnect_ports_do_not_alert(self):
        """ケーブルが挿さっていないだけの notconnect ポートは、実機の
        LogicMonitorの既定インタフェースDataSourceと同じくアラートに
        ならないこと（unusedポートだらけの素のスイッチを繋いだだけで
        大量のノイズにならないようにするための固定）。"""
        _setup_device()
        import json as _json
        body_str = _json.dumps({'name': 'lm-test-device', '_device_id': 'lm-test'})
        r = client.post('/santaba/rest/device/devices', content=body_str,
                        headers=_lm_headers('POST', '/device/devices', body_str))
        lm_id = r.json()['id']

        r = client.get('/santaba/rest/alert/alerts',
                       headers=_lm_headers('GET', '/alert/alerts'))
        alerts = r.json()['items']
        # Catalystの既定状態では未接続ポートが notconnect のまま。
        # err-disabled(bpduguardデモ用)以外はアラートに出ないはず
        assert all(a['alertValue'] != 'notconnect' for a in alerts)

    def test_alerts_can_be_filtered_by_device_id(self):
        _setup_device()
        import json as _json
        body_str = _json.dumps({'name': 'd1', '_device_id': 'lm-test'})
        r = client.post('/santaba/rest/device/devices', content=body_str,
                        headers=_lm_headers('POST', '/device/devices', body_str))
        lm_id = r.json()['id']

        r = client.get(f'/santaba/rest/alert/alerts?deviceId={lm_id}',
                       headers=_lm_headers('GET', '/alert/alerts'))
        assert r.status_code == 200
        assert all(a['deviceId'] == lm_id for a in r.json()['items'])

    def test_delete_nonexistent_device_is_404(self):
        r = client.delete('/santaba/rest/device/devices/999999',
                          headers=_lm_headers('DELETE', '/device/devices/999999'))
        assert r.status_code == 404

    def test_create_device_without_name_is_rejected(self):
        import json as _json
        body_str = _json.dumps({})
        r = client.post('/santaba/rest/device/devices', content=body_str,
                        headers=_lm_headers('POST', '/device/devices', body_str))
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════
# syslog/SNMP Trap受信 (tools/logicmonitor_collector.py が転送する
# POST /santaba/rest/_emulator/events)
#
# 実機のLogicMonitor Collectorはsyslog/SNMP Trapを受信して独自の
# 内部プロトコルでLogicMonitor側へ転送するが、公開REST API v3には
# そのためのエンドポイントが無い。/_emulator/events はこのエミュレータ
# 独自の拡張で、tools/logicmonitor_collector.py が実際にUDPで受信した
# syslog/Trapをここへ転送する。
# ══════════════════════════════════════════════════════════
class TestEventIngestion:
    def _post_event(self, body):
        import json as _json
        body_str = _json.dumps(body)
        return client.post('/santaba/rest/_emulator/events', content=body_str,
                           headers=_lm_headers('POST', '/_emulator/events', body_str))

    def test_syslog_event_requires_source_ip(self):
        r = self._post_event({'type': 'syslog', 'severity': 3, 'message': 'x'})
        assert r.status_code == 422

    def test_unknown_event_type_is_rejected(self):
        r = self._post_event({'type': 'bogus', 'source_ip': '10.9.9.9'})
        assert r.status_code == 422

    def test_syslog_event_below_warning_is_not_an_alert(self):
        """info(6)/debug(7)は実機同様Alertにならない
        （ログが埋め尽くされるノイズを避ける設計）。"""
        r = self._post_event({'type': 'syslog', 'source_ip': '10.9.9.1',
                              'severity': 6, 'facility_tag': 'SYS',
                              'message': 'routine info message'})
        assert r.status_code == 200
        assert r.json()['alert'] is None

    def test_syslog_event_at_error_becomes_critical_alert(self):
        r = self._post_event({'type': 'syslog', 'source_ip': '10.9.9.2',
                              'severity': 3, 'facility_tag': 'LINK',
                              'message': 'something broke'})
        assert r.status_code == 200
        alert = r.json()['alert']
        assert alert['severityLabel'] == 'critical'
        assert alert['resourceTemplateName'] == 'Syslog'
        assert alert['instanceName'] == 'LINK'

    def test_syslog_event_matches_device_by_ip(self):
        _setup_device()
        import json as _json
        body_str = _json.dumps({'name': '10.9.9.3', '_device_id': 'lm-test'})
        r = client.post('/santaba/rest/device/devices', content=body_str,
                        headers=_lm_headers('POST', '/device/devices', body_str))
        lm_id = r.json()['id']

        r = self._post_event({'type': 'syslog', 'source_ip': '10.9.9.3',
                              'severity': 3, 'facility_tag': 'LINK', 'message': 'x'})
        alert_id = r.json()['alert']['id']
        assert r.json()['alert']['deviceId'] == lm_id

        r = client.get(f'/santaba/rest/alert/alerts?deviceId={lm_id}',
                       headers=_lm_headers('GET', '/alert/alerts'))
        assert any(a['id'] == alert_id for a in r.json()['items'])

    def test_syslog_event_from_unregistered_ip_has_no_device(self):
        r = self._post_event({'type': 'syslog', 'source_ip': '10.9.9.99',
                              'severity': 2, 'facility_tag': 'X', 'message': 'x'})
        alert = r.json()['alert']
        assert alert['deviceId'] is None
        assert alert['deviceDisplayName'] == '10.9.9.99'

    def test_linkdown_trap_becomes_critical_alert(self):
        r = self._post_event({'type': 'trap', 'source_ip': '10.9.9.4',
                              'trap_oid': '1.3.6.1.6.3.1.1.5.3',
                              'description': 'if down'})
        assert r.status_code == 200
        alert = r.json()['alert']
        assert alert['severityLabel'] == 'critical'
        assert alert['instanceName'] == 'linkDown'
        assert alert['resourceTemplateName'] == 'SNMP Trap'

    def test_linkup_trap_is_not_a_new_alert(self):
        """復旧(linkUp)は新規Alertにしない。"""
        r = self._post_event({'type': 'trap', 'source_ip': '10.9.9.5',
                              'trap_oid': '1.3.6.1.6.3.1.1.5.4',
                              'description': 'if up'})
        assert r.status_code == 200
        assert r.json()['alert'] is None

    def test_coldstart_trap_becomes_warning_not_critical(self):
        r = self._post_event({'type': 'trap', 'source_ip': '10.9.9.6',
                              'trap_oid': '1.3.6.1.6.3.1.1.5.1',
                              'description': 'reboot'})
        alert = r.json()['alert']
        assert alert['severityLabel'] == 'warning'
        assert alert['instanceName'] == 'coldStart'

    def test_ingested_events_appear_in_alert_list(self):
        before = client.get('/santaba/rest/alert/alerts',
                            headers=_lm_headers('GET', '/alert/alerts')).json()['total']
        self._post_event({'type': 'syslog', 'source_ip': '10.9.9.7',
                          'severity': 3, 'facility_tag': 'X', 'message': 'x'})
        after = client.get('/santaba/rest/alert/alerts',
                           headers=_lm_headers('GET', '/alert/alerts')).json()['total']
        assert after == before + 1
