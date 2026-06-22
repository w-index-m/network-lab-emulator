import os
os.environ['NETLAB_FAST_TIMERS'] = '1'
import sys
import asyncio
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
