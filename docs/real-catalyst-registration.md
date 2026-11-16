# 実機の Catalyst をニルヴァーナに登録する手順

実機はエミュレータ(ラボ版)には入れられない。実機を見るのは **スタンドアロン版**(`cyber-view-standalone/`)で、
既にある監視基盤(Prometheus + snmp_exporter、Loki、Zabbix のどれか)から値を読む。
3D 画面(NETWORK / ROUTER LINKS / OSPF)も BOARD / EXPLORE / ALERTS も、ラボ版と同じものがそのまま使える。

```
Catalyst ──SNMP(UDP/161)──▶ snmp_exporter ──▶ Prometheus(ヴァルハラ) ─┐
         ──syslog(UDP/5514)─▶ syslog_to_loki.py ──▶ Loki ────────────────┼─▶ ニルヴァーナ(スタンドアロン版 :8090)
         ──trap(UDP/1162)──────────────────────────────────────────────┘   (任意)
```

以下の例: Catalyst `CAT-01`(管理 IP 10.10.0.11、管理 VLAN 100)、`CAT-02`(10.10.0.12)、
監視サーバー 10.10.0.50(Prometheus・Loki・ニルヴァーナを置く)、SNMP コミュニティ `netlab-ro`。

## 1. Catalyst 側の設定

```
configure terminal
! --- SNMP(読み取り専用。監視サーバーからだけ許可) ---
ip access-list standard SNMP-RO
 permit 10.10.0.50
snmp-server community netlab-ro RO SNMP-RO
snmp-server ifindex persist                 ! 再起動で ifIndex が変わらないように(グラフが途切れない)
snmp-server location tokyo-dc1
! --- syslog(LINK DOWN の検知経路 SYSLOG、error 以上の警報、フラップの警告に使う) ---
service timestamps log datetime msec localtime
logging source-interface Vlan100            ! 送信元を管理 IP にそろえる(装置の特定に使う)
logging host 10.10.0.50 transport udp port 5514
logging trap informational
! --- SNMP trap(任意。LINK DOWN の検知経路 TRAP) ---
snmp-server enable traps snmp linkdown linkup
snmp-server host 10.10.0.50 version 2c netlab-ro udp-port 1162
! --- 時刻(ログの時刻がずれると再生や注釈の位置がずれる) ---
ntp server 10.10.0.1
end
write memory
```

確認(監視サーバーから):

```bash
snmpwalk -v2c -c netlab-ro 10.10.0.11 1.3.6.1.2.1.31.1.1.1.1   # ifName が Gi1/0/1 ... と返る
snmpget  -v2c -c netlab-ro 10.10.0.11 1.3.6.1.2.1.14.1.1.0      # OSPF を動かしていれば Router-ID
```

## 2. 監視基盤(Prometheus + snmp_exporter + Loki)

まだ無ければ、このリポジトリの IaC で立てられる(`docs/cisco-traffic-monitoring-iac.md`)。

```bash
cd iac/cisco-traffic-monitoring
./deploy.sh up
```

**コミュニティ**を実機に合わせる: `snmp_exporter/snmp.yml` の `auths.netlab_v2.community`。
モジュール `cisco_if` は IF-MIB(流量・状態・速度・ifName)、CPU、OSPF-MIB を取る。Catalyst でもそのまま使える。

**監視対象**を足す: `prometheus/targets/cisco_routers.yml`(ファイルを置き換えれば Prometheus が自動で読み直す)

```yaml
- targets: [10.10.0.11]
  labels: {router: CAT-01, site: tokyo}
- targets: [10.10.0.12]
  labels: {router: CAT-02, site: tokyo}
```

http://10.10.0.50:9090/targets で `cisco-snmp` の2台が UP になれば OK。

**syslog を Loki へ**: 監視サーバーで

```bash
python3 tools/syslog_to_loki.py --syslog-port 5514 --loki-url http://127.0.0.1:3100
```

Loki には `source_ip`(= Catalyst の送信元 IP)ラベル付きで入る。

## 3. ニルヴァーナ(スタンドアロン版)の設定

```bash
cd cyber-view-standalone
cp config.example.yaml config.yaml
```

`config.yaml` を書き換える(装置とつながりは手書き):

```yaml
title: 本番ネットワーク
devices:
  - id: cat01
    hostname: CAT-01                # 表示名。syslog 本文の装置名の照合にも使う
    type: catalyst                  # フラップ検知のログ形式(Catalyst の STP/MAC フラップ等)
    mgmt_ip: 10.10.0.11             # = Prometheus の instance = syslog の送信元
    interfaces:                     # IP アドレス空間(NETWORK)の画面に置く IF。名前は ifName と同じ(Gi1/0/1 でも可)
      - {name: Vlan100, ip: 10.10.0.11, prefix: 24}
      - {name: GigabitEthernet1/0/48, ip: 10.10.1.1, prefix: 30}
  - id: cat02
    hostname: CAT-02
    type: catalyst
    mgmt_ip: 10.10.0.12
    interfaces:
      - {name: Vlan100, ip: 10.10.0.12, prefix: 24}
      - {name: GigabitEthernet1/0/48, ip: 10.10.1.2, prefix: 30}

links:                              # ROUTER LINKS の線。両端の IF 名は ifName の表記で
  - {a: cat01, a_if: GigabitEthernet1/0/48, b: cat02, b_if: GigabitEthernet1/0/48, speed: 1000000000}

sources:
  prometheus:
    url: http://127.0.0.1:9090
    job: cisco-snmp                 # prometheus.yml の snmp_exporter の job 名
  loki:
    url: http://127.0.0.1:3100
    query: '{job="netlab-syslog"}'
    device_labels: [source_ip, host, hostname, instance]   # source_ip で装置を特定する
  snmp:                             # 経路表と CPU を直接定期取得(経路の変化・CPU の情/注)
    community: netlab-ro
    interval: 60
    cpu: {high: 80, jump: 20, hold: 2}
  # snmp_trap:
  #   listen: 0.0.0.0:1162          # 1 の trap を設定した場合
```

`sources.snmp` はニルヴァーナのサーバーから Catalyst へ直接 SNMP で問い合わせるので、1 の `SNMP-RO` の ACL に
そのサーバーの IP も入れておく(監視サーバーと同じ 10.10.0.50 に置くなら追加は不要)。詳細は `docs/snmp-route-cpu-info.md`。

起動:

```bash
pip install -r requirements.txt
python3 server.py -c config.yaml --port 8090
# docker なら: docker compose up -d --build
```

ブラウザで `http://10.10.0.50:8090/`。

## 4. 映っているかの確認

| 見る場所 | 期待すること |
|---|---|
| ROUTER LINKS | CAT-01 ⇄ CAT-02 の線に流量。`SOURCE: VALHALLA` |
| 上の SYSTEM STATUS | ネットワーク「正常」、`METRICS: VALHALLA` `LOGS: LOKI` |
| 試しに片側の Gi1/0/48 を shutdown | 線が LINK DOWN、下に検知経路 POLL / SYSLOG(trap を入れていれば TRAP)が点く |
| OSPF ボタン | Catalyst で OSPF を動かしていれば隣接が Full で出る |
| BOARD | 「…」→「JSON を取り込む」で `iac/.../grafana/dashboards/cisco-traffic.json` を入れると、ルータ・IF を選べるトラフィック画面になる |
| `ip route` を1本足して消す(`sources.snmp` を書いた場合) | 次の取得で「情 INFO 経路追加 / 経路削除」 |
| `http://10.10.0.50:8090/api/health` | `errors` が空(Prometheus・Loki に届いている) |

## よくあるつまずき

- **線が出ない/流量が 0**: `links` の IF 名と ifName の表記違い(`Gi1/0/48` と `GigabitEthernet1/0/48` は同じ扱いだが、
  `Te1/1/1` を `GigabitEthernet...` と書くと一致しない)。`snmpwalk ... 1.3.6.1.2.1.31.1.1.1.1` の値に合わせる。
- **ログが装置に紐付かない**: syslog の送信元 IP が `mgmt_ip` と違う(`logging source-interface` を管理 VLAN に)。
- **NO RESPONSE(消失)になる**: SNMP の ACL で監視サーバーが許可されていない、コミュニティ違い、UDP/161 が FW で閉じている。
- **時刻がずれる**: Catalyst と監視サーバーの両方で NTP を合わせる。
- Zabbix で監視している場合は `sources.zabbix` だけでもよい(`cyber-view-standalone/README.md` の「Zabbix で気をつけること」)。
