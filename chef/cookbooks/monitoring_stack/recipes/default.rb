#
# Chef版: tools/setup_monitoring_stack.sh (シェル版) / ansible/roles/monitoring_stack
# (Ansible版) と同じ手順のChef移植。べき等: execute リソースは not_if で
# 既にサービスが起動しているかを判定してスキップする。
#
s = node['monitoring_stack']
stack_dir = s['stack_dir']
repo_dir  = s['repo_dir']

directory stack_dir do
  recursive true
  mode '0755'
end

# ── 1. app.py ──────────────────────────────────────
execute 'start network-lab-emulator' do
  command "NETLAB_AUTH_DISABLE=1 nohup uvicorn app:app --host 0.0.0.0 --port #{s['app_port']} > #{stack_dir}/app.log 2>&1 &"
  cwd repo_dir
  user 'root'
  not_if "curl -sf -o /dev/null http://localhost:#{s['app_port']}/api/snmp/dashboard"
end

execute 'wait for app' do
  command "for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:#{s['app_port']}/api/snmp/dashboard && exit 0; sleep 2; done; exit 1"
end

# ── 2. prometheus_exporter.py ──────────────────────
execute 'start prometheus_exporter' do
  command "nohup python3 tools/prometheus_exporter.py --emulator-url http://localhost:#{s['app_port']} --port #{s['exporter_port']} --interval 5 > #{stack_dir}/exporter.log 2>&1 &"
  cwd repo_dir
  not_if "curl -sf -o /dev/null http://localhost:#{s['exporter_port']}/metrics"
end

execute 'wait for exporter' do
  command "for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:#{s['exporter_port']}/metrics && exit 0; sleep 2; done; exit 1"
end

# ── 3. Prometheus ───────────────────────────────────
prom_dir = "#{stack_dir}/prometheus-#{s['prom_version']}.linux-amd64"

remote_file "#{stack_dir}/prometheus.tar.gz" do
  source "https://github.com/prometheus/prometheus/releases/download/v#{s['prom_version']}/prometheus-#{s['prom_version']}.linux-amd64.tar.gz"
  not_if { ::File.exist?("#{prom_dir}/prometheus") }
end

execute 'extract prometheus' do
  command "tar xzf #{stack_dir}/prometheus.tar.gz -C #{stack_dir}"
  not_if { ::File.exist?("#{prom_dir}/prometheus") }
end

template "#{stack_dir}/alert_rules.yml" do
  source 'alert_rules.yml.erb'
  variables(exporter_port: s['exporter_port'])
end

template "#{stack_dir}/prometheus.yml" do
  source 'prometheus.yml.erb'
  variables(
    exporter_port: s['exporter_port'],
    alertmanager_port: s['alertmanager_port'],
    stack_dir: stack_dir
  )
end

execute 'start prometheus' do
  command "nohup #{prom_dir}/prometheus --config.file=#{stack_dir}/prometheus.yml --storage.tsdb.path=#{stack_dir}/prom-data --web.listen-address=0.0.0.0:#{s['prom_port']} > #{stack_dir}/prometheus.log 2>&1 &"
  not_if "curl -sf -o /dev/null http://localhost:#{s['prom_port']}/-/healthy"
end

execute 'wait for prometheus' do
  command "for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:#{s['prom_port']}/-/healthy && exit 0; sleep 2; done; exit 1"
end

# ── 4. Alertmanager ─────────────────────────────────
am_dir = "#{stack_dir}/alertmanager-#{s['alertmanager_version']}.linux-amd64"

remote_file "#{stack_dir}/alertmanager.tar.gz" do
  source "https://github.com/prometheus/alertmanager/releases/download/v#{s['alertmanager_version']}/alertmanager-#{s['alertmanager_version']}.linux-amd64.tar.gz"
  not_if { ::File.exist?("#{am_dir}/alertmanager") }
end

execute 'extract alertmanager' do
  command "tar xzf #{stack_dir}/alertmanager.tar.gz -C #{stack_dir}"
  not_if { ::File.exist?("#{am_dir}/alertmanager") }
end

template "#{stack_dir}/alertmanager.yml" do
  source 'alertmanager.yml.erb'
end

execute 'start alertmanager' do
  command "nohup #{am_dir}/alertmanager --config.file=#{stack_dir}/alertmanager.yml --storage.path=#{stack_dir}/alertmanager-data --web.listen-address=0.0.0.0:#{s['alertmanager_port']} --cluster.listen-address=\"\" > #{stack_dir}/alertmanager.log 2>&1 &"
  not_if "curl -sf -o /dev/null http://localhost:#{s['alertmanager_port']}/"
end

execute 'wait for alertmanager' do
  command "for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:#{s['alertmanager_port']}/ && exit 0; sleep 2; done; exit 1"
end

# ── 5. Grafana ──────────────────────────────────────
remote_file "#{stack_dir}/grafana.tar.gz" do
  source s['grafana_release_url']
  not_if "ls #{stack_dir} | grep -q '^grafana-[0-9]'"
end

execute 'extract grafana' do
  command "tar xzf #{stack_dir}/grafana.tar.gz -C #{stack_dir}"
  not_if "ls #{stack_dir} | grep -q '^grafana-[0-9]'"
end

ruby_block 'set grafana_home' do
  block do
    dir = Dir.glob("#{stack_dir}/grafana-[0-9]*").first
    node.run_state['grafana_home'] = dir
  end
end

execute 'start grafana' do
  command lazy {
    gh = node.run_state['grafana_home']
    "nohup #{gh}/bin/grafana server --homepath=#{gh} --config=#{gh}/conf/defaults.ini cfg:default.server.http_port=#{s['grafana_port']} > #{stack_dir}/grafana.log 2>&1 &"
  }
  not_if "curl -sf -o /dev/null http://localhost:#{s['grafana_port']}/api/health"
end

execute 'wait for grafana' do
  command "for i in $(seq 1 20); do curl -sf -o /dev/null http://localhost:#{s['grafana_port']}/api/health && exit 0; sleep 3; done; exit 1"
end

execute 'register grafana datasource' do
  command "curl -sf -X POST http://admin:admin@localhost:#{s['grafana_port']}/api/datasources -H 'Content-Type: application/json' -d '{\"name\":\"Prometheus\",\"type\":\"prometheus\",\"url\":\"http://localhost:#{s['prom_port']}\",\"access\":\"proxy\",\"isDefault\":true}'"
  returns [0, 22] # 22 = curl saw 4xx/5xx (e.g. already exists -> 409)
end

# ── 6. FRR ───────────────────────────────────────────
execute 'install frr' do
  command 'apt-get update -qq && apt-get install -y -qq frr frr-pythontools'
  not_if 'which vtysh'
end
