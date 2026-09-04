# ルーティングジェネレーター（CLI）

`tools/routing_generator.py`

## これは何か

仮想ラボの装置に大量のスタティックルートをCLI経由で注入し、RIBの経路数
（`netlab_route_count`）が実際に増えることを、SNMPダッシュボードや
Prometheus/Grafana側から確認できるようにするツール。

```
tools/routing_generator.py
   │  `ip route <net> <mask> <next-hop>` をN回投入
   ▼
Network Lab Emulator（rib_engine）
   │  route_count = len(rib_engine.get_best_routes(device_id))
   ▼
/api/snmp/dashboard → Prometheus Exporter（netlab_route_count）→ Grafana
```

## 使い方

```bash
# 既定: catalystに 10.50.0.0/24 から連番で100経路注入
python tools/routing_generator.py

# 件数・対象・ベースネットワーク・ネクストホップを指定
python tools/routing_generator.py --device catalyst --count 500 \
    --base-network 172.16.0.0 --prefix 24 --next-hop 10.9.9.2

# 注入した経路を削除（クリーンアップ）
python tools/routing_generator.py --count 500 --base-network 172.16.0.0 \
    --next-hop 10.9.9.2 --cleanup
```

`--next-hop`はあらかじめ対象装置のインターフェースに設定済みの、到達可能な
IPを指定する必要がある（`tools/demo_monitoring_pipeline.py`でセットアップ
した`10.9.9.1`⇄`10.9.9.2`のリンクを使うのが手軽）。

## 実証済みの動作（実Prometheusでの確認）

```
$ python tools/routing_generator.py --device catalyst --count 100 \
    --base-network 172.20.0.0 --next-hop 10.9.9.2
注入前の経路数: 52
注入後の経路数: 152
差分: +100
```

実際にPrometheus（GitHub Releases経由でダウンロードした公式バイナリ）を
起動してscrapeし、`netlab_route_count{device_id="catalyst"}`をクエリして
52 → 152に増加したこと、グラフが階段状に増える様子を確認済み。

## 制約

- スタティックルートのみ対応（RIP/OSPF/BGP経由の大量経路生成は対象外。
  必要であれば`redistribute`と組み合わせて拡張可能）
- `--next-hop`が到達不能なIPの場合、経路自体は登録されるが実質的には
  無効なルートになる（このエミュレーターはCiscoのCLI仕様同様、
  next-hopの到達性チェックはルート登録時には行わない）

## テスト

```bash
pytest tests/test_routing_generator.py -v
# 5/5 成功
```
