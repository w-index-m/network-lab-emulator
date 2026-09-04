# Windows 一括インストール（PowerShell）

network-lab-emulator と、監視・AIツール一式をWindows PCにセットアップする
ためのPowerShellコマンド集。手順の説明文ではなく、**実行するコマンドを
まとめて並べる**形式にしてある。管理者権限のPowerShellで上から順に実行する。

各セクションは独立しているので、必要な部分だけ実行してもよい。

## スクリプト1本でまとめて実行する場合

Prometheus/Alertmanager/Grafanaのダウンロード〜起動〜Grafanaへの
データソース登録までを1本にまとめた `tools/setup_monitoring_stack.ps1`
がある（Linux版 `tools/setup_monitoring_stack.sh` のPowerShell移植）。
べき等（既に起動しているサービスはスキップ）なので、何度実行しても安全。

```powershell
cd $env:USERPROFILE\Documents\network-lab-emulator

# ダウンロード・起動
.\tools\setup_monitoring_stack.ps1 -Action setup

# Ollamaも一緒にセットアップしたい場合
.\tools\setup_monitoring_stack.ps1 -Action setup -WithOllama

# 状態確認
.\tools\setup_monitoring_stack.ps1 -Action status

# 停止
.\tools\setup_monitoring_stack.ps1 -Action stop
```

以下のセクション2〜4は、このスクリプトが内部で行っている処理を
手動で個別に実行したい場合の参考用。

## 0. 前提

```powershell
# Python / Git が入っているか確認（無ければ winget で入れる）
python --version
git --version

# 無い場合:
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
```

## 1. network-lab-emulator 本体

```powershell
cd $env:USERPROFILE\Documents
git clone https://github.com/w-index-m/network-lab-emulator.git
cd network-lab-emulator

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 起動
python app.py
# → http://localhost:8000
```

## 2. Prometheus Exporter

```powershell
# 別のPowerShellウィンドウで（app.pyを起動したまま）
cd $env:USERPROFILE\Documents\network-lab-emulator
.\venv\Scripts\Activate.ps1
python tools\prometheus_exporter.py --emulator-url http://localhost:8000 --port 9877
# → http://localhost:9877/metrics
```

## 3. Prometheus / Alertmanager / Grafana（ネイティブバイナリ、Docker不要）

```powershell
$work = "$env:USERPROFILE\Documents\netlab-monitoring"
New-Item -ItemType Directory -Force -Path $work | Out-Null
cd $work

# ── Prometheus ──────────────────────────────────────────
Invoke-WebRequest `
  -Uri "https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.windows-amd64.zip" `
  -OutFile "prometheus.zip"
Expand-Archive -Path "prometheus.zip" -DestinationPath "." -Force

@"
global:
  scrape_interval: 5s
  evaluation_interval: 5s
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:9093']
rule_files:
  - alert_rules.yml
scrape_configs:
  - job_name: 'netlab-emulator'
    static_configs:
      - targets: ['localhost:9877']
"@ | Out-File -Encoding utf8 "prometheus-2.54.1.windows-amd64\prometheus.yml"

@"
groups:
  - name: netlab
    rules:
      - alert: InterfaceDown
        expr: netlab_interface_oper_status == 2
        for: 0s
        labels:
          severity: critical
        annotations:
          summary: "{{ `$labels.hostname }} interface {{ `$labels.interface }} is down"
"@ | Out-File -Encoding utf8 "prometheus-2.54.1.windows-amd64\alert_rules.yml"

# ── Alertmanager ─────────────────────────────────────────
Invoke-WebRequest `
  -Uri "https://github.com/prometheus/alertmanager/releases/download/v0.27.0/alertmanager-0.27.0.windows-amd64.zip" `
  -OutFile "alertmanager.zip"
Expand-Archive -Path "alertmanager.zip" -DestinationPath "." -Force

@"
route:
  receiver: 'default'
receivers:
  - name: 'default'
"@ | Out-File -Encoding utf8 "alertmanager-0.27.0.windows-amd64\alertmanager.yml"

# ── Grafana ──────────────────────────────────────────────
Invoke-WebRequest `
  -Uri "https://dl.grafana.com/oss/release/grafana-11.2.0.windows-amd64.zip" `
  -OutFile "grafana.zip"
Expand-Archive -Path "grafana.zip" -DestinationPath "." -Force
```

**すべて別々のPowerShellウィンドウで起動する**（フォアグラウンドで動かす前提。
バックグラウンド常駐にしたい場合は末尾の「タスクスケジューラ登録」参照）:

```powershell
# ウィンドウ1: Alertmanager
cd $work\alertmanager-0.27.0.windows-amd64
.\alertmanager.exe --config.file=alertmanager.yml

# ウィンドウ2: Prometheus
cd $work\prometheus-2.54.1.windows-amd64
.\prometheus.exe --config.file=prometheus.yml

# ウィンドウ3: Grafana
cd $work\grafana-11.2.0\bin
.\grafana-server.exe
```

- Grafana: http://localhost:3000 （admin/admin）
- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093

Grafanaのデータソース登録（PowerShellから一発で）:

```powershell
$body = @{
  name = "Prometheus"; type = "prometheus"; url = "http://localhost:9090"
  access = "proxy"; isDefault = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:3000/api/datasources" -Method Post `
  -Body $body -ContentType "application/json" `
  -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:admin")) }
```

## 4. Ollama + 軽量モデル

```powershell
winget install --id Ollama.Ollama -e
# 新しいPowerShellを開き直してからPATHを反映
ollama --version
ollama pull qwen2.5:1.5b

# 動作確認
ollama run qwen2.5:1.5b "こんにちは"
```

network-lab-emulatorのAIツールから使う場合:

```powershell
$env:OLLAMA_URL = "http://localhost:11434"
$env:OLLAMA_MODEL = "qwen2.5:1.5b"

cd $env:USERPROFILE\Documents\network-lab-emulator
.\venv\Scripts\Activate.ps1
python tools\nl_monitor_control.py "Catalystの10.9.9.1のGi1/0/1を監視対象にして"
```

## 5. IaCツール（Ansible/Chef/Puppet/Salt を Windows で使いたい場合）

Windowsネイティブでは制約が大きいため、**WSL2内での実行を推奨**する
（Ansible/Chef/Puppet/SaltStackはいずれもLinux向けツールで、Windows単体
では管理対象としての利用が中心になり、制御ホストとしての機能が弱い）。

```powershell
# WSL2 + Ubuntuのインストール
wsl --install -d Ubuntu-24.04

# WSL2内に入ってから（このコマンドはWSLのbashで実行）
wsl
```

WSL2に入った後は、このリポジトリの`ansible/`・`chef/`・`puppet/`・`salt/`
ディレクトリを、このAI実行環境（Linuxサンドボックス）で検証したのと
同じ手順でそのまま実行できる（Ubuntu 24.04なのでOSも一致する）。

```bash
# WSL2内(bash)
sudo apt-get update
sudo apt-get install -y ansible

cd /mnt/c/Users/<ユーザー名>/Documents/network-lab-emulator
ansible-playbook -i ansible/inventory.ini ansible/site.yml \
  -e repo_dir=$(pwd) -e stack_dir=/tmp/netlab-stack
```

## 6. OpenSCAP（セキュリティベースライン検証）

OpenSCAPもLinux向けツールのため、WSL2内での実行を推奨する。

```bash
# WSL2内(bash)
sudo apt-get install -y openscap-scanner openscap-utils

curl -sL -o scap-content.zip \
  "https://github.com/ComplianceAsCode/content/releases/download/v0.1.82/scap-security-guide-0.1.82.zip"
unzip -q scap-content.zip -d scap-content

oscap xccdf eval \
  --profile xccdf_org.ssgproject.content_profile_cis_level1_server \
  --results results.xml --report report.html \
  scap-content/scap-security-guide-0.1.82/ssg-ubuntu2404-ds.xml

python3 tools/oscap_ai_advisor.py --no-ai \
  --results results.xml \
  --datastream scap-content/scap-security-guide-0.1.82/ssg-ubuntu2404-ds.xml \
  --fix-script scap-content/scap-security-guide-0.1.82/bash/ubuntu2404-script-cis_level1_server.sh
```

## 7. 起動をまとめてやりたい場合（PowerShellスクリプト化）

上記のPrometheus/Alertmanager/Grafanaの起動を1つのスクリプトにまとめる例:

```powershell
# start-monitoring-stack.ps1 として保存して実行
$work = "$env:USERPROFILE\Documents\netlab-monitoring"

Start-Process powershell -ArgumentList `
  "-NoExit", "-Command", "cd '$work\alertmanager-0.27.0.windows-amd64'; .\alertmanager.exe --config.file=alertmanager.yml"

Start-Sleep -Seconds 2

Start-Process powershell -ArgumentList `
  "-NoExit", "-Command", "cd '$work\prometheus-2.54.1.windows-amd64'; .\prometheus.exe --config.file=prometheus.yml"

Start-Process powershell -ArgumentList `
  "-NoExit", "-Command", "cd '$work\grafana-11.2.0\bin'; .\grafana-server.exe"

Write-Host "起動しました。Grafana: http://localhost:3000  Prometheus: http://localhost:9090  Alertmanager: http://localhost:9093"
```

## 参考: 各コンポーネントの詳細ドキュメント

- Prometheus/Grafana全般: `docs/prometheus-grafana-windows.md`
- Ollama/GGUFモデル取得の詳細（レジストリブロック時の回避策含む）: `docs/ollama-windows-setup.md`
- 自然言語での監視対象追加: `docs/nl-monitor-control.md`
- OpenSCAPアドバイザー: `docs/oscap-ai-advisor.md`
- 監視スタック全体の構成: `docs/monitoring-stack-guide.md`
