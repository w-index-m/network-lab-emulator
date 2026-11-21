"""
データの取り込み口: Prometheus(snmp_exporter・gnmic)/ Loki / Zabbix

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
# gnmic(prometheus 出力)が公開するメトリクス名。パス /interfaces/interface/state/... の
# "/" と "-" が "_" になる。装置は source ラベル(gnmic の target、"IP:ポート")、IF は interface_name。
GNMIC_IN = 'interfaces_interface_state_counters_in_octets'
GNMIC_OUT = 'interfaces_interface_state_counters_out_octets'
GNMIC_OPER = 'interfaces_interface_state_oper_status'


class Prometheus:
    """Prometheus から流量・IF の状態・収集できているかを読む。

    flavor(どの集め方の値を読むか):
      snmp   snmp_exporter(if_mib)。job のメトリクス ifHCIn/OutOctets・ifOperStatus、ラベル instance・ifName
      gnmic  gnmic の prometheus 出力(gNMI の OpenConfig)。gnmic_job のメトリクス
             interfaces_interface_state_counters_in/out_octets・…_oper_status、ラベル source・interface_name。
             oper-status は文字列なので gnmic 側で strings-as-labels: true にする(oper_status="UP" が付く)。
             状態の切り替わりを正しく拾うため export-timestamps: true も付ける
      both   両方読む。同じ装置・IF は snmp を優先し、無いところを gnmic で埋める
    装置は instance / source の IP(ポートを外したもの)で、設定の devices[].mgmt_ip と突き合わせる。
    """

    def __init__(self, url, job='snmp', timeout=3.0, use_proxy=False, flavor='snmp',
                 gnmic_job='gnmic', gnmic_prefix='', **_):
        self.url, self.job, self.timeout, self.use_proxy = url.rstrip('/'), job, timeout, use_proxy
        self.flavor = (flavor or 'snmp').lower()
        if self.flavor not in ('snmp', 'gnmic', 'both'):
            raise ValueError(f'sources.prometheus.flavor は snmp / gnmic / both のどれか: {flavor}')
        self.gnmic_job = gnmic_job
        p = (gnmic_prefix.rstrip('_') + '_') if gnmic_prefix else ''
        self.g_in, self.g_out, self.g_oper = p + GNMIC_IN, p + GNMIC_OUT, p + GNMIC_OPER

    @property
    def use_snmp(self):
        return self.flavor in ('snmp', 'both')

    @property
    def use_gnmic(self):
        return self.flavor in ('gnmic', 'both')

    def query(self, q):
        d = _get_json(self.url + '/api/v1/query?' + urllib.parse.urlencode({'query': q}), self.timeout, self.use_proxy)
        if d.get('status') != 'success':
            raise RuntimeError(d.get('error') or 'query failed')
        return d['data']['result']

    def _by_if(self, q, conv=float, dev_label='instance', if_label='ifName'):
        out = {}
        for row in self.query(q):
            m = row.get('metric', {})
            if dev_label in m and if_label in m:
                try:
                    out[(m[dev_label].split(':')[0], norm_if(m[if_label]))] = conv(row['value'][1])
                except (TypeError, ValueError):
                    pass
        return out

    def _g_one(self, metric):
        """装置・IF ごとに 1 本にまとめる。/state をまとめて購読すると、カウンタにも oper_status 等の
        ラベルが付き、状態が変わるたびに系列が分かれる(古い方も expiration まで残る)。max で 1 本に
        してから rate を取る(subquery)ので、分かれ目でも流量が途切れず、2 重にも数えない"""
        return f'max by (source, interface_name) ({metric}{{job="{self.gnmic_job}"}})'

    def _g_by_if(self, q, conv=float):
        return self._by_if(q, conv, 'source', 'interface_name')

    @staticmethod
    def _merge(first, second):
        out = dict(second)
        out.update(first)
        return out

    def _pair(self, snmp_in, snmp_out, g_in, g_out, default=None):
        rin, rout = {}, {}
        if self.use_snmp:
            rin, rout = snmp_in(), snmp_out()
        if self.use_gnmic:
            gi, go = g_in(), g_out()
            keys = set(gi) | set(go)
            # 同じ装置・IF が snmp で取れていれば、snmp の組をそのまま使う
            keys -= set(rin) | set(rout)
            rin = self._merge(rin, {k: gi[k] for k in keys if k in gi})
            rout = self._merge(rout, {k: go[k] for k in keys if k in go})
        return {k: (rin.get(k, default), rout.get(k, default)) for k in set(rin) | set(rout)}

    def rates(self, window_s=60):
        """{(instance, if): (in_bps, out_bps)}"""
        w = f'{max(60, int(window_s))}s'
        sel = f'{{job="{self.job}"}}'
        return self._pair(lambda: self._by_if(f'rate(ifHCInOctets{sel}[{w}]) * 8'),
                          lambda: self._by_if(f'rate(ifHCOutOctets{sel}[{w}]) * 8'),
                          lambda: self._g_by_if(f'rate({self._g_one(self.g_in)}[{w}:15s]) * 8'),
                          lambda: self._g_by_if(f'rate({self._g_one(self.g_out)}[{w}:15s]) * 8'))

    def counters(self):
        sel = f'{{job="{self.job}"}}'
        return self._pair(lambda: self._by_if(f'ifHCInOctets{sel}'),
                          lambda: self._by_if(f'ifHCOutOctets{sel}'),
                          lambda: self._g_by_if(self._g_one(self.g_in)),
                          lambda: self._g_by_if(self._g_one(self.g_out)), default=0)

    def oper_status(self):
        """{(instance, if): 'up'|'down'}(IF-MIB 1=up、2=down・7=lowerLayerDown。OpenConfig は UP / DOWN・
        LOWER_LAYER_DOWN。それ以外は判定に使わない)"""
        out = {}
        if self.use_gnmic:
            # 状態が変わると、古い oper_status の系列も gnmic の expiration まで残る。系列ごとの
            # 最後の時刻(gnmic の export-timestamps: true で gNMI の通知時刻)が新しい方を採る
            m = f'{self.g_oper}{{job="{self.gnmic_job}"}}'
            q = f'topk by (source, interface_name) (1, timestamp({m}) and ({m} == 1))'
            for row in self.query(q):
                m = row.get('metric', {})
                st = (m.get('oper_status') or '').upper()
                if 'source' in m and 'interface_name' in m:
                    k = (m['source'].split(':')[0], norm_if(m['interface_name']))
                    if st == 'UP':
                        out[k] = 'up'
                    elif st in ('DOWN', 'LOWER_LAYER_DOWN'):
                        out[k] = 'down'
        if self.use_snmp:
            for k, v in self._by_if(f'ifOperStatus{{job="{self.job}"}}', lambda x: int(float(x))).items():
                if v == 1:
                    out[k] = 'up'
                elif v in (2, 7):
                    out[k] = 'down'
        return out

    def targets_up(self):
        """{instance: True/False}(収集できているか)。gnmic は装置ごとの up を出さないので、
        snmp の分だけ(gnmic だけのときは空 = 応答なしの判定はしない)"""
        out = {}
        if not self.use_snmp:
            return out
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
