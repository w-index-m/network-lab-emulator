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
    app_module.device_sessions.pop(dev, None)
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
        app_module.device_sessions.pop(d, None)
    yield


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
    """
    sid = _lab()
    client.post(f'/api/3/sites/{sid}/scans', json={})
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
