# 「設定生成」タブ拡張: LLM→Ansible Playbook生成

## これは何か

ユーザー依頼「LLMを使っているのでLLMとansibleのうまい組み合わせあり
ますか？」への回答として提案した4パターンを、「これも試験に反映お
願いします」との指示通り実装・テストした:

1. **構成投入系(push)**: `*_config`モジュールのlinesで設定変更を投入
2. **事実取得・差分検知系(facts)**: `*_facts` + `ansible.builtin.assert`
   で現在の状態と期待値の差分を検知
3. **検証系(verify)**: `*_command`でshowコマンドを実行しregister/debug
4. **ロールバック系(rollback)**: `backup: yes`で変更を適用するplayと、
   バックアップから復元するplayの2play構成

新設`POST /api/generate_playbook`が、自然文の要件(instruction)+
パターン+対象装置から、そのベンダー向けのAnsible Playbook(YAML)を
LLM(Groq優先/Mistral/Ollama)に生成させる。「設定生成」タブに新しい
「🤖 Ansible Playbook生成(LLM)」セクションとして追加した。

## 対応device_type

このセッションで実際にnetmiko/ansibleの実SSH/NETCONF接続を確認済みの
4つのみ対応する(`docs/*.md`の既存ライブ検証記録に基づく、裏取りなしの
組み合わせを増やさない):

| device_type | Ansibleコレクション | 接続方式 | network_os |
|---|---|---|---|
| arista | arista.eos | network_cli(SSH) | eos |
| juniper | junipernetworks.junos | netconf | junos |
| cisco | cisco.ios | network_cli(SSH) | ios |
| catalyst | cisco.ios | network_cli(SSH) | ios |

対応外のdevice_type(sir/srs/asa等)を指定すると`ok: false`で明示的に
エラーを返す(フェイクの成功を作らない)。

## 実装

- `app.py`の`_ANSIBLE_DEVICE_MAP`(コレクション/モジュール名/接続方式の
  対応表)、`_playbook_system_prompt()`(パターン×device_typeに応じた
  system prompt組み立て)、`generate_playbook()`(エンドポイント本体)。
- LLM出力はMarkdownのコードフェンスを剥がした上で`yaml.safe_load()`で
  検証し、妥当なYAMLのリスト(play一覧)でなければ`ok: false`+生出力を
  返す(LLMがsystem promptの指示を無視して説明文を付けたり壊れた
  YAMLを返すことがあるため、信用せず検証する)。
- `/api/generate_playbook`の`device_id`欄にはDEFAULT_DEVICES に新設した
  `arista-demo`/`juniper-demo`(既存の`catalyst`/`cisco`に相乗り)を
  含め、フロントエンドの装置セレクトにも追加した。

**実`ansible-playbook`の実行はこのエンドポイントの責務外**:
netmiko/ansibleの既存ライブ検証(`tests/test_netmiko_ansible_compat.py`
等)と同じ方針で、このエミュレータ自体はAnsibleランタイムを持たず、
生成されたPlaybookの実行はユーザー環境での手動/オプションのライブ
検証に留める。

## ライブ検証

実際にサーバーを起動し、`arista-demo`(Arista)に対して4パターンそれぞれ
エラー系・デバイスタイプ別の挙動を確認(このサンドボックスにはLLM
バックエンドが無いため、`ok: false`+「AIバックエンドが利用できない
ため生成できません」という既存の`generate_config`と同じグレースフル
フォールバックを確認。LLM応答そのものは`tests/test_generate_playbook_
endpoint.py`でモックして検証):

```
$ curl -X POST /api/generate_playbook -d '{"device_id":"arista-demo","pattern":"push","instruction":"Ethernet1にdescriptionを設定して"}'
{"ok":false,"error":"AIバックエンド(Groq/Mistral/Ollama)が利用できないため生成できません"}

$ curl -X POST /api/generate_playbook -d '{"device_id":"sir-check","pattern":"push","instruction":"x"}'
{"ok":false,"error":"device_type 'sir' はPlaybook生成に対応していません(対応: ['arista', 'catalyst', 'cisco', 'juniper'])"}
```

`show version`で`arista-demo`がデフォルトの`cisco`型にフォールバック
せず正しく`arista`として作られていることも確認(Arista EOSの
`show version`の出力形式が実際に返ってくる)。

フロントエンド(Playwright、`/opt/pw-browsers/chromium`): 「設定生成」
タブに新しい「🤖 Ansible Playbook生成(LLM)」ボックスが正しく描画
され、パターン選択・要件入力・生成ボタン・AIバックエンド無し時の
エラー表示(赤字)まで実際に確認した。

## スコープ外(実装していない)

- 実`ansible-playbook`実行(上記の通り、意図的にスコープ外)
- verify パターンの「LLM判定」部分(別タスクのLLM-as-judge型試験
  自動化として切り分け、ここでは生成されるのは`*_command`+
  `debug`タスクまで)
- `edit <path>`的な階層config descent、複数装置をまたぐplaybook
  (hostsは常に`all`固定、1装置向けの単純なplaybookのみ)

## テスト

`tests/test_generate_playbook_endpoint.py`(13 tests)が、4パターン
それぞれのモジュール名/構造反映(push/facts/verify/rollback)、
device_type別のコレクション/接続方式の出し分け(arista=netconf以外/
juniper=netconf/cisco・catalyst=cisco.ios)、対応外device_typeの
エラー、不正なpattern値の拒否、LLMが壊れたYAML/非リスト/マークダウン
フェンス付きを返した場合の扱い(フェンス除去は成功扱い、壊れたYAML/
非リストはエラー)、AIバックエンド無し・LLM無応答時のグレースフル
フォールバックを固定する。`tests/test_generate_config_endpoint.py`と
同じ`monkeypatch`でのquery_llmモックパターンを踏襲。
