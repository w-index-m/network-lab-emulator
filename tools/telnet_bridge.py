#!/usr/bin/env python3
"""
Telnetブリッジ — pyATS/unicon/Genieなどの「本物の」自動化ツールから、
このエミュレーターへtelnet接続できるようにする橋渡しサーバー。

エミュレーター本体はHTTP API(/api/cli)のみで、telnet/sshサーバーを
持っていない。unicon（pyATSの接続エンジン）は実機同様のtelnet/ssh
セッションを前提にするため、telnetセッション⇔HTTP API呼び出しを
中継するこのブリッジを介することで、pyATSのtestbed.yaml + connect()
+ execute()/parse() という標準的なワークフローをそのまま使えるように
する。

プロンプトはCisco IOS慣習（hostname#、hostname(config)#、
hostname(config-if)#...）に従って動的に組み立てる。認証は
NETLAB_AUTH_DISABLE前提のためスキップし、telnet接続直後にexecプロンプト
を返す。

使い方:
  python tools/telnet_bridge.py --device catalyst --port 2323 \
      --emulator-url http://localhost:8000

  # 別ターミナルから接続確認
  telnet localhost 2323
"""

import argparse
import asyncio
import sys

import httpx

# 簡易telnetネゴシエーション: クライアントが送ってくるIAC(0xFF)開始の
# オプション要求には全てDONT/WONTで応答し、以降は素のテキストストリーム
# として扱う（unicon/telnetlibはこの程度の応答で十分動作する）
IAC = 0xFF
DONT = 0xFE
WONT = 0xFC


def _prompt_suffix(mode: str) -> str:
    if mode == 'exec':
        return '#'
    if mode == 'config':
        return '(config)#'
    if mode.startswith('config-'):
        return f'({mode})#'
    return '>'


class TelnetSession:
    def __init__(self, reader, writer, device_id, base_url):
        self.reader = reader
        self.writer = writer
        self.device_id = device_id
        self.base_url = base_url.rstrip('/')
        self.hostname = device_id
        self.mode = 'exec'

    async def _cli(self, command: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f'{self.base_url}/api/cli',
                                   json={'device_id': self.device_id, 'command': command})
            r.raise_for_status()
            return r.json()

    def _prompt(self) -> str:
        return f'{self.hostname}{_prompt_suffix(self.mode)} '

    async def _strip_telnet_negotiation(self, data: bytes) -> bytes:
        """受信データからIACシーケンスを除去し、応答が必要なものには
        DONT/WONTを返してオプション要求を全て拒否する。"""
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 2 < len(data):
                opt_cmd, opt_code = data[i + 1], data[i + 2]
                if opt_cmd in (0xFB, 0xFD):  # WILL/DO -> 拒否で返す
                    reply_cmd = DONT if opt_cmd == 0xFD else WONT
                    self.writer.write(bytes([IAC, reply_cmd, opt_code]))
                i += 3
            else:
                out.append(b)
                i += 1
        return bytes(out)

    async def run(self):
        peer = self.writer.get_extra_info('peername')
        print(f'[telnet_bridge] Connection from {peer}')

        # 初期状態を取得（保存済み装置ならmode/hostnameが既にexec以外の
        # こともある）
        try:
            init = await self._cli('')
            self.hostname = init.get('hostname', self.device_id)
            self.mode = init.get('mode', 'exec')
        except Exception:
            pass

        banner = (f'\r\n{self.hostname} network-lab-emulator (telnet_bridge)\r\n\r\n')
        self.writer.write(banner.encode())
        self.writer.write(self._prompt().encode())
        await self.writer.drain()

        buf = b''
        while True:
            try:
                data = await self.reader.read(4096)
            except (ConnectionResetError, asyncio.IncompleteReadError):
                break
            if not data:
                break
            data = await self._strip_telnet_negotiation(data)
            await self.writer.drain()
            if not data:
                continue
            buf += data
            while b'\n' in buf or b'\r' in buf:
                for sep in (b'\r\n', b'\n', b'\r'):
                    if sep in buf:
                        line, buf = buf.split(sep, 1)
                        break
                else:
                    break
                command = line.decode('utf-8', errors='replace').strip()
                if not command:
                    self.writer.write(self._prompt().encode())
                    await self.writer.drain()
                    continue
                if command.lower() in ('exit', 'quit', 'logout') and self.mode == 'exec':
                    self.writer.write(b'\r\n')
                    await self.writer.drain()
                    self.writer.close()
                    return
                try:
                    result = await self._cli(command)
                except Exception as e:
                    self.writer.write(f'\r\n% Bridge error: {e}\r\n'.encode())
                    await self.writer.drain()
                    continue
                output = result.get('output', '')
                self.hostname = result.get('hostname', self.hostname)
                self.mode = result.get('mode', self.mode)
                out_text = output.replace('\n', '\r\n')
                self.writer.write(f'\r\n{out_text}\r\n{self._prompt()}'.encode())
                await self.writer.drain()

        print(f'[telnet_bridge] Connection closed from {peer}')
        self.writer.close()


async def main_async(args):
    async def handle(reader, writer):
        session = TelnetSession(reader, writer, args.device, args.emulator_url)
        await session.run()

    server = await asyncio.start_server(handle, args.bind, args.port)
    addr = server.sockets[0].getsockname()
    print(f'[telnet_bridge] device={args.device} listening on {addr[0]}:{addr[1]} '
          f'-> {args.emulator_url}')
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='pyATS/unicon用 telnetブリッジ')
    parser.add_argument('--device', required=True, help='エミュレーター側のdevice_id')
    parser.add_argument('--port', type=int, default=2323)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--emulator-url', default='http://localhost:8000')
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
