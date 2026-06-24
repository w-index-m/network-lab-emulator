"""
running-config インポーター
Si-R / SR-S / Cisco IOS / Catalyst の show running-config テキストを
ルールエンジンにリプレイしてデバイス状態を復元する
"""
import re
import logging

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# マスク済みパスワードのパターン
# ──────────────────────────────────────────
_MASKED_PASSWORD_RE = re.compile(
    r'(\*{3,}|password\s+7\s+[0-9A-Fa-f]+|secret\s+[05]\s+\S+)'
    r'|crypto\s+isakmp\s+key\s+\S*\*+\S*\s+address'
)


def _has_masked_secret(line: str) -> bool:
    """行にマスク済みパスワード/秘密鍵が含まれるか"""
    # 典型的なマスク: "****", "password ****", "key ****"
    if re.search(r'\*{3,}', line):
        return True
    # crypto isakmp key **** address X のようなケース
    if re.match(r'\s*crypto\s+isakmp\s+key\s+\S*\*+\S*', line):
        return True
    return False


# ──────────────────────────────────────────
# Cisco IOS / Catalyst パーサー
# ──────────────────────────────────────────

# コンテキストを持つトップレベルコマンドのパターン
_CISCO_CONTEXT_RE = re.compile(
    r'^(interface\s+\S+|'
    r'crypto\s+isakmp\s+policy\s+\d+|'
    r'crypto\s+map\s+\S+\s+\d+\s+ipsec-isakmp|'
    r'router\s+(?:ospf|bgp|rip|eigrp)\b.*|'
    r'ip\s+access-list\s+\S+\s+\S+|'
    r'vlan\s+\d[\d,]*)',
    re.IGNORECASE
)

# サブコンテキストは不要（単一行）なコマンド
_CISCO_NO_SUBCTX_RE = re.compile(
    r'^crypto\s+ipsec\s+transform-set\b',
    re.IGNORECASE
)

# スキップするトップレベルヘッダー行
_CISCO_SKIP_RE = re.compile(
    r'^(Building\s+configuration|'
    r'Current\s+configuration|'
    r'version\s+\d|'
    r'boot\s+|'
    r'service\s+|'
    r'no\s+service\s+|'
    r'!)',
    re.IGNORECASE
)


def _parse_cisco(config_text: str, state, rule_engine) -> dict:
    """Cisco IOS / Catalyst running-config をパースしてコマンドリストを生成・実行"""
    commands_applied = 0
    errors = []

    lines = config_text.splitlines()

    # デフォルトのインターフェースIPをクリア（インポート前の残留を防ぐ）
    for ifname in list(state.interfaces.keys()):
        state.interfaces[ifname]['ip'] = ''
        state.interfaces[ifname]['prefix'] = 0
        state.interfaces[ifname]['status'] = 'down'

    # まず enable + configure terminal でコンフィグモードへ
    rule_engine.process('enable', state)
    rule_engine.process('configure terminal', state)

    i = 0
    current_context = None  # 現在のサブコンテキスト行 (interface X など)

    while i < len(lines):
        raw = lines[i]
        i += 1
        stripped = raw.strip()

        # 空行・end
        if not stripped or stripped == 'end':
            if stripped == 'end':
                break
            continue

        # コメント行
        if stripped.startswith('!'):
            continue

        # スキップすべきヘッダー
        if _CISCO_SKIP_RE.match(stripped):
            continue

        # マスク済みパスワードはスキップ
        if _has_masked_secret(stripped):
            errors.append(f"SKIP masked secret: {stripped[:60]}")
            logger.debug("skipped masked line: %s", stripped)
            continue

        is_indented = raw and raw[0] in (' ', '\t')

        if is_indented:
            # サブコンテキスト内のコマンド
            if current_context is None:
                # コンテキスト外のインデント行は無視
                logger.debug("indented line outside context, skip: %s", stripped)
                continue
            # no shutdown は mode tunnel などインデントコマンドとして処理
            result = rule_engine.process(stripped, state)
            commands_applied += 1
            if re.search(r'%|Error|Invalid', result or '', re.IGNORECASE):
                errors.append(f"WARN [{stripped[:60]}]: {result[:80] if result else ''}")
        else:
            # トップレベルコマンド
            # 前のサブコンテキストを閉じる
            if current_context is not None:
                rule_engine.process('exit', state)
                current_context = None

            # hostname → 直接セット
            m_hn = re.match(r'^hostname\s+(\S+)', stripped, re.IGNORECASE)
            if m_hn:
                state.hostname = m_hn.group(1)
                commands_applied += 1
                continue

            # サブコンテキストを持つコマンド
            if _CISCO_CONTEXT_RE.match(stripped) and not _CISCO_NO_SUBCTX_RE.match(stripped):
                result = rule_engine.process(stripped, state)
                commands_applied += 1
                current_context = stripped
                if re.search(r'%|Error|Invalid', result or '', re.IGNORECASE):
                    errors.append(f"WARN [{stripped[:60]}]: {result[:80] if result else ''}")
                continue

            # その他のトップレベルコマンド（単一行）
            result = rule_engine.process(stripped, state)
            commands_applied += 1
            if re.search(r'%|Error|Invalid', result or '', re.IGNORECASE):
                errors.append(f"WARN [{stripped[:60]}]: {result[:80] if result else ''}")

    # 最後のサブコンテキストを閉じる
    if current_context is not None:
        rule_engine.process('exit', state)

    # コンフィグモードを抜ける
    rule_engine.process('end', state)

    return {"ok": True, "commands_applied": commands_applied, "errors": errors}


# ──────────────────────────────────────────
# Si-R / SR-S パーサー
# ──────────────────────────────────────────

def _parse_sir(config_text: str, state, rule_engine) -> dict:
    """Si-R / SR-S running-config をパースしてコマンドをリプレイ"""
    commands_applied = 0
    errors = []

    lines = config_text.splitlines()

    # Si-R は enable のみでコマンドを受け付ける
    rule_engine.process('enable', state)

    for raw in lines:
        stripped = raw.strip()

        # 空行
        if not stripped:
            continue

        # コメント行
        if stripped.startswith('#') or stripped.startswith('!'):
            continue

        # マスク済みパスワードはスキップ
        if _has_masked_secret(stripped):
            errors.append(f"SKIP masked secret: {stripped[:60]}")
            continue

        # hostname → 直接セット
        m_hn = re.match(r'^hostname\s+(\S+)', stripped, re.IGNORECASE)
        if m_hn:
            state.hostname = m_hn.group(1)
            commands_applied += 1
            continue

        # end / exit は無視
        if stripped in ('end', 'exit'):
            continue

        result = rule_engine.process(stripped, state)
        commands_applied += 1
        if re.search(r'%|Error|Invalid', result or '', re.IGNORECASE):
            errors.append(f"WARN [{stripped[:60]}]: {result[:80] if result else ''}")

    return {"ok": True, "commands_applied": commands_applied, "errors": errors}


# ──────────────────────────────────────────
# 公開エントリーポイント
# ──────────────────────────────────────────

def import_running_config(dev_type: str, config_text: str, rule_engine, state) -> dict:
    """
    running-config テキストをパースしてルールエンジンにリプレイする。

    Args:
        dev_type:    デバイスタイプ ('sir' / 'srs' / 'cisco' / 'catalyst' / ...)
        config_text: show running-config の生テキスト
        rule_engine: RuleEngine インスタンス
        state:       DeviceState インスタンス（インプレース変更される）

    Returns:
        {"ok": True, "commands_applied": N, "errors": [...]}
    """
    if dev_type in ('sir', 'srs'):
        return _parse_sir(config_text, state, rule_engine)
    else:
        # cisco / catalyst / asa / nexus など IOS 系
        return _parse_cisco(config_text, state, rule_engine)
