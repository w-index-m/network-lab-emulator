# ASA threat-detection scanning-threat(スキャン検知/自動シャン)

## これは何か

「④ZGrab2のスキャンをCatalyst側で検知する...今ある中ならASAの方が
面白い？」というユーザー提案への対応。実機Cisco ASAの正式機能
`threat-detection scanning-threat`を実装し、ZGrab2/nmapのような
短時間多数接続のスキャンを実際に検知し、閾値超過したソースIPを
実際にシャン(以降の接続を無条件破棄)する。

フェイクの検知ではない: 実SSHリスナー(`engine/ssh_cli_agent.py`)への
**実際のTCP接続試行**をカウントし、実機同様の
rate-interval(観測窓)/average-rate/burst-rateのスライディング
ウィンドウ判定でシャン対象を決定し、シャン後は本当にその後の接続を
(SSHハンドシェイクが始まる前に)切断する。

## 実機CLI構文

Cisco ASA 5500 Series Configuration Guide / Command Referenceに基づく
(このサンドボックスの egress ポリシーで `cisco.com`/`fujitsu.com` への
到達が403でブロックされており、公式ドキュメントを直接fetchできな
かったため、WebSearchのスニペットとコミュニティ投稿の実コンフィグ
例で裏取りした上で実装——完全な一次情報確認ではない点は明記する):

```
threat-detection scanning-threat [shun [except ip-address <ip> <mask>]]
threat-detection rate scanning-threat rate-interval <sec> average-rate <n> burst-rate <n>
threat-detection shun duration <sec>
show threat-detection rate
show threat-detection shun
clear threat-detection shun [ip-address <ip>]
no threat-detection scanning-threat
```

## 実装

- `engine/threat_detection.py`(新規)の`ThreatDetectionEngine`が、
  接続試行のスライディングウィンドウ(`rate-interval`秒)とバースト
  ウィンドウ(実機通り `rate-interval/30`、最低10秒)の両方でレートを
  計算し、`average-rate`/`burst-rate`いずれかを超えたら以降その
  ソースIPをシャンする。設定値(`enabled`/`shun`/`rate_interval`/
  `average_rate`/`burst_rate`/`shun_duration`/`shun_except`)は
  `state.threat_detection`(DeviceState)に、接続試行カウンタ/シャン
  済みIPのランタイム状態はエンジン側に保持する——他のengine
  (PppoeEngine等)と同じ「設定はstate、ランタイムはengine」という
  既存パターン。DeviceStateは自分のdevice_idを知らない
  (`RuleEngine.process()`がdevice_id非対応)ため、`id(state)`を
  キーに使っている。
- `engine/ssh_cli_agent.py`の`SshCliServer._loop()`(実際にTCP
  接続をacceptする箇所)で、新規接続のソースIPを
  `record_connection_attempt()`に渡し、遮断すべきと判定されたら
  SSHハンドシェイクを始める前に即座に切断する。ASA以外の装置は
  `state.threat_detection['enabled']`が既定で`False`のためno-op。
- CLI(`engine/rules.py` `_asa_process`)・`show running-config`への
  反映・ヘルプテキストを追加。

## ライブ検証(実行結果そのまま)

ASAデバイスを作成し、短い観測窓で閾値を設定(実機の既定値
rate-interval 600秒/average-rate 5接続/秒のままでは検証に10分単位の
時間がかかるため、デモ用に`rate-interval 5 average-rate 3 burst-rate 3`
に設定):

```
configure terminal
crypto key generate rsa modulus 2048
threat-detection rate scanning-threat rate-interval 5 average-rate 3 burst-rate 3
threat-detection scanning-threat shun
end
```

最初の数回は通常通りZGrab2のSSHハンドシェイクが成功する:

```
"status":"success"
"status":"success"
```

その後スキャンを続けると(内部的には`show threat-detection rate`で
`Average(eps): 3.40 >= 3` → `Trigger: Yes`、`Total Events: 17`)、
以降のZGrab2の接続は実際にハンドシェイク前に切断されるようになる:

```
time="2026-10-14T23:26:12Z" level=info msg="started grab at 2026-10-14T23:26:12Z"
{"ip":"203.0.113.1","data":{"ssh":{"status":"handshake-error","protocol":"ssh","port":22,"timestamp":"2026-10-14T23:26:12Z","error":"failed to create SSH client connection: ssh: handshake failed: EOF"}}}
time="2026-10-14T23:26:12Z" level=info msg="finished grab at 2026-10-14T23:26:12Z"
00h:00m:00s; Scan Complete; 1 targets scanned; 160.88 targets/sec; 0.0% success rate
{"statuses":{"ssh":{"successes":0,"failures":1}},"start":"2026-10-14T23:26:12Z","end":"2026-10-14T23:26:12Z","duration":"6.191186ms","zgrab_cli_parameters":"/tmp/gobin/zgrab2 ssh --port 22 --connect-timeout 3s","num_targets_scanned":1}
```

`show threat-detection shun`/`show threat-detection rate`/
`show running-config`も実際にシャン状態を反映する:

```
$ show threat-detection shun
Shunned Hosts  -  Duration     Remaining (secs)
  203.0.113.1          3600              3586

$ show threat-detection rate
                      Average(eps)    Current(eps)   Trigger     Total Events
  scanning-threat (203.0.113.1):  1.20            0.60          No         6

$ show running-config (抜粋)
threat-detection rate scanning-threat rate-interval 5 average-rate 3 burst-rate 3
threat-detection scanning-threat shun
```

(`show threat-detection shun`に表示されたIPが`203.0.113.1`——ASAの
outside IPそのもの——になっているのは、このサンドボックス内で同一
ホストからloopback alias経由のIPに接続した際、送信元アドレスが
その"self"アドレスとして解決されるため。実際のリモートホストから
スキャンすればそのリモートIPが表示される。)

## スコープ外(実装していない)

- UDP/ICMPスキャンの検知(このエミュレータの実リスナーはTCPのSSHの
  みなので、観測対象もTCP接続試行のみ)
- `threat-detection basic-threat`(トラフィック全体の異常率、個別
  ホスト単位ではない機能)
- 複数rate-interval設定(実機はevent-typeごとに最大3つまで設定
  可能、このエミュレータは1 device = 1 rate設定のみ)
- `rate-interval`の実機上の離散値制約(600/1800/3600秒など)の検証
  ——任意の正の整数をそのまま受理する(デモ/テストで現実的な時間で
  検証するため)
