#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
network_route_injector.py

RIP / OSPF(Point-to-Point) / OSPF(broadcast・大量ネイバー) / BGP の
経路配信・ネイバー確立を1つのWindows GUI(Tkinter)にまとめた統合検証ツール。

タブ構成:
  [RIP]           標準UDPソケットで実装。管理者権限不要。
  [OSPF P2P]      Hello→2-Way→ExStart→Exchange→Full+経路注入まで実装。
  [OSPF Broadcast] Priority=0固定・DR/BDR判定込みで、大量の偽装ネイバーを同時生成。
  [BGP]           TCP(179番)ベース。管理者権限不要。ルータ側にneighbor事前設定が必要。

依存関係:
  - RIP・BGPタブ: 標準ライブラリのみで動作
  - OSPFタブ(P2P/Broadcast): scapyが必要
      pip install scapy
      Windows: Npcap(WinPcap互換モード) + 管理者権限で実行してください
      (scapy未インストールの場合、OSPFタブは無効化された案内のみ表示されます)

免責:
  検証・研修用途専用。対象ネットワークの管理者の許可なく実運用環境で
  実行しないでください。OSPF Broadcastタブは複数ネイバーを同時生成するため、
  検証用ラック・自分が管理する機器以外には絶対に実行しないでください。
"""

import multiprocessing
import os
import socket
import struct
import threading
import time
import queue
import csv
from collections import Counter

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

try:
    from scapy.all import IP, UDP, Raw, Ether, conf, send, sendp, sniff, get_if_hwaddr
    from scapy.contrib.ospf import (
        OSPF_DBDesc,
        OSPF_External_LSA,
        OSPF_Hdr,
        OSPF_Hello,
        OSPF_Link,
        OSPF_LSA_Hdr,
        OSPF_LSAck,
        OSPF_LSReq,
        OSPF_LSUpd,
        OSPF_Router_LSA,
    )
    SCAPY_AVAILABLE = True
    SCAPY_IMPORT_ERROR = None
except Exception as _e:  # ImportError等をまとめて捕捉
    SCAPY_AVAILABLE = False
    SCAPY_IMPORT_ERROR = str(_e)


# =====================================================================
# 共通ユーティリティ
# =====================================================================

def ip_to_int(ip: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip))[0]


def int_to_ip(n: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", n & 0xFFFFFFFF))


def prefix_to_netmask(prefix: int) -> str:
    mask_int = 0 if prefix <= 0 else (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return socket.inet_ntoa(struct.pack("!I", mask_int))


def parse_ip_field(label: str, raw: str) -> int:
    """IPアドレス欄を検証し、失敗時は欄名と原因が分かる日本語エラーにする。
    （socketの"illegal IP address string passed to inet_aton"は
    どの欄が悪いのか分からず不親切なため）"""
    value = raw.strip()
    if not value:
        raise ValueError(f"「{label}」が入力されていません。"
                          f"例: 192.168.1.100 の形式で入力してください。")
    try:
        return ip_to_int(value)
    except OSError:
        reason = ""
        if len(value.split('.')) != 4:
            reason = "（ドット区切りが4つの数字になっていません）"
        elif any(not part.isdigit() for part in value.split('.')):
            reason = "（全角数字や余分な文字が混ざっていませんか？半角で入力してください）"
        raise ValueError(f"「{label}」のIPアドレス形式が不正です: 「{value}」{reason}\n"
                          f"例: 192.168.1.100 の形式で入力してください。")


def is_multicast_ip(ip: str) -> bool:
    first_octet = int(ip.split(".")[0])
    return 224 <= first_octet <= 239


def multicast_ip_to_mac(ip: str) -> str:
    parts = [int(x) for x in ip.split(".")]
    return "01:00:5e:%02x:%02x:%02x" % (parts[1] & 0x7F, parts[2], parts[3])


if SCAPY_AVAILABLE:
    def scapy_send(pkt, iface):
        """
        マルチキャスト宛先はL3のsend()だとifaceが無視されOSルーティング任せに
        なる(NIC複数環境で意図しないNICから出てしまう)ため、L2で明示的に
        指定NICから送出する。ユニキャストは通常のL3送信で問題ない。
        """
        dst_ip = pkt[IP].dst
        if is_multicast_ip(dst_ip):
            mac = multicast_ip_to_mac(dst_ip)
            eth = Ether(dst=mac, src=get_if_hwaddr(iface))
            sendp(eth / pkt, iface=iface, verbose=False)
        else:
            send(pkt, iface=iface, verbose=False)


# =====================================================================
# RIP バックエンド
# =====================================================================

RIP_PORT = 520
RIPV2_MULTICAST = "224.0.0.9"
RIPV1_BROADCAST = "255.255.255.255"


def rip_build_rte(network, netmask, nexthop, metric, tag, version):
    afi = 2
    ip_bytes = socket.inet_aton(network)
    if version == 1:
        tag_bytes = struct.pack("!H", 0)
        mask_bytes = b"\x00\x00\x00\x00"
        nexthop_bytes = b"\x00\x00\x00\x00"
    else:
        tag_bytes = struct.pack("!H", tag)
        mask_bytes = socket.inet_aton(netmask)
        nexthop_bytes = socket.inet_aton(nexthop)
    return (struct.pack("!H", afi) + tag_bytes + ip_bytes + mask_bytes
            + nexthop_bytes + struct.pack("!I", metric))


def rip_build_packet(routes, version, command):
    if len(routes) > 25:
        raise ValueError("1パケットに含められる経路は最大25件です(RFC制限)")
    header = struct.pack("!BBH", command, version, 0)
    body = b"".join(
        rip_build_rte(r["network"], r["netmask"], r["nexthop"], r["metric"], r["tag"], version)
        for r in routes
    )
    return header + body


def rip_send_packet(dest_ip, packet, bind_ip, ttl):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    except OSError:
        pass
    # RFC2453: 送信元ポートは520固定でないと受信側(実機含む)に無視されることがある
    sock.bind((bind_ip or "0.0.0.0", RIP_PORT))
    sock.sendto(packet, (dest_ip, RIP_PORT))
    sock.close()


if SCAPY_AVAILABLE:
    def rip_send_packet_spoofed(iface, src_ip, dest_ip, packet, ttl=1):
        """scapyでIP/UDPを組み立て、送信元IPを詐称してRIPパケットを送る(要Npcap/管理者)"""
        pkt = (IP(src=src_ip, dst=dest_ip, ttl=ttl)
               / UDP(sport=RIP_PORT, dport=RIP_PORT)
               / Raw(load=packet))
        scapy_send(pkt, iface)


# =====================================================================
# OSPF (Point-to-Point) バックエンド ※要scapy
# =====================================================================

ALL_SPF_ROUTERS = "224.0.0.5"


if SCAPY_AVAILABLE:

    class OSPFNeighborFaker:
        STATE_DOWN = "DOWN"
        STATE_INIT = "INIT"
        STATE_2WAY = "2-WAY"
        STATE_EXSTART = "EXSTART"
        STATE_EXCHANGE = "EXCHANGE"
        STATE_FULL = "FULL"

        def __init__(self, iface, my_ip, router_id, area, mask="255.255.255.252",
                     hello_interval=10, dead_interval=40, mtu=1500,
                     rxmt_interval=5, on_log=None, debug=False):
            self.iface = iface
            self.my_ip = my_ip
            self.router_id = router_id
            self.area = area
            self.mask = mask
            self.hello_interval = hello_interval
            self.dead_interval = dead_interval
            self.mtu = mtu
            self.rxmt_interval = rxmt_interval
            self.on_log = on_log or (lambda msg: None)
            self.debug = debug

            self.state = self.STATE_DOWN
            self.peer_router_id = None
            self.peer_ip = None

            self.is_master = None
            self.my_ddseq = None
            self.peer_ddseq = None
            self.exstart_sent = False
            self.dbd_phase = None

            self.stop_event = threading.Event()
            self.full_event = threading.Event()

            conf.iface = iface  # 不正なifaceの場合はここでValueErrorが送出される

        def _log(self, msg):
            ts = time.strftime("%H:%M:%S")
            self.on_log(f"[{ts}] {msg}")

        def _set_state(self, new_state):
            if self.state != new_state:
                self._log(f"状態遷移: {self.state} -> {new_state}")
                self.state = new_state
                if new_state == self.STATE_FULL:
                    self.full_event.set()
                else:
                    self.full_event.clear()

        def _base(self, ptype):
            return IP(src=self.my_ip, dst=ALL_SPF_ROUTERS) / OSPF_Hdr(
                type=ptype, src=self.router_id, area=self.area
            )

        def _tx(self, pkt, label):
            if self.debug:
                self._log(f"[TX] {label}")
            scapy_send(pkt, self.iface)

        def _send_hello(self):
            neighbors = [self.peer_router_id] if self.peer_router_id else []
            pkt = self._base("Hello") / OSPF_Hello(
                mask=self.mask, hellointerval=self.hello_interval,
                deadinterval=self.dead_interval, options="E",
                router="0.0.0.0", backup="0.0.0.0", neighbors=neighbors,
            )
            self._tx(pkt, "Hello")

        def _send_dbdesc(self, i_bit, m_bit, ms_bit, ddseq, lsaheaders=None):
            flags = []
            if i_bit:
                flags.append("I")
            if m_bit:
                flags.append("M")
            if ms_bit:
                flags.append("MS")
            flagstr = "+".join(flags) if flags else 0
            pkt = self._base("DBDesc") / OSPF_DBDesc(
                mtu=self.mtu, options="E", dbdescr=flagstr, ddseq=ddseq,
                lsaheaders=lsaheaders or [],
            )
            self._tx(pkt, f"DBDesc(I={i_bit},M={m_bit},MS={ms_bit},seq={ddseq})")

        def _send_lsack(self, lsaheaders):
            pkt = self._base("LSAck") / OSPF_LSAck(lsaheaders=lsaheaders)
            self._tx(pkt, f"LSAck({len(lsaheaders)}件)")

        def originate_router_lsa(self, cost=10):
            """
            自分自身のRouter-LSA(area内トポロジ情報)を広告する。
            これが無いと相手のSPF計算で自分(=ASBR)への経路が解決できず、
            External LSAで注入した経路が実際のルーティングテーブルに反映されない。
            """
            if self.state != self.STATE_FULL or not self.peer_router_id:
                return False
            link = OSPF_Link(id=self.peer_router_id, data=self.my_ip, type=1, metric=cost)
            lsa = OSPF_Router_LSA(
                age=0, options="E", id=self.router_id, adrouter=self.router_id,
                seq=0x80000001, flags="E", linklist=[link],
            )
            pkt = self._base("LSUpd") / OSPF_LSUpd(lsalist=[lsa])
            self._tx(pkt, "LSUpd(Router-LSA 自分自身)")
            self._log(f"[OK] Router-LSA送信(自分 {self.router_id} <-> 相手 {self.peer_router_id})")
            return True

        def inject_external_route(self, network, mask, metric=20, tag=0):
            if self.state != self.STATE_FULL:
                self._log("[WARN] Full状態ではないため経路注入をスキップします")
                return False
            lsa = OSPF_External_LSA(
                age=0, options="E", id=network, adrouter=self.router_id,
                seq=0x80000001, mask=mask, ebit=0, metric=metric,
                fwdaddr="0.0.0.0", tag=tag,
            )
            pkt = self._base("LSUpd") / OSPF_LSUpd(lsalist=[lsa])
            self._tx(pkt, f"LSUpd(External {network}/{mask})")
            self._log(f"[OK] External LSA送信: {network}/{mask} metric={metric} tag={tag}")
            return True

        def _on_packet(self, pkt):
            if IP not in pkt or OSPF_Hdr not in pkt:
                return
            if pkt[IP].src == self.my_ip:
                return
            hdr = pkt[OSPF_Hdr]
            self.peer_ip = pkt[IP].src
            self.peer_router_id = hdr.src
            if hdr.type == 1:
                self._handle_hello(pkt[OSPF_Hello])
            elif hdr.type == 2:
                self._handle_dbdesc(pkt[OSPF_DBDesc])
            elif hdr.type == 3:
                self._handle_lsreq(pkt[OSPF_LSReq])
            elif hdr.type == 4:
                self._handle_lsupd(pkt[OSPF_LSUpd])

        def _handle_hello(self, hello):
            if self.state == self.STATE_DOWN:
                self._set_state(self.STATE_INIT)
                self._log(f"隣接ルータのHelloを検出: RouterID={self.peer_router_id} "
                           f"(IP={self.peer_ip})")
            if self.state == self.STATE_INIT and self.router_id in hello.neighbors:
                self._set_state(self.STATE_2WAY)
                self._log("双方向通信を確認(2-Way)。P2Pのため即ExStartへ進みます")
                self._start_exstart()

        def _start_exstart(self):
            if self.state != self.STATE_2WAY or self.exstart_sent:
                return
            self.exstart_sent = True
            self._set_state(self.STATE_EXSTART)
            self.my_ddseq = int(time.time()) & 0xFFFF
            self._send_dbdesc(1, 1, 1, self.my_ddseq)

        def _handle_dbdesc(self, dbd):
            if self.state == self.STATE_2WAY:
                self._start_exstart()
            if self.state not in (self.STATE_EXSTART, self.STATE_EXCHANGE):
                return
            flags = dbd.dbdescr
            i_bit, m_bit, ms_bit = "I" in flags, "M" in flags, "MS" in flags
            if self.is_master is None:
                self.is_master = ip_to_int(self.router_id) > ip_to_int(self.peer_router_id)
                self._log(f"Master/Slave判定: 自分={'Master' if self.is_master else 'Slave'}")
            if self.is_master:
                self._handle_dbdesc_master(i_bit, m_bit, ms_bit, dbd.ddseq)
            else:
                self._handle_dbdesc_slave(i_bit, m_bit, ms_bit, dbd.ddseq)

        def _handle_dbdesc_master(self, i_bit, m_bit, ms_bit, ddseq):
            if self.state == self.STATE_EXSTART:
                if (not i_bit) and (not ms_bit) and ddseq == self.my_ddseq:
                    self._set_state(self.STATE_EXCHANGE)
                    self.my_ddseq += 1
                    self.dbd_phase = "final_sent"
                    self._send_dbdesc(0, 0, 1, self.my_ddseq)
            elif self.state == self.STATE_EXCHANGE and self.dbd_phase == "final_sent":
                if (not i_bit) and (not ms_bit) and ddseq == self.my_ddseq:
                    self._set_state(self.STATE_FULL)
                    self._log("Fullに到達しました")

        def _handle_dbdesc_slave(self, i_bit, m_bit, ms_bit, ddseq):
            if i_bit and m_bit and ms_bit:
                self.peer_ddseq = ddseq
                self._send_dbdesc(0, 0, 0, self.peer_ddseq)
                self._set_state(self.STATE_EXCHANGE)
                self.dbd_phase = "waiting_final"
            elif (self.dbd_phase == "waiting_final" and not i_bit and ms_bit
                  and not m_bit and ddseq == self.peer_ddseq + 1):
                self._send_dbdesc(0, 0, 0, ddseq)
                self._set_state(self.STATE_FULL)
                self._log("Fullに到達しました")

        def _handle_lsreq(self, lsreq):
            self._log("[INFO] LSRを受信しましたが対応するLSAがありません")

        def _handle_lsupd(self, lsupd):
            headers = [
                OSPF_LSA_Hdr(age=l.age, options=l.options, type=l.type, id=l.id,
                             adrouter=l.adrouter, seq=l.seq, chksum=l.chksum, len=l.len)
                for l in lsupd.lsalist
            ]
            if headers:
                self._send_lsack(headers)
                self._log(f"LSUpdate受信({len(headers)}件) -> LSAck返送")

        def start(self):
            self.stop_event.clear()
            threading.Thread(target=self._sniff_loop, daemon=True).start()
            threading.Thread(target=self._hello_loop, daemon=True).start()
            threading.Thread(target=self._rxmt_loop, daemon=True).start()

        def stop(self):
            self.stop_event.set()

        def _sniff_loop(self):
            sniff(iface=self.iface, filter="ip proto 89", prn=self._on_packet,
                  store=False, stop_filter=lambda p: self.stop_event.is_set())

        def _hello_loop(self):
            while not self.stop_event.is_set():
                self._send_hello()
                time.sleep(self.hello_interval)

        def _rxmt_loop(self):
            while not self.stop_event.wait(self.rxmt_interval):
                if self.is_master is not True:
                    continue
                if self.state == self.STATE_EXSTART:
                    self._send_dbdesc(1, 1, 1, self.my_ddseq)
                elif self.state == self.STATE_EXCHANGE and self.dbd_phase == "final_sent":
                    self._send_dbdesc(0, 0, 1, self.my_ddseq)


    # =================================================================
    # OSPF (broadcast・大量ネイバー) バックエンド ※要scapy
    # =================================================================

    class FakeNeighbor:
        STATE_DOWN, STATE_INIT, STATE_2WAY, STATE_EXSTART, STATE_EXCHANGE, STATE_FULL = (
            "DOWN", "INIT", "2-WAY", "EXSTART", "EXCHANGE", "FULL"
        )

        def __init__(self, manager, my_ip, router_id):
            self.mgr = manager
            self.my_ip = my_ip
            self.router_id = router_id
            self.state = self.STATE_DOWN
            self.peer_router_id = None
            self.peer_ip = None
            self.peer_dr = "0.0.0.0"
            self.peer_bdr = "0.0.0.0"
            self.is_master = None
            self.my_ddseq = None
            self.peer_ddseq = None
            self.dbd_phase = None
            self.exstart_sent = False
            self.next_hello_due = 0.0

        def send_hello(self):
            neighbors = [self.peer_router_id] if self.peer_router_id else []
            pkt = IP(src=self.my_ip, dst=ALL_SPF_ROUTERS) / OSPF_Hdr(
                type="Hello", src=self.router_id, area=self.mgr.area
            ) / OSPF_Hello(
                mask=self.mgr.mask, hellointerval=self.mgr.hello_interval,
                deadinterval=self.mgr.dead_interval, options="E", prio=0,
                router="0.0.0.0", backup="0.0.0.0", neighbors=neighbors,
            )
            self.mgr.tx(pkt, self, "Hello")

        def _send_dbdesc(self, i_bit, m_bit, ms_bit, ddseq):
            flags = []
            if i_bit:
                flags.append("I")
            if m_bit:
                flags.append("M")
            if ms_bit:
                flags.append("MS")
            flagstr = "+".join(flags) if flags else 0
            dst = self.peer_ip or ALL_SPF_ROUTERS
            pkt = IP(src=self.my_ip, dst=dst) / OSPF_Hdr(
                type="DBDesc", src=self.router_id, area=self.mgr.area
            ) / OSPF_DBDesc(mtu=self.mgr.mtu, options="E", dbdescr=flagstr, ddseq=ddseq)
            self.mgr.tx(pkt, self, f"DBDesc(I={i_bit},M={m_bit},MS={ms_bit},seq={ddseq})")

        def _send_lsack(self, lsaheaders):
            dst = self.peer_ip or ALL_SPF_ROUTERS
            pkt = IP(src=self.my_ip, dst=dst) / OSPF_Hdr(
                type="LSAck", src=self.router_id, area=self.mgr.area
            ) / OSPF_LSAck(lsaheaders=lsaheaders)
            self.mgr.tx(pkt, self, f"LSAck({len(lsaheaders)}件)")

        def inject_external_route(self, network, mask, metric=20, tag=0):
            if self.state != self.STATE_FULL:
                return False
            dst = self.peer_ip or ALL_SPF_ROUTERS
            lsa = OSPF_External_LSA(
                age=0, options="E", id=network, adrouter=self.router_id,
                seq=0x80000001, mask=mask, ebit=0, metric=metric,
                fwdaddr="0.0.0.0", tag=tag,
            )
            pkt = IP(src=self.my_ip, dst=dst) / OSPF_Hdr(
                type="LSUpd", src=self.router_id, area=self.mgr.area
            ) / OSPF_LSUpd(lsalist=[lsa])
            self.mgr.tx(pkt, self, f"LSUpd(External {network}/{mask})")
            return True

        def on_hello(self, hello, peer_ip):
            self.peer_ip = peer_ip
            if self.state == self.STATE_DOWN:
                self._set_state(self.STATE_INIT)
            self.peer_dr, self.peer_bdr = hello.router, hello.backup
            if self.state == self.STATE_INIT and self.router_id in hello.neighbors:
                self._set_state(self.STATE_2WAY)
                if peer_ip in (self.peer_dr, self.peer_bdr):
                    self._start_exstart()

        def on_dbdesc(self, dbd, peer_ip):
            if self.state == self.STATE_2WAY:
                self._start_exstart()
            if self.state not in (self.STATE_EXSTART, self.STATE_EXCHANGE):
                return
            flags = dbd.dbdescr
            i_bit, m_bit, ms_bit = "I" in flags, "M" in flags, "MS" in flags
            if self.is_master is None:
                self.is_master = ip_to_int(self.router_id) > ip_to_int(self.peer_router_id)
            if self.is_master:
                self._on_dbdesc_master(i_bit, m_bit, ms_bit, dbd.ddseq)
            else:
                self._on_dbdesc_slave(i_bit, m_bit, ms_bit, dbd.ddseq)

        def _on_dbdesc_master(self, i_bit, m_bit, ms_bit, ddseq):
            if self.state == self.STATE_EXSTART:
                if (not i_bit) and (not ms_bit) and ddseq == self.my_ddseq:
                    self._set_state(self.STATE_EXCHANGE)
                    self.my_ddseq += 1
                    self.dbd_phase = "final_sent"
                    self._send_dbdesc(0, 0, 1, self.my_ddseq)
            elif self.state == self.STATE_EXCHANGE and self.dbd_phase == "final_sent":
                if (not i_bit) and (not ms_bit) and ddseq == self.my_ddseq:
                    self._set_state(self.STATE_FULL)

        def _on_dbdesc_slave(self, i_bit, m_bit, ms_bit, ddseq):
            if i_bit and m_bit and ms_bit:
                self.peer_ddseq = ddseq
                self._send_dbdesc(0, 0, 0, self.peer_ddseq)
                self._set_state(self.STATE_EXCHANGE)
                self.dbd_phase = "waiting_final"
            elif (self.dbd_phase == "waiting_final" and not i_bit and ms_bit
                  and not m_bit and ddseq == self.peer_ddseq + 1):
                self._send_dbdesc(0, 0, 0, ddseq)
                self._set_state(self.STATE_FULL)

        def on_lsreq(self, lsreq, peer_ip):
            pass

        def on_lsupd(self, lsupd, peer_ip):
            headers = [
                OSPF_LSA_Hdr(age=l.age, options=l.options, type=l.type, id=l.id,
                             adrouter=l.adrouter, seq=l.seq, chksum=l.chksum, len=l.len)
                for l in lsupd.lsalist
            ]
            if headers:
                self._send_lsack(headers)

        def _start_exstart(self):
            if self.state != self.STATE_2WAY or self.exstart_sent:
                return
            self.exstart_sent = True
            self._set_state(self.STATE_EXSTART)
            self.my_ddseq = int(time.time() * 1000) & 0xFFFF
            self._send_dbdesc(1, 1, 1, self.my_ddseq)

        def rxmt_tick(self):
            if self.is_master is not True:
                return
            if self.state == self.STATE_EXSTART:
                self._send_dbdesc(1, 1, 1, self.my_ddseq)
            elif self.state == self.STATE_EXCHANGE and self.dbd_phase == "final_sent":
                self._send_dbdesc(0, 0, 1, self.my_ddseq)

        def _set_state(self, new_state):
            if self.state != new_state:
                self.mgr.on_state_change(self, self.state, new_state)
                self.state = new_state


    class OSPFMassNeighborManager:
        def __init__(self, iface, area, mask, hello_interval=10, dead_interval=40,
                     mtu=1500, rxmt_interval=5, stagger=0.2, on_log=None, debug=False):
            self.iface = iface
            self.area = area
            self.mask = mask
            self.hello_interval = hello_interval
            self.dead_interval = dead_interval
            self.mtu = mtu
            self.rxmt_interval = rxmt_interval
            self.stagger = stagger
            self.on_log = on_log or (lambda msg: None)
            self.debug = debug

            self.neighbors = []
            self.by_my_ip = {}
            self.stop_event = threading.Event()

            conf.iface = iface

        def add_neighbor(self, my_ip, router_id):
            n = FakeNeighbor(self, my_ip, router_id)
            self.neighbors.append(n)
            self.by_my_ip[my_ip] = n
            return n

        def tx(self, pkt, neighbor, label):
            if self.debug:
                self.on_log(f"[TX] {neighbor.router_id}({neighbor.my_ip}): {label}")
            scapy_send(pkt, self.iface)

        def on_state_change(self, neighbor, old, new):
            ts = time.strftime("%H:%M:%S")
            self.on_log(f"[{ts}] {neighbor.router_id}({neighbor.my_ip}): {old} -> {new}")

        def _on_packet(self, pkt):
            if IP not in pkt or OSPF_Hdr not in pkt:
                return
            src_ip, dst_ip = pkt[IP].src, pkt[IP].dst
            if src_ip in self.by_my_ip:
                return
            hdr = pkt[OSPF_Hdr]
            peer_router_id = hdr.src
            if hdr.type == 1:
                hello = pkt[OSPF_Hello]
                for n in self.neighbors:
                    n.peer_router_id = peer_router_id
                    n.on_hello(hello, src_ip)
                return
            target = self.by_my_ip.get(dst_ip)
            if target is None:
                return
            target.peer_router_id = peer_router_id
            if hdr.type == 2:
                target.on_dbdesc(pkt[OSPF_DBDesc], src_ip)
            elif hdr.type == 3:
                target.on_lsreq(pkt[OSPF_LSReq], src_ip)
            elif hdr.type == 4:
                target.on_lsupd(pkt[OSPF_LSUpd], src_ip)

        def start(self):
            self.stop_event.clear()
            threading.Thread(target=self._sniff_loop, daemon=True).start()
            threading.Thread(target=self._scheduler_loop, daemon=True).start()
            threading.Thread(target=self._rxmt_loop, daemon=True).start()

        def stop(self):
            self.stop_event.set()

        def _sniff_loop(self):
            sniff(iface=self.iface, filter="ip proto 89", prn=self._on_packet,
                  store=False, stop_filter=lambda p: self.stop_event.is_set())

        def _scheduler_loop(self):
            now = time.time()
            for i, n in enumerate(self.neighbors):
                n.next_hello_due = now + i * self.stagger
            while not self.stop_event.is_set():
                now = time.time()
                for n in self.neighbors:
                    if now >= n.next_hello_due:
                        n.send_hello()
                        n.next_hello_due = now + self.hello_interval
                time.sleep(0.1)

        def _rxmt_loop(self):
            while not self.stop_event.wait(self.rxmt_interval):
                for n in self.neighbors:
                    n.rxmt_tick()

        def state_counts(self):
            return Counter(n.state for n in self.neighbors)


# =====================================================================
# BGP バックエンド(標準ライブラリのみ)
# =====================================================================

BGP_PORT = 179
BGP_MARKER = b"\xff" * 16

BGP_NOTIFICATION_CODES = {
    (1, 1): "Message Header Error: Connection Not Synchronized",
    (1, 2): "Message Header Error: Bad Message Length",
    (1, 3): "Message Header Error: Bad Message Type",
    (2, 1): "OPEN Message Error: Unsupported Version Number",
    (2, 2): "OPEN Message Error: Bad Peer AS(remote-as設定値の不一致の可能性)",
    (2, 3): "OPEN Message Error: Bad BGP Identifier(Router IDの重複/不正)",
    (2, 4): "OPEN Message Error: Unsupported Optional Parameter",
    (2, 6): "OPEN Message Error: Unacceptable Hold Time",
    (2, 7): "OPEN Message Error: Unsupported Capability",
    (3, 1): "UPDATE Message Error: Malformed Attribute List",
    (3, 2): "UPDATE Message Error: Unrecognized Well-known Attribute",
    (3, 3): "UPDATE Message Error: Missing Well-known Attribute",
    (3, 4): "UPDATE Message Error: Attribute Flags Error",
    (3, 5): "UPDATE Message Error: Attribute Length Error",
    (3, 6): "UPDATE Message Error: Invalid ORIGIN Attribute",
    (3, 8): "UPDATE Message Error: Invalid NEXT_HOP Attribute",
    (3, 10): "UPDATE Message Error: Invalid Network Field",
    (3, 11): "UPDATE Message Error: Malformed AS_PATH",
    (4, 0): "Hold Timer Expired",
    (5, 0): "Finite State Machine Error",
    (6, 1): "Cease: Maximum Number of Prefixes Reached",
    (6, 2): "Cease: Administrative Shutdown",
    (6, 3): "Cease: Peer De-configured",
    (6, 4): "Cease: Administrative Reset",
    (6, 5): "Cease: Connection Rejected(相手にneighbor設定がない可能性)",
    (6, 6): "Cease: Other Configuration Change",
    (6, 7): "Cease: Connection Collision Resolution",
    (6, 8): "Cease: Out of Resources",
}


def bgp_decode_notification(code, subcode):
    return BGP_NOTIFICATION_CODES.get((code, subcode), f"Unknown error (code={code}, subcode={subcode})")


def bgp_build_header(msg_type, body):
    length = 19 + len(body)
    return BGP_MARKER + struct.pack("!HB", length, msg_type) + body


def bgp_build_capabilities(local_as):
    caps = struct.pack("!BB", 1, 4) + struct.pack("!HBB", 1, 0, 1)   # Multiprotocol(IPv4/unicast)
    caps += struct.pack("!BB", 1, 4) + struct.pack("!HBB", 1, 0, 133)  # Multiprotocol(IPv4/FlowSpec, RFC5575)
    caps += struct.pack("!BB", 2, 0)                                  # Route Refresh
    caps += struct.pack("!BB", 65, 4) + struct.pack("!I", local_as)   # 4-byte AS
    return caps


# ---- BGP FlowSpec (RFC5575) : GoBGPの特徴的機能の一つ。DDoS対策(RTBH)等で使う ----

FLOWSPEC_COMPONENT_TYPES = {
    "dst": 1, "src": 2, "proto": 3, "port": 4, "dport": 5, "sport": 6,
}
IP_PROTO_NUMBERS = {"icmp": 1, "tcp": 6, "udp": 17}


def _flowspec_encode_prefix_component(comp_type, network, prefix_len):
    ip_bytes = socket.inet_aton(network)
    num_bytes = (prefix_len + 7) // 8
    return struct.pack("!BB", comp_type, prefix_len) + ip_bytes[:num_bytes]


def _flowspec_encode_numeric_component(comp_type, value):
    """
    数値系コンポーネント(proto/port等)を単一の完全一致(=)条件としてエンコードする。
    op-byteのビット構成(RFC5575 4.2.1): eol(0x80) + and(0x40) + lt(0x04)+gt(0x02)+eq(0x01) + len(0x30)
    1byte値なのでlenビットは00、equalのみ(0x01)、末尾要素なのでeol=1 -> 0x81
    """
    if value > 0xFF:
        op = 0x91  # len=01(2byte) + eq(0x01) + eol(0x80) = 1001 0001
        return struct.pack("!BBH", comp_type, op, value)
    op = 0x81  # len=00(1byte) + eq(0x01) + eol(0x80)
    return struct.pack("!BBB", comp_type, op, value)


def bgp_build_flowspec_nlri(dst=None, src=None, proto=None, port=None, dport=None, sport=None):
    """
    FlowSpecのマッチ条件(NLRI)を組み立てる。各引数は該当コンポーネントがあれば指定する。
    dst/src: 'network/prefixlen' 形式。proto: 'tcp'/'udp'/'icmp'または数値。port系: 数値。
    """
    body = b""
    if dst:
        network, plen = dst.split("/")
        body += _flowspec_encode_prefix_component(FLOWSPEC_COMPONENT_TYPES["dst"], network, int(plen))
    if src:
        network, plen = src.split("/")
        body += _flowspec_encode_prefix_component(FLOWSPEC_COMPONENT_TYPES["src"], network, int(plen))
    if proto is not None:
        proto_num = IP_PROTO_NUMBERS.get(str(proto).lower(), None)
        proto_num = proto_num if proto_num is not None else int(proto)
        body += _flowspec_encode_numeric_component(FLOWSPEC_COMPONENT_TYPES["proto"], proto_num)
    if port is not None:
        body += _flowspec_encode_numeric_component(FLOWSPEC_COMPONENT_TYPES["port"], int(port))
    if dport is not None:
        body += _flowspec_encode_numeric_component(FLOWSPEC_COMPONENT_TYPES["dport"], int(dport))
    if sport is not None:
        body += _flowspec_encode_numeric_component(FLOWSPEC_COMPONENT_TYPES["sport"], int(sport))

    if not body:
        raise ValueError("FlowSpecのマッチ条件を最低1つ指定してください")
    if len(body) < 240:
        return struct.pack("!B", len(body)) + body
    return struct.pack("!H", 0xF000 | len(body)) + body  # 2byte長(240byte以上)


def bgp_build_flowspec_traffic_rate_action(rate_mbps=0, asn=0):
    """
    traffic-rate Extended Community(type 0x8006)。rate=0 は事実上の破棄(discard)指示。
    GoBGP/多くの実装でRTBH代替として使われる代表的なFlowSpecアクション。
    """
    import struct as _struct
    return _struct.pack("!BBHf", 0x80, 0x06, asn, float(rate_mbps))


def bgp_build_flowspec_update(nlri_bytes, discard=True, traffic_rate=None, asn=0):
    """
    FlowSpec UPDATEをMP_REACH_NLRI(type14, AFI=1 SAFI=133)で組み立てる。
    Next HopはFlowSpecでは長さ0(付与しない)。
    """
    if discard:
        traffic_rate = 0.0
    ext_comm = bgp_build_flowspec_traffic_rate_action(rate_mbps=traffic_rate or 0.0, asn=asn)
    path_attrs = struct.pack("!BBB", 0x40, 1, 1) + struct.pack("!B", 0)  # ORIGIN
    path_attrs += struct.pack("!BBB", 0x40, 2, 0)  # AS_PATH(空。FlowSpecはローカルスコープが一般的)
    # MP_REACH_NLRI: AFI(2) SAFI(1) NextHopLen(1)=0 NextHop(0) Reserved(1)=0 NLRI
    mp_value = struct.pack("!HBB", 1, 133, 0) + struct.pack("!B", 0) + nlri_bytes
    path_attrs += struct.pack("!BBB", 0x80, 14, len(mp_value)) + mp_value
    # Extended Community(traffic-rate)
    path_attrs += struct.pack("!BBB", 0xC0, 16, len(ext_comm)) + ext_comm
    body = struct.pack("!H", 0) + struct.pack("!H", len(path_attrs)) + path_attrs
    return bgp_build_header(2, body)


def bgp_build_flowspec_withdraw(nlri_bytes):
    """MP_UNREACH_NLRI(type15)でFlowSpecルールを撤回する"""
    mp_value = struct.pack("!HB", 1, 133) + nlri_bytes
    path_attrs = struct.pack("!BBB", 0x80, 15, len(mp_value)) + mp_value
    body = struct.pack("!H", 0) + struct.pack("!H", len(path_attrs)) + path_attrs
    return bgp_build_header(2, body)


def bgp_build_open(local_as, hold_time, router_id):
    caps = bgp_build_capabilities(local_as)
    opt_params = struct.pack("!BB", 2, len(caps)) + caps
    my_as_field = 23456 if local_as > 0xFFFF else local_as
    body = struct.pack("!BHH4sB", 4, my_as_field, hold_time,
                        socket.inet_aton(router_id), len(opt_params)) + opt_params
    return bgp_build_header(1, body)


def bgp_build_keepalive():
    return bgp_build_header(4, b"")


def bgp_build_notification(code, subcode, data=b""):
    return bgp_build_header(3, struct.pack("!BB", code, subcode) + data)


WELL_KNOWN_COMMUNITIES = {
    "no-export": 0xFFFFFF01,
    "no-advertise": 0xFFFFFF02,
    "no-export-subconfed": 0xFFFFFF03,
    "blackhole": 0xFFFF029A,  # RFC7999 (65535:666)
}


def bgp_parse_community(token: str) -> int:
    """'65001:100' 形式、または no-export/no-advertise/blackhole 等の既知名を32bit値に変換"""
    token = token.strip()
    if token.lower() in WELL_KNOWN_COMMUNITIES:
        return WELL_KNOWN_COMMUNITIES[token.lower()]
    asn_str, val_str = token.split(":")
    return (int(asn_str) << 16) | int(val_str)


def bgp_parse_large_community(token: str) -> tuple:
    """'65001:1:2' 形式(Global Admin:Local Data1:Local Data2) をパース(RFC8092)"""
    ga, ld1, ld2 = token.strip().split(":")
    return (int(ga), int(ld1), int(ld2))


def bgp_parse_extended_community(token: str) -> bytes:
    """
    'rt:65001:100' (Route Target) / 'soo:65001:100' (Site of Origin) を8byteにエンコード(RFC4360)
    2-byte AS Specific形式(Type 0x00)のみ対応(64bitの拡張コミュニティ全種は非対応)
    """
    kind, asn_str, val_str = token.strip().split(":")
    subtype = {"rt": 0x02, "soo": 0x03}.get(kind.lower())
    if subtype is None:
        raise ValueError(f"未対応のExtended Community種別です(rt/soo のみ対応): {kind}")
    asn = int(asn_str)
    val = int(val_str)
    return struct.pack("!BBHI", 0x00, subtype, asn, val)


def bgp_encode_prefix(network, prefix_len):
    num_bytes = (prefix_len + 7) // 8
    ip_bytes = socket.inet_aton(network)
    return struct.pack("!B", prefix_len) + ip_bytes[:num_bytes]


def bgp_build_path_attributes(next_hop, as_path, is_ibgp, origin=0, med=None, local_pref=None,
                               communities=None, large_communities=None, ext_communities=None):
    attrs = struct.pack("!BBB", 0x40, 1, 1) + struct.pack("!B", origin)
    if as_path:
        as_seq = b"".join(struct.pack("!I", asn) for asn in as_path)
        as_path_value = struct.pack("!BB", 2, len(as_path)) + as_seq
    else:
        as_path_value = b""
    attrs += struct.pack("!BBB", 0x40, 2, len(as_path_value)) + as_path_value
    attrs += struct.pack("!BBB", 0x40, 3, 4) + socket.inet_aton(next_hop)
    if med is not None:
        attrs += struct.pack("!BBB", 0x80, 4, 4) + struct.pack("!I", med)
    if is_ibgp:
        lp = local_pref if local_pref is not None else 100
        attrs += struct.pack("!BBB", 0x40, 5, 4) + struct.pack("!I", lp)
    if communities:
        comm_value = b"".join(struct.pack("!I", c) for c in communities)
        attrs += struct.pack("!BBB", 0xC0, 8, len(comm_value)) + comm_value
    if ext_communities:
        # type16 = Extended Communities(optional transitive)
        ext_value = b"".join(ext_communities)
        attrs += struct.pack("!BBB", 0xC0, 16, len(ext_value)) + ext_value
    if large_communities:
        # type32 = Large Communities(optional transitive, RFC8092)
        lc_value = b"".join(struct.pack("!III", ga, ld1, ld2) for ga, ld1, ld2 in large_communities)
        attrs += struct.pack("!BBB", 0xC0, 32, len(lc_value)) + lc_value
    return attrs


def bgp_build_update(routes, next_hop, as_path, is_ibgp, med=None, local_pref=None,
                      withdrawn=None, communities=None, large_communities=None, ext_communities=None):
    withdrawn_bytes = b"".join(bgp_encode_prefix(n, p) for n, p in (withdrawn or []))
    path_attrs = (bgp_build_path_attributes(next_hop, as_path, is_ibgp, med=med,
                                             local_pref=local_pref, communities=communities,
                                             large_communities=large_communities,
                                             ext_communities=ext_communities)
                  if routes else b"")
    nlri_bytes = b"".join(bgp_encode_prefix(n, p) for n, p in routes)
    body = (struct.pack("!H", len(withdrawn_bytes)) + withdrawn_bytes
            + struct.pack("!H", len(path_attrs)) + path_attrs + nlri_bytes)
    return bgp_build_header(2, body)


class BGPSpeaker:
    STATE_IDLE = "Idle"
    STATE_CONNECT = "Connect"
    STATE_OPENSENT = "OpenSent"
    STATE_OPENCONFIRM = "OpenConfirm"
    STATE_ESTABLISHED = "Established"

    def __init__(self, peer_ip, local_as, remote_as, router_id, local_ip=None,
                 hold_time=180, port=BGP_PORT, on_log=None, connect_timeout=10):
        self.peer_ip = peer_ip
        self.local_as = local_as
        self.remote_as = remote_as
        self.router_id = router_id
        self.local_ip = local_ip
        self.hold_time = hold_time
        self.port = port
        self.on_log = on_log or (lambda msg: None)
        self.connect_timeout = connect_timeout
        self.is_ibgp = (local_as == remote_as)

        self.sock = None
        self.state = self.STATE_IDLE
        self.stop_event = threading.Event()
        self.established_event = threading.Event()
        self._send_lock = threading.Lock()

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.on_log(f"[{ts}] {msg}")

    def _set_state(self, new_state):
        if self.state != new_state:
            self._log(f"状態遷移: {self.state} -> {new_state}")
            self.state = new_state
            if new_state == self.STATE_ESTABLISHED:
                self.established_event.set()

    def _send(self, data):
        with self._send_lock:
            self.sock.sendall(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("接続がリモートにより閉じられました")
            buf += chunk
        return buf

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.local_ip:
            self.sock.bind((self.local_ip, 0))
        self.sock.settimeout(self.connect_timeout)
        self._set_state(self.STATE_CONNECT)
        self._log(f"{self.peer_ip}:{self.port} へTCP接続中...")
        self.sock.connect((self.peer_ip, self.port))
        self.sock.settimeout(None)
        self._log(f"TCP接続確立。OPEN送信(local_as={self.local_as}, hold_time={self.hold_time})")
        self._send(bgp_build_open(self.local_as, self.hold_time, self.router_id))
        self._set_state(self.STATE_OPENSENT)
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def close(self, send_cease=True):
        if send_cease and self.sock and self.state != self.STATE_IDLE:
            try:
                self._send(bgp_build_notification(6, 2))
            except OSError:
                pass
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self._set_state(self.STATE_IDLE)

    def _recv_loop(self):
        try:
            while not self.stop_event.is_set():
                header = self._recv_exact(19)
                _marker, length, mtype = struct.unpack("!16sHB", header)
                body_len = length - 19
                body = self._recv_exact(body_len) if body_len > 0 else b""
                self._handle_message(mtype, body)
        except (ConnectionError, OSError) as e:
            if not self.stop_event.is_set():
                self._log(f"[ERROR] 受信ループ終了: {e}")
            self._set_state(self.STATE_IDLE)
            self.stop_event.set()

    def _handle_message(self, mtype, body):
        if mtype == 1:
            version, peer_as_field, hold, bgp_id = struct.unpack("!BHH4s", body[:9])
            self._log(f"OPEN受信: peer_as={peer_as_field} hold={hold} "
                       f"router_id={socket.inet_ntoa(bgp_id)}")
            if peer_as_field not in (self.remote_as, 23456):
                self._log(f"[WARN] --remote-as({self.remote_as})と相手が名乗ったAS"
                           f"({peer_as_field})が一致していません")
            self._send(bgp_build_keepalive())
            self._set_state(self.STATE_OPENCONFIRM)
        elif mtype == 4:
            if self.state == self.STATE_OPENCONFIRM:
                self._set_state(self.STATE_ESTABLISHED)
        elif mtype == 2:
            self._log(f"UPDATE受信({len(body)} bytes)")
        elif mtype == 3:
            code, subcode = struct.unpack("!BB", body[:2]) if len(body) >= 2 else (0, 0)
            self._log(f"[NOTIFICATION受信] {bgp_decode_notification(code, subcode)}")
            self.stop_event.set()
            self._set_state(self.STATE_IDLE)

    def _keepalive_loop(self):
        interval = max(1, self.hold_time // 3) if self.hold_time else 30
        while not self.stop_event.wait(interval):
            if self.state in (self.STATE_OPENCONFIRM, self.STATE_ESTABLISHED):
                try:
                    self._send(bgp_build_keepalive())
                except OSError:
                    break

    def announce_flowspec(self, dst=None, src=None, proto=None, port=None, dport=None,
                          sport=None, discard=True, traffic_rate=None):
        """
        FlowSpecルールを1件広告する(RFC5575)。discard=Trueなら実質的な破棄指示
        (traffic-rate=0)、discard=Falseならtraffic_rate(Mbps)でレート制限を指示する。
        """
        if self.state != self.STATE_ESTABLISHED:
            self._log("[WARN] Established状態ではないためFlowSpec広告をスキップします")
            return False
        try:
            nlri = bgp_build_flowspec_nlri(dst=dst, src=src, proto=proto, port=port,
                                           dport=dport, sport=sport)
        except ValueError as e:
            self._log(f"[ERROR] FlowSpec NLRI生成エラー: {e}")
            return False
        pkt = bgp_build_flowspec_update(nlri, discard=discard, traffic_rate=traffic_rate,
                                        asn=self.local_as)
        self._send(pkt)
        action = "discard(rate=0)" if discard else f"rate-limit({traffic_rate}Mbps)"
        cond = ", ".join(f"{k}={v}" for k, v in
                         [("dst", dst), ("src", src), ("proto", proto),
                          ("port", port), ("dport", dport), ("sport", sport)] if v is not None)
        self._log(f"[OK] FlowSpec広告: {cond} -> action={action}")
        return nlri

    def withdraw_flowspec(self, nlri_bytes):
        if self.state != self.STATE_ESTABLISHED:
            self._log("[WARN] Established状態ではないためFlowSpec撤回をスキップします")
            return False
        pkt = bgp_build_flowspec_withdraw(nlri_bytes)
        self._send(pkt)
        self._log("[OK] FlowSpecルールを撤回しました")
        return True

    def advertise(self, routes, next_hop, as_path=None, med=None, local_pref=None,
                  communities=None, large_communities=None, ext_communities=None):
        if self.state != self.STATE_ESTABLISHED:
            self._log("[WARN] Established状態ではないため広告をスキップします")
            return False
        pkt = bgp_build_update(routes, next_hop, as_path, self.is_ibgp, med=med,
                                local_pref=local_pref, communities=communities,
                                large_communities=large_communities, ext_communities=ext_communities)
        self._send(pkt)
        comm_label = ",".join(f"0x{c:08x}" for c in communities) if communities else "(none)"
        lc_label = ",".join(f"{g}:{l1}:{l2}" for g, l1, l2 in large_communities) if large_communities else "(none)"
        for net, plen in routes:
            self._log(f"[OK] 広告: {net}/{plen} next_hop={next_hop} "
                       f"as_path={as_path or '(empty)'} med={med} local_pref={local_pref} "
                       f"community={comm_label} large-community={lc_label}")
        return True

    def withdraw(self, routes):
        if self.state != self.STATE_ESTABLISHED:
            self._log("[WARN] Established状態ではないため撤回をスキップします")
            return False
        pkt = bgp_build_update([], next_hop="0.0.0.0", as_path=None, is_ibgp=self.is_ibgp,
                                withdrawn=routes)
        self._send(pkt)
        for net, plen in routes:
            self._log(f"[OK] 撤回: {net}/{plen}")
        return True


class BgpMultiPeerManager:
    """複数のBGPSpeakerを束ねて同時に接続・自動広告するマネージャー"""

    def __init__(self, on_log=None):
        self.on_log = on_log or (lambda msg: None)
        self.entries = []  # [{"speaker", "routes", "next_hop", "as_path", "med", "local_pref", "advertised"}, ...]

    def add_peer(self, peer_ip, local_ip, local_as, remote_as, router_id, hold_time,
                 routes, next_hop=None, as_path=None, med=None, local_pref=None,
                 communities=None, port=BGP_PORT):
        def _peer_log(msg, _local_ip=local_ip):
            self.on_log(f"[{_local_ip}] {msg}")

        speaker = BGPSpeaker(
            peer_ip=peer_ip, local_as=local_as, remote_as=remote_as,
            router_id=router_id, local_ip=local_ip, hold_time=hold_time,
            port=port, on_log=_peer_log,
        )
        entry = {
            "speaker": speaker, "routes": routes,
            "next_hop": next_hop or local_ip,
            "as_path": as_path, "med": med, "local_pref": local_pref,
            "communities": communities,
            "advertised": False,
            "flap_state": "announced",
        }
        self.entries.append(entry)
        return entry

    def start_all(self):
        for entry in self.entries:
            threading.Thread(target=self._connect_worker, args=(entry,), daemon=True).start()

    def _connect_worker(self, entry):
        speaker = entry["speaker"]
        try:
            speaker.connect()
        except OSError as e:
            self.on_log(f"[{speaker.local_ip}] [ERROR] 接続失敗: {e}"
                        f"(このIPがNICに割り当てられているか確認してください)")

    def stop_all(self):
        for entry in self.entries:
            threading.Thread(target=entry["speaker"].close, kwargs={"send_cease": True}, daemon=True).start()

    def _do_advertise(self, entry):
        e = entry
        e["speaker"].advertise(e["routes"], e["next_hop"], e["as_path"], e["med"],
                                e["local_pref"], e["communities"])

    def poll_and_advertise(self):
        """Establishedに到達したピアについて、まだ広告していなければ自動で経路広告する"""
        for entry in self.entries:
            speaker = entry["speaker"]
            if speaker.state == BGPSpeaker.STATE_ESTABLISHED and not entry["advertised"]:
                self._do_advertise(entry)
                entry["advertised"] = True

    def withdraw_all(self):
        for entry in self.entries:
            speaker = entry["speaker"]
            if speaker.state == BGPSpeaker.STATE_ESTABLISHED:
                threading.Thread(target=speaker.withdraw, args=(entry["routes"],), daemon=True).start()
                entry["advertised"] = False

    def flap_tick(self):
        """全ピアぶん、announce/withdrawを1ステップ交互に進める(呼び出し側がタイマー管理する)"""
        for entry in self.entries:
            speaker = entry["speaker"]
            if speaker.state != BGPSpeaker.STATE_ESTABLISHED:
                continue
            if entry["flap_state"] == "announced":
                threading.Thread(target=speaker.withdraw, args=(entry["routes"],), daemon=True).start()
                entry["flap_state"] = "withdrawn"
            else:
                threading.Thread(target=self._do_advertise, args=(entry,), daemon=True).start()
                entry["flap_state"] = "announced"

    def state_counts(self):
        return Counter(e["speaker"].state for e in self.entries)


# =====================================================================
# GUI: 共通部品
# =====================================================================

class LogMixin:
    """ログキュー + after()ポーリングでスレッドセーフにログ表示するMixin"""

    def _init_log(self, text_widget):
        self._log_widget = text_widget
        self._log_queue = queue.Queue()
        self._poll_log()

    def log(self, msg):
        self._log_queue.put(msg)

    def _poll_log(self):
        while not self._log_queue.empty():
            msg = self._log_queue.get_nowait()
            self._log_widget.configure(state="normal")
            self._log_widget.insert("end", msg + "\n")
            self._log_widget.see("end")
            self._log_widget.configure(state="disabled")
        self.after(150, self._poll_log)


def scapy_unavailable_notice(parent):
    frame = ttk.Frame(parent)
    msg = (
        "scapyがインストールされていないため、このタブは使用できません。\n\n"
        "コマンドプロンプトで以下を実行してから、本ツールを再起動してください:\n"
        "    pip install scapy\n\n"
        "Windowsではさらに Npcap(WinPcap互換モード)の導入と、\n"
        "管理者権限での実行が必要です。"
    )
    if SCAPY_IMPORT_ERROR:
        msg += f"\n\n(詳細: {SCAPY_IMPORT_ERROR})"
    ttk.Label(frame, text=msg, foreground="red", justify="left").pack(padx=20, pady=20, anchor="w")
    return frame


# =====================================================================
# GUI: RIPタブ
# =====================================================================

class RipTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.routes = []
        self.sending = False
        self._build_widgets()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        cfg = ttk.LabelFrame(self, text="送信設定")
        cfg.pack(fill="x", padx=8, pady=6)

        ttk.Label(cfg, text="宛先IP:").grid(row=0, column=0, sticky="e", **pad)
        self.dest_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.dest_var, width=18).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="(空欄なら v2=224.0.0.9 / v1=255.255.255.255)").grid(
            row=0, column=2, columnspan=4, sticky="w", **pad)

        ttk.Label(cfg, text="バージョン:").grid(row=1, column=0, sticky="e", **pad)
        self.version_var = tk.IntVar(value=2)
        ttk.Radiobutton(cfg, text="RIPv1", variable=self.version_var, value=1).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(cfg, text="RIPv2", variable=self.version_var, value=2).grid(row=1, column=2, sticky="w")

        ttk.Label(cfg, text="コマンド:").grid(row=1, column=3, sticky="e", **pad)
        self.command_var = tk.IntVar(value=2)
        ttk.Radiobutton(cfg, text="Response", variable=self.command_var, value=2).grid(row=1, column=4, sticky="w")
        ttk.Radiobutton(cfg, text="Request", variable=self.command_var, value=1).grid(row=1, column=5, sticky="w")

        ttk.Label(cfg, text="送信元IP固定:").grid(row=2, column=0, sticky="e", **pad)
        self.bind_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.bind_var, width=18).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(cfg, text="TTL:").grid(row=3, column=0, sticky="e", **pad)
        self.ttl_var = tk.IntVar(value=1)
        ttk.Spinbox(cfg, from_=1, to=255, textvariable=self.ttl_var, width=6).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="送信回数:").grid(row=3, column=2, sticky="e", **pad)
        self.repeat_var = tk.IntVar(value=1)
        ttk.Spinbox(cfg, from_=1, to=999, textvariable=self.repeat_var, width=6).grid(row=3, column=3, sticky="w", **pad)
        ttk.Label(cfg, text="送信間隔(秒):").grid(row=3, column=4, sticky="e", **pad)
        self.interval_var = tk.DoubleVar(value=2.0)
        ttk.Spinbox(cfg, from_=0.5, to=300, increment=0.5, textvariable=self.interval_var, width=6).grid(
            row=3, column=5, sticky="w", **pad)

        ttk.Label(cfg, text="1パケットあたりの経路数:").grid(row=4, column=0, sticky="e", **pad)
        self.chunk_var = tk.IntVar(value=5)
        ttk.Spinbox(cfg, from_=1, to=25, textvariable=self.chunk_var, width=6).grid(
            row=4, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="(RIP仕様上の上限は25。超過分は自動で複数パケットに分割)").grid(
            row=4, column=2, columnspan=4, sticky="w", **pad)

        route_frame = ttk.LabelFrame(self, text="広告する経路")
        route_frame.pack(fill="both", expand=True, padx=8, pady=6)

        ttk.Label(route_frame, text="Network:").grid(row=0, column=0, **pad)
        self.net_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.net_var, width=16).grid(row=0, column=1, **pad)
        ttk.Label(route_frame, text="Prefix:").grid(row=0, column=2, **pad)
        self.prefix_var = tk.StringVar(value="24")
        ttk.Entry(route_frame, textvariable=self.prefix_var, width=6).grid(row=0, column=3, **pad)
        ttk.Label(route_frame, text="Metric:").grid(row=0, column=4, **pad)
        self.metric_var = tk.StringVar(value="1")
        ttk.Entry(route_frame, textvariable=self.metric_var, width=6).grid(row=0, column=5, **pad)

        ttk.Label(route_frame, text="NextHop:").grid(row=1, column=0, **pad)
        self.nexthop_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(route_frame, textvariable=self.nexthop_var, width=16).grid(row=1, column=1, **pad)
        ttk.Label(route_frame, text="Tag:").grid(row=1, column=2, **pad)
        self.tag_var = tk.StringVar(value="0")
        ttk.Entry(route_frame, textvariable=self.tag_var, width=6).grid(row=1, column=3, **pad)
        ttk.Button(route_frame, text="追加", command=self._add_route).grid(row=1, column=4, **pad)
        ttk.Button(route_frame, text="選択削除", command=self._delete_route).grid(row=1, column=5, **pad)
        ttk.Button(route_frame, text="全削除", command=self._clear_routes).grid(row=1, column=6, **pad)
        ttk.Button(route_frame, text="CSVから読み込み", command=self._load_csv).grid(
            row=0, column=6, rowspan=1, sticky="e", **pad)

        columns = ("network", "metric", "nexthop", "tag")
        self.tree = ttk.Treeview(route_frame, columns=columns, show="headings", height=6)
        for c, label, w in [("network", "Network/Prefix", 160), ("metric", "Metric", 70),
                            ("nexthop", "NextHop", 140), ("tag", "Tag", 70)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        self.tree.grid(row=2, column=0, columnspan=7, sticky="nsew", padx=6, pady=6)
        route_frame.grid_rowconfigure(2, weight=1)
        route_frame.grid_columnconfigure(6, weight=1)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.send_btn = ttk.Button(action_frame, text="送信開始", command=self._on_send_clicked)
        self.send_btn.pack(side="left", padx=4)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="left", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

    def _add_route_values(self, network, prefix, metric, nexthop, tag):
        """入力検証込みで1経路をリストとTreeviewに追加する共通処理"""
        network = network.strip()
        prefix = int(prefix)
        metric = int(metric)
        nexthop = (nexthop or "0.0.0.0").strip() or "0.0.0.0"
        tag = int(tag or 0)
        socket.inet_aton(network)
        socket.inet_aton(nexthop)
        if not (0 <= prefix <= 32):
            raise ValueError(f"Prefixは0〜32で指定してください: {network}/{prefix}")
        if not (1 <= metric <= 16):
            raise ValueError(f"Metricは1〜16で指定してください: {network} metric={metric}")
        netmask = prefix_to_netmask(prefix)
        route = {"network": network, "prefix": prefix, "netmask": netmask,
                 "metric": metric, "nexthop": nexthop, "tag": tag}
        self.routes.append(route)
        self.tree.insert("", "end", values=(f"{network}/{prefix}", metric, nexthop, tag))

    def _add_route(self):
        try:
            self._add_route_values(
                self.net_var.get(), self.prefix_var.get(), self.metric_var.get(),
                self.nexthop_var.get(), self.tag_var.get(),
            )
        except (ValueError, OSError) as e:
            messagebox.showerror("入力エラー", str(e))

    def _load_csv(self):
        """
        CSVから経路を一括読み込みする。
        想定フォーマット(ヘッダー行あり): network,prefix,metric,nexthop,tag
        nexthop/tag列は省略可(無ければ 0.0.0.0 / 0 を使用)。
        例:
          network,prefix,metric,nexthop,tag
          10.10.10.0,24,1,0.0.0.0,0
          172.16.5.0,24,3,,100
        """
        path = filedialog.askopenfilename(
            title="経路CSVを選択", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        added, errors = 0, []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                required = {"network", "prefix", "metric"}
                if not required.issubset(set(h.strip().lower() for h in (reader.fieldnames or []))):
                    messagebox.showerror(
                        "CSV形式エラー",
                        "ヘッダー行に network,prefix,metric は最低限必要です\n"
                        "(nexthop,tag は省略可)"
                    )
                    return
                for row_num, row in enumerate(reader, start=2):
                    row = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
                    try:
                        self._add_route_values(
                            row.get("network", ""), row.get("prefix", ""), row.get("metric", ""),
                            row.get("nexthop", ""), row.get("tag", ""),
                        )
                        added += 1
                    except (ValueError, OSError) as e:
                        errors.append(f"{row_num}行目: {e}")
        except OSError as e:
            messagebox.showerror("読み込みエラー", str(e))
            return

        self.log(f"[CSV] {path} から{added}件読み込みました")
        if errors:
            self.log(f"[CSV] {len(errors)}件のエラーがありました:")
            for e in errors:
                self.log(f"  {e}")
            messagebox.showwarning(
                "一部読み込みエラー",
                f"{added}件読み込み成功、{len(errors)}件エラー(詳細はログ参照)"
            )
        else:
            messagebox.showinfo("読み込み完了", f"{added}件の経路を読み込みました")

    def _delete_route(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = sorted((self.tree.index(item) for item in selected), reverse=True)
        for item in selected:
            self.tree.delete(item)
        for idx in indices:
            del self.routes[idx]

    def _clear_routes(self):
        self.tree.delete(*self.tree.get_children())
        self.routes.clear()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_send_clicked(self):
        if self.sending:
            messagebox.showinfo("送信中", "送信処理が完了するまでお待ちください")
            return
        if not self.routes:
            messagebox.showwarning("経路未入力", "広告する経路を1件以上追加してください")
            return
        version = self.version_var.get()
        command = self.command_var.get()
        dest = self.dest_var.get().strip() or (RIPV2_MULTICAST if version == 2 else RIPV1_BROADCAST)
        bind_ip = self.bind_var.get().strip() or None
        ttl = self.ttl_var.get()
        repeat = self.repeat_var.get()
        interval = self.interval_var.get()
        chunk_size = self.chunk_var.get()
        self.sending = True
        self.send_btn.configure(text="送信中...", state="disabled")
        threading.Thread(target=self._send_worker,
                          args=(dest, list(self.routes), bind_ip, ttl, repeat, interval,
                                version, command, chunk_size),
                          daemon=True).start()

    def _send_worker(self, dest, routes, bind_ip, ttl, repeat, interval, version, command, chunk_size):
        cmd_label = "Request" if command == 1 else "Response"
        chunks = [routes[i:i + chunk_size] for i in range(0, len(routes), chunk_size)]
        self.log(f"=== 送信開始: {dest}:{RIP_PORT} (RIPv{version} / {cmd_label}) "
                  f"経路数={len(routes)} -> {len(chunks)}パケットに分割(最大{chunk_size}経路/パケット) ===")
        for i in range(repeat):
            for c_idx, chunk in enumerate(chunks):
                try:
                    packet = rip_build_packet(chunk, version, command)
                    rip_send_packet(dest, packet, bind_ip, ttl)
                    names = ", ".join(f"{r['network']}/{r['prefix']}" for r in chunk)
                    self.log(f"[OK] {i + 1}/{repeat}回目 パケット{c_idx + 1}/{len(chunks)} "
                              f"({len(chunk)}経路: {names})")
                except (ValueError, OSError) as e:
                    self.log(f"[ERROR] パケット{c_idx + 1}/{len(chunks)} 送信失敗: {e}")
            if i < repeat - 1:
                time.sleep(interval)
        self.log("=== 送信終了 ===\n")
        self.sending = False
        self.after(0, lambda: self.send_btn.configure(text="送信開始", state="normal"))


# =====================================================================
# GUI: OSPF(P2P)タブ
# =====================================================================

class OspfP2PTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.faker = None
        self.routes = []
        self.injected = set()
        if not SCAPY_AVAILABLE:
            scapy_unavailable_notice(self).pack(fill="both", expand=True)
            return
        self._build_widgets()
        self._poll_inject()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        cfg = ttk.LabelFrame(self, text="接続設定")
        cfg.pack(fill="x", padx=8, pady=6)

        ttk.Label(cfg, text="NIC名(iface):").grid(row=0, column=0, sticky="e", **pad)
        self.iface_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.iface_var, width=20).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="自分のIP:").grid(row=0, column=2, sticky="e", **pad)
        self.my_ip_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.my_ip_var, width=16).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="Router ID:").grid(row=1, column=0, sticky="e", **pad)
        self.router_id_var = tk.StringVar(value="9.9.9.9")
        ttk.Entry(cfg, textvariable=self.router_id_var, width=16).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="Area:").grid(row=1, column=2, sticky="e", **pad)
        self.area_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(cfg, textvariable=self.area_var, width=16).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="Mask:").grid(row=2, column=0, sticky="e", **pad)
        self.mask_var = tk.StringVar(value="255.255.255.252")
        ttk.Entry(cfg, textvariable=self.mask_var, width=16).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="Hello/Dead(秒):").grid(row=2, column=2, sticky="e", **pad)
        self.hello_var = tk.IntVar(value=10)
        ttk.Spinbox(cfg, from_=1, to=120, textvariable=self.hello_var, width=5).grid(row=2, column=3, sticky="w")
        self.dead_var = tk.IntVar(value=40)
        ttk.Spinbox(cfg, from_=4, to=480, textvariable=self.dead_var, width=5).grid(row=2, column=3, sticky="e")

        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="デバッグ(送受信サマリ表示)", variable=self.debug_var).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)

        route_frame = ttk.LabelFrame(self, text="Full到達後に注入する経路(AS External LSA)")
        route_frame.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(route_frame, text="Network:").grid(row=0, column=0, **pad)
        self.net_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.net_var, width=16).grid(row=0, column=1, **pad)
        ttk.Label(route_frame, text="Prefix:").grid(row=0, column=2, **pad)
        self.prefix_var = tk.StringVar(value="24")
        ttk.Entry(route_frame, textvariable=self.prefix_var, width=6).grid(row=0, column=3, **pad)
        ttk.Label(route_frame, text="Metric:").grid(row=0, column=4, **pad)
        self.metric_var = tk.StringVar(value="20")
        ttk.Entry(route_frame, textvariable=self.metric_var, width=6).grid(row=0, column=5, **pad)
        ttk.Label(route_frame, text="Tag:").grid(row=0, column=6, **pad)
        self.tag_var = tk.StringVar(value="0")
        ttk.Entry(route_frame, textvariable=self.tag_var, width=6).grid(row=0, column=7, **pad)
        ttk.Button(route_frame, text="追加", command=self._add_route).grid(row=0, column=8, **pad)
        ttk.Button(route_frame, text="削除", command=self._delete_route).grid(row=0, column=9, **pad)
        ttk.Button(route_frame, text="CSVから読み込み", command=self._load_csv).grid(row=0, column=10, **pad)

        self.tree = ttk.Treeview(route_frame, columns=("net", "metric", "tag"), show="headings", height=5)
        for c, label, w in [("net", "Network/Prefix", 200), ("metric", "Metric", 80), ("tag", "Tag", 80)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        self.tree.grid(row=1, column=0, columnspan=11, sticky="nsew", padx=6, pady=6)
        route_frame.grid_rowconfigure(1, weight=1)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(action_frame, text="開始", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(action_frame, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(action_frame, text="状態: -")
        self.status_label.pack(side="left", padx=12)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

    def _add_route_values(self, network, prefix, metric, tag):
        network = network.strip()
        prefix = int(prefix)
        metric = int(metric)
        tag = int(tag or 0)
        socket.inet_aton(network)
        mask = prefix_to_netmask(prefix)
        self.routes.append((network, mask, metric, tag))
        self.tree.insert("", "end", values=(f"{network}/{prefix}", metric, tag))

    def _add_route(self):
        try:
            self._add_route_values(self.net_var.get(), self.prefix_var.get(),
                                    self.metric_var.get(), self.tag_var.get())
        except (ValueError, OSError) as e:
            messagebox.showerror("入力エラー", str(e))

    def _load_csv(self):
        """CSVから経路を一括読み込み。ヘッダー行: network,prefix,metric,tag(tagは省略可)"""
        path = filedialog.askopenfilename(
            title="経路CSVを選択", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        added, errors = 0, []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                required = {"network", "prefix", "metric"}
                if not required.issubset(set(h.strip().lower() for h in (reader.fieldnames or []))):
                    messagebox.showerror("CSV形式エラー",
                                          "ヘッダー行に network,prefix,metric は最低限必要です(tagは省略可)")
                    return
                for row_num, row in enumerate(reader, start=2):
                    row = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
                    try:
                        self._add_route_values(row.get("network", ""), row.get("prefix", ""),
                                                row.get("metric", ""), row.get("tag", ""))
                        added += 1
                    except (ValueError, OSError) as e:
                        errors.append(f"{row_num}行目: {e}")
        except OSError as e:
            messagebox.showerror("読み込みエラー", str(e))
            return
        self.log(f"[CSV] {path} から{added}件読み込みました")
        if errors:
            self.log(f"[CSV] {len(errors)}件のエラー:")
            for e in errors:
                self.log(f"  {e}")
            messagebox.showwarning("一部読み込みエラー", f"{added}件成功、{len(errors)}件エラー(ログ参照)")
        else:
            messagebox.showinfo("読み込み完了", f"{added}件の経路を読み込みました")

    def _delete_route(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = sorted((self.tree.index(i) for i in selected), reverse=True)
        for item in selected:
            self.tree.delete(item)
        for idx in indices:
            del self.routes[idx]

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_start(self):
        try:
            self.faker = OSPFNeighborFaker(
                iface=self.iface_var.get().strip(),
                my_ip=self.my_ip_var.get().strip(),
                router_id=self.router_id_var.get().strip(),
                area=self.area_var.get().strip() or "0.0.0.0",
                mask=self.mask_var.get().strip() or "255.255.255.252",
                hello_interval=self.hello_var.get(),
                dead_interval=self.dead_var.get(),
                on_log=self.log,
                debug=self.debug_var.get(),
            )
        except Exception as e:
            messagebox.showerror("開始エラー", str(e))
            return
        self.injected.clear()
        self.faker.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log("=== OSPF(P2P) 開始 ===")

    def _on_stop(self):
        if self.faker:
            self.faker.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.log("=== 停止しました ===")

    def _poll_inject(self):
        if self.faker:
            self.status_label.configure(text=f"状態: {self.faker.state}")
            if self.faker.full_event.is_set():
                for net, mask, metric, tag in self.routes:
                    key = (net, mask)
                    if key not in self.injected:
                        self.faker.inject_external_route(net, mask, metric, tag)
                        self.injected.add(key)
        self.after(1000, self._poll_inject)


# =====================================================================
# GUI: OSPF(Broadcast・大量ネイバー)タブ
# =====================================================================

class OspfMassTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.mgr = None
        self.injected = set()
        if not SCAPY_AVAILABLE:
            scapy_unavailable_notice(self).pack(fill="both", expand=True)
            return
        self._build_widgets()
        self._poll_status()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        warn = ttk.Label(
            self, foreground="red", justify="left",
            text="【重要】検証用ラック・自分が管理するスイッチにのみ実行してください。\n"
                 "台数は少数から始め、スイッチのCPU/メモリを監視しながら段階的に増やしてください。"
        )
        warn.pack(fill="x", padx=8, pady=(6, 0))

        cfg = ttk.LabelFrame(self, text="接続設定")
        cfg.pack(fill="x", padx=8, pady=6)

        ttk.Label(cfg, text="NIC名(iface):").grid(row=0, column=0, sticky="e", **pad)
        self.iface_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.iface_var, width=20).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="Area:").grid(row=0, column=2, sticky="e", **pad)
        self.area_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(cfg, textvariable=self.area_var, width=16).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="対象セグメントMask:").grid(row=1, column=0, sticky="e", **pad)
        self.mask_var = tk.StringVar(value="255.255.255.0")
        ttk.Entry(cfg, textvariable=self.mask_var, width=16).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="台数(count):").grid(row=1, column=2, sticky="e", **pad)
        self.count_var = tk.IntVar(value=10)
        ttk.Spinbox(cfg, from_=1, to=5000, textvariable=self.count_var, width=8).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="開始IP:").grid(row=2, column=0, sticky="e", **pad)
        self.base_ip_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.base_ip_var, width=16).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="開始Router ID:").grid(row=2, column=2, sticky="e", **pad)
        self.base_rid_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.base_rid_var, width=16).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="Hello/Dead/Rxmt(秒):").grid(row=3, column=0, sticky="e", **pad)
        hdr_frame = ttk.Frame(cfg)
        hdr_frame.grid(row=3, column=1, columnspan=3, sticky="w")
        self.hello_var = tk.IntVar(value=10)
        self.dead_var = tk.IntVar(value=40)
        self.rxmt_var = tk.IntVar(value=5)
        self.stagger_var = tk.DoubleVar(value=0.2)
        ttk.Spinbox(hdr_frame, from_=1, to=120, textvariable=self.hello_var, width=5).pack(side="left", padx=2)
        ttk.Spinbox(hdr_frame, from_=4, to=480, textvariable=self.dead_var, width=5).pack(side="left", padx=2)
        ttk.Spinbox(hdr_frame, from_=1, to=60, textvariable=self.rxmt_var, width=5).pack(side="left", padx=2)
        ttk.Label(hdr_frame, text="stagger(秒):").pack(side="left", padx=(10, 2))
        ttk.Spinbox(hdr_frame, from_=0, to=10, increment=0.1, textvariable=self.stagger_var, width=5).pack(side="left")

        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="デバッグ(送受信サマリ表示)", variable=self.debug_var).grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)

        inject_frame = ttk.LabelFrame(self, text="Full到達ごとに注入する経路(任意)")
        inject_frame.pack(fill="x", padx=8, pady=6)
        self.inject_enable_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inject_frame, text="有効化", variable=self.inject_enable_var).grid(row=0, column=0, **pad)
        ttk.Label(inject_frame, text="開始アドレス:").grid(row=0, column=1, sticky="e", **pad)
        self.inject_base_var = tk.StringVar(value="172.20.0.0")
        ttk.Entry(inject_frame, textvariable=self.inject_base_var, width=16).grid(row=0, column=2, **pad)
        ttk.Label(inject_frame, text="Prefix:").grid(row=0, column=3, sticky="e", **pad)
        self.inject_prefix_var = tk.IntVar(value=32)
        ttk.Spinbox(inject_frame, from_=0, to=32, textvariable=self.inject_prefix_var, width=5).grid(row=0, column=4, **pad)
        ttk.Label(inject_frame, text="Metric:").grid(row=0, column=5, sticky="e", **pad)
        self.inject_metric_var = tk.IntVar(value=20)
        ttk.Spinbox(inject_frame, from_=1, to=16777215, textvariable=self.inject_metric_var, width=8).grid(row=0, column=6, **pad)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(action_frame, text="開始", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(action_frame, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(action_frame, text="Full: -/-")
        self.status_label.pack(side="left", padx=12)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_start(self):
        try:
            self.mgr = OSPFMassNeighborManager(
                iface=self.iface_var.get().strip(),
                area=self.area_var.get().strip() or "0.0.0.0",
                mask=self.mask_var.get().strip(),
                hello_interval=self.hello_var.get(), dead_interval=self.dead_var.get(),
                rxmt_interval=self.rxmt_var.get(), stagger=self.stagger_var.get(),
                on_log=self.log, debug=self.debug_var.get(),
            )
            base_ip_int = ip_to_int(self.base_ip_var.get().strip())
            base_rid_int = ip_to_int(self.base_rid_var.get().strip())
            count = self.count_var.get()
            for idx in range(count):
                self.mgr.add_neighbor(int_to_ip(base_ip_int + idx), int_to_ip(base_rid_int + idx))
        except Exception as e:
            messagebox.showerror("開始エラー", str(e))
            return

        self.injected.clear()
        self.mgr.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log(f"=== {count}台の偽装OSPFネイバーを開始します ===")

    def _on_stop(self):
        if self.mgr:
            self.mgr.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.log("=== 停止しました ===")

    def _poll_status(self):
        if self.mgr:
            counts = self.mgr.state_counts()
            total = len(self.mgr.neighbors)
            full = counts.get(FakeNeighbor.STATE_FULL, 0)
            self.status_label.configure(text=f"Full: {full}/{total}")

            if self.inject_enable_var.get():
                try:
                    inject_base_int = ip_to_int(self.inject_base_var.get().strip())
                    inject_mask = prefix_to_netmask(self.inject_prefix_var.get())
                    for idx, n in enumerate(self.mgr.neighbors):
                        if n.state == FakeNeighbor.STATE_FULL and idx not in self.injected:
                            network = int_to_ip(inject_base_int + idx)
                            n.inject_external_route(network, inject_mask, self.inject_metric_var.get())
                            self.injected.add(idx)
                except (ValueError, OSError):
                    pass
        self.after(1000, self._poll_status)


# =====================================================================
# GUI: BGPタブ
# =====================================================================

class BgpTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.speaker = None
        self.routes = []
        self.advertised = False
        self._build_widgets()
        self._poll_status()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        cfg = ttk.LabelFrame(self, text="接続設定")
        cfg.pack(fill="x", padx=8, pady=6)

        ttk.Label(cfg, text="Peer IP:").grid(row=0, column=0, sticky="e", **pad)
        self.peer_ip_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.peer_ip_var, width=16).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="自分のIP:").grid(row=0, column=2, sticky="e", **pad)
        self.local_ip_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.local_ip_var, width=16).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="Local AS:").grid(row=1, column=0, sticky="e", **pad)
        self.local_as_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.local_as_var, width=10).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="Remote AS:").grid(row=1, column=2, sticky="e", **pad)
        self.remote_as_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.remote_as_var, width=10).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(cfg, text="Router ID:").grid(row=2, column=0, sticky="e", **pad)
        self.router_id_var = tk.StringVar(value="9.9.9.9")
        ttk.Entry(cfg, textvariable=self.router_id_var, width=16).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="Hold Time:").grid(row=2, column=2, sticky="e", **pad)
        self.hold_var = tk.IntVar(value=180)
        ttk.Spinbox(cfg, from_=0, to=65535, textvariable=self.hold_var, width=8).grid(row=2, column=3, sticky="w", **pad)

        route_frame = ttk.LabelFrame(self, text="広告する経路")
        route_frame.pack(fill="both", expand=True, padx=8, pady=6)

        ttk.Label(route_frame, text="Network:").grid(row=0, column=0, **pad)
        self.net_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.net_var, width=16).grid(row=0, column=1, **pad)
        ttk.Label(route_frame, text="Prefix:").grid(row=0, column=2, **pad)
        self.prefix_var = tk.StringVar(value="24")
        ttk.Entry(route_frame, textvariable=self.prefix_var, width=6).grid(row=0, column=3, **pad)
        ttk.Button(route_frame, text="追加", command=self._add_route).grid(row=0, column=4, **pad)
        ttk.Button(route_frame, text="削除", command=self._delete_route).grid(row=0, column=5, **pad)
        ttk.Button(route_frame, text="CSVから読み込み", command=self._load_csv).grid(row=0, column=6, **pad)

        ttk.Label(route_frame, text="Next-Hop:").grid(row=1, column=0, sticky="e", **pad)
        self.next_hop_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.next_hop_var, width=16).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(route_frame, text="(空欄なら自分のIP)").grid(row=1, column=2, columnspan=2, sticky="w", **pad)

        ttk.Label(route_frame, text="AS-PATH(カンマ区切り):").grid(row=2, column=0, sticky="e", **pad)
        self.as_path_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.as_path_var, width=20).grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(route_frame, text="(空欄: eBGPはLocal ASのみ、iBGPは空)").grid(row=2, column=3, columnspan=3, sticky="w", **pad)

        ttk.Label(route_frame, text="MED:").grid(row=3, column=0, sticky="e", **pad)
        self.med_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.med_var, width=10).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(route_frame, text="Local-Pref(iBGP時):").grid(row=3, column=2, sticky="e", **pad)
        self.local_pref_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.local_pref_var, width=10).grid(row=3, column=3, sticky="w", **pad)

        ttk.Label(route_frame, text="Community:").grid(row=4, column=0, sticky="e", **pad)
        self.community_var = tk.StringVar()
        ttk.Entry(route_frame, textvariable=self.community_var, width=30).grid(
            row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(route_frame, text="(カンマ区切り。例: 65001:100,no-export)").grid(
            row=4, column=3, columnspan=3, sticky="w", **pad)

        self.tree = ttk.Treeview(route_frame, columns=("net",), show="headings", height=5)
        self.tree.heading("net", text="Network/Prefix")
        self.tree.column("net", width=200, anchor="center")
        self.tree.grid(row=5, column=0, columnspan=6, sticky="nsew", padx=6, pady=6)
        route_frame.grid_rowconfigure(5, weight=1)

        flap_frame = ttk.LabelFrame(self, text="フラップテスト(ExaBGP風: announce/withdrawを繰り返す)")
        flap_frame.pack(fill="x", padx=8, pady=6)
        self.flap_enable_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flap_frame, text="有効化", variable=self.flap_enable_var).grid(row=0, column=0, **pad)
        ttk.Label(flap_frame, text="間隔(秒):").grid(row=0, column=1, sticky="e", **pad)
        self.flap_interval_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(flap_frame, from_=1, to=600, textvariable=self.flap_interval_var, width=6).grid(
            row=0, column=2, sticky="w", **pad)
        ttk.Label(flap_frame, text="回数(0=無限):").grid(row=0, column=3, sticky="e", **pad)
        self.flap_count_var = tk.IntVar(value=0)
        ttk.Spinbox(flap_frame, from_=0, to=100000, textvariable=self.flap_count_var, width=8).grid(
            row=0, column=4, sticky="w", **pad)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.connect_btn = ttk.Button(action_frame, text="接続してEstablish", command=self._on_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.advertise_btn = ttk.Button(action_frame, text="経路広告(Announce)", command=self._on_advertise, state="disabled")
        self.advertise_btn.pack(side="left", padx=4)
        self.withdraw_btn = ttk.Button(action_frame, text="経路撤回(Withdraw)", command=self._on_withdraw, state="disabled")
        self.withdraw_btn.pack(side="left", padx=4)
        self.disconnect_btn = ttk.Button(action_frame, text="切断", command=self._on_disconnect, state="disabled")
        self.disconnect_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(action_frame, text="状態: Idle")
        self.status_label.pack(side="left", padx=12)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

        self.flap_state = "announced"  # フラップテスト中の現在状態
        self.flap_done_count = 0
        self.flap_stop_event = threading.Event()

    def _add_route_values(self, network, prefix):
        network = network.strip()
        prefix = int(prefix)
        socket.inet_aton(network)
        self.routes.append((network, prefix))
        self.tree.insert("", "end", values=(f"{network}/{prefix}",))

    def _add_route(self):
        try:
            self._add_route_values(self.net_var.get(), self.prefix_var.get())
        except (ValueError, OSError) as e:
            messagebox.showerror("入力エラー", str(e))

    def _load_csv(self):
        """CSVから経路を一括読み込み。ヘッダー行: network,prefix"""
        path = filedialog.askopenfilename(
            title="経路CSVを選択", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        added, errors = 0, []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                required = {"network", "prefix"}
                if not required.issubset(set(h.strip().lower() for h in (reader.fieldnames or []))):
                    messagebox.showerror("CSV形式エラー", "ヘッダー行に network,prefix が必要です")
                    return
                for row_num, row in enumerate(reader, start=2):
                    row = {k.strip().lower(): (v.strip() if v else v) for k, v in row.items()}
                    try:
                        self._add_route_values(row.get("network", ""), row.get("prefix", ""))
                        added += 1
                    except (ValueError, OSError) as e:
                        errors.append(f"{row_num}行目: {e}")
        except OSError as e:
            messagebox.showerror("読み込みエラー", str(e))
            return
        self.log(f"[CSV] {path} から{added}件読み込みました")
        if errors:
            self.log(f"[CSV] {len(errors)}件のエラー:")
            for e in errors:
                self.log(f"  {e}")
            messagebox.showwarning("一部読み込みエラー", f"{added}件成功、{len(errors)}件エラー(ログ参照)")
        else:
            messagebox.showinfo("読み込み完了", f"{added}件の経路を読み込みました")

    def _delete_route(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = sorted((self.tree.index(i) for i in selected), reverse=True)
        for item in selected:
            self.tree.delete(item)
        for idx in indices:
            del self.routes[idx]

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_connect(self):
        try:
            local_as = int(self.local_as_var.get().strip())
            remote_as = int(self.remote_as_var.get().strip())
            self.speaker = BGPSpeaker(
                peer_ip=self.peer_ip_var.get().strip(),
                local_as=local_as, remote_as=remote_as,
                router_id=self.router_id_var.get().strip(),
                local_ip=self.local_ip_var.get().strip() or None,
                hold_time=self.hold_var.get(),
                on_log=self.log,
            )
        except Exception as e:
            messagebox.showerror("入力エラー", str(e))
            return
        self.advertised = False
        self.flap_state = "announced"
        self.flap_done_count = 0
        if hasattr(self, "_flap_next_due"):
            del self._flap_next_due
        self.connect_btn.configure(state="disabled")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            self.speaker.connect()
        except OSError as e:
            self.log(f"[ERROR] 接続失敗: {e}")
            self.after(0, lambda: self.connect_btn.configure(state="normal"))
            return
        if self.speaker.established_event.wait(timeout=30):
            self.after(0, self._on_established)
        else:
            self.log("[ERROR] 30秒以内にEstablishedへ到達しませんでした")
            self.after(0, lambda: self.connect_btn.configure(state="normal"))

    def _on_established(self):
        self.advertise_btn.configure(state="normal")
        self.withdraw_btn.configure(state="normal")
        self.disconnect_btn.configure(state="normal")

    def _parse_communities(self):
        raw = self.community_var.get().strip()
        if not raw:
            return None
        try:
            return [bgp_parse_community(tok) for tok in raw.split(",") if tok.strip()]
        except ValueError as e:
            messagebox.showerror("Community入力エラー", str(e))
            return None

    def _build_advertise_args(self):
        next_hop = self.next_hop_var.get().strip() or self.local_ip_var.get().strip()
        if not next_hop:
            messagebox.showwarning("Next-Hop未指定", "Next-Hopまたは自分のIPを指定してください")
            return None
        as_path_str = self.as_path_var.get().strip()
        if as_path_str:
            as_path = [int(x) for x in as_path_str.split(",") if x.strip() != ""]
        else:
            as_path = None if self.speaker.is_ibgp else [self.speaker.local_as]
        med = int(self.med_var.get()) if self.med_var.get().strip() else None
        local_pref = int(self.local_pref_var.get()) if self.local_pref_var.get().strip() else None
        communities = self._parse_communities()
        return next_hop, as_path, med, local_pref, communities

    def _on_advertise(self):
        if not self.speaker or self.speaker.state != BGPSpeaker.STATE_ESTABLISHED:
            messagebox.showwarning("未接続", "Establishedになってから広告してください")
            return
        if not self.routes:
            messagebox.showwarning("経路未入力", "広告する経路を1件以上追加してください")
            return
        args = self._build_advertise_args()
        if args is None:
            return
        next_hop, as_path, med, local_pref, communities = args
        threading.Thread(
            target=self.speaker.advertise,
            args=(list(self.routes), next_hop, as_path, med, local_pref, communities),
            daemon=True,
        ).start()

    def _on_withdraw(self):
        if not self.speaker or self.speaker.state != BGPSpeaker.STATE_ESTABLISHED:
            messagebox.showwarning("未接続", "Establishedになってから撤回してください")
            return
        if not self.routes:
            messagebox.showwarning("経路未入力", "撤回する経路がありません")
            return
        threading.Thread(target=self.speaker.withdraw, args=(list(self.routes),), daemon=True).start()

    def _on_disconnect(self):
        self.flap_enable_var.set(False)
        if self.speaker:
            threading.Thread(target=self.speaker.close, kwargs={"send_cease": True}, daemon=True).start()
        self.connect_btn.configure(state="normal")
        self.advertise_btn.configure(state="disabled")
        self.withdraw_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="disabled")

    def _poll_status(self):
        if self.speaker:
            self.status_label.configure(text=f"状態: {self.speaker.state}")
            self._flap_tick()
        self.after(1000, self._poll_status)

    def _flap_tick(self):
        """フラップテスト有効時、間隔ごとにannounce/withdrawを交互に実行する"""
        if not self.flap_enable_var.get():
            return
        if self.speaker.state != BGPSpeaker.STATE_ESTABLISHED or not self.routes:
            return
        now = time.time()
        if not hasattr(self, "_flap_next_due"):
            self._flap_next_due = now
        if now < self._flap_next_due:
            return
        self._flap_next_due = now + self.flap_interval_var.get()

        max_count = self.flap_count_var.get()
        if max_count and self.flap_done_count >= max_count:
            self.flap_enable_var.set(False)
            self.log(f"=== フラップテスト完了({self.flap_done_count}回) ===")
            return

        if self.flap_state == "announced":
            threading.Thread(target=self.speaker.withdraw, args=(list(self.routes),), daemon=True).start()
            self.flap_state = "withdrawn"
        else:
            args = self._build_advertise_args()
            if args is not None:
                next_hop, as_path, med, local_pref, communities = args
                threading.Thread(
                    target=self.speaker.advertise,
                    args=(list(self.routes), next_hop, as_path, med, local_pref, communities),
                    daemon=True,
                ).start()
            self.flap_state = "announced"
            self.flap_done_count += 1


# =====================================================================
# GUI: RIP疑似ルータ生成タブ(複数送信元からそれぞれ異なる経路を配信)
# =====================================================================

class RipMultiRouterTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.routers = []  # [{src_ip, network, prefix, netmask, metric, nexthop, tag}, ...]
        self.running = False
        self.stop_event = threading.Event()
        self._build_widgets()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        info = ttk.Label(
            self, foreground="blue", justify="left",
            text="同一セグメント上の複数の疑似ルータから、それぞれ別の経路を配信するための一括生成ツールです。\n"
                 "「bindモード」は送信元IPが実際にこのPCのNICに割り当てられている必要があります。\n"
                 "「scapy偽装モード」はIPを詐称して送信します(要Npcap・管理者権限)。"
        )
        info.pack(fill="x", padx=8, pady=(6, 0))

        gen_frame = ttk.LabelFrame(self, text="疑似ルータ一括生成")
        gen_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(gen_frame, text="台数:").grid(row=0, column=0, sticky="e", **pad)
        self.count_var = tk.IntVar(value=5)
        ttk.Spinbox(gen_frame, from_=1, to=254, textvariable=self.count_var, width=6).grid(
            row=0, column=1, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始送信元IP:").grid(row=0, column=2, sticky="e", **pad)
        self.base_src_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.base_src_var, width=16).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(gen_frame, text="(1台ごとに+1。例: .11,.12,.13...)").grid(
            row=0, column=4, columnspan=2, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始ネットワーク:").grid(row=1, column=0, sticky="e", **pad)
        self.base_net_var = tk.StringVar(value="192.168.1.0")
        ttk.Entry(gen_frame, textvariable=self.base_net_var, width=16).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(gen_frame, text="Prefix:").grid(row=1, column=2, sticky="e", **pad)
        self.prefix_var = tk.StringVar(value="24")
        ttk.Entry(gen_frame, textvariable=self.prefix_var, width=6).grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(gen_frame, text="(1台ごとに/24ブロック分+1。"
                                    "192.168.1.0→192.168.2.0→...)").grid(
            row=1, column=4, columnspan=2, sticky="w", **pad)

        ttk.Label(gen_frame, text="Metric(共通):").grid(row=2, column=0, sticky="e", **pad)
        self.metric_var = tk.StringVar(value="1")
        ttk.Entry(gen_frame, textvariable=self.metric_var, width=6).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(gen_frame, text="NextHop(共通):").grid(row=2, column=2, sticky="e", **pad)
        self.nexthop_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(gen_frame, textvariable=self.nexthop_var, width=16).grid(row=2, column=3, sticky="w", **pad)

        ttk.Button(gen_frame, text="1件追加", command=self._on_add_one).grid(
            row=2, column=4, sticky="e", **pad)
        ttk.Button(gen_frame, text="生成(全部作り直し)", command=self._on_generate).grid(
            row=2, column=5, sticky="e", **pad)

        list_frame = ttk.LabelFrame(self, text="生成された疑似ルータ一覧")
        list_frame.pack(fill="both", expand=True, padx=8, pady=6)
        columns = ("no", "src_ip", "network", "metric", "nexthop")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8)
        for c, label, w in [("no", "No.", 40), ("src_ip", "送信元IP", 130),
                            ("network", "Network/Prefix", 160), ("metric", "Metric", 70),
                            ("nexthop", "NextHop", 130)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btn_row, text="選択削除", command=self._delete_selected).pack(side="left", padx=4)
        ttk.Button(btn_row, text="全削除", command=self._clear_all).pack(side="left", padx=4)

        send_frame = ttk.LabelFrame(self, text="送信設定")
        send_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(send_frame, text="送信モード:").grid(row=0, column=0, sticky="e", **pad)
        self.mode_var = tk.StringVar(value="bind")
        ttk.Radiobutton(send_frame, text="bind(実IPがNIC設定済み)", variable=self.mode_var,
                        value="bind").grid(row=0, column=1, sticky="w", **pad)
        ttk.Radiobutton(send_frame, text="scapy偽装(要Npcap/管理者)", variable=self.mode_var,
                        value="scapy", state="normal" if SCAPY_AVAILABLE else "disabled").grid(
            row=0, column=2, sticky="w", **pad)

        ttk.Label(send_frame, text="NIC名(scapy時):").grid(row=1, column=0, sticky="e", **pad)
        self.iface_var = tk.StringVar()
        ttk.Entry(send_frame, textvariable=self.iface_var, width=20).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(send_frame, text="宛先IP:").grid(row=1, column=2, sticky="e", **pad)
        self.dest_var = tk.StringVar()
        ttk.Entry(send_frame, textvariable=self.dest_var, width=16).grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(send_frame, text="(空欄なら224.0.0.9)").grid(row=1, column=4, sticky="w", **pad)

        ttk.Label(send_frame, text="バージョン:").grid(row=2, column=0, sticky="e", **pad)
        self.version_var = tk.IntVar(value=2)
        ttk.Radiobutton(send_frame, text="RIPv1", variable=self.version_var, value=1).grid(row=2, column=1, sticky="w")
        ttk.Radiobutton(send_frame, text="RIPv2", variable=self.version_var, value=2).grid(row=2, column=1, sticky="e")

        self.continuous_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(send_frame, text="継続送信する(RIPの実運用と同様に定期送信し続ける)",
                        variable=self.continuous_var).grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(send_frame, text="送信間隔(秒):").grid(row=3, column=3, sticky="e", **pad)
        self.interval_var = tk.DoubleVar(value=30.0)
        ttk.Spinbox(send_frame, from_=1, to=600, textvariable=self.interval_var, width=6).grid(
            row=3, column=4, sticky="w", **pad)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(action_frame, text="全疑似ルータから送信開始", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(action_frame, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

    _parse_ip_field = staticmethod(parse_ip_field)

    def _parse_common_fields(self):
        """疑似ルータ一括生成欄の共通パラメータを検証して返す。
        戻り値: (base_src_int, base_net_int, prefix, metric, nexthop)"""
        base_src_int = self._parse_ip_field("開始送信元IP", self.base_src_var.get())
        base_net_int = self._parse_ip_field("開始ネットワーク", self.base_net_var.get())
        try:
            prefix = int(self.prefix_var.get().strip())
        except ValueError:
            raise ValueError(f"「Prefix」は数字で入力してください（例: 24）: "
                             f"「{self.prefix_var.get()}」")
        if not (0 <= prefix <= 32):
            raise ValueError("「Prefix」は0〜32で指定してください")
        try:
            metric = int(self.metric_var.get().strip())
        except ValueError:
            raise ValueError(f"「Metric(共通)」は数字で入力してください（例: 1）: "
                             f"「{self.metric_var.get()}」")
        if not (1 <= metric <= 16):
            raise ValueError("Metricは1〜16で指定してください")
        nexthop_raw = self.nexthop_var.get().strip()
        nexthop = "0.0.0.0" if not nexthop_raw else int_to_ip(
            self._parse_ip_field("NextHop(共通)", nexthop_raw))
        return base_src_int, base_net_int, prefix, metric, nexthop

    def _append_router_row(self, src_ip, network, prefix, netmask, metric, nexthop):
        idx = len(self.routers)
        self.routers.append({
            "src_ip": src_ip, "network": network, "prefix": prefix,
            "netmask": netmask, "metric": metric, "nexthop": nexthop, "tag": 0,
        })
        self.tree.insert("", "end", values=(idx + 1, src_ip, f"{network}/{prefix}", metric, nexthop))

    # ---- 生成 / 一覧操作 ----
    def _on_add_one(self):
        """現在の入力値のまま(送信元IP・ネットワークとも増分せず)1件だけ
        既存リストに追加する。1台のPCから複数の経路を少しずつ足していく
        使い方（"生成"はリストを全部作り直してしまうため、それとは別に用意）。"""
        try:
            base_src_int, base_net_int, prefix, metric, nexthop = self._parse_common_fields()
            netmask = prefix_to_netmask(prefix)
            self._append_router_row(int_to_ip(base_src_int), int_to_ip(base_net_int),
                                    prefix, netmask, metric, nexthop)
            self.log(f"[OK] 経路を1件追加しました: {int_to_ip(base_net_int)}/{prefix}")
        except (ValueError, OSError) as e:
            messagebox.showerror("追加エラー", str(e))

    def _on_generate(self):
        try:
            count = self.count_var.get()
            base_src_int, base_net_int, prefix, metric, nexthop = self._parse_common_fields()
            netmask = prefix_to_netmask(prefix)

            self.tree.delete(*self.tree.get_children())
            self.routers.clear()
            for idx in range(count):
                src_ip = int_to_ip(base_src_int + idx)
                network = int_to_ip(base_net_int + idx * 256)  # /24ブロック単位でインクリメント
                self.routers.append({
                    "src_ip": src_ip, "network": network, "prefix": prefix,
                    "netmask": netmask, "metric": metric, "nexthop": nexthop, "tag": 0,
                })
                self.tree.insert("", "end", values=(idx + 1, src_ip, f"{network}/{prefix}", metric, nexthop))
            self.log(f"[OK] 疑似ルータを{count}台生成しました")
        except (ValueError, OSError) as e:
            messagebox.showerror("生成エラー", str(e))

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = sorted((self.tree.index(i) for i in selected), reverse=True)
        for item in selected:
            self.tree.delete(item)
        for idx in indices:
            del self.routers[idx]

    def _clear_all(self):
        self.tree.delete(*self.tree.get_children())
        self.routers.clear()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---- 送信 ----
    def _on_start(self):
        if self.running:
            messagebox.showinfo("送信中", "既に送信中です")
            return
        if not self.routers:
            messagebox.showwarning("未生成", "先に疑似ルータを生成してください")
            return
        mode = self.mode_var.get()
        if mode == "scapy" and not SCAPY_AVAILABLE:
            messagebox.showerror("エラー", "scapyがインストールされていません")
            return
        if mode == "scapy" and not self.iface_var.get().strip():
            messagebox.showwarning("NIC未指定", "scapy偽装モードではNIC名を指定してください")
            return

        version = self.version_var.get()
        dest = self.dest_var.get().strip() or (RIPV2_MULTICAST if version == 2 else RIPV1_BROADCAST)
        continuous = self.continuous_var.get()
        interval = self.interval_var.get()
        iface = self.iface_var.get().strip()

        self.stop_event.clear()
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(
            target=self._send_worker,
            args=(mode, iface, dest, version, continuous, interval),
            daemon=True,
        ).start()

    def _on_stop(self):
        self.stop_event.set()

    def _send_worker(self, mode, iface, dest, version, continuous, interval):
        self.log(f"=== {len(self.routers)}台の疑似ルータから送信開始 "
                  f"(mode={mode}, dest={dest}, RIPv{version}) ===")
        try:
            while True:
                # 継続送信中でも「1件追加」等でリストを更新できるよう、
                # 周期ごとに最新のself.routersを読み直す（開始時点の
                # スナップショット固定だと、送信中に追加した経路が
                # 停止→再開しないと配信されないという分かりにくい挙動になっていた）
                for r in list(self.routers):
                    if self.stop_event.is_set():
                        break
                    try:
                        packet = rip_build_packet([r], version, command=2)
                        if mode == "scapy":
                            rip_send_packet_spoofed(iface, r["src_ip"], dest, packet)
                        else:
                            rip_send_packet(dest, packet, bind_ip=r["src_ip"], ttl=1)
                        self.log(f"[OK] {r['src_ip']} -> {r['network']}/{r['prefix']} "
                                  f"metric={r['metric']} 送信")
                    except OSError as e:
                        self.log(f"[ERROR] {r['src_ip']} からの送信に失敗: {e}"
                                  + ("(bindモードではこのIPがNICに設定されている必要があります)"
                                     if mode == "bind" else ""))
                if not continuous or self.stop_event.is_set():
                    break
                if self.stop_event.wait(interval):
                    break
        finally:
            self.log("=== 送信終了 ===\n")
            self.running = False
            self.after(0, lambda: (self.start_btn.configure(state="normal"),
                                    self.stop_btn.configure(state="disabled")))


# =====================================================================
# GUI: BGP複数ピア生成タブ(複数の疑似ルータから同時にBGPセッションを張る)
# =====================================================================

class BgpMultiPeerTab(ttk.Frame, LogMixin):
    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.mgr = None
        self.peers = []  # 生成されたピア定義(GUI表示用)
        self.running = False
        self._build_widgets()
        self._poll_status()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        info = ttk.Label(
            self, foreground="blue", justify="left",
            text="複数の疑似ルータから、対向ルータへ同時にBGPセッションを張って"
                 "それぞれ別の経路を広告するツールです。\n"
                 "各送信元IPは実際にこのPCのNICに割り当てられている必要があります"
                 "(RIPタブのbindモードと同じ前提)。\n"
                 "対向ルータ側にも、各送信元IPごとに neighbor <IP> remote-as <AS> の"
                 "事前設定が必要です。"
        )
        info.pack(fill="x", padx=8, pady=(6, 0))

        gen_frame = ttk.LabelFrame(self, text="疑似ルータ一括生成")
        gen_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(gen_frame, text="台数:").grid(row=0, column=0, sticky="e", **pad)
        self.count_var = tk.IntVar(value=5)
        ttk.Spinbox(gen_frame, from_=1, to=200, textvariable=self.count_var, width=6).grid(
            row=0, column=1, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始送信元IP:").grid(row=0, column=2, sticky="e", **pad)
        self.base_local_ip_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.base_local_ip_var, width=16).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(gen_frame, text="(1台ごとに+1)").grid(row=0, column=4, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始Router ID:").grid(row=1, column=0, sticky="e", **pad)
        self.base_router_id_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.base_router_id_var, width=16).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(gen_frame, text="(1台ごとに+1)").grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(gen_frame, text="Peer IP(共通・対向ルータ):").grid(row=2, column=0, sticky="e", **pad)
        self.peer_ip_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.peer_ip_var, width=16).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(gen_frame, text="Remote AS(共通):").grid(row=2, column=2, sticky="e", **pad)
        self.remote_as_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.remote_as_var, width=10).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始Local AS:").grid(row=3, column=0, sticky="e", **pad)
        self.base_local_as_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.base_local_as_var, width=10).grid(row=3, column=1, sticky="w", **pad)
        self.increment_as_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(gen_frame, text="AS番号も1台ごとに+1する(未チェックなら全台同一AS)",
                        variable=self.increment_as_var).grid(row=3, column=2, columnspan=3, sticky="w", **pad)

        ttk.Label(gen_frame, text="開始ネットワーク:").grid(row=4, column=0, sticky="e", **pad)
        self.base_net_var = tk.StringVar(value="192.168.1.0")
        ttk.Entry(gen_frame, textvariable=self.base_net_var, width=16).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(gen_frame, text="Prefix:").grid(row=4, column=2, sticky="e", **pad)
        self.prefix_var = tk.StringVar(value="24")
        ttk.Entry(gen_frame, textvariable=self.prefix_var, width=6).grid(row=4, column=3, sticky="w", **pad)
        ttk.Label(gen_frame, text="(1台ごとに/24ブロック分+1)").grid(row=4, column=4, sticky="w", **pad)

        ttk.Label(gen_frame, text="Hold Time(共通):").grid(row=5, column=0, sticky="e", **pad)
        self.hold_var = tk.IntVar(value=180)
        ttk.Spinbox(gen_frame, from_=0, to=65535, textvariable=self.hold_var, width=8).grid(
            row=5, column=1, sticky="w", **pad)

        ttk.Label(gen_frame, text="Community(共通):").grid(row=6, column=0, sticky="e", **pad)
        self.community_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self.community_var, width=30).grid(
            row=6, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(gen_frame, text="(カンマ区切り。例: 65001:100,no-export)").grid(
            row=6, column=3, columnspan=2, sticky="w", **pad)
        ttk.Button(gen_frame, text="生成", command=self._on_generate).grid(row=6, column=5, sticky="e", **pad)

        list_frame = ttk.LabelFrame(self, text="生成された疑似ルータ一覧")
        list_frame.pack(fill="both", expand=True, padx=8, pady=6)
        columns = ("no", "local_ip", "local_as", "router_id", "network")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=7)
        for c, label, w in [("no", "No.", 40), ("local_ip", "送信元IP", 130),
                            ("local_as", "Local AS", 80), ("router_id", "Router ID", 130),
                            ("network", "広告経路", 160)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btn_row, text="選択削除", command=self._delete_selected).pack(side="left", padx=4)
        ttk.Button(btn_row, text="全削除", command=self._clear_all).pack(side="left", padx=4)

        flap_frame = ttk.LabelFrame(self, text="フラップテスト(全ピア一斉にannounce/withdrawを繰り返す)")
        flap_frame.pack(fill="x", padx=8, pady=6)
        self.flap_enable_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flap_frame, text="有効化", variable=self.flap_enable_var).grid(row=0, column=0, **pad)
        ttk.Label(flap_frame, text="間隔(秒):").grid(row=0, column=1, sticky="e", **pad)
        self.flap_interval_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(flap_frame, from_=1, to=600, textvariable=self.flap_interval_var, width=6).grid(
            row=0, column=2, sticky="w", **pad)

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(action_frame, text="全ピア接続開始(自動広告)", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.withdraw_btn = ttk.Button(action_frame, text="全ピア経路撤回", command=self._on_withdraw_all,
                                        state="disabled")
        self.withdraw_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(action_frame, text="全ピア切断", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(action_frame, text="Established: -/-")
        self.status_label.pack(side="left", padx=12)
        ttk.Button(action_frame, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)
        self._flap_next_due = 0.0

    # ---- 生成 / 一覧操作 ----
    def _on_generate(self):
        try:
            count = self.count_var.get()
            base_local_ip_int = ip_to_int(self.base_local_ip_var.get().strip())
            base_rid_int = ip_to_int(self.base_router_id_var.get().strip())
            peer_ip = self.peer_ip_var.get().strip()
            socket.inet_aton(peer_ip)
            remote_as = int(self.remote_as_var.get().strip())
            base_local_as = int(self.base_local_as_var.get().strip())
            increment_as = self.increment_as_var.get()
            base_net_int = ip_to_int(self.base_net_var.get().strip())
            prefix = int(self.prefix_var.get().strip())
            hold_time = self.hold_var.get()
            community_raw = self.community_var.get().strip()
            communities = ([bgp_parse_community(t) for t in community_raw.split(",") if t.strip()]
                           if community_raw else None)

            self.tree.delete(*self.tree.get_children())
            self.peers.clear()
            for idx in range(count):
                local_ip = int_to_ip(base_local_ip_int + idx)
                router_id = int_to_ip(base_rid_int + idx)
                local_as = base_local_as + idx if increment_as else base_local_as
                network = int_to_ip(base_net_int + idx * 256)
                peer = {
                    "local_ip": local_ip, "router_id": router_id, "local_as": local_as,
                    "remote_as": remote_as, "peer_ip": peer_ip, "hold_time": hold_time,
                    "network": network, "prefix": prefix, "communities": communities,
                }
                self.peers.append(peer)
                self.tree.insert("", "end", values=(idx + 1, local_ip, local_as, router_id,
                                                       f"{network}/{prefix}"))
            self.log(f"[OK] 疑似ルータを{count}台生成しました")
        except (ValueError, OSError) as e:
            messagebox.showerror("生成エラー", str(e))

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        indices = sorted((self.tree.index(i) for i in selected), reverse=True)
        for item in selected:
            self.tree.delete(item)
        for idx in indices:
            del self.peers[idx]

    def _clear_all(self):
        self.tree.delete(*self.tree.get_children())
        self.peers.clear()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---- 接続 / 広告 ----
    def _on_start(self):
        if self.running:
            messagebox.showinfo("実行中", "既に実行中です")
            return
        if not self.peers:
            messagebox.showwarning("未生成", "先に疑似ルータを生成してください")
            return

        self.mgr = BgpMultiPeerManager(on_log=self.log)
        for p in self.peers:
            is_ibgp = (p["local_as"] == p["remote_as"])
            as_path = None if is_ibgp else [p["local_as"]]
            self.mgr.add_peer(
                peer_ip=p["peer_ip"], local_ip=p["local_ip"], local_as=p["local_as"],
                remote_as=p["remote_as"], router_id=p["router_id"], hold_time=p["hold_time"],
                routes=[(p["network"], p["prefix"])], next_hop=p["local_ip"], as_path=as_path,
                communities=p.get("communities"),
            )

        self.mgr.start_all()
        self.running = True
        self._flap_next_due = 0.0
        self.start_btn.configure(state="disabled")
        self.withdraw_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.log(f"=== {len(self.peers)}台の疑似BGPルータで接続開始します ===")

    def _on_withdraw_all(self):
        if self.mgr:
            self.mgr.withdraw_all()
            self.log("=== 全Established済みピアへ経路撤回を送信しました ===")

    def _on_stop(self):
        self.flap_enable_var.set(False)
        if self.mgr:
            self.mgr.stop_all()
        self.running = False
        self.start_btn.configure(state="normal")
        self.withdraw_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self.log("=== 全ピアへ切断要求を送信しました ===")

    def _poll_status(self):
        if self.mgr:
            counts = self.mgr.state_counts()
            total = len(self.mgr.entries)
            established = counts.get(BGPSpeaker.STATE_ESTABLISHED, 0)
            self.status_label.configure(text=f"Established: {established}/{total}")
            self.mgr.poll_and_advertise()

            if self.flap_enable_var.get():
                now = time.time()
                if now >= self._flap_next_due:
                    self._flap_next_due = now + self.flap_interval_var.get()
                    self.mgr.flap_tick()
        self.after(1000, self._poll_status)


# =====================================================================
# メインウィンドウ
# =====================================================================

# =====================================================================
# トラフィック生成 / スループット測定
#   L4(TCP/UDP)のトラフィックを流して pps/bps を実測するための機能。
#   経路注入(制御プレーン)だけでは「経路は入ったが実際に転送されるか」が
#   分からないため、データプレーン側の確認手段として用意している。
#   GUIから切り離してテストできるよう、tkinterに依存しない形にしてある。
# =====================================================================

class TrafficStats:
    """送信/受信のカウンタ。ワーカースレッドが更新しGUIスレッドが読むため
    ロックで保護する。

    計測区間は「最初のパケット〜最後のパケット」であって、開始ボタンを
    押した時刻からではない。受信側は送信側より先に起動しておくのが普通で、
    待ち受け開始から数えると待っていた時間の分だけ平均レートが薄まって
    しまうため（実測で 1,000pps のトラフィックが 606pps と表示された）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.packets = 0
        self.bytes = 0
        self.errors = 0
        self.last_error = ""
        self.first_at = None
        self.last_at = None

    def start(self):
        """カウンタをリセットする（計測時刻は最初のパケットで始まる）"""
        with self._lock:
            self.packets = 0
            self.bytes = 0
            self.errors = 0
            self.last_error = ""
            self.first_at = None
            self.last_at = None

    def add(self, nbytes: int):
        with self._lock:
            now = time.time()
            if self.first_at is None:
                self.first_at = now
            self.last_at = now
            self.packets += 1
            self.bytes += nbytes

    def add_error(self, msg: str):
        with self._lock:
            self.errors += 1
            self.last_error = msg

    def snapshot(self):
        """(packets, bytes, errors, elapsed秒, pps, bps) を返す"""
        with self._lock:
            first, last = self.first_at, self.last_at
            packets, nbytes, errors = self.packets, self.bytes, self.errors
        elapsed = (last - first) if (first and last) else 0.0
        pps = packets / elapsed if elapsed > 0 else 0.0
        bps = nbytes * 8 / elapsed if elapsed > 0 else 0.0
        return packets, nbytes, errors, elapsed, pps, bps


def run_traffic_sender(proto, dest_ip, dest_port, size, count, rate_pps,
                        src_ip, src_port, tos, stop_event, stats):
    """UDP/TCPでパケットを送信する。
    count<=0なら停止されるまで無制限、rate_pps<=0ならレート制限なし。
    tos(DSCP<<2 等のToSバイト値)を指定するとIP_TOSを設定する
    （Si-R等のQoS/優先制御の動作確認用。Hachiには無い機能）。"""
    payload = b'H' * max(1, size)
    sock_type = socket.SOCK_DGRAM if proto == 'udp' else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, sock_type)
    try:
        if tos:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
            except OSError as e:
                # 環境によってはIP_TOSを設定できない(Windowsの一部構成)。
                # 送信自体は続行し、設定できなかったことだけ記録する。
                stats.add_error(f"ToS設定不可: {e}")
        if src_ip or src_port:
            sock.bind((src_ip or '', src_port or 0))
        if proto == 'tcp':
            sock.settimeout(5.0)
            sock.connect((dest_ip, dest_port))
            sock.settimeout(None)
        stats.start()
        started = time.perf_counter()
        sent = 0
        while not stop_event.is_set():
            if count > 0 and sent >= count:
                break
            if rate_pps > 0:
                # 「開始からの経過時間から見て、本来ここまでに何パケット
                # 送信済みであるべきか」で判定する。
                # 1パケットごとにsleepで間隔を刻む実装だと、OSのタイマー
                # 粒度(数ms)のせいで毎回寝過ごし、指定レートに全く届かない
                # (1000pps指定で実測667pps = 33%低い、という測定結果だった)。
                # 経過時間基準にすると、寝過ごした分は次の周回でまとめて
                # 送られるので、平均レートが指定値に収束する。
                due = (time.perf_counter() - started) * rate_pps
                if sent >= due:
                    sleep_for = (sent + 1) / rate_pps - (time.perf_counter() - started)
                    if sleep_for > 0 and stop_event.wait(min(sleep_for, 0.05)):
                        break
                    continue
            try:
                if proto == 'udp':
                    sock.sendto(payload, (dest_ip, dest_port))
                else:
                    sock.sendall(payload)
            except OSError as e:
                stats.add_error(str(e))
                break
            stats.add(len(payload))
            sent += 1
    finally:
        sock.close()


def _enlarge_recv_buffer(sock, stats, want=4 * 1024 * 1024):
    """受信ソケットバッファを広げる。
    既定サイズのままだと、高レート時に受信側で取りこぼしてしまい
    「ネットワークで落ちた」ように見えてしまう（実測で3.5%の損失が出た。
    これを装置側の損失と誤読すると原因調査が完全に迷走する）。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, want)
    except OSError as e:
        stats.add_error(f"受信バッファ拡大に失敗: {e}")


def run_traffic_receiver(proto, listen_ip, listen_port, stop_event, stats):
    """UDP/TCPで待ち受け、受信パケット数/バイト数をstatsに記録する。"""
    if proto == 'udp':
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _enlarge_recv_buffer(sock, stats)
        sock.bind((listen_ip or '', listen_port))
        sock.settimeout(0.5)
        stats.start()
        try:
            while not stop_event.is_set():
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as e:
                    stats.add_error(str(e))
                    break
                stats.add(len(data))
        finally:
            sock.close()
        return

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_ip or '', listen_port))
    srv.listen(5)
    srv.settimeout(0.5)
    stats.start()
    try:
        while not stop_event.is_set():
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError as e:
                stats.add_error(str(e))
                break
            conn.settimeout(0.5)
            try:
                while not stop_event.is_set():
                    try:
                        data = conn.recv(65535)
                    except socket.timeout:
                        continue
                    except OSError as e:
                        stats.add_error(str(e))
                        break
                    if not data:
                        break  # 相手が切断
                    stats.add(len(data))
            finally:
                conn.close()
    finally:
        srv.close()


# ── マルチプロセス送受信 ───────────────────────────────────
# 1プロセス(1スレッド)では sendto() のシステムコールが処理時間の約77%を
# 占めるため、CPUを何コア積んでいても約24万pps(1472Bで約2.8Gbps)で頭打ちに
# なる。スレッドを増やすとGILの奪い合いで逆に1/5まで落ちる(実測)。
# プロセスを分けると素直に伸び、4プロセスで約85万pps=10Gbpsに到達する。
#
# プロセスごとに別ポートを使う。SO_REUSEPORTで1ポートを共有する手もあるが
# Windowsには無いため、可搬性を優先して「プロセス数分の連番ポート」にする。

_MP_FLUSH_INTERVAL = 0.2


def _make_shared_cell():
    """ワーカー1つ分の共有カウンタを作る"""
    return {
        'lock': multiprocessing.Lock(),
        'packets': multiprocessing.Value('q', 0, lock=False),
        'bytes': multiprocessing.Value('q', 0, lock=False),
        'errors': multiprocessing.Value('q', 0, lock=False),
        'first': multiprocessing.Value('d', 0.0, lock=False),
        'last': multiprocessing.Value('d', 0.0, lock=False),
    }


class _SharedStopEvent:
    """multiprocessing.Value を threading.Event 互換に見せる薄いラッパ。
    run_traffic_sender/receiver は is_set()/wait() しか使わないので足りる。"""

    def __init__(self, flag):
        self._flag = flag

    def is_set(self):
        return bool(self._flag.value)

    def wait(self, timeout=None):
        if self._flag.value:
            return True
        if timeout is None:
            timeout = 0.05
        if timeout <= 0.01:
            # レート制御の細かいsleepはそのまま寝る（刻むと精度が落ちる）
            time.sleep(timeout)
            return bool(self._flag.value)
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if self._flag.value:
                return True
            time.sleep(0.005)
        return bool(self._flag.value)


class _SharedStats:
    """ワーカー側の記帳。1パケットごとにプロセス間ロックを取ると
    その分だけ送信性能が落ちるので、ローカルに貯めて一定間隔でだけ
    共有メモリへ書き戻す。"""

    def __init__(self, cell):
        self._cell = cell
        self._packets = 0
        self._bytes = 0
        self._first = 0.0
        self._next_flush = 0.0

    def start(self):
        self._packets = 0
        self._bytes = 0
        self._first = 0.0
        self._next_flush = time.perf_counter() + _MP_FLUSH_INTERVAL

    def add(self, nbytes: int):
        if not self._packets:
            self._first = time.time()
        self._packets += 1
        self._bytes += nbytes
        now = time.perf_counter()
        if now >= self._next_flush:
            self.flush()
            self._next_flush = now + _MP_FLUSH_INTERVAL

    def add_error(self, msg: str):
        c = self._cell
        with c['lock']:
            c['errors'].value += 1

    def flush(self):
        c = self._cell
        with c['lock']:
            c['packets'].value = self._packets
            c['bytes'].value = self._bytes
            c['first'].value = self._first
            c['last'].value = time.time() if self._packets else 0.0


def _mp_sender_worker(cell, flag, proto, dest_ip, dest_port, size, count,
                       rate_pps, src_ip, tos):
    """マルチプロセス送信のワーカー（Windowsのspawnでも動くようモジュール直下）"""
    stats = _SharedStats(cell)
    try:
        run_traffic_sender(proto, dest_ip, dest_port, size, count, rate_pps,
                            src_ip, 0, tos, _SharedStopEvent(flag), stats)
    finally:
        stats.flush()


def _mp_receiver_worker(cell, flag, proto, listen_ip, listen_port):
    """マルチプロセス受信のワーカー"""
    stats = _SharedStats(cell)
    try:
        run_traffic_receiver(proto, listen_ip, listen_port,
                              _SharedStopEvent(flag), stats)
    finally:
        stats.flush()


class MultiProcessTraffic:
    """送信または受信を複数プロセスで動かし、統計を合算して返す。

    プロセスごとにポートを1つずつ使う(base_port, base_port+1, ...)。
    送信側と受信側で同じプロセス数・同じベースポートを指定すれば
    1対1で対応する。
    """

    def __init__(self):
        self.cells = []
        self.procs = []
        self.flag = None

    def start_senders(self, nproc, proto, dest_ip, base_port, size, count,
                       rate_pps, src_ip, tos):
        """nproc個の送信プロセスを起動する。
        レートと送信数はプロセス間で等分する（合計が指定値になるように）。"""
        self.flag = multiprocessing.Value('b', 0, lock=False)
        self.cells = [_make_shared_cell() for _ in range(nproc)]
        self.procs = []
        for i in range(nproc):
            # 端数は先頭のプロセスに寄せて、合計を指定値ちょうどにする
            share_rate = (rate_pps // nproc + (1 if i < rate_pps % nproc else 0)
                          if rate_pps > 0 else 0)
            share_count = (count // nproc + (1 if i < count % nproc else 0)
                           if count > 0 else 0)
            p = multiprocessing.Process(
                target=_mp_sender_worker,
                args=(self.cells[i], self.flag, proto, dest_ip, base_port + i,
                      size, share_count, share_rate, src_ip, tos),
                daemon=True)
            p.start()
            self.procs.append(p)
        return len(self.procs)

    def start_receivers(self, nproc, proto, listen_ip, base_port):
        self.flag = multiprocessing.Value('b', 0, lock=False)
        self.cells = [_make_shared_cell() for _ in range(nproc)]
        self.procs = []
        for i in range(nproc):
            p = multiprocessing.Process(
                target=_mp_receiver_worker,
                args=(self.cells[i], self.flag, proto, listen_ip, base_port + i),
                daemon=True)
            p.start()
            self.procs.append(p)
        return len(self.procs)

    def stop(self, timeout=5):
        if self.flag is not None:
            self.flag.value = 1
        for p in self.procs:
            p.join(timeout=timeout)
        for p in self.procs:
            if p.is_alive():
                p.terminate()

    def is_running(self):
        return any(p.is_alive() for p in self.procs)

    def snapshot(self):
        """全プロセス合算の (packets, bytes, errors, elapsed, pps, bps)。
        計測区間は「どれかが最初に送/受した時刻〜最後の時刻」。"""
        packets = nbytes = errors = 0
        firsts, lasts = [], []
        for c in self.cells:
            with c['lock']:
                packets += c['packets'].value
                nbytes += c['bytes'].value
                errors += c['errors'].value
                if c['first'].value:
                    firsts.append(c['first'].value)
                if c['last'].value:
                    lasts.append(c['last'].value)
        elapsed = (max(lasts) - min(firsts)) if firsts and lasts else 0.0
        pps = packets / elapsed if elapsed > 0 else 0.0
        bps = nbytes * 8 / elapsed if elapsed > 0 else 0.0
        return packets, nbytes, errors, elapsed, pps, bps


def format_rate(pps: float, bps: float) -> str:
    """pps/bpsを読みやすい単位に整形する"""
    if bps >= 1_000_000_000:
        bps_str = f"{bps / 1_000_000_000:.2f} Gbps"
    elif bps >= 1_000_000:
        bps_str = f"{bps / 1_000_000:.2f} Mbps"
    elif bps >= 1_000:
        bps_str = f"{bps / 1_000:.2f} kbps"
    else:
        bps_str = f"{bps:.0f} bps"
    return f"{pps:,.0f} pps / {bps_str}"


class TrafficGenTab(ttk.Frame, LogMixin):
    """L4トラフィック生成/測定タブ（Hachi相当）。

    送信側と受信側を同じタブに持たせている。実機を挟んだスループット測定
    （PC-A → 実機 → PC-B）では両端でこのタブを使い、送信側の送信数と
    受信側の受信数を突き合わせれば損失も分かる。
    """

    def __init__(self, parent):
        ttk.Frame.__init__(self, parent)
        self.send_stats = TrafficStats()
        self.recv_stats = TrafficStats()
        self.send_stop = threading.Event()
        self.recv_stop = threading.Event()
        self.sending = False
        self.receiving = False
        # プロセス数2以上のときに使うマルチプロセス実行器（1のときはNone）
        self.send_mp = None
        self.recv_mp = None
        self._build_widgets()
        self._poll_stats()

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        info = ttk.Label(
            self, foreground="blue", justify="left",
            text="TCP/UDPのトラフィックを流してスループット(pps/bps)を実測します。\n"
                 "実機を挟んだ測定(PC-A → 装置 → PC-B)では、両端でこのタブの"
                 "送信側/受信側をそれぞれ使ってください。\n"
                 "ソケットベースのためVLANタグやMACの細工はできません"
                 "（そういった試験はRIP/OSPFタブのscapyモードを使ってください）。"
        )
        info.pack(fill="x", padx=8, pady=(6, 0))

        # ── 送信 ──
        send_frame = ttk.LabelFrame(self, text="送信")
        send_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(send_frame, text="プロトコル:").grid(row=0, column=0, sticky="e", **pad)
        self.send_proto_var = tk.StringVar(value="udp")
        ttk.Radiobutton(send_frame, text="UDP", variable=self.send_proto_var,
                        value="udp").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(send_frame, text="TCP", variable=self.send_proto_var,
                        value="tcp").grid(row=0, column=1, sticky="e")

        ttk.Label(send_frame, text="宛先IP:").grid(row=0, column=2, sticky="e", **pad)
        self.dest_ip_var = tk.StringVar()
        ttk.Entry(send_frame, textvariable=self.dest_ip_var, width=16).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(send_frame, text="宛先ポート:").grid(row=0, column=4, sticky="e", **pad)
        self.dest_port_var = tk.StringVar(value="5001")
        ttk.Entry(send_frame, textvariable=self.dest_port_var, width=8).grid(
            row=0, column=5, sticky="w", **pad)

        ttk.Label(send_frame, text="送信元IP:").grid(row=1, column=0, sticky="e", **pad)
        self.src_ip_var = tk.StringVar()
        ttk.Entry(send_frame, textvariable=self.src_ip_var, width=16).grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Label(send_frame, text="(空欄ならOS任せ)").grid(row=1, column=2, sticky="w")

        ttk.Label(send_frame, text="パケットサイズ(byte):").grid(row=2, column=0, sticky="e", **pad)
        self.size_var = tk.StringVar(value="1400")
        ttk.Entry(send_frame, textvariable=self.size_var, width=8).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Label(send_frame, text="レート(pps):").grid(row=2, column=2, sticky="e", **pad)
        self.rate_var = tk.StringVar(value="1000")
        ttk.Entry(send_frame, textvariable=self.rate_var, width=8).grid(
            row=2, column=3, sticky="w", **pad)
        ttk.Label(send_frame, text="(0=無制限)").grid(row=2, column=4, sticky="w")

        ttk.Label(send_frame, text="送信パケット数:").grid(row=3, column=0, sticky="e", **pad)
        self.count_var = tk.StringVar(value="0")
        ttk.Entry(send_frame, textvariable=self.count_var, width=8).grid(
            row=3, column=1, sticky="w", **pad)
        ttk.Label(send_frame, text="(0=停止するまで)").grid(row=3, column=2, sticky="w")
        ttk.Label(send_frame, text="ToS/DSCP:").grid(row=3, column=3, sticky="e", **pad)
        self.tos_var = tk.StringVar(value="0")
        ttk.Entry(send_frame, textvariable=self.tos_var, width=8).grid(
            row=3, column=4, sticky="w", **pad)
        ttk.Label(send_frame, text="(0-255。QoS確認用)").grid(row=3, column=5, sticky="w")

        ttk.Label(send_frame, text="プロセス数:").grid(row=5, column=0, sticky="e", **pad)
        self.send_nproc_var = tk.IntVar(value=1)
        ttk.Spinbox(send_frame, from_=1, to=32, textvariable=self.send_nproc_var,
                    width=6).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(send_frame,
                  text=f"(1プロセスは約24万pps=1472Bで約2.8Gbpsが上限。"
                       f"このPCは{os.cpu_count()}コア。"
                       f"2以上にすると宛先ポートを連番で使います)").grid(
            row=5, column=2, columnspan=4, sticky="w", **pad)

        btn_row = ttk.Frame(send_frame)
        btn_row.grid(row=4, column=0, columnspan=6, sticky="w", **pad)
        self.send_start_btn = ttk.Button(btn_row, text="送信開始", command=self._on_send_start)
        self.send_start_btn.pack(side="left", padx=4)
        self.send_stop_btn = ttk.Button(btn_row, text="送信停止", command=self._on_send_stop,
                                         state="disabled")
        self.send_stop_btn.pack(side="left", padx=4)
        self.send_stat_label = ttk.Label(btn_row, text="送信: -")
        self.send_stat_label.pack(side="left", padx=16)

        # ── 受信 ──
        recv_frame = ttk.LabelFrame(self, text="受信(測定)")
        recv_frame.pack(fill="x", padx=8, pady=6)

        ttk.Label(recv_frame, text="プロトコル:").grid(row=0, column=0, sticky="e", **pad)
        self.recv_proto_var = tk.StringVar(value="udp")
        ttk.Radiobutton(recv_frame, text="UDP", variable=self.recv_proto_var,
                        value="udp").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(recv_frame, text="TCP", variable=self.recv_proto_var,
                        value="tcp").grid(row=0, column=1, sticky="e")

        ttk.Label(recv_frame, text="待ち受けIP:").grid(row=0, column=2, sticky="e", **pad)
        self.listen_ip_var = tk.StringVar()
        ttk.Entry(recv_frame, textvariable=self.listen_ip_var, width=16).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(recv_frame, text="(空欄なら全アドレス)").grid(row=0, column=4, sticky="w")

        ttk.Label(recv_frame, text="待ち受けポート:").grid(row=1, column=0, sticky="e", **pad)
        self.listen_port_var = tk.StringVar(value="5001")
        ttk.Entry(recv_frame, textvariable=self.listen_port_var, width=8).grid(
            row=1, column=1, sticky="w", **pad)

        ttk.Label(recv_frame, text="プロセス数:").grid(row=1, column=2, sticky="e", **pad)
        self.recv_nproc_var = tk.IntVar(value=1)
        ttk.Spinbox(recv_frame, from_=1, to=32, textvariable=self.recv_nproc_var,
                    width=6).grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(recv_frame,
                  text="(送信側と同じ数にしてください。ポートを連番で使います)").grid(
            row=1, column=4, sticky="w", **pad)

        rbtn_row = ttk.Frame(recv_frame)
        rbtn_row.grid(row=2, column=0, columnspan=6, sticky="w", **pad)
        self.recv_start_btn = ttk.Button(rbtn_row, text="受信開始", command=self._on_recv_start)
        self.recv_start_btn.pack(side="left", padx=4)
        self.recv_stop_btn = ttk.Button(rbtn_row, text="受信停止", command=self._on_recv_stop,
                                         state="disabled")
        self.recv_stop_btn.pack(side="left", padx=4)
        self.recv_stat_label = ttk.Label(rbtn_row, text="受信: -")
        self.recv_stat_label.pack(side="left", padx=16)

        action = ttk.Frame(self)
        action.pack(fill="x", padx=8, pady=4)
        ttk.Button(action, text="ログクリア", command=self._clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._init_log(self.log_text)

    # ---- 入力検証 ----
    @staticmethod
    def _parse_int_field(label, raw, minimum=None, maximum=None):
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"「{label}」は数字で入力してください: 「{raw}」")
        if minimum is not None and value < minimum:
            raise ValueError(f"「{label}」は{minimum}以上で指定してください")
        if maximum is not None and value > maximum:
            raise ValueError(f"「{label}」は{maximum}以下で指定してください")
        return value

    # ---- 送信 ----
    def _on_send_start(self):
        if self.sending:
            messagebox.showinfo("送信中", "既に送信中です")
            return
        try:
            dest_ip = self.dest_ip_var.get().strip()
            parse_ip_field("宛先IP", dest_ip)  # 形式チェックのみ
            src_ip = self.src_ip_var.get().strip()
            if src_ip:
                parse_ip_field("送信元IP", src_ip)
            dest_port = self._parse_int_field("宛先ポート", self.dest_port_var.get(), 1, 65535)
            size = self._parse_int_field("パケットサイズ(byte)", self.size_var.get(), 1, 65507)
            rate = self._parse_int_field("レート(pps)", self.rate_var.get(), 0)
            count = self._parse_int_field("送信パケット数", self.count_var.get(), 0)
            tos = self._parse_int_field("ToS/DSCP", self.tos_var.get(), 0, 255)
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        proto = self.send_proto_var.get()
        nproc = max(1, int(self.send_nproc_var.get()))
        self.send_stop.clear()
        self.sending = True
        self.send_start_btn.configure(state="disabled")
        self.send_stop_btn.configure(state="normal")
        port_desc = (f"{dest_port}" if nproc == 1
                     else f"{dest_port}〜{dest_port + nproc - 1}")
        self.log(f"=== 送信開始 {proto.upper()} -> {dest_ip}:{port_desc} "
                  f"size={size}B rate={'無制限' if rate == 0 else str(rate) + 'pps'} "
                  f"count={'無制限' if count == 0 else count} tos={tos} "
                  f"プロセス={nproc} ===")

        self.send_mp = None
        if nproc > 1:
            # レート/送信数はプロセス間で等分される（合計が指定値になる）
            self.send_mp = MultiProcessTraffic()
            try:
                self.send_mp.start_senders(nproc, proto, dest_ip, dest_port, size,
                                            count, rate, src_ip, tos)
            except OSError as e:
                self.log(f"[ERROR] 送信プロセスの起動に失敗: {e}")
                self.send_mp = None
                self.sending = False
                self.send_start_btn.configure(state="normal")
                self.send_stop_btn.configure(state="disabled")
                return

            def watch_mp():
                while self.sending and self.send_mp and self.send_mp.is_running():
                    if self.send_stop.wait(0.2):
                        break
                self._finish_send()

            threading.Thread(target=watch_mp, daemon=True).start()
            return

        def worker():
            try:
                run_traffic_sender(proto, dest_ip, dest_port, size, count, rate,
                                    src_ip, 0, tos, self.send_stop, self.send_stats)
            except OSError as e:
                self.log(f"[ERROR] 送信に失敗: {e}")
            finally:
                self._finish_send()

        threading.Thread(target=worker, daemon=True).start()

    def _finish_send(self):
        if self.send_mp is not None:
            self.send_mp.stop()
        packets, nbytes, errors, elapsed, pps, bps = self._send_snapshot()
        self.log(f"=== 送信終了 {packets:,}パケット / {nbytes:,}バイト / "
                  f"{elapsed:.1f}秒 / {format_rate(pps, bps)} "
                  f"(エラー{errors}件) ===")
        self.sending = False
        self.after(0, lambda: (self.send_start_btn.configure(state="normal"),
                                self.send_stop_btn.configure(state="disabled")))

    def _send_snapshot(self):
        if self.send_mp is not None:
            return self.send_mp.snapshot()
        return self.send_stats.snapshot()

    def _on_send_stop(self):
        self.send_stop.set()
        if self.send_mp is not None:
            self.send_mp.stop()

    # ---- 受信 ----
    def _on_recv_start(self):
        if self.receiving:
            messagebox.showinfo("受信中", "既に受信中です")
            return
        try:
            listen_ip = self.listen_ip_var.get().strip()
            if listen_ip:
                parse_ip_field("待ち受けIP", listen_ip)
            listen_port = self._parse_int_field("待ち受けポート", self.listen_port_var.get(),
                                                 1, 65535)
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        proto = self.recv_proto_var.get()
        nproc = max(1, int(self.recv_nproc_var.get()))
        self.recv_stop.clear()
        self.receiving = True
        self.recv_start_btn.configure(state="disabled")
        self.recv_stop_btn.configure(state="normal")
        port_desc = (f"{listen_port}" if nproc == 1
                     else f"{listen_port}〜{listen_port + nproc - 1}")
        self.log(f"=== 受信開始 {proto.upper()} "
                  f"{listen_ip or '0.0.0.0'}:{port_desc} プロセス={nproc} ===")

        self.recv_mp = None
        if nproc > 1:
            self.recv_mp = MultiProcessTraffic()
            try:
                self.recv_mp.start_receivers(nproc, proto, listen_ip, listen_port)
            except OSError as e:
                self.log(f"[ERROR] 受信プロセスの起動に失敗: {e}")
                self.recv_mp = None
                self.receiving = False
                self.recv_start_btn.configure(state="normal")
                self.recv_stop_btn.configure(state="disabled")
                return

            def watch_mp():
                while self.receiving and not self.recv_stop.wait(0.2):
                    pass
                self._finish_recv()

            threading.Thread(target=watch_mp, daemon=True).start()
            return

        def worker():
            try:
                run_traffic_receiver(proto, listen_ip, listen_port,
                                      self.recv_stop, self.recv_stats)
            except OSError as e:
                self.log(f"[ERROR] 受信の待ち受けに失敗: {e}"
                          "(ポートが既に使われていませんか？)")
            finally:
                self._finish_recv()

        threading.Thread(target=worker, daemon=True).start()

    def _finish_recv(self):
        if self.recv_mp is not None:
            self.recv_mp.stop()
        packets, nbytes, errors, elapsed, pps, bps = self._recv_snapshot()
        self.log(f"=== 受信終了 {packets:,}パケット / {nbytes:,}バイト / "
                  f"{elapsed:.1f}秒 / {format_rate(pps, bps)} "
                  f"(エラー{errors}件) ===")
        self.receiving = False
        self.after(0, lambda: (self.recv_start_btn.configure(state="normal"),
                                self.recv_stop_btn.configure(state="disabled")))

    def _recv_snapshot(self):
        if self.recv_mp is not None:
            return self.recv_mp.snapshot()
        return self.recv_stats.snapshot()

    def _on_recv_stop(self):
        self.recv_stop.set()
        if self.recv_mp is not None:
            self.recv_mp.stop()

    # ---- 統計表示 ----
    def _poll_stats(self):
        for snap, label, name in ((self._send_snapshot, self.send_stat_label, "送信"),
                                   (self._recv_snapshot, self.recv_stat_label, "受信")):
            packets, nbytes, _errors, elapsed, pps, bps = snap()
            if packets or elapsed:
                label.configure(text=f"{name}: {packets:,}pkt / {nbytes:,}B / "
                                      f"{elapsed:.1f}s / {format_rate(pps, bps)}")
        self.after(500, self._poll_stats)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ネットワーク試験統合ツール (RIP / OSPF / BGP / トラフィック)")
        self.geometry("900x760")
        self.minsize(760, 600)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        notebook.add(RipTab(notebook), text="RIP")
        rip_multi_tab = RipMultiRouterTab(notebook)
        notebook.add(rip_multi_tab, text="RIP (疑似ルータ生成)")
        notebook.add(OspfP2PTab(notebook), text="OSPF (P2P)")
        notebook.add(OspfMassTab(notebook), text="OSPF (Broadcast/大量)")
        notebook.add(BgpTab(notebook), text="BGP")
        notebook.add(BgpMultiPeerTab(notebook), text="BGP (疑似ルータ生成)")
        notebook.add(TrafficGenTab(notebook), text="トラフィック生成/測定")
        # 実機との継続的なRIP交換検証では「RIP」タブ(1回限りの送信)より
        # 「RIP (疑似ルータ生成)」タブ(実機同様に定期送信し続けられる)を
        # 使うことが多いため、こちらを起動時のデフォルトタブにする
        notebook.select(rip_multi_tab)


def main():
    # PyInstaller等でexe化した場合、これが無いと子プロセスがGUIを
    # 再起動してしまう（Windowsのspawn方式のため）
    multiprocessing.freeze_support()
    app = MainApp()
    app.mainloop()


if __name__ == "__main__":
    main()
