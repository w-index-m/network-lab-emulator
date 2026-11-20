"""
SNMPv3(USM + 簡易 VACM)— 設定コマンド・show・エージェント側の処理

対応する機種と構文:
  IOS / IOS-XE(cisco・catalyst・wlc)・ASA・Arista EOS
    snmp-server engineID local <16進>
    snmp-server view <名前> <OID か名前> included|excluded
    snmp-server group <グループ> v3 {noauth|auth|priv} [read <view>] [write <view>] [notify <view>]
    snmp-server user <ユーザー> <グループ> v3 [auth {md5|sha|sha256...} <pw> [priv {des|aes [128|192|256]} <pw>]]
    show snmp user / show snmp group / show snmp engineID / show snmp view
  NX-OS(nexus)
    snmp-server user <ユーザー> [<ロール>] auth {md5|sha|sha-256...} <pw> [priv [aes-128] <pw>] [localizedkey]
    show snmp user / show snmp engineID

鍵は実機と同じく engineID で「局所化」した鍵だけを持ち、パスワードそのものは保存しない
(NX-OS の running-config に出る 0x... の localizedkey と同じ)。IOS の AES-192/256 は Cisco と同じ
Reeder 方式で鍵を伸ばす(net-snmp では -x AES192C / AES256C)。

エージェント側(UDP/161 の v3 要求)は engine/snmp_udp_agent.py から process() を呼ぶ。
engineID の発見・時刻同期(Report PDU)・HMAC・DES/AES・ユーザーのグループの安全レベル・読める範囲(view)を見る。
"""
import hashlib
import re
import time

from engine import snmp_usm as usm

V3_TYPES = ('cisco', 'catalyst', 'wlc', 'asa', 'arista', 'nexus')
_START = time.time()
_STATS = {}            # device_id -> {OID: 回数}
state_lookup = None    # app.py が device_id → DeviceState を引ける関数を入れる(設定が未登録の装置のため)

# view の OID に名前で書けるもの(IOS と同じ名前)
OID_NAMES = {
    'iso': '1', 'org': '1.3', 'dod': '1.3.6', 'internet': '1.3.6.1', 'mgmt': '1.3.6.1.2', 'mib-2': '1.3.6.1.2.1',
    'system': '1.3.6.1.2.1.1', 'interfaces': '1.3.6.1.2.1.2', 'ip': '1.3.6.1.2.1.4', 'ifmib': '1.3.6.1.2.1.31',
    'private': '1.3.6.1.4', 'enterprises': '1.3.6.1.4.1', 'cisco': '1.3.6.1.4.1.9', 'snmpv2': '1.3.6.1.6',
    'ospf': '1.3.6.1.2.1.14', 'ipforward': '1.3.6.1.2.1.4.24',
}
LEVEL_RANK = {'noauth': 0, 'auth': 1, 'priv': 2}
AUTH_SHOW = {'md5': 'MD5', 'sha': 'SHA', 'sha224': 'SHA-224', 'sha256': 'SHA-256', 'sha384': 'SHA-384', 'sha512': 'SHA-512'}
PRIV_SHOW = {'des': 'DES', 'aes128': 'AES128', 'aes192c': 'AES192', 'aes256c': 'AES256', 'aes192': 'AES192', 'aes256': 'AES256'}


# ── 設定の保管 ─────────────────────────────────────
def default_engine_id(device_id):
    """Cisco と同じ形(企業番号 9・MAC 形式)。MAC は装置 ID から決める"""
    mac = hashlib.md5(('snmp-engine:' + device_id).encode()).hexdigest()[:12]
    return '800000090300' + mac.upper()


def config(state, device_id=None):
    cfg = getattr(state, 'snmp_v3', None)
    if not isinstance(cfg, dict):
        cfg = {}
        try:
            state.snmp_v3 = cfg
        except Exception:
            pass
    cfg.setdefault('engine_id', default_engine_id(device_id or getattr(state, 'hostname', 'dev')))
    cfg.setdefault('engine_id_set', False)
    cfg.setdefault('boots', 1)
    cfg.setdefault('groups', {})
    cfg.setdefault('users', {})
    cfg.setdefault('views', {})
    return cfg


def _oid_of(name):
    n = name.strip('.')
    if re.fullmatch(r'[\d.]+', n):
        return n
    base = OID_NAMES.get(n.lower())
    if base is None:
        raise ValueError(f'OID の名前 {name} が分かりません(数字で書くか {", ".join(sorted(OID_NAMES))})')
    return base


def in_view(cfg, view, oid):
    """view が None なら全部読める(IOS の v1default 相当)。いちばん長く一致した行で決める"""
    if not view:
        return True
    rows = cfg['views'].get(view)
    if not rows:
        return False
    parts = oid.strip('.').split('.')
    best, inc = -1, False
    for r in rows:
        sub = r['oid'].split('.')
        if parts[:len(sub)] == sub and len(sub) > best:
            best, inc = len(sub), r['type'] == 'included'
    return inc


def _make_user(cfg, name, group, auth=None, auth_pw=None, priv=None, priv_pw=None, localized=False):
    eid = bytes.fromhex(cfg['engine_id'])
    u = {'group': group, 'auth': None, 'auth_key': None, 'priv': None, 'priv_key': None,
         'engine_id': cfg['engine_id']}
    if auth:
        u['auth'] = usm.norm_auth(auth)
        if localized:
            u['auth_key'] = _hex(auth_pw)
        else:
            u['auth_key'] = usm.auth_key(u['auth'], auth_pw, eid).hex()
    if priv:
        u['priv'] = usm.norm_priv(priv)
        if localized:
            u['priv_key'] = _hex(priv_pw)
        else:
            u['priv_key'] = usm.priv_key(u['auth'], u['priv'], priv_pw, eid).hex()
    cfg['users'][name] = u
    return u


def _hex(v):
    v = v[2:] if v.lower().startswith('0x') else v
    bytes.fromhex(v)
    return v.lower()


def user_level(u):
    return 'priv' if u.get('priv') else ('auth' if u.get('auth') else 'noauth')


# ── CLI ──────────────────────────────────────────────
_RE_ENGINE = re.compile(r'^snmp-server\s+engineid\s+local\s+([0-9a-f:]{10,80})\s*$', re.I)
_RE_VIEW = re.compile(r'^snmp-server\s+view\s+(\S+)\s+(\S+)\s+(included|excluded)\s*$', re.I)
_RE_GROUP = re.compile(r'^snmp-server\s+group\s+(\S+)\s+v3\s+(noauth|auth|priv)((?:\s+(?:read|write|notify|access|context)\s+\S+)*)\s*$', re.I)
_RE_USER_IOS = re.compile(
    r'^snmp-server\s+user\s+(\S+)\s+(\S+)\s+v3'
    r'(?:\s+(?:encrypted\s+)?auth\s+(md5|sha|sha224|sha256|sha384|sha512)\s+(\S+)'
    r'(?:\s+priv\s+(des|3des|aes(?:\s+(?:128|192|256))?|aes128|aes192|aes256)\s+(\S+))?)?'
    r'(?:\s+access\s+(\S+))?\s*$', re.I)
_RE_USER_NX_ACL = re.compile(r'^snmp-server\s+user\s+(\S+)\s+use-(?:ipv4)?acl\s+(\S+)\s*$', re.I)
_RE_USER_NX = re.compile(
    r'^snmp-server\s+user\s+(\S+)(?:\s+(?!auth\b)(\S+))?'
    r'(?:\s+auth\s+(md5|sha|sha-224|sha-256|sha-384|sha-512)\s+(\S+)'
    r'(?:\s+priv(?:\s+(aes-128))?\s+(\S+))?(?:\s+(localizedkey|localizedv2key))?)?'
    r'(?:\s+engineid\s+\S+)?\s*$', re.I)


def handle(device_id, state, command):
    """SNMPv3 のコマンドなら出力(空文字含む)を、関係なければ None を返す"""
    if getattr(state, 'device_type', '') not in V3_TYPES:
        return None
    cmd = command.strip()
    low = cmd.lower()
    if low.startswith('show '):
        return _show(device_id, state, low)
    if not (low.startswith('snmp-server') or low.startswith('no snmp-server')):
        return None
    if state.mode not in ('config',):
        return None
    nx = state.device_type == 'nexus'
    try:
        return _config(device_id, state, cmd, low, nx)
    except ValueError as e:
        return f'% {e}'


def _config(device_id, state, cmd, low, nx):
    cfg = config(state, device_id)
    m = _RE_ENGINE.match(cmd)
    if m:
        eid = m.group(1).replace(':', '').upper()
        if len(eid) % 2:
            raise ValueError('engineID は16進の偶数桁で指定してください')
        cfg['engine_id'], cfg['engine_id_set'] = eid, True
        stale = [n for n, u in cfg['users'].items() if u.get('engine_id') != eid and u.get('auth')]
        if stale:   # 鍵は engineID ごとに違う。実機と同じく、作り直すまで使えない
            return ('% SNMP engineID を変更しました。次のユーザーは鍵の作り直し(snmp-server user の再設定)が必要です: '
                    + ', '.join(sorted(stale)))
        return ''
    if re.match(r'^no\s+snmp-server\s+engineid\s+local', low):
        cfg['engine_id'], cfg['engine_id_set'] = default_engine_id(device_id), False
        return ''
    m = _RE_VIEW.match(cmd)
    if m:
        name, oid, typ = m.group(1), _oid_of(m.group(2)), m.group(3).lower()
        rows = [r for r in cfg['views'].get(name, []) if r['oid'] != oid]
        rows.append({'oid': oid, 'type': typ, 'name': m.group(2)})
        cfg['views'][name] = sorted(rows, key=lambda r: [int(x) for x in r['oid'].split('.')])
        return ''
    m = re.match(r'^no\s+snmp-server\s+view\s+(\S+)(?:\s+(\S+))?', cmd, re.I)
    if m:
        if m.group(2):
            oid = _oid_of(m.group(2))
            cfg['views'][m.group(1)] = [r for r in cfg['views'].get(m.group(1), []) if r['oid'] != oid]
            if not cfg['views'][m.group(1)]:
                cfg['views'].pop(m.group(1))
        else:
            cfg['views'].pop(m.group(1), None)
        return ''
    m = _RE_GROUP.match(cmd)
    if m and not nx:
        opts = dict(re.findall(r'(read|write|notify|access|context)\s+(\S+)', m.group(3) or '', re.I))
        opts = {k.lower(): v for k, v in opts.items()}
        cfg['groups'][m.group(1)] = {'level': m.group(2).lower(), 'read': opts.get('read'),
                                      'write': opts.get('write'), 'notify': opts.get('notify'),
                                      'access': opts.get('access')}
        return ''
    m = re.match(r'^no\s+snmp-server\s+group\s+(\S+)', cmd, re.I)
    if m and not nx:
        cfg['groups'].pop(m.group(1), None)
        return ''
    m = re.match(r'^no\s+snmp-server\s+user\s+(\S+)', cmd, re.I)
    if m:
        cfg['users'].pop(m.group(1), None)
        return ''
    if not nx:
        m = _RE_USER_IOS.match(cmd)
        if m:
            name, group, auth, apw, priv, ppw, uacl = m.groups()
            if priv and priv.lower() == '3des':
                raise ValueError('3DES はこのエミュレータでは未対応です(des / aes 128|192|256 を使ってください)')
            if priv:
                p = priv.lower().replace(' ', '')
                priv = {'aes': 'aes128', 'aes128': 'aes128', 'aes192': 'aes192c', 'aes256': 'aes256c',
                        'des': 'des'}[p]
                if state.device_type == 'arista' and p in ('aes192', 'aes256'):
                    priv = p      # EOS は Blumenthal(net-snmp の AES192 / AES256 と同じ)
            _make_user(cfg, name, group, auth, apw, priv, ppw)
            if uacl:
                cfg['users'][name]['acl'] = uacl       # access <ACL>: このユーザーで問い合わせてよい送信元
            if group not in cfg['groups']:
                return f'% Warning: グループ {group} はまだありません(snmp-server group {group} v3 ... で作ってください)'
            return ''
        return None
    m = _RE_USER_NX_ACL.match(cmd)
    if m:   # NX-OS: snmp-server user <名前> use-ipv4acl <ACL>
        if m.group(1) not in cfg['users']:
            raise ValueError(f'ユーザー {m.group(1)} はまだありません')
        cfg['users'][m.group(1)]['acl'] = m.group(2)
        return ''
    m = _RE_USER_NX.match(cmd)
    if m:
        name, role, auth, apw, aes, ppw, loc = m.groups()
        auth = (auth or 'md5').lower()
        if not apw:
            raise ValueError('NX-OS の SNMPv3 ユーザーには auth <方式> <パスワード> が必要です')
        priv = ('aes128' if aes else 'des') if ppw else None
        _old_acl = (cfg['users'].get(name) or {}).get('acl')
        _make_user(cfg, name, role or 'network-operator', auth, apw, priv, ppw, localized=bool(loc))
        if _old_acl:
            cfg['users'][name]['acl'] = _old_acl
        return ''
    return None


# ── show ─────────────────────────────────────────────
def _fmt_eid(eid, nx=False):
    return ':'.join(eid[i:i + 2] for i in range(0, len(eid), 2)).lower() if nx else eid


def _show(device_id, state, low):
    nx = state.device_type == 'nexus'
    if re.match(r'^show\s+snmp\s+engine-?id\s*$', low):
        cfg = config(state, device_id)
        if nx:
            return f'Local SNMP engineID: [Hex] {cfg["engine_id"]}\n                     [Dec] ' + \
                ':'.join(str(int(cfg['engine_id'][i:i + 2], 16)) for i in range(0, len(cfg['engine_id']), 2))
        return f'Local SNMP engineID: {cfg["engine_id"]}\nRemote Engine ID          IP-addr    Port\n'
    m = re.match(r'^show\s+snmp\s+user(?:\s+(\S+))?\s*$', low)
    if m:
        cfg = config(state, device_id)
        users = {n: u for n, u in cfg['users'].items() if not m.group(1) or n.lower() == m.group(1)}
        if nx:
            out = ['_' * 74, ' ' * 26 + 'SNMP USERS', '_' * 74, '',
                   f'{"User":<30}{"Auth":<8}{"Priv(enforce)":<14}{"Groups":<31}acl_filter',
                   f'{"____":<30}{"____":<8}{"_____________":<14}{"______":<31}__________']
            for n, u in sorted(users.items()):
                pv = {'aes128': 'aes-128(no)', 'des': 'des(no)'}.get(u.get('priv'), 'no')
                out.append(f'{n:<30}{(u.get("auth") or "no"):<8}{pv:<14}{u["group"]:<31}'
                           f'{("ipv4:" + u["acl"]) if u.get("acl") else ""}'.rstrip())
            return '\n'.join(out)
        out = []
        for n, u in sorted(users.items()):
            out += [f'User name: {n}', f'Engine ID: {u.get("engine_id") or cfg["engine_id"]}',
                    'storage-type: nonvolatile\t active',
                    f'Authentication Protocol: {AUTH_SHOW.get(u.get("auth"), "None")}',
                    f'Privacy Protocol: {PRIV_SHOW.get(u.get("priv"), "None")}',
                    f'Group-name: {u["group"]}', '']
        return '\n'.join(out)
    if re.match(r'^show\s+snmp\s+group\s*$', low) and not nx:
        cfg = config(state, device_id)
        out = []
        for g, v in sorted(cfg['groups'].items()):
            out += [f'groupname: {g:<33}security model:v3 {v["level"]}',
                    f'contextname: {"<no context specified>":<31}storage-type: nonvolatile',
                    f'readview : {(v.get("read") or "v1default"):<33}writeview: {v.get("write") or "<no writeview specified>"}',
                    f'notifyview: {v.get("notify") or "<no notifyview specified>"}', 'row status: active', '']
        return '\n'.join(out)
    if re.match(r'^show\s+snmp\s+view\s*$', low) and not nx:
        cfg = config(state, device_id)
        out = []
        for name, rows in sorted(cfg['views'].items()):
            for r in rows:
                out.append(f'{name} {r["name"]} - {r["type"]} nonvolatile active')
        return '\n'.join(out)
    return None


def running_config_lines(state, nxos=False):
    cfg = getattr(state, 'snmp_v3', None)
    if not isinstance(cfg, dict):
        return []
    out = []
    if cfg.get('engine_id_set') and not nxos:
        out.append(f'snmp-server engineID local {cfg["engine_id"]}')
    for name, rows in sorted(cfg.get('views', {}).items()):
        for r in rows:
            out.append(f'snmp-server view {name} {r["name"]} {r["type"]}')
    for g, v in sorted(cfg.get('groups', {}).items()):
        opt = ''.join(f' {k} {v[k]}' for k in ('read', 'write', 'notify', 'access') if v.get(k))
        out.append(f'snmp-server group {g} v3 {v["level"]}{opt}')
    if nxos:   # NX-OS は局所化した鍵で出す(実機と同じ)。IOS は running-config に user を出さない
        for n, u in sorted(cfg.get('users', {}).items()):
            s = f'snmp-server user {n} {u["group"]}'
            if u.get('auth'):
                a = {'sha256': 'sha-256', 'sha384': 'sha-384', 'sha512': 'sha-512', 'sha224': 'sha-224'}.get(u['auth'], u['auth'])
                s += f' auth {a} 0x{u["auth_key"]}'
                if u.get('priv'):
                    s += f' priv{" aes-128" if u["priv"] == "aes128" else ""} 0x{u["priv_key"]}'
                s += ' localizedkey'
            out.append(s)
            if u.get('acl'):
                out.append(f'snmp-server user {n} use-ipv4acl {u["acl"]}')
    return out


# ── エージェント側 ───────────────────────────────────
def engine_time():
    return int(time.time() - _START) & 0x7FFFFFFF


def _count(device_id, oid):
    st = _STATS.setdefault(device_id, {})
    st[oid] = st.get(oid, 0) + 1
    return st[oid]


def stats(device_id):
    return {usm.REPORT_NAMES.get(k, k): v for k, v in _STATS.get(device_id, {}).items()}


def source_denied(device_id, acl, src_ip):
    """SNMP のアクセス制限 ACL でこの送信元を拒否するか。
    実機と同じく、存在しない ACL 名を指定しただけなら素通し(ipfilter_engine.check_source)"""
    if not acl or not src_ip:
        return False
    try:
        from engine.protocols import ipfilter_engine
        return not ipfilter_engine.check_source(device_id, acl, src_ip)
    except Exception:  # noqa: BLE001
        return False


def process(data, device_id, snmp_agent, enc, src_ip=None):
    """v3 の要求を処理して応答 bytes を返す(捨てるなら None)。
    enc は engine/snmp_udp_agent(_encode_oid / _encode_value / _tlv / _encode_int を借りる)"""
    dev = snmp_agent.devices.get(device_id) or {}
    cfg = dev.get('v3')
    if dev.get('type') not in V3_TYPES:
        return None
    if not isinstance(cfg, dict) and state_lookup:
        st = state_lookup(device_id)
        if st is not None:
            cfg = dev['v3'] = config(st, device_id)
    if not isinstance(cfg, dict):
        return None
    try:
        p = usm.parse_message(data)
    except Exception:
        return None
    if p['sec_model'] != usm.SEC_MODEL_USM:
        return None
    eid = bytes.fromhex(cfg['engine_id'])
    boots, now = cfg.get('boots', 1), engine_time()
    reportable = bool(p['flags'] & usm.FLAG_REPORTABLE)
    want_auth, want_priv = bool(p['flags'] & usm.FLAG_AUTH), bool(p['flags'] & usm.FLAG_PRIV)

    def plain_reqid():
        if p['encrypted']:
            return 0
        try:
            _c, _n, _t, body = usm.parse_scoped_pdu(p['msg_data'])
            _tg, a, b = usm.read_tlv(body, 0)
            return int.from_bytes(body[a:b], 'big', signed=True)
        except Exception:
            return 0

    def report(oid, req_id, user=None, flags=0, ctx=b''):
        n = _count(device_id, oid)
        if not reportable:
            return None
        vb = enc._tlv(0x30, enc._encode_oid(oid) + enc._encode_value('Counter32', n))
        pdu = enc._tlv(0xA8, enc._encode_int(req_id) + enc._encode_int(0) + enc._encode_int(0) + enc._tlv(0x30, vb))
        sp = usm.scoped_pdu(eid, ctx, pdu)
        if flags and user:
            return usm.build_message(p['msg_id'], flags, eid, boots, now, p['user'], sp,
                                     auth=user['auth'], akey=bytes.fromhex(user['auth_key']))
        return usm.build_message(p['msg_id'], 0, eid, boots, now, p['user'], sp)

    if p['engine_id'] != eid:
        return report(usm.OID_UNKNOWN_ENGINE_IDS, plain_reqid())
    uname = p['user'].decode('utf-8', 'replace')
    user = cfg['users'].get(uname)
    if not user:
        return report(usm.OID_UNKNOWN_USER_NAMES, plain_reqid())
    # 送信元の制限(IOS: user ... access <ACL> / group ... access <ACL>、NX-OS: user ... use-ipv4acl)。
    # 許可されていない送信元には Report も返さず黙って捨てる(実機と同じ。応答すると手掛かりになる)
    _g = cfg['groups'].get(user.get('group')) or {}
    if source_denied(device_id, user.get('acl') or _g.get('access'), src_ip):
        _count(device_id, 'acl_denied')
        return None
    if (want_priv and not want_auth) or (want_auth and not user.get('auth')) or (want_priv and not user.get('priv')):
        return report(usm.OID_UNSUPPORTED_SEC_LEVELS, plain_reqid())
    if user.get('engine_id') and user['engine_id'] != cfg['engine_id']:
        return report(usm.OID_WRONG_DIGESTS, plain_reqid())     # engineID を変えた後の古い鍵
    akey = bytes.fromhex(user['auth_key']) if user.get('auth_key') else None
    pkey = bytes.fromhex(user['priv_key']) if user.get('priv_key') else None
    if want_auth:
        if not usm.verify(data, p, user['auth'], akey):
            return report(usm.OID_WRONG_DIGESTS, plain_reqid())
        if p['boots'] != boots or abs(p['time'] - now) > usm.TIME_WINDOW:
            return report(usm.OID_NOT_IN_TIME_WINDOWS, plain_reqid(), user=user, flags=usm.FLAG_AUTH)
    sp = p['msg_data']
    if want_priv:
        try:
            sp = usm.decrypt(user['priv'], pkey, p['boots'], p['time'], p['priv_params'], p['msg_data'])
            ceid, cname, tag, body = usm.parse_scoped_pdu(sp)
        except Exception:
            return report(usm.OID_DECRYPTION_ERRORS, 0, user=user, flags=usm.FLAG_AUTH)
    else:
        try:
            ceid, cname, tag, body = usm.parse_scoped_pdu(sp)
        except Exception:
            return None
    try:
        req_id, a2, a3, oids = _parse_pdu(body)
    except Exception:
        return None

    # VACM(簡易): グループの安全レベルと read view
    req_level = 'priv' if want_priv else ('auth' if want_auth else 'noauth')
    nx = dev.get('type') == 'nexus'
    group = {'level': user_level(user), 'read': None} if nx else cfg['groups'].get(user['group'])
    err = 0
    if not group or LEVEL_RANK[req_level] < LEVEL_RANK[group['level']]:
        err = 16                                   # authorizationError
    view = group.get('read') if group else None
    if group and view and view not in cfg['views']:
        err = 16
    varbinds = []
    if not err:
        if tag == 0xA0:
            mib = {o: (t, v) for o, t, v in snmp_agent._build_mib(device_id)}
            for oid in oids:
                o = oid.strip('.')
                if o in mib and in_view(cfg, view, o):
                    varbinds.append((o,) + mib[o])
                else:
                    varbinds.append((o, None, None))
        elif tag in (0xA1, 0xA5):
            mib = [(o, t, v) for o, t, v in snmp_agent._build_mib(device_id) if in_view(cfg, view, o)]
            keys = [[int(x) for x in o.split('.')] for o, _t, _v in mib]

            def nxt(oid):
                k = [int(x) for x in oid.strip('.').split('.') if x != '']
                for i, kk in enumerate(keys):
                    if kk > k:
                        return mib[i]
                return None
            if tag == 0xA1:
                reps = [(o, 1) for o in oids]
            else:
                nonrep, maxrep = max(0, a2), max(1, a3)
                reps = [(o, 1) for o in oids[:nonrep]] + [(o, maxrep) for o in oids[nonrep:]]
            for oid, n in reps:
                cur = oid
                for _ in range(n):
                    r = nxt(cur)
                    if not r:
                        varbinds.append((cur, 'ENDOFMIBVIEW', None))
                        break
                    varbinds.append(r)
                    cur = r[0]
                    if len(varbinds) > 2000:
                        break
        else:
            err = 6                                   # SET 等: noAccess(このエミュレータの v3 は読み取りのみ)
    if err:
        varbinds = [(o.strip('.'), 'NULL', None) for o in oids]
    vbs = []
    for o, t, v in varbinds:
        if t is None:
            val = enc._tlv(0x80, b'')
        elif t == 'ENDOFMIBVIEW':
            val = enc._tlv(0x82, b'')
        elif t == 'NULL':
            val = b'\x05\x00'
        else:
            val = enc._encode_value(t, v)
        vbs.append(enc._tlv(0x30, enc._encode_oid(o) + val))
    pdu = enc._tlv(0xA2, enc._encode_int(req_id) + enc._encode_int(err) + enc._encode_int(1 if err else 0)
                   + enc._tlv(0x30, b''.join(vbs)))
    out_sp = usm.scoped_pdu(eid, cname, pdu)
    flags = (usm.FLAG_AUTH if want_auth else 0) | (usm.FLAG_PRIV if want_priv else 0)
    return usm.build_message(p['msg_id'], flags, eid, boots, now, p['user'], out_sp,
                             auth=user.get('auth'), akey=akey, priv=user.get('priv'), pkey=pkey)


def _parse_pdu(body):
    t, a, b = usm.read_tlv(body, 0)
    req_id = int.from_bytes(body[a:b], 'big', signed=True)
    t, a, b2 = usm.read_tlv(body, b)
    p2 = int.from_bytes(body[a:b2], 'big', signed=True)
    t, a, b3 = usm.read_tlv(body, b2)
    p3 = int.from_bytes(body[a:b3], 'big', signed=True)
    t, vs, ve = usm.read_tlv(body, b3)
    oids, i = [], vs
    while i < ve:
        t, s, e = usm.read_tlv(body, i)
        t2, os_, oe = usm.read_tlv(body, s)
        oids.append(_dec_oid(body[os_:oe]))
        i = e
    return req_id, p2, p3, oids


def _dec_oid(b):
    if not b:
        return ''
    out = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            out.append(v)
            v = 0
    return '.'.join(map(str, out))
