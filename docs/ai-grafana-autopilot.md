# AI Grafana Autopilot

`tools/ai_grafana_autopilot.py`

## これは何か

人手を介さず、AIが仮想ラボの異常（CPU高騰・インターフェースダウン・
状態フラップ）を検知し、**Grafana Annotations API に自動でインシデントを
書き込む**。Annotationは「タイムライン上のマーカー」機能で、どのダッシュ
ボード・どのグラフパネルを見ていても該当時刻に自動で印がつく。

```
Network Lab Emulator (/api/snmp/dashboard)
   │
   ▼
AnomalyDetector（ルールベース: CPU閾値/interface down/フラップ）
   │
   ├─▶ Ollamaがあれば要約を生成（AI要約）
   │
   ▼
GrafanaClient.create_annotation()
   │  POST /api/annotations
   ▼
Grafana（人手を介さず自動記録される）
```

## 使い方

```bash
# Grafana Service Account トークンを指定して実行
python tools/ai_grafana_autopilot.py \
  --emulator-url http://localhost:8000 \
  --grafana-url http://localhost:3000 \
  --grafana-token <トークン>

# Grafanaが無い/挙動だけ見たい場合は --dry-run
python tools/ai_grafana_autopilot.py --dry-run
```

Grafana Service Account トークンは Grafana UI の
`Administration > Users and access > Service accounts` から発行する
（Annotationの書き込み権限が必要）。

## 検知ロジック

| 種類 | 条件 | severity |
|---|---|---|
| CPU高騰 | `cpu_percent >= 80` | warning（90%以上はcritical） |
| インターフェースダウン | `ifOperStatus != up` | critical |
| フラップ | 直近30秒でインターフェース状態が2回以上変化 | warning |

同一インシデント（タイトルが同じ）は60秒間再投稿を抑制する。

## 検証方法（このリポジトリ内でのテスト）

実際のGrafanaはこの開発環境からダウンロードできない
（`docs/prometheus-grafana-windows.md`参照）ため、以下の2段階で検証した:

1. **単体テスト**: `tests/test_ai_grafana_autopilot.py` で、Grafana
   Annotations APIと同じ形式（`POST /api/annotations`、`Authorization:
   Bearer <token>`、`{time, tags, text}`）を話す**モックHTTPサーバー**を
   標準ライブラリのみで実際に起動し、`GrafanaClient.create_annotation()`
   が正しいペイロードを送ることを実HTTP通信で確認（8/8成功）

2. **実機統合確認**: 実際にエミュレーターを起動し、`catalyst`の
   インターフェースをCLIで`shutdown`して異常を発生させ、
   `ai_grafana_autopilot.py`（dry-runなし・モックGrafanaサーバー宛て）を
   実行。モックサーバーのログに実際に
   ```
   RECEIVED: {"time": ..., "tags": ["netlab", "interface-down", "catalyst"],
              "text": "[CRITICAL] Dist-SW: GigabitEthernet1/0/1 がダウン..."}
   AUTH: Bearer demo-token-xyz
   ```
   が記録されることを確認した。**人が「異常発生→記録」の間を一切操作していない**。

### 再現ログ（2026-09-13）

ユーザー要望でデモを再実行し、上記2の手順を新規デバイスで再確認した。

```bash
# モックGrafana（標準ライブラリのみ）を起動
python3 mock_grafana.py 3999

# エミュレーターを実際に起動
NETLAB_AUTH_DISABLE=1 python3 -m uvicorn app:app --host 127.0.0.1 --port 8010

# デバイスを作成し、Gi1/0/1をno shutdownでUpにする
curl -u admin:admin -X POST localhost:8010/api/device \
  -d '{"device_id":"demo-sw","type":"cisco","hostname":"Demo-SW"}'
curl -u admin:admin -X POST localhost:8010/api/cli \
  -d '{"device_id":"demo-sw","command":"enable"}'
# configure terminal → interface GigabitEthernet1/0/1 →
# ip address 10.250.0.1 255.255.255.0 → no shutdown

# autopilotを実際に3秒間隔で起動（バックグラウンド）
python3 tools/ai_grafana_autopilot.py \
  --emulator-url http://127.0.0.1:8010 \
  --grafana-url http://127.0.0.1:3999 \
  --grafana-token demo-token-xyz --interval 3
```

健全な状態では何も検知しないことをまず確認した上で、実際にCLIで
インターフェースを落とした：

```bash
curl -u admin:admin -X POST localhost:8010/api/cli \
  -d '{"device_id":"demo-sw","command":"shutdown"}'
```

3秒以内（次のポーリング）に自律的に検知し、モックGrafanaへ実際にPOSTされた：

```
[16:50:33] 🔎 1件の異常を検知
  ✅ demo-sw: GigabitEthernet1/0/1 がダウン

RECEIVED: {"time": 1789318233935, "tags": ["netlab", "interface-down", "cisco"],
           "text": "[CRITICAL] demo-sw: GigabitEthernet1/0/1 がダウン\nifOperStatus=down（管理状態: down）"}
AUTH: Bearer demo-token-xyz
```

指定した`--grafana-token`がそのまま`Authorization: Bearer`ヘッダに
使われていること、`severity=critical`に対応するタグ・本文が付くことも
実通信で確認できた。デモ用に起動したuvicorn/モックサーバーは終了後に
停止している（このデモはリポジトリに恒久的な状態を残さない）。

## 実際のGrafanaに繋ぐ場合

`--grafana-url`を実際のGrafanaのURLに、`--grafana-token`を発行した
Service Accountトークンに置き換えるだけでよい（モックサーバーと
本物のGrafanaは同じAPI形式なので、コード変更は不要）。

## テスト

```bash
pytest tests/test_ai_grafana_autopilot.py -v
# 8/8 成功
```

## 制約・今後の拡張余地

- Annotation（記録）のみ対応。Grafana Alert Rule（アラート通知）の
  自動作成はスコープ外（別APIで、より複雑なスキーマが必要）
- 同一インシデントの重複抑制は60秒固定（設定不可）
- Slack/メール等への横展開は`docs/syslog-ai-monitor.md`と組み合わせて
  拡張可能
