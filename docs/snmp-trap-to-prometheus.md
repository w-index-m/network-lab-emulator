# SNMP Trap → Prometheus/Alertmanager ブリッジ

PrometheusはPull型のため、機器が送るSNMP Trap（Push型）を直接受信できない。
`tools/snmp_trap_receiver.py` はUDPでSNMPv2c Trapを受信し、Prometheusが
スクレイプできる `/metrics` として公開する（追加パッケージ不要、標準ライブラリのみ）。

対応済みプロトコルスタック: Cisco / Catalyst / Si-R / SR-S / Nexus / APRESIA
（`snmp-server host` / `snmp trap host` いずれも実UDP送信され、機種を問わず
このツールで受信できる — Cisco機種での動作は2026-09-05に実機構成で確認済み）。

## 使い方

```bash
# デフォルト: UDP 1162 でTrap受信、9162番でメトリクス公開
# (162番は特権ポートのため、root権限が要らないよう既定を1162にしている)
python tools/snmp_trap_receiver.py

# 実機同様162番で受けたい場合（要root）
sudo python tools/snmp_trap_receiver.py --trap-port 162
```

エミュレーター側の設定（Cisco/Catalystの例。ポートは受信ツールに合わせる）:

```
conf t
snmp-server host <受信ツールのIP> udp-port 1162 traps public
interface GigabitEthernet0/1
shutdown        ! → linkDown trapを実送信
no shutdown     ! → linkUp trapを実送信
```

Si-Rの場合:
```
snmp trap host <受信ツールのIP> community public
```
（Si-Rの`snmp trap host`はudp-port指定に対応していないため162番固定。
受信ツール側を`--trap-port 162`でroot起動する必要がある）

## Prometheus設定

`prometheus.yml`:
```yaml
scrape_configs:
  - job_name: 'snmp-trap-receiver'
    static_configs:
      - targets: ['localhost:9162']
```

## メトリクス

- `snmptrap_received_total{source_ip, trap_oid, trap_name}` — 送信元・OID別の累計受信数
- `snmptrap_last_received_timestamp_seconds{source_ip, trap_oid, trap_name}` — 最終受信時刻
- `snmptrap_linkdown_total` / `snmptrap_linkup_total` — 全ソース合計のlinkDown/linkUp数
  （アラートルールを書きやすいよう別名でも公開）

`/recent` にGETすると直近200件の生ログをJSONで確認できる（デバッグ用）。

## Alertmanagerルール例

```yaml
groups:
  - name: snmp-traps
    rules:
      - alert: SnmpLinkDownTrapReceived
        expr: increase(snmptrap_linkdown_total[5m]) > 0
        labels:
          severity: warning
        annotations:
          summary: "直近5分でlinkDown trapを受信"
```

## 実装メモ

- `engine/syslog_sender.py`のトラップ送信フォーマット（version/community/PDU、
  varbindに sysUpTime・snmpTrapOID・sysDescr("hostname: description")）に
  合わせた最小限のBERデコーダを実装。厳密なASN.1バリデーションは行わない
  ベストエフォート実装だが、多くの一般的なSNMPv2c Trapも同じ構造
  （version, community, PDU中にsnmpTrapOID）を持つため、実機からのTrapでも
  概ね動作する。
- 2026-09-05: cisco機種で`snmp-server host ... udp-port 1162 traps public`
  設定後の`shutdown`/`no shutdown`により、実際にlinkDown/linkUp Trapが届き、
  `/metrics`に反映されることを確認済み。
