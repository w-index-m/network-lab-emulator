# このプロジェクトの立ち位置: NIRVANA改のリイマジン

「10年くらい前にあったソフト(NICT の NIRVANA / NIRVANA改)を AI で復活させることを何と言うか」という質問への答えをまとめたもの。

## 一言で言うと

> 論文と公開資料をもとに、AI と一緒にクリーンルームで再実装・リイマジンした、NIRVANA改のオマージュ。

外に出すときは「NIRVANA改に着想を得た」と書き、製品名としては名乗らない
(NIRVANA / NIRVANA改 は NICT の名称。このプロジェクトの画面の名前・ロゴは独自のもの)。

## 言葉の整理

### 今回やったことに一番近い言い方

| 言葉 | 意味 | このプロジェクトとの関係 |
|---|---|---|
| **クリーンルーム再実装**(clean-room reimplementation) | 元のソースコードを見ずに、論文・写真・公開情報だけから同じ考え方のものを作り直すこと | NIRVANA改は一般配布されていないので、まさにこれ |
| **リイマジン**(re-imagining) | 昔の作品を、今の技術や解釈で作り直すこと。中身をそのまま戻すのではなく「今の時代ならこうなる」版 | 下の「今の時代ならではの部分」がこれにあたる |
| **オマージュ** | 元への敬意を込めて、似た雰囲気で作るもの | 名前・ロゴは元のものを使わず独自にしている、という立ち位置 |

### AI で復活させることを指して使われる言い方

| 言葉 | 意味 |
|---|---|
| **AI リバイバル / AI 復刻** | 昔のソフト・ゲーム・映像を AI で甦らせる、という意味で一般に使われる |
| **AI 支援による再実装**(AI-assisted reimplementation) | 技術的な文脈での言い方 |
| **レガシーモダナイゼーション** | 古いコードが手元にあり、それを AI で新しい言語・基盤に移す場合の業界用語。今回は元のコードが無いので厳密には当てはまらない |
| **ソフトウェア考古学**(software archaeology) | 失われた・資料の少ないソフトを、残った論文や画面写真から掘り起こして理解すること。今回の最初の段階はこれに近い |

## このプロジェクトでの「リイマジン」の部分

元の NIRVANA改 の考え方(IP アドレス空間の立体表示、通信の粒、警報の「警」と花びら、組織ごとの攻撃の進行度など)は
論文・写真から読み取って再現し、そのうえで「今の時代ならこうなる」として次を足した。

| 足したもの | 内容 | 詳細 |
|---|---|---|
| Prometheus(ヴァルハラ)・Loki 連携 | 今の標準的な監視基盤から流量・IF 状態・ログを読む。ニルヴァーナの判定も `/metrics` で Prometheus へ渡す | [valhalla-nirvana.md](valhalla-nirvana.md)、[cyber-view-3d.md](cyber-view-3d.md) |
| Grafana 相当の BOARD / EXPLORE / ALERTS | ダッシュボード・PromQL / LogQL・アラートルールを同じ画面で | [nirvana-board.md](nirvana-board.md) |
| 地球儀(WORLD) | クラウド(AWS / Google Cloud)のリージョン別の流量、Cloudflare Radar、IIJ IHR、MAWI、RIPE Atlas の都市間経路、攻撃元の都市 | [world-view.md](world-view.md) |
| AI 要約 | 取得したログの解釈を LLM(Ollama / Groq など)に任せる | [syslog-anomaly-detection.md](syslog-anomaly-detection.md) |
| 遮断・隔離(RESPONSE)、ATT&CK、ダークネット、L2 ループ | 警報から本物の CLI で止める、攻撃の進行度、NICTER のエッセンス、ループの検知と止め方 | [nirvana-response-attack-nexus-wan-flow.md](nirvana-response-attack-nexus-wan-flow.md)、[darknet-nicter.md](darknet-nicter.md)、[l2-loop-detection.md](l2-loop-detection.md) |
| 単体版 | 実機の監視環境(Prometheus / Loki / Zabbix)にそのまま置ける | [cyber-view-standalone.md](cyber-view-standalone.md) |
