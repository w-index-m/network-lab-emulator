# 実Telnetサーバ（TCP/23）と `transport input`

[`ssh-cli-server.md`](./ssh-cli-server.md) のTelnet版。
素のソケットでログインしてCLIを叩ける。

```
$ telnet 10.225.0.1

User Access Verification

Username: netadmin
Password:
TELNET-SW# show ip interface brief
```

---

## 1. なぜ必要だったか

Nexposeエミュレーションに `netlab-telnet-cleartext`（平文管理プロトコル）
という所見を用意していたが、**これは一度も成立しない死んだ判定だった**。

- 検出条件が `state.telnet_enabled` を見ていた
- ところが**この属性を立てるコードがどこにも無かった**
- さらに `transport input ssh telnet` は running-config に
  **ハードコード**されているだけで、コマンド自体が未実装だった

つまり「平文管理が有効かどうか」を表現する手段が無く、
所見が立つことも、直すこともできなかった。

実Telnetサーバと `transport input` を実装して、この運用ループが
本当に回るようにした:

```
transport input all   → 23番が開く   → 平文管理の所見が立つ
transport input ssh   → 23番が閉じる → 所見が消える
```

---

## 2. 有効にする

```
Switch(config)# line vty 0 4
Switch(config-line)# transport input all      ← telnet + ssh
```

止めるとき:

```
Switch(config-line)# transport input ssh      ← ssh のみ
Switch(config-line)# transport input none     ← どちらも不可
```

`show running-config` にそのまま出る（固定文字列ではなくなった）。

### 既定は「閉じている」— 実機との意図的な差

実機（IOS）の既定は **telnet 許可**だが、このエミュレータでは
**明示的に設定するまで23番は開かない**。

装置を作っただけで平文ポートが開くのは事故のもとで、
このリポジトリは実際にホスト上で本当にソケットを開くため。
実機の挙動を再現することより、意図しないポート開放を避けることを
優先している。

---

## 3. 対応している範囲

- Username/Password のログインプロンプト。3回間違えると切断
  （ユーザはローカルユーザ、無ければ `admin`/`admin`。
  SSH・NETCONFとまったく同じ関数を参照している）
- IAC（Telnetオプション交渉）の読み飛ばし。
  ECHO と SGA だけこちらから WILL を送る
- パスワード入力中のエコー抑制
- `/api/cli` と同じ経路を通す。Telnetで変えた設定がWeb UI側にも出る

## 4. 対応していない範囲（実機との差）

- **暗号化しない**。これは欠落ではなく仕様（平文であることが
  そもそもこの所見の中身）
- `line vty` 単位の同時接続数制限、`exec-timeout`、`access-class`
- `enable` による権限昇格
- 端末制御（カーソル移動・履歴・TAB補完）。行単位で読むだけ
- `line con` / `line aux`。`line vty` のみ

---

## 5. 脆弱性スキャンとの連動

Nexposeエミュレーションは、telnet資格情報を**実際に試す**。
通れば、平文のまま `show running-config` を読んで所見の判定に使う。

```
=== 資格情報なし（telnet開放中）===
   services: [('tcp', 23), ('udp', 161)]
   findings: ['netlab-telnet-cleartext']

=== 間違ったパスワードのtelnet資格情報 ===
   credentialStatus=credential-status-login-failed
     bad-tn  telnet  verified=False login failed

=== 正しいtelnet資格情報 ===
   credentialStatus=credential-status-success
     tn  telnet  verified=True
         authenticated on tcp/23 (cleartext), read running-config (3404 bytes)
   findings: ['netlab-telnet-cleartext', 'netlab-no-aaa-authentication']

=== telnetを止める ===
   credentialStatus=credential-status-service-not-found
     tn  telnet  verified=False no Telnet service found
   services: [('udp', 161)]
   findings: []
```

詳細は [`nexpose-api.md`](./nexpose-api.md)。

---

## 6. テスト

`tests/test_telnet_cli_server.py`（11件）。
本物の uvicorn サーバを立て、素のソケットでTelnetを喋る。

固定しているのは主にこの5点:

1. **既定では23番が開かないこと**
2. `transport input all` で開き、`ssh` / `none` で閉じること
3. **running-config が実際の設定を反映すること**
   （以前は固定文字列で、変えても出力が変わらなかった）
4. 正しいパスワードで通り、**間違ったパスワードは弾かれること**
5. **Telnetで変えた設定が `/api/cli` 側からも見えること**

```bash
python3 -m pytest tests/test_telnet_cli_server.py -q
```
