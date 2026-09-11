"""
ISMU（In-Service Model Update）のテスト

ISSUが「ソフトウェアイメージ」を入れ替えるのに対し、ISMUはリロードせずに
**データモデル(YANG)だけ** を更新する。パッケージは .dmp.bin で、命名規則は

    <プラットフォーム>-<ライセンス>.<リリース>.<DDTS ID>.dmp.bin
    例) cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin

実機は add / activate 時にイメージとプラットフォームの一致を確認し、
食い違っていればインストールを失敗させる。

install コマンドはISSUと共有しているため、.dmp.bin を扱うISMUを先に
判定し、対象外なら従来のISSU(イメージ)処理へ回している。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.rules import DeviceState, RuleEngine      # noqa: E402

PKG = 'flash:cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin'
BASE = 'cat9k-universalk9.17.09.03.CSCvk58435.dmp.bin'


def _dev():
    e = RuleEngine()
    st = DeviceState('catalyst', 'C9300')
    st.mode = 'exec'
    return e, st


def _st_of(st, base=BASE):
    for p in st.ismu['packages']:
        if p['base'] == base:
            return p['st']
    return None


# ── 状態遷移 I -> U -> C ───────────────────────────────
def test_add_then_activate_then_commit():
    e, st = _dev()
    out = e.process(f'install add file {PKG}', st)
    assert 'SUCCESS: install_add' in out
    assert _st_of(st) == 'I'

    out = e.process(f'install activate file {PKG}', st)
    assert 'SUCCESS: install_activate' in out
    assert 'does not require a reload' in out
    assert _st_of(st) == 'U'

    out = e.process('install commit', st)
    assert 'SUCCESS: install_commit' in out
    assert _st_of(st) == 'C'


def test_one_step_add_activate_commit():
    e, st = _dev()
    out = e.process(f'install add file {PKG} activate commit', st)
    assert 'SUCCESS: install_add' in out
    assert 'SUCCESS: install_activate' in out
    assert 'SUCCESS: install_commit' in out
    assert _st_of(st) == 'C'


def test_activate_with_commit_keyword():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    e.process(f'install activate file {PKG} commit', st)
    assert _st_of(st) == 'C'


def test_deactivate_then_remove():
    e, st = _dev()
    e.process(f'install add file {PKG} activate commit', st)
    assert 'SUCCESS: install_deactivate' in e.process(
        f'install deactivate file {PKG}', st)
    assert _st_of(st) == 'D'
    assert 'SUCCESS: install_remove' in e.process(
        f'install remove file {PKG}', st)
    assert _st_of(st) is None


def test_rollback_to_committed_reverts_uncommitted():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    e.process(f'install activate file {PKG}', st)
    assert _st_of(st) == 'U'
    out = e.process('install rollback to committed', st)
    assert 'SUCCESS: install_rollback' in out
    assert _st_of(st) == 'I'


def test_remove_inactive_clears_inactive_packages():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    out = e.process('install remove inactive', st)
    assert 'SUCCESS: install_remove' in out
    assert st.ismu['packages'] == []


# ── 実機同様の検証（add/activate時のイメージ・プラットフォーム一致）──
def test_platform_mismatch_fails():
    e, st = _dev()
    out = e.process(
        'install add file flash:isr4300-universalk9.17.09.03.CSCvk1.dmp.bin',
        st)
    assert 'FAILED' in out and 'Platform mismatch' in out
    assert st.ismu['packages'] == []


def test_image_version_mismatch_fails():
    e, st = _dev()
    out = e.process(
        'install add file flash:cat9k-universalk9.16.12.01.CSCvk2.dmp.bin', st)
    assert 'FAILED' in out and 'version mismatch' in out
    assert st.ismu['packages'] == []


def test_malformed_package_name_fails():
    e, st = _dev()
    out = e.process('install add file flash:garbage.dmp.bin', st)
    assert 'FAILED' in out and 'Invalid package name' in out


def test_adding_the_same_package_twice_fails():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    assert 'already added' in e.process(f'install add file {PKG}', st)


def test_activate_without_add_fails():
    e, st = _dev()
    out = e.process(f'install activate file {PKG}', st)
    assert 'FAILED' in out and 'not added' in out


def test_remove_active_package_is_refused():
    e, st = _dev()
    e.process(f'install add file {PKG} activate commit', st)
    out = e.process(f'install remove file {PKG}', st)
    assert 'FAILED' in out and 'is active' in out
    assert _st_of(st) == 'C'


# ── 表示 ───────────────────────────────────────────────
def test_show_install_summary_lists_dmp_and_img():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    out = e.process('show install summary', st)
    assert 'Type  St   Filename/Version' in out
    assert f'DMP   I    {PKG}' in out
    # イメージ側の行も従来どおり残る
    assert 'IMG   C    17.09.03' in out


def test_show_install_summary_reflects_state_transitions():
    e, st = _dev()
    e.process(f'install add file {PKG} activate commit', st)
    assert f'DMP   C    {PKG}' in e.process('show install summary', st)


def test_show_install_package_details():
    e, st = _dev()
    e.process(f'install add file {PKG} activate commit', st)
    out = e.process(f'show install package {PKG}', st)
    assert f'Package: {BASE}' in out
    assert 'Package type: DMP' in out
    assert 'Package platform: cat9k' in out
    # DDTS IDは実機表記のまま（小文字化しない）
    assert 'Package DDTS: CSCvk58435' in out
    assert 'Package state: Activated & Committed' in out


def test_package_filename_case_is_preserved():
    """DDTS ID の大小文字が保たれること

    コマンドを小文字化したものでマッチした結果をそのまま使うと
    CSCvk58435 が cscvk58435 になり、実機と表示が食い違う。
    """
    e, st = _dev()
    out = e.process(f'install add file {PKG}', st)
    assert 'CSCvk58435' in out
    assert 'cscvk58435' not in out


def test_show_install_log_records_operations():
    e, st = _dev()
    e.process(f'install add file {PKG}', st)
    e.process(f'install activate file {PKG}', st)
    out = e.process('show install log', st)
    assert 'install_add' in out
    assert 'install_activate' in out


# ── ISSU(イメージ)との共存 ─────────────────────────────
def test_image_install_still_works():
    """.dmp.bin でない通常のイメージは従来どおりISSU側が処理する"""
    e, st = _dev()
    out = e.process('install add file flash:cat9k_iosxe.17.12.01.SPA.bin', st)
    assert 'install_add' in out
    assert st.ismu['packages'] == []      # ISMU側には入らない


def test_ismu_is_not_offered_on_nexus():
    e = RuleEngine()
    st = DeviceState('nexus', 'N9K')
    st.mode = 'exec'
    assert e._cmd_ismu(f'install add file {PKG}', st) is None
