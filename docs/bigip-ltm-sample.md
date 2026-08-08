# F5 BIG-IP (LTM) コマンドサンプル

エミュレータで**動作確認済み**の F5 BIG-IP（TMOS / tmsh）LTM 設定サンプル。
機種追加で「＋ F5 BIG-IP」を選ぶと `bigip` 機種として作成できる。

- CLI は `tmsh <cmd>`（bash から）でも、tmsh シェル内で `<cmd>` 直接でも受け付けます。
- 負荷分散対象（プールメンバー）の up/down を手動で切り替えて動作確認できます。

```
              ┌─ Virtual Server (VIP) ─┐
 client ──▶ 192.0.2.10:80 ──▶ Pool web_pool ──┬─▶ 10.0.0.1:80 (member)
                                              ├─▶ 10.0.0.2:80 (member)
                                              └─▶ 10.0.0.3:80 (member)
                          load-balancing-mode / monitor で制御
```

---

## 1. プール作成（メンバー・モニター・負荷分散方式）

```
tmsh create ltm pool web_pool {
    members add { 10.0.0.1:80 10.0.0.2:80 10.0.0.3:80 }
    monitor http
    load-balancing-mode least-connections-member
}
```
> 1行で書いてもOK:
> `tmsh create ltm pool web_pool { members add { 10.0.0.1:80 10.0.0.2:80 } monitor http }`

## 2. 仮想サーバ（VIP）作成

```
tmsh create ltm virtual vs_web {
    destination 192.0.2.10:80
    pool web_pool
    profiles add { http tcp }
}
```

## 3. 状態確認

```
tmsh show ltm pool web_pool
```
```
Ltm::Pool: web_pool
------------------------------------------------------------
  Status
    Availability : available (green)
    State        : enabled
    Load Balancing Mode : least-connections-member
    Monitor      : http
    Members      : 3 (up: 3, down: 0)

  Member: 10.0.0.1:80   available (green)
  Member: 10.0.0.2:80   available (green)
  Member: 10.0.0.3:80   available (green)
```
```
tmsh show ltm virtual vs_web          # 仮想サーバ（宛先/プール/upメンバー数）
tmsh show ltm virtual detail          # detail / all-properties も可
tmsh show ltm node                    # ノード（実サーバ）一覧と状態
```

## 4. メンバーの手動 up/down（負荷分散の動作確認）

```
# メンバーを user-down（保守などで切り離し）
tmsh modify ltm pool web_pool members modify { 10.0.0.1:80 { state user-down } }

tmsh show ltm pool web_pool
#   Members : 3 (up: 2, down: 1)
#   Member: 10.0.0.1:80   offline (red)   ← 切り離された
```
→ `show ltm virtual` の「Members up」も 2 に減り、VIP の稼働メンバーが変化します。
戻すには `{ state user-up }`（または再度 `user-enabled`）。

## 5. running-config / list

```
tmsh show running-config          # LTM 設定を tmsh 形式で出力
tmsh list ltm pool web_pool       # 個別セクションを list 形式で出力
tmsh list ltm virtual vs_web
```

## 6. 削除

```
tmsh delete ltm virtual vs_web
tmsh delete ltm pool web_pool
```

---

## 対応コマンド一覧

| コマンド | 説明 |
|---|---|
| `create/modify ltm pool <name> { members add {...} monitor <m> load-balancing-mode <mode> }` | プール |
| `modify ltm pool <name> members modify { ip:port { state user-down\|user-up } }` | メンバー手動 up/down |
| `modify ltm pool <name> members delete { ip:port }` | メンバー削除 |
| `create/modify ltm virtual <name> { destination ip:port pool <p> profiles add {...} }` | 仮想サーバ(VIP) |
| `create ltm node <name> { address <ip> }` | ノード |
| `create ltm monitor <type> <name>` | ヘルスモニター |
| `show ltm pool [<name>] [detail]` | プール状態 |
| `show ltm virtual [<name>] [detail]` | 仮想サーバ状態 |
| `show ltm node` | ノード状態 |
| `show running-config` / `list ltm ...` | 設定出力 |
| `delete ltm pool\|virtual\|node\|monitor <name>` | 削除 |
| `save sys config` / `show sys version` | 保存 / バージョン |

## 補足

- 現状は **LTM（ローカル負荷分散）中心**の実装です。GTM(DNS)/APM(アクセス)/ASM(WAF) や
  iRule、SNAT の詳細動作は未対応です。
- 実機の Availability 判定はヘルスモニターの結果で自動的に変わりますが、本エミュレータでは
  メンバーの手動 up/down で負荷分散対象の増減を再現します。
- TeraTerm マクロ等での自動投入も、上記コマンドをそのまま送出すれば同様に動作します
  （プロンプト待ちは `tmsh` シェルの `# ` を目印にしてください）。
