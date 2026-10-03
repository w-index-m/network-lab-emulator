# PPPoE → IPsec 構成: 現状と全体マトリクスまとめ

ユーザー依頼: 「Ciscoは、PPPoEで繋いでそのあと、IPsecで繋がるし
Cisco、YAMAHA、si-rそれぞれ相互的にPPPoEしてそのあとIPsecして欲しい
まとめもお願いします」への回答として、**今すでに動いているもの**と、
**全ベンダー相互のPPPoE→IPsecを実現するために何が必要か**を整理する。

## 1. 今すでに動いているもの(ライブ検証済み・テスト済み)

```
┌─────────┐  擬似PPPoE   ┌──────────┐
│ Yamaha  │ ───────────→ │   bas    │ (擬似FLET'S収容局/ルールサーバー)
│  RTX    │ ←動的IP払い出し │ (IP pool) │
└────┬────┘               └──────────┘
     │ PPPoE払い出しIPを
     │ ipsec ike local-addressに使う
     ▼
┌─────────┐
│  Cisco  │ (固定WAN IP、crypto map)
│   IOS   │
└─────────┘
```

- **Yamaha RTX(PPPoEクライアント)→ `bas`(収容局)**: `pp select`/
  `pppoe use`/`pp auth myname`/`pp enable` で実際にLCP/PAP-CHAP認証/
  IPCPのIP払い出しに相当する処理が走り、プールから実IPを取得する
  (`engine.protocols.PppoeEngine`)。
  → `docs/`配下は本件固有のmdは無いが、`tests/test_pseudo_flets_pppoe.py`
  (9 tests)で固定済み。CLAUDE.mdの「擬似FLET'S network」の項目に
  詳細あり。
- **Yamaha RTX(PPPoE払い出しIP)⇔ Cisco IOS(固定WAN)のIPsec**: PPPoEで
  取得した動的IPを`ipsec ike local-address`に使い、Cisco側の
  `crypto isakmp key ... address <そのIP>`と実際にネゴシエーションが
  成立する(`engine/ike_engine.py`の`sir`/`srs`判定を`yamaha`にも
  拡張済み)。
  → `tests/test_yamaha_cisco_ipsec_over_pppoe.py`(6 tests)。
- **Yamaha RTX(v6プラス/MAP-E払い出しIP)⇔ Cisco IOSのIPsec**: PPPoEの
  代わりにMAP-E(IPoE)払い出しIPを使う版。同じ`ike_engine.py`がIPの
  出自を区別しないため、これも無改修で成立。
  → `tests/test_yamaha_cisco_ipsec_over_ipoe_mape.py`(6 tests)。

**つまり現状は「Yamaha RTXだけがPPPoEクライアントになれる」状態**で、
Cisco・Si-Rは`bas`に対してPPPoEクライアントとして繋ぐCLI自体が無い。
IPsec側(`ike_engine.py`)はSi-R↔Si-R/Si-R↔Cisco/Cisco↔Cisco/
Yamaha↔Cisco・Si-Rを全部サポート済みなので、**ボトルネックは
「PPPoEクライアントになれる機種がYamahaしかない」という1点だけ**。

## 2. 全ベンダー相互のPPPoE→IPsecマトリクス(目標形)

6通りの組み合わせ(自己接続3種+相互3種)を想定。現状の対応状況:

| PPPoE接続元 \ IPsec対向先 | Cisco | Yamaha RTX | Si-R |
|---|---|---|---|
| **Cisco**(PPPoEクライアント) | - | ❌PPPoE未対応 | ❌PPPoE未対応 |
| **Yamaha RTX**(PPPoEクライアント) | ✅実装済み(ライブ検証済み) | △PPPoE: Yamaha同士は未検証、IPsec: Si-R↔Si-R対応ロジック流用可 | ❌Si-R側PPPoE未対応 |
| **Si-R**(PPPoEクライアント) | ❌PPPoE未対応 | ❌PPPoE未対応 | ❌PPPoE未対応 |

凡例: ✅=実装・ライブ検証・テスト済み / △=片方の要素は流用できるが未検証 / ❌=未実装

**結論**: 今の質問(「Cisco、Yamaha、Si-Rそれぞれ相互にPPPoEしてIPsec」)
に答えるには、**CiscoとSi-Rの両方にPPPoEクライアント機能を追加する**
必要がある。IPsec側は`ike_engine.py`が既に全組み合わせ
(`sir`/`srs`/`yamaha`、Ciscoはそもそも対応済み)をサポートしているため、
PPPoEクライアントが使えるようになった時点で、組み合わせは基本的に
自動的に繋がるはず(Yamaha⇔Ciscoで実証済みのパターンがそのまま
他の組み合わせにも当てはまる設計のため)。

## 3. 実装が必要な部分(スコープ)

### 3-1. Cisco IOSにPPPoEクライアント機能を追加

実機Cisco IOSのPPPoEクライアント設定は代表的にこの形:

```
interface Dialer1
 ip address negotiated
 encapsulation ppp
 ppp authentication pap callin
 ppp pap sent-username <user> password <pass>
interface GigabitEthernet0/0
 pppoe-client dial-pool-number 1
```

- `pppoe-client dial-pool-number <N>`(物理interface配下)と
  `interface Dialer<N>` + `encapsulation ppp` + `ppp pap sent-username`
  を新規サポートし、`app.py`の`handle_protocol_config`から
  `pppoe_engine.connect()`を呼ぶ(Yamahaの`pp enable`トリガーと
  同じ構造、device_typeの判定を`cisco`/`catalyst`にも広げる形)。
- IPアドレス反映先は`state.interfaces['Dialer<N>']`(Yamahaの`pp<N>`
  と同じ役割)。

### 3-2. Si-RにPPPoEクライアント機能を追加

実機Si-RのPPPoE設定は(マニュアル上)`pp`コンテキストを使う形式
(Yamahaに近い)。具体的なコマンド名はSi-Rマニュアルで確認してから
実装する必要がある(現時点では未調査)。構造としては:

- `_sir_process`(`engine/rules.py`)にPPPoEクライアント設定の
  サブモードを追加。
- `app.py`側に「接続トリガー」コマンドのハンドラを追加し、
  `pppoe_engine.connect()`を呼ぶ(Yamahaの`pp enable`と同じパターン)。

### 3-3. IPsec側(ike_engine.py)の変更は基本的に不要

現在`dt in ('sir', 'srs', 'yamaha')`のタプルに、CiscoはそもそもIPsec
対応済み(別の判定分岐)なので、PPPoEクライアントとして動くように
なった時点で:

- Cisco(PPPoE払い出しIP)⇔ Yamaha/Si-R/Cisco のIPsec
- Si-R(PPPoE払い出しIP)⇔ Yamaha/Si-R/Cisco のIPsec
- Yamaha(PPPoE払い出しIP)⇔ Yamaha のIPsec(Yamaha同士。現状は
  Yamaha⇔Ciscoしかライブ検証していないが、ロジック上は対応している
  はず)

は追加のIKEエンジン変更なしで成立する見込み。実際に繋がるかは
各組み合わせでライブ検証が必要(このプロジェクトの方針: コードレビュー
だけで済ませず、実際に繋げて確認する)。

## 4. 次のステップ

実装するなら、優先順位はこう考えている:

1. **Cisco PPPoEクライアント** — 実機のcisco pppoe-client構文は
   既にある程度知られているため、調査コストが低い。
2. **Si-R PPPoEクライアント** — マニュアル確認が先に必要。
3. 1・2がどちらも入った時点で、6通りの組み合わせを総当たりで
   ライブ検証し、テストファイル(`tests/test_<vendor>_pppoe_ipsec_mesh.py`
   のような形)で固定する。

進めてよければ、まず3-1(Cisco)から着手します。
