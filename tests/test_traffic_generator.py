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

import pytest

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
    MultiProcessTraffic,
    benchmark_process_scaling,
    benchmark_send_capacity,
    calc_rate_and_count,
    frame_bits,
    describe_system,
    get_nic_link_speeds,
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


def test_rate_limit_is_accurate():
    """指定pps付近の実測レートが出る。

    1パケットごとにsleepで間隔を刻む実装だと、OSのタイマー粒度で
    毎回寝過ごして指定レートに届かない（1000pps指定で実測667ppsだった）。
    経過時間基準で送信数を決める実装に直したので、ここで精度を担保する。
    """
    port = _free_udp_port()
    stats = TrafficStats()
    stop = threading.Event()

    start = time.time()
    run_traffic_sender('udp', '127.0.0.1', port, size=64, count=500, rate_pps=500,
                       src_ip='', src_port=0, tos=0, stop_event=stop, stats=stats)
    wall = time.time() - start

    packets, _, _, elapsed, pps, _ = stats.snapshot()
    assert packets == 500
    # 500ppsで500パケット = 理論値1.0秒
    assert 0.8 < wall < 1.6, f"実時間が理論値1.0秒から外れすぎ: {wall:.3f}秒"
    assert 400 < pps < 650, f"実測レートが指定500ppsから外れすぎ: {pps:.0f}pps"
    assert 0.7 < elapsed < 1.5, f"計測区間がおかしい: {elapsed:.3f}秒"


def test_receiver_rate_is_not_diluted_by_idle_listening_time():
    """受信側を先に起動して待っていても、その待ち時間で平均レートが
    薄まらない（計測区間は最初のパケット〜最後のパケット）。

    待ち受け開始から数える実装では、1,000ppsのトラフィックが
    606ppsと表示されてしまっていた。
    """
    port = _free_udp_port()
    recv_stats, send_stats = TrafficStats(), TrafficStats()
    recv_stop, send_stop = threading.Event(), threading.Event()

    receiver = threading.Thread(
        target=run_traffic_receiver,
        args=('udp', '127.0.0.1', port, recv_stop, recv_stats), daemon=True)
    receiver.start()
    time.sleep(1.0)  # 送信を始める前にわざと長めに待ち受けさせる

    run_traffic_sender('udp', '127.0.0.1', port, size=64, count=400, rate_pps=400,
                       src_ip='', src_port=0, tos=0,
                       stop_event=send_stop, stats=send_stats)
    time.sleep(0.5)
    recv_stop.set()
    receiver.join(timeout=3)

    recv_packets, _, _, _, recv_pps, _ = recv_stats.snapshot()
    assert recv_packets == 400
    # 1秒待ってから受信しているので、待ち受け開始起点だと約200ppsまで薄まる
    assert recv_pps > 300, f"待ち時間で平均が薄まっている: {recv_pps:.0f}pps"


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


def test_multiprocess_send_and_receive_all_packets_arrive():
    """マルチプロセス送受信で、全プロセス合算のパケット数が一致する。

    1プロセス(1スレッド)ではsendto()のシステムコールが処理時間の約77%を
    占め、CPUのコア数に関係なく約24万pps(1472Bで約2.8Gbps)で頭打ちになる。
    スレッドを増やすとGILの奪い合いで逆に遅くなるため、プロセス分割で
    スケールさせている（実測で4プロセス3.8倍）。
    """
    base_port = _free_udp_port()
    nproc = 2
    per_proc = 500

    rx = MultiProcessTraffic()
    rx.start_receivers(nproc, 'udp', '127.0.0.1', base_port)
    time.sleep(1.0)  # 各プロセスのbind完了を待つ

    tx = MultiProcessTraffic()
    tx.start_senders(nproc, 'udp', '127.0.0.1', base_port, size=200,
                     count=per_proc * nproc, rate_pps=0, src_ip='', tos=0)
    deadline = time.time() + 30
    while tx.is_running() and time.time() < deadline:
        time.sleep(0.1)
    tx.stop()
    time.sleep(1.0)
    rx.stop()

    sent_packets, sent_bytes, sent_errors, _, _, _ = tx.snapshot()
    recv_packets, recv_bytes, _, _, _, _ = rx.snapshot()

    assert sent_packets == per_proc * nproc, f"送信数が合わない: {sent_packets}"
    assert sent_bytes == per_proc * nproc * 200
    assert sent_errors == 0
    assert recv_packets == sent_packets, (
        f"受信数が送信数と一致しない: 送信{sent_packets} 受信{recv_packets}")
    assert recv_bytes == sent_bytes


def test_multiprocess_splits_the_requested_count_across_processes():
    """送信数はプロセス間で等分され、合計が指定値ちょうどになる
    （端数があっても失われない）"""
    base_port = _free_udp_port()
    nproc = 3
    total = 100  # 3で割り切れない

    tx = MultiProcessTraffic()
    tx.start_senders(nproc, 'udp', '127.0.0.1', base_port, size=64,
                     count=total, rate_pps=0, src_ip='', tos=0)
    deadline = time.time() + 30
    while tx.is_running() and time.time() < deadline:
        time.sleep(0.1)
    tx.stop()

    sent_packets, _, _, _, _, _ = tx.snapshot()
    assert sent_packets == total, f"合計が指定値と違う: {sent_packets} != {total}"


def test_benchmark_send_capacity_reports_a_real_rate():
    """このPCの送信上限を測る機能が、実際に測れていること。

    「指定レートが出ない」ときに装置側の限界かPC側の限界かを
    切り分けるための機能なので、値が取れないと意味がない。
    """
    packets, nbytes, _errors, elapsed, pps, bps = benchmark_send_capacity(
        nproc=1, size=512, seconds=1.0, base_port=_free_udp_port())
    assert packets > 0, "1秒間で1パケットも送れていない"
    assert nbytes == packets * 512
    assert 0 < elapsed < 5
    assert pps > 0 and bps > 0
    assert abs(bps - pps * 512 * 8) < pps * 512  # bpsとppsの整合


def test_benchmark_process_scaling_returns_one_row_per_process_count():
    """プロセス数を倍々にしながら測り、各条件の結果が返る"""
    results = benchmark_process_scaling(size=512, seconds=0.5, max_proc=2)
    assert [r[0] for r in results] == [1, 2]
    for nproc, pps, bps in results:
        assert pps > 0, f"{nproc}プロセスでレートが0"
        assert bps > 0


def test_describe_system_mentions_core_count():
    desc = describe_system()
    assert 'コア' in desc
    assert str(os.cpu_count()) in desc


def test_get_nic_link_speeds_returns_a_dict_without_raising():
    """NIC速度は取れない環境（コンテナ等）もあるので、
    取れなくても例外にせず空で返ること"""
    speeds = get_nic_link_speeds()
    assert isinstance(speeds, dict)


class TestRateCalculation:
    """目標スループット(Mbps)からレート(pps)と送信パケット数を逆算する。"""

    def test_l2_basis_matches_real_sir_measurement(self):
        """実機Si-R G110Bで確認した値と一致すること。

        ペイロード1400B/UDPを996ppsで送ったとき、
        show ether statistics の bits/sec は 11,532,808 だった。
        L2基準(ペイロード+46B)で計算するとこれに一致する。
        """
        assert frame_bits(1400, 'udp', 'l2') == (1400 + 46) * 8
        # 装置のbits/secは移動平均なので1pps程度の誤差は許容する
        pps, _, _ = calc_rate_and_count(1400, 11.532808, 1.0, 'udp', 'l2')
        assert abs(pps - 996) <= 1

    def test_l1_basis_adds_preamble_and_ifg(self):
        assert frame_bits(1400, 'udp', 'l1') == (1400 + 46 + 20) * 8

    def test_tcp_header_is_larger_than_udp(self):
        assert frame_bits(1400, 'tcp', 'l2') > frame_bits(1400, 'udp', 'l2')

    def test_count_is_rate_times_seconds(self):
        pps, count, _ = calc_rate_and_count(1472, 100.0, 10.0)
        assert count == pps * 10
        # 1472Bで100Mbpsは概ね8100pps前後になる
        assert 8000 <= pps <= 8200

    def test_rejects_invalid_input(self):
        for args in ((0, 100.0, 10.0), (1472, 0, 10.0), (1472, 100.0, 0)):
            with pytest.raises(ValueError):
                calc_rate_and_count(*args)

    def test_very_low_rate_still_sends_at_least_one_pps(self):
        pps, count, _ = calc_rate_and_count(1472, 0.001, 5.0)
        assert pps == 1
        assert count == 5
