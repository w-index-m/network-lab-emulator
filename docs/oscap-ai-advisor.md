# OpenSCAP fail項目 AI是正アドバイザー

`tools/oscap_ai_advisor.py`

## これは何か

`oscap xccdf eval`（OpenSCAPによるCISベンチマーク等のスキャン）が
出した`fail`（不適合）項目を、利用者にそのまま突きつけるのではなく、
**「何が問題で・なぜ重要で・どう直すか」を日本語でまとめて提示する**
アドバイザー。

```
oscap xccdf eval --results results.xml ssg-ubuntu2404-ds.xml
        │
        ▼
tools/oscap_ai_advisor.py
   │ results.xml から fail項目のrule idを抽出
   │ DataStream(ds.xml)から title/description/rationale を引く
   │ 公式remediationスクリプト(bash/*.sh)から該当ルールの修正コマンドを抽出
   ▼
日本語での是正アドバイス（Ollamaがあれば自然文、無ければテンプレート）
```

## 使い方

```bash
# 1. ComplianceAsCode公式のremediationスクリプトも一緒に使う場合
python tools/oscap_ai_advisor.py \
    --results results.xml \
    --datastream ssg-ubuntu2404-ds.xml \
    --fix-script bash/ubuntu2404-script-cis_level1_server.sh

# Ollamaを使わずテンプレートのみで出す
python tools/oscap_ai_advisor.py --no-ai --results results.xml --datastream ssg-ubuntu2404-ds.xml

# JSON出力（他ツール連携用）
python tools/oscap_ai_advisor.py --json --results results.xml --datastream ssg-ubuntu2404-ds.xml
```

## 実際に確認した動作

このリポジトリの実行環境自体をOpenSCAP（ComplianceAsCode v0.1.82、
CIS Ubuntu 24.04 Level 1 Server Benchmark）でスキャンし、21件の
fail項目を検出。本ツールで実際にアドバイスを生成し、以下を確認した:

- **AIDE未インストール**などrationale（なぜ重要か）が正しく英語原文のまま
  抽出できる
- **pam_pwquality未インストール**は公式remediationスクリプトから
  `apt-get install -y libpam-pwquality` を含む実行可能なfixブロックが
  正しく抽出される
- **`/tmp`パーティション分離**のように、公式remediationスクリプト自体に
  fixが存在しない項目（`FIX FOR THIS RULE ... IS MISSING!`という
  プレースホルダのみ）は、正直に「手動確認が必要」と表示される
  （フェイクの対処法を出さない）

## Ollama連携

`httpx`がインストールされておりOllama（既定 `http://localhost:11434`、
モデル`llama3`）が起動していれば、rationale/fixを渡して自然な説明文に
言い換えさせる。接続できない/インストールされていない場合は自動的に
テンプレート出力にフォールバックする（`_template_advice()`）。

## 制約

- fixスクリプトの抽出は、ComplianceAsCode公式のbash remediation
  スクリプトのコメントマーカー（`BEGIN fix ... for 'RULE_ID'` /
  `END fix for 'RULE_ID'`）に依存している。他のSCAPコンテンツ提供元
  （Red Hat公式など）ではマーカー形式が異なる可能性がある
- 抽出したfixコマンドをそのまま自動実行する機能は無い（あくまで
  「利用者への提示」のみ。実行するかどうかは利用者の判断に委ねる）

## テスト

```bash
pytest tests/test_oscap_ai_advisor.py -v
# 8/8 成功
```
