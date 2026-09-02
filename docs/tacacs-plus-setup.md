# TACACS+ (tac_plus-ng) セットアップ + コマンド認可/アカウンティング検証

## サーバー本体の取得・ビルド

`tac_plus-ng`（[MarcJHuber/event-driven-servers](https://github.com/MarcJHuber/event-driven-servers)）
は旧Shrubbery社版tac_plusの後継として現在も活発にメンテナンスされて
いるフリー(BSD系ライセンス)のTACACS+/RADIUSサーバー。GitHub上はソース
のみでReleaseアセットが無いため`git clone`で取得する。

```bash
git clone https://github.com/MarcJHuber/event-driven-servers
cd event-driven-servers

apt-get install -y libpcre2-dev libc-ares-dev libssl-dev
./configure
make -j$(nproc)
```

`tac_plus-ng/`配下に`tac_plus-ng`本体と`tactester`（テスト用TACACS+
クライアント）ができる。ビルド後は`libmavis.so`が
`build/<platform>/fakeroot/usr/local/lib`にあるため、実行時に
`LD_LIBRARY_PATH`を通す必要がある。

## 起動

最小構成の設定例（`tac_plus-ng-cmds.cfg`）:

```
id = spawnd {
	background = no
	listen { port = 4949 }
	spawn { instances min = 1; instances max = 32 }
}

id = tac_plus-ng {
	log authorlog { destination = "/path/to/author.log" }
	log acctlog { destination = "/path/to/accounting.log" }
	authorization log = authorlog
	accounting log = acctlog

	host world { address = 0.0.0.0/0; key = demo }

	profile admin {
		enable 15 = login
		script {
			if (service == shell) {
			    if (cmd == "") set priv-lvl = 15
			    permit
			}
		}
	}

	user demo {
		password login = clear demo
		password pap = login
		profile = admin
	}
}
```

```bash
LD_LIBRARY_PATH=<build>/fakeroot/usr/local/lib \
  ./build/<platform>/tac_plus-ng/tac_plus-ng -f tac_plus-ng-cmds.cfg
```

## 動作確認1: 認証 (authentication)

`tactester`で実際にログイン認証:

```bash
LD_LIBRARY_PATH=<...>/fakeroot/usr/local/lib \
  CONFDIR=<config dir> ./build/<platform>/tactester/tactester \
  -C tactester.cfg -s tacacs.tcp -m authc -u demo -p demo -S login -A ascii
# -> "authc ack"（正しいパスワード）
# -> "authc nak"（間違ったパスワード）
```

## 動作確認2: Nexus投入コマンドのTACACS+認可・アカウンティング転送

network-lab-emulator自体はまだTACACS+クライアント機能を持たない
（装置側から実際にAAAサーバーへ問い合わせる実装は無い）。この制約を
明示した上で、`tools/nexus_cmd_to_tacacs.py`が投入コマンドを実際の
TACACS+ワイヤプロトコルでtac_plus-ngへ転送する（PyPIの`tacacs_plus`
ライブラリ使用）。

```bash
pip install tacacs_plus

# 1. Nexusにコマンドを投入
curl -X POST http://localhost:8000/api/cli -H "Content-Type: application/json" \
  -d '{"device_id":"nexus","command":"show ip interface brief"}'

# 2. そのコマンドをTACACS+へ転送(認可+アカウンティング)
python tools/nexus_cmd_to_tacacs.py \
  --tacacs-host 127.0.0.1 --tacacs-key demo \
  --device nexus --user demo --command "show ip interface brief"
```

### 見つけて直した実バグ

1. **`arguments`はbytesである必要がある**: `tacacs_plus`ライブラリの
   `authorize()`/`account()`に`str`のリストを渡すと
   `argument for 's' must be a bytes object`で例外。`.encode()`で解決。
2. **AV-pairに`service=shell`が無いと常にdeny**: TACACS+の認可リクエスト
   ヘッダにある`authen_service`（ライブラリが内部固定で送る
   `TAC_PLUS_AUTHEN_SVC_LOGIN`）と、tac_plus-ng側の`if (service == shell)`
   スクリプトが参照する**AV-pairとしての`service=shell`は別物**。
   `arguments`に`service=shell`を明示的に含めないと、サーバー側の
   スクリプトが`service == shell`の判定を通らず問答無用でdenyになる。

### 実際に確認した結果

サーバー側ログ（`author.log`）:
```
2026-09-02 07:16:54 +0000 127.0.0.1  demo  python_tty0  nexus  admin  permit  shell  show ip interface brief <cr>
```

サーバー側ログ（`accounting.log`）:
```
2026-09-02 07:16:54 +0000 127.0.0.1  demo  python_tty0  nexus  start  shell  show ip interface brief <cr>
2026-09-02 07:16:54 +0000 127.0.0.1  demo  python_tty0  nexus  stop   shell  show ip interface brief <cr>
```

Nexusから投入したコマンドが実際にTACACS+サーバーへ届き、認可結果
（permit）とアカウンティング記録（start/stop）の両方がサーバー側の
ログファイルに正しく記録されることを確認済み。

## ログイン/ログアウトのアカウンティング

`aaa accounting commands default group <name>`（コマンド単位=操作ログ）
とは別に、`aaa accounting default group <name>`（`commands`キーワード
無し）でEXECセッション（ログイン/ログアウト）単位のアカウンティングを
設定できる。両方共存可能で、`show running-config`にも両方反映される。

```
aaa accounting default group TAC-GROUP           ! ログイン/ログアウト
aaa accounting commands default group TAC-GROUP  ! 個々の操作コマンド
```

`tools/nexus_cmd_to_tacacs.py`に`--session login`/`--session logout`
オプションを追加し、実際にTACACS+サーバーへログイン/ログアウトの
アカウンティングを送信できる:

```bash
python tools/nexus_cmd_to_tacacs.py --tacacs-host 127.0.0.1 --tacacs-key demo \
  --device nexus --user demo --session login
python tools/nexus_cmd_to_tacacs.py --tacacs-host 127.0.0.1 --tacacs-key demo \
  --device nexus --user demo --session logout
```

実際に送信し、サーバー側`accounting.log`に記録されることを確認済み:

```
2026-09-02 07:36:17 +0000  127.0.0.1  demo  python_tty0  nexus  start  shell
2026-09-02 07:36:17 +0000  127.0.0.1  demo  python_tty0  nexus  stop   shell
```

コマンド欄が空であることから、個別コマンドのアカウンティング
（`show ip interface brief <cr>`等が入る）とは区別されたセッション単位の
記録であることが分かる。

## 制約

- network-lab-emulator本体にはTACACS+クライアント機能が無いため、
  この連携は「装置投入コマンドを別ツールで拾ってTACACS+に転送する」
  形（`tools/nexus_cmd_to_tacacs.py`を手動/スクリプトで呼ぶ）に
  なっている。エミュレーター側で`aaa authorization commands`等の設定
  を入れて自動的に全コマンドをTACACS+へ問い合わせる、という本格的な
  統合は未実装（今後の拡張ポイント）
