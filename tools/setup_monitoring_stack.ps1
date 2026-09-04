<#
.SYNOPSIS
  network-lab-emulator の監視スタック(Prometheus/Alertmanager/Grafana/Ollama)を
  Windows上でまとめてセットアップ・起動する。

.DESCRIPTION
  tools/setup_monitoring_stack.sh (Linux版) の PowerShell移植版。
  ダウンロード済みのバイナリ・既に起動しているサービスはスキップするため、
  何度実行しても安全（べき等）。

.PARAMETER Action
  setup  : ダウンロード・起動（既定）
  status : 各サービスのヘルスチェック
  stop   : 起動したプロセスを停止

.EXAMPLE
  .\tools\setup_monitoring_stack.ps1 -Action setup
  .\tools\setup_monitoring_stack.ps1 -Action status
  .\tools\setup_monitoring_stack.ps1 -Action stop
#>

param(
    [ValidateSet('setup', 'status', 'stop')]
    [string]$Action = 'setup',

    [string]$StackDir = "$env:USERPROFILE\Documents\netlab-monitoring",
    [string]$RepoDir  = (Resolve-Path "$PSScriptRoot\..").Path,

    [int]$AppPort = 8000,
    [int]$ExporterPort = 9877,
    [int]$PromPort = 9090,
    [int]$AlertmanagerPort = 9093,
    [int]$GrafanaPort = 3000,

    [string]$PromVersion = '2.54.1',
    [string]$AlertmanagerVersion = '0.27.0',
    [string]$GrafanaVersion = '11.2.0',

    [switch]$WithOllama,
    [string]$OllamaModel = 'qwen2.5:1.5b'
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $StackDir | Out-Null

function Write-Log($msg) {
    Write-Host "[setup_monitoring_stack] $msg"
}

function Test-Port($port, $path = '/') {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port$path" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

# ── 1. network-lab-emulator 本体 ─────────────────────────
function Start-App {
    if (Test-Port $AppPort '/api/snmp/dashboard') {
        Write-Log "app.py: already running on :$AppPort"
        return
    }
    Write-Log "app.py: starting on :$AppPort"
    $venvPython = Join-Path $RepoDir 'venv\Scripts\python.exe'
    $python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
    Start-Process -FilePath $python -ArgumentList 'app.py' -WorkingDirectory $RepoDir `
        -RedirectStandardOutput "$StackDir\app.log" -RedirectStandardError "$StackDir\app.err.log" `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port $AppPort '/api/snmp/dashboard') { return }
    }
    Write-Warning "app.py が起動しませんでした。$StackDir\app.err.log を確認してください。"
}

# ── 2. Prometheus Exporter ────────────────────────────────
function Start-Exporter {
    if (Test-Port $ExporterPort '/metrics') {
        Write-Log "prometheus_exporter: already running on :$ExporterPort"
        return
    }
    Write-Log "prometheus_exporter: starting on :$ExporterPort"
    $venvPython = Join-Path $RepoDir 'venv\Scripts\python.exe'
    $python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
    Start-Process -FilePath $python `
        -ArgumentList "tools\prometheus_exporter.py --emulator-url http://localhost:$AppPort --port $ExporterPort --interval 5" `
        -WorkingDirectory $RepoDir `
        -RedirectStandardOutput "$StackDir\exporter.log" -RedirectStandardError "$StackDir\exporter.err.log" `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port $ExporterPort '/metrics') { return }
    }
    Write-Warning "exporter が起動しませんでした。$StackDir\exporter.err.log を確認してください。"
}

# ── 3. Prometheus ─────────────────────────────────────────
function Get-Prometheus {
    $dir = "$StackDir\prometheus-$PromVersion.windows-amd64"
    if (Test-Path "$dir\prometheus.exe") {
        Write-Log "prometheus: binary already present"
        return $dir
    }
    Write-Log "prometheus: downloading v$PromVersion"
    $zip = "$StackDir\prometheus.zip"
    Invoke-WebRequest -Uri "https://github.com/prometheus/prometheus/releases/download/v$PromVersion/prometheus-$PromVersion.windows-amd64.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $StackDir -Force
    return $dir
}

function Write-PrometheusConfig($dir) {
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
      - alert: HighCpu
        expr: netlab_cpu_percent >= 80
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: "{{ `$labels.hostname }} CPU {{ `$value }}% is high"
"@ | Out-File -Encoding utf8 "$dir\alert_rules.yml"

    @"
global:
  scrape_interval: 5s
  evaluation_interval: 5s
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:$AlertmanagerPort']
rule_files:
  - alert_rules.yml
scrape_configs:
  - job_name: 'netlab-emulator'
    static_configs:
      - targets: ['localhost:$ExporterPort']
"@ | Out-File -Encoding utf8 "$dir\prometheus.yml"
}

function Start-Prometheus {
    if (Test-Port $PromPort '/-/healthy') {
        Write-Log "prometheus: already running on :$PromPort"
        return
    }
    $dir = Get-Prometheus
    Write-PrometheusConfig $dir
    Write-Log "prometheus: starting on :$PromPort"
    Start-Process -FilePath "$dir\prometheus.exe" `
        -ArgumentList "--config.file=prometheus.yml --storage.tsdb.path=data --web.listen-address=0.0.0.0:$PromPort" `
        -WorkingDirectory $dir `
        -RedirectStandardOutput "$StackDir\prometheus.log" -RedirectStandardError "$StackDir\prometheus.err.log" `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port $PromPort '/-/healthy') { return }
    }
    Write-Warning "prometheus が起動しませんでした。$StackDir\prometheus.err.log を確認してください。"
}

# ── 4. Alertmanager ───────────────────────────────────────
function Get-Alertmanager {
    $dir = "$StackDir\alertmanager-$AlertmanagerVersion.windows-amd64"
    if (Test-Path "$dir\alertmanager.exe") {
        Write-Log "alertmanager: binary already present"
        return $dir
    }
    Write-Log "alertmanager: downloading v$AlertmanagerVersion"
    $zip = "$StackDir\alertmanager.zip"
    Invoke-WebRequest -Uri "https://github.com/prometheus/alertmanager/releases/download/v$AlertmanagerVersion/alertmanager-$AlertmanagerVersion.windows-amd64.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $StackDir -Force
    return $dir
}

function Start-Alertmanager {
    if (Test-Port $AlertmanagerPort '/') {
        Write-Log "alertmanager: already running on :$AlertmanagerPort"
        return
    }
    $dir = Get-Alertmanager
    @"
route:
  receiver: 'default'
receivers:
  - name: 'default'
"@ | Out-File -Encoding utf8 "$dir\alertmanager.yml"

    Write-Log "alertmanager: starting on :$AlertmanagerPort"
    Start-Process -FilePath "$dir\alertmanager.exe" `
        -ArgumentList "--config.file=alertmanager.yml --storage.path=data --web.listen-address=0.0.0.0:$AlertmanagerPort" `
        -WorkingDirectory $dir `
        -RedirectStandardOutput "$StackDir\alertmanager.log" -RedirectStandardError "$StackDir\alertmanager.err.log" `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Port $AlertmanagerPort '/') { return }
    }
    Write-Warning "alertmanager が起動しませんでした。$StackDir\alertmanager.err.log を確認してください。"
}

# ── 5. Grafana ─────────────────────────────────────────────
function Get-Grafana {
    $existing = Get-ChildItem -Path $StackDir -Directory -Filter "grafana-*" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($existing) {
        Write-Log "grafana: already extracted"
        return $existing.FullName
    }
    Write-Log "grafana: downloading v$GrafanaVersion"
    $zip = "$StackDir\grafana.zip"
    Invoke-WebRequest -Uri "https://dl.grafana.com/oss/release/grafana-$GrafanaVersion.windows-amd64.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $StackDir -Force
    return (Get-ChildItem -Path $StackDir -Directory -Filter "grafana-*" | Select-Object -First 1).FullName
}

function Start-Grafana {
    if (Test-Port $GrafanaPort '/api/health') {
        Write-Log "grafana: already running on :$GrafanaPort"
        return
    }
    $dir = Get-Grafana
    Write-Log "grafana: starting on :$GrafanaPort"
    Start-Process -FilePath "$dir\bin\grafana-server.exe" `
        -ArgumentList "--homepath=`"$dir`"" `
        -WorkingDirectory "$dir\bin" `
        -RedirectStandardOutput "$StackDir\grafana.log" -RedirectStandardError "$StackDir\grafana.err.log" `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Port $GrafanaPort '/api/health') { break }
    }
    if (-not (Test-Port $GrafanaPort '/api/health')) {
        Write-Warning "grafana が起動しませんでした。$StackDir\grafana.err.log を確認してください。"
        return
    }

    Write-Log "grafana: registering Prometheus datasource"
    $body = @{
        name = "Prometheus"; type = "prometheus"; url = "http://localhost:$PromPort"
        access = "proxy"; isDefault = $true
    } | ConvertTo-Json
    $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:admin"))
    try {
        Invoke-RestMethod -Uri "http://localhost:$GrafanaPort/api/datasources" -Method Post `
            -Body $body -ContentType "application/json" `
            -Headers @{ Authorization = "Basic $auth" } | Out-Null
    } catch {
        # 既に登録済みの場合は409になるので無視してよい
    }
}

# ── 6. Ollama（任意） ─────────────────────────────────────
function Start-OllamaIfRequested {
    if (-not $WithOllama) { return }

    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Log "ollama: インストールします (winget)"
        winget install --id Ollama.Ollama -e
        Write-Warning "Ollamaを新規インストールしました。PATHを反映するため新しいPowerShellでこのスクリプトを再実行してください。"
        return
    }

    if (-not (Test-Port 11434 '/')) {
        Write-Log "ollama: サーバーを起動します"
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 3
    } else {
        Write-Log "ollama: already running"
    }

    $models = & ollama list 2>$null
    if ($models -notmatch [regex]::Escape($OllamaModel)) {
        Write-Log "ollama: モデル $OllamaModel をpullします"
        & ollama pull $OllamaModel
    } else {
        Write-Log "ollama: model $OllamaModel already pulled"
    }
}

# ── コマンド ────────────────────────────────────────────────
function Invoke-Setup {
    Start-App
    Start-Exporter
    Start-Prometheus
    Start-Alertmanager
    Start-Grafana
    Start-OllamaIfRequested
    Write-Host ""
    Invoke-Status
}

function Invoke-Status {
    Write-Log "── ヘルスチェック ──"
    $checks = @(
        @{ Name = 'app';          Port = $AppPort;          Path = '/api/snmp/dashboard' }
        @{ Name = 'exporter';     Port = $ExporterPort;     Path = '/metrics' }
        @{ Name = 'prometheus';   Port = $PromPort;         Path = '/-/healthy' }
        @{ Name = 'alertmanager'; Port = $AlertmanagerPort; Path = '/' }
        @{ Name = 'grafana';      Port = $GrafanaPort;      Path = '/api/health' }
    )
    foreach ($c in $checks) {
        $ok = Test-Port $c.Port $c.Path
        $status = if ($ok) { '200' } else { 'down' }
        Write-Host ("{0,-14} :{1,-6} {2}" -f $c.Name, $c.Port, $status)
    }
    if ($WithOllama) {
        $ollamaOk = Test-Port 11434 '/'
        Write-Host ("{0,-14} :{1,-6} {2}" -f 'ollama', 11434, $(if ($ollamaOk) { '200' } else { 'down' }))
    }
}

function Invoke-Stop {
    Write-Log "stopping app.py / exporter / prometheus / alertmanager / grafana"
    Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -match 'app\.py|prometheus_exporter\.py'
    } | Stop-Process -Force -ErrorAction SilentlyContinue

    foreach ($name in @('prometheus', 'alertmanager', 'grafana-server')) {
        Get-Process $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

switch ($Action) {
    'setup'  { Invoke-Setup }
    'status' { Invoke-Status }
    'stop'   { Invoke-Stop }
}
