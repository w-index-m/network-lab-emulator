# pyATS / Genie 連携（telnet_bridge経由）

`tools/telnet_bridge.py`

## これは何か

このエミュレーターはHTTP API(`/api/cli`)のみを持ち、telnet/sshサーバー
機能を持っていない。一方、**本物のpyATS**（Cisco製のネットワーク
自動化・テストフレームワーク）の接続エンジン`unicon`は、実機同様の
telnet/sshセッションを前提とする設計になっている。

そこで`telnet_bridge.py`が「telnetセッション ⇔ HTTP API呼び出し」を
中継し、testbed.yaml + `device.connect()` + `device.execute()` /
`device.parse()`という**pyATSの標準ワークフローをそのまま**この
エミュレーターに対して使えるようにした。

`engine/protocols.py`の`GenieEngine`（pyATS/Genie風の独自実装）とは
別物: そちらは「Genieのような機能を自前実装」したもので、こちらは
**本物のCisco製pyATS/Genieライブラリ**をこのラボに接続する経路。

```
pyATS (testbed.yaml, dev.connect/execute/parse)
    │ telnet (unicon経由)
    ▼
tools/telnet_bridge.py  ← プロンプト管理・telnetネゴシエーション処理
    │ HTTP POST /api/cli
    ▼
app.py (エミュレーター本体)
```

## 使い方

```bash
# 1. エミュレーター本体を起動
NETLAB_AUTH_DISABLE=1 python app.py &

# 2. telnetブリッジを起動（catalyst装置向け、ポート2323）
python tools/telnet_bridge.py --device catalyst --port 2323 &

# 3. pyATS用の別環境にpyats/unicon/genieをインストール
#    (fastapi等との依存衝突を避けるため専用venv推奨)
python3.11 -m venv pyats-venv
source pyats-venv/bin/activate
pip install "setuptools<70" pyats unicon genie
```

```yaml
# testbed.yaml
testbed:
  name: netlab-emulator
devices:
  catalyst:
    os: ios          # iosxeだと接続直後のcontroller-mode判定で
                      # "show version | include ..." を使うため未対応
                      # (下記「制約」参照)。iosで代用。
    type: switch
    connections:
      cli:
        protocol: telnet
        ip: 127.0.0.1
        port: 2323
```

```python
from pyats.topology import loader

tb = loader.load('testbed.yaml')
dev = tb.devices['catalyst']
dev.connect(log_stdout=False, learn_hostname=True)

# 生のCLI実行
print(dev.execute('show version'))

# Genie構造化パース（本物のCisco公式パーサーが動く）
parsed = dev.parse('show ip interface brief')
print(parsed['interface']['GigabitEthernet1/0/1']['ip_address'])
# → '10.9.9.1'

dev.disconnect()
```

## 実際に確認した動作（2026-09-02）

- `dev.connect()` → `Connected: True`
- `dev.execute('show version')` → 実際のCatalyst風`show version`テキストを取得
- `dev.parse('show ip interface brief')` → **Cisco公式Genieパーサー**が
  正しく動作し、構造化JSON（interface名→ip_address/status/protocol）を取得
- `dev.parse('show version')` → version/platform/chassis等のキーを正しく抽出
- `dev.parse('show vlan')` → vlansキーで正しくパース

いずれも本物のGenieパーサーが「エミュレーターの出力を実機の出力と
区別できずにパースできた」ことを意味しており、このラボのCLI出力が
実機フォーマットに十分忠実であることの裏付けにもなっている。

## スタティックルートの設定と確認（2026-09-03 確認）

`show` 系の `execute` / `parse` だけでなく、**config モードでの設定投入
(`dev.configure()`) も動作する**ことを確認した。

```python
from pyats.topology import loader

tb = loader.load('testbed.yaml')
dev = tb.devices['catalyst']
dev.connect(log_stdout=False, learn_hostname=True)

# 設定投入（configure terminal / exit は unicon が自動で行う）
dev.configure([
    'ip route 10.220.1.0 255.255.255.0 10.9.9.2',
    'ip route 10.220.2.0 255.255.255.0 10.9.9.2',
    'ip route 10.220.3.0 255.255.255.0 10.9.9.2 200',   # フローティングスタティック
])

# 構造化パースで検証
parsed = dev.parse('show ip route')
routes = parsed['vrf']['default']['address_family']['ipv4']['routes']
assert routes['10.220.3.0/24']['route_preference'] == 200
assert routes['10.220.3.0/24']['source_protocol'] == 'static'

dev.disconnect()
```

投入前後の `show ip route`:

```
（投入前）
C     192.168.10.0/24 [0/0] via directly, Vlan10
S     0.0.0.0/0 [1/0] via 203.0.113.1, GigabitEthernet1/0/24

（投入後）
S     10.220.1.0/24 [1/0] via 10.9.9.2,
S     10.220.2.0/24 [1/0] via 10.9.9.2,
S     10.220.3.0/24 [200/0] via 10.9.9.2,     ← AD 200 が反映
```

Genieパーサーでの検証結果:

| 経路 | source_protocol | route_preference | next_hop |
|---|---|---|---|
| `10.220.1.0/24` | static | 1 | 10.9.9.2 |
| `10.220.2.0/24` | static | 1 | 10.9.9.2 |
| `10.220.3.0/24` | static | **200** | 10.9.9.2 |

### この検証で見つかった実バグ（修正済み）

**フローティングスタティックのADが失われていた。**
`ip route <dst> <mask> <gw> 200` と指定しても `show ip route` は
`[1/0]` と表示され、通常のスタティックと区別が付かなかった。

原因は2箇所:

1. `engine/rules.py` の `ip route` 正規表現が末尾のADを取り込んで
   おらず、`state.static_routes` にADが残らなかった
2. `app.py` の再同期ループ（**設定コマンドのたびに走る**）が
   `add_static_route(..., ad=1)` とAD固定で呼び直しており、
   ハンドラが正しく登録したAD200のエントリを上書きしていた

エンジン単体（`RibEngine`）は最初から正しくADを扱えており、
`app.py`／`rules.py` 側でADが落ちていた。回帰テストは
`tests/test_protocols.py` の
`test_floating_static_route_preserves_admin_distance` ほか2件。

## つまずいた点（実装中に見つけた実バグ含む）

- **`os: iosxe`だと接続に失敗する**: uniconのiosxe接続確立処理は、
  接続直後に`show version | include operating mode`（コントローラー
  モード判定）を自動送信する。このエミュレーターは`|`パイプフィルタ
  未対応のため`% Invalid input`を返し、これがuniconの内部エラー
  パターンに一致して`ConnectionError`になる。`os: ios`（クラシックIOS
  プラグイン）を使えばこの初期化ステップがなく回避できる
- **`terminal length 0`が未実装だった（実バグ、今回修正）**:
  unicon（`os: ios`でも）は接続確立時に必ず`term length 0`
  （ページング無効化）を送る。このエミュレーターにはこのコマンドの
  ハンドラーが無く`% Invalid input`となり接続が失敗していた。
  `app.py`の`/api/cli`エンドポイント冒頭に、全機種共通で
  `terminal length/width/monitor/editing`系コマンドを受理する
  ハンドラーを追加して解消した（実機でも純粋に表示設定でしかない
  ため、状態には影響させていない）
- **uniconは`telnet`コマンド（バイナリ）を直接spawnする**:
  Python標準ライブラリの`telnetlib`は使っていない。`apt-get install
  telnet`でクライアントバイナリを別途入れる必要がある
- **pyATSのフルインストール(`pyats[full]`)は失敗する**: `tftpy`や
  `f5-icontrol-rest`等のレガシー依存がPython 2時代の`distutils`
  オプション（`install_layout`）を使っており、新しい`setuptools`
  では`AttributeError`でビルド不可。`pyats[library]`または
  `pyats unicon genie`のみを個別インストールし、かつ
  `pip install "setuptools<70"`を先に入れておくことで回避

## テスト

```bash
# メインリポジトリのpython環境ではpyats未インストールのため自動skip
pytest tests/test_pyats_bridge.py -v

# pyats venv側で実行すると実際に接続・parseまで検証される
source /opt/pyats-venv/bin/activate
pip install pytest httpx fastapi "uvicorn[standard]" python-dotenv websockets
pytest tests/test_pyats_bridge.py -v
# 3/3 成功（emulator起動→bridge起動→connect→execute→parseを
#            フィクスチャで完全自動化）
```

## 制約

- telnetのみ対応（ssh未実装。unicon側は`protocol: ssh`も指定できるが、
  ブリッジ側にsshサーバー機能が無いため現状はtelnetのみ）
- プロンプト構築はCisco IOS慣習のみ想定（Nexus/ASA等、他機種の
  プロンプト書式には未対応、必要であれば`_prompt_suffix()`の拡張が必要）
- 認証は常にスキップ（`NETLAB_AUTH_DISABLE=1`前提。credentialsの
  検証は行っていない）
