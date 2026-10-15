"""pc装置向けの最小限の実HTTPサーバ(`python3 -m http.server <port>`)。

ALB/BIG-IPの実ヘルスチェック・実L7ルーティングを本物のHTTP往復で
デモできるようにするための追加——このエミュレータの装置は元々実HTTP
サーバを持っていなかった(SSH/SNMP/NETCONF/BGP等の実リスナーのみ)ため、
pc(汎用Linuxエンドポイント)に実機通りのコマンドで本物の
`http.server`を立てられるようにした。新しいプロトコルを発明した
わけではなく、Python標準ライブラリの`http.server`をそのまま使い、
装置のホスト名を返すだけの最小限のGETハンドラを持たせる(ロード
バランサのデモで「どのバックエンドが応答したか」が一目でわかる
ようにするため)。
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_servers = {}  # (ip, port) -> HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(f'{self.server.netlab_hostname}\n'.encode())

    def log_message(self, fmt, *args):
        pass  # 標準出力を汚さない(実機のhttp.serverはアクセスログを吐くが、ここでは不要)


def ensure_mock_http_server(ip: str, port: int, hostname: str) -> bool:
    key = (ip, port)
    if key in _servers:
        return True
    from engine.loopback_alias import ensure_loopback_alias
    ensure_loopback_alias(ip)
    try:
        srv = HTTPServer((ip, port), _Handler)
    except OSError:
        return False
    srv.netlab_hostname = hostname
    _servers[key] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def stop_mock_http_server(ip: str, port: int) -> bool:
    key = (ip, port)
    srv = _servers.pop(key, None)
    if srv is None:
        return False
    srv.shutdown()
    return True
