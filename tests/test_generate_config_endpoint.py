"""
「設定生成」タブの不具合修正(ユーザー指摘「ospf設定有効としているのに
ospfが有効になっていない」)。

元々の`generateConfig()`(static/index.html)は、フォームの入力値
(ホスト名/IP/VRRP/「その他」自由記述)を一切バックエンドに送らず、
選択した装置に対して単に`show running-config`を実行しているだけ
だった——つまり「現在の設定を表示するだけ」の機能で、フォームは
見た目だけの未接続UIだった。

本来ユーザーが意図していた挙動(方向A、ユーザー確認済み「Aがやりたい
ことなので」)に合わせ、`/api/generate_config`を新設し、フォーム値
から実際にそのベンダーのCLIコマンド列を組み立てて装置へ投入してから
`show running-config`を返すようにした(`app.py`の
`_generate_config_commands()`/`generate_config()`)。

「その他」自由記述欄だけはルールベースでは解釈できないため、LLM
(Groq優先/Ollama)に「このベンダーの実コマンドのみを1行1コマンドで
出力させる」形で変換させる。LLM_BACKENDが"rules"(未検出)の場合は
その部分だけスキップし、notesにその旨を返す(フェイクの成功を
作らない)。

**実装中にライブ検証で見つけた既存の実バグ**: Si-R(`sir`/`srs`)の
`hostname`コマンドは`engine/rules.py`の共有Cisco系ツリー内で
`state.mode == 'config'`を要求する一方、Si-Rの`lan/wan ip address`
系コマンドは`app.py`の`handle_protocol_config`層が現在モードに
関係なく反映する——両者が別々の層で別々のモード要件を持つため、
"configure terminal"を挟まずに`hostname`だけ単独で打つと
サイレントに無視される(エラーも出ない)。`_generate_config_commands`
のSi-R分岐で`configure terminal`/`end`を挟むよう修正して解消。

Live-verified: sir/catalyst/cisco の3ベンダーでフォーム値が実際に
`show running-config`へ反映されることを確認(Si-Rの`hostname`が
`configure terminal`無しでは無視される上記バグも、ここで実際に
踏んで発見・修正した)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_, hostname=None):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': hostname or id_})


def _generate(device_id, **kwargs):
    body = {
        'device_id': device_id, 'hostname': '', 'password': '',
        'lan_ip': '', 'wan_ip': '', 'gw': '',
        'vrrp_enabled': False, 'vrid': '', 'vpri': '', 'vip': '',
        'extra_text': '',
    }
    body.update(kwargs)
    return client.post('/api/generate_config', json=body).json()


class TestSirFormValuesActuallyApply:
    def test_hostname_and_lan_wan_ip_are_reflected(self):
        _dev('gc-sir-1', 'sir')
        d = _generate('gc-sir-1', hostname='MyRouter',
                       lan_ip='192.168.5.1/24', wan_ip='203.0.113.9/30',
                       gw='203.0.113.10')
        assert 'hostname MyRouter' in d['output']
        assert 'lan 0 ip address 192.168.5.1/24' in d['output']
        assert 'wan 1 ip address 203.0.113.9/30' in d['output']
        assert 'ip route default gateway 203.0.113.10' in d['output']

    def test_hostname_alone_is_not_silently_ignored(self):
        """regressionガード: configure terminalを挟まずhostnameだけ
        打つと無視される既存バグの固定。"""
        _dev('gc-sir-2', 'sir')
        d = _generate('gc-sir-2', hostname='OnlyHostname')
        assert 'hostname OnlyHostname' in d['output']

    def test_vrrp_reflected_when_enabled(self):
        _dev('gc-sir-3', 'sir')
        d = _generate('gc-sir-3', vrrp_enabled=True, vrid='15',
                       vpri='200', vip='192.168.1.200')
        assert 'lan 0 vrrp group 1 id 15 200 192.168.1.200' in d['output']

    def test_vrrp_not_reflected_when_disabled(self):
        _dev('gc-sir-4', 'sir')
        d = _generate('gc-sir-4', vrrp_enabled=False, vrid='15',
                       vpri='200', vip='192.168.1.200')
        assert 'vrrp' not in d['output']

    def test_no_commands_run_means_no_applied_commands_beyond_mode_wrap(self):
        _dev('gc-sir-5', 'sir')
        d = _generate('gc-sir-5')
        cmds = [a['command'] for a in d['applied_commands']]
        assert cmds == ['configure terminal', 'end']


class TestCatalystFormValues:
    def test_lan_ip_applied_to_vlan10_svi(self):
        d = _generate('catalyst', hostname='Dist-SW-Test', lan_ip='192.168.44.1/24')
        assert 'hostname Dist-SW-Test' in d['output']
        assert 'ip address 192.168.44.1 255.255.255.0' in d['output']

    def test_wan_or_gw_on_catalyst_produces_a_note_and_is_not_applied(self):
        d = _generate('catalyst', wan_ip='203.0.113.1/30', gw='203.0.113.2')
        assert any('WAN' in n or 'GW' in n for n in d['notes'])
        assert '203.0.113.1' not in d['output']


class TestCiscoFormValues:
    def test_lan_wan_gw_password_applied(self):
        d = _generate('cisco', hostname='GW-Test', password='mysecret',
                       lan_ip='10.9.9.1/24', wan_ip='198.51.100.5/30',
                       gw='198.51.100.6')
        assert 'hostname GW-Test' in d['output']
        assert 'enable secret mysecret' in d['output']
        assert 'ip address 10.9.9.1 255.255.255.0' in d['output']
        assert 'ip address 198.51.100.5 255.255.255.252' in d['output']
        assert 'ip route 0.0.0.0 0.0.0.0 198.51.100.6' in d['output']


class TestExtraTextWithoutLlmBackend:
    def test_extra_text_produces_note_when_no_llm_backend(self):
        _dev('gc-extra-1', 'sir')
        old_backend = app_module.LLM_BACKEND
        try:
            app_module.LLM_BACKEND = 'rules'
            d = _generate('gc-extra-1', extra_text='OSPFを有効にして')
            assert any('AI' in n for n in d['notes'])
        finally:
            app_module.LLM_BACKEND = old_backend

    def test_extra_text_commands_applied_when_llm_available(self, monkeypatch):
        _dev('gc-extra-2', 'sir')

        async def fake_query_llm(prompt, system):
            return 'router ospf 1\nnetwork 192.168.1.0 0.0.0.255 area 0'

        monkeypatch.setattr(app_module, 'query_llm', fake_query_llm)
        old_backend = app_module.LLM_BACKEND
        try:
            app_module.LLM_BACKEND = 'groq'
            d = _generate('gc-extra-2', extra_text='OSPFを有効にして')
            cmds = [a['command'] for a in d['applied_commands']]
            assert 'router ospf 1' in cmds
            assert 'network 192.168.1.0 0.0.0.255 area 0' in cmds
            assert not any('AI' in n for n in d['notes'])
        finally:
            app_module.LLM_BACKEND = old_backend
