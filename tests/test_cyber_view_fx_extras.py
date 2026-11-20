"""
/cyber-view の追加演出(起動演出・レーダー・発光のにじみと色ずれ・効果音・本物のデータの雨・タイプ表示・照準・端末の配色)

- 画面ファイルに部品がそろっていて、ラボ版と単体版が同じファイルであること
- 実際のブラウザ(Playwright。無ければスキップ)で開いて、起動演出が出て消える・?boot=0 で出ない・
  配色と効果音のボタン・FX のオン/オフ・ページのエラーが無いこと(API が無い状態でも落ちない)
"""
import filecmp
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PAGE = os.path.join(ROOT, 'static', 'cyber_view.html')
SA_PAGE = os.path.join(ROOT, 'cyber-view-standalone', 'web', 'cyber_view.html')


def _src():
    with open(PAGE, encoding='utf-8') as f:
        return f.read()


def test_standalone_copy_is_identical():
    assert filecmp.cmp(PAGE, SA_PAGE, shallow=False)


def test_page_has_all_eight_effects_and_controls():
    s = _src()
    for needle in ('id="boot"', 'id="zSe"', 'id="zTerm"',          # 1 起動演出 / 4 効果音 / 8 配色 のボタン
                   'function fxRadar(', 'function fxBloom(', 'function fxAberration(',
                   'function fxType(', 'function fxReticle(', 'function fxWord(',
                   'function setTerm(', 'function setSe(', 'window.__cvFxExtra'):
        assert needle in s, needle
    # 効果音は BGM が無くても SE のボタンで鳴る
    assert "if (!(musicOn || seOn) || !AC) return;" in s
    # 発光のにじみは遅い環境で自動で止まる(?bloom=1 で止めない)
    assert 'fxSlowMs > 4000' in s and "qBloom === '1'" in s
    # 照準はタッチの画面では出さない
    assert "(pointer: coarse)" in s


_NODE_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const base = process.argv[2], out = {}, errs = [];
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1200, height: 800 } });
  const p = await ctx.newPage();
  p.on('pageerror', e => errs.push(e.message));
  await p.goto(base + '/cyber_view.html?view=links&bgm=off');
  await p.waitForTimeout(400);
  out.bootEarly = (await p.evaluate(() => window.__cvFxExtra())).boot;
  await p.waitForTimeout(3600);
  out.bootLate = (await p.evaluate(() => window.__cvFxExtra())).boot;
  await p.click('#zTerm');
  out.term = (await p.evaluate(() => window.__cvFxExtra())).term;
  out.termAttr = await p.evaluate(() => document.documentElement.getAttribute('data-term'));
  for (let i = 0; i < 3; i++) await p.click('#zTerm');
  out.termBack = (await p.evaluate(() => window.__cvFxExtra())).term;
  await p.click('#zSe');
  out.se = (await p.evaluate(() => window.__cvFxExtra())).se;
  await p.click('#zFx');
  out.fxOff = await p.evaluate(() => [window.__cvFxExtra().fx, document.body.classList.contains('fxon')]);
  await p.click('#zFx');
  await p.mouse.move(600, 400); await p.waitForTimeout(300);
  const p2 = await (await b.newContext()).newPage();
  p2.on('pageerror', e => errs.push(e.message));
  await p2.goto(base + '/cyber_view.html?boot=0');
  await p2.waitForTimeout(300);
  out.bootSkipped = !(await p2.evaluate(() => window.__cvFxExtra())).boot;
  out.errors = errs;
  console.log(JSON.stringify(out));
  await b.close();
})();
"""


def _node_playwright():
    node = shutil.which('node')
    if not node:
        return None
    r = subprocess.run([node, '-e', "require('playwright')"], capture_output=True, timeout=30)
    return node if r.returncode == 0 else None


@pytest.mark.skipif(_node_playwright() is None, reason='node の playwright が無い環境')
def test_effects_run_in_a_real_browser_without_errors(tmp_path):
    shutil.copy(PAGE, tmp_path / 'cyber_view.html')

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(('127.0.0.1', 0), Quiet)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        script = tmp_path / 'run.js'
        script.write_text(_NODE_SCRIPT, encoding='utf-8')
        r = subprocess.run([_node_playwright(), str(script), f'http://127.0.0.1:{srv.server_address[1]}'],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]
        out = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        srv.shutdown()
    assert out['bootEarly'] is True and out['bootLate'] is False      # 1 起動演出: 出て、消える
    assert out['bootSkipped'] is True                                   # ?boot=0 では出ない
    assert out['term'] == 'green' and out['termAttr'] == 'green'        # 8 配色
    assert out['termBack'] == 'cyber'
    assert out['se'] is True                                            # 4 効果音
    assert out['fxOff'] == [False, False]                               # FX をオフにすると全部止まる
    assert out['errors'] == [], out['errors']                           # API が無くてもページは落ちない
