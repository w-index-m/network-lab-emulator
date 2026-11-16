"""
/cyber-view のデモ用シナリオ

4台の Cisco ルータを四角形(R1-R2-R3-R4-R1)につないで OSPF を張り、
90秒周期で「通常の通信 → リンク障害(迂回) → 復旧 → フラップ → 経路の追加/削除」を自動で繰り返す。
デモ用の装置は demo-r1〜demo-r4、アドレスは 10.200.x.0/24。

app.py から cli / add_device / add_link を渡して使う(循環 import を避けるため)。
"""
import asyncio
import math
import time

IDS = ['demo-r1', 'demo-r2', 'demo-r3', 'demo-r4']
IF = ['GigabitEthernet0/0/0', 'GigabitEthernet0/0/1', 'GigabitEthernet0/0/2']
# (a, a の IF, b, b の IF, 第3オクテット)
RING = [(0, 0, 1, 1, 12), (1, 0, 2, 1, 23), (2, 0, 3, 1, 34), (3, 0, 0, 1, 41)]
LAN = {0: 101, 2: 103}             # R1 と R3 の LAN(Gi0/0/2)
PERIOD = 90

state = {'running': False, 'step': '', 'started': None, 'task': None, 'cycle': 0}

# (周期内の秒, 説明, 動作)
TIMELINE = [
    (0, '通常運用: R1 ⇄ R3 の LAN 間で通信', 'traffic'),
    (20, 'リンク障害: R3 ⇄ R4 を shutdown(R2 経由に迂回)', 'down'),
    (36, '復旧: R3 ⇄ R4 を no shutdown', 'up'),
    (50, 'フラップ: R1 ⇄ R2 が up/down を繰り返す', 'flap'),
    (64, '経路追加: R1 にスタティック 10.201.0.0/16', 'route_add'),
    (78, '経路削除: スタティックを削除', 'route_del'),
]


async def build(cli, add_device, add_link):
    for i, d in enumerate(IDS):
        await add_device({'id': d, 'type': 'cisco', 'hostname': f'DEMO-R{i + 1}'})
    for a, ia, b, ib, o in RING:
        await add_link({'a': IDS[a], 'b': IDS[b], 'iface_a': IF[ia], 'iface_b': IF[ib]})
        await cli(IDS[a], 'configure terminal', f'interface {IF[ia]}', f'ip address 10.200.{o}.1 255.255.255.0',
                  'no shutdown', 'end')
        await cli(IDS[b], 'configure terminal', f'interface {IF[ib]}', f'ip address 10.200.{o}.2 255.255.255.0',
                  'no shutdown', 'end')
    for i, o in LAN.items():
        await cli(IDS[i], 'configure terminal', f'interface {IF[2]}', f'ip address 10.200.{o}.1 255.255.255.0',
                  'no shutdown', 'end')
    for d in IDS:
        await cli(d, 'configure terminal', 'router ospf 1', 'network 10.200.0.0 0.0.255.255 area 0', 'end')


async def _act(cli, act):
    # 通常時の R1→R3 は R4 経由(同コストで R4 側が選ばれる)なので、その R3-R4 を落として R2 経由に迂回させる
    if act == 'down':
        await cli(IDS[3], 'configure terminal', f'interface {IF[1]}', 'shutdown', 'end')
    elif act == 'up':
        await cli(IDS[3], 'configure terminal', f'interface {IF[1]}', 'no shutdown', 'end')
    elif act == 'flap':
        for _ in range(2):
            await cli(IDS[0], 'configure terminal', f'interface {IF[0]}', 'shutdown', 'end')
            await asyncio.sleep(1.5)
            await cli(IDS[0], 'configure terminal', f'interface {IF[0]}', 'no shutdown', 'end')
            await asyncio.sleep(1.5)
    elif act == 'route_add':
        await cli(IDS[0], 'configure terminal', 'ip route 10.201.0.0 255.255.0.0 10.200.12.2', 'end')
    elif act == 'route_del':
        await cli(IDS[0], 'configure terminal', 'no ip route 10.201.0.0 255.255.0.0 10.200.12.2', 'end')


async def loop(cli, add_device, add_link):
    state.update(running=True, started=time.time(), step='デモ用ラボを構築中', cycle=0)
    try:
        await build(cli, add_device, add_link)
        await asyncio.sleep(6)
        while state['running']:
            state['cycle'] += 1
            t0 = time.time()
            done = set()
            while state['running'] and time.time() - t0 < PERIOD:
                el = time.time() - t0
                for at, desc, act in TIMELINE:
                    if el >= at and at not in done:
                        done.add(at)
                        state['step'] = desc
                        if act != 'traffic':
                            await _act(cli, act)
                # 流量に強弱を付けた通信(R1→R3 LAN は大きめ、R4→R2 は小さめ)
                big = 400 + int(600 * (0.5 + 0.5 * math.sin(el / 9)))
                await cli(IDS[0], f'ping 10.200.103.1 repeat {big} size 1400')
                await cli(IDS[3], 'ping 10.200.12.2 repeat 40 size 200')
                await asyncio.sleep(4)
    finally:
        state.update(running=False, step='停止')


def start(cli, add_device, add_link):
    if state['running']:
        return False
    state['task'] = asyncio.get_event_loop().create_task(loop(cli, add_device, add_link))
    return True


def stop():
    state['running'] = False
    t = state.get('task')
    if t and not t.done():
        t.cancel()
    state['step'] = '停止'
    return True


def status():
    return {'running': state['running'], 'step': state['step'], 'cycle': state['cycle'],
            'elapsed': round(time.time() - state['started'], 1) if state['started'] and state['running'] else 0,
            'period': PERIOD, 'devices': IDS}
