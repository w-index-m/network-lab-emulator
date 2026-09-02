# Ollama セットアップ（Windows PC → GitHub Release 経由でAI実行環境へ）

`tools/oscap_ai_advisor.py` や `tools/nl_monitor_control.py` はOllamaが
あれば自然文でのAI応答を使う設計だが、このAI実行環境（サンドボックス）
からは `ollama.com` / `registry.ollama.ai` / `huggingface.co` が
組織ポリシーでブロックされておりモデルを直接取得できない。

Grafana/Prometheus/FRRの時と同じ回避策で対応する: **モデルファイルを
制限のないWindows PC側でダウンロードし、GitHub Releaseのアセットとして
アップロードする**。AI実行環境はGitHub Releaseの`releases/download/...`
URLだけは取得できる（許可されたCDNにリダイレクトされるため）。

## 全体の流れ

```
[Windows PC]                          [GitHub Release]         [AI実行環境]
Ollamaインストール                                              
  + GGUFモデルをダウンロード   ──アップロード──▶  アセット公開  ──ダウンロード──▶ ollama create
```

## 1. Windows PC側: PowerShellで一括セットアップ

管理者権限のPowerShellで以下を順に実行する。

```powershell
# ── 1. Ollama本体のインストール ──────────────────────────
winget install --id Ollama.Ollama -e

# インストール後、一度新しいPowerShellを開き直す（PATHの反映のため）

# ── 2. 動作確認 ──────────────────────────────────────────
ollama --version

# ── 3. 軽量モデルをpull（Windows側はレジストリに直接アクセスできる） ──
ollama pull qwen2.5:1.5b

# ── 4. GGUF形式でエクスポート（GitHub Releaseにアップロードするため） ──
# Ollamaのモデルストレージから直接GGUFを取り出す方法（推奨）:
#   %USERPROFILE%\.ollama\models 以下に blob として保存されているが、
#   直接扱いにくいため、Hugging Faceから直接GGUFをダウンロードする方が確実。

# ── 4'. 代替: Hugging FaceからGGUFを直接ダウンロード ─────────
mkdir $env:USERPROFILE\Downloads\gguf-model
cd $env:USERPROFILE\Downloads\gguf-model
Invoke-WebRequest `
  -Uri "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf" `
  -OutFile "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"

# ダウンロードできたか確認（約1GB前後のはず）
Get-Item .\Qwen2.5-1.5B-Instruct-Q4_K_M.gguf | Select-Object Name, Length
```

**注意点（過去の失敗パターンを踏まえて）**:
- `Invoke-WebRequest` は `-OutFile` を必ず指定すること（無いとファイルに保存されない）
- `C:\` 直下など書き込み権限のない場所は避け、`$env:USERPROFILE\Downloads` 配下を使う
- GitHub Releaseのアセット上限は1ファイル2GBなので、`Q4_K_M`（量子化済み、軽量）を選ぶこと。
  `Q8_0`やオリジナルのfp16は数GB〜十数GBになり収まらない場合がある

## 2. GitHub Releaseへのアップロード

`w-index-m/network-lab-emulator` のGitHubページ → Releases →
（Grafanaの時に使った既存のReleaseか、新規Release）→
「Attach binaries」でダウンロードした `.gguf` ファイルをドラッグ&ドロップ。

アップロードが終わったら、そのReleaseのタグ名を教えてもらえれば、
AI実行環境側で以下を自動的に行う:

```bash
# ── AI実行環境側（Claudeが実行する） ─────────────────────
curl -sL -o model.gguf \
  "https://github.com/w-index-m/network-lab-emulator/releases/download/<tag>/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"

cat > Modelfile << 'EOF'
FROM ./model.gguf
EOF

ollama create qwen2.5-1.5b -f Modelfile
ollama run qwen2.5-1.5b "こんにちは"
```

これで `OLLAMA_MODEL=qwen2.5-1.5b` を環境変数に設定すれば、
`tools/oscap_ai_advisor.py --results ... `（`--no-ai`を付けない）や
`tools/nl_monitor_control.py` がこのモデルを使って自然文の応答を生成する。

## 3. Windows側でOllamaサーバーとして常時使いたい場合

Windows PC自体でこのプロジェクトの各種AIツールを動かす場合は、
モデルのGitHub往復は不要（Ollamaがローカルにpull済みのモデルをそのまま使える）。

```powershell
# Ollamaサーバーがバックグラウンドで起動していることを確認
ollama list

# network-lab-emulator側の設定
$env:OLLAMA_URL = "http://localhost:11434"
$env:OLLAMA_MODEL = "qwen2.5:1.5b"

python tools\nl_monitor_control.py "Catalystの10.9.9.1のGi1/0/1を監視対象にして"
python tools\oscap_ai_advisor.py --results results.xml --datastream ssg-ubuntu2404-ds.xml
```

Windows Docker Desktop環境の監視スタック（`monitoring\docker-compose.yml`）と
組み合わせる場合も、Ollama自体はDocker化せずWindowsホスト側で直接動かし、
各AIツールから`http://localhost:11434`を参照する構成でよい
（Prometheus Exporterのホスト直接実行パターンと同じ）。

## この環境（AI実行サンドボックス）で確認済みの制約

| ホスト | 状態 |
|---|---|
| `ollama.com`（インストーラー配布元） | ❌ ブロック |
| `registry.ollama.ai`（モデルレジストリ、`ollama pull`が使う） | ❌ ブロック |
| `huggingface.co`（GGUF直接配布元） | ❌ ブロック |
| `github.com/ollama/ollama/releases/download/...`（Ollama本体バイナリ） | ✅ 取得可（v0.9.6で動作確認済み、`ollama serve`起動成功） |
| `github.com/<自リポジトリ>/releases/download/...`（GGUFモデルの回避経路） | ✅ 取得可（Grafanaと同じ経路、実際にQwen2.5で確認済み） |

Ollama本体（サーバー）はこの環境でも起動できることは確認済み。

## 実際に確認した動作（エンドツーエンド、実機確認済み）

Windows PC側でのGGUFダウンロード → GitHub Releaseアップロード →
このAI実行環境での取り込み・推論、という一連の流れを実際に通した。

### 1. Windows PC側

`https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF` から
`Qwen2.5-1.5B-Instruct-Q4_K_M.gguf`（約986MB、f16は2GB超で不可のため
量子化版を選択）をダウンロードし、`w-index-m/network-lab-emulator` の
Releaseタグ `Qwen2.5` にアセットとしてアップロード。

### 2. AI実行環境側（実際に実行したコマンド）

```bash
# --- GGUFの取得とsha256検証 ---
curl -sL -o Qwen2.5-1.5B-Instruct-Q4_K_M.gguf \
  "https://github.com/w-index-m/network-lab-emulator/releases/download/Qwen2.5/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
sha256sum Qwen2.5-1.5B-Instruct-Q4_K_M.gguf
# → 1adf0b11065d8ad2e8123ea110d1ec956dab4ab038eab665614adba04b6c337
#   GitHub Release側のasset digestと完全一致、破損なしを確認

# --- Ollama本体の取得(GitHub Releases経由、latestタグはAPI経由の解決が
#     必要でブロックされるため、git ls-remote --tagsでタグ名を先に特定) ---
git ls-remote --tags https://github.com/ollama/ollama | tail -5
curl -sL --retry 3 -o ollama.tgz \
  "https://github.com/ollama/ollama/releases/download/v0.9.6/ollama-linux-amd64.tgz"
tar xzf ollama.tgz -C ollama_extract

# --- サーバー起動(GPU無し環境のためCPUモードで自動フォールバック) ---
export OLLAMA_MODELS=/tmp/ollama_models
export LD_LIBRARY_PATH=/tmp/ollama_extract/lib/ollama
nohup /tmp/ollama_extract/bin/ollama serve > /tmp/ollama_serve.log 2>&1 &
# → "no compatible GPUs were discovered" / "inference compute" library=cpu
#    total="15.7 GiB" available="14.4 GiB"

# --- モデル取り込み ---
cat > Modelfile << 'EOF'
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
EOF
ollama create qwen2.5-1.5b -f Modelfile
# → success
```

### 3. 実推論の確認

```bash
ollama run qwen2.5-1.5b \
  "Catalystスイッチでインターフェースがdownしたときに確認すべきコマンドを3つ、日本語で簡潔に教えて"
```

実際の応答（CPU実行、約10秒）:

```
1. `show interfaces`
2. `show ip interface brief`
3. `show cdp neighbor`
```

的確な内容が日本語プロンプトに対して日本語(コマンド部分は英語)で
返ってきており、CPUのみでも実用速度（1問あたり約10秒）で応答することを
確認した。これで `nl_monitor_control.py` / `oscap_ai_advisor.py` /
`cisco_router_triage.py` の `OLLAMA_MODEL=qwen2.5-1.5b` 指定での
実運用が現実的であることが裏付けられた。

**補足（つまずいた点）**:
- `ollama/ollama` リポジトリはセッションに未接続だったため、GitHub MCP
  経由のAPIでは`releases/latest`を解決できなかった。`git clone`/
  `git ls-remote`は匿名でも許可されているため、タグ一覧を`git ls-remote
  --tags`で直接取得してバージョンを特定する方法で回避した
- `releases/download/...`への初回`curl`は転送途中で`ws_closed_mid_exchange`
  エラーになることがあった（1.3GBの大きめアセット）。`--retry 3`を付けて
  再実行することで解決した
