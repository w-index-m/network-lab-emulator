"""
ログからのフラップ(短時間に up/down を繰り返す)検知ルール — /cyber-view の WARN 表示用

Cisco IOS ルータ / Catalyst / Si-R(SR-S)が出すログの形式ごとに、
「同じ対象(IF・ネイバー・グループ)が window 秒の間に threshold 回以上状態を変えたら警告」
というルールを決めている。1回の down は障害(リンク断・syslog error)として既存の仕組みで
出るので、ここでは「繰り返し」だけを見る。

ルールの考え方(しきい値の根拠):
- IF フラップ      : 60秒に状態変化3回(down→up→down)。IOS の link-flap errdisable
                     (既定 10秒に5回)より緩くし、errdisable に至る前に気づけるようにする
- OSPF/EIGRP ネイバー: 120秒に状態変化3回。Dead/Hold(40s/15s)で1往復しかしない
                     通常の再起動とは区別できる
- BGP ネイバー     : 300秒に状態変化3回。BGP は Hold 180秒と遅いので広めに見る
- HSRP/VRRP        : 120秒に状態遷移4回(Master⇔Backup の往復2回)
- STP トポロジ変更  : 60秒に5回(Catalyst)。単発の TCN は正常運用でも起きる
- STP ルート変更    : 300秒に3回(Catalyst)
- MAC フラップ      : %SW_MATM-4-MACFLAP_NOTIF は IOS 自身がフラップと判定済みなので1回で警告
- link-flap errdisable: %PM-4-ERR_DISABLE: link-flap は IOS がフラップでポートを止めたので1回で警告
- Si-R の LINK / OSPF / BGP / VRRP も同じしきい値(形式だけ違う)
"""

import re

# (rule_id, 名前, 対象の機種, 正規表現, 対象キーを作る関数, 状態を返す関数, window, threshold, 説明)
# 状態関数は None を返すと「状態変化として数えない」


def _g(n):
    return lambda m: m.group(n)


def _down_up(word):
    w = word.lower()
    if w in ('down', 'administratively down', 'is down'):
        return 'down'
    return 'up'


IOS = ('cisco', 'catalyst')
SIR = ('sir', 'srs')

FLAP_RULES = [
    # ── Cisco IOS ルータ / Catalyst ─────────────────────
    {'id': 'if_flap', 'name': 'インタフェースのフラップ', 'vendors': IOS + ('nexus',),
     're': re.compile(r'%LINK-\d-(?:UPDOWN|CHANGED): Interface ([\w/.:\-]+), changed state to (administratively down|down|up)'),
     'key': _g(1), 'state': lambda m: _down_up(m.group(2)), 'window': 60, 'threshold': 3,
     'desc': '同じIFが60秒に3回以上 up/down'},
    {'id': 'ospf_flap', 'name': 'OSPFネイバーのフラップ', 'vendors': IOS + SIR + ('nexus',),
     're': re.compile(r'%OSPF-5-ADJCHG: Process \S+, Nbr ([\d.]+) .*?(?:to|changing state from \w+ to) (\w+)'),
     'key': _g(1), 'state': lambda m: 'up' if m.group(2).upper() == 'FULL' else 'down' if m.group(2).upper() == 'DOWN' else None,
     'window': 120, 'threshold': 3, 'desc': '同じネイバーが120秒に3回以上 FULL/DOWN'},
    {'id': 'bgp_flap', 'name': 'BGPネイバーのフラップ', 'vendors': IOS + SIR + ('nexus',),
     're': re.compile(r'%BGP-5-ADJCHANGE: (?:\S+ \[\w+\] )?neighbor (\S+)(?: \(AS \d+\))? (Up|Down)'),
     'key': _g(1), 'state': lambda m: m.group(2).lower(), 'window': 300, 'threshold': 3,
     'desc': '同じピアが300秒に3回以上 Up/Down'},
    {'id': 'eigrp_flap', 'name': 'EIGRPネイバーのフラップ', 'vendors': IOS,
     're': re.compile(r'%DUAL-5-NBRCHANGE: EIGRP-IPv4 \d+: Neighbor ([\d.]+).*? is (up|down)', re.I),
     'key': _g(1), 'state': lambda m: m.group(2).lower(), 'window': 120, 'threshold': 3,
     'desc': '同じネイバーが120秒に3回以上 up/down'},
    {'id': 'fhrp_flap', 'name': 'HSRP/VRRPの状態フラップ', 'vendors': IOS + SIR,
     're': re.compile(r'%(HSRP|VRRP)-\d-STATECHANGE: (\S+) Grp (\d+) state (\w+) -> (\w+)'),
     'key': lambda m: f'{m.group(1)} {m.group(2)} Grp {m.group(3)}',
     'state': lambda m: m.group(5).lower() if m.group(4) != m.group(5) else None,
     'window': 120, 'threshold': 4, 'desc': '同じグループが120秒に4回以上 状態遷移'},
    # ── Catalyst(L2) ─────────────────────────────────
    {'id': 'stp_tc_flap', 'name': 'STPトポロジ変更の多発', 'vendors': ('catalyst',),
     're': re.compile(r'%SPANTREE-6-TOPOTRAP|%STP-\d-TOPO(?:LOGY)?_CHANGE'),
     'key': lambda m: 'topology', 'state': lambda m: 'tc', 'window': 60, 'threshold': 5,
     'desc': '60秒に5回以上トポロジ変更(ループ・ポートのフラップの疑い)'},
    {'id': 'stp_root_flap', 'name': 'STPルートブリッジの変動', 'vendors': ('catalyst',),
     're': re.compile(r'%SPANTREE-5-ROOTCHANGE'),
     'key': lambda m: 'root', 'state': lambda m: 'chg', 'window': 300, 'threshold': 3,
     'desc': '300秒に3回以上ルートが変わった'},
    {'id': 'mac_flap', 'name': 'MACアドレスのフラップ', 'vendors': ('catalyst',),
     're': re.compile(r'%SW_MATM-4-MACFLAP_NOTIF: Host ([0-9a-fA-F.:]+)'),
     'key': _g(1), 'state': lambda m: 'flap', 'window': 0, 'threshold': 1,
     'desc': '同じMACが複数ポートを行き来(ループの疑い)。IOSが判定済みなので1回で警告'},
    {'id': 'errdisable_flap', 'name': 'link-flapでポート停止(errdisable)', 'vendors': IOS,
     're': re.compile(r'%PM-4-ERR_DISABLE: link-flap error detected on ([\w/.:\-]+)'),
     'key': _g(1), 'state': lambda m: 'errdisable', 'window': 0, 'threshold': 1,
     'desc': 'IOSがフラップを検出してポートを止めた'},
    # ── Si-R / SR-S ─────────────────────────────────
    {'id': 'if_flap', 'name': 'インタフェースのフラップ', 'vendors': SIR,
     're': re.compile(r'\[LINK\] (\S+) Link (Up|Down)'),
     'key': _g(1), 'state': lambda m: m.group(2).lower(), 'window': 60, 'threshold': 3,
     'desc': '同じIFが60秒に3回以上 Link Up/Down'},
    {'id': 'ospf_flap', 'name': 'OSPFネイバーのフラップ', 'vendors': SIR,
     're': re.compile(r'\[OSPF\] neighbor ([\d.]+) on \S+ (?:\(area [\d.]+\) state: (Full)|(is down))'),
     'key': _g(1), 'state': lambda m: 'up' if m.group(2) else 'down', 'window': 120, 'threshold': 3,
     'desc': '同じネイバーが120秒に3回以上 Full/down'},
    {'id': 'bgp_flap', 'name': 'BGPネイバーのフラップ', 'vendors': SIR,
     're': re.compile(r'\[BGP\] peer ([\d.]+) \(AS\d+\) (?:state: (Established)|(is down))'),
     'key': _g(1), 'state': lambda m: 'up' if m.group(2) else 'down', 'window': 300, 'threshold': 3,
     'desc': '同じピアが300秒に3回以上 Established/down'},
    {'id': 'fhrp_flap', 'name': 'HSRP/VRRPの状態フラップ', 'vendors': SIR,
     're': re.compile(r'\[VRRP\] VRID (\d+) on (\S+): state changed to (\w+)'),
     'key': lambda m: f'VRRP {m.group(2)} VRID {m.group(1)}', 'state': lambda m: m.group(3).lower(),
     'window': 120, 'threshold': 4, 'desc': '同じVRIDが120秒に4回以上 状態遷移'},
]

# 経路のフラップ(route_watch のイベントから。機種を問わない)
ROUTE_FLAP = {'id': 'route_flap', 'name': '経路のフラップ', 'window': 120, 'threshold': 4,
              'desc': '同じ宛先の経路が120秒に4回以上 追加/削除/変更'}

FLAP_RULE_NAMES = {r['id']: r['name'] for r in FLAP_RULES}
FLAP_RULE_NAMES[ROUTE_FLAP['id']] = ROUTE_FLAP['name']


def match(device_type, msg):
    """ログ1行に当てはまるルールと (key, state) を返す。該当しなければ None"""
    for r in FLAP_RULES:
        if device_type and device_type not in r['vendors']:
            continue
        m = r['re'].search(msg or '')
        if not m:
            continue
        st = r['state'](m)
        if st is None:
            return None
        return r, r['key'](m), st
    return None


def rules_table():
    """画面・API 用のルール一覧"""
    out = []
    for r in FLAP_RULES:
        out.append({'id': r['id'], 'name': r['name'], 'vendors': list(r['vendors']),
                    'window': r['window'], 'threshold': r['threshold'], 'desc': r['desc']})
    out.append({'id': ROUTE_FLAP['id'], 'name': ROUTE_FLAP['name'], 'vendors': ['*'],
                'window': ROUTE_FLAP['window'], 'threshold': ROUTE_FLAP['threshold'],
                'desc': ROUTE_FLAP['desc']})
    return out
