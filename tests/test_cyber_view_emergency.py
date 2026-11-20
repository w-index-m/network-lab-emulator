"""複数の障害が同時に起きたときの EMERGENCY 六角パネル(/cyber-view の FX)

1件なら従来の表現のまま、2件以上で赤い六角パネル。段数(帯の太さ)は 2〜3件で1段、4〜7件で2段、8件以上で3段。
並べ方はエヴァ風に、横一列ではなく右上と左下の角から斜めの帯(スマホ幅は下の両隅から V 字)。
情・注・警のマークにも、左下へ斜めに小さな六角を連ねて順に点滅させる(hexCascade)。
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


def test_panels_are_diagonal_bands_and_marks_have_diagonal_tails():
    h = _html('static', 'cyber_view.html')
    assert 'const sides = W < 640' in h                                   # 広い画面は角から斜め、スマホは下の V 字
    assert 'Math.min(W * 0.3, H * 0.55)' in h                             # 帯は角に収める(真ん中を隠さない)
    assert "ctx.fillText('警', x, y + 1)" in h                            # 数個おきに大きな「警」
    assert 'function hexCascade(x, y, r, col, ch, t)' in h
    assert 'hexCascade(x, y, r, col, ch, t);' in h                        # markHex(情・注・予)から
    assert "hexCascade(ap.x, ap.y, 13, '#ff6b78', '警', t)" in h          # リンクの上の「警」から


def test_standalone_screen_is_identical():
    assert _html('static', 'cyber_view.html') == _html('cyber-view-standalone', 'web', 'cyber_view.html')
