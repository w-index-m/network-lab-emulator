"""
装置(pc)を削除したら、その装置で動かしていた `python3 -m http.server` も止まること。

以前は装置を消しても http.server が残り、撤去したはずのサーバーが BIG-IP/ALB の
ヘルスチェックに応答し続けた(CI の BIG-IP シナリオ「サーバ撤去でモニター自動down」で発覚)。
"""
import os
import socket
import uuid

from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402

client = TestClient(app_module.app)


def _open(ip, port):
    try:
        socket.create_connection((ip, port), timeout=1).close()
        return True
    except OSError:
        return False


def test_deleting_pc_stops_its_http_server():
    dev = 'delhttp-' + uuid.uuid4().hex[:6]
    ip, port = '10.252.7.10', 18081
    client.post('/api/device', json={'id': dev, 'type': 'pc', 'hostname': dev, 'ip': ip, 'prefix': 24})
    client.post('/api/cli', json={'device_id': dev, 'command': f'python3 -m http.server {port}'})
    assert _open(ip, port), 'http.server が起動していない(lo への IP 追加に root が必要)'
    assert client.delete(f'/api/device/{dev}').json()['ok']
    assert not _open(ip, port)
