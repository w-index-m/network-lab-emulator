# 旧世代Cisco ISRルータ 切り分けツール

`tools/cisco_router_triage.py`

## これは何か

network-lab-emulatorの`cisco`デバイス（ISR4321相当）に対して、
CPU・メモリ・インターフェース・ログを横断的に自動チェックし、
「問題があるか」「あるなら何が疑わしいか」を日本語で提示する切り分けツール。

`tools/syslog_ai_monitor.py`・`tools/oscap_ai_advisor.py`と同じ設計:
Ollamaがあれば自然文のアドバイスに言い換え、無ければルールベースの
テンプレートでそのまま出す（`--no-ai`で明示的にテンプレートのみにもできる）。

```
show processes cpu ─┐
show memory statistics ─┤
show ip interface brief ─┼─▶ 各種パーサー ─▶ 閾値判定(ok/warning/critical) ─▶ 日本語アドバイス
show interfaces counters errors ─┤              （Ollamaがあれば自然文化）
show logging ─┘
```

## チェック項目と閾値

| 項目 | warning | critical |
|---|---|---|
| CPU使用率 | 1分平均 ≥ 50% | 5分平均 ≥ 80% |
| メモリ使用率 | ≥ 80% | ≥ 90% |
| インターフェース | downが1件でもあれば警告 | - |
| インターフェースエラーカウンタ | 累積エラーが1件でもあれば警告 | - |
| ログ | severity 0〜3（emergency〜error）のメッセージがあれば警告 | - |

## 使い方

```bash
# Ollamaがあれば自然文のアドバイス付きで診断
python tools/cisco_router_triage.py --device-id cisco

# Ollamaを使わずテンプレートのみ
python tools/cisco_router_triage.py --device-id cisco --no-ai

# JSON出力（他ツール連携用）
python tools/cisco_router_triage.py --device-id cisco --json
```

終了コード: 重大(critical)項目が1件でもあれば`1`、無ければ`0`
（CI/監視ジョブでの自動判定に利用可能）。

## 実際に確認した動作

1. **正常時**: 全項目がokと判定されることを確認（1件、意図せずdownの
   ままだったインターフェースが警告として検出された。これはエミュレーター側の
   初期トポロジで未接続ポートだったため正しい挙動）
2. **インターフェース障害注入**: `GigabitEthernet0/0/1`をCLIで`shutdown`
   → 直後の診断で
   - 「downしているインターフェースあり」警告
   - `show logging`から`%LINK-3-UPDOWN: Interface GigabitEthernet0/0/1, changed state to down`
     を重大度3のログとして正しく抽出
   の両方が検出されることを確認。`no shutdown`で復旧後は正常判定に戻る。

## 制約

- 現状は本エミュレーターの`cisco`デバイス（ISR4321相当、IOS-XE）専用。
  実機の旧IOS classic（2900/3900系等）は`show processes cpu`の出力形式が
  異なる場合があり、そのままでは動かない可能性がある
- CPUプロセス別内訳（`show processes cpu sorted`の個別プロセス行）は
  本エミュレーターが集計値のみ返すため未対応。実機接続時に拡張が必要

## テスト

```bash
pytest tests/test_cisco_router_triage.py -v
# 9/9 成功
```
