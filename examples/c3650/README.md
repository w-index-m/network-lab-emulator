# Catalyst 3650 実機で STP/OSPF を試す（3台構成）

実機 Catalyst 3650 を netmiko(`cisco_ios`) で投入・検証するための雛形一式。

## 構成イメージ（3台トライアングル）

```
        sw1 (STPルート/最小priority)
       /   \
     sw2 --- sw3   ← どこか1ポートが Blocking(BLK)、ループ遮断
```
- L2: 各リンクをトランクにして VLAN10 を通す → STPでループ収束
- L3: 各SVI(Vlan10)に OSPF を載せて相互に FULL 隣接

## 手順

### 1. 各3650でSSHを有効化（コンソールから一度だけ）
`bootstrap-ssh.cfg` を投入（hostname/管理IPは各機体で変える）。

### 2. 本ツールで3台トポロジを作り、3650向けにエクスポート
```bash
python tools/eveng_deploy.py export \
    --api http://127.0.0.1:8099 --out ./c3650_out --platform c3650
```
`--platform c3650` でインターフェース名を3650体系へ変換します。
- catalyst機器（元から `Gi1/0/x`）は**そのまま**（実質no-op）。
- ルータ流の `Gi0/0/x` / `Fa0/x` は**出現順に1始まりで採番**して
  `Gi1/0/1, Gi1/0/2, ...`（TenGigは `Te1/1/1...`）へ写します。
  ポート0や重複衝突を避けるための安全側変換で、変換表は実行時に
  `IF <元> -> <先>` として表示されます（`topology.json` にも同じ変換を反映）。
- 実ポート番号は物理配線に合わせて最終確認してください。

### 3. inventory.json を実機に合わせて編集（host=各3650の管理IP）

### 4. 投入 → 検証
```bash
python tools/eveng_deploy.py deploy --inventory ./c3650_out/inventory.json
python tools/eveng_deploy.py verify --inventory ./c3650_out/inventory.json \
    --checks examples/c3650/checks.stp-ospf.json
```

## 検証は SSH と RESTCONF を混在できる

`checks.json` の各項目は**キーで自動振り分け**されます。

| 項目形式 | 経路 | 例 |
|---|---|---|
| `{"cmd": ..., "expect": ...}` | SSH(netmiko) | show出力に文字列が含まれるか |
| `{"path": ..., "expect": ...}` | RESTCONF(GET) | 返却JSONに文字列が含まれるか |
| `{"path": ..., "all_equal": {"key","value"}}` | RESTCONF(GET) | 指定キーの全値が期待値と一致か |

RESTCONF版のサンプルは `checks.restconf.json`。OSPF隣接を
`Cisco-IOS-XE-ospf-oper` から取り、**全隣接の `adjacency-state` が `full`** かを
構造で判定します（文字列grepより堅い）。RESTCONF利用時は 3650 で
`restconf` / `ip http secure-server` を有効化し、`pip install requests` が必要です。

```bash
python tools/eveng_deploy.py verify --inventory ./c3650_out/inventory.json \
    --checks examples/c3650/checks.restconf.json
```

> RESTCONFのモデルパスはIOS-XE版で差異あり。合わない場合は実機で
> `GET /restconf/data/ietf-yang-library:modules-state` を叩き搭載モデルを確認。
> 詳細は `docs/c3650-api.md`。

## checks.stp-ospf.json（SSHのみ版）が見ているもの

| 機器 | チェック | 期待 |
|---|---|---|
| sw1 | `show spanning-tree vlan 10` | `This bridge is the root`（ルート選出） |
| sw3 | `show spanning-tree vlan 10` | `BLK`（Blockingポートでループ遮断） |
| 全機 | `show ip ospf neighbor` | `FULL`（隣接確立） |
| 全機 | `show ip route ospf` | `O`（OSPF学習経路あり） |

> 期待文字列はIOS-XEの標準出力に合わせています。ルート機を変える等で
> `sw1/sw3` の役割が入れ替わる場合は expect を調整してください。

## 実機ならではの注意
- STPのBlockingは**ループ（2リンク以上）が要る**ので必ず3台を三角結線。
- OSPF隣接は**両端のSVIが同一サブネット・`ip routing`有効**が前提。
- 3650のポートは `Gi1/0/1〜48` / `Te1/1/1〜4`。使う物理ポートに合わせて
  エクスポート後の `interface` 行を最終確認。
