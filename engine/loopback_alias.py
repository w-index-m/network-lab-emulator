"""
装置のIPでリスナーを待ち受けられるようにするためのループバックエイリアス

このエミュレータの装置は `ip address 10.200.0.1 255.255.255.0` のように
好きなIPを名乗れるが、ホスト側にそのIPが無ければ実際のソケットは
bind できない（`Cannot assign requested address`）。

    [NETCONF] a (10.200.0.1:830) 起動失敗: [Errno 99] Cannot assign requested address
    [gNMI]    a (10.200.0.1:50052) 起動失敗: Failed to bind to address ...

SNMPエージェントだけが自前でこの回避をしていて、NETCONF/gNMI は
していなかったため、装置IPを振ると SNMP だけ上がって他は落ちる、
という非対称な状態になっていた。共通化してここに置く。

lo に /32 を scope host で足すだけなので、外部には出ない。

GitHub Actions の ubuntu-latest ランナーでCIが"Cannot assign requested
address"で全滅した件(pytest run #54)で判明した注意点: このsshpassなどを
使う実ソケット系テストは、開発用サンドボックス(root実行)ではこの
ensure_loopback_alias が無条件に成功するが、GitHub Actions の既定ユーザ
"runner"はrootではない(パスワード無しsudoは使える)ため、素の
`ip addr add ...` がPermission deniedで失敗し、権限エラーを握りつぶす
仕様(呼び出し側で起動失敗として扱うだけ)のせいでエラーメッセージも
出ないまま、実際にはエイリアスが足されず後続のbindが
`OSError: [Errno 99] Cannot assign requested address` で落ちていた。
root無しでも通るよう、まず素の`ip`を試し、失敗したら
`sudo -n ip ...`（非対話、パスワード入力待ちで固まらない）に
フォールバックする。
"""

import subprocess
import threading

# 同じIPに対して ip コマンドを何度も叩かないための記録
_added = set()
_lock = threading.Lock()


def _run_ip_addr_add(ip: str, use_sudo: bool):
    cmd = ['ip', 'addr', 'add', f'{ip}/32', 'dev', 'lo', 'scope', 'host']
    if use_sudo:
        cmd = ['sudo', '-n'] + cmd
    return subprocess.run(cmd, capture_output=True, timeout=5)


def ensure_loopback_alias(ip: str) -> bool:
    """`ip` をループバックに足して bind 可能にする。

    既にある場合や、権限が無くて失敗した場合も例外は投げない
    （bind 側で失敗が観測できるので、ここで止める意味が無い）。
    戻り値は「呼び出し後にエイリアスが存在するとみなせるか」。
    """
    if not ip or ip == '127.0.0.1':
        return True
    with _lock:
        if ip in _added:
            return True
        try:
            r = _run_ip_addr_add(ip, use_sudo=False)
        except Exception:
            return False
        # rc=2 は "File exists"（既に足されている）なので成功扱い
        ok = r.returncode == 0 or b'File exists' in (r.stderr or b'')
        if not ok and b'Operation not permitted' in (r.stderr or b''):
            # root権限が無い(CI等)。パスワード無しsudoでリトライする。
            try:
                r = _run_ip_addr_add(ip, use_sudo=True)
                ok = r.returncode == 0 or b'File exists' in (r.stderr or b'')
            except Exception:
                ok = False
        if ok:
            _added.add(ip)
        return ok


def forget(ip: str):
    """テスト用。キャッシュから落として次回もう一度 ip コマンドを走らせる"""
    with _lock:
        _added.discard(ip)
