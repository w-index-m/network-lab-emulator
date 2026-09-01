# Route Injector CLI

`tools/route_injector_cli.py`

## これは何か

`tools/route_injector/network_route_injector.py`（Windows GUI/Tkinter版の
経路配信・ネイバー確立ツール）のうち、**RIP と BGP** のロジックを
画面操作無しでコマンドラインから実行できるように移植したもの。

GUI版は起動そのものに Tkinter が必須（Tkinterが無い環境ではimportすら
失敗する）だが、本CLI版は Tkinter にも scapy にも依存しない標準ライブラリ
のみの実装なので、Linux/CI環境や自動化スクリプトからそのまま呼び出せる。

**GUI版のファイル自体は一切変更していない**。プロトコルのパケット構築
ロジック（RIP RTE組み立て、BGP OPEN/UPDATE組み立て、`BGPSpeaker`クラス）
を同一実装のまま新規ファイルに移植した。

## 使い方

```bash
# RIP: 経路を1件広告（UDP、宛先は特定ルーターのIPかマルチキャスト224.0.0.9）
python tools/route_injector_cli.py rip --dest 192.168.1.1 \
    --route 10.0.0.0/24:192.168.1.100:1

# BGP: eBGPネイバーを確立し経路を広告
python tools/route_injector_cli.py bgp --peer 192.168.1.1 \
    --local-as 65002 --remote-as 65001 --router-id 10.0.0.1 \
    --route 10.10.0.0/24 --next-hop 192.168.1.2 \
    --community 65002:100 --hold-seconds 5
```

RIPは `network/prefix:nexthop:metric[:tag]` 形式で `--route` を複数指定できる
（1パケット最大25件、RFC制限）。BGPは `--route network/prefix` を複数指定し、
`--as-path`、`--med`、`--local-pref`、`--community`（`65001:100`形式または
`no-export`等の既知名）に対応。

## 対応プロトコル

| プロトコル | 対応状況 |
|---|---|
| RIP v1/v2 | ✅ 対応 |
| BGP | ✅ 対応（OPEN/UPDATE/KEEPALIVE、community/MED/local-pref/AS_PATH） |
| OSPF | ❌ 未対応 |

OSPFはscapyでの生パケット構築（Hello→2-Way→ExStart→Exchange→Full）が必要で、
GUI版の実装がTkinterのイベントループ・タブクラスと密結合しているため、
今回のCLI移植では対象外とした。

## 検証方法

このリポジトリにはTkinter・scapyのどちらもインストールされていないため、
GUI版をimportして比較することはできない。代わりに以下で実際の動作を確認した:

**RIP**: パケット構築関数（`rip_build_rte`/`rip_build_packet`）の出力を
バイト単位でデコードし、AFI・タグ・ネットワーク・マスク・ネクストホップ・
メトリックが期待通りエンコードされていることを確認（`tests/test_route_injector_cli.py`）。
実UDP送受信は、送信元ポートもRIP仕様上520固定になるため、ループバック環境
特有のポート競合が起きやすく、バイト単位検証の方が確実と判断した。

**BGP**: 標準ライブラリのみで書いた**実TCPサーバー**（モックBGPピア）を
実際に起動し、本ツールで実際にTCP接続・OPEN送信・UPDATE送信を実行して、
相手側で本物のバイト列としてパースできることを確認:

```
$ python tools/route_injector_cli.py bgp --peer 127.0.0.1 --local-as 65002 \
    --remote-as 65001 --router-id 10.0.0.1 --route 10.10.0.0/24 \
    --next-hop 192.168.1.2 --community 65002:100
[OK] 広告: 10.10.0.0/24 next_hop=192.168.1.2 as_path=(empty)

モック側で受信・パース:
  received OPEN: as=65002 hold=30 router_id=10.0.0.1
  received type=4 (KEEPALIVE)
  received type=2 (UPDATE), parsed routes: ['10.10.0.0/24']
```

## テスト

```bash
pytest tests/test_route_injector_cli.py -v
# 11/11 成功（BGPは実TCPソケットでの統合テストを含む）
```

## 制約

- OSPF未対応（上記の通り）
- FlowSpec（GUI版の目玉機能の一つ）は移植していない。必要であれば
  GUI版の`bgp_build_flowspec_*`関数群を同様に移植可能
- 実運用環境への投入は検証・研修用途のみ。対象ネットワーク管理者の許可
  なく実施しないこと（GUI版と同じ免責）
