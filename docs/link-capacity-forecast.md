# リンク帯域逼迫の予測とOSPFコスト見直し提案

`tools/link_capacity_forecast.py`

## これは何か

`docs/anomaly-autoencoder.md` のAutoencoderが「今この瞬間、普段と
違うか」を判定するのに対し、このモジュールは**「このまま増え続けたら
いつ閾値に達するか」というトレンドの外挿**を扱う。

インタフェースの実トラフィックカウンタ（`engine/protocols.py`の
`dp_engine.get_counter()` — ping等の実データプレーントラフィックが
実際にそのIFを通過するたびに増える本物の累積カウンタ）から、
NumPyの`polyfit`で1次線形回帰を取り、帯域の何%かに到達するまでの
残り時間を見積もる。さらに、そのリンクが現在OSPFの最有力経路
（コスト最小）に選ばれているかどうかも見て、
**「最有力経路なのに、このまま増え続けると近いうちに逼迫する」**
場合にコスト見直しを提案する。OSPFは最小コスト経路を選ぶので、
そのリンクのコストを上げれば通信を別経路（あれば）に逃がせる、
という考え方。

合成データではなく、Autoencoderと同じく実際に転送されたバイト数を
使う。1Gbpsのラボ用LANでは事実上飽和しないので、意味のある値が
出るのは低速リンク（Si-RのWAN回線のような数十〜数百kbps級）を
想定した使い方になる。

## 使い方

```python
from tools.link_capacity_forecast import RateSample, analyze_interface

# (時刻, その時点の累積バイト数) のサンプル列。
# engine.protocols.dp_engine.get_counter(device_id, iface) の
# in_bytes+out_bytesを定期的に記録して集める。
samples = [RateSample(t, cum_bytes), ...]

result = analyze_interface(
    samples,
    capacity_bps=256_000,       # そのリンクの物理帯域
    ospf_cost=1,                 # 現在のOSPFコスト
    min_ospf_cost=1,             # このサイトで使われている最小コスト
    target_utilization=0.8,      # 何%を「逼迫」とみなすか
    warn_within_seconds=3600.0,  # 何秒以内に到達しそうなら警告するか
)
# {'current_utilization': 0.42, 'seconds_to_threshold': 330.0,
#  'is_preferred_path': True, 'recommend_raise_cost': True}
```

## 実データでの検証（`python3`で実際に実行して確認）

エミュレータを実際に起動し、2台のCatalystをリンクで結んで
`ping`を10回実行、その都度 `dp_engine.get_counter()` の実カウンタを
サンプリングして`analyze_interface()`に渡した実際の出力：

```
t=0.07s cum_bytes=500
t=0.28s cum_bytes=1000
t=0.48s cum_bytes=1500
t=0.69s cum_bytes=2000
t=0.89s cum_bytes=2500
t=1.09s cum_bytes=3000
t=1.30s cum_bytes=3500
t=1.50s cum_bytes=4000
t=1.70s cum_bytes=4500
t=1.91s cum_bytes=5000

result: {'current_utilization': 2.46, 'seconds_to_threshold': 0.0,
         'is_preferred_path': True, 'recommend_raise_cost': True}
```

`ping`5発×100バイトが約0.2秒おきに実際にICMPとして転送され、
そのつどdp_engineの実カウンタ（合成データではない）が増える。
8,000bps（8kbps）という細い回線を想定してこのペースの実トラフィック
を流すと、実際に利用率が246%（＝閾値を大きく超過）と判定され、
「最有力経路だがコスト見直しを推奨」という結果になることを確認した。
使ったデバイス（`lc-a`/`lc-b`）はデモ後に削除済み。

## デモ（`python3 tools/link_capacity_forecast.py`）

256kbpsのWAN回線で、負荷が徐々に右肩上がりになっていく合成データ
での実行例：

```
現在の利用率: 42.2%
80%到達まで: 約330秒後（現在のトレンドを線形外挿）
最有力経路(OSPFコスト最小)か: True
コスト見直しを推奨: True
```

## テスト

```bash
pytest tests/test_link_capacity_forecast.py -v
# 10/10 成功
```

固定している内容：
1. サンプルが2点未満では計算できないこと
2. 累積バイト数の差分から正しくbpsのレートを計算できること
3. カウンタの巻き戻り（リセット等）を含む区間は除外すること
4. トレンドが横ばい/下降なら「到達しない」(`None`)と判定すること
   （完全に横ばいのデータでも浮動小数点誤差でごく微小な傾きが
   出ることがあるため、1bps/秒未満の傾きは横ばい扱いにしている）
5. 右肩上がりのトレンドから閾値到達時刻を正しく外挿できること
6. 直近サンプルからの利用率計算が正しいこと
7. **最有力経路（OSPFコスト最小）かつ近いうちに逼迫するリンクは
   コスト見直しを推奨すること**（このモジュールの存在意義）
8. 同じ逼迫トレンドでも、既にコストが高く最有力経路でなければ
   推奨しないこと（OSPFがそもそも選ばない経路のため）
9. 最有力経路でも、負荷が増えていなければ推奨しないこと

## 制約・今後の拡張余地

- OSPFコストはこのエミュレータでは装置単位（`ospf_engine.set_cost`）
  であり、実機のようなインタフェース単位ではない。インタフェース
  ごとのコスト管理に対応すれば、より正確な「この特定リンクだけ
  コストを上げる」提案ができる
- `ifSpeed`はこのエミュレータでは固定1Gbps（`engine/protocols.py`の
  SNMP ifTable実装）。低速リンクを扱う場合の`capacity_bps`は
  呼び出し側が別途与える必要がある（回線契約速度など）
- 1次線形回帰のみ。急激な変化（ステップ状の増加）や周期性
  （日中/夜間パターン）には対応していない
- 実際にOSPFコストを自動で書き換えるところまでは実装していない
  （判定を返すだけ。実際の変更はCLIコマンドとして別途打つ想定）
