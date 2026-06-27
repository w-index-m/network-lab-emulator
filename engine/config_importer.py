"""
running-config インポーター
Si-R / SR-S / Cisco IOS / Catalyst の show running-config テキストを
ルールエンジンにリプレイしてデバイス状態を復元する
"""
import re
import logging

logger = logging.getLogger(__name__)

# ホスト名として許可する文字（英数字・ハイフン・アンダースコア・ドット、最大63文字）
_HOSTNAME_RE = re.compile(r'^[A-Za-z0-9_\-\.]{1,63}$')

# ASCII印字可能文字（制御文字・2バイト文字を弾く）
_ASCII_PRINTABLE_RE = re.compile(r'^[\x20-\x7e\t]*$')

# IPアドレスとして有効なパターン
_IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')

# IPアドレスパターンを行から抽出するための正規表現
_IP_PATTERN_RE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')


def _is_valid_ip(ip: str) -> bool:
    """IPv4アドレスの妥当性チェック（各オクテット0-255、4オクテット必須）"""
    try:
        parts = ip.split('.')
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
    except (ValueError, AttributeError):
        return False


def _has_invalid_ip(line: str) -> tuple:
    """
    行にIPアドレスパターンがあればバリデーション。
    不正なIPがあれば (True, 説明文字列) を返す。
    正常なら (False, '') を返す。
    パスワード・キーワードが含まれる行（isakmp key など）は検査をスキップ。
    """
    # パスワード行はIPチェック対象外（キーにドット区切り数字が含まれることがある）
    stripped = line.strip()
    if re.match(r'^\s*(password|secret|key|enable\s+secret|username\s+\S+\s+(?:password|secret))', stripped, re.IGNORECASE):
        return False, ''

    found = _IP_PATTERN_RE.findall(line)
    for ip in found:
        if not _is_valid_ip(ip):
            return True, f"invalid IP address '{ip}'"
    return False, ''


def _sanitize_line(line: str) -> str | None:
    """
    行のサニタイズ。ASCII印字可能文字以外を含む行はNoneを返してスキップさせる。
    タブは許容する。NULLバイト・制御文字・2バイト文字はすべて除外。
    """
    # NULLバイトと制御文字（タブ以外）を除去
    cleaned = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', line)
    # ASCII範囲外（2バイト文字等）が残っていれば無効
    if not _ASCII_PRINTABLE_RE.match(cleaned):
        return None
    return cleaned


def _sanitize_hostname(name: str) -> str | None:
    """ホスト名バリデーション。不正な場合はNoneを返す。"""
    if not name:
        return None
    # ASCII英数字・ハイフン・アンダースコア・ドットのみ、最大63文字
    if _HOSTNAME_RE.match(name):
        return name
    return None


# ──────────────────────────────────────────
# マスク済みパスワードのパターン
# ──────────────────────────────────────────
_MASKED_PASSWORD_RE = re.compile(
    r'(\*{3,}|password\s+7\s+[0-9A-Fa-f]+|secret\s+[05]\s+\S+)'
    r'|crypto\s+isakmp\s+key\s+\S*\*+\S*\s+address'
)


def _has_masked_secret(line: str) -> bool:
    """行にマスク済みパスワード/秘密鍵が含まれるか"""
    if re.search(r'\*{3,}', line):
        return True
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

        # サニタイズ: ASCII範囲外（2バイト文字・制御文字）を含む行はスキップ
        sanitized = _sanitize_line(raw)
        if sanitized is None:
            errors.append(f"SKIP invalid chars (non-ASCII): {raw[:40].encode('unicode_escape').decode()}")
            continue
        raw = sanitized
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

        # IPアドレスバリデーション：不正なIPを含む行はスキップ
        has_bad_ip, bad_ip_desc = _has_invalid_ip(stripped)
        if has_bad_ip:
            errors.append(f"SKIP {bad_ip_desc}: {stripped[:60]}")
            logger.debug("skipped line with invalid IP: %s", stripped)
            continue

        is_indented = raw and raw[0] in (' ', '\t')

        if is_indented:
            # サブコンテキスト内のコマンド
            if current_context is None:
                logger.debug("indented line outside context, skip: %s", stripped)
                continue
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

            # hostname → バリデーションして直接セット
            m_hn = re.match(r'^hostname\s+(\S+)', stripped, re.IGNORECASE)
            if m_hn:
                valid_name = _sanitize_hostname(m_hn.group(1))
                if valid_name:
                    state.hostname = valid_name
                    commands_applied += 1
                else:
                    errors.append(f"SKIP invalid hostname: {m_hn.group(1)[:40]}")
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

    # Si-R は enable → configure でコンフィグコマンドを受け付ける
    rule_engine.process('enable', state)
    rule_engine.process('configure', state)

    for raw in lines:
        # サニタイズ: ASCII範囲外（2バイト文字・制御文字）を含む行はスキップ
        sanitized = _sanitize_line(raw)
        if sanitized is None:
            errors.append(f"SKIP invalid chars (non-ASCII): {raw[:40].encode('unicode_escape').decode()}")
            continue
        stripped = sanitized.strip()

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

        # IPアドレスバリデーション：不正なIPを含む行はスキップ
        has_bad_ip, bad_ip_desc = _has_invalid_ip(stripped)
        if has_bad_ip:
            errors.append(f"SKIP {bad_ip_desc}: {stripped[:60]}")
            logger.debug("skipped line with invalid IP: %s", stripped)
            continue

        # hostname → バリデーションして直接セット
        m_hn = re.match(r'^hostname\s+(\S+)', stripped, re.IGNORECASE)
        if m_hn:
            valid_name = _sanitize_hostname(m_hn.group(1))
            if valid_name:
                state.hostname = valid_name
                commands_applied += 1
            else:
                errors.append(f"SKIP invalid hostname: {m_hn.group(1)[:40]}")
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

_MAX_CONFIG_BYTES = 512 * 1024  # 512KB上限
_MAX_LINES = 10_000             # 行数上限


def validate_config_text(config_text: str) -> dict:
    """
    コンフィグテキストの事前チェック。インポート前に呼び出して問題を検出する。

    チェック項目:
      1. 存在確認（空でないか）
      2. 2バイト文字（日本語等）が含まれていないか
      3. 行数が異常に多くないか（1万行超）
      4. 不要なスペースのみの行が多くないか

    Returns:
        {
          "ok": bool,           # 全チェック通過でTrue
          "line_count": int,
          "issues": [           # 問題点リスト（ok=Falseの場合に詳細）
            {"level": "error"|"warn", "code": str, "message": str}
          ]
        }
    """
    issues = []

    # 1. 存在確認
    if not config_text or not config_text.strip():
        return {
            "ok": False,
            "line_count": 0,
            "issues": [{"level": "error", "code": "EMPTY",
                        "message": "コンフィグが空です"}],
        }

    lines = config_text.splitlines()
    line_count = len(lines)

    # 2. 行数チェック（1万行超はエラー）
    if line_count > _MAX_LINES:
        issues.append({
            "level": "error",
            "code": "TOO_MANY_LINES",
            "message": f"行数が多すぎます: {line_count} 行（上限 {_MAX_LINES} 行）",
        })

    # 3. 2バイト文字チェック（最初の5件を報告）
    multibyte_lines = []
    for lineno, line in enumerate(lines, 1):
        try:
            line.encode('ascii')
        except UnicodeEncodeError:
            # どの文字が非ASCIIか特定
            bad_chars = [c for c in line if ord(c) > 0x7e]
            sample = ''.join(dict.fromkeys(bad_chars))[:10]
            multibyte_lines.append((lineno, sample))
            if len(multibyte_lines) >= 5:
                break
    if multibyte_lines:
        detail = ', '.join(f"行{ln}「{ch}」" for ln, ch in multibyte_lines)
        issues.append({
            "level": "error",
            "code": "MULTIBYTE_CHARS",
            "message": f"2バイト文字が含まれています: {detail}",
            "lines": [ln for ln, _ in multibyte_lines],
        })

    # 4. 空白のみの行が全体の30%超なら警告（不要スペース・改行が多い）
    blank_or_space_lines = sum(1 for l in lines if not l.strip())
    if line_count > 0:
        blank_ratio = blank_or_space_lines / line_count
        if blank_ratio > 0.30:
            issues.append({
                "level": "warn",
                "code": "EXCESSIVE_BLANK_LINES",
                "message": (f"空白行が多すぎます: {blank_or_space_lines}/{line_count} 行"
                            f"（{blank_ratio:.0%}）— 不要なスペース・改行が混入している可能性があります"),
            })

    ok = not any(i["level"] == "error" for i in issues)
    return {"ok": ok, "line_count": line_count, "issues": issues}


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
    if not isinstance(config_text, str):
        return {"ok": False, "commands_applied": 0, "errors": ["config must be a string"]}

    if len(config_text.encode('utf-8', errors='replace')) > _MAX_CONFIG_BYTES:
        return {"ok": False, "commands_applied": 0,
                "errors": [f"config too large (max {_MAX_CONFIG_BYTES // 1024}KB)"]}

    # 事前バリデーション（存在確認・2バイト文字・行数）
    pre = validate_config_text(config_text)
    if not pre["ok"]:
        msgs = [i["message"] for i in pre["issues"] if i["level"] == "error"]
        return {"ok": False, "commands_applied": 0, "errors": msgs,
                "validation": pre}

    if dev_type in ('sir', 'srs'):
        return _parse_sir(config_text, state, rule_engine)
    else:
        # cisco / catalyst / asa / nexus など IOS 系
        return _parse_cisco(config_text, state, rule_engine)
