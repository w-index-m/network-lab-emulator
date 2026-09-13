"""
Nexpose / InsightVM Console API v3 のテスト

Rapid7公式のSwagger 2.0仕様(206エンドポイント)のうち、脆弱性スキャナと
して筋が通る最小限だけを実装している:

    Site作成 → スキャン → Asset検出 → Vulnerability → Report

スキャン対象はこのエミュレータ上の装置なので、**装置側で有効化した管理
サービス(SNMP/NETCONF/gNMI)が、そのまま検出結果に効く**。ここではその
連動を固定する。

注意: 返す脆弱性はこのエミュレータ用の作り物で、CVE番号もダミー。
実在機器の脆弱性情報ではない。
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient       # noqa: E402

import app as app_module                        # noqa: E402
from engine.nexpose import nexpose_engine, page_of   # noqa: E402

client = TestClient(app_module.app)


def _cli(dev, cmd):
    return client.post('/api/cli',
                       json={'device_id': dev, 'command': cmd}).json()['output']


def _device(dev, hostname, ip, extra=()):
    # DELETE で消す（pop だけだと実リスナーが残り、次のテストの
    # スキャンがその開きっぱなしのポートを拾ってしまう）
    client.delete(f'/api/device/{dev}')
    client.post('/api/device',
                json={'id': dev, 'type': 'catalyst', 'hostname': hostname})
    cmds = ['configure terminal', 'interface GigabitEthernet1/0/1',
            'no switchport', f'ip address {ip} 255.255.255.0',
            'no shutdown', 'exit'] + list(extra) + ['end']
    for c in cmds:
        _cli(dev, c)
    return dev


@pytest.fixture(autouse=True)
def _clean():
    nexpose_engine.reset()
    for d in ('t-np-a', 't-np-b'):
        client.delete(f'/api/device/{d}')
    yield
    for d in ('t-np-a', 't-np-b'):
        client.delete(f'/api/device/{d}')


def _lab():
    """管理サービスを開けた装置と、締めた装置の2台"""
    _device('t-np-a', 'VULN-A', '10.200.0.1',
            ['snmp-server community public ro', 'netconf-yang',
             'gnxi', 'gnxi server'])
    _device('t-np-b', 'VULN-B', '10.200.0.2',
            ['username netadmin privilege 15 secret Str0ngP@ss'])
    r = client.post('/api/3/sites', json={
        'name': 'Lab Segment', 'importance': 'high',
        'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.1', '10.200.0.2']}}}})
    return r.json()['id']


# ══════════════════════════════════════════
# Site
# ══════════════════════════════════════════
def test_site_create_returns_201_and_id():
    r = client.post('/api/3/sites', json={'name': 'S1'})
    assert r.status_code == 201
    body = r.json()
    assert isinstance(body['id'], int)
    assert body['links'][0]['href'].endswith(f'/sites/{body["id"]}')


def test_site_requires_name():
    r = client.post('/api/3/sites', json={'description': 'no name'})
    assert r.status_code == 400
    assert 'name is required' in r.json()['message']


def test_site_get_and_delete():
    sid = client.post('/api/3/sites', json={'name': 'S1'}).json()['id']
    assert client.get(f'/api/3/sites/{sid}').json()['name'] == 'S1'
    assert client.delete(f'/api/3/sites/{sid}').status_code == 200
    assert client.get(f'/api/3/sites/{sid}').status_code == 404


def test_unknown_site_is_404_with_message():
    r = client.get('/api/3/sites/999')
    assert r.status_code == 404
    assert '999' in r.json()['message']


def test_sites_listing_is_paged():
    for i in range(3):
        client.post('/api/3/sites', json={'name': f'S{i}'})
    body = client.get('/api/3/sites', params={'page': 0, 'size': 2}).json()
    assert len(body['resources']) == 2
    assert body['page'] == {'number': 0, 'size': 2,
                            'totalResources': 3, 'totalPages': 2}


# ══════════════════════════════════════════
# Scan
# ══════════════════════════════════════════
def test_scan_finds_assets_and_vulnerabilities():
    sid = _lab()
    r = client.post(f'/api/3/sites/{sid}/scans', json={'name': 'initial'})
    assert r.status_code == 201
    scan = client.get(f'/api/3/scans/{r.json()["id"]}').json()
    assert scan['status'] == 'finished'
    assert scan['assets'] == 2
    assert scan['vulnerabilities']['total'] > 0


def test_open_management_services_drive_the_findings():
    """SNMP/NETCONF/gNMIを開けた装置のほうが多く検出される

    「装置の設定が、そのままスキャン結果に効く」というのがこの実装の
    肝なので、ここが崩れたら意味が無くなる。

    ここは probe=False（設定から候補を出すだけ）で確認している。
    TestClient は**リクエストの間しかイベントループを回さない**ため、
    同じループ上で動く SNMP UDP エージェントが応答できず、
    実プローブだと 161 が閉じていると判定されてしまうため。
    実プローブ側は tests/test_nexpose_real_scan.py が
    本物の uvicorn サーバを立てて確認している。
    """
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={'probe': False})
    assets = {a['ip']: a for a in
              client.get(f'/api/3/sites/{sid}/assets').json()['resources']}
    open_dev = assets['10.200.0.1']
    tight_dev = assets['10.200.0.2']
    assert open_dev['vulnerabilities']['total'] > tight_dev['vulnerabilities']['total']
    ports = {s['port'] for s in open_dev['services']}
    assert {161, 830, 50052} <= ports
    assert tight_dev['services'] == []


def test_scan_of_unknown_site_is_404():
    assert client.post('/api/3/sites/999/scans', json={}).status_code == 404


def test_scan_status_transitions_follow_the_spec():
    """pause/resume/stop は実機と同じ遷移だけ許す"""
    sid = _lab()
    scan_id = client.post(f'/api/3/sites/{sid}/scans', json={}).json()['id']
    # finished のスキャンは pause できない
    r = client.post(f'/api/3/scans/{scan_id}/pause')
    assert r.status_code == 400
    assert 'finished' in r.json()['message']

    nexpose_engine.scans[scan_id]['status'] = 'running'
    assert client.post(f'/api/3/scans/{scan_id}/pause').status_code == 200
    assert nexpose_engine.scans[scan_id]['status'] == 'paused'
    assert client.post(f'/api/3/scans/{scan_id}/resume').status_code == 200
    assert nexpose_engine.scans[scan_id]['status'] == 'running'
    assert client.post(f'/api/3/scans/{scan_id}/stop').status_code == 200
    assert nexpose_engine.scans[scan_id]['status'] == 'stopped'


def test_unsupported_scan_status_is_rejected():
    sid = _lab()
    scan_id = client.post(f'/api/3/sites/{sid}/scans', json={}).json()['id']
    r = client.post(f'/api/3/scans/{scan_id}/restart')
    assert r.status_code == 400
    assert 'unsupported status' in r.json()['message']


def test_rescan_does_not_duplicate_assets():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assets = client.get(f'/api/3/sites/{sid}/assets').json()
    assert assets['page']['totalResources'] == 2


def test_targets_restrict_what_is_scanned():
    sid = _lab()
    client.put(f'/api/3/sites/{sid}/included_targets', json=['10.200.0.2'])
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assets = client.get(f'/api/3/sites/{sid}/assets').json()['resources']
    assert [a['ip'] for a in assets] == ['10.200.0.2']


# ══════════════════════════════════════════
# Asset / Vulnerability
# ══════════════════════════════════════════
def test_asset_detail_and_services():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']
    a = client.get(f'/api/3/assets/{aid}').json()
    assert a['ip'] and a['hostName'] and a['os']
    assert a['assessedForVulnerabilities'] is True
    svc = client.get(f'/api/3/assets/{aid}/services').json()
    assert svc['page']['totalResources'] == len(a['services'])


def test_asset_vulnerabilities_reference_the_catalog():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = next(a['id'] for a in
               client.get(f'/api/3/sites/{sid}/assets').json()['resources']
               if a['ip'] == '10.200.0.1')
    rows = client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']
    ids = {r['vulnerabilityId'] for r in rows}
    assert 'netlab-snmp-default-community' in ids
    assert all(r['status'] == 'vulnerable' for r in rows)


def test_vulnerability_catalog_is_sorted_by_severity():
    body = client.get('/api/3/vulnerabilities',
                      params={'size': 50}).json()['resources']
    order = [v['severity'] for v in body]
    rank = {'Critical': 0, 'Severe': 1, 'Moderate': 2}
    assert order == sorted(order, key=lambda s: rank[s])


def test_vulnerability_detail_has_risk_and_cvss_fields():
    v = client.get('/api/3/vulnerabilities/netlab-default-credentials').json()
    assert v['severity'] == 'Critical'
    assert 0 <= v['severityScore'] <= 10
    assert v['riskScore'] > 0          # Real Risk Score 風の0-1000スケール
    assert v['cves'] and v['description']['text']


def test_vulnerability_affected_assets_and_solutions():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    vid = 'netlab-snmp-default-community'
    assets = client.get(f'/api/3/vulnerabilities/{vid}/assets').json()['resources']
    assert len(assets) == 1            # SNMPを開けた1台だけ
    sol = client.get(f'/api/3/vulnerabilities/{vid}/solutions').json()['resources']
    assert 'community string' in sol[0]['summary']['text']


def test_unknown_vulnerability_is_404():
    assert client.get('/api/3/vulnerabilities/no-such').status_code == 404


def test_internal_match_rules_are_not_exposed():
    """検出条件(match_port等)はAPIレスポンスに漏らさない"""
    v = client.get('/api/3/vulnerabilities/netlab-ssh-weak-kex').json()
    assert 'match_port' not in v
    assert 'match_default_creds' not in v


# ══════════════════════════════════════════
# Report
# ══════════════════════════════════════════
def test_report_create_generate_and_history():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    rid = client.post('/api/3/reports', json={
        'name': 'Lab Audit', 'template': 'audit-report',
        'format': 'pdf', 'scope': {'sites': [sid]}}).json()['id']
    assert client.post(f'/api/3/reports/{rid}/generate').status_code == 201
    hist = client.get(f'/api/3/reports/{rid}/history').json()['resources']
    assert len(hist) == 1 and hist[0]['status'] == 'complete'


def test_report_content_lists_assets_and_findings():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    rid = client.post('/api/3/reports', json={
        'name': 'Lab Audit', 'scope': {'sites': [sid]}}).json()['id']
    client.post(f'/api/3/reports/{rid}/generate')
    body = client.get(f'/api/3/reports/{rid}/content').text
    assert 'Site 1: Lab Segment' in body
    assert '10.200.0.1' in body
    assert 'SNMP Agent Default Community Name' in body


def test_report_requires_name():
    assert client.post('/api/3/reports', json={}).status_code == 400


def test_generate_unknown_report_is_404():
    assert client.post('/api/3/reports/999/generate').status_code == 404


# ══════════════════════════════════════════
# ページング関数
# ══════════════════════════════════════════
def test_page_of_handles_empty_and_last_page():
    assert page_of([])['page']['totalPages'] == 0
    p = page_of(list(range(5)), page=2, size=2)
    assert p['resources'] == [4]
    assert p['page']['totalPages'] == 3


def test_page_of_out_of_range_returns_empty_resources():
    p = page_of(list(range(3)), page=9, size=2)
    assert p['resources'] == []
    assert p['page']['totalResources'] == 3


# ══════════════════════════════════════════
# 既定クレデンシャルの検出
# ══════════════════════════════════════════
def test_default_credentials_clear_once_a_real_account_exists():
    """ユーザを作り直したら所見が消えること

    回帰テスト: 以前は存在しない属性 `state.local_users` を読んでいたため
    この所見が常に立ち、設定を直しても消えなかった。
    """
    _device('t-np-b', 'VULN-B', '10.200.0.2',
            ['username netadmin privilege 15 secret Str0ngP@ss'])
    sid = client.post('/api/3/sites', json={
        'name': 'S', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.2']}}}}).json()['id']
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']
    ids = {r['vulnerabilityId'] for r in
           client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}
    assert 'netlab-default-credentials' not in ids


def test_default_account_name_still_trips_the_finding():
    _device('t-np-b', 'VULN-B', '10.200.0.2',
            ['username admin privilege 15 secret Str0ngP@ss'])
    sid = client.post('/api/3/sites', json={
        'name': 'S', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.2']}}}}).json()['id']
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']
    ids = {r['vulnerabilityId'] for r in
           client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}
    assert 'netlab-default-credentials' in ids


def test_renaming_the_snmp_community_clears_the_finding():
    """回帰テスト: 以前はポート161が開いているだけで
    「デフォルトコミュニティ(public)」を上げていた"""
    _device('t-np-a', 'VULN-A', '10.200.0.1',
            ['snmp-server community n0t-public ro',
             'username netadmin privilege 15 secret Str0ngP@ss'])
    sid = client.post('/api/3/sites', json={
        'name': 'S', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.1']}}}}).json()['id']
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']
    ids = {r['vulnerabilityId'] for r in
           client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}
    assert 'netlab-snmp-default-community' not in ids
    # サービス自体は見えている（ポートは開いたまま）
    assert 161 in {s['port'] for s in
                   client.get(f'/api/3/assets/{aid}').json()['services']}


# ══════════════════════════════════════════
# スキャンテンプレート
# ══════════════════════════════════════════
def test_scan_templates_are_listed():
    body = client.get('/api/3/scan_templates').json()
    ids = {t['id'] for t in body['resources']}
    assert {'discovery', 'full-audit-without-web-spider', 'exhaustive'} <= ids


def test_unknown_scan_template_is_404():
    assert client.get('/api/3/scan_templates/nope').status_code == 404


def test_site_rejects_unknown_scan_template():
    r = client.post('/api/3/sites', json={'name': 'S', 'scanTemplate': 'nope'})
    assert r.status_code == 400
    assert 'unknown scan template' in r.json()['message']


def test_discovery_template_finds_services_but_no_vulnerabilities():
    sid = _lab()
    r = client.post(f'/api/3/sites/{sid}/scans',
                    json={'templateId': 'discovery'})
    assert r.status_code == 201
    scan = client.get(f'/api/3/scans/{r.json()["id"]}').json()
    assert scan['assets'] == 2
    assert scan['vulnerabilities']['total'] == 0
    a = next(x for x in
             client.get(f'/api/3/sites/{sid}/assets').json()['resources']
             if x['ip'] == '10.200.0.1')
    assert a['assessedForVulnerabilities'] is False
    assert {161, 830, 50052} <= {s['port'] for s in a['services']}   # 発見はする


def test_unknown_template_on_scan_is_400_not_404():
    sid = _lab()
    r = client.post(f'/api/3/sites/{sid}/scans', json={'templateId': 'nope'})
    assert r.status_code == 400


# ══════════════════════════════════════════
# 認証スキャン（site credentials）
# ══════════════════════════════════════════
def _ssh_cred(sid, name='lab-ssh'):
    return client.post(f'/api/3/sites/{sid}/site_credentials', json={
        'name': name,
        'account': {'service': 'ssh', 'username': 'netadmin',
                    'password': 'Str0ngP@ss'}})


def test_credential_secret_is_never_returned():
    sid = _lab()
    assert _ssh_cred(sid).status_code == 201
    rows = client.get(f'/api/3/sites/{sid}/site_credentials').json()['resources']
    assert rows[0]['account'] == {'service': 'ssh', 'username': 'netadmin'}
    assert 'password' not in rows[0]['account']
    assert '_secret' not in rows[0]


def test_credential_requires_a_supported_service():
    sid = _lab()
    r = client.post(f'/api/3/sites/{sid}/site_credentials', json={
        'name': 'x', 'account': {'service': 'rdp', 'username': 'u'}})
    assert r.status_code == 400
    assert 'unsupported credential service' in r.json()['message']


def test_snmp_credential_needs_a_community_not_a_username():
    sid = _lab()
    r = client.post(f'/api/3/sites/{sid}/site_credentials', json={
        'name': 'x', 'account': {'service': 'snmp'}})
    assert r.status_code == 400
    assert 'community is required' in r.json()['message']
    assert client.post(f'/api/3/sites/{sid}/site_credentials', json={
        'name': 'x', 'account': {'service': 'snmp',
                                 'community': 'public'}}).status_code == 201


def test_credentialed_scan_reaches_config_only_findings():
    """資格情報があるスキャンだけが config を読む所見を上げる

    非認証では「開いているポート」しか見えない。
    """
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    scan = client.get('/api/3/scans').json()['resources'][-1]
    assert scan['credentialed'] is False
    aid = next(a['id'] for a in
               client.get(f'/api/3/sites/{sid}/assets').json()['resources']
               if a['ip'] == '10.200.0.1')
    before = {r['vulnerabilityId'] for r in
              client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}
    assert 'netlab-no-aaa-authentication' not in before

    _ssh_cred(sid)
    client.post(f'/api/3/sites/{sid}/scans', json={})
    scan = client.get('/api/3/scans').json()['resources'][-1]
    assert scan['credentialed'] is True
    after = {r['vulnerabilityId'] for r in
             client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}
    assert 'netlab-no-aaa-authentication' in after
    assert before < after
    assert client.get(f'/api/3/assets/{aid}').json()['credentialStatus'] \
        == 'credential-status-success'


def test_fixing_aaa_clears_the_authenticated_finding():
    sid = _lab()
    _ssh_cred(sid)
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = next(a['id'] for a in
               client.get(f'/api/3/sites/{sid}/assets').json()['resources']
               if a['ip'] == '10.200.0.1')
    assert 'netlab-no-aaa-authentication' in {
        r['vulnerabilityId'] for r in
        client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}

    for c in ('configure terminal', 'aaa new-model', 'end'):
        _cli('t-np-a', c)
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assert 'netlab-no-aaa-authentication' not in {
        r['vulnerabilityId'] for r in
        client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}


def test_rw_community_is_only_visible_with_credentials():
    _device('t-np-a', 'VULN-A', '10.200.0.1',
            ['snmp-server community secret-rw rw',
             'username netadmin privilege 15 secret Str0ngP@ss',
             'aaa new-model'])
    sid = client.post('/api/3/sites', json={
        'name': 'S', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.1']}}}}).json()['id']
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']

    def ids():
        return {r['vulnerabilityId'] for r in
                client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}

    assert ids() == set()          # 非認証では何も見えない
    _ssh_cred(sid)
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assert 'netlab-snmp-rw-community' in ids()


def test_short_privileged_password_is_flagged():
    _device('t-np-b', 'VULN-B', '10.200.0.2',
            ['username netadmin privilege 15 secret short', 'aaa new-model'])
    sid = client.post('/api/3/sites', json={
        'name': 'S', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.2']}}}}).json()['id']
    _ssh_cred(sid)
    client.post(f'/api/3/sites/{sid}/scans', json={})
    aid = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]['id']
    assert 'netlab-weak-local-password' in {
        r['vulnerabilityId'] for r in
        client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}


def test_disabled_credential_does_not_authenticate_the_scan():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/site_credentials', json={
        'name': 'off', 'enabled': False,
        'account': {'service': 'ssh', 'username': 'u', 'password': 'p'}})
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assert client.get('/api/3/scans').json()['resources'][-1]['credentialed'] is False


def test_credential_can_be_deleted():
    sid = _lab()
    cid = _ssh_cred(sid).json()['id']
    assert client.delete(
        f'/api/3/sites/{sid}/site_credentials/{cid}').status_code == 200
    assert client.get(
        f'/api/3/sites/{sid}/site_credentials').json()['resources'] == []
    assert client.delete(
        f'/api/3/sites/{sid}/site_credentials/{cid}').status_code == 404


def test_credentials_on_unknown_site_are_404():
    assert client.get('/api/3/sites/999/site_credentials').status_code == 404
    assert client.post('/api/3/sites/999/site_credentials',
                       json={'name': 'x'}).status_code == 404


# ══════════════════════════════════════════
# 脆弱性例外
# ══════════════════════════════════════════
def _exception(vid='netlab-snmp-default-community', scope=None):
    return client.post('/api/3/vulnerability_exceptions', json={
        'vulnerability': vid, 'reason': 'Acceptable Risk',
        'comment': 'lab segment', 'scope': scope or {'type': 'global'}})


def test_exception_starts_under_review_and_does_not_apply_yet():
    sid = _lab()
    _exception()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    site = client.get(f'/api/3/sites/{sid}').json()
    assert site['vulnerabilities']['critical'] >= 1      # まだ効かない
    eid = list(client.get('/api/3/vulnerability_exceptions')
               .json()['resources'])[0]['id']
    assert client.get(f'/api/3/vulnerability_exceptions/{eid}'
                      ).json()['state'] == 'under-review'


def test_approved_exception_drops_the_finding_and_the_risk_score():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    before = client.get(f'/api/3/sites/{sid}').json()

    eid = _exception().json()['id']
    assert client.post(f'/api/3/vulnerability_exceptions/{eid}/approve',
                       json={'comment': 'ok'}).status_code == 200
    client.post(f'/api/3/sites/{sid}/scans', json={})
    after = client.get(f'/api/3/sites/{sid}').json()

    assert after['vulnerabilities']['total'] == before['vulnerabilities']['total'] - 1
    assert after['riskScore'] < before['riskScore']
    aid = next(a['id'] for a in
               client.get(f'/api/3/sites/{sid}/assets').json()['resources']
               if a['ip'] == '10.200.0.1')
    assert 'netlab-snmp-default-community' not in {
        r['vulnerabilityId'] for r in
        client.get(f'/api/3/assets/{aid}/vulnerabilities').json()['resources']}


def test_rejected_exception_has_no_effect():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
    before = client.get(f'/api/3/sites/{sid}').json()['vulnerabilities']['total']
    eid = _exception().json()['id']
    client.post(f'/api/3/vulnerability_exceptions/{eid}/reject')
    client.post(f'/api/3/sites/{sid}/scans', json={})
    assert client.get(f'/api/3/sites/{sid}'
                      ).json()['vulnerabilities']['total'] == before


def test_asset_scoped_exception_only_covers_that_asset():
    sid = _lab()
    # 両方の装置で上がる所見を選ぶため、B側もSNMPを開ける
    for c in ('configure terminal', 'snmp-server community public ro', 'end'):
        _cli('t-np-b', c)
    client.post(f'/api/3/sites/{sid}/scans', json={})

    eid = _exception(scope={'type': 'asset', 'id': '10.200.0.1'}).json()['id']
    client.post(f'/api/3/vulnerability_exceptions/{eid}/approve')
    client.post(f'/api/3/sites/{sid}/scans', json={})

    assets = {a['ip']: a for a in
              client.get(f'/api/3/sites/{sid}/assets').json()['resources']}
    assert 'netlab-snmp-default-community' not in assets['10.200.0.1']['_vuln_ids']
    assert 'netlab-snmp-default-community' in assets['10.200.0.2']['_vuln_ids']


def test_site_scoped_exception_does_not_leak_to_another_site():
    sid_a = _lab()
    sid_b = client.post('/api/3/sites', json={
        'name': 'Other', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.1']}}}}).json()['id']
    eid = _exception(scope={'type': 'site', 'id': sid_a}).json()['id']
    client.post(f'/api/3/vulnerability_exceptions/{eid}/approve')
    client.post(f'/api/3/sites/{sid_a}/scans', json={})
    client.post(f'/api/3/sites/{sid_b}/scans', json={})

    assets = client.get(f'/api/3/sites/{sid_b}/assets').json()['resources']
    assert 'netlab-snmp-default-community' in assets[0]['_vuln_ids']


def test_exception_validation():
    assert client.post('/api/3/vulnerability_exceptions',
                       json={'reason': 'Acceptable Risk'}).status_code == 400
    r = client.post('/api/3/vulnerability_exceptions',
                    json={'vulnerability': 'no-such',
                          'reason': 'Acceptable Risk'})
    assert r.status_code == 400 and 'unknown vulnerability' in r.json()['message']
    r = client.post('/api/3/vulnerability_exceptions',
                    json={'vulnerability': 'netlab-telnet-cleartext',
                          'reason': 'because'})
    assert r.status_code == 400 and 'reason must be one of' in r.json()['message']
    r = client.post('/api/3/vulnerability_exceptions',
                    json={'vulnerability': 'netlab-telnet-cleartext',
                          'reason': 'Other', 'scope': {'type': 'site'}})
    assert r.status_code == 400 and 'scope.id is required' in r.json()['message']


def test_exception_cannot_be_reviewed_twice():
    eid = _exception().json()['id']
    assert client.post(
        f'/api/3/vulnerability_exceptions/{eid}/approve').status_code == 200
    r = client.post(f'/api/3/vulnerability_exceptions/{eid}/reject')
    assert r.status_code == 400
    assert 'already "approved"' in r.json()['message']


def test_unsupported_review_action_is_rejected():
    eid = _exception().json()['id']
    r = client.post(f'/api/3/vulnerability_exceptions/{eid}/escalate')
    assert r.status_code == 400
    assert 'unsupported review action' in r.json()['message']


def test_exception_delete_and_404s():
    eid = _exception().json()['id']
    assert client.delete(
        f'/api/3/vulnerability_exceptions/{eid}').status_code == 200
    assert client.get(
        f'/api/3/vulnerability_exceptions/{eid}').status_code == 404
    assert client.post(
        '/api/3/vulnerability_exceptions/999/approve').status_code == 404


def test_report_marks_excepted_findings():
    sid = _lab()
    eid = _exception().json()['id']
    client.post(f'/api/3/vulnerability_exceptions/{eid}/approve')
    client.post(f'/api/3/sites/{sid}/scans', json={})
    rid = client.post('/api/3/reports', json={
        'name': 'Audit', 'scope': {'sites': [sid]}}).json()['id']
    client.post(f'/api/3/reports/{rid}/generate')
    body = client.get(f'/api/3/reports/{rid}/content').text
    assert '[excepted] SNMP Agent Default Community Name (public)' in body


def test_remediation_loop_drives_the_risk_score_down():
    """この実装の全体像: 設定を直すたびにリスクが下がること

    discovery → 非認証 → 認証 → 是正 → 例外承認、と進めて
    件数とリスクスコアが期待どおり動くかを一本で見る。
    """
    _device('t-np-a', 'VULN-A', '10.200.0.1',
            ['snmp-server community public ro',
             'snmp-server community wr1te rw',
             'netconf-yang', 'gnxi', 'gnxi server'])
    sid = client.post('/api/3/sites', json={
        'name': 'Lab', 'scan': {'assets': {'includedTargets': {
            'addresses': ['10.200.0.1']}}}}).json()['id']

    def run(**kw):
        client.post(f'/api/3/sites/{sid}/scans', json=kw)
        a = client.get(f'/api/3/sites/{sid}/assets').json()['resources'][0]
        return set(a['_vuln_ids']), a['riskScore']

    # 1. discovery — 評価しないので所見ゼロ
    assert run(templateId='discovery') == (set(), 0.0)

    # 2. 非認証 — 開いているポートから分かるぶんだけ
    unauth, risk_unauth = run()
    assert 'netlab-snmp-default-community' in unauth
    assert 'netlab-no-aaa-authentication' not in unauth

    # 3. 認証 — config を読む所見が上乗せされる
    _ssh_cred(sid)
    auth, risk_auth = run()
    assert unauth < auth
    assert risk_auth > risk_unauth

    # 4. 是正 — SNMPを締めてAAAとユーザを入れる
    for c in ('configure terminal', 'no snmp-server community public',
              'no snmp-server community wr1te',
              'snmp-server community n0t-guessable ro', 'aaa new-model',
              'username netadmin privilege 15 secret Str0ngP@ssw0rd', 'end'):
        _cli('t-np-a', c)
    fixed, risk_fixed = run()
    assert risk_fixed < risk_auth
    for gone in ('netlab-snmp-default-community', 'netlab-snmp-rw-community',
                 'netlab-default-credentials', 'netlab-no-aaa-authentication'):
        assert gone not in fixed

    # 5. 残ったものは例外で許容する
    eid = client.post('/api/3/vulnerability_exceptions', json={
        'vulnerability': 'netlab-grpc-no-tls', 'reason': 'Compensating Control',
        'scope': {'type': 'site', 'id': sid}}).json()['id']
    client.post(f'/api/3/vulnerability_exceptions/{eid}/approve')
    final, risk_final = run()
    assert 'netlab-grpc-no-tls' not in final
    assert risk_final < risk_fixed


def test_report_notes_a_discovery_scan_was_not_assessed():
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={'templateId': 'discovery'})
    rid = client.post('/api/3/reports', json={
        'name': 'Audit', 'scope': {'sites': [sid]}}).json()['id']
    client.post(f'/api/3/reports/{rid}/generate')
    assert 'not assessed — discovery scan' in \
        client.get(f'/api/3/reports/{rid}/content').text
