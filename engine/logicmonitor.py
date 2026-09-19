"""
LogicMonitor REST API v3 のエミュレーション。

`engine/nexpose.py`（Nexpose/InsightVM）と同じ考え方: LogicMonitorは
完全SaaS型の製品でオンプレ配布物が無いため、「動かす」ことはできない。
代わりにこのエミュレータ内にREST API v3の一部（Device/Alert）を
実装し、装置側の実際の状態（インタフェースのup/down）がそのまま
Alertに反映される、という運用のループを再現する。

## LMv1署名認証

実機のLogicMonitor REST APIは「LMv1」という独自のHMAC署名方式を
使う（Basic認証やBearerトークンではない）。クライアントは:

    Authorization: LMv1 <AccessId>:<Signature>:<Epoch>

というヘッダーを送る。Signatureは

    Base64(HMAC-SHA256(AccessKey, Method + Epoch + RequestBody + ResourcePath))

で計算する（Method/ResourcePathは大文字小文字を含め正確に一致させる
必要がある）。GETの場合RequestBodyは空文字列。

このエミュレータでは `LM_ACCESS_ID` / `LM_ACCESS_KEY`
（既定: `emulator-access-id` / `emulator-access-key`、Nexposeの
admin/adminと同じ「デモ用の固定資格情報」という位置づけ）を使う。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import time

LM_ACCESS_ID = 'emulator-access-id'
LM_ACCESS_KEY = 'emulator-access-key'

# 署名のずれを許容する範囲(秒)。実機も時計のずれをある程度許容する。
LMV1_TIME_SKEW_SECONDS = 300

_SEVERITY_CRITICAL = 4

# 実機のLogicMonitorの既定インタフェースDataSourceは
# 「ifAdminStatus=up かつ ifOperStatus=down」だけをアラート対象にする
# （ケーブルが挿さっていないだけの notconnect ポートや、管理者が
# 意図的にshutdownしたポートまでアラートにすると、素のスイッチを
# 繋いだだけで大量のノイズになってしまう）。
# ここでは同じ考え方で、「意図した状態」ではない down だけを拾う。
_UNEXPECTED_DOWN_STATUSES = {'down', 'err-disabled'}

# よく知られたSNMP Trap OID(engine/syslog_sender.py や
# tools/snmp_trap_receiver.py と同じ定義)。critical/warning/無視の
# 判定に使う。coldStart/warmStart(再起動)はwarning、linkDownはcritical、
# linkUp(復旧)はアラートを新規に立てる必要が無いので無視する。
TRAP_OID_LINKDOWN = '1.3.6.1.6.3.1.1.5.3'
TRAP_OID_LINKUP = '1.3.6.1.6.3.1.1.5.4'
TRAP_OID_COLDSTART = '1.3.6.1.6.3.1.1.5.1'
TRAP_OID_WARMSTART = '1.3.6.1.6.3.1.1.5.2'
_TRAP_NAMES = {
    TRAP_OID_LINKDOWN: 'linkDown', TRAP_OID_LINKUP: 'linkUp',
    TRAP_OID_COLDSTART: 'coldStart', TRAP_OID_WARMSTART: 'warmStart',
}
_TRAP_IGNORED = {TRAP_OID_LINKUP}  # 復旧イベントは新規Alertにしない
_SEVERITY_WARNING = 2
_SEVERITY_NAME_BY_LM = {_SEVERITY_WARNING: 'warning', _SEVERITY_CRITICAL: 'critical'}

# RFC3164のsyslog severity(0-7)のうち、実機のLogicMonitor既定
# Syslog DataSourceがAlertにするのは概ねwarning以上（4以下）。
# info/debug(6,7)はログとしては記録されてもAlertは立てない
# （実機同様、平常運用のログでAlertが埋め尽くされるのを避けるため）。
_SYSLOG_SEVERITY_TO_LM = {
    0: _SEVERITY_CRITICAL, 1: _SEVERITY_CRITICAL, 2: _SEVERITY_CRITICAL,  # emerg/alert/crit
    3: _SEVERITY_CRITICAL,  # err
    4: _SEVERITY_WARNING,   # warning
}


def compute_lmv1_signature(access_key: str, method: str, epoch: str,
                           request_body: str, resource_path: str) -> str:
    """実機と同じ計算式でLMv1署名を作る（クライアント側の参考実装も兼ねる）。"""
    data = f'{method}{epoch}{request_body}{resource_path}'
    digest = hmac.new(access_key.encode(), data.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(digest.encode()).decode()


def parse_authorization_header(header: str):
    """`LMv1 <id>:<signature>:<epoch>` を分解する。形式不正ならNoneを返す。"""
    if not header or not header.startswith('LMv1 '):
        return None
    payload = header[len('LMv1 '):]
    parts = payload.split(':')
    if len(parts) != 3:
        return None
    access_id, signature, epoch = parts
    return access_id, signature, epoch


def verify_lmv1_auth(header: str, method: str, request_body: str,
                     resource_path: str, now: float = None) -> tuple[bool, str]:
    """Authorizationヘッダーを検証する。(成功したか, 理由)を返す。"""
    parsed = parse_authorization_header(header or '')
    if parsed is None:
        return False, 'missing or malformed Authorization header (expected "LMv1 id:sig:epoch")'
    access_id, signature, epoch = parsed
    if access_id != LM_ACCESS_ID:
        return False, 'unknown Access ID'
    try:
        epoch_val = int(epoch)
    except ValueError:
        return False, 'epoch is not a valid integer (milliseconds)'
    now = now if now is not None else time.time()
    # LogicMonitorの実APIはミリ秒epoch
    skew = abs(now - epoch_val / 1000.0)
    if skew > LMV1_TIME_SKEW_SECONDS:
        return False, f'request expired (clock skew {skew:.0f}s > {LMV1_TIME_SKEW_SECONDS}s)'
    expected = compute_lmv1_signature(LM_ACCESS_KEY, method, epoch, request_body, resource_path)
    if not hmac.compare_digest(expected, signature):
        return False, 'signature mismatch'
    return True, 'ok'


class LogicMonitorEngine:
    """Device/Alertのごく薄いストア。実機はもっと多くのリソース
    (DataSource、Collector、Dashboard等)を持つが、Nexposeの時と
    同じく「筋が通る最小限」に絞っている。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.devices: dict[int, dict] = {}
        self._id_seq = itertools.count(1)
        self._event_id_seq = itertools.count(1)
        # syslog/SNMP Trapから受信したイベント由来のAlert
        # （インタフェース状態から動的に作るAlertとは別に保持する。
        # 実機のLogicMonitorも「イベント由来」と「メトリクス閾値由来」の
        # Alertが両方存在する）
        self.external_alerts: list[dict] = []

    def add_device(self, body: dict):
        name = body.get('name') or body.get('displayName')
        if not name:
            return None, 'name is required'
        host_group_ids = body.get('hostGroupIds', '1')
        did = next(self._id_seq)
        self.devices[did] = {
            'id': did,
            'name': name,
            'displayName': body.get('displayName') or name,
            'hostGroupIds': host_group_ids,
            '_device_id': body.get('_device_id'),  # このエミュレータの実装置とのひも付け
            'createdOn': int(time.time()),
        }
        return did, None

    def list_devices(self):
        return list(self.devices.values())

    def get_device(self, did: int):
        return self.devices.get(did)

    def delete_device(self, did: int) -> bool:
        return self.devices.pop(did, None) is not None

    def find_device_by_ip(self, ip: str):
        """実機のLogicMonitorは装置の`name`欄に監視対象IPを入れるのが
        通例なので、それと同じ約束事でマッチさせる。"""
        return next((d for d in self.devices.values() if d.get('name') == ip), None)

    def _device_alerts(self, lm_device: dict, device_sessions: dict) -> list[dict]:
        """1台ぶんのAlertを、実際の装置状態(DeviceState)から組み立てる。

        Nexposeの脆弱性判定と同じ考え方: 固定の一覧ではなく、
        その時点の実際のインタフェース状態を見て動的に作る。
        """
        real_id = lm_device.get('_device_id')
        state = device_sessions.get(real_id) if real_id else None
        alerts = []
        if state is None:
            return alerts

        for ifname, info in (getattr(state, 'interfaces', {}) or {}).items():
            status = str(info.get('status', '')).lower()
            if status in _UNEXPECTED_DOWN_STATUSES:
                alerts.append({
                    'id': f'{lm_device["id"]}-if-{ifname}',
                    'deviceId': lm_device['id'],
                    'deviceDisplayName': lm_device['displayName'],
                    'resourceTemplateName': 'Interface Status',
                    'instanceName': ifname,
                    'severity': _SEVERITY_CRITICAL,
                    'severityLabel': 'critical',
                    'type': 'datasource',
                    'startEpoch': int(time.time()),
                    'cleared': False,
                    'alertValue': status,
                    'threshold': 'up',
                    'detail': f'{ifname} is {status} (expected up)',
                })

        return alerts

    def list_alerts(self, device_sessions: dict, device_id: int = None) -> list[dict]:
        out = []
        targets = ([self.devices[device_id]] if device_id in self.devices
                  else self.devices.values()) if device_id is not None else self.devices.values()
        for lm_device in targets:
            out.extend(self._device_alerts(lm_device, device_sessions))
        out.extend(a for a in self.external_alerts
                   if device_id is None or a.get('deviceId') == device_id)
        return out

    def ingest_syslog(self, source_ip: str, severity: int, facility_tag: str,
                      message: str) -> dict | None:
        """syslog(RFC3164)を1件受信したときに呼ぶ。実機のLogicMonitorの
        既定Syslog DataSourceと同じくwarning以上だけAlertにする
        （info/debugはログとして記録されるだけでAlertにはしない）。
        Alertにしない場合はNoneを返す。"""
        lm_severity = _SYSLOG_SEVERITY_TO_LM.get(severity)
        if lm_severity is None:
            return None
        device = self.find_device_by_ip(source_ip)
        alert = {
            'id': f'evt-{next(self._event_id_seq)}',
            'deviceId': device['id'] if device else None,
            'deviceDisplayName': device['displayName'] if device else source_ip,
            'resourceTemplateName': 'Syslog',
            'instanceName': facility_tag,
            'severity': lm_severity,
            'severityLabel': _SEVERITY_NAME_BY_LM[lm_severity],
            'type': 'eventsource',
            'startEpoch': int(time.time()),
            'cleared': False,
            'alertValue': facility_tag,
            'threshold': 'n/a',
            'detail': message,
        }
        self.external_alerts.append(alert)
        return alert

    def ingest_trap(self, source_ip: str, trap_oid: str, description: str) -> dict | None:
        """SNMP Trapを1件受信したときに呼ぶ。linkUp(復旧)のように
        新規Alertを立てる必要が無いTrapはNoneを返す。"""
        if trap_oid in _TRAP_IGNORED:
            return None
        trap_name = _TRAP_NAMES.get(trap_oid, trap_oid)
        severity = _SEVERITY_CRITICAL if trap_oid == TRAP_OID_LINKDOWN else _SEVERITY_WARNING
        device = self.find_device_by_ip(source_ip)
        alert = {
            'id': f'evt-{next(self._event_id_seq)}',
            'deviceId': device['id'] if device else None,
            'deviceDisplayName': device['displayName'] if device else source_ip,
            'resourceTemplateName': 'SNMP Trap',
            'instanceName': trap_name,
            'severity': severity,
            'severityLabel': _SEVERITY_NAME_BY_LM[severity],
            'type': 'eventsource',
            'startEpoch': int(time.time()),
            'cleared': False,
            'alertValue': trap_oid,
            'threshold': 'n/a',
            'detail': description or trap_name,
        }
        self.external_alerts.append(alert)
        return alert


logicmonitor_engine = LogicMonitorEngine()
