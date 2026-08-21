"""
BIG-IP / F5OS 診断・バックアップ 一括取得ツール
SSH (paramiko) で接続し、プラットフォームを自動判別して各ファイルを生成・ SCP でローカルに保存する。

取得内容 (--mode で選択):
  qkview  : qkview のみ
  ucs     : UCS / F5OS バックアップのみ
  all     : 両方（デフォルト）

対応プラットフォーム:
  - TMOS (BIG-IP iSeries / VIPRION blade / 従来型) … SSH(tmsh) で生成、SCP回収
  - F5OS  (rSeries / VELOS chassis / VELOS blade) … REST API で生成、SCP回収

事前準備:
    pip install paramiko scp requests

hosts.txt の形式 (1行1台、プラットフォーム指定は省略可):
    192.168.1.1
    192.168.1.2          tmos
    192.168.1.3          f5os
    10.202.127.253       tmos   LTM0344A     # 3列目=ホスト名(ファイル名に使用)
"""

import os
import re
import sys
import time
import logging
import argparse
import getpass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import paramiko
import requests
import urllib3
from scp import SCPClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # noqa: S501 - 内部ネットワーク前提

TMOS_QKVIEW_REMOTE_DIR = "/var/tmp"
TMOS_UCS_REMOTE_DIR = "/var/local/ucs"
QKVIEW_TIMEOUT_SEC = 600
UCS_TIMEOUT_SEC = 300
TMOS_USERNAME = "root"
F5OS_USERNAME = "admin"
F5OS_API_PORT = 443

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("qkview_collector.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


class Platform(Enum):
    TMOS = auto()
    F5OS = auto()
    UNKNOWN = auto()


# ---------------------------------------------------------------------------
# SSH ユーティリティ
# ---------------------------------------------------------------------------

def run_command(ssh: paramiko.SSHClient, command: str, timeout: int = 60, use_pty: bool = True) -> tuple[str, str]:
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout, get_pty=use_pty)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    return out, err


def _scp_progress(filename, size, sent):
    if size > 0:
        pct = int(sent / size * 100)
        name = filename.decode(errors='replace') if isinstance(filename, bytes) else filename
        print(f"\r  転送中: {name} {pct}%", end="", flush=True)
        if sent >= size:
            print()


def download_file(ssh: paramiko.SSHClient, remote_path: str, local_dir: Path, hostname: str) -> Path:
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / os.path.basename(remote_path)
    log.info(f"[{hostname}] ダウンロード開始: {remote_path} -> {local_file}")
    with SCPClient(ssh.get_transport(), progress=_scp_progress) as scp:
        scp.get(remote_path, str(local_file))
    log.info(f"[{hostname}] ダウンロード完了: {local_file}")
    return local_file


# ---------------------------------------------------------------------------
# プラットフォーム判別
# ---------------------------------------------------------------------------

def detect_platform(ssh: paramiko.SSHClient, hostname: str) -> Platform:
    """
    TMOS: `tmsh show sys version` が成功する
    F5OS: `show system information` (F5OS CLI) が成功する
    """
    out, _ = run_command(ssh, "tmsh show sys version", timeout=20)
    if "BIG-IP" in out or "Version" in out:
        log.info(f"[{hostname}] プラットフォーム判別: TMOS")
        return Platform.TMOS

    out, _ = run_command(ssh, "show system information", timeout=20, use_pty=False)
    if "F5OS" in out or "Platform" in out or "system" in out.lower():
        log.info(f"[{hostname}] プラットフォーム判別: F5OS")
        return Platform.F5OS

    log.warning(f"[{hostname}] プラットフォームを判別できませんでした。TMOS として処理します。")
    return Platform.TMOS


# ---------------------------------------------------------------------------
# TMOS qkview
# ---------------------------------------------------------------------------

def tmos_generate_qkview(ssh: paramiko.SSHClient, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"qkview_{hostname}_{timestamp}.qkview"
    remote_file = f"{TMOS_QKVIEW_REMOTE_DIR}/{filename}"

    log.info(f"[{hostname}] [TMOS] qkview 生成開始: {remote_file}")
    out, err = run_command(ssh, f"qkview -f {remote_file}", timeout=QKVIEW_TIMEOUT_SEC)

    if err and "error" in err.lower():
        raise RuntimeError(f"qkview 生成エラー: {err}")

    check_out, _ = run_command(ssh, f"ls -lh {remote_file}")
    if filename not in check_out:
        raise FileNotFoundError(f"生成ファイルが見つかりません: {remote_file}")

    log.info(f"[{hostname}] [TMOS] qkview 生成完了")
    return remote_file


def process_tmos(ssh: paramiko.SSHClient, hostname: str, local_dir: Path) -> Path:
    remote_path = tmos_generate_qkview(ssh, hostname)
    local_file = download_file(ssh, remote_path, local_dir, hostname)
    run_command(ssh, f"rm -f {remote_path}")
    log.info(f"[{hostname}] [TMOS] リモート一時ファイルを削除しました")
    return local_file


# ---------------------------------------------------------------------------
# F5OS REST API ヘルパー
# ---------------------------------------------------------------------------

def _f5os_api_post(host: str, username: str, password: str, path: str, body: dict, timeout: int = 60) -> dict:
    url = f"https://{host}:{F5OS_API_PORT}/restconf{path}"
    resp = requests.post(
        url,
        auth=(username, password),
        headers={"Content-Type": "application/yang-data+json"},
        json=body,
        verify=False,
        timeout=timeout,
    )
    if not resp.ok:
        raise RuntimeError(f"REST API エラー [{resp.status_code}]: {resp.text[:300]}")
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# F5OS qkview (REST API)
# ---------------------------------------------------------------------------

def f5os_generate_qkview_api(host: str, username: str, password: str, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"qkview_{hostname}_{timestamp}"

    log.info(f"[{hostname}] [F5OS] qkview 生成開始 (filename={filename})")
    result = _f5os_api_post(
        host, username, password,
        "/operations/openconfig-system:system/f5-diagnostics:diagnostics/f5-diagnostics:qkview/f5-diagnostics:capture",
        {"f5-diagnostics:filename": filename, "f5-diagnostics:timeout": 0},
        timeout=30,
    )
    log.info(f"[{hostname}] [F5OS] qkview 開始 API 応答: {result}")

    # qkview は非同期生成のためステータスをポーリング
    for i in range(QKVIEW_TIMEOUT_SEC // 10):
        time.sleep(10)
        try:
            status = _f5os_api_post(
                host, username, password,
                "/operations/openconfig-system:system/f5-diagnostics:diagnostics/f5-diagnostics:qkview/f5-diagnostics:status",
                {},
                timeout=30,
            )
        except Exception as e:
            log.warning(f"[{hostname}] [F5OS] qkview ステータス取得失敗 (無視): {e}")
            continue
        log.info(f"[{hostname}] [F5OS] qkview ステータス: {status}")
        # 応答の result 文字列に "Busy":false または "Percent":100 が含まれれば完了
        result_str = str(status)
        if '"Busy": false' in result_str or '"Busy":false' in result_str or '"Percent": 100' in result_str:
            log.info(f"[{hostname}] [F5OS] qkview 生成完了")
            break
    else:
        raise TimeoutError(f"F5OS qkview タイムアウト ({QKVIEW_TIMEOUT_SEC}秒)")

    # SCP ダウンロード用パス
    return f"diags/shared/qkview/{filename}"


def process_f5os(ssh: paramiko.SSHClient, host: str, username: str, password: str, hostname: str, local_dir: Path) -> Path:
    remote_path = f5os_generate_qkview_api(host, username, password, hostname)
    return download_file(ssh, remote_path, local_dir, hostname)


# ---------------------------------------------------------------------------
# TMOS UCS バックアップ
# ---------------------------------------------------------------------------

def tmos_generate_ucs(ssh: paramiko.SSHClient, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{hostname}_{timestamp}.ucs"
    remote_file = f"{TMOS_UCS_REMOTE_DIR}/{filename}"

    log.info(f"[{hostname}] [TMOS] UCS 生成開始: {remote_file}")
    out, err = run_command(ssh, f"tmsh save sys ucs {remote_file}", timeout=UCS_TIMEOUT_SEC)

    if "error" in out.lower() or "error" in err.lower():
        raise RuntimeError(f"UCS 生成エラー: {out or err}")

    check_out, _ = run_command(ssh, f"ls -lh {remote_file}")
    if filename not in check_out:
        raise FileNotFoundError(f"生成ファイルが見つかりません: {remote_file}")

    log.info(f"[{hostname}] [TMOS] UCS 生成完了")
    return remote_file


def process_tmos_ucs(ssh: paramiko.SSHClient, hostname: str, local_dir: Path) -> Path:
    remote_path = tmos_generate_ucs(ssh, hostname)
    local_file = download_file(ssh, remote_path, local_dir, hostname)
    run_command(ssh, f"rm -f {remote_path}")
    log.info(f"[{hostname}] [TMOS] リモート UCS を削除しました")
    return local_file


# ---------------------------------------------------------------------------
# F5OS バックアップ (REST API)
# ---------------------------------------------------------------------------

def f5os_generate_backup_api(host: str, username: str, password: str, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = hostname.replace("-", "_")
    filename = f"bkup_{safe_name}_{timestamp}"

    log.info(f"[{hostname}] [F5OS] バックアップ生成開始 (name={filename})")
    result = _f5os_api_post(
        host, username, password,
        "/operations/openconfig-system:system/f5-database:database/f5-database:config-backup",
        {"f5-database:name": filename, "f5-database:proceed": "yes"},
        timeout=UCS_TIMEOUT_SEC,
    )
    log.info(f"[{hostname}] [F5OS] バックアップ API 応答: {result}")
    return f"configs/{filename}"


def process_f5os_backup(ssh: paramiko.SSHClient, host: str, username: str, password: str, hostname: str, local_dir: Path) -> Path:
    remote_path = f5os_generate_backup_api(host, username, password, hostname)
    return download_file(ssh, remote_path, local_dir, hostname)


# ---------------------------------------------------------------------------
# ホスト処理
# ---------------------------------------------------------------------------

def _ssh_connect(ssh: paramiko.SSHClient, host: str, username: str, password: str | None, key_file: str | None) -> None:
    kwargs: dict = {"username": username, "timeout": 30}
    if key_file:
        kwargs["key_filename"] = key_file
    else:
        kwargs["password"] = password
    ssh.connect(host, **kwargs)


def process_host(
    host: str,
    label: str,
    password: str | None,
    key_file: str | None,
    local_dir: Path,
    forced_platform: Platform | None,
    mode: str = "all",
) -> bool:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507 - 内部ネットワーク前提

    try:
        if forced_platform == Platform.TMOS:
            username = TMOS_USERNAME
        elif forced_platform == Platform.F5OS:
            username = F5OS_USERNAME
        else:
            username = None

        platform = forced_platform

        if username:
            log.info(f"[{host}] 接続中 (user={username})...")
            _ssh_connect(ssh, host, username, password, key_file)
            log.info(f"[{host}] 接続成功")
            if not platform:
                platform = detect_platform(ssh, host)
        else:
            # 自動判別: TMOS (root) → F5OS (admin) の順で試行
            for try_user, try_plat in [(TMOS_USERNAME, Platform.TMOS), (F5OS_USERNAME, Platform.F5OS)]:
                try:
                    log.info(f"[{host}] 接続試行 (user={try_user})...")
                    _ssh_connect(ssh, host, try_user, password, key_file)
                    log.info(f"[{host}] 接続成功 (user={try_user})")
                    platform = detect_platform(ssh, host)
                    break
                except paramiko.AuthenticationException:
                    log.info(f"[{host}] 認証失敗 (user={try_user})、次を試行...")
                    ssh.close()
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                raise RuntimeError(f"すべての認証情報で接続に失敗しました ({TMOS_USERNAME}, {F5OS_USERNAME})")

        collected: list[Path] = []
        errors: list[str] = []
        # ファイル名にプラットフォームサフィックスを付与
        plat_suffix = "F5OS" if platform == Platform.F5OS else "TMOS"
        file_label = f"{label}-{plat_suffix}"

        if mode in ("qkview", "all"):
            try:
                if platform == Platform.F5OS:
                    collected.append(process_f5os(ssh, host, F5OS_USERNAME, password, file_label, local_dir / "qkview"))
                else:
                    collected.append(process_tmos(ssh, file_label, local_dir / "qkview"))
            except Exception as e:
                errors.append(f"qkview: {e}")

        if mode in ("ucs", "all"):
            try:
                if platform == Platform.F5OS:
                    collected.append(process_f5os_backup(ssh, host, F5OS_USERNAME, password, file_label, local_dir / "backup"))
                else:
                    collected.append(process_tmos_ucs(ssh, file_label, local_dir / "ucs"))
            except Exception as e:
                errors.append(f"ucs/backup: {e}")

        for f in collected:
            log.info(f"[{host}] 完了 -> {f}")
        if errors:
            for err in errors:
                log.error(f"[{host}] {err}")
            return len(collected) > 0  # 一部成功でも失敗扱いにしたい場合は False に変更
        return True

    except Exception as e:
        log.error(f"[{host}] 失敗: {e}")
        return False
    finally:
        ssh.close()


# ---------------------------------------------------------------------------
# hosts ファイル読み込み
# ---------------------------------------------------------------------------

def load_hosts(hosts_file: str) -> list[tuple[str, Platform | None, str]]:
    """
    各行: <host> [tmos|f5os] [ホスト名]
    ホスト名の指定がない場合は IP アドレスをファイル名に使用。
    """
    entries: list[tuple[str, Platform | None, str]] = []
    with open(hosts_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            host = parts[0]
            plat: Platform | None = None
            label = host  # ファイル名に使用する識別子
            if len(parts) >= 2:
                token = parts[1].lower()
                if token == "f5os":
                    plat = Platform.F5OS
                elif token == "tmos":
                    plat = Platform.TMOS
            if len(parts) >= 3:
                label = parts[2]
            entries.append((host, plat, label))
    return entries


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="BIG-IP / F5OS qkview 一括取得ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
hosts.txt の形式:
  192.168.1.1              # 自動判別（root で試行→失敗時 admin で再試行）
  192.168.1.2   tmos       # TMOS 固定 (user=root)
  192.168.1.3   f5os       # F5OS 固定 (user=admin)
  10.202.127.253 tmos LTM0344A   # 3列目=ホスト名(ファイル名に使用)

取得モード (--mode):
  all    : qkview + UCS/バックアップ を両方取得（デフォルト）
  qkview : qkview のみ
  ucs    : UCS (TMOS) / バックアップ (F5OS) のみ""",
    )
    parser.add_argument("hosts_file", help="対象ホスト一覧ファイル")
    parser.add_argument("-p", "--password", default=None, help="SSH パスワード")
    parser.add_argument("-k", "--key-file", default=None, help="SSH 秘密鍵ファイルパス")
    parser.add_argument("-o", "--output-dir", default="bigip_output", help="保存先ディレクトリ (デフォルト: bigip_output)")
    parser.add_argument("--mode", choices=["qkview", "ucs", "all"], default="all",
                        help="取得内容: qkview / ucs / all (デフォルト: all)")
    args = parser.parse_args()

    if not args.password and not args.key_file:
        args.password = getpass.getpass("SSH パスワード: ")

    entries = load_hosts(args.hosts_file)
    if not entries:
        log.error("対象ホストが見つかりません。")
        sys.exit(1)

    log.info(f"対象ホスト数: {len(entries)}")
    local_dir = Path(args.output_dir)

    results: dict[str, list[str]] = {"success": [], "failure": []}
    for host, plat, label in entries:
        ok = process_host(host, label, args.password, args.key_file, local_dir, plat, args.mode)
        (results["success"] if ok else results["failure"]).append(label)

    print("\n========== 結果サマリー ==========")
    print(f"成功: {len(results['success'])} 台")
    for h in results["success"]:
        print(f"  ✓ {h}")
    print(f"失敗: {len(results['failure'])} 台")
    for h in results["failure"]:
        print(f"  ✗ {h}")
    print("==================================")

    if results["failure"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
