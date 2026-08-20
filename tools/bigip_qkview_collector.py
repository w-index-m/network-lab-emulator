"""
BIG-IP / F5OS 診断・バックアップ 一括取得ツール
SSH (paramiko) で接続し、プラットフォームを自動判別して各ファイルを生成・ SCP でローカルに保存する。

取得内容 (--mode で選択):
  qkview  : qkview のみ
  ucs     : UCS / F5OS バックアップのみ
  all     : 両方（デフォルト）

対応プラットフォーム:
  - TMOS (BIG-IP iSeries / VIPRION blade / 従来型)
  - F5OS  (rSeries / VELOS chassis / VELOS blade)

事前準備:
    pip install paramiko scp

hosts.txt の形式 (1行1台、プラットフォーム指定は省略可):
    192.168.1.1
    192.168.1.2          tmos
    192.168.1.3          f5os
    bigip-hostname.example.com

ユーザ名:
    TMOS 既定 root / F5OS 既定 admin（--tmos-username / --f5os-username で変更）。
    自動判別時は root で接続→認証失敗なら admin で再試行。
"""

import os
import re
import sys
import logging
import argparse
import getpass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import paramiko
try:
    from scp import SCPClient          # あれば進捗表示付きSCPを使う
    _HAS_SCP = True
except ImportError:                     # 無ければ paramiko の SFTP で回収（paramikoは必須）
    SCPClient = None
    _HAS_SCP = False

TMOS_QKVIEW_REMOTE_DIR = "/var/tmp"
F5OS_QKVIEW_REMOTE_DIR = "/var/export/chassis/diagnostics/qkview"
TMOS_UCS_REMOTE_DIR = "/var/local/ucs"
F5OS_BACKUP_REMOTE_DIR = "/var/F5OS/backup"
QKVIEW_TIMEOUT_SEC = 600
UCS_TIMEOUT_SEC = 300

# プラットフォーム別の既定ユーザ名（main で --tmos-username / --f5os-username により上書き）
TMOS_USERNAME = "root"
F5OS_USERNAME = "admin"

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

def run_command(ssh: paramiko.SSHClient, command: str, timeout: int = 60) -> tuple[str, str]:
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout, get_pty=True)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    return out, err


def _scp_progress(filename, size, sent):
    if size > 0:
        pct = int(sent / size * 100)
        print(f"\r  転送中: {filename.decode(errors='replace')} {pct}%", end="", flush=True)
        if sent >= size:
            print()


def download_file(ssh: paramiko.SSHClient, remote_path: str, local_dir: Path, hostname: str) -> Path:
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / os.path.basename(remote_path)
    log.info(f"[{hostname}] ダウンロード開始: {remote_path} -> {local_file}")
    if _HAS_SCP:
        with SCPClient(ssh.get_transport(), progress=_scp_progress) as scp:
            scp.get(remote_path, str(local_file))
    else:
        # scp未インストール時: paramiko の SFTP で回収（paramikoのみで完結）
        sftp = ssh.open_sftp()
        try:
            sftp.get(remote_path, str(local_file))
        finally:
            sftp.close()
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
    if "BIG-IP" in out or "Sys::Version" in out or "Product" in out:
        log.info(f"[{hostname}] プラットフォーム判別: TMOS")
        return Platform.TMOS

    out, _ = run_command(ssh, "show system information", timeout=20)
    low = out.lower()
    if "f5os" in low or "os-version" in low or "system information" in low:
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
# F5OS qkview
# ---------------------------------------------------------------------------

def f5os_generate_qkview(ssh: paramiko.SSHClient, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"qkview_{hostname}_{timestamp}"

    log.info(f"[{hostname}] [F5OS] qkview 生成開始")

    # F5OS CLI でキャプチャ実行
    out, err = run_command(
        ssh,
        f"system diagnostics qkview capture filename {filename}",
        timeout=QKVIEW_TIMEOUT_SEC,
    )
    log.debug(f"[{hostname}] [F5OS] qkview output: {out}")

    if "error" in out.lower() or "error" in err.lower():
        raise RuntimeError(f"F5OS qkview エラー: {out or err}")

    # 生成ファイルのパスを検索（拡張子 .tar.gz または .qkview）
    list_out, _ = run_command(ssh, f"ls -t {F5OS_QKVIEW_REMOTE_DIR}/", timeout=30)
    # ファイル名に timestamp 文字列が含まれる行を探す
    match = re.search(rf"({re.escape(filename)}[^\s]*)", list_out)
    if not match:
        # フォールバック: 最新ファイル
        lines = [l.strip() for l in list_out.splitlines() if l.strip()]
        if not lines:
            raise FileNotFoundError("F5OS qkview 生成ファイルが見つかりません")
        generated = lines[0]
        log.warning(f"[{hostname}] [F5OS] タイムスタンプでマッチせず。最新ファイルを使用: {generated}")
    else:
        generated = match.group(1)

    remote_file = f"{F5OS_QKVIEW_REMOTE_DIR}/{generated}"
    log.info(f"[{hostname}] [F5OS] qkview 生成完了: {remote_file}")
    return remote_file


def process_f5os(ssh: paramiko.SSHClient, hostname: str, local_dir: Path) -> Path:
    remote_path = f5os_generate_qkview(ssh, hostname)
    local_file = download_file(ssh, remote_path, local_dir, hostname)
    # F5OS はシステム管理領域のため削除しない（必要なら以下をコメント解除）
    # run_command(ssh, f"file delete {remote_path}")
    return local_file


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
# F5OS バックアップ
# ---------------------------------------------------------------------------

def f5os_generate_backup(ssh: paramiko.SSHClient, hostname: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{hostname}_{timestamp}"

    log.info(f"[{hostname}] [F5OS] バックアップ生成開始")
    out, err = run_command(ssh, f"system backup create name {filename}", timeout=UCS_TIMEOUT_SEC)

    if "error" in out.lower() or "error" in err.lower():
        raise RuntimeError(f"F5OS バックアップエラー: {out or err}")

    list_out, _ = run_command(ssh, f"ls -t {F5OS_BACKUP_REMOTE_DIR}/", timeout=30)
    match = re.search(rf"({re.escape(filename)}[^\s]*)", list_out)
    if not match:
        lines = [ln.strip() for ln in list_out.splitlines() if ln.strip()]
        if not lines:
            raise FileNotFoundError("F5OS バックアップファイルが見つかりません")
        generated = lines[0]
        log.warning(f"[{hostname}] [F5OS] タイムスタンプでマッチせず。最新ファイルを使用: {generated}")
    else:
        generated = match.group(1)

    remote_file = f"{F5OS_BACKUP_REMOTE_DIR}/{generated}"
    log.info(f"[{hostname}] [F5OS] バックアップ生成完了: {remote_file}")
    return remote_file


def process_f5os_backup(ssh: paramiko.SSHClient, hostname: str, local_dir: Path) -> Path:
    remote_path = f5os_generate_backup(ssh, hostname)
    local_file = download_file(ssh, remote_path, local_dir, hostname)
    return local_file


# ---------------------------------------------------------------------------
# ホスト処理
# ---------------------------------------------------------------------------

def _ssh_connect(ssh: paramiko.SSHClient, host: str, username: str, password: str | None, key_file: str | None) -> None:
    kwargs: dict = {"username": username, "timeout": 30, "allow_agent": False, "look_for_keys": False}
    if key_file:
        kwargs["key_filename"] = key_file
    else:
        kwargs["password"] = password
    ssh.connect(host, **kwargs)


def process_host(
    host: str,
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

        if mode in ("qkview", "all"):
            try:
                if platform == Platform.F5OS:
                    collected.append(process_f5os(ssh, host, local_dir / "qkview"))
                else:
                    collected.append(process_tmos(ssh, host, local_dir / "qkview"))
            except Exception as e:
                errors.append(f"qkview: {e}")

        if mode in ("ucs", "all"):
            try:
                if platform == Platform.F5OS:
                    collected.append(process_f5os_backup(ssh, host, local_dir / "backup"))
                else:
                    collected.append(process_tmos_ucs(ssh, host, local_dir / "ucs"))
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

def load_hosts(hosts_file: str) -> list[tuple[str, Platform | None]]:
    """
    各行: <host> [tmos|f5os]
    プラットフォーム指定がない場合は None（自動判別）。
    """
    entries: list[tuple[str, Platform | None]] = []
    with open(hosts_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            host = parts[0]
            plat: Platform | None = None
            if len(parts) >= 2:
                token = parts[1].lower()
                if token == "f5os":
                    plat = Platform.F5OS
                elif token == "tmos":
                    plat = Platform.TMOS
            entries.append((host, plat))
    return entries


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main():
    global TMOS_USERNAME, F5OS_USERNAME
    parser = argparse.ArgumentParser(
        description="BIG-IP / F5OS qkview 一括取得ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
hosts.txt の形式:
  192.168.1.1              # 自動判別（root で試行→失敗時 admin で再試行）
  192.168.1.2   tmos       # TMOS 固定 (user=root)
  192.168.1.3   f5os       # F5OS 固定 (user=admin)

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
    parser.add_argument("--tmos-username", default=TMOS_USERNAME,
                        help=f"TMOS の既定ユーザ名 (デフォルト: {TMOS_USERNAME})")
    parser.add_argument("--f5os-username", default=F5OS_USERNAME,
                        help=f"F5OS の既定ユーザ名 (デフォルト: {F5OS_USERNAME})")
    parser.add_argument("--platform", choices=["auto", "tmos", "f5os"], default="auto",
                        help="全ホストのプラットフォームを固定 (既定 auto=自動判別)。"
                             "tmos指定でF5OS判別を行わずTMOS処理に固定")
    args = parser.parse_args()

    # 既定ユーザ名の上書き
    TMOS_USERNAME = args.tmos_username
    F5OS_USERNAME = args.f5os_username

    # --platform でプラットフォームを全ホスト固定（TMOS専用運用など）
    forced = {"tmos": Platform.TMOS, "f5os": Platform.F5OS}.get(args.platform)

    if not args.password and not args.key_file:
        args.password = getpass.getpass("SSH パスワード: ")

    entries = load_hosts(args.hosts_file)
    if not entries:
        log.error("対象ホストが見つかりません。")
        sys.exit(1)

    log.info(f"対象ホスト数: {len(entries)}")
    local_dir = Path(args.output_dir)

    results: dict[str, list[str]] = {"success": [], "failure": []}
    for host, plat in entries:
        eff_plat = forced if forced else plat   # --platform 指定があれば全ホスト固定
        ok = process_host(host, args.password, args.key_file, local_dir, eff_plat, args.mode)
        (results["success"] if ok else results["failure"]).append(host)

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
