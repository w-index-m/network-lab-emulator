node default {
  class { 'monitoring_stack':
    stack_dir => '/tmp/netlab-stack',
    repo_dir  => '/home/user/network-lab-emulator',
  }
}
