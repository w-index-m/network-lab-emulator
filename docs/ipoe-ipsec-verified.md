# IPoE経由の2拠点間IPsec ― 検証結果

**結論**: IPsecトンネル自体は今回も**確立できた**。ただし
「本当にIPoE(DHCP方式)でIPが動的に払い出され、それをIPsecが使う」
という一連の流れは、このエミュレータでは**IPoEクライアント側の
実装が無いため未検証**。今回はPPPoEのときと同じ構造の制約が
IPoEにもそのまま当てはまることを確認したうえで、「IPoEで
払い出されたと仮定したIP」を手動設定してIPsecが張れることを示した。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`のHTTP APIに対するcurlコマンドで再現できる。
サーバーはTestClientではなく実際にuvicornで起動して検証すること
（理由は`docs/aws-directconnect-bgp-design.md`参照）。

## まず確認したこと: `ip address dhcp` は未実装

IPoEはWAN側インタフェースがDHCPクライアントとして動作し、
ISP(NTT東西のフレッツ光ネクスト等)からグローバルIPを動的に
受け取る方式。Cisco IOSではこれを`ip address dhcp`で設定する。

実際に投入して確認した:

```bash
B=http://127.0.0.1:8322
curl -s -X POST $B/api/device -d '{"id":"ipoe-r1","type":"cisco","hostname":"IPOE-R1"}'
```

```
configure terminal
interface GigabitEthernet0/0
ip address dhcp
end
show ip interface brief
```

### 実際の出力

```
% Incomplete command.
```

`show ip interface brief`でもGigabitEthernet0/0にIPは一切割り当た
らなかった（`unassigned`のまま）。`ip address dhcp`は`ip address`の
不完全な入力として処理され、DHCPクライアントとしての動作には
一切つながっていない。つまり:

- ルータのWAN側をDHCPクライアントにする機能（IPoEの受信側）は
  **存在しない**
- 前回のPPPoE検証で判明した「PPPoEクライアント側（PADI送出〜
  IP受信）が無い」のと**全く同じ種類の欠落**が、IPoE(DHCP)側にも
  ある

サーバー側（`ip dhcp pool`によるDHCPサーバ機能）は以前から
コンフィグレベルで実装済みだが、これも「投入したコマンドを
running-configにそのまま反映するだけ」で、実際にリースを払い出し
装置間でDHCPDISCOVER〜ACKのやり取りをする機能はない
（`docs/sir-g110b-real-device-comparison.md`で以前確認した
Si-Rの`lan ip dhcp service client|server`と同種の制約）。

## IPsec自体の確認（手動でIPoE払い出し相当のIPを設定）

IPsecエンジンは「インタフェースに設定されているIPアドレス」だけを
見て動作するため、そのIPがDHCPで来たものか手動設定かは区別しない。
そこで「IPoEで払い出された想定」のグローバルIPを手動投入し、
前回のPPPoE/固定IP検証と同じcrypto構成でIPsecが張れることを確認した。

### 構成

```
10.11.0.0/24 --- [IPOE-Tokyo: Gi0/0 198.51.100.10/30] ===IPsec(ESP)=== [IPOE-Osaka: Gi0/0 198.51.100.9/30] --- 10.12.0.0/24
```

（`198.51.100.0/24`はTEST-NET-2。実際のIPoE環境ではISPから
払い出されるグローバルIPだが、ラボ検証用に予約アドレス帯を使用）

### 投入コマンド（東京側）

```
configure terminal
interface GigabitEthernet0/0
ip address 198.51.100.10 255.255.255.252
no shutdown
exit
interface Loopback0
ip address 10.11.0.1 255.255.255.0
exit
crypto isakmp policy 10
authentication pre-share
encryption aes 256
hash sha256
group 14
lifetime 86400
exit
crypto isakmp key IPoE@VPN2024 address 198.51.100.9
crypto ipsec transform-set TS-IPOE esp-aes-256 esp-sha256-hmac
crypto map IPOEMAP 10 ipsec-isakmp
match address 100
set peer 198.51.100.9
set transform-set TS-IPOE
exit
interface GigabitEthernet0/0
crypto map IPOEMAP
exit
crypto isakmp enable GigabitEthernet0/0
end
```

大阪側はIPと`crypto isakmp key`/`set peer`の相手IPを対称に
入れ替えるだけ（`docs/ipsec-cisco-cisco-verified.md`と同じ構成）。
**`crypto ipsec transform-set`の直後に`exit`を打たないこと**という
同じ注意点が今回も当てはまる（実機の`mode tunnel`サブモードが
このエミュレータに無いため、直後の`exit`でconfigモードごと
抜けてしまう）。

### 実際に確認できた結果

`show crypto ipsec sa`（東京側）:

```
interface: GigabitEthernet0/0
    Crypto map tag: IPOEMAP, local addr 198.51.100.10

   current_peer 198.51.100.9 port 500
   DPD status: ESTABLISHED
   PERMIT, flags={origin_is_acl,}
   #pkts encaps: 377, #pkts encrypt: 4235
   #pkts decaps: 4643, #pkts decrypt: 7745
   #send errors 0, #recv errors 0

     local crypto endpt.: 198.51.100.10, remote crypto endpt.: 198.51.100.9
     inbound esp sas:
      spi: 0x45db1c04(decimal: 1171987460)
       Status: ACTIVE
     outbound esp sas:
      spi: 0x98610e6f(decimal: 2556497519)
       Status: ACTIVE
```

`show crypto ipsec sa`（大阪側）:

```
interface: GigabitEthernet0/0
    Crypto map tag: IPOEMAP, local addr 198.51.100.9

   current_peer 198.51.100.10 port 500
   DPD status: ESTABLISHED
   PERMIT, flags={origin_is_acl,}
   #pkts encaps: 8710, #pkts encrypt: 3958
   #pkts decaps: 1006, #pkts decrypt: 7240
   #send errors 0, #recv errors 0

     local crypto endpt.: 198.51.100.9, remote crypto endpt.: 198.51.100.10
     inbound esp sas:
      spi: 0x5581ce15(decimal: 1434570261)
       Status: ACTIVE
     outbound esp sas:
      spi: 0xb442ab0e(decimal: 3024268046)
       Status: ACTIVE
```

両側とも`DPD status: ESTABLISHED`、inbound/outbound ESP SAが
`Status: ACTIVE`。ピアIPも相互に一致しており、**IPsecトンネル自体は
問題なく確立する**ことを確認した。

## まとめ: 質問への回答

> IPoEして2拠点間に、IPsecを貼ることできますか？

- **IPsec自体は張れる**（今回確認済み。手動設定・PPPoE経由の固定
  IPと同じ構成がそのまま動く）
- **「IPoEで動的にIPが払い出される」という前提部分は今のところ
  シミュレートできない**。`ip address dhcp`が実装されていないため、
  ルータが実際にDHCPクライアントとして動作し払い出しを受ける
  流れそのものが無い
- 実務でIPoE環境にIPsecを重ねる場合の実際の注意点（このエミュレータの
  範囲外の一般知識）:
  - IPoE(v6プラス、DS-Lite等)は多くの場合**IPv4アドレスがCGNAT配下**
    になり、拠点側から見た「自分のグローバルIP」を用いた従来型の
    IPsec(拠点固定IPでのピア指定)が使えないケースがある
  - そのため実務では、IPoE環境からのIPsecは
    「動的グローバルIPをオンプレ側で監視しDDNS等でピアIPを追従させる」
    か、あるいは**IPoE用のグローバルIPv6アドレスを使う**方式
    (v6プラスのIPv6グローバルアドレスは動的CGNATの影響を受けない)
    のいずれかで設計されることが多い

## 未実装として今後の課題に残るもの

- Cisco IOSの`ip address dhcp`（DHCPクライアント動作）
- Si-Rの`lan <n> ip dhcp service client`の意味的動作
  （`docs/sir-g110b-real-device-comparison.md`で既に指摘済み）
- 上記が実装されれば、`ip local pool`からのPPPoE払い出しと同様に、
  「IPoEで実際に払い出されたIPでIPsecが確立する」エンドツーエンドの
  検証が可能になる見込み

## 関連ドキュメント

- `docs/ipsec-cisco-cisco-verified.md` — 固定IPでのCisco↔Cisco IPsec
  検証（今回のベースにした構成・ハマりどころ）
- `docs/aws-directconnect-bgp-design.md` — TestClientの制約についての
  詳しい説明
- `docs/sir-g110b-real-device-comparison.md` — Si-RのDHCP関連コマンドが
  「意味的な動作までは実装されていない」ことの既存の記録
