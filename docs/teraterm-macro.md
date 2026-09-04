# TeraTermマクロ — ログイン & ログ採取

実機へのログイン・ログ採取をTeraTermマクロ(`.ttl`)で行う際のメモ。
ここで採取したログは、Catalyst/Si-Rの実機比較や`tools/rag_query.py`
の参考資料として`docs/*.md`にそのまま貼れる。

## 多段接続版（踏み台経由）

ユーザーから共有された実マクロ(2026-09-04)を元にした構成。
1台目（踏み台）にログイン・enableした後、2台目（対象機器）へ
telnetで多段接続し、日付＋ホスト名でログファイル名を自動生成する
ところまでの「ログイン専用マクロ」。

```
connect '1.1.1.1:23 /nossh /2 /auth=password /user=admin /passwd=passowrd /nosecuritywarning'
;
; --- 1台目ログイン & enable ---
wait 'Password:'
sendln 'password'
wait '>'
;
sendln 'enable'
;
wait 'Password:'
sendln 'password'
wait Prompt
pause 1
wait Prompt
sendln ''
;
; --- 2台目ログイン & enable ---
sendln 'telnet 10.203.254.252'
mylogroot='c:\log\'
remotehostip='10.203.254.252'
remotehostname='RMT0354C'
Prompt='#'
;
;-------------------------------------------------------------------------------
;logsave
mylogname=mylogroot
strconcat mylogname remotehostname
strconcat mylogname '_'
;-------------------------------------------------------------------------------
; 日時の取得、ログファイル名の自動設定、ロギング開始
getdate mylogdate
strcopy mylogdate 1 4 myyear
strcopy mylogdate 6 2 mymo
strcopy mylogdate 9 2 myda
;
```

### 気になった点

- **`Prompt='#'`が2台目へのtelnet送信の後に定義されている。**
  TeraTermマクロは逐次実行のため、1台目の`wait Prompt`
  （6〜14行目相当）の時点ではまだ`Prompt`が未定義（空文字扱い）
  になる。先頭で`Prompt='>'`のような初期値を置いておくべき。
- **パスワードが平文でマクロに書かれている**（`passwd=passowrd`と
  typoもあり）。`passwordbox`コマンドで実行時に入力させる形の方が
  ファイルとして残るリスクを減らせる。
- 通信断時の`wait`タイムアウト処理（`timeout`変数）が今のところ
  見当たらない。

### この後に続くはずの部分（未作成・参考）

このマクロ自体は「ログイン専用」で、ここで止まっている。実際に
ログを採取するなら以下が続く想定。

```
strconcat mylogname myyear
strconcat mylogname mymo
strconcat mylogname myda
strconcat mylogname '.log'
logopen mylogname 0 1     ; ログ採取開始
;
; --- 2台目でenable ---
wait 'Password:'
sendln 'password'
wait Prompt
;
; --- 採取したいコマンドをここに ---
sendln 'show tech-support'
wait Prompt
;
logclose
sendln 'exit'
disconnect
```

## 単発接続版（踏み台なし）

多段版から中間ログイン（`telnet <対象IP>`の送信部分）を削るだけで
流用できる。`Prompt`の初期値も先頭に置いてあるので、多段版で
指摘した未定義問題も同時に解消している。

```
Prompt='>'
connect '10.203.254.252:23 /nossh /2 /auth=password /user=admin /passwd=password /nosecuritywarning'
;
wait 'Password:'
sendln 'password'
wait Prompt
;
sendln 'enable'
wait 'Password:'
sendln 'password'
Prompt='#'
wait Prompt
;
mylogname = 'c:\log\RMT0354C_'
getdate mylogdate
strcopy mylogdate 1 4 myyear
strcopy mylogdate 6 2 mymo
strcopy mylogdate 9 2 myda
strconcat mylogname myyear
strconcat mylogname mymo
strconcat mylogname myda
strconcat mylogname '.log'
logopen mylogname 0 1
```

## このマクロで採取したログの使い道

1. `logopen`〜`logclose`で保存された`.log`ファイルの中身を、
   このリポジトリの`docs/*.md`にコピペで貼る
   （`docs/real-device-comparison.md`と同じ流れ）
2. `tools/rag_query.py`はdocs配下をBM25で検索対象にするため、
   貼るだけで自動的にQwenへの参考資料になる
3. 実機とエミュレーターの出力差分があれば、これまでと同じ手順
   （Genieパーサーでのキー比較 or 目視diff）で突き合わせて修正できる
