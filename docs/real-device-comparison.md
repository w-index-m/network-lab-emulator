# 実機との出力突き合わせ（Catalyst 3650 / IOS-XE 16.12.11）

## 何をしたか

実機 **Cisco Catalyst WS-C3650-24TD（IOS-XE 16.12.11）** の
`show` 系出力を入手し、エミュレーターの同じコマンドの出力と
突き合わせて、書式・項目の食い違いを洗い出した。

自己完結したテストでは「正しい姿」が分からないため見つからない類の
バグが多数出た。実機は**その正解**を持っている。

## 方法1: Genieパーサーでキーを差分比較（推奨）

実機に接続できない環境でも突き合わせられる。Genieは実機の書式を前提に
書かれた**Cisco公式パーサー**なので、「パースは通るがキーが取れない」
という形で欠落を数値化できる。

```python
from unittest.mock import Mock
from genie.libs.parser.iosxe.show_platform import ShowVersion

dev = Mock()
dev.execute = Mock(return_value=open('real_show_version.txt').read())
parsed = ShowVersion(device=dev).parse()
```

実機側とエミュレーター側の両方をこれに通し、キー集合の差を取る。
比較スクリプトの例は `/tmp/cmp/parse_both.py`（本ドキュメント末尾に再掲）。

## 方法2: 目視比較

書式の崩れ（列幅・区切り文字）はGenieが吸収してしまうことがあるため、
raw出力の目視も併用する。

## 見つかった食い違いと修正（2026-09-03）

### `show version` — Genieの抽出キーが27件欠落

機種違い（実機3650 / エミュC9300）による値の差は当然として、
**機種に依らずIOS-XEなら必ず出る項目が丸ごと無かった**。

| 欠けていたキー | 元になる行 |
|---|---|
| `system_image` | `System image file is "..."` |
| `rom` / `bootldr` | `ROM:` / `BOOTLDR:` |
| `compiled_date` / `compiled_by` | `Compiled <日付> by <人>` |
| `last_reload_reason` / `returned_to_rom_by` | `Last reload reason:` 等 |
| `uptime_this_cp` | `Uptime for this control processor is` |
| `license_package.*` | `Technology Package License Information:` ブロック |
| `disks.*` | `NNNK bytes of Flash at flash:.` |
| `next_config_register` | `... (will be 0x102 at next reload)` |
| `switch_num.*.mb_sn` 等 | `Motherboard Serial Number` 等 |

さらに **`version.os` が実機 `IOS-XE` に対しエミュレーターは `IOS`**
だった（`Cisco IOS-XE software, Copyright ...` の行が無いため）。
OSで分岐する自動化スクリプトの挙動が変わる。

→ 修正後、機種非依存の欠落はゼロ（27件 → 9件、残りは全て3650固有の値）。

### `show ip route` — 実機に存在しない表記

```
修正前: C     192.168.10.0/24 [0/0] via directly, Vlan10
実機  : C        192.168.1.0/24 is directly connected, Vlan1
```

connected経路を静的経路と同じテンプレートで描画していたため、
next-hopとして持っていた `'directly'` がそのまま出ていた。

これを追うと**さらに別の問題**が出た。インターフェースの直結
ネットワークは `_register_icmp` が `ad=0 / next_hop='0.0.0.0'` で
登録するが、これがstatic扱いのままだったため:

```
修正前: S     10.9.9.0/24 [0/0] via 0.0.0.0,      ← インターフェース名も空
実機  : C        10.9.9.0/24 is directly connected, GigabitEthernet1/0/1
```

加えて以下が欠けていた:

- **`L`(local) 経路**: 実機は各インターフェースIPの `/32` を必ず持つ
- **`Gateway of last resort is ...`** 行
- 候補デフォルトルートの **`*`**（`S*` 表記）

### `show vlan brief` — VLAN1のPorts欄が常に空

実機はどのVLANにも割り当てていないaccessポートを**全てVLAN1に置く**。
エミュレーターは明示的に設定したポートしか持っていなかったため、
VLAN1が空欄で `vlans.1.interfaces` も取れなかった。
また実機が必ず持つ **1002-1005**（fddi-default等）も無かった。

→ 未割当ポートをVLAN1に表示（実機同様に列幅で折り返し）、
1002-1005を追加。

### `show mac address-table` — 装置の出力に説明文が混入

```
修正前: (動的に学習されたエントリはありません — pingやトラフィックで学習されます)
実機  : （ヘッダと合計行だけ）
```

日本語の案内文が出力に混ざっており、どんなパーサーも壊れる。

### `show lldp neighbors` — 未有効なのにテーブルを表示

```
実機  : % LLDP is not enabled
修正前: Capability codes: ... / Device ID  Local Intf ...（表を表示）
```

LLDPは実機では既定で無効。`lldp run` / `no lldp run` を実装し、
未有効時は実機同様のメッセージを返すようにした。
（先に修正したEIGRPの幽霊ネイバーと同じ構造の問題）

### MACアドレスの表記がコロン区切り

```
実機  : Internet  192.168.1.10   -   00a6.ca54.3647  ARPA   Vlan1
修正前: Internet  10.9.9.1       -   00:1b:0d:88:d0:4d  ARPA   Gi...
```

**Ciscoの出力にコロン区切りは存在しない**。17文字と14文字で桁数も
違うため列がずれる。`show ip arp` / `show spanning-tree` /
`show mac address-table` に共通ヘルパー `cisco_mac()` を適用した。

### `show spanning-tree` — Priorityが空欄

```
実機  : Root ID    Priority    32769
        Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
修正前: Root ID    Priority              ← 空欄
        Bridge ID  Priority    32768 (priority 32768 sys-id-ext 10)
```

実機は `priority + sys-id-ext(VLAN番号)` を表示する。
VLAN10なら 32778 になるべきところ、Root IDは空欄、Bridge IDは
VLAN番号を足していなかった。

## まだ直していない差（優先度低）

- `show mac address-table` の **STATIC / CPU エントリ**（実機は
  `0100.0ccc.cccc`(CDP)、`0180.c200.000x`(STP/LLDP) 等の予約
  マルチキャストMACを21件持つ）
- `show etherchannel summary` の Flags 説明が実機9行に対し2行。
  `Group Port-channel Protocol Ports` のヘッダ行も無い
- `show cdp neighbors` の Capability Codes が実機3行に対し2行
  （`P - Phone, D - Remote, C - CVTA, M - Two-port Mac Relay` が不足）

## テスト

```bash
pytest tests/test_show_version_fidelity.py -v   # 17件
pytest tests/test_show_output_fidelity.py -v    # 17件
```

Genie本体はこのリポジトリ側の実行環境に無いため、パーサーが手掛かりに
する「行」が出力に含まれるかを直接検証している。

## 比較スクリプト

```python
# 実機とエミュレーターの出力を同じGenieパーサーに通してキーを比較する
from unittest.mock import Mock
from genie.libs.parser.iosxe.show_platform import ShowVersion

def parse(path):
    dev = Mock()
    dev.execute = Mock(return_value=open(path).read())
    return ShowVersion(device=dev).parse()

def flatten(d, prefix=''):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f'{prefix}.{k}' if prefix else str(k)))
    else:
        out[prefix] = d
    return out

real = flatten(parse('real_show_version.txt'))
emu  = flatten(parse('emu_show_version.txt'))
print('実機だけが持つキー:', sorted(set(real) - set(emu)))
```

`show ip route` / `show vlan` / `show interfaces status` も同様に
`genie.libs.parser.iosxe` 配下の対応クラスで比較できる
（`ShowIpRoute` / `ShowVlan` / `ShowInterfacesStatus`）。
