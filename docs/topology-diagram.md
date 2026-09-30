# CDP/LLDP自動発見によるトポロジー図生成

`tools/topology_diagram.py` / `GET /api/topology/neighbors`

## これは何か

各装置が実際に持っているCDP/LLDPの隣接情報（`DeviceState.cdp_neighbors`
/`.lldp_neighbors` — `/api/link`でリンクを張った時点で自動的に
更新される）をAPI経由で集めて、ネットワークトポロジー図
（Mermaid/Graphviz DOT）を自動生成する。NetBox/LibreNMS等の
実機トポロジー自動発見ツールと同じ考え方で、**あくまでネイバー
広告から分かる範囲**しか描けない（リンクの物理配線そのものを
見ているわけではない）。

## API: `GET /api/topology/neighbors`

```json
{
  "devices": {
    "td-a": {
      "hostname": "Dist-A", "type": "catalyst",
      "cdp_neighbors": [{"device": "Core-C", "local_if": "GigabitEthernet1/0/1",
                          "remote_if": "Ethernet1/1", "platform": "NEXUS"}],
      "lldp_neighbors": [...]
    }
  },
  "edges": [
    {"a": "td-a", "a_if": "GigabitEthernet1/0/1", "b": "td-c", "b_if": "Ethernet1/1"}
  ]
}
```

`edges`は同じリンクを両端から二重に持たないよう重複排除している
（`td-a`視点と`td-c`視点の両方にネイバー情報が載っていても1本として
扱う）。

### CDPとLLDPの扱いの違い（実機と同じ制約）

- **CDP**: Cisco系（`catalyst`/`cisco`/`srs`）同士のリンクでしか
  成立しない
- **LLDP**: 全機種共通（ベンダーを問わない標準規格なので）

そのため、例えばCatalystとNexusの間のリンクは`cdp_neighbors`には
現れず、**LLDPだけがそのリンクを教えてくれる**。これは実機でも
全く同じで、異なるベンダーの機器を混在させたネットワークで
トポロジーを自動発見したい場合は結局LLDPが頼りになる、という
現実の運用の縮図になっている。

## 使い方

```bash
# Mermaid形式で標準出力へ
python3 tools/topology_diagram.py --emulator-url http://localhost:8000

# Graphviz DOT形式でファイルへ
python3 tools/topology_diagram.py --format dot --out topology.dot
```

## 実際に動かして確認した結果

エミュレータを起動し、Catalyst 2台（Dist-A/Dist-B）をNexus 1台
（Core-C）にリンクさせた状態で実行した実際の出力：

```
graph LR
    td-a["🖧 Dist-A\n(catalyst)"]
    td-b["🖧 Dist-B\n(catalyst)"]
    td-c["🗄️ Core-C\n(nexus)"]
    td-a ---|"Gi1/0/1 - Eth1/1"| td-c
    td-b ---|"Gi1/0/1 - Eth1/1"| td-c
```

この構成では`cdp_neighbors`は空（CatalystとNexus間なのでCDPが
成立しない）で、エッジは全て**LLDPから検出**されたものだった。
インタフェース名も`GigabitEthernet1/0/1`→`Gi1/0/1`のように
実機の`show cdp neighbors`と同じ短縮表記にしている。

## テスト

```bash
pytest tests/test_topology_diagram.py -v
# 8/8 成功
```

固定している内容：
1. Cisco系同士のリンクはCDPにもエッジにも現れること
2. 同じリンクが往復で二重にエッジ化されないこと
3. **異なるベンダー間のリンクはCDPには現れず、LLDPだけで
   検出できること**（このツールの存在意義に関わる制約）
4. リンクの無い装置にネイバーが無いこと
5. Mermaid/Graphviz DOT双方の出力にノード・エッジ・インタフェース
   ラベルが正しく含まれること
6. インタフェース名の短縮ロジック（`GigabitEthernet`→`Gi`等）

## 制約・今後の拡張余地

- CDP/LLDPが無効な装置、あるいはリンクそのものが無い区間は
  一切見えない（実機のトポロジー自動発見ツールと同じ制約）
- 装置の配置（レイアウト）はMermaid/Graphvizのレンダラ任せで、
  物理的なラック配置やフロア図とは無関係
- SNMPの`lldpRemTable`/`cdpCacheTable`経由（実プロトコルでの発見）
  ではなく、このエミュレータ内部の`DeviceState`を直接読んでいる。
  実機に対して本当にSNMPでトポロジーを発見したい場合は別途
  `engine/snmp_udp_agent.py`側にこれらのMIBを実装する必要がある
