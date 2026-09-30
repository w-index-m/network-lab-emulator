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
"""

import subprocess
import threading

# 同じIPに対して ip コマンドを何度も叩かないための記録
_added = set()
_lock = threading.Lock()


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
            r = subprocess.run(
                ['ip', 'addr', 'add', f'{ip}/32', 'dev', 'lo', 'scope', 'host'],
                capture_output=True, timeout=5,
            )
        except Exception:
            return False
        # rc=2 は "File exists"（既に足されている）なので成功扱い
        ok = r.returncode == 0 or b'File exists' in (r.stderr or b'')
        if ok:
            _added.add(ip)
        return ok


def forget(ip: str):
    """テスト用。キャッシュから落として次回もう一度 ip コマンドを走らせる"""
    with _lock:
        _added.discard(ip)
