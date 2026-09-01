{#
  SaltStack版: tools/setup_monitoring_stack.sh (シェル) / ansible / chef /
  puppet と同じ手順のSalt State移植。cmd.run の unless でべき等性を確保する。
#}
{% set s = pillar.get('monitoring_stack', {}) %}
{% set stack_dir = s.get('stack_dir', '/tmp/netlab-stack') %}
{% set repo_dir = s.get('repo_dir', '/home/user/network-lab-emulator') %}
{% set app_port = s.get('app_port', 8000) %}
{% set exporter_port = s.get('exporter_port', 9877) %}
{% set prom_port = s.get('prom_port', 9090) %}
{% set alertmanager_port = s.get('alertmanager_port', 9093) %}
{% set grafana_port = s.get('grafana_port', 3000) %}
{% set prom_version = s.get('prom_version', '2.54.1') %}
{% set alertmanager_version = s.get('alertmanager_version', '0.27.0') %}
{% set grafana_release_url = s.get('grafana_release_url', 'https://github.com/w-index-m/network-lab-emulator/releases/download/grafana/grafana.tar.gz') %}
{% set prom_dir = stack_dir ~ '/prometheus-' ~ prom_version ~ '.linux-amd64' %}
{% set am_dir = stack_dir ~ '/alertmanager-' ~ alertmanager_version ~ '.linux-amd64' %}

stack_dir:
  file.directory:
    - name: {{ stack_dir }}
    - mode: '0755'
    - makedirs: True

# ── 1. app.py ────────────────────────────────────────
start-app:
  cmd.run:
    - name: >
        cd {{ repo_dir }} &&
        NETLAB_AUTH_DISABLE=1 nohup uvicorn app:app --host 0.0.0.0 --port {{ app_port }}
        > {{ stack_dir }}/app.log 2>&1 &
    - shell: /bin/bash
    - unless: curl -sf -o /dev/null http://localhost:{{ app_port }}/api/snmp/dashboard
    - require:
      - file: stack_dir

wait-app:
  cmd.run:
    - name: >
        for i in $(seq 1 15); do
          curl -sf -o /dev/null http://localhost:{{ app_port }}/api/snmp/dashboard && exit 0;
          sleep 2;
        done; exit 1
    - shell: /bin/bash
    - require:
      - cmd: start-app

# ── 2. prometheus_exporter.py ────────────────────────
start-exporter:
  cmd.run:
    - name: >
        cd {{ repo_dir }} &&
        nohup python3 tools/prometheus_exporter.py
        --emulator-url http://localhost:{{ app_port }} --port {{ exporter_port }} --interval 5
        > {{ stack_dir }}/exporter.log 2>&1 &
    - shell: /bin/bash
    - unless: curl -sf -o /dev/null http://localhost:{{ exporter_port }}/metrics
    - require:
      - cmd: wait-app

wait-exporter:
  cmd.run:
    - name: >
        for i in $(seq 1 15); do
          curl -sf -o /dev/null http://localhost:{{ exporter_port }}/metrics && exit 0;
          sleep 2;
        done; exit 1
    - shell: /bin/bash
    - require:
      - cmd: start-exporter

# ── 3. Prometheus ─────────────────────────────────────
download-prometheus:
  cmd.run:
    - name: >
        curl -sL https://github.com/prometheus/prometheus/releases/download/v{{ prom_version }}/prometheus-{{ prom_version }}.linux-amd64.tar.gz
        -o {{ stack_dir }}/prometheus.tar.gz
    - unless: test -x {{ prom_dir }}/prometheus
    - require:
      - file: stack_dir

extract-prometheus:
  cmd.run:
    - name: tar xzf {{ stack_dir }}/prometheus.tar.gz -C {{ stack_dir }}
    - unless: test -x {{ prom_dir }}/prometheus
    - require:
      - cmd: download-prometheus

alert-rules-yml:
  file.managed:
    - name: {{ stack_dir }}/alert_rules.yml
    - source: salt://monitoring_stack/files/alert_rules.yml.jinja
    - template: jinja
    - require:
      - file: stack_dir

prometheus-yml:
  file.managed:
    - name: {{ stack_dir }}/prometheus.yml
    - source: salt://monitoring_stack/files/prometheus.yml.jinja
    - template: jinja
    - context:
        exporter_port: {{ exporter_port }}
        alertmanager_port: {{ alertmanager_port }}
        stack_dir: {{ stack_dir }}
    - require:
      - file: stack_dir

start-prometheus:
  cmd.run:
    - name: >
        nohup {{ prom_dir }}/prometheus
        --config.file={{ stack_dir }}/prometheus.yml
        --storage.tsdb.path={{ stack_dir }}/prom-data
        --web.listen-address=0.0.0.0:{{ prom_port }}
        > {{ stack_dir }}/prometheus.log 2>&1 &
    - shell: /bin/bash
    - unless: curl -sf -o /dev/null http://localhost:{{ prom_port }}/-/healthy
    - require:
      - cmd: extract-prometheus
      - file: prometheus-yml
      - file: alert-rules-yml

wait-prometheus:
  cmd.run:
    - name: >
        for i in $(seq 1 15); do
          curl -sf -o /dev/null http://localhost:{{ prom_port }}/-/healthy && exit 0;
          sleep 2;
        done; exit 1
    - shell: /bin/bash
    - require:
      - cmd: start-prometheus

# ── 4. Alertmanager ───────────────────────────────────
download-alertmanager:
  cmd.run:
    - name: >
        curl -sL https://github.com/prometheus/alertmanager/releases/download/v{{ alertmanager_version }}/alertmanager-{{ alertmanager_version }}.linux-amd64.tar.gz
        -o {{ stack_dir }}/alertmanager.tar.gz
    - unless: test -x {{ am_dir }}/alertmanager
    - require:
      - file: stack_dir

extract-alertmanager:
  cmd.run:
    - name: tar xzf {{ stack_dir }}/alertmanager.tar.gz -C {{ stack_dir }}
    - unless: test -x {{ am_dir }}/alertmanager
    - require:
      - cmd: download-alertmanager

alertmanager-yml:
  file.managed:
    - name: {{ stack_dir }}/alertmanager.yml
    - source: salt://monitoring_stack/files/alertmanager.yml.jinja
    - template: jinja
    - require:
      - file: stack_dir

start-alertmanager:
  cmd.run:
    - name: >
        nohup {{ am_dir }}/alertmanager
        --config.file={{ stack_dir }}/alertmanager.yml
        --storage.path={{ stack_dir }}/alertmanager-data
        --web.listen-address=0.0.0.0:{{ alertmanager_port }}
        --cluster.listen-address=""
        > {{ stack_dir }}/alertmanager.log 2>&1 &
    - shell: /bin/bash
    - unless: curl -sf -o /dev/null http://localhost:{{ alertmanager_port }}/
    - require:
      - cmd: extract-alertmanager
      - file: alertmanager-yml

wait-alertmanager:
  cmd.run:
    - name: >
        for i in $(seq 1 15); do
          curl -sf -o /dev/null http://localhost:{{ alertmanager_port }}/ && exit 0;
          sleep 2;
        done; exit 1
    - shell: /bin/bash
    - require:
      - cmd: start-alertmanager

# ── 5. Grafana ─────────────────────────────────────────
download-grafana:
  cmd.run:
    - name: curl -sL {{ grafana_release_url }} -o {{ stack_dir }}/grafana.tar.gz
    - unless: bash -c 'ls {{ stack_dir }} | grep -q "^grafana-[0-9]"'
    - require:
      - file: stack_dir

extract-grafana:
  cmd.run:
    - name: tar xzf {{ stack_dir }}/grafana.tar.gz -C {{ stack_dir }}
    - unless: bash -c 'ls {{ stack_dir }} | grep -q "^grafana-[0-9]"'
    - require:
      - cmd: download-grafana

start-grafana:
  cmd.run:
    - name: >
        gh=$(ls -d {{ stack_dir }}/grafana-[0-9]* | head -1);
        nohup $gh/bin/grafana server --homepath=$gh --config=$gh/conf/defaults.ini
        cfg:default.server.http_port={{ grafana_port }}
        > {{ stack_dir }}/grafana.log 2>&1 &
    - shell: /bin/bash
    - unless: curl -sf -o /dev/null http://localhost:{{ grafana_port }}/api/health
    - require:
      - cmd: extract-grafana

wait-grafana:
  cmd.run:
    - name: >
        for i in $(seq 1 20); do
          curl -sf -o /dev/null http://localhost:{{ grafana_port }}/api/health && exit 0;
          sleep 3;
        done; exit 1
    - shell: /bin/bash
    - require:
      - cmd: start-grafana

register-grafana-datasource:
  cmd.run:
    - name: >
        curl -sf -X POST http://admin:admin@localhost:{{ grafana_port }}/api/datasources
        -H 'Content-Type: application/json'
        -d '{"name":"Prometheus","type":"prometheus","url":"http://localhost:{{ prom_port }}","access":"proxy","isDefault":true}'
    - success_retcodes: [0, 22]
    - require:
      - cmd: wait-grafana

# ── 6. FRR ─────────────────────────────────────────────
install-frr:
  cmd.run:
    - name: apt-get update -qq && apt-get install -y -qq frr frr-pythontools
    - unless: which vtysh
