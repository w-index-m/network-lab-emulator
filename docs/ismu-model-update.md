# ISMU（In-Service Model Update）— Catalyst / IOS-XE

ISSUが**ソフトウェアイメージ**を入れ替えるのに対し、ISMUはリロードせずに
**データモデル（YANG）だけ**を更新する仕組み。NETCONF/RESTCONFで使える
モデルを、フルアップグレードなしで追加・修正するために使う。

- 実装: `engine/rules.py`（`_cmd_ismu` / `_ismu_*`）
- テスト: `tests/test_ismu.py`（19件）
- 関連: `docs/netconf-catalyst.md`, `docs/model-based-aaa-nacm.md`

> **出典**: cisco.com はこの環境のegressプロキシでブロックされているため、
> 公式ガイドを直接参照できていない。コマンド体系・パッケージ命名規則・
> 「add/activate時にイメージとプラットフォームの一致を検査し、
> 食い違えば失敗する」という仕様は二次情報（下記「参考」）で確認した。
> `show install summary` の桁揃えなど細部の書式は IOS-XE の一般的な
> 表記に合わせている。公式マニュアルで差異が見つかったら直す前提。

## パッケージ命名規則

```
<プラットフォーム>-<ライセンス>.<リリース>.<DDTS ID>.dmp.bin
例) cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
```

`install` コマンドはISSU（イメージ）と共有しているため、実装では
**`.dmp.bin` を扱うISMUを先に判定し、対象外なら従来のISSU処理へ回す**。

## 対応コマンド

```
install add file <url> [activate] [commit]
install activate file <url> [commit]
install commit
install deactivate file <url>
install remove file <url>
install remove inactive
install rollback to committed
show install summary
show install package <url>
show install log
```

状態は実機と同じ4つ:

| St | 意味 |
|---|---|
| `I` | Inactive（addしただけ） |
| `U` | Activated & Uncommitted（未commit＝自動ロールバック対象） |
| `C` | Activated & Committed |
| `D` | Deactivated & Uncommitted |

## 実際の出力

### add → activate → commit

```
C9300# install add file flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
install_add: START flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
install_add: Adding DMP
--- Starting Add ---
Performing Add on all members
  [1] Add package(s) on switch 1
  [1] Finished Add on switch 1
Checking status of Add on [1]
Add: Passed on [1]
Finished Add

SUCCESS: install_add flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
```

```
C9300# show install summary
[ Switch 1 ] Installed Package(s) Information:
State (St): I - Inactive, U - Activated & Uncommitted,
            C - Activated & Committed, D - Deactivated & Uncommitted
------------------------------------------------------------------------------
Type  St   Filename/Version
------------------------------------------------------------------------------
DMP   I    flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
IMG   C    17.09.03
------------------------------------------------------------------------------
```

activate すると `U`、commit すると `C` になる。
イメージ更新と違い**リロードは不要**:

```
C9300# install activate file flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
install_activate: START flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
install_activate: Activating DMP
Following packages shall be activated:
  cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
Model update does not require a reload.
SUCCESS: install_activate cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin

※ commit するには "install commit" を実行してください（未commitは自動ロールバック対象）
```

一括指定もできる:

```
C9300# install add file flash:cat9k-...CSCvk58435.dmp.bin activate commit
```

### show install package

```
C9300# show install package flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
Package: cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
  Size: 184365
  Timestamp: 2026-09-11 05:23:00 UTC
  Canonical path: /flash/cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
  Raw disk-file SHA1sum: ee2dbe886a636175e010dfd404e6f36751d0dc89
  Header size: 1000 bytes
  Package type: DMP
  Package released: 17.09.03
  Package platform: cat9k
  Package DDTS: CSCvk58435
  Package state: Activated & Committed
```

## 実機同様の検証（重要）

add / activate 時にプラットフォームとイメージ版数を検査し、
食い違えば**インストールを失敗させる**:

```
C9300# install add file flash:isr4300-universalk9.17.09.03.CSCvk1.dmp.bin
FAILED: install_add : Platform mismatch. Package is for "isr4300", this device is "cat9k"

C9300# install add file flash:cat9k-universalk9.16.12.01.CSCvk2.dmp.bin
FAILED: install_add : Image version mismatch. Package targets 16.12.01, running version is 17.09.03

C9300# install add file flash:garbage.dmp.bin
FAILED: install_add : Invalid package name "garbage.dmp.bin".
        Expected <platform>-<license>.<release>.<DDTS>.dmp.bin
```

その他の異常系:

```
C9300# install add file <同じパッケージ>
FAILED: install_add : Package cat9k-...dmp.bin is already added

C9300# install activate file <未追加のパッケージ>
FAILED: install_activate : Package ... is not added. Run "install add file ..." first.

C9300# install remove file <activate済みのパッケージ>
FAILED: install_remove : cat9k-...dmp.bin is active. Deactivate it first.
```

## ロールバック

未commit（`U`）のパッケージは `install rollback to committed` で
`I` に戻る（commit済みの `C` は影響を受けない）:

```
C9300# install rollback to committed
install_rollback: START
  Rolling back cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin
SUCCESS: install_rollback to committed
```

## ハマりどころ

1. **`install` コマンドはISSUと共有。**
   `.dmp.bin` かどうかで振り分ける。`_cmd_ismu` が `None` を返したら
   従来のISSU（イメージ）処理へ落ちる、という二段構えにしている。
   `install commit` はISMUに未commitパッケージが無ければISSU側に渡す。
2. **ファイル名の大小文字を保つこと。**
   このコードベースは判定に小文字化した `c` を使う慣習だが、それを
   そのまま表示に使うと DDTS ID が `CSCvk58435` → `cscvk58435` になり
   実機と食い違う。元コマンド（`orig`）から取り直す
   （`_path()` ヘルパ）。同種のバグはZBFWやSi-Rの事前共有鍵でも
   起きているので、**ファイル名・鍵・名前を扱うときは必ず元文字列から**。
3. **`flash:` プレフィックスを外してからキーにする。**
   `path.split('/')[-1]` だけだと `flash:cat9k-...` がそのまま残り、
   `Canonical path: /flash/flash:cat9k-...` のように二重になる。

## 未対応（実機との差）

- 実際にYANGモデルが増減するわけではない（状態遷移と表示のみ）
- Auto abort timer（未commitの自動ロールバック猶予）のカウントダウン
- スタック構成での複数スイッチ個別ステータス（常に switch 1 として表示）
- `install prepare` / `install abort` のISMU版

## 参考

- [Upgrade Catalyst 9000 series (CiscoZine)](https://www.ciscozine.com/catalyst-9000-upgrade/)
- [How to Use the install commit Command in Cisco IOS XE Upgrades (router-switch.com)](https://www.router-switch.com/faq/how-to-use-install-commit-in-cisco-ios-xe-upgrades.html)
- [Catalyst 9300 Upgrading IOS-XE (Install Mode) (Apronets)](https://apronets.com/2019/10/24/catalyst-9300-upgrading-ios-xe-16-6-2-onward-install-mode/)
