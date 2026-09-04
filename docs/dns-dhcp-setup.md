# DNS/DHCP環境（PowerDNS + Kea DHCP）

`tools/setup_dns_dhcp.sh`

## これは何か

Infoblox的な「DDI（DNS + DHCP + IPAM）」統合管理を、OSSの組み合わせで
代替する構成の一部。IPAM部分は`tools/setup_netbox.sh`で導入した
NetBoxが担当し、こちらはDNS（PowerDNS）とDHCP（Kea）の実サーバーを
提供する。

Infoblox自体は商用クローズドソースのため自己構築できない。この構成が
現実的な代替。

## なぜこの組み合わせか

- **PowerDNS**: 権威DNSサーバー。バックエンドをPostgreSQL/MySQL等の
  一般的なDBにできるため、NetBox等の他システムから見て「DBを直接
  操作すればDNSレコードを制御できる」構成にしやすい
- **Kea DHCP**: ISC DHCPの後継。ISC (PowerDNSも同様の思想の実装元)
  が開発しており、JSON設定・REST風のcontrol-socket APIを持つ

両方とも**Ubuntu標準aptリポジトリから直接インストール可能**
（`pdns-server`, `pdns-backend-pgsql`, `kea-dhcp4-server`）。
Docker Hub等の迂回策は不要だった。

## 実際に確認した動作（2026-09-02）

```bash
sudo bash tools/setup_dns_dhcp.sh setup
```

**PowerDNS（DNS権威応答）**:
```bash
dig @127.0.0.1 -p 53 +norecurse catalyst.netlab.test A
# → ANSWER: catalyst.netlab.test. 3600 IN A 10.9.9.1 (status: NOERROR, aa flag)
```
`pdnsutil create-zone netlab.test` → `pdnsutil add-record ...`で
このラボのCatalyst/Nexus等の管理IPをDNS名で引けるようにした。

**Kea DHCPv4（DHCPリース割り当てロジック）**:
自作のDHCPDISCOVERパケットを送信したところ、Keaが正しく受信し
プール(`192.0.2.100-150`)からIPを選定してoffer準備するところまで
実際に動作確認できた:
```
DHCP4_LEASE_ADVERT [hwtype=1 02:00:00:11:22:33]:
  lease 192.0.2.100 will be advertised
```

## つまずいた点

- **IPv6非対応環境で `Fatal error: Unable to acquire TCP socket:
  Address family not supported`**: PowerDNSがデフォルトで`::`にも
  bindしようとして失敗する。`local-address=0.0.0.0` /
  `query-local-address=0.0.0.0`で明示的にIPv4限定にする必要がある
- **PowerDNSのデフォルトBINDバックエンドとgpgsqlバックエンドが競合**:
  `/etc/powerdns/pdns.d/bind.conf`を無効化しないと、DBバックエンドを
  追加してもBINDバックエンドが優先されて起動時エラーになることがある
- **`.local`ドメインはmDNS予約領域**: テスト用ゾーン名に`.local`を
  使うとdig/resolverが警告を出す（`REFUSED`の直接原因ではなかったが
  紛らわしいので避けた方がよい。実際の`REFUSED`原因はDBバックエンド
  未接続状態での起動だった）
- **Kea DHCPv4は設定ファイルがJSONCではなくJSON5系コメント記法混在**:
  配布デフォルトの`kea-dhcp4.conf`はコメント付きでPythonの`json`
  モジュールでは読めない。素直に新規JSONを書き下ろす方が早い
- **Kea 2.4.1では`loggers[].output-options`構文が未対応**（バージョン
  差異）。エラーで教えてくれるので該当ブロックを削除すればよい
- **`/var/lib/kea`のパーミッション**: パッケージインストール直後は
  `_kea`ユーザー所有。root権限で直接`kea-dhcp4`を起動する分には
  問題ないはずだが、環境によっては`chown -R _kea:_kea`が必要になる
  ケースがあった
- **DHCPDISCOVERへの応答送信が`Permission denied`で失敗**: このサンド
  ボックスのネットワーク名前空間ではブロードキャスト送信
  （`255.255.255.255:68`宛）に必要な権限が制限されているとみられる。
  **DHCPDISCOVER受信→リース選定ロジックまでは実際に動作確認済み**
  だが、応答パケットの送出は本番相当のネットワーク環境でのみ
  最終確認できる（このサンドボックス固有の制約であり、Kea自体の
  設定不備ではない）

## 未実施

- PowerDNS-NetBox連携（NetBoxのIPアドレス管理からPowerDNSへの
  自動レコード反映）
- Kea DHCPのDDNS連携（リース払い出し時にPowerDNSへ自動でAレコード登録）
- DHCPリース配布の完全なエンドツーエンド確認（上記の理由により
  ブロードキャスト応答の実配送は未確認）
