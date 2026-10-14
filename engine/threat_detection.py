"""Cisco ASAの"threat-detection scanning-threat"(スキャン検知/自動シャン)。

実機ASAはTCP/UDPの接続試行レートをホスト単位で監視し、設定した
average-rate/burst-rateを超えたホストを攻撃者とみなして自動的に
シャン(以降のトラフィックを無条件破棄)する——ZGrab2/nmapのような
短時間多数接続はまさにこの機能が狙う挙動そのもの。

このエミュレータでは、実SSHリスナー(engine/ssh_cli_agent.py)への
実際のTCP接続試行を"接続試行"としてカウントし、実機同様の
rate-interval(観測窓)/average-rate/burst-rateのスライディングウィンドウ
判定を行い、閾値超過で実際にそのソースIPへの以降の接続を拒否する
(フェイクの検知ではなく、本当に以降のZGrab2/nmap的なスキャンを
ブロックする)。

実機CLI構文(Cisco ASA 5500 Series Configuration Guide / Command
Reference):
  threat-detection scanning-threat [shun [except ip-address <ip> <mask>]]
  threat-detection rate scanning-threat rate-interval <sec> average-rate <n> burst-rate <n>
  threat-detection shun duration <sec>
  show threat-detection rate
  show threat-detection shun
  clear threat-detection shun [ip-address <ip>]
  no threat-detection scanning-threat

スコープ外(実装していない): UDP/ICMPスキャンの検知(このエミュレータの
実リスナーはTCPのSSHのみ)、basic-threat(トラフィック全体の異常率)、
複数rate-interval(実機は event-type ごとに最大3つまで設定可能)——
このエミュレータは1 device = 1 rate設定のみ。
"""

import ipaddress
import time
from collections import defaultdict, deque

DEFAULT_RATE_INTERVAL = 600      # 秒(実機の既定値)
DEFAULT_AVERAGE_RATE = 5         # 接続/秒
DEFAULT_BURST_RATE = 10          # 接続/秒
DEFAULT_SHUN_DURATION = 3600     # 秒(実機の既定値)


def default_config():
    """state.threat_detectionの初期値。DeviceState.__init__から呼ぶ。"""
    return {
        'enabled': False,
        'shun': False,
        'shun_duration': DEFAULT_SHUN_DURATION,
        'shun_except': [],        # [(ip, mask), ...]
        'rate_interval': DEFAULT_RATE_INTERVAL,
        'average_rate': DEFAULT_AVERAGE_RATE,
        'burst_rate': DEFAULT_BURST_RATE,
    }


class ThreatDetectionEngine:
    """接続試行カウンタ/シャン済みIPは、他のengine(PppoeEngine等)と同じ
    パターンでここに保持し、DeviceStateには設定値(state.threat_detection)
    のみ置く——ランタイムのdeque/タイムスタンプはJSON永続化できない
    ため state 側に持たせない、という判断。

    DeviceStateは外部のdevice_idを自分では持っていない
    (engine/rules.pyのRuleEngine.process()はdevice_id非対応)ため、
    id(state)をキーに使う。装置が削除され同じdevice_idで再作成されても
    新しいDeviceStateインスタンスになるため実害はない(エミュレータの
    プロセス内メモリに数エントリ残るだけ)。
    """

    def __init__(self):
        self._attempts = defaultdict(lambda: defaultdict(deque))  # key -> src_ip -> deque[ts]
        self._shunned = defaultdict(dict)                          # key -> src_ip -> shunned_at

    @staticmethod
    def _burst_interval(rate_interval):
        # 実機: バーストインターバルはrate-intervalの1/30、最低10秒
        return max(10, rate_interval // 30)

    @staticmethod
    def _is_excepted(src_ip, shun_except):
        try:
            addr = ipaddress.ip_address(src_ip)
        except ValueError:
            return False
        for exc_ip, exc_mask in shun_except:
            try:
                if addr in ipaddress.ip_network(f'{exc_ip}/{exc_mask}', strict=False):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _rate(dq, now, interval):
        if interval <= 0:
            return 0.0
        cutoff = now - interval
        count = sum(1 for t in dq if t >= cutoff)
        return count / interval

    def record_connection_attempt(self, key, src_ip, cfg):
        """実際のTCP接続試行を1件記録する。この接続を直ちに遮断すべき
        (既にシャン済み、またはこの接続で新たに閾値超過しシャンされた)
        なら True を返す。"""
        if not cfg.get('enabled'):
            return False
        now = time.time()
        rate_interval = cfg.get('rate_interval', DEFAULT_RATE_INTERVAL)
        dq = self._attempts[key][src_ip]
        dq.append(now)
        cutoff = now - rate_interval
        while dq and dq[0] < cutoff:
            dq.popleft()

        if self.is_shunned(key, src_ip, cfg):
            return True
        if self._is_excepted(src_ip, cfg.get('shun_except', [])):
            return False

        burst_interval = self._burst_interval(rate_interval)
        avg_rate = self._rate(dq, now, rate_interval)
        burst_rate = self._rate(dq, now, burst_interval)
        if cfg.get('shun') and (avg_rate >= cfg.get('average_rate', DEFAULT_AVERAGE_RATE)
                                 or burst_rate >= cfg.get('burst_rate', DEFAULT_BURST_RATE)):
            self._shunned[key][src_ip] = now
            return True
        return False

    def is_shunned(self, key, src_ip, cfg):
        shunned_at = self._shunned[key].get(src_ip)
        if shunned_at is None:
            return False
        if time.time() - shunned_at > cfg.get('shun_duration', DEFAULT_SHUN_DURATION):
            del self._shunned[key][src_ip]
            return False
        return True

    def get_rates(self, key, cfg):
        now = time.time()
        rate_interval = cfg.get('rate_interval', DEFAULT_RATE_INTERVAL)
        burst_interval = self._burst_interval(rate_interval)
        out = {}
        for src_ip, dq in self._attempts[key].items():
            out[src_ip] = {
                'average_rate': self._rate(dq, now, rate_interval),
                'burst_rate': self._rate(dq, now, burst_interval),
                'total_attempts': len(dq),
            }
        return out

    def get_shun_list(self, key, cfg):
        now = time.time()
        duration = cfg.get('shun_duration', DEFAULT_SHUN_DURATION)
        return [
            {'ip': ip, 'remaining': max(0, int(duration - (now - t)))}
            for ip, t in list(self._shunned[key].items())
            if now - t <= duration
        ]

    def clear_shun(self, key, ip=None):
        if ip is None:
            self._shunned[key].clear()
        else:
            self._shunned[key].pop(ip, None)


threat_detection_engine = ThreatDetectionEngine()
