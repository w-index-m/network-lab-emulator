"""
並列リンク（同一ペア間の複数物理リンク）の取り扱い

`vnet.interface_links` は {peer -> iface} と **1本しか覚えられない** ため、
主回線と予備回線を張ると後勝ちで上書きされる。そのまま「そのピアへ
向かうIF」の判定に使うと、主回線をshutdownしても予備回線側のIF名が
見えていて「まだ生きている」と誤判定する。

並列リンクを全部持っているのは `vnet.link_ifaces` のほうなので、
両方をまとめた `ifaces_between()` / `up_ifaces_between()` を経由する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import vnet                # noqa: E402

A, B = 't-par-a', 't-par-b'
PRI = 'GigabitEthernet1/0/1'
BAK = 'GigabitEthernet1/0/2'


def _setup():
    for d in (A, B):
        vnet.links.pop(d, None)
        vnet.interface_links.pop(d, None)
        vnet.down_interfaces.pop(d, None)
        if hasattr(vnet, 'link_ifaces'):
            vnet.link_ifaces.pop(d, None)
    vnet.add_link(A, B, PRI, PRI)
    vnet.add_link(A, B, BAK, BAK)


# ── 基本 ───────────────────────────────────────────────
def test_interface_links_only_remembers_one_link():
    """前提の確認: interface_links は後勝ちで1本しか残らない

    この性質があるため、ここを直接見る実装は並列リンクで壊れる。
    """
    _setup()
    assert vnet.interface_links[A][B] in (PRI, BAK)
    assert len({vnet.interface_links[A][B]}) == 1


def test_ifaces_between_returns_all_parallel_links():
    _setup()
    assert vnet.ifaces_between(A, B) == {PRI, BAK}
    assert vnet.ifaces_between(B, A) == {PRI, BAK}


def test_ifaces_between_is_empty_for_unconnected_pair():
    _setup()
    assert vnet.ifaces_between(A, 'nobody') == set()


# ── shutdown の反映 ────────────────────────────────────
def test_up_ifaces_excludes_shut_interface():
    _setup()
    vnet.interface_down(A, PRI)
    assert vnet.up_ifaces_between(A, B) == {BAK}
    # 対向側は落としていないので両方生きている
    assert vnet.up_ifaces_between(B, A) == {PRI, BAK}


def test_all_links_down_leaves_no_up_interface():
    _setup()
    vnet.interface_down(A, PRI)
    vnet.interface_down(A, BAK)
    assert vnet.up_ifaces_between(A, B) == set()


def test_interface_up_restores():
    _setup()
    vnet.interface_down(A, PRI)
    vnet.interface_up(A, PRI)
    assert vnet.up_ifaces_between(A, B) == {PRI, BAK}


# ── そのIF経由のピア解決 ───────────────────────────────
def test_get_peers_on_interface_finds_peer_on_each_parallel_link():
    """並列リンクのどちらのIFを指定してもピアが引けること

    interface_links だけを見ていた頃は、上書きで消えた側のIFを
    指定するとピアが取れなかった（shutdown時に相手へ通知が飛ばず、
    隣接が落ちない原因になる）。
    """
    _setup()
    assert vnet.get_peers_on_interface(A, PRI) == {B}
    assert vnet.get_peers_on_interface(A, BAK) == {B}


def test_get_peers_on_interface_accepts_short_name():
    _setup()
    assert vnet.get_peers_on_interface(A, 'Gi1/0/1') == {B}


def test_get_peers_on_interface_unknown_interface():
    _setup()
    assert vnet.get_peers_on_interface(A, 'GigabitEthernet1/0/9') == set()


# ── リンク生存判定 ─────────────────────────────────────
def test_edge_stays_up_while_one_parallel_link_survives():
    _setup()
    vnet.interface_down(A, PRI)
    assert vnet._edge_up(A, B) is True
    assert B in vnet.active_neighbors(A)


def test_edge_goes_down_when_every_parallel_link_is_shut():
    _setup()
    vnet.interface_down(A, PRI)
    vnet.interface_down(A, BAK)
    assert vnet._edge_up(A, B) is False
    assert B not in vnet.active_neighbors(A)


def test_live_neighbors_reflects_parallel_links():
    _setup()
    vnet.interface_down(A, PRI)
    assert B in vnet._live_neighbors(A)      # 予備が生きている
    vnet.interface_down(A, BAK)
    assert B not in vnet._live_neighbors(A)  # 全部落ちた


# ── 単一リンクのときに壊れていないこと ─────────────────
def test_single_link_behaviour_is_unchanged():
    for d in (A, B):
        vnet.links.pop(d, None)
        vnet.interface_links.pop(d, None)
        vnet.down_interfaces.pop(d, None)
        if hasattr(vnet, 'link_ifaces'):
            vnet.link_ifaces.pop(d, None)
    vnet.add_link(A, B, PRI, PRI)
    assert vnet.ifaces_between(A, B) == {PRI}
    assert vnet.get_peers_on_interface(A, PRI) == {B}
    assert vnet._edge_up(A, B) is True
    vnet.interface_down(A, PRI)
    assert vnet.up_ifaces_between(A, B) == set()
    assert vnet._edge_up(A, B) is False
