# このコードベースの構造的な落とし穴

同じ種類のバグを繰り返し踏んでいるため、原因と対処をまとめておく。
**新しい機能を足す前に、該当する項目に目を通すこと。**

---

## 1. CLIディスパッチは2層。app.py が先に勝つ

CLIは上から順に見て、**最初に非Noneを返した層が勝つ**:

| 順 | 場所 | 役割 |
|---|---|---|
| 1 | `app.py` `handle_protocol_show()` | show系 |
| 2 | `app.py` `handle_protocol_config()` | 設定系 |
| 3 | `engine/rules.py` `RuleEngine.process()` | 受け皿（ベンダ別の既定応答・補完・ヘルプ） |

**app.py 側に同じコマンドのハンドラがあると、rules.py 側の実装には
決して到達しない。** rules.py の `_show_ip_route` のように「app.py 側が
条件付きでフォールスルーしたときだけ動く」コードが実在する。

> rules.py を直したのに挙動が変わらない、という事故が繰り返し起きている。
> **必ず先に app.py 側で拾われていないかを確認すること。**

新しいコマンドを足すときの原則:

- プロトコルエンジンの状態を読む/書くもの → **app.py 側**
- 装置種別ごとの定型応答・ヘルプ・補完 → **rules.py 側**

### 「到達しないコード」と「未テスト」は別物

カバレッジを測ると `engine/rules.py` に一度も実行されない関数が125件
あり、上位は `_asa_process`(約345行) / `_bigip_process`(約134行) /
`_pc_process`(約87行) だった。二層ディスパッチで死んでいると疑ったが、
**実際に動かしたら3つとも正常に動作していた**。単にテストが無かっただけ。

→ `tests/test_untested_device_types.py` で現状の動作を固定した。
**カバレッジが低い＝デッドコード、と決めつけないこと。**

---

## 2. 設定サブモードは登録簿に1行足すだけ

`engine/rules.py` の `CONFIG_SUBMODES` に
`"モード名": ("親モード", ("exit時に消す属性", ...))` を足す。

```python
CONFIG_SUBMODES = {
    "config-openflow":        ("config",          ()),
    "config-openflow-switch": ("config-openflow", ('_of_switch',)),
    ...
}
```

`process()` の許可モードと `_cmd_exit()` の戻り先は**両方ここから導出**
される。`tests/test_config_submodes.py` が全モードを総当たりで検証する。

> かつては2か所に別々に書く必要があり、片方を忘れるとコマンドが
> ハンドラに届かず `% Invalid input detected` になった。
> ZBFW実装時に実際に踏み、原因特定に時間を溶かしている。

`exit` は一段ずつ戻り、`end` はどこからでも exec まで戻る
（`end` が一段しか戻らない不具合も、この総当たりテストで発見した）。

---

## 3. 小文字化したコマンドから「値」を取り出さない

判定には `c = command.lower()` を使う慣習だが、**その match から値を
取り出すと大小文字が潰れる**。実害が出た例:

| 箇所 | 症状 |
|---|---|
| `snmp-server community` | コミュニティ文字列（認証情報）が小文字化 |
| `neighbor <ip> password` | BGPパスワードが小文字化 |
| `tacacs-server host key` | TACACSキーが小文字化 |
| ZBFW の policy-map 名 | 名前が一致せず適用できない |
| Si-R の事前共有鍵 | IPsecがLARVALのまま上がらない |
| ISMU のファイル名 | `CSCvk58435` → `cscvk58435` |
| MDT の xpath | モデル名が別物になる |

**対処**: `app.py` の `orig_groups(m, orig)` を使う。
同じパターンを元コマンドに当て直して、大小文字を保った match を返す。

```python
m = re.match(r'^snmp-server\s+community\s+(\S+)\s+(ro|rw)', c)
if m:
    name = orig_groups(m, orig).group(1)   # ← 元の大小文字が残る
```

---

## 4. 並列リンクは `interface_links` では表現できない

`vnet.interface_links` は `{peer -> iface}` と **1本しか覚えられない**。
同一ペア間に主回線と予備回線を張ると**後勝ちで上書き**される。

**対処**: `vnet.ifaces_between(a, b)` / `vnet.up_ifaces_between(a, b)`
を使う。`link_ifaces`（全リンクを持つ）と `interface_links` をまとめて
返す。

```python
# NG: 並列リンクで誤判定する
iface = vnet.interface_links.get(a, {}).get(b)

# OK
ifaces = vnet.ifaces_between(a, b)
alive  = vnet.up_ifaces_between(a, b)
```

> 主回線をshutdownしても予備回線側のIF名しか見えず「まだ生きている」と
> 誤判定する、という事故が起きた。`interface_links` は**表示用**と
> 割り切ること。

---

## 5. OSPFには実装が2つある

| 実装 | 中身 |
|---|---|
| `engine/protocols.py` `OspfEngine` | vnet上のシミュレーション |
| `engine/real_ospf_agent.py` | scapyの実パケット（全装置が `lo` を共有） |

`show ip ospf neighbor` に出るのは**後者が同期した内容**。前者だけを
操作しても表示は変わらない。片方だけ直すと表示と実体が食い違う。

実リスナーは `lo` を共有しているため、**サブネットマスクを決め打ちに
すると別セグメントの装置とも隣接してしまう**（`_mask_for_ip()` で
インタフェースの実際のprefixを使うよう修正済み）。

---

## 6. 障害試験をしないと再収束のバグは見つからない

収束状態が正しくても、**再収束が正しいとは限らない**。

- OSPF: shutdownしても隣接が落ちず経路も撤回されない不具合が**10件**
- EIGRP: 2台構成で設定した瞬間に**無限再帰でクラッシュ**
  （`receive → _send_update → send_to → receive → …`。
  `vnet.send_to` は `receive` を直接awaitするのでスタックが伸び続ける）

いずれも「静的な状態だけを見るテスト」では素通りしていた。
新しいプロトコルを足したら、必ず

```
主回線 = 動的プロトコル / 予備回線 = AD 210 のフローティングスタティック
→ 主回線 shutdown → 予備に切り替わるか
→ no shutdown → 戻るか
```

を確認すること（`tests/test_ospf_failover.py`,
`tests/test_failover_regression.py` が雛形）。

Hello 10秒 / Dead 40秒が既定なので、**障害後は最低50秒**待つ。

---

## 7. 検証時のハマりどころ

1. **引用符を含むコマンドをシェル経由のcurlで流さない。**
   `curl -d '{"command":"action 1.0 syslog msg \"x\""}'` はシェルの
   クォート処理で壊れる。EEMの検証で「アクションが保存されない」と
   誤診しかけた（Pythonドライバで流すと正常だった）。
   **JSONの組み立てはPython側でやること。**
2. **装置IDとIPの重複に注意。** `saved_config.json` に前回の装置が
   残る。試験前に `curl -s localhost:8000/api/status` で確認する。
3. `/api/cli` のレスポンスキーは **`output`**（`response` ではない）。
   `/api/link` のパラメータは **`iface_a` / `iface_b`**。
4. 認証を切って起動するには `NETLAB_AUTH_DISABLE=1`。
5. `pkill -f "uvicorn app:app"` は**自分自身のシェルにマッチして落ちる**。
   `pkill -f "uvicorn app:[a]pp"` と書く。

---

## 関連ドキュメント

- `docs/ospf-failover-floating-static.md` — OSPF障害切替の検証記録
- `docs/gnmi-telemetry.md` — gNMI/MDT
- `docs/eem-apphosting-openflow.md` — EEM/App Hosting/OpenFlow
- `docs/netconf-restconf-service-acl.md` — サービスレベルACL
