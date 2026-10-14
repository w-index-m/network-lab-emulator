#!/usr/bin/env bash
# strix-pentest.yml用のLLMフォールバック実行ラッパー。
#
# ユーザーに「ネットワークラボの改善点」として提案し実装を依頼された項目の
# 一つ。初回Dispatch(run #1)・2回目(run #2)とも、デフォルトのGroqモデル名
# (llama-3.3-70b-versatile → llama-3.1-8b-instant)がこのAPIキーのアカウント
# では"model_not_found"になり、ワークフロー全体が失敗していた
# (docs/strix-pentest-integration.md / CLAUDE.mdに記録済み)。
#
# アプリ本体(app.py)のGroq→Mistral→Ollamaフォールバックチェーン
# (query_llm() / _LLM_CHAIN)と同じ考え方を、Strix実行にも適用する:
# カンマ区切りのモデル候補を先頭から順に試し、最初に成功したところで
# 終える。プロバイダ名(先頭の"groq/"/"mistral/"等)に応じて対応する
# APIキーのSecretを自動選択する——Secretが無い(空文字列)候補はAPI
# キー無しで呼ばずにスキップする(detect_groq()/detect_mistral()の
# 「キー無しなら即座にFalse」という安全なフォールバックと同じ考え方)。
#
# 使い方: run_strix_with_fallback.sh <strixの--target以降の引数...>
set -uo pipefail

MODEL_LIST="${STRIX_LLM_CANDIDATES:-}"
if [ -z "$MODEL_LIST" ]; then
  echo "::error::STRIX_LLM_CANDIDATES が設定されていません" >&2
  exit 1
fi

IFS=',' read -ra MODELS <<< "$MODEL_LIST"

for RAW_MODEL in "${MODELS[@]}"; do
  MODEL="$(echo "$RAW_MODEL" | xargs)"
  [ -z "$MODEL" ] && continue

  case "$MODEL" in
    groq/*)
      KEY="${GROQ_API_KEY:-}"
      ;;
    mistral/*)
      KEY="${MISTRAL_API_KEY:-}"
      ;;
    openai/*)
      KEY="${OPENAI_API_KEY:-}"
      ;;
    *)
      KEY="${LLM_API_KEY:-}"
      ;;
  esac

  if [ -z "$KEY" ]; then
    echo "::warning::$MODEL 用のAPIキー(Secret)が設定されていないためスキップします"
    continue
  fi

  echo "=== Trying STRIX_LLM=$MODEL ==="
  if STRIX_LLM="$MODEL" LLM_API_KEY="$KEY" strix "$@"; then
    echo "=== $MODEL で成功 ==="
    exit 0
  fi
  echo "::warning::STRIX_LLM=$MODEL での実行に失敗しました。次の候補を試します"
done

echo "::error::すべてのLLM候補(${MODEL_LIST})で失敗しました"
exit 1
