# RAG（検索拡張生成） — ファインチューニングの代替

`tools/rag_query.py`

## これは何か

「Qwenをこのプロジェクトの知見で育てたい（ファインチューニングしたい）」
という要望に対する、この環境で実際に動く代替策。

## なぜファインチューニングではなくRAGなのか

実際にllama.cppの`llama-finetune`ツールで本物の学習を試みたが、
以下の理由で断念した（`docs/qwen-network-system-prompt.md`とは別の
検証）:

- llama.cppの学習ツールは**非量子化(F32)のGGUF**が必須（公式README
  記載）だが、手元にあるのは推論専用の量子化版(Q4_K_M)のみ
- 非量子化版やHugging Face形式の元重みを入手するには配布元
  （Hugging Face、ModelScope）へのアクセスが必要だが、この環境では
  ブロックされている
- 実際に量子化版で学習を強行したところ、`GGML_ASSERT(*cur_backend_id
  != -1)`で学習グラフ構築時にクラッシュした（量子化テンソルは
  逆伝播計算に対応していないため）

RAGなら学習済みモデルの重みを一切変更せず、**質問のたびに関連する
`docs/*.md`の内容を検索してプロンプトに埋め込む**だけで、実質的に
「このプロジェクト固有の知識を踏まえた回答」を実現できる。

## 仕組み

埋め込みモデル（ニューラルネットワークによる意味検索）ではなく、
**BM25**（統計的なキーワードマッチング、外部モデル不要）を使っている。
理由は、埋め込みモデルの配布元も大抵Hugging Faceであり、同じブロック
問題に当たるため。BM25はPyPIの`rank_bm25`パッケージのみで完結する。

```
質問
  │
  ▼
docs/*.md を##見出し単位でチャンク分割 → BM25でスコアリング
  │
  ▼
上位N件のチャンクを「参考資料」としてプロンプトに埋め込む
  │
  ▼
qwen2.5-netops (Ollama) に問い合わせ
  │
  ▼
このプロジェクト固有の内容を踏まえた回答
```

## 使い方

```bash
python tools/rag_query.py "PowerDNSでIPv6絡みのエラーが出たときの対処法は？"

# 検索でヒットした参考資料も見たい場合
python tools/rag_query.py "Zabbixのビルドでハマった点は？" --show-context
```

## 実際に確認した動作（2026-09-03）

```
$ python tools/rag_query.py "PowerDNSでIPv6絡みのエラーが出たときの対処法は？"

=== Qwen の回答 ===
PowerDNSでIPv6絡みのエラーが発生した場合、以下の手順を試してみてください：

1. local-address=0.0.0.0 / query-local-address=0.0.0.0 を設定
   - この設定はPowerDNSがデフォルトで`::`にbindしようとして失敗する
     問題を解決します。
2. /etc/powerdns/pdns.d/bind.conf を無効化
   ...
```

`docs/dns-dhcp-setup.md`に実際に記録した`Fatal error: Unable to
acquire TCP socket: Address family not supported`のトラブルシュート
内容が、一般論ではなく**このプロジェクトで実際に起きた原因・対処法**
としてそのまま回答に反映されることを確認した。

BM25検索自体の精度もテストで検証済み（`tests/test_rag_query.py`）:
「PowerDNS IPv6 エラー」→`dns-dhcp-setup.md`のIPv6関連セクションが
最上位でヒットし、「Zabbixのスキーマ生成でハマった点」→
`zabbix-setup.md`が正しくヒットする。

## 制約（正直な観察）

- BM25はキーワードの表記ゆれに弱い（例えば英語表記と日本語表記が
  混在する技術文書では、言い換えられた質問だと関連文書を逃すことが
  ある）
- **回答に含まれるコマンド構文はQwen自身が生成しているため、たまに
  架空のコマンドを創作することがある**（実際の確認で
  `powerdns-recursor config set local-address 0.0.0.0`という
  存在しないコマンドが出力に混入した）。RAGは「事実（原因・設定値）」
  の正確性は大きく改善するが、「コマンド構文」の正確性までは保証しない。
  重要な操作の前には必ず元の`docs/*.md`を直接参照すること
- ファインチューニングと異なり、モデル自体は何も学習していない。
  `docs/*.md`を検索対象から外せば、プロジェクト知識は一切反映されなくなる
  （＝知識はモデルにではなく`docs/`ディレクトリ側に存在する）

## テスト

```bash
pytest tests/test_rag_query.py -v
# 4/4 成功（BM25検索ロジックのみ、Ollama呼び出しは含まない）
```
