"""
F5OS API(RESTCONF)経由 qkview / config-backup 取得  ―― WIP（開発中の器）

⚠️ このモジュールは未完成です。SSH方式(bigip_qkview_collector.py の process_f5os)
は動くまま残してあり、API方式はここに実装を流し込む「器」として用意しています。
確定した実機のエンドポイント/認証が分かり次第、下の TODO を埋めてください。

F5OS(rSeries F5OS-A / VELOS F5OS-C)の想定フロー:
  ① capture 発行     : POST  {base}/restconf/... qkview capture (RPC/アクション)
  ② 完了ポーリング   : GET   {base}/restconf/... qkview status
  ③ 一覧→実ファイル名: GET   {base}/restconf/... qkview list
  ④ 取得             : GET   files API で直DL、または file export→SFTP
基本:
  - base = https://<mgmt-ip>        （ポートは版により 443 / 8888 等）
  - 認証 = HTTP Basic (admin) もしくはトークン
  - 自己署名TLSのためラボでは verify=False（運用ではCA必須）

依存: requests（遅延import。API方式を使う時だけ必要）
"""
from pathlib import Path


class F5osApiNotReady(NotImplementedError):
    """F5OS API方式は未実装。実機のエンドポイント確定後に実装する。"""


def _client(host, username, password, port=443, verify=False, timeout=30):
    import requests
    from requests.auth import HTTPBasicAuth
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    base = f"https://{host}:{port}"
    sess = requests.Session()
    sess.auth = HTTPBasicAuth(username, password)
    sess.verify = verify
    sess.headers.update({"Accept": "application/yang-data+json",
                         "Content-Type": "application/yang-data+json"})
    return base, sess, timeout


def collect_qkview(host, username, password, local_dir: Path, port=443) -> Path:
    """F5OS API で qkview を取得する（WIP）。
    TODO: capture/status/list/download の各RESTCONFパスを実機仕様で実装。"""
    base, sess, timeout = _client(host, username, password, port)
    # --- TODO(①capture): sess.post(f"{base}/restconf/operations/...:capture", json={...}) ---
    # --- TODO(②status ): poll sess.get(f"{base}/restconf/data/...qkview...status") ---
    # --- TODO(③list   ): name = sess.get(f"{base}/restconf/data/...qkview...list") ---
    # --- TODO(④取得   ): file export もしくは files API から local_dir へ保存 ---
    raise F5osApiNotReady(
        "F5OS API方式は未実装です。実機のRESTCONFエンドポイント確定後に "
        "tools/f5os_api.py の TODO を実装してください。"
        "当面は SSH 方式（--f5os-method ssh, 既定）をご利用ください。")


def collect_backup(host, username, password, local_dir: Path, port=443) -> Path:
    """F5OS API で config-backup を取得する（WIP）。"""
    raise F5osApiNotReady(
        "F5OS API方式(config-backup)は未実装です。SSH方式をご利用ください。")
