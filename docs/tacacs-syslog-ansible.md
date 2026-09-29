# TACACS+(tac_plus-ng)→syslog→Loki連携のAnsible化

`ansible/roles/tacacs_syslog/`

## これは何か

`docs/tacacs-plus-setup.md`で手動検証したtac_plus-ng→syslog→Loki連携
（tac_plus-ngのauthorization/accountingログを実syslog UDPとして
`tools/syslog_to_loki.py`ブリッジへ送り、Grafana Lokiに溜める構成）を、
既存の`ansible/roles/monitoring_stack/`（Prometheus/Alertmanager/
Grafana/FRRのIaC化）と同じパターンでAnsible化したもの。

ユーザーから「tac_plusからsyslog連携などをIaCで組める状況でしょうか」
と提案を受け、まず対象をAnsibleに絞って実装した
（Chef/Puppet/Saltは既存の`monitoring_stack`同様、将来必要になれば
同じ構成で追加できる）。

## 構成

`ansible/site.yml`が`monitoring_stack`ロールの後に`tacacs_syslog`
ロールを実行する（tac_plus-ngのログ送信先がmonitoring_stackの
syslogブリッジのため、順序に依存する）。

```
ansible/roles/tacacs_syslog/
├── defaults/main.yml       # stack_dir, tacacs_port, key, user等
├── templates/
│   └── tac_plus-ng.cfg.j2  # docs/tacacs-plus-setup.mdの設定をテンプレ化
└── tasks/main.yml
```

`tasks/main.yml`の流れ（`monitoring_stack`の各ロールと同じ
べき等パターン: 既に動いていれば何もしない）:

1. ビルド依存パッケージ(`libpcre2-dev`/`libc-ares-dev`/`libssl-dev`等)を
   `dpkg -s`で確認してから未導入時のみ`apt-get install`
2. `event-driven-servers`(tac_plus-ng)をソースが無ければ`git clone`
3. ビルド済みバイナリを`find`で検索、無ければ`./configure && make`
4. `templates/tac_plus-ng.cfg.j2`から設定を生成
   （`destination = 127.0.0.1:{{ syslog_port }}`が
   `syslog_to_loki.py`のポートに一致するようテンプレ変数で結線）
5. ポート4949が未リッスンの場合のみ`nohup`でtac_plus-ng起動、
   `wait_for`で起動確認
6. `tools/nexus_cmd_to_tacacs.py`が使う`tacacs_plus`Pythonクライアントを
   未導入時のみ`pip install`

## 見つけて直した実バグ

1. **`ansible.builtin.apt`が`python3-apt`をこの環境のPython
   インタプリタ(3.11)向けにimportできずMODULE FAILURE** —
   `dpkg -s`での存在チェック＋`apt-get install`をshellするだけの
   `command`タスクに置き換えて回避した（`python3-apt`自体は
   システムには入っているが、Ansibleが使うインタプリタ向けの
   `apt_pkg`拡張がビルドされていなかった）。
2. **`find`で`build/`配下の`tac_plus-ng`を検索すると2つ見つかる** —
   ビルド直後の`build/<platform>/tac_plus-ng/tac_plus-ng`
   （共有ライブラリが隣に無い）と、
   `build/<platform>/fakeroot/usr/local/sbin/tac_plus-ng`
   （`../lib`に`libmavis.so`一式がある、実際に動かすべき方）の
   2種類が同名で存在する。最初は無条件に`files[0]`を使っていたため
   非fakerootの方が選ばれ、`error while loading shared libraries:
   libmavis.so`で起動失敗した。`selectattr('path', 'search',
   '/fakeroot/')`で絞り込んで解決。

## 実際に動かして確認した結果

すでに手動で構築済みのtac_plus-ng(`docs/tacacs-plus-setup.md`)を
一旦停止し、Ansibleロールから実際にゼロから起動して検証した。

```bash
$ ansible-playbook -i ansible/inventory.ini ansible/site.yml \
    --tags tacacs_syslog -e repo_dir=/home/user/network-lab-emulator
...
TASK [tacacs_syslog : Start tac_plus-ng] ***
changed: [localhost]
TASK [tacacs_syslog : Wait for tac_plus-ng to start listening] ***
ok: [localhost]
...
PLAY RECAP ***
localhost : ok=14  changed=1  unreachable=0  failed=0  skipped=7

$ ss -ltn | grep 4949
LISTEN 0  128  0.0.0.0:4949  0.0.0.0:*
```

実際にTACACS+ワイヤプロトコルで認可/アカウンティングを送信:

```bash
$ python tools/nexus_cmd_to_tacacs.py --tacacs-host 127.0.0.1 \
    --tacacs-key demo --device nexus --user demo --command "show version"
🔍 TACACS+へ認可(authorize)リクエスト送信...
   -> PASS (valid=True)
📝 TACACS+へアカウンティング(start/stop)送信...
   -> 送信完了
```

Lokiに実際に届いたことを確認:

```
Sep 29 06:03:22 vm tacplus[2765]: AUTHZ-PASS|127.0.0.1|demo|python_tty0|nexus|admin|permit|shell|show version <cr>
Sep 29 06:03:22 vm tacplus[2765]: ACCT-START|127.0.0.1|demo|python_tty0|nexus|start|shell|show version <cr>
Sep 29 06:03:22 vm tacplus[2765]: ACCT-STOP|127.0.0.1|demo|python_tty0|nexus|stop|shell|show version <cr>
```

べき等性も確認: 直後にもう一度同じplaybookを実行すると全タスクが
`changed=0`でスキップされ、tac_plus-ngは再起動されない
（`Start tac_plus-ng`が`skipping`）。

## 使い方

```bash
# monitoring_stack(Loki+syslogブリッジ) + tacacs_syslogを一括構築
ansible-playbook -i ansible/inventory.ini ansible/site.yml

# tacacs_syslogだけ実行(monitoring_stackが既に動いている前提)
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags tacacs_syslog

# 変数の上書き例(ポート/認証情報を変える)
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags tacacs_syslog \
  -e tacacs_key=mysecret -e tacacs_user=admin -e tacacs_password=mysecret
```

## 制約・今後の拡張余地

- `docs/tacacs-plus-setup.md`と同じく、network-lab-emulator本体は
  TACACS+クライアント機能を持たないため、この連携は
  「装置投入コマンドを`tools/nexus_cmd_to_tacacs.py`で拾って転送する」
  形のまま
- Chef/Puppet/Saltは未対応（既存`monitoring_stack`が3ツールとも
  対応しているのに対し、tacacs_syslogは現状Ansibleのみ）。
  スコープを絞った結果であり、必要になれば`ansible/roles/
  tacacs_syslog/`と同じ構成を移植すればよい
- TLS/PSK(tac_plus-ngの`tacacs.tls`)には未対応。平文TCPのみ
