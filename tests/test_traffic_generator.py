"""
route_injector の「トラフィック生成/測定」タブ（Hachi相当のL4スループット
試験機能）のコア部分のテスト。

GUI(tkinter)には依存しない形で run_traffic_sender / run_traffic_receiver /
TrafficStats を実装してあるので、実際にlocalhostへ送受信して
カウンタが合うことを確認する。

network_route_injector.py 自体はtkinterをimportするため、
engine/real_ospf_agent.py と同じ要領でダミーモジュールを注入して
ヘッドレス環境でも読み込めるようにしている。
"""

import os
import socket
import sys
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools', 'route_injector'))


class _DummyModule(types.ModuleType):
    def __getattr__(self, name):
        return type(name, (object,), {})


def _stub_tkinter():
    if 'tkinter' in sys.modules and not isinstance(sys.modules['tkinter'], _DummyModule):
        return
    for modname in ('tkinter.ttk', 'tkinter.scrolledtext', 'tkinter.messagebox',
                    'tkinter.font', 'tkinter.filedialog', 'tkinter'):
        m = _DummyModule(modname)
        if modname == 'tkinter':
            m.ttk = sys.modules.get('tkinter.ttk')
            m.messagebox = sys.modules.get('tkinter.messagebox')
            m.scrolledtext = sys.modules.get('tkinter.scrolledtext')
            m.filedialog = sys.modules.get('tkinter.filedialog')
        sys.modules[modname] = m


_stub_tkinter()

from network_route_injector import (  # noqa: E402
    TrafficStats,
    format_rate,
    parse_ip_field,
    run_traffic_receiver,
    run_traffic_sender,
)


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_udp_send_and_receive_counters_match():
    """送信した分だけ受信側でカウントされる（localhostなので損失しない）"""
    port = _free_udp_port()
    recv_stats = TrafficStats()
    send_stats = TrafficStats()
    recv_stop = threading.Event()
    send_stop = threading.Event()

    receiver = threading.Thread(
        target=run_traffic_receiver,
        args=('udp', '127.0.0.1', port, recv_stop, recv_stats),
        daemon=True,
    )
    receiver.start()
    time.sleep(0.3)  # bind完了を待つ

    run_traffic_sender('udp', '127.0.0.1', port, size=100, count=50, rate_pps=0,
                       src_ip='', src_port=0, tos=0,
                       stop_event=send_stop, stats=send_stats)

    time.sleep(0.5)  # 受信側が処理しきるのを待つ
    recv_stop.set()
    receiver.join(timeout=3)

    sent_packets, sent_bytes, sent_errors, _, _, _ = send_stats.snapshot()
    recv_packets, recv_bytes, _, _, _, _ = recv_stats.snapshot()

    assert sent_packets == 50
    assert sent_bytes == 50 * 100
    assert sent_errors == 0
    assert recv_packets == 50, f"受信パケット数が一致しない: {recv_packets}"
    assert recv_bytes == 50 * 100


def test_tcp_send_and_receive_counters_match():
    """TCPでも送信バイト数と受信バイト数が一致する
    （TCPはストリームなのでパケット数ではなくバイト数で突き合わせる）"""
    port = _free_udp_port()
    recv_stats = TrafficStats()
    send_stats = TrafficStats()
    recv_stop = threading.Event()
    send_stop = threading.Event()

    receiver = threading.Thread(
        target=run_traffic_receiver,
        args=('tcp', '127.0.0.1', port, recv_stop, recv_stats),
        daemon=True,
    )
    receiver.start()
    time.sleep(0.3)

    run_traffic_sender('tcp', '127.0.0.1', port, size=200, count=25, rate_pps=0,
                       src_ip='', src_port=0, tos=0,
                       stop_event=send_stop, stats=send_stats)

    time.sleep(0.5)
    recv_stop.set()
    receiver.join(timeout=3)

    _, sent_bytes, sent_errors, _, _, _ = send_stats.snapshot()
    _, recv_bytes, _, _, _, _ = recv_stats.snapshot()

    assert sent_bytes == 25 * 200
    assert sent_errors == 0
    assert recv_bytes == 25 * 200, f"受信バイト数が一致しない: {recv_bytes}"


def test_rate_limit_is_roughly_honoured():
    """レート指定(pps)がおおよそ守られる。
    厳密な精度は環境依存なので「明らかに速すぎない」ことだけ見る。"""
    port = _free_udp_port()
    stats = TrafficStats()
    stop = threading.Event()

    start = time.time()
    run_traffic_sender('udp', '127.0.0.1', port, size=64, count=20, rate_pps=50,
                       src_ip='', src_port=0, tos=0, stop_event=stop, stats=stats)
    elapsed = time.time() - start

    # 50ppsで20パケット = 理論値0.4秒。無制限なら一瞬で終わるので、
    # レート制限が効いていれば最低でも0.2秒はかかる
    assert elapsed > 0.2, f"レート制限が効いていない可能性: {elapsed:.3f}秒"
    packets, _, _, _, _, _ = stats.snapshot()
    assert packets == 20


def test_sender_stops_on_stop_event():
    """無制限送信(count=0)でもstop_eventで止まる"""
    port = _free_udp_port()
    stats = TrafficStats()
    stop = threading.Event()

    thread = threading.Thread(
        target=run_traffic_sender,
        args=('udp', '127.0.0.1', port, 64, 0, 200, '', 0, 0, stop, stats),
        daemon=True,
    )
    thread.start()
    time.sleep(0.4)
    stop.set()
    thread.join(timeout=3)

    assert not thread.is_alive(), "stop_eventで送信スレッドが停止しない"
    packets, _, _, _, _, _ = stats.snapshot()
    assert packets > 0


def test_format_rate_units():
    assert 'Mbps' in format_rate(1000, 5_000_000)
    assert 'kbps' in format_rate(10, 8_000)
    assert 'Gbps' in format_rate(1_000_000, 2_500_000_000)


def test_parse_ip_field_reports_the_field_name_and_reason():
    try:
        parse_ip_field('宛先IP', '192168.1.100')
    except ValueError as e:
        assert '宛先IP' in str(e)
        assert 'ドット区切り' in str(e)
    else:
        raise AssertionError('不正なIPでValueErrorが送出されなかった')

    try:
        parse_ip_field('宛先IP', '')
    except ValueError as e:
        assert '入力されていません' in str(e)
    else:
        raise AssertionError('空欄でValueErrorが送出されなかった')
