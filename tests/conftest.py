import os
os.environ['NETLAB_FAST_TIMERS'] = '1'
os.environ.setdefault('NETLAB_CTRL_TRAFFIC', '0')   # BPDU/Hello 等の背景トラフィックは止める(ping の分だけを数えるテストのため)
os.environ.setdefault('NETLAB_WORLD_FETCH', '0')   # WORLD の外部データ(AWS / Radar / IHR)をテスト中は取りに行かない
import sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import engine.protocols as proto


def _cancel_all():
    for t in list(proto._background_tasks):
        try:
            t.cancel()
        except Exception:
            pass
    proto._background_tasks.clear()
    for eng in (proto.rip_engine, proto.ospf_engine, proto.bgp_engine, proto.stp_engine):
        for node in eng.nodes.values():
            node['enabled'] = False
            for key in ('timer_task', 'hello_task', 'bpdu_task'):
                t = node.get(key)
                if t:
                    try: t.cancel()
                    except Exception: pass
            # BGP セッションのkeepalive_taskも停止
            for sess in node.get('sessions', {}).values():
                kt = getattr(sess, 'keepalive_task', None)
                if kt:
                    try: kt.cancel()
                    except Exception: pass
            for t in node.get('expire_tasks', {}).values():
                if t:
                    try: t.cancel()
                    except Exception: pass


@pytest.fixture(autouse=True)
def _cleanup():
    _cancel_all()
    yield
    _cancel_all()


# ── 試験の様子をニルヴァーナの画面へ送る(NETLAB_ACTIVITY_URL を指定したときだけ) ──
# 例: NETLAB_ACTIVITY_URL=http://127.0.0.1:8000 pytest tests/ -q
#     → 起動中のニルヴァーナ(/cyber-view)の TEST RUN パネルに 1 件ごとの合否が流れる。
#     ログインが要るサーバーなら NETLAB_ACTIVITY_TOKEN にセッショントークンを入れる。
#     届かなくなったら以降は送らない(試験そのものは止めない)
_ACT_URL = os.environ.get('NETLAB_ACTIVITY_URL', '').rstrip('/')
_ACT_RUN = f"pytest-{os.getpid()}"
_act_on = bool(_ACT_URL)


def _act_send(**ev):
    global _act_on
    if not _act_on:
        return
    import json as _json
    import urllib.request as _ur
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('NETLAB_ACTIVITY_TOKEN'):
        headers['X-Session-Token'] = os.environ['NETLAB_ACTIVITY_TOKEN']
    try:
        req = _ur.Request(_ACT_URL + '/api/testrun/event', headers=headers,
                          data=_json.dumps({'suite': 'pytest', 'run': _ACT_RUN, **ev}).encode())
        _ur.urlopen(req, timeout=2).read()
    except Exception:  # noqa: BLE001
        _act_on = False


def pytest_sessionstart(session):
    _act_send(kind='start', name='pytest')


def pytest_runtest_logreport(report):
    if not _act_on:
        return
    if report.when == 'call' or (report.when == 'setup' and not report.passed):
        kind = 'skip' if report.skipped else 'pass' if report.passed else 'fail'
        detail = '' if kind == 'pass' else str(getattr(report, 'longreprtext', '') or '').strip().splitlines()[-1:]
        _act_send(kind=kind, name=report.nodeid, detail=detail[0][:300] if detail else '')


def pytest_sessionfinish(session, exitstatus):
    _act_send(kind='end', name='pytest', detail=f'exit {int(exitstatus)}')
