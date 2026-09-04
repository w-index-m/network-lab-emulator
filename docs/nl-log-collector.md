# 装置ログ採取 自然文操作（Qwen経由）

`tools/nl_log_collector.py`

## これは何か

「catalystのログ採って」のような自然文をQwen(Ollama)に解釈させ、
対象装置と採取プロファイルを決めて、実際に`/api/cli`経由でコマンドを
実行してログをファイルに保存するツール。`tools/nl_route_control.py`
と同じ設計思想を踏襲している。

## 設計上の安全策（重要）

**Qwenには実行するCLIコマンド文字列そのものを生成させない。**
Qwenが選べるのは「装置名」と、事前に定義した固定コマンド集合
（`PROFILES`）の「プロファイル名」だけで、実行コマンドはこちら側の
辞書から引く。

```
自然文「catalystのログ採って」
       │
       ▼
Qwen(Ollama) ── {"devices":["catalyst"], "profile":"tech-support"} を返すだけ
       │
       ▼
PROFILES["tech-support"] という固定リストからコマンドを引く
       │
       ▼
RoutingGenerator.cli() が /api/cli へPOST（既存の確定的な実装）
       │
       ▼
logs/catalyst_tech-support_<timestamp>.log に保存
```

自然文（外部入力）がそのままCLIコマンドとして実行される経路を
作らないための制約。プロファイルは全て `show` コマンドのみで構成
されており（テストで担保）、状態を変更するコマンドは混ざらない。

## プロファイル一覧

| プロファイル | 内容 |
|---|---|
| `tech-support` | version/running-config/ip route/STP/CDP/MACテーブル等の全般確認 |
| `interfaces` | インターフェース状態確認 |
| `routing` | OSPF/BGP/RIPのネイバー・経路確認 |
| `stp` | スパニングツリー/EtherChannel確認 |
| `vpc` | NexusのvPC状態確認 |
| `neighbors` | CDP/LLDPネイバー確認 |

## 使い方

```bash
python tools/nl_log_collector.py "catalystのログ採って"
python tools/nl_log_collector.py "catのSTPとインターフェースの状態を見たい"
python tools/nl_log_collector.py "nexusのvPCまわり確認して" --dry-run
```

`--dry-run` を付けるとQwenの解釈結果と実行予定コマンドだけ表示し、
実際の採取は行わない。

保存先は既定で `./logs/`（`--out-dir` で変更可）、ファイル名は
`<装置>_<プロファイル>_<タイムスタンプ>.log`。

## 動作確認（2026-09-04）

エミュレーター起動中に `execute()` を直接叩いて確認済み
（`interpret()`＝Qwen呼び出し部分はOllama非接続環境のためスキップ、
`RoutingGenerator.cli()`以降の実行パスをそのまま検証）。

```
🤖 Qwen解釈結果: devices=['catalyst'] profile=tech-support
   実行コマンド: ['show version', 'show running-config', ...]
  採取中: catalyst (10コマンド)...
  ✅ 保存: logs/catalyst_tech-support_20260904_051617.log
```

保存された `.log` ファイルには、実機比較で使ってきたのと同じ形式で
`show version` 以下10コマンドの出力がそのまま記録される。

## 今後の展望

まずはこのエミュレーター向け（`/api/cli`経由）で運用し、将来的に
実機（Catalyst等）へ`paramiko`または`telnetlib`で直接SSH/Telnet
接続する版に拡張する想定。その場合も「Qwenは選択肢を選ぶだけ、
実行コマンドは固定辞書から引く」という制約は維持する
（`tools/bigip_qkview_collector.py` のようにparamikoで接続する層を
`collect()` の実装だけ差し替える形になる見込み）。
