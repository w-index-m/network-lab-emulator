"""
Cisco IOS の `login block-for`(ログイン総当たり対策)と、接続数ベースのスキャン検知

IOS の実コマンド:
  login block-for <秒> attempts <回数> within <秒>
      <within> 秒の間にログイン失敗が <attempts> 回に達すると、<block-for> 秒間
      「Quiet Mode」になり、その間の新しいログイン(SSH/Telnet)を拒否する。
      %SEC_LOGIN-1-QUIET_MODE_ON / %SEC_LOGIN-5-QUIET_MODE_OFF を出す。
  login on-failure log   … 失敗ごとに %SEC_LOGIN-4-LOGIN_FAILED
  login on-success log   … 成功ごとに %SEC_LOGIN-5-LOGIN_SUCCESS
  show login

実機の IOS ルータには、ポートスキャン(接続だけして認証しない)を検知する標準機能は無い。
それはネットワーク側のセンサー(IDS)の役目なので、接続数ベースの検知は /cyber-view の
IDS ルール(engine.traffic_analytics の conn_scan)として別に記録する。
"""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_failures = defaultdict(deque)        # device_id -> deque[t]
_quiet_until = {}                     # device_id -> t
_logger = None                        # fn(device_id, facility, severity, mnemonic, text)


def set_logger(fn):
    global _logger
    _logger = fn


def _log(device_id, sev, mnemonic, text):
    if _logger:
        try:
            _logger(device_id, 'SEC_LOGIN', sev, mnemonic, text)
        except Exception:  # noqa: BLE001 - ログ出力の失敗で認証処理を止めない
            pass


def config(state):
    return getattr(state, 'login_block', None) or {}


def handle_config(state, c):
    """設定コマンドを処理したら True。c は小文字化済みのコマンド"""
    import re
    m = re.match(r'^login\s+block-for\s+(\d+)\s+attempts\s+(\d+)\s+within\s+(\d+)$', c)
    if m:
        lb = dict(config(state))
        lb.update({'block_for': int(m.group(1)), 'attempts': int(m.group(2)), 'within': int(m.group(3))})
        state.login_block = lb
        return True
    if c == 'no login block-for':
        lb = dict(config(state))
        for k in ('block_for', 'attempts', 'within'):
            lb.pop(k, None)
        state.login_block = lb
        return True
    for flag, key in (('login on-failure log', 'on_failure_log'), ('login on-success log', 'on_success_log')):
        if c == flag:
            state.login_block = {**config(state), key: True}
            return True
        if c == 'no ' + flag:
            state.login_block = {**config(state), key: False}
            return True
    return False


def is_quiet(device_id, state, now=None):
    """Quiet Mode 中なら残り秒数、そうでなければ 0。期限切れ時に QUIET_MODE_OFF を出す"""
    now = time.time() if now is None else now
    with _lock:
        until = _quiet_until.get(device_id)
        if until is None:
            return 0
        if now < until:
            return int(until - now) + 1
        _quiet_until.pop(device_id, None)
        _failures[device_id].clear()
    _log(device_id, 5, 'QUIET_MODE_OFF', 'Quiet Mode is OFF, because block period timed out at '
         + time.strftime('%H:%M:%S UTC %a %b %d %Y', time.gmtime(now)))
    return 0


def record_failure(device_id, state, user, src_ip, port=22, now=None):
    now = time.time() if now is None else now
    cfg = config(state)
    if cfg.get('on_failure_log'):
        _log(device_id, 4, 'LOGIN_FAILED', f'Login failed [user: {user}] [Source: {src_ip}] '
             f'[localport: {port}] [Reason: Login Authentication Failed] at '
             + time.strftime('%H:%M:%S UTC %a %b %d %Y', time.gmtime(now)))
    if not cfg.get('attempts'):
        return False
    with _lock:
        dq = _failures[device_id]
        dq.append(now)
        while dq and now - dq[0] > cfg['within']:
            dq.popleft()
        if len(dq) < cfg['attempts'] or _quiet_until.get(device_id, 0) > now:
            return False
        _quiet_until[device_id] = now + cfg['block_for']
    _log(device_id, 1, 'QUIET_MODE_ON', f"Still timeleft for watching failures is {cfg['within']} secs, "
         f'[user: {user}] [Source: {src_ip}] [localport: {port}] [Reason: Login Authentication Failed] '
         f'[ACL: sl_def_acl] at ' + time.strftime('%H:%M:%S UTC %a %b %d %Y', time.gmtime(now)))
    return True


def record_success(device_id, state, user, src_ip, port=22, now=None):
    now = time.time() if now is None else now
    if config(state).get('on_success_log'):
        _log(device_id, 5, 'LOGIN_SUCCESS', f'Login Success [user: {user}] [Source: {src_ip}] '
             f'[localport: {port}] at ' + time.strftime('%H:%M:%S UTC %a %b %d %Y', time.gmtime(now)))


def show_login(device_id, state):
    cfg = config(state)
    if not cfg.get('attempts'):
        return ('     No login delay has been applied.\n'
                '     No Quiet-Mode access list has been configured.\n\n'
                '     Router NOT enabled to watch for login Attacks')
    left = is_quiet(device_id, state)
    with _lock:
        nfail = len(_failures[device_id])
    lines = ['     A default login delay of 1 seconds is applied.',
             '     No Quiet-Mode access list has been configured.', '',
             f"     Router enabled to watch for login Attacks.",
             f"     If more than {cfg['attempts']} login failures occur in {cfg['within']} seconds or less,",
             f"     logins will be disabled for {cfg['block_for']} seconds.", '']
    if left:
        lines.append(f'     Router presently in Quiet-Mode.')
        lines.append(f'     Will remain in Quiet-Mode for {left} seconds.')
        lines.append('     Denying logins from all sources.')
    else:
        lines.append('     Router presently in Normal-Mode.')
        lines.append(f'     Current Watch Window remaining time {cfg["within"]} seconds.')
        lines.append(f'     Present login failure count {nfail}.')
    return '\n'.join(lines)


def running_config_lines(state):
    cfg = config(state)
    out = []
    if cfg.get('attempts'):
        out.append(f"login block-for {cfg['block_for']} attempts {cfg['attempts']} within {cfg['within']}")
    if cfg.get('on_failure_log'):
        out.append('login on-failure log')
    if cfg.get('on_success_log'):
        out.append('login on-success log')
    return out


def reset(device_id=None):
    with _lock:
        if device_id is None:
            _failures.clear()
            _quiet_until.clear()
        else:
            _failures.pop(device_id, None)
            _quiet_until.pop(device_id, None)
