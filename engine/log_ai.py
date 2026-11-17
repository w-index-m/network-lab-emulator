"""
syslog を AI(LLM / クラウド AI)に読ませて、原因と対処(CLI)を提案させる

流れ:
  1. 下ごしらえ(prepare): 時刻の接頭辞を外して同じ文を「×N」にまとめ、重要度の高いもの(%FAC-0〜4-)を優先して
     上限 max_lines 行にする。クラウドへ送る前に、パスワード・secret・community・鍵などを伏せる。
     redact_ips=True なら IP アドレスも IP_1, IP_2… に置き換え、返ってきたコマンドの中で元に戻す
  2. 問い合わせ(ask): プロバイダは
       claude  … Anthropic の公式 SDK(`anthropic`)。モデルは NIRVANA_AI_MODEL(既定 claude-opus-5-5)。
                 構造化出力(output_config.format = json_schema)で必ず JSON が返る
       openai  … OpenAI 互換の Chat Completions(Groq / OpenAI / Mistral など。NIRVANA_AI_BASE_URL・
                 NIRVANA_AI_KEY、無ければ GROQ_API_KEY / OPENAI_API_KEY)
       ollama  … 手元の Ollama(OLLAMA_URL、既定 http://localhost:11434)
     NIRVANA_AI_PROVIDER が無ければ、鍵のあるものを claude → openai → ollama の順に使う
  3. 検証(validate): 返ってきた対処を、知っている装置だけに絞り、コマンドを危険度で分ける。
     reload / write erase / erase / format / delete / crypto key zeroize / no username などは「実行不可」
     (表示だけ)。shutdown・no router・clear などは「要確認」。実行はいつも人が押す(自動では入れない)
  4. AI が使えない時はルールベース(analyze_rules)で、よくあるログ(リンク断・OSPF/BGP の隣接断・ログイン
     失敗・MAC フラップ・設定変更・CPU)の所見と、確認用の show コマンドを返す(画面に「ルール」と出す)

AI の答えは提案であり、正しいとは限らない。画面と API の結果には必ず provider / model を載せる。
"""
import json
import os
import re
import time
import urllib.request
from collections import OrderedDict

SEV_RE = re.compile(r'%[\w-]+-(\d)-')
TS_PREFIX = re.compile(r'^\*?(?:[A-Z][a-z]{2} +\d+ [\d:.]+|\d{4}-\d\d-\d\d[T ][\d:.]+\S*)[: ]+')
IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
SECRET_RES = [
    re.compile(r'((?:password|secret|key-string|pre-shared-key|psk|key)\s+(?:\d\s+)?)(\S+)', re.I),
    re.compile(r'((?:community)\s+)(\S+)', re.I),
    re.compile(r'(snmp-server community\s+)(\S+)', re.I),
]
BLOCKED = re.compile(r'^\s*(reload|write erase|erase\b|format\b|delete\b|crypto key zeroize|no username|'
                     r'boot system|copy .* (startup|flash)|config-register|debug all)', re.I)
CAUTION = re.compile(r'^\s*(shutdown|no router|no ip route|clear\b|no interface|no vlan|spanning-tree|'
                     r'access-list|no access-list|ip access-group|line vty|username)', re.I)

SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string'},
        'findings': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low', 'info']},
                'device': {'type': 'string'},
                'title': {'type': 'string'},
                'cause': {'type': 'string'},
                'evidence': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['severity', 'device', 'title', 'cause', 'evidence'],
            'additionalProperties': False}},
        'actions': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'device': {'type': 'string'},
                'title': {'type': 'string'},
                'why': {'type': 'string'},
                'mode': {'type': 'string', 'enum': ['exec', 'config']},
                'commands': {'type': 'array', 'items': {'type': 'string'}},
                'rollback': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['device', 'title', 'why', 'mode', 'commands', 'rollback'],
            'additionalProperties': False}},
    },
    'required': ['summary', 'findings', 'actions'],
    'additionalProperties': False,
}

SYSTEM = (
    'あなたは企業ネットワークの運用担当(NOC)を補佐するネットワークエンジニアです。'
    '渡された syslog(装置名・重要度・回数つき)を読み、日本語で次をまとめてください。'
    'summary: 全体で何が起きているかを2〜3文で。'
    'findings: 問題ごとに、重要度・装置・題名・考えられる原因・根拠のログ行(そのまま引用)。'
    'actions: 運用者が取れる対処。まず状況を確かめる show コマンド、次に必要なら設定変更。'
    '装置の種類(cisco / catalyst / nexus / asa / juniper / sir / yamaha 等)に合った実機のコマンドだけを書き、'
    'mode は exec(特権 EXEC で打つもの)か config(configure terminal の中で打つもの)。'
    '設定変更には必ず rollback(元に戻すコマンド)を付ける。装置名は与えられた一覧の名前だけを使う。'
    '再起動・設定消去・ファイル削除・鍵の消去は提案しない。'
    'ログから言えないことは推測と明記し、根拠のない断定はしない。問題が無ければ findings と actions は空でよい。'
    'IP_1 のような記号は伏せたアドレスなので、そのまま使ってよい。'
)


# ── 下ごしらえ ───────────────────────────────────
def sev_of(row):
    m = SEV_RE.search(row.get('msg', ''))
    if m:
        return int(m.group(1))
    try:
        return int(row.get('sev', 6))
    except (TypeError, ValueError):
        return 6


def redact(text, ip_map=None):
    for rx in SECRET_RES:
        text = rx.sub(lambda m: m.group(1) + '<REDACTED>', text)
    if ip_map is not None:
        def sub(m):
            ip = m.group(0)
            if ip not in ip_map:
                ip_map[ip] = f'IP_{len(ip_map) + 1}'
            return ip_map[ip]
        text = IP_RE.sub(sub, text)
    return text


def prepare(rows, devices=None, max_lines=200, redact_ips=False):
    """rows: [{'t','host' or 'dev','sev','msg'}] → (本文, ip_map, 統計)"""
    ip_map = {} if redact_ips else None
    groups = OrderedDict()
    for r in sorted(rows, key=lambda x: x.get('t', 0)):
        host = r.get('host') or r.get('dev') or '?'
        msg = TS_PREFIX.sub('', str(r.get('msg', ''))).strip()
        k = (host, msg)
        g = groups.get(k)
        if g is None:
            groups[k] = g = {'host': host, 'msg': msg, 'sev': sev_of(r), 'n': 0, 'first': r.get('t', 0), 'last': r.get('t', 0)}
        g['n'] += 1
        g['last'] = r.get('t', 0)
    items = list(groups.values())
    picked = sorted(items, key=lambda g: (g['sev'], -g['n'], -g['last']))[:max_lines]
    picked.sort(key=lambda g: g['first'])
    lines = []
    for g in picked:
        ts = time.strftime('%H:%M:%S', time.localtime(g['first'])) if g['first'] else '--:--:--'
        cnt = f' ×{g["n"]}' if g['n'] > 1 else ''
        lines.append(redact(f'{ts} {g["host"]} sev={g["sev"]}{cnt}: {g["msg"]}', ip_map))
    head = ''
    if devices:
        head = '装置一覧: ' + ', '.join(f'{d["hostname"]}({d.get("type", "?")})' for d in devices) + '\n'
    stats = {'rows': len(rows), 'unique': len(items), 'sent': len(picked), 'dropped': max(0, len(items) - len(picked))}
    return head + '\n'.join(lines), ip_map or {}, stats


# ── プロバイダ ──────────────────────────────────
def provider_config(env=None):
    env = env if env is not None else os.environ
    want = (env.get('NIRVANA_AI_PROVIDER') or 'auto').lower()
    model = env.get('NIRVANA_AI_MODEL', '')
    has_claude = bool(env.get('ANTHROPIC_API_KEY') or env.get('ANTHROPIC_AUTH_TOKEN'))
    oa_key = env.get('NIRVANA_AI_KEY') or env.get('GROQ_API_KEY') or env.get('OPENAI_API_KEY') or ''
    oa_base = env.get('NIRVANA_AI_BASE_URL') or ('https://api.groq.com/openai/v1' if env.get('GROQ_API_KEY') and not env.get('NIRVANA_AI_KEY')
                                                 else 'https://api.openai.com/v1')
    ollama = env.get('OLLAMA_URL', '')
    if want == 'auto':
        want = 'claude' if has_claude else 'openai' if oa_key else 'ollama' if ollama else 'rules'
    if want == 'claude':
        return {'provider': 'claude', 'model': model or 'claude-opus-5-5', 'cloud': True}
    if want == 'openai':
        default = 'llama-3.1-8b-instant' if 'groq' in oa_base else 'gpt-4o-mini'
        return {'provider': 'openai', 'model': model or env.get('GROQ_MODEL') or default, 'base_url': oa_base.rstrip('/'),
                'key': oa_key, 'cloud': True}
    if want == 'ollama':
        return {'provider': 'ollama', 'model': model or env.get('OLLAMA_MODEL', 'llama3'),
                'base_url': (ollama or 'http://localhost:11434').rstrip('/'), 'cloud': False}
    return {'provider': 'rules', 'model': None, 'cloud': False}


def _ask_claude(cfg, user_text):
    import anthropic                              # 公式 SDK(requirements の任意パッケージ)
    client = anthropic.Anthropic()                # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant のプロファイル
    kwargs = dict(model=cfg['model'], max_tokens=16000, system=SYSTEM,
                  messages=[{'role': 'user', 'content': user_text}],
                  output_config={'effort': 'medium', 'format': {'type': 'json_schema', 'schema': SCHEMA}})
    if cfg['model'] in ('claude-opus-5-5', 'claude-fable-5-2', 'claude-fable-5-1', 'claude-opus-5', 'claude-sonnet-5-5'):
        # 安全分類で断られた時はサーバー側で別のモデルに回す(既定の振り分け)
        resp = client.beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == 'refusal':
        cat = getattr(getattr(resp, 'stop_details', None), 'category', None)
        raise RuntimeError(f'AI が回答を断りました(refusal: {cat})')
    if resp.stop_reason == 'max_tokens':
        raise RuntimeError('AI の回答が長すぎて途中で切れました(ログを減らしてください)')
    text = next((b.text for b in resp.content if b.type == 'text'), '')
    return json.loads(text), getattr(resp, 'model', cfg['model'])


def _post_json(url, body, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _loose_json(text):
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1)
    a, b = text.find('{'), text.rfind('}')
    return json.loads(text[a:b + 1] if a >= 0 and b > a else text)


def _ask_openai(cfg, user_text):
    sys_text = SYSTEM + ' 出力は次の JSON スキーマに従う JSON オブジェクトだけ: ' + json.dumps(SCHEMA, ensure_ascii=False)
    d = _post_json(cfg['base_url'] + '/chat/completions', {
        'model': cfg['model'], 'temperature': 0.2, 'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': sys_text}, {'role': 'user', 'content': user_text}]},
        {'Authorization': f'Bearer {cfg["key"]}'})
    return _loose_json(d['choices'][0]['message']['content']), d.get('model', cfg['model'])


def _ask_ollama(cfg, user_text):
    sys_text = SYSTEM + ' 出力は次の JSON スキーマに従う JSON オブジェクトだけ: ' + json.dumps(SCHEMA, ensure_ascii=False)
    d = _post_json(cfg['base_url'] + '/api/chat', {
        'model': cfg['model'], 'stream': False, 'format': SCHEMA,
        'messages': [{'role': 'system', 'content': sys_text}, {'role': 'user', 'content': user_text}]}, {}, timeout=180)
    return _loose_json(d['message']['content']), cfg['model']


ASK = {'claude': _ask_claude, 'openai': _ask_openai, 'ollama': _ask_ollama}


# ── ルールベース(AI が無い時) ─────────────────────────
RULES = [
    (re.compile(r'Interface (\S+), changed state to (?:administratively )?down|Line protocol on Interface (\S+), changed state to down', re.I),
     'high', 'インタフェース断', 'ケーブル・対向装置・SFP の故障、または意図した shutdown',
     lambda m: [f'show interfaces {m.group(1) or m.group(2)}', 'show logging | include LINK|LINEPROTO', 'show cdp neighbors']),
    (re.compile(r'OSPF-5-ADJCHG.*Nbr (\S+) on (\S+) from FULL to (\w+)', re.I),
     'high', 'OSPF 隣接断', '対向のリンク断・Hello/Dead タイマー不一致・MTU 不一致・エリア/認証の設定違い',
     lambda m: ['show ip ospf neighbor', f'show ip ospf interface {m.group(2)}', 'show ip route ospf']),
    (re.compile(r'BGP-5-ADJCHANGE: neighbor (\S+) Down', re.I),
     'high', 'BGP ピア断', '対向の到達性・ホールドタイマー満了・設定(AS 番号・認証)の違い',
     lambda m: [f'show ip bgp neighbors {m.group(1)}', 'show ip bgp summary', f'ping {m.group(1)}']),
    (re.compile(r'SEC_LOGIN-4-LOGIN_FAILED|Login failed|Authentication failed', re.I),
     'medium', 'ログイン失敗', '総当たり・パスワード誤り。送信元を確認し、必要なら login block-for / access-class',
     lambda m: ['show login failures', 'show users', 'show logging | include LOGIN']),
    (re.compile(r'MAC_?MOVE|MACFLAP|flapping between port', re.I),
     'high', 'MAC アドレスのフラップ(ループの疑い)', 'L2 ループ・冗長リンクの STP 不備・仮想環境の MAC 移動',
     lambda m: ['show spanning-tree summary', 'show mac address-table notification mac-move', 'show interfaces counters errors']),
    (re.compile(r'SYS-5-CONFIG_I', re.I),
     'info', '設定変更', '誰かが configure terminal で設定を変えた。意図した変更か確認',
     lambda m: ['show archive log config all', 'show running-config | section ^interface|^router']),
    (re.compile(r'CPUHOG|cpu.*(high|threshold)', re.I),
     'medium', 'CPU 高負荷', 'プロセスの暴走・大量のパケットの CPU 処理(ARP・ルーティング更新・攻撃)',
     lambda m: ['show processes cpu sorted', 'show processes cpu history']),
]


def analyze_rules(rows):
    findings, actions, seen = [], [], set()
    for r in rows:
        msg = str(r.get('msg', ''))
        host = r.get('host') or r.get('dev') or '?'
        for rx, sev, title, cause, cmds in RULES:
            m = rx.search(msg)
            if not m:
                continue
            k = (host, title, m.group(0)[:60])
            if k in seen:
                continue
            seen.add(k)
            findings.append({'severity': sev, 'device': host, 'title': title, 'cause': cause, 'evidence': [msg[:200]]})
            actions.append({'device': host, 'title': f'{title}の確認', 'why': cause, 'mode': 'exec', 'commands': cmds(m), 'rollback': []})
    n = len(findings)
    summary = (f'ルールで {n} 件の所見を見つけました(AI は使っていません)。' if n else 'ルールで分かる問題は見つかりませんでした(AI は使っていません)。')
    return {'summary': summary, 'findings': findings[:30], 'actions': actions[:30]}


# ── 検証 ──────────────────────────────────────
def restore_ips(text, ip_map):
    back = {v: k for k, v in ip_map.items()}
    return re.sub(r'\bIP_\d+\b', lambda m: back.get(m.group(0), m.group(0)), text)


def validate(result, devices, ip_map=None):
    """AI の答えを、知っている装置・安全なコマンドに絞る。devices: [{'id','hostname','type'}]"""
    ip_map = ip_map or {}
    by_name = {}
    for d in devices:
        for k in (d.get('id'), d.get('hostname')):
            if k:
                by_name[str(k).lower()] = d
    out = {'summary': restore_ips(str(result.get('summary', '')), ip_map)[:2000], 'findings': [], 'actions': []}
    for f in result.get('findings') or []:
        d = by_name.get(str(f.get('device', '')).lower())
        out['findings'].append({
            'severity': f.get('severity') if f.get('severity') in ('critical', 'high', 'medium', 'low', 'info') else 'info',
            'device': d['hostname'] if d else str(f.get('device', ''))[:60], 'device_id': d['id'] if d else None,
            'title': restore_ips(str(f.get('title', '')), ip_map)[:200], 'cause': restore_ips(str(f.get('cause', '')), ip_map)[:1000],
            'evidence': [restore_ips(str(x), ip_map)[:300] for x in (f.get('evidence') or [])][:5]})
    for a in result.get('actions') or []:
        d = by_name.get(str(a.get('device', '')).lower())
        cmds = [restore_ips(str(c), ip_map).strip() for c in (a.get('commands') or []) if str(c).strip()][:20]
        cmds = [c for c in cmds if c.lower() not in ('configure terminal', 'conf t', 'end', 'exit')]
        rb = [restore_ips(str(c), ip_map).strip() for c in (a.get('rollback') or []) if str(c).strip()][:20]
        blocked = [c for c in cmds if BLOCKED.search(c)]
        caution = [c for c in cmds if CAUTION.search(c)]
        mode = a.get('mode') if a.get('mode') in ('exec', 'config') else 'exec'
        risk = 'blocked' if blocked else 'caution' if (caution or mode == 'config') else 'read'
        out['actions'].append({
            'device': d['hostname'] if d else str(a.get('device', ''))[:60], 'device_id': d['id'] if d else None,
            'title': restore_ips(str(a.get('title', '')), ip_map)[:200], 'why': restore_ips(str(a.get('why', '')), ip_map)[:1000],
            'mode': mode, 'commands': cmds, 'rollback': rb, 'risk': risk,
            'blocked': blocked, 'executable': bool(d) and not blocked and bool(cmds)})
    return out


# ── まとめ ──────────────────────────────────────
class LogAnalyst:
    def __init__(self, maxlen=30):
        self.history = []
        self.maxlen = maxlen
        self.seq = 0

    def analyze(self, rows, devices, use_ai=True, max_lines=200, redact_ips=None, env=None, ask=None, now=None):
        cfg = provider_config(env)
        if redact_ips is None:                       # クラウドへ送るときは既定でアドレスも伏せる
            redact_ips = cfg['cloud'] and (env or os.environ).get('NIRVANA_AI_REDACT_IPS', '1') != '0'
        seen = {str(r.get(k, '')).lower() for r in rows for k in ('host', 'dev')}
        in_logs = [d for d in devices if str(d.get('hostname', '')).lower() in seen or str(d.get('id', '')).lower() in seen]
        text, ip_map, stats = prepare(rows, in_logs or devices, max_lines, redact_ips)
        used = {'provider': 'rules', 'model': None}
        error = None
        if use_ai and cfg['provider'] != 'rules' and stats['sent']:
            try:
                raw, model = (ask or ASK[cfg['provider']])(cfg, text)
                result = validate(raw, devices, ip_map)
                used = {'provider': cfg['provider'], 'model': model}
            except Exception as e:  # noqa: BLE001 - AI が使えなければルールで答える(理由は画面に出す)
                error = str(e)[:300]
                result = validate(analyze_rules(rows), devices)
        else:
            if use_ai and cfg['provider'] == 'rules':
                error = 'AI が設定されていません(ANTHROPIC_API_KEY / NIRVANA_AI_* / GROQ_API_KEY / OLLAMA_URL)'
            result = validate(analyze_rules(rows), devices)
        self.seq += 1
        rec = {'id': self.seq, 't': now or time.time(), **used, 'error': error, 'stats': stats,
               'redacted_ips': len(ip_map), 'sent_text': text, **result, 'applied': []}
        self.history.append(rec)
        self.history = self.history[-self.maxlen:]
        return rec

    def get(self, rid):
        return next((h for h in self.history if h['id'] == rid), None)


analyst = LogAnalyst()
