# ZGrab2によるプロトコルハンドシェイク確認

## これは何か

ユーザー依頼「ZGrab2: ポートスキャンはせず、指定したIP:ポート群に対して
プロトコルのハンドシェイクを最後まで実行してバナー/証明書/レスポンス
全体を構造化データ(JSON)で取得する。これもラボに入れてほしい」への
対応。

[ZGrab2](https://github.com/zmap/zgrab2)はZMapプロジェクト製のL7
アプリケーション層スキャナ(Censysのバックエンドにも使われている本物の
ツール)。nmapとの違い:

| | nmap | ZGrab2 |
|---|---|---|
| 主眼 | ポートの開放有無の発見 | 指定したIP:ポートでプロトコルの**ハンドシェイクを最後まで完了**させる |
| 出力 | 人間向けテキスト | 構造化JSON(鍵交換アルゴリズム・証明書・バナー等) |
| 典型的な使い方 | 単独でスキャン | ZMap等で見つけた「生きていそうなIP」に対して深掘り |

このリポジトリの実SSHリスナー(`engine/ssh_cli_agent.py`、paramiko製)に
対して、本物のzgrab2バイナリで実際にSSHハンドシェイクを完了させ、
鍵交換情報まで取得できることを確認した。

## インストール

Go製なので`go install`でビルドできる(このサンドボックスでも実際に
ビルド・実行確認済み):

```bash
GOBIN=/path/to/bin go install github.com/zmap/zgrab2/cmd/zgrab2@latest
```

## ライブ検証(実行結果そのまま)

Catalystデバイスを作成し、実機同様`crypto key generate rsa`でSSHを
有効化:

```bash
curl -X POST http://127.0.0.1:8150/api/device -d '{"id":"zg-live-catalyst","type":"catalyst","hostname":"zg-live-catalyst"}'
# configure terminal / crypto key generate rsa modulus 2048 / end
```

実際に`zgrab2 ssh`を、Catalystの管理IP(`192.168.10.1`、Vlan10 SVI)に
向けて実行した結果(生のJSON出力、ログ行も含めてそのまま):

```
time="2026-10-14T17:22:09Z" level=info msg="started grab at 2026-10-14T17:22:09Z"
{"ip":"192.168.10.1","data":{"ssh":{"status":"success","protocol":"ssh","port":22,"result":{"server_id":{"raw":"SSH-2.0-paramiko_5.0.0","version":"2.0","software":"paramiko_5.0.0"},"server_key_exchange":{"cookie":"Vb2S+RMtF6YIDF6+lUFLBg==","kex_algorithms":["curve25519-sha256@libssh.org","ecdh-sha2-nistp256","ecdh-sha2-nistp384","ecdh-sha2-nistp521","diffie-hellman-group16-sha512","diffie-hellman-group-exchange-sha256","diffie-hellman-group14-sha256","kex-strict-s-v00@openssh.com"],"host_key_algorithms":["rsa-sha2-512","rsa-sha2-256"],"client_to_server_ciphers":["aes128-ctr","aes192-ctr","aes256-ctr","aes128-cbc","aes192-cbc","aes256-cbc","3des-cbc","aes128-gcm@openssh.com","aes256-gcm@openssh.com"],"server_to_client_ciphers":["aes128-ctr","aes192-ctr","aes256-ctr","aes128-cbc","aes192-cbc","aes256-cbc","3des-cbc","aes128-gcm@openssh.com","aes256-gcm@openssh.com"],"client_to_server_macs":["hmac-sha2-256","hmac-sha2-512","hmac-sha2-256-etm@openssh.com","hmac-sha2-512-etm@openssh.com","hmac-sha1","hmac-md5","hmac-sha1-96","hmac-md5-96"],"server_to_client_macs":["hmac-sha2-256","hmac-sha2-512","hmac-sha2-256-etm@openssh.com","hmac-sha2-512-etm@openssh.com","hmac-sha1","hmac-md5","hmac-sha1-96","hmac-md5-96"],"client_to_server_compression":["none"],"server_to_client_compression":["none"],"first_kex_follows":false,"reserved":0,"serverHaSSH":"48c4b4ca3f96168c5bbbadc362b47dc5"},"algorithm_selection":{"dh_kex_algorithm":"curve25519-sha256@libssh.org","host_key_algorithm":"rsa-sha2-256","client_to_server_alg_group":{"cipher":"aes128-gcm@openssh.com","mac":"","compression":"none"},"server_to_client_alg_group":{"cipher":"aes128-gcm@openssh.com","mac":"","compression":"none"}},"key_exchange":{"curve25519_sha256_params":{"server_public":"UwkAr52Oz+Kjl3K+vsmYDBivcLA8y+dV6G8iwH1uewI="},"server_signature":{"parsed":{"algorithm":"rsa-sha2-256","value":"kklqY4+mam6tEgrnZVE1uLcOl1TWbMRuofKga5nv2OAh6rtBVr+wGl6dKJ78FJE9M0Ithvapc/bVKu+dly9SM9U0G3778H8DoXxETdUhuMIPfA5jF/mr5GR0UB7rJP5zHoGofrN8xsE20uQGYPA4SDyaQLdHUjOAj7OMtwOANo74nMcQRVuutjxyQxkpyXoAH58iLt7t8c/S8bsTMN2TmL++Di0P6LkB4xtdtgU2UV2NnijETTL/hRmZByDSI7hbi/oNk2JEkWS88vh+XPxMvzLqknlQD6WHI5U1XBkP5fyOWTAqkkUs6NSx4a8q7QhUopHaFsh6tFmFNG4MWs+DVA=="},"raw":"AAAADHJzYS1zaGEyLTI1NgAAAQCSSWpjj6Zqbq0SCudlUTW4tw6XVNZsxG6h8qBrme/Y4CHqu0FWv7AaXp0onvwUkT0zQi2G9qlz9tUq752XL1Iz1TQbfvvwfwOhfERN1SG4wg98DmMX+avkZHRQHusk/nMegah+s3zGwTbS5AZg8DhIPJpAt0dSM4CPs4y3A4A2jvicxxBFW662PHJDGSnJegAfnyIu3u3xz9LxuxMw3ZOYv74OLQ/ouQHjG122BTZRXY2eKMRNMv+FGZkHINIjuFuL+g2TYkSRZLzy+H5c/Ey/MuqSeVAPpYcjlTVcGQ/l/I5ZMCqSRSzo1LHhryrtCFSikdoWyHq0WYU0bgxaz4NU","h":"hOJFelV4+6QtCT6r/3BASGaydjlOycCFbGO6pa38U7k="},"server_host_key":{"rsa_public_key":{"exponent":65537,"modulus":"rUoXO4cMg8AgOgpvklsWXexIZmqG+iioy86gJaMOTVgIH4cDlBBixJO+WbzGVG5bGkj0WWhPra59kGoZ5H9DnP0SNEfHUe/g/oiBrJcLVPcI6HijZyUY3dRkqWIoEKwdJpagUD3DyMqOQeOiqHaiJTDshx0m+XkOFQLFFRgQgx04XPviHw0NQLbL9FUJO2mND897w6hOFswOtlr2UEtr7Sj1iIQmQhz7zsvsYaWqbR7ZPPFOaMVj4aprs87cn1d4TxhuqpEw2WpuD8mrdPKt0tms7062muj9R1ERyvgT5Ml67oXJpp4eq7X/nhEv7srMSm3RDOv0vCbZAlbcm0xN6Q==","length":2048},"raw":"AAAAB3NzaC1yc2EAAAADAQABAAABAQCtShc7hwyDwCA6Cm+SWxZd7Ehmaob6KKjLzqAlow5NWAgfhwOUEGLEk75ZvMZUblsaSPRZaE+trn2Qahnkf0Oc/RI0R8dR7+D+iIGslwtU9wjoeKNnJRjd1GSpYigQrB0mlqBQPcPIyo5B46KodqIlMOyHHSb5eQ4VAsUVGBCDHThc++IfDQ1Atsv0VQk7aY0Pz3vDqE4WzA62WvZQS2vtKPWIhCZCHPvOy+xhpaptHtk88U5oxWPhqmuzztyfV3hPGG6qkTDZam4Pyat08q3S2azvTraa6P1HURHK+BPkyXruhcmmnh6rtf+eES/uysxKbdEM6/S8JtkCVtybTE3p","algorithm":"ssh-rsa","fingerprint_sha256":"6d8ba339b47b8c85875d53deefa0dbca2a24df8e79bd5d807ff35abbed69c4f6"}}},"timestamp":"2026-10-14T17:22:09Z"}}}
time="2026-10-14T17:22:09Z" level=info msg="finished grab at 2026-10-14T17:22:09Z"
00h:00m:00s; Scan Complete; 1 targets scanned; 22.15 targets/sec; 100.0% success rate
{"statuses":{"ssh":{"successes":1,"failures":0}},"start":"2026-10-14T17:22:09Z","end":"2026-10-14T17:22:09Z","duration":"45.078541ms","zgrab_cli_parameters":"/tmp/gobin/zgrab2 ssh --port 22 --connect-timeout 10s","num_targets_scanned":1}
```

`server_id.raw`が実際に`SSH-2.0-paramiko_5.0.0`(`engine/ssh_cli_agent.py`
が使っているparamikoのバージョン文字列そのまま)になっており、本物の
SSHハンドシェイクが最後まで完了して鍵交換アルゴリズム一覧・ホスト鍵
(RSA 2048bit、SHA256フィンガープリント)まで取得できていることが分かる。

## 実装中に見つけた実バグ(2件)

ライブ検証で初めて踏んだ、コードレビューだけでは気づけなかった不具合:

1. **`--timeout`フラグは存在しない**: `zgrab2 ssh --help`を実際に見ずに
   「たぶんこのフラグだろう」で書いた`--timeout`は`unknown flag`で
   即座に失敗した。正しくは`--connect-timeout`(接続確立までのタイム
   アウト)または`-t`/`--target-timeout`(1ターゲットあたりの総タイム
   アウト、既定60秒)。
2. **既定のblocklistファイルが無いと即座に失敗する**: `-b`/
   `--blocklist-file`の既定値`-`は「ブロックリスト無効」ではなく
   `$HOME/.config/zgrab2/blocklist.conf`を指す特殊な意味で、この
   ファイルが存在しない環境(未初期化のCI環境含む)では`could not read
   blocklist file`で即`fatal`終了する。テスト側では`tmp_path`に空の
   ブロックリストファイルを作って明示的に`--blocklist-file`で指定する
   ことで回避した。

## テスト

`tests/test_zgrab2_ssh_banner.py`(2 tests、`tests/test_ssh_cli_server.py`
と同じ自己完結サーバーfixtureパターン)が、Catalystデバイスへの実SSH
ハンドシェイク成功(`SSH-2.0`バナー取得)と、SSHが上がっていないポート
への失敗判定の両方を固定する。`netmiko`/`ansible`の既存テストと同じ
考え方で、`zgrab2`バイナリがPATH上に無い環境ではファイル全体を
`pytest.mark.skipif`でスキップする(回帰テスト自体はこの外部バイナリ
に依存しない、手動/オプションのライブ検証という位置づけ)。

## 今後の展開(未着手、次の合意済みタスク)

- ASAにも同じ`crypto key generate rsa`パターンで実SSHを有効化し、
  ZGrab2でのバナー取得を同様に確認する
- Si-RのSCPサーバー機能(実機の正式機能)を有効化できれば、SSH
  トランスポート経由でZGrab2のバナー取得対象にできる(実機コマンド
  確認が先)
- ASAの`threat-detection scanning-threat`(実機の正式機能)で、ZGrab2
  のような短時間多数接続を検知する機能の実装
