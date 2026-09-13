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
import socket
import time
from collections import OrderedDict


# ══════════════════════════════════════════
# スキャンテンプレート
# ══════════════════════════════════════════
# 実機の /api/3/scan_templates 相当。テンプレートによって
# 「ポートを見つけるだけ」か「脆弱性まで評価するか」が変わる。
SCAN_TEMPLATES = OrderedDict([
    ('discovery', {
        'id': 'discovery',
        'name': 'Discovery Scan',
        'description': 'Discovers live assets and open services. '
                       'Does not assess vulnerabilities.',
        'assessVulnerabilities': False,
    }),
    ('full-audit-without-web-spider', {
        'id': 'full-audit-without-web-spider',
        'name': 'Full audit without Web Spider',
        'description': 'Full vulnerability assessment of discovered services.',
        'assessVulnerabilities': True,
    }),
    ('exhaustive', {
        'id': 'exhaustive',
        'name': 'Exhaustive',
        'description': 'Full assessment including checks that only apply '
                       'when credentials are supplied.',
        'assessVulnerabilities': True,
    }),
])

DEFAULT_TEMPLATE = 'full-audit-without-web-spider'

# 認証情報で使えるサービス（実機の SiteCredential.service より抜粋）
CREDENTIAL_SERVICES = ('ssh', 'snmp', 'telnet', 'https')


# ── 検出条件（authenticated check）────────
# 認証スキャンでしか判定できないもの。装置の config を読んで初めて
# 分かる内容を、そのまま関数にしている。
def _chk_no_aaa(state, ports):
    return not getattr(state, 'aaa_new_model', False)


def _chk_weak_local_password(state, ports):
    for u in (getattr(state, 'users', None) or []):
        if int(u.get('privilege', 1)) >= 15 and 0 < len(u.get('password') or '') < 8:
            return True
    return False


def _chk_snmp_rw_community(state, ports):
    return any((c.get('perm') or '').lower() == 'rw'
               for c in (getattr(state, 'snmp_community', None) or []))


def _chk_snmp_default_community(state, ports):
    """既定のコミュニティ名がそのまま使われているか。

    以前はポート161が開いているだけで上げていたため、コミュニティ名を
    変えても所見が消えなかった（タイトルは "(public)" なのに）。
    """
    if 161 not in ports:
        return False
    return any((c.get('name') or '').lower() in ('public', 'private')
               for c in (getattr(state, 'snmp_community', None) or []))


# ══════════════════════════════════════════
# 脆弱性カタログ（このエミュレータ用のダミーデータ）
# ══════════════════════════════════════════
# severity は実機同様 'Critical' / 'Severe' / 'Moderate' の3段階。
# severityScore は 0-10、riskScore は Rapid7 の Real Risk Score を模して
# 0-1000 のスケールで持たせている（CVSS単体ではなく悪用可能性を加味する、
# という考え方だけを再現したもの。数値そのものは作り物）。
#
# 検出条件は次の2種類:
#   match_port      … そのポートが開いていれば検出（非認証で分かる）
#   check           … state を読む関数。requires_auth=True なら
#                     認証情報が設定されたサイトのスキャンでのみ評価する
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
        'check': _chk_snmp_default_community, 'port': 161,
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
        'check': lambda state, ports: _uses_default_creds(state),
    },

    # ── 認証スキャンでのみ判定できるもの ──
    {
        'id': 'netlab-no-aaa-authentication',
        'title': 'AAA Authentication Not Configured',
        'severity': 'Severe', 'severityScore': 7, 'riskScore': 631,
        'cves': ['CVE-0000-0006'],
        'categories': ['Authentication', 'Configuration'],
        'description': 'The device does not use AAA, so login attempts are not '
                       'centrally authenticated or accounted for. '
                       '(Emulated finding — not a real advisory.)',
        'solution': 'Enable "aaa new-model" and point authentication at a '
                    'TACACS+/RADIUS group with local fallback.',
        'exploits': 0, 'malwareKits': 0,
        'check': _chk_no_aaa, 'requires_auth': True,
    },
    {
        'id': 'netlab-weak-local-password',
        'title': 'Privileged Local Account With Short Password',
        'severity': 'Severe', 'severityScore': 8, 'riskScore': 702,
        'cves': ['CVE-0000-0007'],
        'categories': ['Authentication'],
        'description': 'A local account with privilege level 15 has a password '
                       'shorter than 8 characters. '
                       '(Emulated finding — not a real advisory.)',
        'solution': 'Enforce a minimum password length and re-issue the account '
                    'secret.',
        'exploits': 1, 'malwareKits': 0,
        'check': _chk_weak_local_password, 'requires_auth': True,
    },
    {
        'id': 'netlab-snmp-rw-community',
        'title': 'SNMP Community With Read-Write Access',
        'severity': 'Critical', 'severityScore': 9, 'riskScore': 845,
        'cves': ['CVE-0000-0008'],
        'categories': ['SNMP', 'Configuration'],
        'description': 'An SNMP community is configured with read-write access, '
                       'allowing configuration changes over SNMP. '
                       '(Emulated finding — not a real advisory.)',
        'solution': 'Remove the read-write community, or restrict it with an ACL '
                    'and move to SNMPv3.',
        'exploits': 2, 'malwareKits': 0,
        'check': _chk_snmp_rw_community, 'requires_auth': True, 'port': 161,
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
        self.exceptions = OrderedDict()  # id -> vulnerability exception
        self._next = {'site': 1, 'scan': 1, 'asset': 1, 'report': 1,
                      'credential': 1, 'exception': 1}

    def _id(self, kind):
        v = self._next[kind]
        self._next[kind] = v + 1
        return v

    # ── Site ─────────────────────────────
    def create_site(self, body):
        name = (body or {}).get('name')
        if not name:
            return None, 'name is required'
        tmpl = (body or {}).get('scanTemplate', DEFAULT_TEMPLATE)
        if tmpl not in SCAN_TEMPLATES:
            return None, (f'unknown scan template: {tmpl} '
                          f'(known: {", ".join(SCAN_TEMPLATES)})')
        sid = self._id('site')
        self.sites[sid] = {
            'id': sid,
            'name': name,
            'description': (body or {}).get('description', ''),
            'importance': (body or {}).get('importance', 'normal'),
            'scanTemplate': tmpl,
            'scanEngine': (body or {}).get('engineId', 1),
            'credentials': [],
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

    # ── Site credentials ─────────────────
    # 認証スキャン用の資格情報。実機同様パスワードは書き込み専用扱いで、
    # GET しても返さない。
    def add_site_credential(self, sid, body):
        site = self.sites.get(sid)
        if site is None:
            return None, f'site {sid} not found'
        body = body or {}
        name = body.get('name')
        account = body.get('account') or {}
        service = (account.get('service') or '').lower()
        if not name:
            return None, 'name is required'
        if service not in CREDENTIAL_SERVICES:
            return None, (f'unsupported credential service: '
                          f'{account.get("service")!r} '
                          f'(supported: {", ".join(CREDENTIAL_SERVICES)})')
        if service != 'snmp' and not account.get('username'):
            return None, f'username is required for service "{service}"'
        if service == 'snmp' and not account.get('community'):
            return None, 'community is required for service "snmp"'
        cid = self._id('credential')
        site['credentials'].append({
            'id': cid,
            'name': name,
            'description': body.get('description', ''),
            'enabled': bool(body.get('enabled', True)),
            'service': service,
            'username': account.get('username', ''),
            '_secret': account.get('password') or account.get('community') or '',
        })
        return cid, None

    def list_site_credentials(self, sid):
        site = self.sites.get(sid)
        if site is None:
            return None
        return [_credential_public(c) for c in site['credentials']]

    def delete_site_credential(self, sid, cid):
        site = self.sites.get(sid)
        if site is None:
            return False, f'site {sid} not found'
        before = len(site['credentials'])
        site['credentials'] = [c for c in site['credentials'] if c['id'] != cid]
        if len(site['credentials']) == before:
            return False, f'credential {cid} not found on site {sid}'
        return True, None

    # ── Vulnerability exception ──────────
    # 「この所見はうちでは許容する」を登録する運用。承認されたものは
    # 次のスキャンから件数・リスクスコアの計算に入らない。
    def create_exception(self, body):
        body = body or {}
        vid = body.get('vulnerability')
        if not vid:
            return None, 'vulnerability is required'
        if not any(v['id'] == vid for v in VULN_CATALOG):
            return None, f'unknown vulnerability: {vid}'
        reason = body.get('reason')
        if reason not in ('False Positive', 'Compensating Control',
                          'Acceptable Use', 'Acceptable Risk', 'Other'):
            return None, ('reason must be one of: False Positive, '
                          'Compensating Control, Acceptable Use, '
                          'Acceptable Risk, Other')
        scope = body.get('scope') or {}
        stype = (scope.get('type') or 'global').lower()
        if stype not in ('global', 'site', 'asset'):
            return None, f'unsupported scope type: {stype}'
        if stype != 'global' and scope.get('id') is None:
            return None, f'scope.id is required for scope type "{stype}"'
        eid = self._id('exception')
        self.exceptions[eid] = {
            'id': eid,
            'vulnerability': vid,
            'scope': {'type': stype, 'id': scope.get('id')},
            'reason': reason,
            'comment': body.get('comment', ''),
            'state': 'under-review',
            'submittedBy': 'admin',
            'submit': {'date': _iso(time.time()), 'name': 'admin'},
            'review': None,
        }
        return eid, None

    def review_exception(self, eid, action, comment=''):
        exc = self.exceptions.get(eid)
        if exc is None:
            return False, f'exception {eid} not found'
        if action not in ('approve', 'reject'):
            return False, f'unsupported review action: {action}'
        if exc['state'] != 'under-review':
            return False, (f'exception {eid} is already "{exc["state"]}" '
                           f'and cannot be reviewed again')
        exc['state'] = 'approved' if action == 'approve' else 'rejected'
        exc['review'] = {'date': _iso(time.time()), 'name': 'admin',
                         'comment': comment}
        return True, None

    def delete_exception(self, eid):
        return self.exceptions.pop(eid, None) is not None

    def _is_excepted(self, vid, sid, asset_keys):
        """承認済みの例外がこの所見に掛かっているか

        asset スコープは Asset ID でも IP でも指定できるようにしている
        （例外はスキャン前に登録されることがあり、その時点ではまだ
        Asset ID が採番されていないため）。
        """
        for exc in self.exceptions.values():
            if exc['state'] != 'approved' or exc['vulnerability'] != vid:
                continue
            sc = exc['scope']
            if sc['type'] == 'global':
                return True
            if sc['type'] == 'site' and sc['id'] == sid:
                return True
            if sc['type'] == 'asset' and sc['id'] in asset_keys:
                return True
        return False

    # ── Scan ─────────────────────────────
    def start_scan(self, sid, device_sessions, body=None):
        """サイトのスキャンを実行し、対象装置からAssetと脆弱性を作る。

        実機は非同期だが、ここでは即座に完了させる（状態遷移だけ再現）。
        """
        site = self.sites.get(sid)
        if site is None:
            return None, f'site {sid} not found'
        tmpl_id = (body or {}).get('templateId') or site['scanTemplate']
        tmpl = SCAN_TEMPLATES.get(tmpl_id)
        if tmpl is None:
            return None, (f'unknown scan template: {tmpl_id} '
                          f'(known: {", ".join(SCAN_TEMPLATES)})')
        targets = self._resolve_targets(site, device_sessions)
        scan_id = self._id('scan')
        now = time.time()

        probe = bool((body or {}).get('probe', True))
        found = []
        for dev_id, state, ip in targets:
            found.append(self._assess(dev_id, state, ip, sid,
                                      assess=tmpl['assessVulnerabilities'],
                                      credentials=site['credentials'],
                                      probe=probe))
        authenticated = any(a['credentialStatus'] == CRED_OK for a in found)

        vsum = {'critical': 0, 'severe': 0, 'moderate': 0, 'total': 0}
        for a in found:
            for k in vsum:
                vsum[k] += a['vulnerabilities'][k]

        self.scans[scan_id] = {
            'id': scan_id,
            'scanName': (body or {}).get('name') or f'Scan {scan_id}',
            'scanType': 'manual',
            'status': 'finished',
            'scanTemplate': tmpl_id,
            'credentialed': authenticated,
            'probed': probe,
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

    def _assess(self, dev_id, state, ip, sid, assess=True, credentials=(),
                probe=True):
        """1台ぶんの診断結果（Asset）を作る

        assess=False（discovery テンプレート）ならサービスの検出までで止め、
        脆弱性は評価しない。probe=True なら候補ポートに本当に接続して
        開閉を確かめる。

        資格情報は**装置ごとに実際に試す**。認証が通った装置だけが
        requires_auth の所見まで評価される（通らない装置では、
        本物のスキャナ同様、外から見える範囲しか分からない）。
        """
        services = _services_of(state, ip, probe=probe)
        if probe:
            authenticated, cred_status, cred_detail = verify_credentials(
                credentials, ip, services)
        else:
            # モデル読みモードでは実際に試せないので、有効な資格情報が
            # あれば通ったものとして扱う（以前の挙動）
            authenticated = any(c.get('enabled') for c in credentials)
            cred_status = CRED_OK if authenticated else CRED_NONE
            cred_detail = []
        open_ports = {s['port'] for s in services}

        existing = next((a for a in self.assets.values()
                         if a['ip'] == ip and a.get('_site') == sid), None)
        aid = existing['id'] if existing else self._id('asset')

        hits = []
        if assess:
            for v in VULN_CATALOG:
                if v.get('requires_auth') and not authenticated:
                    continue
                if v.get('match_port') and v['match_port'] in open_ports:
                    hits.append(v)
                elif v.get('check') and v['check'](state, open_ports):
                    hits.append(v)

        # 承認済みの例外が掛かっているものは件数にもリスクにも入れない
        keys = {aid, ip}
        vulns = [v for v in hits if not self._is_excepted(v['id'], sid, keys)]
        excluded = [v['id'] for v in hits if v not in vulns]

        counts = {'critical': 0, 'severe': 0, 'moderate': 0}
        for v in vulns:
            counts[v['severity'].lower()] += 1
        asset = {
            'id': aid,
            'ip': ip,
            'hostName': getattr(state, 'hostname', dev_id),
            'os': _OS_BY_TYPE.get(getattr(state, 'device_type', ''), 'Unknown'),
            'type': 'device',
            'mac': _mac_for(dev_id),
            'assessedForVulnerabilities': bool(assess),
            'assessedForPolicies': False,
            'credentialStatus': cred_status,
            'credentials': cred_detail,
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
            '_excluded_ids': excluded,
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
                'results': [{'port': v.get('match_port') or v.get('port'),
                             'status': 'vulnerable-version',
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
                if not a.get('assessedForVulnerabilities'):
                    lines.append('      (not assessed — discovery scan)')
                for vid in a['_vuln_ids']:
                    vv = next(x for x in VULN_CATALOG if x['id'] == vid)
                    lines.append(f'      [{vv["severity"]:<8}] {vv["title"]}')
                for vid in a.get('_excluded_ids') or []:
                    vv = next(x for x in VULN_CATALOG if x['id'] == vid)
                    lines.append(f'      [excepted] {vv["title"]}')
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


# ══════════════════════════════════════════
# ポートスキャン
# ══════════════════════════════════════════
# このエミュレータは NETCONF(830) / gNMI(50052) / SNMP(161) を本物の
# ソケットで待ち受けている。so スキャナ側も本当に繋いで確かめる。
#
# 以前は state を読んで「設定されているから開いている」としていたが、
# それだと待ち受けに失敗していても開いていることになってしまう
# （実際 NETCONF/gNMI は装置IPに bind できず落ちていた）。
PROBE_TIMEOUT = 0.4


def probe_tcp(ip, port, timeout=PROBE_TIMEOUT):
    """本物のTCP接続を試す。開いていれば True"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def probe_snmp(ip, port=161, community='public', timeout=PROBE_TIMEOUT):
    """本物のSNMP v2c GET（sysDescr）を投げて応答があるか見る。

    UDPなので「繋がるか」では判定できない。実際にリクエストを投げて
    返事が来るかどうかで判断する。
    """
    try:
        from engine.snmp_udp_agent import encode_response  # noqa: F401
        from engine.snmp_udp_agent import (_encode_int, _encode_oid, _tlv,
                                           PDU_GET, TAG_OCTET_STRING,
                                           TAG_SEQUENCE)
    except Exception:                                   # pragma: no cover
        return False
    vb = _tlv(TAG_SEQUENCE, _encode_oid('1.3.6.1.2.1.1.1.0') + _tlv(0x05, b''))
    pdu = _tlv(PDU_GET, _encode_int(1) + _encode_int(0) + _encode_int(0) +
               _tlv(TAG_SEQUENCE, vb))
    msg = _tlv(TAG_SEQUENCE,
               _encode_int(1) +                       # version 1 = v2c
               _tlv(TAG_OCTET_STRING, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (ip, int(port)))
        data, _ = s.recvfrom(4096)
        return bool(data)
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def probe_ssh_login(ip, port, username, password, timeout=4.0):
    """本物のSSHログインを試す。

    このエミュレータのNETCONFサーバは paramiko の実SSHサーバなので、
    資格情報が本当に通るかどうかを実際に認証して確かめられる。
    以前は「登録されていれば認証成功」とみなしていたので、
    でたらめなパスワードでも認証スキャンになっていた。
    """
    try:
        import paramiko
    except Exception:                                   # pragma: no cover
        return False
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(ip, port=int(port), username=username, password=password,
                    timeout=timeout, auth_timeout=timeout, banner_timeout=timeout,
                    allow_agent=False, look_for_keys=False)
        return True
    except Exception:
        # 認証失敗・接続不可・SSHで無い、いずれも「通らなかった」
        return False
    finally:
        try:
            cli.close()
        except Exception:
            pass


# credentialStatus の値。Swagger仕様には列挙が無い（レポート側の
# データモデルにあり、API定義には現れない）ため、実機の表記に
# 寄せた独自の値。
CRED_NONE = 'no-credentials-supplied'
CRED_OK = 'credential-status-success'
CRED_FAILED = 'credential-status-login-failed'
CRED_NO_SERVICE = 'credential-status-service-not-found'


def verify_credentials(credentials, ip, services):
    """サイトの資格情報を本当に試して、認証スキャンにできるか判定する。

    戻り値は (認証できたか, credentialStatus, 明細)。
    """
    enabled = [c for c in credentials if c.get('enabled')]
    if not enabled:
        return False, CRED_NONE, []

    open_tcp = {s['port'] for s in services if s['protocol'] == 'tcp'}
    open_udp = {s['port'] for s in services if s['protocol'] == 'udp'}
    details, any_ok, any_service = [], False, False

    for c in enabled:
        svc, ok, note = c['service'], False, ''
        if svc == 'ssh':
            # SSHを喋るポート（22 / NETCONFの830）を順に試す
            ports = [p for p in (22, 830) if p in open_tcp]
            if not ports:
                note = 'no SSH service found'
            else:
                any_service = True
                for p in ports:
                    if probe_ssh_login(ip, p, c['username'], c['_secret']):
                        ok, note = True, f'authenticated on tcp/{p}'
                        break
                else:
                    note = 'login failed'
        elif svc == 'snmp':
            if 161 not in open_udp:
                note = 'no SNMP service found'
            else:
                any_service = True
                ok = probe_snmp(ip, 161, c['_secret'])
                note = 'community accepted' if ok else 'community rejected'
        else:
            # telnet/https はこのエミュレータに認証できる実体が無い
            note = f'{svc} verification not implemented'
        any_ok = any_ok or ok
        details.append({'id': c['id'], 'name': c['name'], 'service': svc,
                        'verified': ok, 'note': note})

    if any_ok:
        status = CRED_OK
    elif not any_service:
        status = CRED_NO_SERVICE
    else:
        status = CRED_FAILED
    return any_ok, status, details


def candidate_services(state):
    """装置の設定から「開いているはずの」管理サービスを挙げる。

    これはあくまで候補。実際に開いているかは probe で確かめる。
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
    if getattr(state, 'gnxi_secure_server', False):
        add(int(getattr(state, 'gnxi_secure_port', 9339) or 9339), 'gNMI',
            'tcp', 'gRPC', 'TLS')
    if getattr(state, 'restconf_enabled', False):
        add(443, 'HTTPS', 'tcp', 'nginx', '')
    if getattr(state, 'telnet_enabled', False):
        add(23, 'Telnet', 'tcp', '', '')
    return out


# 設定に出ていなくても必ず叩いてみるポート。
# 候補だけを確かめる作りにすると「設定を消したのにリスナーが残っている」
# という状態を見逃す（実際 `no netconf-yang` でポートが開いたままだった）。
# スキャナなのだから、設定ではなく現物を見るのが筋。
WELL_KNOWN_PORTS = [
    (22,    'SSH',     'tcp'),
    (23,    'Telnet',  'tcp'),
    (80,    'HTTP',    'tcp'),
    (443,   'HTTPS',   'tcp'),
    (830,   'SSH',     'tcp'),
    (9339,  'gNMI',    'tcp'),
    (50051, 'gNMI',    'tcp'),
    (50052, 'gNMI',    'tcp'),
]


def _services_of(state, ip=None, probe=True):
    """検出されたサービス一覧。

    probe=True なら実際にソケットを開いて確かめる。対象は
    「設定から予想される候補」＋「よく使われるポート」で、
    応答したものだけを返す。各エントリの 'detectedBy' に、
    どう確かめたかを残す。
    """
    cands = candidate_services(state)
    if not probe or not ip:
        for s in cands:
            s['detectedBy'] = 'configuration'
        return cands

    comm = ''
    for c_ in (getattr(state, 'snmp_community', None) or []):
        comm = c_.get('name') or ''
        break

    by_port = {(s['port'], s['protocol']): s for s in cands}
    for port, name, proto in WELL_KNOWN_PORTS:
        by_port.setdefault((port, proto), {
            'port': port, 'protocol': proto, 'name': name,
            'product': '', 'version': '', 'family': ''})
    # SNMPは設定が無くてもエージェントが動いていることがあるので必ず叩く
    by_port.setdefault((161, 'udp'), {
        'port': 161, 'protocol': 'udp', 'name': 'SNMP',
        'product': '', 'version': '', 'family': ''})

    out = []
    for (port, proto), s in sorted(by_port.items()):
        if proto == 'udp':
            ok = probe_snmp(ip, port, comm or 'public')
            how = 'snmp-get'
        else:
            ok = probe_tcp(ip, port)
            how = 'tcp-connect'
        if ok:
            s['detectedBy'] = how
            out.append(s)
    return out


DEFAULT_ACCOUNT_NAMES = ('admin', 'cisco', 'root')


def _uses_default_creds(state):
    """既定の管理者アカウントがそのまま残っているか。

    注意: 以前は存在しない属性 `state.local_users` を読んでいたため
    常に True（＝常に検出）になっており、ユーザを作り直しても所見が
    消えなかった。実際のローカルユーザは `state.users` に
    [{'name':..., 'privilege':..., 'password':...}] の形で入っている。
    """
    users = getattr(state, 'users', None) or []
    if not users:
        return True          # 何も設定されていない = 既定のまま
    return any((u.get('name') or '').lower() in DEFAULT_ACCOUNT_NAMES
               for u in users)


def _credential_public(c):
    """API公開用。パスワード/コミュニティ文字列は返さない（実機同様）"""
    return {'id': c['id'], 'name': c['name'], 'description': c['description'],
            'enabled': c['enabled'],
            'account': {'service': c['service'], 'username': c['username']}}


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
