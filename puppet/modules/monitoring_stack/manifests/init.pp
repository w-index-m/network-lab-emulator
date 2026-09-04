# Puppet版: tools/setup_monitoring_stack.sh (シェル版) / ansible / chef と
# 同じ手順のPuppet移植。exec の unless でべき等性を確保する。
class monitoring_stack (
  String $stack_dir              = '/tmp/netlab-stack',
  String $repo_dir                = '/home/user/network-lab-emulator',
  Integer $app_port               = 8000,
  Integer $exporter_port          = 9877,
  Integer $prom_port              = 9090,
  Integer $alertmanager_port      = 9093,
  Integer $grafana_port           = 3000,
  String $prom_version            = '2.54.1',
  String $alertmanager_version    = '0.27.0',
  String $grafana_release_url     = 'https://github.com/w-index-m/network-lab-emulator/releases/download/grafana/grafana.tar.gz',
) {

  file { $stack_dir:
    ensure => directory,
    mode   => '0755',
  }

  # ── 1. app.py ────────────────────────────────────
  exec { 'start-app':
    command => "/bin/bash -c 'cd ${repo_dir} && NETLAB_AUTH_DISABLE=1 nohup uvicorn app:app --host 0.0.0.0 --port ${app_port} > ${stack_dir}/app.log 2>&1 &'",
    unless  => "/usr/bin/curl -sf -o /dev/null http://localhost:${app_port}/api/snmp/dashboard",
    require => File[$stack_dir],
  }

  exec { 'wait-app':
    command => "/bin/bash -c 'for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:${app_port}/api/snmp/dashboard && exit 0; sleep 2; done; exit 1'",
    require => Exec['start-app'],
  }

  # ── 2. exporter ──────────────────────────────────
  exec { 'start-exporter':
    command => "/bin/bash -c 'cd ${repo_dir} && nohup python3 tools/prometheus_exporter.py --emulator-url http://localhost:${app_port} --port ${exporter_port} --interval 5 > ${stack_dir}/exporter.log 2>&1 &'",
    unless  => "/usr/bin/curl -sf -o /dev/null http://localhost:${exporter_port}/metrics",
    require => Exec['wait-app'],
  }

  exec { 'wait-exporter':
    command => "/bin/bash -c 'for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:${exporter_port}/metrics && exit 0; sleep 2; done; exit 1'",
    require => Exec['start-exporter'],
  }

  # ── 3. Prometheus ────────────────────────────────
  $prom_dir = "${stack_dir}/prometheus-${prom_version}.linux-amd64"

  exec { 'download-prometheus':
    command => "/usr/bin/curl -sL https://github.com/prometheus/prometheus/releases/download/v${prom_version}/prometheus-${prom_version}.linux-amd64.tar.gz -o ${stack_dir}/prometheus.tar.gz",
    creates => "${stack_dir}/prometheus.tar.gz",
    unless  => "/usr/bin/test -x ${prom_dir}/prometheus",
    require => File[$stack_dir],
  }

  exec { 'extract-prometheus':
    command => "/bin/tar xzf ${stack_dir}/prometheus.tar.gz -C ${stack_dir}",
    unless  => "/usr/bin/test -x ${prom_dir}/prometheus",
    require => Exec['download-prometheus'],
  }

  file { "${stack_dir}/alert_rules.yml":
    ensure  => file,
    content => template('monitoring_stack/alert_rules.yml.erb'),
    require => File[$stack_dir],
  }

  file { "${stack_dir}/prometheus.yml":
    ensure  => file,
    content => template('monitoring_stack/prometheus.yml.erb'),
    require => File[$stack_dir],
  }

  exec { 'start-prometheus':
    command => "/bin/bash -c 'nohup ${prom_dir}/prometheus --config.file=${stack_dir}/prometheus.yml --storage.tsdb.path=${stack_dir}/prom-data --web.listen-address=0.0.0.0:${prom_port} > ${stack_dir}/prometheus.log 2>&1 &'",
    unless  => "/usr/bin/curl -sf -o /dev/null http://localhost:${prom_port}/-/healthy",
    require => [Exec['extract-prometheus'], File["${stack_dir}/prometheus.yml"], File["${stack_dir}/alert_rules.yml"]],
  }

  exec { 'wait-prometheus':
    command => "/bin/bash -c 'for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:${prom_port}/-/healthy && exit 0; sleep 2; done; exit 1'",
    require => Exec['start-prometheus'],
  }

  # ── 4. Alertmanager ──────────────────────────────
  $am_dir = "${stack_dir}/alertmanager-${alertmanager_version}.linux-amd64"

  exec { 'download-alertmanager':
    command => "/usr/bin/curl -sL https://github.com/prometheus/alertmanager/releases/download/v${alertmanager_version}/alertmanager-${alertmanager_version}.linux-amd64.tar.gz -o ${stack_dir}/alertmanager.tar.gz",
    unless  => "/usr/bin/test -x ${am_dir}/alertmanager",
    require => File[$stack_dir],
  }

  exec { 'extract-alertmanager':
    command => "/bin/tar xzf ${stack_dir}/alertmanager.tar.gz -C ${stack_dir}",
    unless  => "/usr/bin/test -x ${am_dir}/alertmanager",
    require => Exec['download-alertmanager'],
  }

  file { "${stack_dir}/alertmanager.yml":
    ensure  => file,
    content => template('monitoring_stack/alertmanager.yml.erb'),
    require => File[$stack_dir],
  }

  exec { 'start-alertmanager':
    command => "/bin/bash -c 'nohup ${am_dir}/alertmanager --config.file=${stack_dir}/alertmanager.yml --storage.path=${stack_dir}/alertmanager-data --web.listen-address=0.0.0.0:${alertmanager_port} --cluster.listen-address=\"\" > ${stack_dir}/alertmanager.log 2>&1 &'",
    unless  => "/usr/bin/curl -sf -o /dev/null http://localhost:${alertmanager_port}/",
    require => [Exec['extract-alertmanager'], File["${stack_dir}/alertmanager.yml"]],
  }

  exec { 'wait-alertmanager':
    command => "/bin/bash -c 'for i in $(seq 1 15); do curl -sf -o /dev/null http://localhost:${alertmanager_port}/ && exit 0; sleep 2; done; exit 1'",
    require => Exec['start-alertmanager'],
  }

  # ── 5. Grafana ────────────────────────────────────
  exec { 'download-grafana':
    command => "/usr/bin/curl -sL ${grafana_release_url} -o ${stack_dir}/grafana.tar.gz",
    unless  => "/bin/bash -c 'ls ${stack_dir} | grep -q \"^grafana-[0-9]\"'",
    require => File[$stack_dir],
  }

  exec { 'extract-grafana':
    command => "/bin/tar xzf ${stack_dir}/grafana.tar.gz -C ${stack_dir}",
    unless  => "/bin/bash -c 'ls ${stack_dir} | grep -q \"^grafana-[0-9]\"'",
    require => Exec['download-grafana'],
  }

  exec { 'start-grafana':
    command => "/bin/bash -c 'gh=\$(ls -d ${stack_dir}/grafana-[0-9]* | head -1); nohup \$gh/bin/grafana server --homepath=\$gh --config=\$gh/conf/defaults.ini cfg:default.server.http_port=${grafana_port} > ${stack_dir}/grafana.log 2>&1 &'",
    unless  => "/usr/bin/curl -sf -o /dev/null http://localhost:${grafana_port}/api/health",
    require => Exec['extract-grafana'],
  }

  exec { 'wait-grafana':
    command => "/bin/bash -c 'for i in $(seq 1 20); do curl -sf -o /dev/null http://localhost:${grafana_port}/api/health && exit 0; sleep 3; done; exit 1'",
    require => Exec['start-grafana'],
  }

  exec { 'register-grafana-datasource':
    command => "/usr/bin/curl -sf -X POST http://admin:admin@localhost:${grafana_port}/api/datasources -H 'Content-Type: application/json' -d '{\"name\":\"Prometheus\",\"type\":\"prometheus\",\"url\":\"http://localhost:${prom_port}\",\"access\":\"proxy\",\"isDefault\":true}'",
    returns => [0, 22],
    require => Exec['wait-grafana'],
  }

  # ── 6. FRR ────────────────────────────────────────
  exec { 'install-frr':
    command => '/usr/bin/apt-get update -qq && /usr/bin/apt-get install -y -qq frr frr-pythontools',
    unless  => '/usr/bin/which vtysh',
  }
}
