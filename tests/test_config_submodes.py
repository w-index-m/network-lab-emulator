"""
設定サブモード登録簿（CONFIG_SUBMODES）の総当たり検証

このコードベースには「新しい設定サブモードを2か所に登録しないと
コマンドがハンドラに届かない」という落とし穴があった:

  (1) RuleEngine.process() の「設定コマンドを通すモード」許可リスト
  (2) RuleEngine._cmd_exit() の戻り先リスト

片方を忘れると、投入したコマンドが _unknown() に落ちて
"% Invalid input detected" になる。ZBFW実装時に実際に踏み、
原因特定に時間を溶かしている。

現在は engine/rules.py の CONFIG_SUBMODES 登録簿から両方を導出して
いるので、ここでは**登録簿と実際の挙動が食い違っていないこと**を
全モード総当たりで固定する。新しいサブモードを足したら、
登録簿に1行足すだけでこのテストの対象になる。
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.rules import (                        # noqa: E402
    CONFIG_MODES, CONFIG_SUBMODES, DeviceState, RuleEngine,
)

engine = RuleEngine()


def _state(mode):
    st = DeviceState('catalyst', 'SW')
    st.mode = mode
    return st


# ── 登録簿の整合性 ─────────────────────────────────────
def test_config_modes_covers_config_and_all_submodes():
    assert 'config' in CONFIG_MODES
    for mode in CONFIG_SUBMODES:
        assert mode in CONFIG_MODES, f'{mode} が CONFIG_MODES に無い'
    assert len(CONFIG_MODES) == len(CONFIG_SUBMODES) + 1


@pytest.mark.parametrize('mode', sorted(CONFIG_SUBMODES))
def test_every_submode_parent_is_reachable(mode):
    """親モードは config 本体か、登録簿にある別のサブモードであること"""
    parent, _attrs = CONFIG_SUBMODES[mode]
    assert parent == 'config' or parent in CONFIG_SUBMODES, \
        f'{mode} の親 {parent} が未登録'


@pytest.mark.parametrize('mode', sorted(CONFIG_SUBMODES))
def test_no_submode_is_its_own_ancestor(mode):
    """exit を繰り返せば必ず config に到達すること（循環していない）"""
    seen = [mode]
    cur = mode
    for _ in range(len(CONFIG_SUBMODES) + 2):
        parent = CONFIG_SUBMODES[cur][0]
        if parent == 'config':
            return
        assert parent not in seen, f'サブモードが循環している: {seen}'
        seen.append(parent)
        cur = parent
    pytest.fail(f'{mode} から config に到達できない: {seen}')


# ── 実際の挙動 ─────────────────────────────────────────
@pytest.mark.parametrize('mode', sorted(CONFIG_SUBMODES))
def test_exit_returns_to_registered_parent(mode):
    parent, _attrs = CONFIG_SUBMODES[mode]
    st = _state(mode)
    engine.process('exit', st)
    assert st.mode == parent, \
        f'{mode} から exit したら {parent} に戻るはずが {st.mode}'


@pytest.mark.parametrize('mode', sorted(CONFIG_SUBMODES))
def test_exit_clears_registered_context_attrs(mode):
    """exit でそのモード専用のコンテキスト属性が消えること

    消し忘れると、次に別のサブモードへ入ったとき古い名前が残り、
    まったく関係ないオブジェクトを書き換えてしまう。
    """
    _parent, attrs = CONFIG_SUBMODES[mode]
    st = _state(mode)
    for a in attrs:
        setattr(st, a, 'STALE')
    engine.process('exit', st)
    for a in attrs:
        assert not hasattr(st, a), f'{mode} の exit で {a} が残っている'


@pytest.mark.parametrize('mode', sorted(CONFIG_SUBMODES))
def test_unknown_command_in_submode_is_not_rejected_as_invalid(mode):
    """サブモードが process() の許可リストに入っていること

    登録漏れがあると _cmd_config に届かず、そのモードで投入した
    コマンドが軒並み "% Invalid input detected" になる。
    ここでは「設定コマンドとして処理経路に乗る」ことだけを見る。
    """
    st = _state(mode)
    out = engine.process('description test-marker', st) or ''
    assert 'Invalid input' not in out, \
        f'{mode} が設定コマンドの許可リストから漏れている'


def test_exit_from_config_goes_to_exec():
    st = _state('config')
    engine.process('exit', st)
    assert st.mode == 'exec'


def test_nested_submode_exits_one_level_at_a_time():
    """入れ子は一段ずつ戻る（一気に config まで戻らない）"""
    st = _state('config-openflow-switch')
    engine.process('exit', st)
    assert st.mode == 'config-openflow'
    engine.process('exit', st)
    assert st.mode == 'config'
    engine.process('exit', st)
    assert st.mode == 'exec'


def test_end_returns_to_exec_from_any_submode():
    for mode in CONFIG_SUBMODES:
        st = _state(mode)
        engine.process('end', st)
        assert st.mode == 'exec', f'{mode} から end で exec に戻らない'
