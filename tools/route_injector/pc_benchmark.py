#!/usr/bin/env python3
"""
このPCがトラフィック生成にどれだけ使えるかを測るベンチマーク。

「指定したレートが出ない」ときに、それが試験対象の装置の限界なのか、
トラフィックを流している側(このPC)の限界なのかが分からないと、原因調査は
必ず迷走する。先にこのPCの上限を測っておけば、その切り分けができる。

GUI(network_route_injector.py の「トラフィック生成/測定」タブ)が実際に使う
コードをそのまま呼んでいるので、ここで出た値がそのままGUIでの実力になる。

使い方:
    python pc_benchmark.py                  # 既定(1472B, 各2秒)
    python pc_benchmark.py --size 64        # 小さいパケットでのpps上限
    python pc_benchmark.py --seconds 5      # 1条件あたりの測定時間
    python pc_benchmark.py --max-proc 8     # 試す最大プロセス数
"""

import argparse
import multiprocessing
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_tkinter_if_missing():
    """network_route_injector はGUIツールなのでtkinterをimportするが、
    ここで使うのは送受信のコアだけ。GUIの無いサーバー等（トラフィックの
    受け側に使うLinux機など）でも測れるよう、tkinterが無ければ
    ダミーを入れて読み込めるようにする。"""
    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass

    class _Dummy(types.ModuleType):
        def __getattr__(self, name):
            return type(name, (object,), {})

    for modname in ('tkinter.ttk', 'tkinter.scrolledtext', 'tkinter.messagebox',
                    'tkinter.font', 'tkinter.filedialog', 'tkinter'):
        mod = _Dummy(modname)
        if modname == 'tkinter':
            mod.ttk = sys.modules.get('tkinter.ttk')
            mod.messagebox = sys.modules.get('tkinter.messagebox')
            mod.scrolledtext = sys.modules.get('tkinter.scrolledtext')
            mod.filedialog = sys.modules.get('tkinter.filedialog')
        sys.modules[modname] = mod


_stub_tkinter_if_missing()

from network_route_injector import (  # noqa: E402
    benchmark_process_scaling,
    describe_system,
    format_rate,
)

TEN_G = 10_000_000_000
ONE_G = 1_000_000_000


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--size', type=int, default=1472,
                    help='UDPペイロードのバイト数(既定:1472=Ethernetフレーム1500B相当)')
    ap.add_argument('--seconds', type=float, default=2.0,
                    help='1条件あたりの測定時間(既定:2秒)')
    ap.add_argument('--max-proc', type=int, default=None,
                    help='試す最大プロセス数(既定:コア数の2倍、最大16)')
    args = ap.parse_args()

    print(describe_system())
    print(f"UDPペイロード {args.size}B / 1条件あたり {args.seconds}秒\n")
    print("測定中... (プロセス数を倍々に増やしながら測ります)\n")

    results = benchmark_process_scaling(
        size=args.size, seconds=args.seconds, max_proc=args.max_proc)

    print(f"{'プロセス数':>10}{'pps':>14}{'スループット':>16}{'1プロセス比':>12}")
    print("-" * 54)
    base = results[0][1] if results else 0
    best_pps = best_bps = 0
    for nproc, pps, bps in results:
        ratio = (pps / base) if base else 0
        bps_str = format_rate(pps, bps).split(' / ')[1]
        print(f"{nproc:>10}{pps:>13,.0f}{bps_str:>16}{ratio:>11.1f}x")
        if pps > best_pps:
            best_pps, best_bps = pps, bps

    frame_bits = (args.size + 28) * 8  # +IP20 +UDP8
    print()
    print(f"このPCの送信上限: {best_pps:,.0f} pps / {format_rate(best_pps, best_bps).split(' / ')[1]}")
    print(f"  1Gbps相当  ({ONE_G / frame_bits:>10,.0f} pps 必要): "
          f"{'到達可能' if best_pps >= ONE_G / frame_bits else '到達不可'}")
    print(f"  10Gbps相当 ({TEN_G / frame_bits:>10,.0f} pps 必要): "
          f"{'到達可能' if best_pps >= TEN_G / frame_bits else '到達不可'}")
    print()
    print("注意:")
    print("  - loopback宛の送信性能です。実際のNIC経由ではドライバ処理の分だけ下がります")
    print("  - 送信のみの値です。同じPCで受信もすると大きく落ちます")
    print("    (実機試験では送信側と受信側を別PCに分けてください)")
    print("  - NICのリンク速度が上限を決めることもあります(1GbEなら1Gbpsまで)")


if __name__ == '__main__':
    # PyInstallerでexe化した場合に子プロセスが再帰起動しないようにする
    multiprocessing.freeze_support()
    main()
