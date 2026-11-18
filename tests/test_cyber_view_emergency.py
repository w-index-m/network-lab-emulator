"""複数の障害が同時に起きたときの EMERGENCY 六角パネル(/cyber-view の FX)

1件なら従来の表現のまま、2件以上で上下の縁に赤い六角パネル。段数は 2〜3件で1段、4〜7件で2段、8件以上で3段。
画面(canvas)の描画なので、ここでは仕組みが入っていることと、ラボ版・単体版の画面が同一であることを確かめる
(実際の見え方は docs/cyber-view-3d.md のスクリーンショット)。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _html(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def test_emergency_panels_wired_into_fx():
    h = _html('static', 'cyber_view.html')
    assert 'function fxEmergency(t, level)' in h
    assert 'fxEmergency(t, alert);' in h                                  # fxOver から呼ばれる
    assert "ctx.fillText('EMERGENCY'" in h
    assert 'level >= 8 ? 3 : level >= 4 ? 2 : level >= 2 ? 1 : 0' in h    # 1件は従来どおり、段数は件数で増える
    assert 'window.__cvEmergency' in h                                    # 画面確認用のフック


def test_standalone_screen_is_identical():
    assert _html('static', 'cyber_view.html') == _html('cyber-view-standalone', 'web', 'cyber_view.html')
