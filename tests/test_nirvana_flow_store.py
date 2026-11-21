"""
ニルヴァーナ単体版: NetFlow / sFlow で受けたフローの保存と、送信元・宛先・通信量のまとめ(cvs/flow_store.py、/flows)

本物の送り手 softflowd(v9 / IPFIX)→ 設定画面が作る設定のニルヴァーナ → /api/flows/summary で数字が合うことは
docs/nirvana-gnmi-via-gnmic.md に記録。ここでは保存・まとめ方・絞り込み・CSV・古いものの削除・API を固定する。
"""
import os
import sys

import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
from cvs.flow_store import FlowStore  # noqa: E402


T0 = time.time() - 1000      # 保存時に keep_days より古いものは消えるので、今に近い時刻で試す


def _fl(t, src, dst, sport, dport, b, p=1):
    return {'t': T0 + t, 'src': src, 'dst': dst, 'proto': 'tcp', 'sport': sport, 'dport': dport, 'bytes': b, 'packets': p,
            'exporter': '192.168.1.1', 'via': 'netflow9'}


def _store(tmp_path):
    st = FlowStore(tmp_path / 'f.db', keep_days=1)
    for f in (_fl(100, '10.0.0.5', '10.0.0.1', 51000, 443, 9000, 9), _fl(110, '10.0.0.5', '10.0.0.1', 51001, 443, 1000, 1),
              _fl(120, '10.0.0.7', '10.0.0.5', 53, 40000, 500, 2), _fl(130, '10.0.0.9', '10.0.0.1', 22, 50000, 300, 3)):
        st.add(f)
    return st


def test_summary_groups_sorted_by_bytes(tmp_path):
    st = _store(tmp_path)
    s = st.summary(T0, T0 + 200)
    assert s['total'] == {'bytes': 10800, 'packets': 15, 'flows': 4}
    assert [(r['src'], r['dst'], r['bytes'], r['flows']) for r in s['rows']] == [
        ('10.0.0.5', '10.0.0.1', 10000, 2), ('10.0.0.7', '10.0.0.5', 500, 1), ('10.0.0.9', '10.0.0.1', 300, 1)]
    assert [(r['dst'], r['bytes']) for r in st.summary(T0, T0 + 200, group='dst')['rows']] == [('10.0.0.1', 10300), ('10.0.0.5', 500)]
    assert [(r['service'], r['bytes']) for r in st.summary(T0, T0 + 200, group='service')['rows']] == \
        [('tcp/443', 10000), ('tcp/53', 500), ('tcp/22', 300)]          # 小さいほうのポートをサービスとみなす
    only = st.summary(T0, T0 + 200, group='src', ip='10.0.0.5')                 # 送信元・宛先のどちらかが 10.0.0.5
    assert [(r['src'], r['bytes']) for r in only['rows']] == [('10.0.0.5', 10000), ('10.0.0.7', 500)]
    assert st.summary(T0 + 115, T0 + 200)['total']['flows'] == 2                     # 期間で切る
    with pytest.raises(ValueError):
        st.summary(T0, T0 + 200, group='x; DROP TABLE flows')


def test_csv_and_prune_and_persistence(tmp_path):
    st = _store(tmp_path)
    lines = st.export_csv(T0, T0 + 200).strip().splitlines()
    assert lines[0] == 'time,t,exporter,src,dst,proto,sport,dport,bytes,packets,via' and len(lines) == 5
    assert FlowStore(tmp_path / 'f.db').summary(T0, T0 + 200)['total']['flows'] == 4     # 再起動しても残る
    st.flush(now=T0 + 100 + 86400 + 15)                                         # keep_days=1 より古いものを消す
    assert st.summary(0, 10**10)['total']['flows'] == 2


def test_api_and_config(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import server
    from cvs import config as C
    (tmp_path / 'c.yaml').write_text(
        'devices:\n  - {id: sw1, hostname: SW1, type: catalyst, mgmt_ip: 10.0.0.1, interfaces: [{name: mgmt, ip: 10.0.0.1, prefix: 24}]}\n'
        'sources:\n  flow: {store: flows.db}\n', encoding='utf-8')
    cfg = C.load(str(tmp_path / 'c.yaml'))
    app = server.create_app(cfg, start_collector=False)
    st = app.state.collector.sec.store
    assert st.path == str(tmp_path / 'flows.db')                             # 相対パスは設定ファイルの場所から
    st.add(_fl(995, '10.0.0.5', '10.0.0.1', 51000, 443, 4000, 4))
    c = TestClient(app)
    r = c.get('/api/flows/summary?window=600').json()
    assert r['rows'][0]['bytes'] == 4000
    assert c.get('/api/flows/summary?group=bad').status_code == 400
    assert c.get('/api/flows/export.csv').text.count('\n') == 2
    assert 'フロー集計' in c.get('/flows').text
    (tmp_path / 'n.yaml').write_text(
        'devices:\n  - {id: sw1, hostname: SW1, type: catalyst, mgmt_ip: 10.0.0.1, interfaces: [{name: x}]}\n', encoding='utf-8')
    c2 = TestClient(server.create_app(C.load(str(tmp_path / 'n.yaml')), start_collector=False))
    assert c2.get('/api/flows/summary').status_code == 404                   # 保存していなければ 404
