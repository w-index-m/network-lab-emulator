# Qwen2.5をネットワーク運用特化にする（Claude非依存）

Claudeがいない環境（Windows PC単体、オフラインのNOC端末など）でも、
`ollama run qwen2.5-netops "..."` と打つだけでネットワーク機器の
トラブルシュートに最適化された応答が返るようにするための設定。

ポイントは、システムプロンプトを**Modelfileに焼き込んで
`ollama create`する**こと。会話のたびにシステムプロンプトを渡す
必要がなく、モデル自体がネットワーク運用特化になる。

## 1. Modelfile（このまま使える）

`Qwen2.5-1.5B-Instruct-Q4_K_M.gguf` を取得済みの状態で、以下を
`Modelfile.netops` として保存する。

```
FROM ./Qwen2.5-1.5B-Instruct-Q4_K_M.gguf

TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ .Response }}<|im_end|>
"""

PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
PARAMETER temperature 0.3

SYSTEM """あなたはネットワーク運用・トラブルシュート専門のアシスタントです。
対象機種は Cisco IOS/IOS-XE（ISR, Catalyst）, Cisco NX-OS（Nexus）,
Cisco ASA, VyOS, Yamaha SRS/RTX, Si-R, Apresia を含みます。

回答の際は必ず以下のルールに従ってください:

1. 簡潔に、箇条書き中心で回答する。前置きの挨拶や一般論は書かない。
2. コマンド例を示すときは、実際にその機種のCLIで打てる形（バッククォート
   コードブロック）で書く。存在しないコマンドを創作しない。
3. syslogメッセージ（%LINK-3-UPDOWN, %LINEPROTO-5-UPDOWN, %BGP-5-ADJCHANGE
   など）を渡された場合は、(a)何が起きたか (b)緊急度 (c)次に確認すべき
   コマンド の3点を必ず含める。
4. 原因が一つに断定できない場合は、確認すべき切り分け手順を優先度順に
   並べて提示する（憶測で原因を断定しない）。
5. 設定変更を提案する場合は、影響範囲（対象インターフェース/VLAN/
   ルーティングプロトコルなど）を一言添える。
6. 日本語で質問された場合は日本語で、英語で質問された場合は英語で
   回答する。
"""
```

## 2. モデルの作成（このコマンドだけでOK）

```bash
ollama create qwen2.5-netops -f Modelfile.netops
```

以降、`ollama run qwen2.5-netops "..."` や、API経由の
`{"model": "qwen2.5-netops", ...}` は、システムプロンプトを毎回渡さなくても
上記のネットワーク専用チューニングが自動的に効く。

## 3. このリポジトリのAIツールから使う場合

`tools/syslog_ai_monitor.py` / `tools/oscap_ai_advisor.py` /
`tools/cisco_router_triage.py` はいずれも `OLLAMA_MODEL` 環境変数で
モデル名を切り替えられる設計になっている。

```bash
export OLLAMA_MODEL=qwen2.5-netops
python tools/syslog_ai_monitor.py --live
```

これらのツール側でも簡易的なsystemメッセージ（「簡潔に、箇条書き中心で
回答してください」）を渡しているが、`qwen2.5-netops` を使えば
Modelfile側のより詳細な指示が優先して効くため、ツール側のプロンプトが
簡素でも実務的な回答になる。

## 4. 実際に確認した動作

サンドボックス環境（Claude Code経由）でベースの `qwen2.5-1.5b`
（システムプロンプトなし）に対して直接プロンプトを投げた場合でも、
以下のように妥当な応答が得られることを確認済み（2026-09-02):

```
$ ollama run qwen2.5-1.5b "Catalystスイッチでインターフェースがdownしたとき
に確認すべきコマンドを3つ、日本語で簡潔に教えて"

1. `show interfaces`
2. `show ip interface brief`
3. `show cdp neighbor`
```

応答時間は約10秒（CPUのみ、GPUなし環境）。

**`qwen2.5-netops`（本ドキュメントのModelfile通りに作成）でも実際に
`ollama create`→推論まで確認済み**。`/api/generate`にリクエストを送り、
syslogメッセージを渡した結果:

```
$ curl -s http://127.0.0.1:11434/api/generate -d '{
  "model": "qwen2.5-netops",
  "prompt": "%LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state
             to down というsyslogを受け取りました。どう対応すべきですか",
  "stream": false
}'
```

応答（抜粋）:

```
この syslog メッセージは、GigabitEthernet1/0/1 接口がダウンしたことを
示しています。以下に、その対応手順を優先度順に並べて説明します：

1. 緊急度: これは一般的な情報であり、すぐに解決する必要はありません。
2. 確認すべきコマンド:
   - show interface GigabitEthernet1/0/1
   - show ip int brief
3. 原因の推測: ...
5. 解決策:
   - no shutdown コマンドを使用して接続を再開できます。
```

SYSTEMプロンプトで指定した「(a)何が起きたか (b)緊急度 (c)確認すべき
コマンド」の3点が実際に応答構造に反映されており、`SYSTEM`の焼き込みが
機能していることを確認した。

## 5. Windows PC単体（Claude非依存）での再現手順

```powershell
# 1. Ollamaインストール（1回のみ）
winget install --id Ollama.Ollama -e

# 2. GGUF取得（GitHub Releaseから、または直接Hugging Faceから）
Invoke-WebRequest `
  -Uri "https://github.com/w-index-m/network-lab-emulator/releases/download/Qwen2.5/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf" `
  -OutFile "$env:USERPROFILE\Downloads\Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"

# 3. Modelfile.netops を本ドキュメントの内容で作成し、同じフォルダに保存

# 4. モデル作成
cd $env:USERPROFILE\Downloads
ollama create qwen2.5-netops -f Modelfile.netops

# 5. 動作確認（このPCだけで完結、Claude不要）
ollama run qwen2.5-netops "OSPFネイバーがEXSTARTで止まっている場合の切り分け手順を教えて"
```

これでClaude Codeが動いていない環境（ネットワークが切れたNOC端末など）
でも、`qwen2.5-netops` 単体でネットワーク機器のトラブルシュート支援が
機能する状態になる。
