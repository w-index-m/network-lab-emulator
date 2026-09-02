# 実SNMP(UDP/161, v2c)エージェント

`engine/snmp_udp_agent.py`

## これは何か

これまでの`tools/prometheus_exporter.py`は、実は**SNMPプロトコルを一切
使わず**、エミュレーターの内部JSON API（`/api/snmp/dashboard`）を直接
叩いてPrometheus形式に変換していただけだった。本モジュールは、この
制約を解消し、**本物のSNMP v2cワイヤプロトコル（BER符号化）でUDP/161に
応答する**実エージェントを実装したもの。

`engine/protocols.py`の`SnmpAgent`クラスには元々`get()`/`getnext()`/
`walk()`という内部シミュレーションロジックが実装済みだったため、
このモジュールはそれをBER符号化/復号でラップするだけで済んでいる。

```
net-snmp (snmpget/snmpwalk)  ─┐
Prometheus snmp_exporter     ─┼─UDP/161 (SNMPv2c)─▶ engine/snmp_udp_agent.py
その他任意のSNMPクライアント  ─┘                         │
                                                    engine/protocols.py
                                                    の SnmpAgent (既存)
```

## 仕組み

- 各装置のinterfacesから management IP を1つ選び（`mgmt`を含む
  インターフェース名を優先、無ければ最初に見つかったIP）、
  `ip addr add <ip>/32 dev lo scope host`でループバックにエイリアスを
  追加してから、そのIP:161にasyncio UDPソケットをbindする
- GetRequest/GetNextRequest/GetBulkRequestを受信したら、
  `SnmpAgent.get()`/`getnext()`を呼んで値を取得し、SNMPv2c
  GetResponseとしてBER符号化して返す
- 対応する型: INTEGER, OCTET STRING(STRING), OBJECT IDENTIFIER(OID),
  Timeticks, Counter32, Gauge32, IpAddress
- MIB終端は`endOfMibView`、存在しないOIDは`noSuchObject`を正しく返す

app.pyの起動時（lifespan）に自動的に全装置分のエージェントが立ち上がる。

## 実際に確認した動作

```bash
apt-get install -y snmp   # net-snmpクライアント

# 単発GET
snmpget -v2c -c public 10.9.9.1 1.3.6.1.2.1.1.1.0
# → iso.3.6.1.2.1.1.1.0 = STRING: "Cisco IOS XE Software, Catalyst L3 Switch, Version 17.09.01"

# ウォーク（GETNEXT連鎖）
snmpwalk -v2c -c public 10.9.9.1 1.3.6.1.2.1.1
# → system group 全OID(STRING/OID/Timeticks/INTEGER)が正しくデコードされる

# CISCO-PROCESS-MIB（GetBulk相当のウォーク、終端はendOfMibView）
snmpwalk -v2c -c public 10.9.9.1 1.3.6.1.4.1.9.9.109.1.1.1.1
```

**Prometheus公式`snmp_exporter`からの実ポーリングも確認済み**:

```bash
curl -sL -o snmp_exporter.tar.gz \
  "https://github.com/prometheus/snmp_exporter/releases/download/v0.26.0/snmp_exporter-0.26.0.linux-amd64.tar.gz"
tar xzf snmp_exporter.tar.gz --strip-components=1
./snmp_exporter --config.file=snmp.yml --web.listen-address=:9116

curl "http://localhost:9116/snmp?target=10.9.9.1&module=if_mib"
# → ifOperStatus{ifDescr="GigabitEthernet1/0/1",...} 1
#   ifOperStatus{ifDescr="Vlan10",...} 1
```

これで「Prometheus本体＋`snmp_exporter`があれば実SNMPポーリングできる」
という構成が、このリポジトリのエミュレーターに対しても実際に成立する。

## SNMP trap + syslog の実送信（link down/up）

これまで`logging host`/`snmp-server host`で送信先を設定していても、
CLIの`shutdown`/`no shutdown`によるインターフェース状態変化では
実際にはUDP送信されていなかった（`show logging`の内部バッファには
記録されるが、実配送パイプラインには乗らない設計だった）。この制約を
解消し、`shutdown`/`no shutdown`時に以下を実際にUDP送信するようにした:

- **syslog**: `%LINK-3-UPDOWN: Interface <ifname>, changed state to down/up`
  （RFC3164形式、`logging host`で設定した宛先へ）
- **SNMP trap**: `linkDown`(OID `1.3.6.1.6.3.1.1.5.3`) /
  `linkUp`(OID `1.3.6.1.6.3.1.1.5.4`)
  （SNMPv2c trap、`snmp-server host`で設定した宛先へ）

Catalyst/Cisco/Nexus/Si-R等、`shutdown`コマンドを持つ全機種で共通して
動作する（`app.py`のインターフェース状態変化ハンドラーが機種非依存の
ため）。

実際に確認した動作（UDPパケットを自作リスナーで捕捉）:

```
[SYSLOG] from ('127.0.0.1', ...): b'<188>Sep 02 13:08:36 Dist-SW %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down'
[TRAP]   from ('127.0.0.1', ...): b'...public...Dist-SW: GigabitEthernet1/0/1 down'
```

`no shutdown`側もlinkUp trap + up側syslogの両方を確認済み。

これでSNMP polling(GET/WALK)・SNMP trap送信・syslog送信の3つが揃い、
PRTG/SolarWinds/Zabbix/LibreNMS等の標準SNMP監視ツールから本物の
ネットワーク機器として扱える状態になった。

## 制約

- 現状8/10台のみ実エージェントが起動する。`sir-b`/`apresia`は
  このトポロジー内で他の装置（`sir-a`/`srs`）と管理IPが重複しており、
  同一IP:161への二重bindができないため起動をスキップしている
  （このリポジトリの初期トポロジー上の制約であり、SNMPエージェント側の
  バグではない）
- SetRequest（SNMP SET）は未対応
- SNMPv3（認証・暗号化）は未対応、v2c(コミュニティベース)のみ

## テスト

```bash
pytest tests/test_snmp_udp_agent.py -v
# 8/8 成功（BER符号化/復号の単体テスト）

pytest tests/test_link_trap_syslog.py -v
# 3/3 成功（shutdown/no shutdown時のsyslog+trap実送信テスト）
```
