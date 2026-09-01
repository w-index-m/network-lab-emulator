default['monitoring_stack']['stack_dir'] = '/tmp/netlab-stack'
default['monitoring_stack']['repo_dir'] = '/home/user/network-lab-emulator'

default['monitoring_stack']['app_port'] = 8000
default['monitoring_stack']['exporter_port'] = 9877
default['monitoring_stack']['prom_port'] = 9090
default['monitoring_stack']['alertmanager_port'] = 9093
default['monitoring_stack']['grafana_port'] = 3000

default['monitoring_stack']['prom_version'] = '2.54.1'
default['monitoring_stack']['alertmanager_version'] = '0.27.0'
default['monitoring_stack']['grafana_release_url'] =
  'https://github.com/w-index-m/network-lab-emulator/releases/download/grafana/grafana.tar.gz'
