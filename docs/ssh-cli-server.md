# 実SSHサーバ（TCP/22）— CLIをSSH越しに提供する

本物のSSHクライアントで装置にログインして、CLIを叩ける。

> 動作確認は paramiko のクライアントで行っている。
> OpenSSH の `ssh` コマンドとの相互接続は未検証（§5参照）。

```
$ ssh netadmin@10.222.0.1
SSH-SW# show ip interface brief
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet1/0/1   10.222.0.1      YES NVRAM   up                    up

SSH-SW# configure terminal
Enter configuration commands, one per line.  End with CNTL/Z.
SSH-SW(config)# hostname RENAMED-BY-SSH
RENAMED-BY-SSH(config)# end
RENAMED-BY-SSH#
```

`ssh host "command"` の形（execチャンネル）にも対応している。

```
$ ssh netadmin@10.222.0.1 "show running-config"
```

---

## 1. なぜ必要だったか

それまでCLIは HTTP の `/api/cli` からしか叩けなかった。
NETCONFサーバ(830)はあるが `netconf` サブシステムしか受け付けないので、
**本物のSSHクライアントでログインして show コマンドを打つ手段が無かった**。

直接のきっかけは Nexpose エミュレーションで、認証スキャンが

- ログインは本物のSSH認証
- しかしその後の読み取りは `DeviceState` を直接覗く

という中途半端な状態だったこと。実SSHシェルがあれば、スキャナは実機と
同じく「SSHでログインして `show running-config` を実行して解析する」に
なる。実際そうした（[`nexpose-api.md`](./nexpose-api.md) の §3.5）。

---

## 2. 有効にする

実機と同じく、**RSA鍵を生成するまでSSHは起動しない**。

```
Switch(config)# crypto key generate rsa modulus 2048
The name for the keys will be: SSH-SW.netlab
% The key modulus size is 2048 bits
% Generating 2048 bit RSA keys, keys will be non-exportable...
[OK]
```

これで装置の管理IPの TCP/22 が待ち受けを始める。
止めるときは実機同様 `crypto key zeroize rsa`。

装置ごとに勝手に22番を開けたりはしない（全装置で開けると、
何もしていないのにポートが開いている状態になって不自然）。

### ログイン情報

`username` で作ったローカルユーザ。1つも無ければ `admin` / `admin`。
NETCONFサーバとまったく同じ規則を**同じ関数で**参照しているので、
「SSHは通るがNETCONFは通らない」のような食い違いは起きない。

### 公開鍵認証

実機同様 `ip ssh pubkey-chain` で登録した鍵でもログインできる。
RSA / Ed25519 / ECDSA に対応（`ssh-dss` はクラス自体は用意しているが、
paramiko 4.0 でDSA/DSSサポートが落ちたため実質使えない）。

```
Switch(config)# ip ssh pubkey-chain
Switch(conf-ssh-pubkey)# username netadmin
Switch(conf-ssh-pubkey-user)# key-string
Switch(conf-ssh-pubkey-user-key)# AAAAB3NzaC1yc2EAAAADAQABAAABgQDBGV...
Switch(conf-ssh-pubkey-user-key)# exit
Switch(conf-ssh-pubkey-user)# exit
Switch(conf-ssh-pubkey)# exit
```

`key-string` の配下は「行をそのまま貼り付ける」特殊な入力モードで、
`exit`/`end` を打つまではコマンドとして解釈されない（実機と同じ）。
base64本体は複数行に分けて貼ってよい（1行の折り返し幅は問わない）。

実機は base64 本体だけを貼らせる方式だが、`~/.ssh/id_rsa.pub` の中身を
そのまま貼りたくなるのが自然なので、`ssh-rsa AAAA... comment` という
OpenSSH形式の1行を貼っても受け付ける（意図的な緩和）。

鍵の種類（RSA/Ed25519/ECDSA）は明示的に書かせるのではなく、
**base64を復号したバイト列自身から読み取る**。SSHの鍵のワイヤ形式は
自己記述的（先頭にアルゴリズム名の文字列が入っている）なので、それを見る。
最初は無条件に `ssh-rsa` を仮定していたため、Ed25519/ECDSA鍵が
常に「RSAとして解釈できない」で弾かれる不具合があった。

`no username <name>` でその名前に登録された鍵を丸ごと失効できる。

---

## 3. 作り

```
SSHクライアント → SshCliServer (paramiko, 別スレッド)
                     ↓ run_coroutine_threadsafe
                  app.py の cli_command()  ← /api/cli と同じ経路
                     ↓
                  DeviceState
```

**`/api/cli` と同じ経路を通すことが肝**で、ここで別実装を作ると
SSH越しの結果とWeb UIの結果が食い違う。実際
`tests/test_ssh_cli_server.py` で、SSHで `hostname` を変えたら
`/api/cli` 側の `show running-config` にも出ることを固定している。

CLI処理はイベントループ上の非同期関数なので、SSHのワーカースレッドからは
`asyncio.run_coroutine_threadsafe` で投げて結果を待つ。
そのループは `lifespan` で捕まえている。

> このアプリは `lifespan` を使っているので `@app.on_event("startup")` は
> **呼ばれない**。最初そちらに書いてしまい、ループが `None` のまま
> `% CLI is not available` を返していた。

---

## 4. 対応している範囲

- password認証（ローカルユーザ、無ければ admin/admin）
- **公開鍵認証**（`ip ssh pubkey-chain`。RSA/Ed25519/ECDSA）
- shell チャンネル（対話）と exec チャンネル（`ssh host "..."`）
- プロンプト（`host#` / `host(config)#` / `host(config-if)#`）とモード遷移
- Backspace / Ctrl-C / Ctrl-D / `exit` での切断
- ポートスキャン対策: 接続してすぐ切る相手は静かに落とす
  （RFC 4253 では双方が接続直後に識別文字列を送るので、
  何も送らず切る相手はSSHではない）

## 5. 対応していない範囲（実機との差）

- **`enable` による権限昇格**、AAA連携（TACACS+/RADIUS でのログイン認証）
- 端末制御。カーソル移動・履歴・TAB補完は無く、行単位で読むだけ
- **ホスト鍵はプロセス内で1本を共有**する。装置ごとに2048bitの鍵を
  生成すると装置作成が目に見えて遅くなるため。実機は装置ごとに別鍵なので、
  ホスト鍵で装置を見分けることはできない
- `line vty` の `transport input` / `access-class` は見ていない
  （SSHの可否はRSA鍵の有無だけで決まる）
- SCP / SFTP、ポートフォワード
- `key-hash`（すでにSCPで転送済みの鍵をハッシュ値で参照する方式）。
  `key-string` で本体を貼る方式のみ

### 検証できていないこと

動作確認は **paramiko のクライアント**で行っている（モックではなく
実装の異なる本物のSSHクライアントだが、サーバ側と同じライブラリ）。
**OpenSSH の `ssh` コマンドとの相互接続は未検証** — 開発環境に
`ssh` クライアントが入っておらず試せていない。OpenSSH は鍵交換・
暗号方式の選択が paramiko より厳しいので、実際に繋ぐと
アルゴリズムの調整が要る可能性がある。

---

## 6. テスト

`tests/test_ssh_cli_server.py`（21件）。本物の uvicorn サーバを
サブプロセスで立て、paramiko のクライアントで実際にログインする。

固定しているのは主にこの7点:

1. **RSA鍵を作るまで22番が開かないこと**、`zeroize` で閉じること
2. 正しいパスワードで通り、**間違ったパスワードは弾かれること**
3. exec と shell の両方が動き、**1本の接続で続けて開けること**
   （チャンネルを1本処理して transport ごと閉じており、2本目が
   `SSH session not active` で失敗していた）
4. プロンプトがモードに追従すること
5. **SSHで変えた設定が `/api/cli` 側からも見えること**
6. **登録した公開鍵でログインでき、登録していない鍵・別ユーザ名に
   登録した鍵は拒否されること**。RSA / Ed25519 の両方で確認
   （Ed25519は生成直後、算出したアルゴリズムを 'ssh-rsa' 決め打ちで
   誤判定していた回帰）
7. `no username` で鍵が失効すること、公開鍵を登録してもパスワード
   認証が塞がれないこと

```bash
python3 -m pytest tests/test_ssh_cli_server.py -q
```
