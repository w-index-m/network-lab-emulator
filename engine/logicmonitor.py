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
        return out


logicmonitor_engine = LogicMonitorEngine()
