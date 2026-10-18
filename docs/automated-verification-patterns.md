# 自動検証のパターン集 — ユーザー主導ではなくClaude主導で検証対象を見つける方法

## これは何か

ユーザー指摘「今までの検証を色々やってみましたが自分が依頼したものが
多いのでどうかなと...opus使うので試験と試験結果まとめをClaudeに案を
出して、自動検証のパターン出しとかお願いしたい」への対応。

これまでのこのセッションの検証は、ほとんどが「ユーザーがこの機能を
試してほしい」という具体的な依頼から始まっていた。ここでは逆に、
**Claude側がコード自体を調査して検証対象を自発的に見つける**ための
再利用可能なパターンを書き残す。実際にこのパターンの(3)を適用した
結果、`app.py`の設定永続化層(`_save_config`/`_load_config`)が
このセッションで追加されたBIG-IP/ALBの全状態を一度も保存・復元して
いない——アプリ再起動のたびに全て消える——という、誰にも指摘されて
いなかった実バグを見つけた(下記「パターン3の実例」参照)。

## パターン1: 組み合わせ検証(クロス機能テスト)

**狙い**: 別々のタイミング・別々の文脈で実装された2つの機能は、
それぞれ単体ではテスト済みでも、**組み合わせたときの振る舞い**は
一度も検証されていないことが多い。

**やり方**:
1. `CLAUDE.md`を読み、「〇〇を実装した」というセッション履歴の中から
   互いに無関係な(別の依頼・別の時期に追加された)機能を2つ以上選ぶ。
2. 実機ではその2機能がどう相互作用するはずか(ドキュメント/実機の
   仕様)を確認する。
3. 実際に両方を同時に設定し、相互作用が正しいかをライブ検証する。

**このセッションでの実例**: 「モックIP/Self IP」(`net vlan`/`net self`)
と「SNAT(automap)」は別々に実装されたため組み合わせテストが一度も
無かった。実機のAutoMap SNATはVIPではなくSelf IPを送信元に使うはずだが、
実際に組み合わせてみると常にVIPを使っていた——実バグ。
(`docs/unusual-verifications-gnmi-cps-automap.md`の「3」)

**他に適用できそうな組み合わせ(未実施)**:
- Juniper NETCONF(実装済み) × BIG-IP/ALBの背後に置く(gNMIと同じ
  「VirtualServerProxyは本当にプロトコル非依存か」のNETCONF版)。
- RTBH(BGP Null0経路) × BIG-IPのヘルスチェック(Null0経路でブラック
  ホール化したバックエンドをBIG-IPのヘルスチェックが正しくdown判定
  するか)。
- VRRP unicast-peer × ALBのターゲットグループ(VIPがVRRPで冗長化
  されたバックエンドに向いている場合のフェイルオーバー)。

## パターン2: 負荷の「軸」を変えたストレス試験

**狙い**: 同じ機能に対する既存の負荷試験が「同時並列数」だけを見て
いるなら、「新規接続生成レート」「接続の持続時間」「ペイロードサイズ」
など別の軸でも壊れ方を確認する。壁にぶつかる閾値や壊れ方の*表現*が
軸ごとに違うことがある。

**やり方**:
1. 既存の負荷試験ツール(`tools/k6_slb_load.js`等)が何を変数にしている
   かを確認する(同時VU数、など)。
2. 同じ対象(同じVIP/同じバックエンド)に対して、別の軸(新規接続レート
   等)で独自の簡易負荷スクリプトを書く。
3. 既存の結果(例: 200 VUでGIL競合が顕在化)と比較し、壊れ方の性質の
   違いを記録する。

**このセッションでの実例**: k6のHTTP試験(同時並列200 VU)でCPython
GIL競合が見つかっていたが、SSHバックエンドへの秒間新規コネクション
レート(CPS)試験でも、~100 CPS付近で別の表現(接続失敗/レイテンシ激増)
として同じ根本原因が再現した。(`docs/unusual-verifications-gnmi-cps-automap.md`の「2」)

## パターン3: 「横展開されていない機能」の持続性チェック(状態永続化の監査)

**狙い**: 新しいdevice_type/機能を追加する際、`app.py`の設定永続化層
(`_save_config()`/`_load_config()`)は**自動では追従しない**——
`DeviceState`の新しい属性を保存・復元リストに手で追加しないといけない、
手動管理のallowlist方式になっている。新機能を追加したセッションの
中で「保存・復元」は実際にテストされないことが多く、サイレントに
漏れたまま何セッションも気づかれないことがある。

**やり方(チェックリスト化)**:
1. `app.py`の`_save_config()`/`_load_config()`を開き、各
   `if state.device_type == "X":`ブロックの一覧を作る。
2. `CLAUDE.md`で「新しいdevice_type」「新しい状態を持つ機能」として
   追加されたものの一覧(grep `device_type ==`等)と突き合わせ、
   **永続化ブロックに存在しないdevice_type/属性**を探す。
3. 見つかったら、実際に`_save_config()`→(擬似的な再起動として)
   `device_sessions`を空にする→`_load_config()`という往復を、
   辞書状態の復元**と**(リアルソケットを持つ機能なら)実リスナーの
   再起動の両方について確認する。辞書が戻るだけでは「直った」とは
   言えない。

**このセッションでの実例(今回新たに発見・修正)**: BIG-IPのLTM設定
(pool/virtual/monitor/persistence/snatpool/net vlan/net self)と
ALBのtarget-group/listener/ruleが、追加されたセッションでは一度も
`_save_config`/`_load_config`に反映されておらず、**アプリの再起動
(=60秒ごとの自動保存→次回起動時のロード)のたびに全て消え、実TCP
リスナー(VirtualServerProxy/AlbListenerProxy)も再起動されないまま
失われる**という実バグだった。`_save_config()`/`_load_config()`に
両device_typeの保存・復元ブロックを追加し、`_load_config()`の最後
(`_rebuild_all_neighbors()`の直後)で復元済みの`f5_virtuals`/
`alb_listeners`に基づいて実リスナーを再起動する処理を追加した
(Juniperのcommit後SSHリスナー起動と同じ「post-restore再評価」
パターン)。`tests/test_bigip_alb_config_persistence.py`が、辞書
状態の往復**と**復元後に実際にTCP/HTTP接続が通ることの両方を固定する。

**まだ永続化されていない既知のギャップ(スコープ外、次の一手)**:
Juniper(`junos_candidate`/`junos_committed`)・Yamaha(NAT descriptor/
PPPoE設定)・BAS(`map_e_engine`のプール設定)・FortiGate
(`fortigate_pppoe`/phase1-interface)・ASA threat-detection
(`state.threat_detection`)の状態は、本調査時点でもまだ永続化層に
含まれていない。パターン3のチェックリストをそのまま適用すれば
横展開できる。

## パターン4: 「二層/三層ディスパッチの先取り」の横断チェック

**狙い**: このリポジトリで繰り返し踏まれている実バグのクラス——
`app.py`の汎用ハンドラ(`handle_protocol_show`/`handle_protocol_config`/
`_build_running_config`/`_validate_command`等)が、device_type固有の
`RuleEngine`ハンドラより**先に**評価されるため、新しいdevice_typeを
追加したときにこれらの汎用ハンドラが明示的な除外/委譲リストに
入っていないと、新device_typeのコマンドがサイレントに誤処理される。
これは機能追加のたびに繰り返し踏まれており(ASA/bas/fortigate/alb/
bigipの`show running-config`で過去に複数回発生)、**次の新
device_type追加時にも同じ罠が待っている**。

**やり方**: 新しいdevice_typeを追加したら、以下の汎用ハンドラの
device_type別分岐リストに新device_typeが入っているかを**機械的に
grep確認する**チェックリストとして使う:
- `app.py`: `handle_protocol_show`、`handle_protocol_config`、
  `_build_running_config`、`RuleEngine._validate_command`の冒頭の
  各種ガード
- `engine/rules.py`: `_expand_abbreviation`のスキップリスト

## まとめ: 新device_type/新機能追加時のチェックリスト

機能追加そのものが完了した時点で、以下を自発的に確認する(ユーザーに
指摘される前に):

1. **永続化**(パターン3): `_save_config`/`_load_config`に保存・復元
   ブロックを追加したか。実際に保存→再起動→復元の往復テストを
   書いたか(辞書状態+実リスナーの両方)。
2. **二層/三層ディスパッチの先取り**(パターン4): 汎用ハンドラの
   device_type別分岐に追加したか。
3. **組み合わせ検証**(パターン1): このセッションで追加した他の機能と
   組み合わせて動くかを最低1つ試したか。
4. **負荷の別軸**(パターン2): 既存の負荷試験ツールがある機能なら、
   違う軸でのストレスも最低1回試したか。

この4点を、機能追加のたびに「完了」と報告する前のセルフチェックとして
運用する——ユーザーに次の検証対象を聞く前に、まずこの4点を自分で
確認する、という運用ルール。
