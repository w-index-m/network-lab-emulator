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
import threading
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

# 実機の SiteCredential.account.permissionElevation の許容値
# （Rapid7公式Swaggerに実在する列挙。"privileged-exec" が
# Cisco の `enable` に相当する）。
PERMISSION_ELEVATION_TYPES = ('none', 'sudo', 'sudosu', 'su', 'pbrun',
                             'privileged-exec')
# 機能として実際にシミュレートしているのはこれだけ。su/sudo/pbrun等は
# UNIX系装置向けで、このエミュレータにその実体が無いため、
# 受理はするが素通しする（何も昇格しない）。
IMPLEMENTED_ELEVATION_TYPES = ('privileged-exec',)


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


# ── 実際に読み取った running-config を見る版 ──
# SSHでログインできた装置では `show running-config` を本当に実行して
# 持ち帰るので、その本文を解析する。DeviceState を覗くのではなく、
# 実機のスキャナと同じ「取得した設定を読む」形にするためのもの。
def _cfg_no_aaa(cfg):
    return not re.search(r'^\s*aaa new-model\s*$', cfg, re.M | re.I)


def _cfg_weak_local_password(cfg):
    for m in re.finditer(r'^\s*username\s+(\S+)(?:\s+privilege\s+(\d+))?'
                         r'\s+(?:password|secret)(?:\s+[057])?\s+(\S+)',
                         cfg, re.M | re.I):
        if int(m.group(2) or 1) >= 15 and len(m.group(3)) < 8:
            return True
    return False


def _cfg_snmp_rw_community(cfg):
    return bool(re.search(r'^\s*snmp-server\s+community\s+\S+\s+RW\s*$',
                          cfg, re.M | re.I))


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
        'check': _chk_no_aaa, 'requires_auth': True, 'check_cfg': _cfg_no_aaa,
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
        'check': _chk_weak_local_password, 'requires_auth': True, 'check_cfg': _cfg_weak_local_password,
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
        'check': _chk_snmp_rw_community, 'requires_auth': True, 'check_cfg': _cfg_snmp_rw_community, 'port': 161,
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
        self._scan_control = {}         # scan_id -> {'pause':..., 'stop':...}
        # reset() をまたいで生き残っているスキャンのバックグラウンド
        # スレッドが、reset後の新しい self.assets/self.scans に書き込んで
        # テスト間で結果が混ざるのを防ぐための世代カウンタ。
        self._generation = getattr(self, '_generation', 0) + 1

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

        elevation = (account.get('permissionElevation') or 'none').lower()
        if elevation not in PERMISSION_ELEVATION_TYPES:
            return None, (f'unsupported permissionElevation: '
                          f'{account.get("permissionElevation")!r} '
                          f'(supported: {", ".join(PERMISSION_ELEVATION_TYPES)})')
        elevation_user = account.get('permissionElevationUsername', '')
        elevation_pw = account.get('permissionElevationPassword', '')
        # 実機の規則: none/pbrun 以外はユーザ名・パスワードとも必須
        if elevation not in ('none', 'pbrun'):
            if not elevation_user:
                return None, ('permissionElevationUsername is required '
                              f'when permissionElevation is "{elevation}"')
            if not elevation_pw:
                return None, ('permissionElevationPassword is required '
                              f'when permissionElevation is "{elevation}"')

        cid = self._id('credential')
        site['credentials'].append({
            'id': cid,
            'name': name,
            'description': body.get('description', ''),
            'enabled': bool(body.get('enabled', True)),
            'service': service,
            'username': account.get('username', ''),
            '_secret': account.get('password') or account.get('community') or '',
            'elevation': elevation,
            'elevation_username': elevation_user,
            '_elevation_secret': elevation_pw,
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
        """サイトのスキャンを開始する。

        実機は非同期で、スキャンは装置を1台ずつ順に評価しながら
        進む。以前はここで全装置を評価し終えてから即座に "finished"
        を返しており、pause/resume/stop は状態遷移の型だけあって
        実際には**テストが `scans[id]['status']` を手で書き換えないと
        到達できない**死んだコードだった。

        装置ごとの評価は常にバックグラウンドスレッド（`_run_scan`）で
        行う。既定（`body['async']` を指定しない）ではこのメソッド
        自身がスレッドの完了を `join()` して待つため、**呼び出し側
        から見た挙動は以前と同じ**（戻ってきた時点で "finished"）。
        既存のテスト・呼び出し側を一切変えずに済ませるための選択。

        `body['async'] = True` を指定すると `join()` せずにすぐ
        "running" のスキャンIDを返す。これで初めて pause/resume/stop
        が実際に意味を持つ（次の装置に進む前のチェックポイントで
        効く。装置単位の粒度で、1台の評価そのものは中断できない）。
        実機のAPI仕様には無い、テスト容易性のためのこのエミュレータ
        独自の拡張 — 詳細は docs/nexpose-api.md 参照。
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

        self.scans[scan_id] = {
            'id': scan_id,
            'scanName': (body or {}).get('name') or f'Scan {scan_id}',
            'scanType': 'manual',
            'status': 'running',
            'scanTemplate': tmpl_id,
            'credentialed': False,
            'probed': probe,
            'engineId': site['scanEngine'],
            'engineName': 'Local scan engine',
            'startTime': _iso(now),
            'endTime': None,
            'duration': None,
            'startedBy': 'admin',
            'assets': 0,
            'message': '',
            'vulnerabilities': {'critical': 0, 'severe': 0, 'moderate': 0,
                               'total': 0},
            '_site': sid,
        }
        ctl = {'pause': threading.Event(), 'stop': threading.Event()}
        self._scan_control[scan_id] = ctl
        worker = threading.Thread(
            target=self._run_scan, daemon=True,
            args=(scan_id, sid, targets, tmpl, list(site['credentials']),
                 probe, ctl, self._generation, now))
        worker.start()
        if not (body or {}).get('async'):
            worker.join()
        return scan_id, None

    def _run_scan(self, scan_id, sid, targets, tmpl, credentials, probe,
                 ctl, generation, start_ts):
        """バックグラウンドスレッド本体。装置を1台ずつ評価する。"""
        found = []
        stopped = False
        for dev_id, state, ip in targets:
            if generation != self._generation:
                return          # reset()された。この結果はもう要らない
            if ctl['stop'].is_set():
                stopped = True
                break
            while ctl['pause'].is_set():
                if ctl['stop'].is_set():
                    stopped = True
                    break
                time.sleep(0.05)
            if stopped or ctl['stop'].is_set():
                stopped = True
                break
            found.append(self._assess(dev_id, state, ip, sid,
                                      assess=tmpl['assessVulnerabilities'],
                                      credentials=credentials, probe=probe))
            scan = self.scans.get(scan_id)
            if scan is None or generation != self._generation:
                return
            # 見つかった台数を随時反映する。paused中にGETしても
            # 「そこまでは終わっている」台数が見えるのが実機と同じ。
            scan['assets'] = len(found)

        if generation != self._generation:
            return
        scan = self.scans.get(scan_id)
        site = self.sites.get(sid)
        if scan is None:
            return
        vsum = {'critical': 0, 'severe': 0, 'moderate': 0, 'total': 0}
        for a in found:
            for k in vsum:
                vsum[k] += a['vulnerabilities'][k]
        authenticated = any(a['credentialStatus'] == CRED_OK for a in found)
        end_ts = time.time()
        scan['status'] = 'stopped' if stopped else 'finished'
        scan['credentialed'] = authenticated
        scan['assets'] = len(found)
        scan['vulnerabilities'] = vsum
        scan['endTime'] = _iso(end_ts)
        scan['duration'] = f'PT{max(1, int(end_ts - start_ts))}S'
        if site is not None:
            site['assets'] = len(found)
            site['lastScanTime'] = scan['endTime']
            site['vulnerabilities'] = dict(vsum)
            site['riskScore'] = round(sum(a['riskScore'] for a in found), 2)
        self._scan_control.pop(scan_id, None)

    def set_scan_status(self, scan_id, status):
        """pause / stop / resume（実機と同じ遷移だけ許す）

        バックグラウンドスレッドの `_run_scan` が次の装置へ進む前の
        チェックポイントでこれを見るので、実際にスキャンの進行を
        止めたり再開したりする（テストが手で status を書き換えて
        「見た目だけ」通していた状態ではない）。
        """
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
        ctl = self._scan_control.get(scan_id)
        # 呼び出し側にはここで即座に新しい状態を見せる。実際に
        # バックグラウンドスレッドが「次の装置へ進むのをやめる」のは
        # 今評価中の装置が終わった直後（装置単位の粒度なので、
        # 中断は多少遅れて効く。実機のスキャンエンジンも同様）。
        if status == 'pause' and ctl is not None:
            ctl['pause'].set()
        elif status == 'resume' and ctl is not None:
            ctl['pause'].clear()
        elif status == 'stop' and ctl is not None:
            ctl['stop'].set()
            ctl['pause'].clear()          # 一時停止中でもすぐ抜けられるように
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
            authenticated, cred_status, cred_detail, config = verify_credentials(
                credentials, ip, services)
        else:
            # モデル読みモードでは実際に試せないので、有効な資格情報が
            # あれば通ったものとして扱う（以前の挙動）
            authenticated = any(c.get('enabled') for c in credentials)
            cred_status = CRED_OK if authenticated else CRED_NONE
            cred_detail, config = [], None
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
                elif config is not None and v.get('check_cfg'):
                    # SSHで実際に取得した running-config を解析する。
                    # DeviceState を覗くのではなく、実機のスキャナと
                    # 同じ「読み取った設定を読む」経路になる。
                    if v['check_cfg'](config):
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


def _ssh_expect(chan, needles, timeout=6.0):
    """paramikoの対話チャンネルから、needlesのどれかが出るまで読む。

    `engine/telnet_cli_agent.py` で使っている素ソケット版
    `_telnet_expect` の paramiko Channel 版。IACの読み飛ばしが
    要らない分だけ単純。
    """
    import time as _t
    buf = b''
    end = _t.time() + timeout
    chan.settimeout(0.5)
    while _t.time() < end:
        try:
            chunk = chan.recv(4096)
        except Exception:
            continue
        if not chunk:
            return None, buf
        buf += chunk
        for n in needles:
            if n in buf:
                return n, buf
    return None, buf


def probe_ssh_login(ip, port, username, password, timeout=4.0,
                    fetch_config=False, enable_password=None):
    """本物のSSHログインを試す。

    このエミュレータのNETCONFサーバ(830)とCLIサーバ(22)は、どちらも
    paramiko の実SSHサーバなので、資格情報が本当に通るかどうかを
    実際に認証して確かめられる。以前は「登録されていれば認証成功」と
    みなしていたので、でたらめなパスワードでも認証スキャンになっていた。

    fetch_config=True かつ CLI が喋れるポート(22)なら、続けて
    `show running-config` を**実際に実行して**中身を持ち帰る。

    enable_password を渡すと、`show running-config` の前に対話シェルで
    `enable` を実行し、そのパスワードで昇格を試みてから読む
    （実機の InsightVM が site credential に持たせる
    `permissionElevation: "privileged-exec"` に相当。
    docs/nexpose-api.md 参照）。

    戻り値は (通ったか, config文字列 or None, elevated)。
    elevated は昇格を試みていなければ None、試みた結果は True/False。
    """
    try:
        import paramiko
    except Exception:                                   # pragma: no cover
        return False, None, None
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(ip, port=int(port), username=username, password=password,
                    timeout=timeout, auth_timeout=timeout, banner_timeout=timeout,
                    allow_agent=False, look_for_keys=False)
    except Exception:
        # 認証失敗・接続不可・SSHで無い、いずれも「通らなかった」
        try:
            cli.close()
        except Exception:
            pass
        return False, None, None

    config = None
    elevated = None
    if fetch_config:
        try:
            if enable_password:
                elevated, fetched = _elevate_and_read_config(
                    cli, enable_password, timeout)
            else:
                _in, out, _err = cli.exec_command('show running-config',
                                                  timeout=timeout)
                fetched = out.read().decode(errors='replace')
            # privilege 1 のアカウントで認証はできたが `show
            # running-config` はprivileged EXEC専用コマンドなので
            # 拒否される、というケースがある（engine/ssh_cli_agent.py
            # の enable 権限昇格を参照）。エラー文言をそのまま config
            # として扱うと、"aaa new-model" 等の文字列検索がすべて
            # 空振りし、正しい所見判定ができなくなる。
            if 'Invalid input detected' not in fetched:
                config = fetched
        except Exception:
            config = None       # NETCONFサブシステムしか無いポート等
    try:
        cli.close()
    except Exception:
        pass
    return True, config, elevated


def _elevate_and_read_config(cli, enable_password, timeout):
    """対話シェルで `enable` してから `show running-config` を読む。

    execチャンネル（`exec_command`）は1コマンド1回きりで対話できない
    ので、`enable` のパスワード入力には対話シェル（`invoke_shell`）
    が要る。戻り値は (昇格できたか, 読めた本文)。
    """
    chan = cli.invoke_shell()
    try:
        _ssh_expect(chan, [b'#', b'>'], timeout)      # 最初のプロンプト
        chan.send('enable\n')
        hit, _buf = _ssh_expect(chan, [b'Password:'], timeout)
        elevated = False
        if hit is not None:
            chan.send(enable_password + '\n')
            hit2, buf2 = _ssh_expect(chan, [b'#'], timeout)
            elevated = (hit2 == b'#' and b'Access denied' not in buf2
                       and b'No password set' not in buf2)
        chan.send('show running-config\n')
        _hit, out = _ssh_expect(chan, [b'\r\nend', b'#'], timeout)
        return elevated, out.decode(errors='replace')
    finally:
        try:
            chan.close()
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
        return False, CRED_NONE, [], None

    open_tcp = {s['port'] for s in services if s['protocol'] == 'tcp'}
    open_udp = {s['port'] for s in services if s['protocol'] == 'udp'}
    details, any_ok, any_service = [], False, False
    config = None

    for c in enabled:
        svc, ok, note = c['service'], False, ''
        if svc == 'ssh':
            # SSHを喋るポートを順に試す。22 のCLIサーバを先に試すのは、
            # そこでログインできれば `show running-config` を実際に
            # 実行して設定を持ち帰れるため（830 は netconf サブシステム
            # しか受け付けないので、認証の可否しか分からない）。
            ports = [p for p in (22, 830) if p in open_tcp]
            # permissionElevation: "privileged-exec" が実機の
            # Cisco enable 相当。su/sudo/pbrun等は受理だけしていて
            # 機能的には何もしない（UNIX系向けでこのエミュレータに
            # 実体が無いため）。
            elevate_pw = (c.get('_elevation_secret')
                         if c.get('elevation') in IMPLEMENTED_ELEVATION_TYPES
                         else None)
            if not ports:
                note = 'no SSH service found'
            else:
                any_service = True
                for p in ports:
                    ok, cfg, elevated = probe_ssh_login(
                        ip, p, c['username'], c['_secret'],
                        fetch_config=(p == 22),
                        enable_password=elevate_pw if p == 22 else None)
                    if ok:
                        note = f'authenticated on tcp/{p}'
                        if elevated is True:
                            note += ' (elevated via enable)'
                        elif elevated is False:
                            note += ', enable failed'
                        if cfg:
                            config = cfg
                            note += f', read running-config ({len(cfg)} bytes)'
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
        elif svc == 'telnet':
            if 23 not in open_tcp:
                note = 'no Telnet service found'
            else:
                any_service = True
                ok, cfg = probe_telnet_login(ip, 23, c['username'],
                                             c['_secret'],
                                             fetch_config=(config is None))
                if ok and cfg:
                    config = cfg
                    note = (f'authenticated on tcp/23 (cleartext), '
                            f'read running-config ({len(cfg)} bytes)')
                elif ok:
                    note = 'authenticated on tcp/23 (cleartext)'
                else:
                    note = 'login failed'
        else:
            # https は装置ごとの待ち受けが無い。RESTCONF はアプリ自身の
            # ポートで /restconf/{device_id}/ として提供していて、
            # 認証もアプリ全体のユーザなので、装置の資格情報としては
            # 試しようがない（docs/nexpose-api.md の未対応範囲を参照）
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
    return any_ok, status, details, config


def _telnet_allowed(state):
    """vty の transport input に telnet が含まれているか

    以前は `state.telnet_enabled` を見ていたが、**この属性を立てる
    コードがどこにも無かった**ので、平文管理の所見は一度も成立しない
    死んだ判定だった。実際の設定値を見る。
    """
    try:
        from engine.telnet_cli_agent import telnet_allowed
    except Exception:                                   # pragma: no cover
        return False
    return telnet_allowed(state)


def probe_telnet_login(ip, port, username, password, timeout=6.0,
                       fetch_config=False):
    """本物のTelnetログインを試す。

    平文なので Username:/Password: のプロンプトに素で流し込むだけ。
    「平文だから資格情報がそのまま流れる」というのが、この所見の
    そもそもの中身でもある。

    戻り値は (通ったか, config文字列 or None)。

    注意: Python 3.13 で telnetlib が標準ライブラリから外れたので、
    素のソケットで実装している（依存を増やさないため）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, int(port)))
    except OSError:
        s.close()
        return False, None
    try:
        hit, _ = _telnet_expect(s, [b'Username:'], timeout)
        if hit is None:
            return False, None
        s.sendall(username.encode() + b'\r\n')
        hit, _ = _telnet_expect(s, [b'Password:'], timeout)
        if hit is None:
            return False, None
        s.sendall(password.encode() + b'\r\n')
        # プロンプト(#)が出れば成功、"Login invalid" なら失敗
        hit, buf = _telnet_expect(s, [b'#', b'Login invalid'], timeout)
        if hit != b'#':
            return False, None
        if not fetch_config:
            return True, None
        s.sendall(b'show running-config\r\n')
        # 出力が終わってプロンプトが戻るまで読む
        import time as _t
        end, out = _t.time() + timeout, b''
        s.settimeout(0.5)
        while _t.time() < end:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                if b'\nend' in out or out.rstrip().endswith(b'#'):
                    break
                continue
            except OSError:
                break
            if not chunk:
                break
            out += _strip_iac(chunk)
        return True, out.decode(errors='replace')
    except OSError:
        return False, None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _telnet_expect(sock, needles, timeout=5.0):
    """needles のどれかが現れるまで読む。現れた needle を返す（無ければ None）"""
    import time as _t
    buf = b''
    end = _t.time() + timeout
    sock.settimeout(0.5)
    while _t.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return None, buf
        if not chunk:
            return None, buf
        buf += _strip_iac(chunk)
        for n in needles:
            if n in buf:
                return n, buf
    return None, buf


def _strip_iac(data):
    """Telnetのオプション交渉バイト列を落とす"""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 255:                      # IAC
            if i + 1 < len(data) and data[i + 1] in (251, 252, 253, 254):
                i += 3
                continue
            if i + 1 < len(data) and data[i + 1] == 250:    # SB ... SE
                j = data.find(bytes([255, 240]), i)
                i = len(data) if j < 0 else j + 2
                continue
            i += 2
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def candidate_services(state):
    """装置の設定から「開いているはずの」管理サービスを挙げる。

    これはあくまで候補。実際に開いているかは probe で確かめる。
    """
    out = []

    def add(port, name, protocol='tcp', product='', version=''):
        out.append({'port': port, 'protocol': protocol, 'name': name,
                    'product': product, 'version': version, 'family': ''})

    if getattr(state, 'ssh_rsa_key', False):
        add(22, 'SSH', 'tcp', 'Cisco SSH', '2.0')
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
    # RESTCONF は装置ごとの :443 ではなく、アプリ自身のポートで
    # /restconf/{device_id}/... として提供している。以前はここで
    # 443 を候補に挙げていたが、そのアドレスでは誰も待ち受けておらず
    # 実プローブでは絶対に確認できない（＝嘘の候補だった）ので外した。
    if _telnet_allowed(state):
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
    """API公開用。パスワード/コミュニティ文字列は返さない（実機同様）。

    permissionElevation と そのusernameは秘密ではないので返す
    （permissionElevationPasswordは秘密なので返さない）。
    """
    account = {'service': c['service'], 'username': c['username']}
    elevation = c.get('elevation', 'none')
    if elevation != 'none':
        account['permissionElevation'] = elevation
        account['permissionElevationUsername'] = c.get('elevation_username', '')
    return {'id': c['id'], 'name': c['name'], 'description': c['description'],
            'enabled': c['enabled'], 'account': account}


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
