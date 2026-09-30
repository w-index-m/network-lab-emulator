# Si-R拠点間IPsec VPN 設定サンプル

Si-R同士（東京-大阪の2拠点）でIKE(自動鍵交換)によるIPsec VPNを張るサンプル構成。
`network-lab-emulator`のHTTP API(`/api/device`, `/api/cli`)経由で再現できる。
実際にuvicornを起動して`show ike sa`/`show ipsec sa`が`MATURE`になることを
確認済み（2026-09-09）。

## 構成

```
10.1.0.0/24 --- [TOKYO: lan0 10.1.0.1/24, wan1 192.0.2.201/30]
                       ===IPsec(ESP, AES256/SHA256)===
                [OSAKA: lan0 10.2.0.0/24, wan1 192.0.2.202/30] --- 10.2.0.0/24
```

**注意（重要）**: `192.0.2.0/24`や`198.51.100.0/24`のようなテスト用
アドレス帯は、このラボで過去に何度も使い回されており、
`saved_config.json`に永続化された過去の検証用デバイスが同じIPを
既に使っている場合がある。ピア探索(`_find_peer`)はIPだけを見て
device_sessions全体から一致する装置を探すため、無関係な古い装置と
誤って照合されてLARVALのまま進まないことがある。**新しく検証する
ときは、事前に該当IPが未使用か確認してから使うこと**:

```python
import json
d = json.load(open('saved_config.json'))
used = set()
for dev in d.get('devices', {}).values():
    for t in (dev.get('ipsec_tunnels') or {}).values():
        if t.get('local_ip'): used.add(t['local_ip'])
    for info in (dev.get('interfaces') or {}).values():
        if isinstance(info, dict) and info.get('ip'):
            used.add(info['ip'])
print('192.0.2.201' in used, '192.0.2.202' in used)
```

## 投入コマンド

### 東京側 (`sir-vpn-tokyo`)

```
configure terminal
interface lan0
ip address 10.1.0.1/24
exit
interface wan1
ip address 192.0.2.201/30
exit
remote 1 ap 0 name OSAKA
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 192.0.2.201
remote 1 ap 0 tunnel remote 192.0.2.202
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike lifetime 28800
remote 1 ap 0 ipsec sa lifetime 3600
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ike dpd use on
remote 1 ap 0 ike dpd idle 60s
remote 1 ap 0 ike dpd retry 10s 3
remote 1 ap 0 ipsec ike preshared-key TokyoOsaka@2024
ike use on
ipsec use on
exit
```

### 大阪側 (`sir-vpn-osaka`)

東京側と対称に、IPと`tunnel local`/`tunnel remote`を入れ替えるだけ。
事前共有鍵(`preshared-key`)は両側で完全に一致させること（大文字小文字を
区別する）。

```
configure terminal
interface lan0
ip address 10.2.0.1/24
exit
interface wan1
ip address 192.0.2.202/30
exit
remote 1 ap 0 name TOKYO
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 192.0.2.202
remote 1 ap 0 tunnel remote 192.0.2.201
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike lifetime 28800
remote 1 ap 0 ipsec sa lifetime 3600
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ike dpd use on
remote 1 ap 0 ike dpd idle 60s
remote 1 ap 0 ike dpd retry 10s 3
remote 1 ap 0 ipsec ike preshared-key TokyoOsaka@2024
ike use on
ipsec use on
exit
```

### DPDタイマーの制約（投入時にエラーになった実例）

`ike dpd idle`/`ike dpd retry`は数値のみだと以下のようにエラーになる。
単位(`s`/`m`/`h`/`d`)を付けること:

```
remote 1 ap 0 ike dpd idle 30
<ERROR> : 3 : format error
  (無通信監視時間は数値+単位(s/m/h/d)で指定してください。例: 10s)
```

さらに「再送時間×(再送回数+1)」は「無通信監視時間」より短くする必要が
ある。例えば`idle 30s`のまま`retry 10s 3`（10×4=40s > 30s）にすると:

```
remote 1 ap 0 ike dpd retry 10s 3
<ERROR> : 3 : format error
  (再送時間×(再送回数+1)は無通信監視時間より短くしてください)
```

→ `idle 60s` + `retry 10s 3`（10×4=40s < 60s）ならOK。

## 確認コマンドと期待される出力

### `show ike sa`（東京側）

```
  Peer             Phase1   Initiator  DH   Lifetime(rem)  Mode
  ---------------  -------  ---------  ---  -------------  ----------
  192.0.2.202      MATURE   yes        14   25468          main
```

### `show ipsec sa`（東京側）

```
  Remote           Local            Protocol  SPI(In)    SPI(Out)   State
  ---------------  ---------------  --------  ---------  ---------  -----------
  192.0.2.202      192.0.2.201      ESP       0xf8f8f882 0xaf8f8f88 MATURE
```

`Phase1`/`State`が`MATURE`になっていればVPNトンネルは確立成功。
`LARVAL`のまま変化しない場合は主に以下を疑う:

1. 双方の`preshared-key`が完全一致しているか（大文字小文字含む）
2. `ike use on` / `ipsec use on`が両方入っているか
3. 上記の「注意」に書いたIPアドレス衝突（過去の検証済みデバイスと
   同じIPを使っていないか）

### `show ipsec tunnel`

```
  Tunnel  Status         Local-IP        Remote-IP       Encrypt    Hash   DH   Mode
  ------  -------------  --------------  --------------  -----------  ------------  ---  ----------
  1       Waiting        192.0.2.201     192.0.2.202     aes256       sha256        14   main
```

（ネゴシエーション未完了時は`Waiting`、確立後は`show ipsec sa`側で
`MATURE`を確認する。`show ipsec tunnel`自体はコンフィグのサマリ表示。）

## この検証で見つけて修正したエミュレータ側の不具合

1. **事前共有鍵の大文字小文字が保持されない**
   (`engine/rules.py` `remote N ap 0 ipsec ike preshared-key`)
   小文字化済みのコマンド文字列から鍵を抽出していたため、
   `TokyoOsaka@2024`のような大文字混じりの鍵が黙って
   `tokyoosaka@2024`に変換されていた。元の大文字小文字を保持した
   文字列から再抽出するよう修正。
2. **`show ipsec sa`のRemote/Local列がIPv4最大長で崩れる**
   列幅が13桁固定で、13文字ちょうどのIP(`192.0.2.201`等)や
   15文字のIP(`255.255.255.255`)だと次の列とスペース無しで
   くっついて表示されていた。17桁に拡大して修正
   (`show spanning-tree`のインタフェース列で見つけた同種の
   不具合と同じ原因)。

## 関連ドキュメント

- `docs/ipsec-cisco-cisco-verified.md` — Cisco IOS同士のIPsec検証
- `docs/ipoe-ipsec-verified.md` — IPoE経由のCisco IPsec検証
- `docs/routing-cli-verification.md` — RIP/OSPF/EIGRP/BGP/vPCの
  実機突き合わせ記録（同じ手法）
