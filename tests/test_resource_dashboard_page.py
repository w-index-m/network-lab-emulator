"""
/dashboard （リソース監視ダッシュボードページ）の最小対応。

ユーザーからの「稼働してるモックのリソース感じをかっこいい画面で見たい」
という依頼を受け、既存の /api/snmp/dashboard（CPU%/インタフェース/
トラフィック履歴をSNMP経由で集約するAPI。元々フロントエンド無し）を
ポーリングするだけの新規ページ static/dashboard.html を追加した。
新しい計測の仕組みは足していない（既存データのビジュアライズのみ）。

ログイン画面を経由せずにページ自体は開けるよう _NO_AUTH_PATHS に
"/dashboard" を追加した点だけ固定する（ページ内のfetchは通常通り
X-Session-Tokenで認証される。index.htmlと同じlocalStorageキー
"netlabToken_v1"をdashboard.html側でも読む）。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def test_dashboard_page_served():
    r = client.get('/dashboard')
    assert r.status_code == 200
    assert 'NETWORK-LAB RESOURCE MONITOR' in r.text
    assert '/api/snmp/dashboard' in r.text


def test_dashboard_path_is_auth_exempt():
    assert '/dashboard' in app_module._NO_AUTH_PATHS


def test_dashboard_reads_same_session_token_key_as_main_ui():
    """index.html側のSESSION_KEYと食い違うとログイン済みでも401になるため固定する。"""
    index_html = (app_module.static_dir / 'index.html').read_text(encoding='utf-8')
    dash_html = (app_module.static_dir / 'dashboard.html').read_text(encoding='utf-8')
    import re
    m = re.search(r"SESSION_KEY\s*=\s*'([^']+)'", index_html)
    assert m, 'index.htmlにSESSION_KEY定義が見つからない'
    assert f"SESSION_KEY = '{m.group(1)}'" in dash_html


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


class TestGnmiOnDashboard:
    """ユーザーからの「gNMIも(ダッシュボードに)含めてほしい」という依頼への対応。

    gNMIは実gRPCコールせず、同じstate.interfacesを
    engine.gnmi_agent.active_gnmi_device_ids()/gnmi_port_for()経由で
    読んで『gNMIが実際に見ている値』を/api/snmp/dashboardに併記するだけ
    (gNMIはCPU/トラフィックカウンタを公開していないため、SNMP側の
    cpu_percent等とは別枠のgnmiキー)。"""

    def test_device_without_gnxi_server_has_no_gnmi_field(self):
        _dev('dash-gnmi-off')
        r = client.get('/api/snmp/dashboard').json()
        dev = next(d for d in r['devices'] if d['device_id'] == 'dash-gnmi-off')
        assert dev['gnmi'] is None

    def test_device_with_gnxi_server_exposes_gnmi_field(self):
        _dev('dash-gnmi-on')
        _cli('dash-gnmi-on', 'configure terminal')
        _cli('dash-gnmi-on', 'gnxi')
        _cli('dash-gnmi-on', 'gnxi server')
        r = client.get('/api/snmp/dashboard').json()
        dev = next(d for d in r['devices'] if d['device_id'] == 'dash-gnmi-on')
        assert dev['gnmi'] is not None
        assert dev['gnmi']['port'] == 50052
        assert dev['gnmi']['interfaces_total'] > 0

    def test_dashboard_page_renders_gnmi_badge_markup(self):
        dash_html = (app_module.static_dir / 'dashboard.html').read_text(encoding='utf-8')
        assert 'gnmi-badge' in dash_html
        assert 'd.gnmi' in dash_html
