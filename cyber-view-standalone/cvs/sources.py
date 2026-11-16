"""
データの取り込み口: Prometheus(snmp_exporter)/ Loki / Zabbix

どれも標準ライブラリの urllib だけで叩く(追加の依存を増やさない)。監視基盤は同じ構内に
あることが多いので、既定ではプロキシを通さない(sources.<name>.use_proxy: true で通す)。
"""
import json
import re
import urllib.parse
import urllib.request

from .link_evidence import norm_if


def _opener(use_proxy=False):
    return urllib.request.build_opener() if use_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url, timeout, use_proxy=False, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with _opener(use_proxy).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── Prometheus ─────────────────────────────────────────
class Prometheus:
    """snmp_exporter(if_mib)が集めた値を読む。instance は装置の管理IP(ポート付きでも可)"""

    def __init__(self, url, job='snmp', timeout=3.0, use_proxy=False, **_):
        self.url, self.job, self.timeout, self.use_proxy = url.rstrip('/'), job, timeout, use_proxy

    def query(self, q):
        d = _get_json(self.url + '/api/v1/query?' + urllib.parse.urlencode({'query': q}), self.timeout, self.use_proxy)
        if d.get('status') != 'success':
            raise RuntimeError(d.get('error') or 'query failed')
        return d['data']['result']

    def _by_if(self, q, conv=float):
        out = {}
        for row in self.query(q):
            m = row.get('metric', {})
            if 'instance' in m and 'ifName' in m:
                try:
                    out[(m['instance'].split(':')[0], norm_if(m['ifName']))] = conv(row['value'][1])
                except (TypeError, ValueError):
                    pass
        return out

    def rates(self, window_s=60):
        """{(instance, if): (in_bps, out_bps)}"""
        w = f'{max(60, int(window_s))}s'
        sel = f'{{job="{self.job}"}}'
        rin = self._by_if(f'rate(ifHCInOctets{sel}[{w}]) * 8')
        rout = self._by_if(f'rate(ifHCOutOctets{sel}[{w}]) * 8')
        return {k: (rin.get(k), rout.get(k)) for k in set(rin) | set(rout)}

    def counters(self):
        sel = f'{{job="{self.job}"}}'
        cin = self._by_if(f'ifHCInOctets{sel}')
        cout = self._by_if(f'ifHCOutOctets{sel}')
        return {k: (cin.get(k, 0), cout.get(k, 0)) for k in set(cin) | set(cout)}

    def oper_status(self):
        """{(instance, if): 'up'|'down'}(IF-MIB 1=up、2=down・7=lowerLayerDown。それ以外は判定に使わない)"""
        out = {}
        for k, v in self._by_if(f'ifOperStatus{{job="{self.job}"}}', lambda x: int(float(x))).items():
            if v == 1:
                out[k] = 'up'
            elif v in (2, 7):
                out[k] = 'down'
        return out

    def targets_up(self):
        """{instance: True/False}(収集できているか)"""
        out = {}
        for row in self.query(f'up{{job="{self.job}"}}'):
            inst = row.get('metric', {}).get('instance')
            v = float(row['value'][1])
            if inst and v in (0, 1):
                out[inst.split(':')[0]] = v == 1
        return out


# ── Loki ───────────────────────────────────────────────
class Loki:
    def __init__(self, url, query='{job=~".+"}', timeout=3.0, use_proxy=False, limit=1000, **_):
        self.url, self.q, self.timeout, self.use_proxy, self.limit = url.rstrip('/'), query, timeout, use_proxy, limit

    def logs(self, start, end):
        """[{t, labels, msg}](古い順)"""
        d = _get_json(self.url + '/loki/api/v1/query_range?' + urllib.parse.urlencode(
            {'query': self.q, 'start': str(int(start * 1e9)), 'end': str(int(end * 1e9)),
             'limit': str(self.limit), 'direction': 'backward'}), self.timeout, self.use_proxy)
        if d.get('status') != 'success':
            raise RuntimeError(d.get('error') or 'query failed')
        out = []
        for st in d['data']['result']:
            lab = st.get('stream', {})
            for ts, line in st.get('values', []):
                out.append({'t': int(ts) / 1e9, 'labels': lab, 'msg': line})
        out.sort(key=lambda x: x['t'])
        return out


# ── Zabbix ─────────────────────────────────────────────
# 「Network interfaces by SNMP」系テンプレートのアイテム名(Zabbix 5.0〜7.x)
_ZBX_IF_ITEM = re.compile(r'^Interface\s+(.+?)(?:\(.*\))?:\s*(Bits received|Bits sent|Operational status)$')
# Zabbix の重大度 → この画面の重大度(0/1 は表示しない)
ZBX_SEVERITY = {5: 'high', 4: 'high', 3: 'medium', 2: 'warning'}


class Zabbix:
    """Zabbix JSON-RPC API(api_jsonrpc.php)。token は API トークン(5.4 以降)。
    7.2 以降は Authorization ヘッダ、それより前は auth パラメータで送る(両方送っておく)"""

    def __init__(self, url, token='', user='', password='', timeout=5.0, use_proxy=False, **_):
        self.url, self.token, self.timeout, self.use_proxy = url, token, timeout, use_proxy
        self.user, self.password = user, password
        self._id = 0

    def call(self, method, params, auth=True):
        self._id += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': self._id}
        headers = {'Content-Type': 'application/json-rpc'}
        if auth and self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        with _opener(self.use_proxy).open(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode())
        if 'error' in d:
            raise RuntimeError(f"zabbix {method}: {d['error'].get('data') or d['error'].get('message')}")
        return d['result']

    def ensure_auth(self):
        if not self.token and self.user:
            self.token = self.call('user.login', {'username': self.user, 'password': self.password}, auth=False)

    def hosts(self, names):
        """{host名: {'hostid', 'available': True/False/None}}(SNMP/エージェントの到達性)"""
        self.ensure_auth()
        rows = self.call('host.get', {'output': ['hostid', 'host', 'name'], 'filter': {'host': list(names)},
                                      'selectInterfaces': ['available', 'type']})
        out = {}
        for h in rows:
            av = [int(i.get('available', 0)) for i in h.get('interfaces', [])]
            # 1=応答あり 2=応答なし 0=不明
            out[h['host']] = {'hostid': h['hostid'],
                              'available': True if 1 in av else False if av and all(a == 2 for a in av) else None}
        return out

    def interface_items(self, hostids):
        """{hostid: {if: {'in_bps','out_bps','oper'}}}"""
        if not hostids:
            return {}
        rows = self.call('item.get', {'output': ['hostid', 'name', 'lastvalue', 'lastclock', 'status'],
                                      'hostids': list(hostids), 'search': {'name': 'Interface'},
                                      'selectItemDiscovery': ['disable_source']})
        out = {}
        for it in rows:
            m = _ZBX_IF_ITEM.match(it.get('name', '').strip())
            if not m:
                continue
            ent = out.setdefault(it['hostid'], {}).setdefault(norm_if(m.group(1)), {})
            if str(it.get('status')) == '1':
                # Cisco 等のテンプレートは管理者が shutdown した IF(ifAdminStatus=down)を
                # 自動検出から外し、そのアイテムを「検出ルールにより無効化」する(disable_source=1)。
                # = その IF は落とされている。手で無効にしたアイテムは無視する
                if str((it.get('itemDiscovery') or {}).get('disable_source')) == '1':
                    ent['oper'] = 'down'
                    ent['lld_disabled'] = True
                continue
            if not it.get('lastclock') or it['lastclock'] == '0':
                continue
            try:
                v = float(it.get('lastvalue') or 0)
            except ValueError:
                continue
            if m.group(2) == 'Bits received':
                ent['in_bps'] = v
            elif m.group(2) == 'Bits sent':
                ent['out_bps'] = v
            else:
                ent['oper'] = 'up' if int(v) == 1 else 'down' if int(v) in (2, 7) else None
        return out

    def problems(self, hostids):
        """[{'hostid','name','severity','clock'}](未解決の問題)"""
        out = []
        for hid in hostids:
            for p in self.call('problem.get', {'output': ['eventid', 'name', 'severity', 'clock'],
                                               'hostids': [hid], 'recent': False}):
                out.append({'hostid': hid, 'eventid': p['eventid'], 'name': p['name'],
                            'severity': int(p['severity']), 'clock': int(p['clock'])})
        return out
