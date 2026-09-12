"""
Nexpose / InsightVM Console API v3 のエミュレーション

Rapid7 公式の Swagger 2.0 仕様（InsightVM API v3, 206エンドポイント）に基づく。
仕様の取得元:
  https://raw.githubusercontent.com/rapid7/vm-console-client-python/master/api-files/console-swagger.json
  （rapid7/vm-console-client-python は BSD 3-Clause。Swaggerファイル自体は
    リポジトリに取り込まず、仕様を参照して実装している）

**206本すべてではなく、脆弱性スキャナとして筋が通る最小限**に絞っている:

    Site作成 → スキャン実行 → Asset検出 → Vulnerability検出 → Report取得

スキャン対象はこのエミュレータ上の装置（device_sessions）で、装置種別と
バージョンから脆弱性を引き当てる。つまり「本物のプロトコルで動く仮想機器を、
仮想の脆弱性スキャナで診断する」という構成になる。

注意: ここで返す脆弱性は**このエミュレータ用の作り物**であり、実在機器の
脆弱性情報ではない。CVE番号も説明用のダミー。実際の脆弱性判断には使えない。
"""

import re
import time
from collections import OrderedDict


# ══════════════════════════════════════════
# 脆弱性カタログ（このエミュレータ用のダミーデータ）
# ══════════════════════════════════════════
# severity は実機同様 'Critical' / 'Severe' / 'Moderate' の3段階。
# severityScore は 0-10、riskScore は Rapid7 の Real Risk Score を模して
# 0-1000 のスケールで持たせている（CVSS単体ではなく悪用可能性を加味する、
# という考え方だけを再現したもの。数値そのものは作り物）。
VULN_CATALOG = [
    {
        'id': 'netlab-ssh-weak-kex',
        'title': 'SSH Server Supports Weak Key Exchange Algorithms',
        'severity': 'Moderate', 'severityScore': 4, 'riskScore': 214,
        'cves': ['CVE-0000-0001'],
        'categories': ['Network', 'SSH'],
        'description': 'The SSH service offers key exchange algorithms that are '
                       'considered weak. (Emulated finding — not a real advisory.)',
        'solution': 'Restrict the KEX algorithms to modern curves.',
        'exploits': 0, 'malwareKits': 0,
        # 検出条件: このポートが開いていれば検出
        'match_port': 830,
    },
    {
        'id': 'netlab-snmp-default-community',
        'title': 'SNMP Agent Default Community Name (public)',
        'severity': 'Critical', 'severityScore': 9, 'riskScore': 872,
        'cves': ['CVE-0000-0002'],
        'categories': ['Network', 'SNMP'],
        'description': 'The SNMP agent responds to the default community string '
                       '"public", exposing device information. '
                       '(Emulated finding — not a real advisory.)',
        'solution': 'Change the community string and restrict access with an ACL.',
        'exploits': 2, 'malwareKits': 1,
        'match_port': 161,
    },
    {
        'id': 'netlab-telnet-cleartext',
        'title': 'Cleartext Management Protocol Enabled (Telnet)',
        'severity': 'Severe', 'severityScore': 7, 'riskScore': 604,
        'cves': ['CVE-0000-0003'],
        'categories': ['Network'],
        'description': 'Management traffic is carried in cleartext. '
                       '(Emulated finding — not a real advisory.)',
        'solution': 'Disable Telnet and use SSH.',
        'exploits': 1, 'malwareKits': 0,
        'match_port': 23,
    },
    {
        'id': 'netlab-grpc-no-tls',
        'title': 'gNMI Service Exposed Without TLS',
        'severity': 'Severe', 'severityScore': 7, 'riskScore': 588,
        'cves': ['CVE-0000-0004'],
        'categories': ['Network', 'Management'],
        'description': 'The gNMI service accepts connections without transport '
                       'encryption. (Emulated finding — not a real advisory.)',
        'solution': 'Enable the secure server (gnxi secure-server).',
        'exploits': 0, 'malwareKits': 0,
        'match_port': 50052,
    },
    {
        'id': 'netlab-default-credentials',
        'title': 'Default Administrative Credentials In Use',
        'severity': 'Critical', 'severityScore': 10, 'riskScore': 961,
        'cves': ['CVE-0000-0005'],
        'categories': ['Authentication'],
        'description': 'The device accepts a well-known default administrative '
                       'account. (Emulated finding — not a real advisory.)',
        'solution': 'Change the default credentials.',
        'exploits': 3, 'malwareKits': 2,
        'match_default_creds': True,
    },
]

_SEVERITY_ORDER = {'Critical': 0, 'Severe': 1, 'Moderate': 2}

# 装置種別 → OS表記（Assetのos/osFingerprint用）
_OS_BY_TYPE = {
    'catalyst': 'Cisco IOS XE 17.9.3',
    'cisco':    'Cisco IOS 15.7',
    'nexus':    'Cisco NX-OS 10.2',
    'asa':      'Cisco ASA 9.16(4)',
    'sir':      'Fujitsu Si-R G210',
    'srs':      'Fujitsu SR-S324TR1',
    'apresia':  'APRESIA ApresiaLight GM200',
    'bigip':    'F5 BIG-IP 17.1.0',
    'pc':       'Linux 5.x',
}


class NexposeEngine:
    """Nexpose / InsightVM Console のサーバ側状態"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sites = OrderedDict()      # id -> site
        self.scans = OrderedDict()      # id -> scan
        self.assets = OrderedDict()     # id -> asset
        self.reports = OrderedDict()    # id -> report
        self._next = {'site': 1, 'scan': 1, 'asset': 1, 'report': 1}

    def _id(self, kind):
        v = self._next[kind]
        self._next[kind] = v + 1
        return v

    # ── Site ─────────────────────────────
    def create_site(self, body):
        name = (body or {}).get('name')
        if not name:
            return None, 'name is required'
        sid = self._id('site')
        self.sites[sid] = {
            'id': sid,
            'name': name,
            'description': (body or {}).get('description', ''),
            'importance': (body or {}).get('importance', 'normal'),
            'scanTemplate': (body or {}).get('scanTemplate', 'full-audit-without-web-spider'),
            'scanEngine': (body or {}).get('engineId', 1),
            'type': 'static',
            'assets': 0,
            'riskScore': 0.0,
            'lastScanTime': None,
            'targets': list((body or {}).get('scan', {}).get('assets', {})
                            .get('includedTargets', {}).get('addresses', []) or []),
            'vulnerabilities': {'critical': 0, 'severe': 0, 'moderate': 0, 'total': 0},
        }
        return sid, None

    def delete_site(self, sid):
        if sid not in self.sites:
            return False
        self.sites.pop(sid)
        for aid in [a for a, v in self.assets.items() if v.get('_site') == sid]:
            self.assets.pop(aid, None)
        return True

    def set_targets(self, sid, addresses):
        site = self.sites.get(sid)
        if site is None:
            return False
        site['targets'] = list(addresses or [])
        return True

    # ── Scan ─────────────────────────────
    def start_scan(self, sid, device_sessions, body=None):
        """サイトのスキャンを実行し、対象装置からAssetと脆弱性を作る。

        実機は非同期だが、ここでは即座に完了させる（状態遷移だけ再現）。
        """
        site = self.sites.get(sid)
        if site is None:
            return None, f'site {sid} not found'
        targets = self._resolve_targets(site, device_sessions)
        scan_id = self._id('scan')
        now = time.time()

        found = []
        for dev_id, state, ip in targets:
            found.append(self._assess(dev_id, state, ip, sid))

        vsum = {'critical': 0, 'severe': 0, 'moderate': 0, 'total': 0}
        for a in found:
            for k in vsum:
                vsum[k] += a['vulnerabilities'][k]

        self.scans[scan_id] = {
            'id': scan_id,
            'scanName': (body or {}).get('name') or f'Scan {scan_id}',
            'scanType': 'manual',
            'status': 'finished',
            'engineId': site['scanEngine'],
            'engineName': 'Local scan engine',
            'startTime': _iso(now),
            'endTime': _iso(now + 12),
            'duration': 'PT12S',
            'startedBy': 'admin',
            'assets': len(found),
            'message': '',
            'vulnerabilities': vsum,
            '_site': sid,
        }
        site['assets'] = len(found)
        site['lastScanTime'] = _iso(now + 12)
        site['vulnerabilities'] = dict(vsum)
        site['riskScore'] = round(sum(a['riskScore'] for a in found), 2)
        return scan_id, None

    def set_scan_status(self, scan_id, status):
        """pause / stop / resume（実機と同じ遷移だけ許す）"""
        scan = self.scans.get(scan_id)
        if scan is None:
            return False, f'scan {scan_id} not found'
        cur = scan['status']
        allowed = {
            'pause': ({'running'}, 'paused'),
            'resume': ({'paused'}, 'running'),
            'stop': ({'running', 'paused'}, 'stopped'),
        }
        if status not in allowed:
            return False, f'unsupported status: {status}'
        ok_from, new = allowed[status]
        if cur not in ok_from:
            return False, (f'cannot {status} a scan in state "{cur}" '
                           f'(expected one of: {", ".join(sorted(ok_from))})')
        scan['status'] = new
        return True, None

    # ── 評価（スキャンの中身）─────────────
    def _resolve_targets(self, site, device_sessions):
        """サイトのincludedTargetsに載っているIPを持つ装置を拾う。
        ターゲット未指定なら全装置を対象にする。"""
        out = []
        targets = set(site.get('targets') or [])
        for dev_id, state in (device_sessions or {}).items():
            ip = _primary_ip(state)
            if not ip:
                continue
            if targets and ip not in targets:
                continue
            out.append((dev_id, state, ip))
        return out

    def _assess(self, dev_id, state, ip, sid):
        """1台ぶんの診断結果（Asset）を作る"""
        services = _services_of(state)
        open_ports = {s['port'] for s in services}
        vulns = []
        for v in VULN_CATALOG:
            hit = False
            if v.get('match_port') and v['match_port'] in open_ports:
                hit = True
            if v.get('match_default_creds') and _uses_default_creds(state):
                hit = True
            if hit:
                vulns.append(v)

        counts = {'critical': 0, 'severe': 0, 'moderate': 0}
        for v in vulns:
            counts[v['severity'].lower()] += 1

        existing = next((a for a in self.assets.values()
                         if a['ip'] == ip and a.get('_site') == sid), None)
        aid = existing['id'] if existing else self._id('asset')
        asset = {
            'id': aid,
            'ip': ip,
            'hostName': getattr(state, 'hostname', dev_id),
            'os': _OS_BY_TYPE.get(getattr(state, 'device_type', ''), 'Unknown'),
            'type': 'device',
            'mac': _mac_for(dev_id),
            'assessedForVulnerabilities': True,
            'assessedForPolicies': False,
            'addresses': [{'ip': ip, 'mac': _mac_for(dev_id)}],
            'services': services,
            'rawRiskScore': float(sum(v['riskScore'] for v in vulns)),
            'riskScore': float(sum(v['riskScore'] for v in vulns)),
            'vulnerabilities': {
                **counts,
                'total': len(vulns),
                'exploits': sum(v['exploits'] for v in vulns),
                'malwareKits': sum(v['malwareKits'] for v in vulns),
            },
            '_site': sid,
            '_device_id': dev_id,
            '_vuln_ids': [v['id'] for v in vulns],
        }
        self.assets[aid] = asset
        return asset

    # ── Vulnerability ────────────────────
    def list_vulnerabilities(self):
        return [_vuln_public(v) for v in sorted(
            VULN_CATALOG, key=lambda x: _SEVERITY_ORDER[x['severity']])]

    def get_vulnerability(self, vid):
        v = next((x for x in VULN_CATALOG if x['id'] == vid), None)
        return _vuln_public(v) if v else None

    def asset_vulnerabilities(self, aid):
        """GET /api/3/assets/{id}/vulnerabilities の中身"""
        a = self.assets.get(aid)
        if a is None:
            return None
        out = []
        for vid in a['_vuln_ids']:
            v = next(x for x in VULN_CATALOG if x['id'] == vid)
            out.append({
                'id': f'{aid}-{vid}',
                'results': [{'port': v.get('match_port'), 'status': 'vulnerable-version',
                             'proof': f'<p>{v["title"]}</p>'}],
                'since': _iso(time.time()),
                'status': 'vulnerable',
                'vulnerabilityId': vid,
            })
        return out

    def vulnerability_assets(self, vid):
        return [a['id'] for a in self.assets.values() if vid in a['_vuln_ids']]

    def vulnerability_solutions(self, vid):
        v = next((x for x in VULN_CATALOG if x['id'] == vid), None)
        if v is None:
            return None
        return [{'id': f'{vid}-fix', 'summary': {'text': v['solution']},
                 'type': 'configuration'}]

    # ── Report ───────────────────────────
    def create_report(self, body):
        name = (body or {}).get('name')
        if not name:
            return None, 'name is required'
        rid = self._id('report')
        self.reports[rid] = {
            'id': rid,
            'name': name,
            'format': (body or {}).get('format', 'pdf'),
            'template': (body or {}).get('template', 'audit-report'),
            'scope': (body or {}).get('scope', {}),
            'owner': 1,
            '_generated': [],
        }
        return rid, None

    def generate_report(self, rid):
        r = self.reports.get(rid)
        if r is None:
            return None, f'report {rid} not found'
        inst = len(r['_generated']) + 1
        r['_generated'].append({
            'id': inst, 'status': 'complete',
            'generated': _iso(time.time()), 'size': 10240 + inst * 37,
        })
        return inst, None

    def report_history(self, rid):
        r = self.reports.get(rid)
        return None if r is None else list(r['_generated'])

    def report_content(self, rid):
        """レポート本文（人が読めるテキスト）。実機はPDF/HTML等のバイナリだが、
        ここでは検証しやすいテキストで返す。"""
        r = self.reports.get(rid)
        if r is None:
            return None
        scope_sites = (r.get('scope') or {}).get('sites') or list(self.sites)
        lines = [f'Report: {r["name"]}',
                 f'Template: {r["template"]}   Format: {r["format"]}', '']
        for sid in scope_sites:
            site = self.sites.get(sid)
            if site is None:
                continue
            v = site['vulnerabilities']
            lines.append(f'Site {sid}: {site["name"]}')
            lines.append(f'  Assets: {site["assets"]}   '
                         f'Risk score: {site["riskScore"]}')
            lines.append(f'  Vulnerabilities: total={v["total"]} '
                         f'critical={v["critical"]} severe={v["severe"]} '
                         f'moderate={v["moderate"]}')
            for a in self.assets.values():
                if a.get('_site') != sid:
                    continue
                lines.append(f'    {a["ip"]:<16} {a["hostName"]:<16} '
                             f'{a["os"]}')
                for vid in a['_vuln_ids']:
                    vv = next(x for x in VULN_CATALOG if x['id'] == vid)
                    lines.append(f'      [{vv["severity"]:<8}] {vv["title"]}')
            lines.append('')
        return '\n'.join(lines)


# ══════════════════════════════════════════
# ヘルパ
# ══════════════════════════════════════════
def _iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ts))


def _primary_ip(state):
    for _n, info in (getattr(state, 'interfaces', {}) or {}).items():
        ip = info.get('ip') if isinstance(info, dict) else None
        if ip and ip != '127.0.0.1':
            return ip
    return ''


def _mac_for(dev_id):
    h = abs(hash(dev_id))
    return 'aa:bb:cc:%02x:%02x:%02x' % ((h >> 16) & 0xff, (h >> 8) & 0xff, h & 0xff)


def _services_of(state):
    """装置で実際に有効化されている管理サービスをポート一覧にする。

    エミュレータ側で本当にリスナーが上がるもの（SNMP/NETCONF/gNMI）に
    対応させているので、「設定した結果として検出される」という筋が通る。
    """
    out = []

    def add(port, name, protocol='tcp', product='', version=''):
        out.append({'port': port, 'protocol': protocol, 'name': name,
                    'product': product, 'version': version, 'family': ''})

    if getattr(state, 'snmp_community', None):
        add(161, 'SNMP', 'udp', 'net-snmp', 'v2c')
    if getattr(state, 'netconf_enabled', False):
        add(int(getattr(state, 'netconf_ssh_port', 830) or 830), 'SSH',
            'tcp', 'OpenSSH', '8.0')
    if getattr(state, 'gnxi_server', False):
        add(int(getattr(state, 'gnxi_port', 50052) or 50052), 'gNMI',
            'tcp', 'gRPC', '')
    if getattr(state, 'restconf_enabled', False):
        add(443, 'HTTPS', 'tcp', 'nginx', '')
    if getattr(state, 'telnet_enabled', False):
        add(23, 'Telnet', 'tcp', '', '')
    return out


def _uses_default_creds(state):
    """既定の管理者アカウントがそのまま残っているか。

    username コマンドで privilege 15 のユーザが作られていれば、
    その名前と既定名が一致するかで判断する。
    """
    users = getattr(state, 'local_users', None) or {}
    if not users:
        return True          # 何も設定されていない = 既定のまま
    return any(u in ('admin', 'cisco') for u in users)


def _vuln_public(v):
    """内部の検出条件（match_*）を落とした、API公開用の形"""
    return {
        'id': v['id'],
        'title': v['title'],
        'severity': v['severity'],
        'severityScore': v['severityScore'],
        'riskScore': v['riskScore'],
        'cves': list(v['cves']),
        'categories': list(v['categories']),
        'description': {'text': v['description'], 'html': f'<p>{v["description"]}</p>'},
        'exploits': v['exploits'],
        'malwareKits': v['malwareKits'],
        'denialOfService': False,
        'published': '2026-01-01',
        'added': '2026-01-01',
        'modified': '2026-01-01',
    }


def page_of(resources, page=0, size=10):
    """PageOf«T» の形に包む（実機のページング書式）"""
    page, size = max(0, int(page)), max(1, int(size))
    total = len(resources)
    total_pages = (total + size - 1) // size if total else 0
    start = page * size
    return {
        'resources': resources[start:start + size],
        'page': {'number': page, 'size': size,
                 'totalResources': total, 'totalPages': total_pages},
        'links': [{'rel': 'self', 'href': ''}],
    }


nexpose_engine = NexposeEngine()
