"""
ネットワークラボ FastAPI サーバー
- ルールベースCLIエンジン（オフライン完全動作）
- Ollama連携（インストール済みなら自動切替）
- WebSocket（VRRP/RIP/OSPF/STPプロトコルシミュレーション）
"""
import os, asyncio, json, re, httpx, time, random
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# dotenvがあれば読む
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ルールベースエンジン
import sys
sys.path.insert(0, str(Path(__file__).parent))
from engine.rules import RuleEngine, DeviceState
from engine.protocols import (
    vnet, rip_engine, ospf_engine, bgp_engine, stp_engine, rib_engine,
    icmp_engine, redistribute, filter_engine, arp_engine, ipfilter_engine,
    genie_engine, lacp_engine, vrrp_engine, vlan_engine, vpc_engine,
    sir_msg, cisco_msg, nxos_msg, apresia_msg,
)
from engine.syslog_sender import syslog_dispatcher, snmp_dispatcher, ntp_client
from engine.ike_engine import negotiate_ipsec
from engine.config_importer import import_running_config, validate_config_text

# ══════════════════════════════════════════
# 設定
# ══════════════════════════════════════════
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
USE_OLLAMA   = False   # 起動時に自動検出

SAVED_CONFIG_PATH = Path(__file__).parent / "saved_config.json"

rule_engine = RuleEngine()

# セッション管理（デバイスIDごとに状態を保持）
device_sessions: Dict[str, DeviceState] = {}

# デフォルト装置定義
DEFAULT_DEVICES = {
    "sir-a":    {"type": "sir",      "hostname": "Router-A", "color": "#00c840"},
    "sir-b":    {"type": "sir",      "hostname": "Router-B", "color": "#00a040"},
    "srs":      {"type": "srs",      "hostname": "Core-SW",  "color": "#00a0ff"},
    "catalyst": {"type": "catalyst", "hostname": "Dist-SW",  "color": "#1e90ff"},
    "cisco":    {"type": "cisco",    "hostname": "GW-Router","color": "#e05a00"},
    "apresia":  {"type": "apresia",  "hostname": "sw1",      "color": "#c00040"},
    "pc-1":     {"type": "pc",       "hostname": "PC-1",     "color": "#888888", "ip": "192.168.1.10", "gateway": "192.168.1.1"},
    "pc-2":     {"type": "pc",       "hostname": "PC-2",     "color": "#555555", "ip": "192.168.1.11", "gateway": "192.168.1.1"},
    "asa":      {"type": "asa",      "hostname": "ASA-FW",   "color": "#cc4400"},
}

# ══════════════════════════════════════════
# Ollama 自動検出
# ══════════════════════════════════════════
async def detect_ollama() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code == 200:
                models = r.json().get("models", [])
                names = [m["name"] for m in models]
                print(f"[Ollama] 検出: {names}")
                return len(models) > 0
    except Exception:
        pass
    return False

async def query_ollama(prompt: str, system: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            })
            if r.status_code == 200:
                return r.json()["message"]["content"].strip()
    except Exception as e:
        print(f"[Ollama] エラー: {e}")
    return None

# ══════════════════════════════════════════
# アプリ起動
# ══════════════════════════════════════════
def _trigger_ike_negotiation(device_id: str):
    """IKE ネゴシエーションを実行してログをバッファに積む"""
    results = negotiate_ipsec(device_sessions, device_id)
    buf = proto_log_buffer.setdefault(device_id, [])
    for tid_str, result in results.items():
        for log_line in result.logs:
            buf.append({'type': 'sir_log', 'message': log_line,
                        'hostname': device_sessions[device_id].hostname,
                        'facility': 'IPSEC' if 'IPsec' in log_line else 'IKE',
                        'level': 'INFO' if result.success else 'ERROR'})
        # 対向デバイスにも結果ログを積む
        state = device_sessions.get(device_id)
        if state:
            for _, t in getattr(state, 'ipsec_tunnels', {}).items():
                peer_ip = t.get('remote_ip', '')
                for pid, ps in device_sessions.items():
                    if pid == device_id:
                        continue
                    for _, pt in getattr(ps, 'ipsec_tunnels', {}).items():
                        if pt.get('local_ip') == peer_ip:
                            pbuf = proto_log_buffer.setdefault(pid, [])
                            for log_line in result.logs:
                                pbuf.append({'type': 'sir_log', 'message': log_line,
                                             'hostname': ps.hostname,
                                             'facility': 'IPSEC' if 'IPsec' in log_line else 'IKE',
                                             'level': 'INFO' if result.success else 'ERROR'})


def _save_config():
    """現在のデバイス設定・リンクをJSONに保存"""
    data = {"devices": {}, "links": []}
    for dev_id, state in device_sessions.items():
        dev_data = {
            "type": state.device_type,
            "hostname": state.hostname,
            "interfaces": {},
        }
        for ifname, info in state.interfaces.items():
            dev_data["interfaces"][ifname] = {
                "ip":     info.get("ip", ""),
                "prefix": info.get("prefix", 24),
                "status": info.get("status", "up"),
            }
        # ルートテーブルを保存（全デバイス共通）
        routes = getattr(state, "routes", [])
        if routes:
            dev_data["routes"] = routes
        if state.device_type == "pc":
            dev_data["gateway"] = getattr(state, "gateway", "")
        # VLANを保存
        vlans = getattr(state, "vlans", {})
        if vlans:
            dev_data["vlans"] = vlans
        # Si-R IPsec設定を保存
        if state.device_type in ("sir", "srs"):
            tunnels = getattr(state, "ipsec_tunnels", {})
            if tunnels:
                dev_data["ipsec_tunnels"]  = tunnels
                dev_data["ipsec_enabled"]  = getattr(state, "ipsec_enabled", False)
                dev_data["ike_enabled"]    = getattr(state, "ike_enabled", False)
        data["devices"][dev_id] = dev_data
    # vnetリンク
    for a, neighbors in vnet.links.items():
        for b in neighbors:
            if {"a": a, "b": b} not in data["links"] and {"a": b, "b": a} not in data["links"]:
                data["links"].append({"a": a, "b": b})
    try:
        with open(SAVED_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[Config] 設定を保存しました: {SAVED_CONFIG_PATH}")
    except Exception as e:
        print(f"[Config] 保存エラー: {e}")


async def _auto_save_loop():
    """60秒ごとに設定を自動保存"""
    while True:
        await asyncio.sleep(60)
        _save_config()
        print("[Config] 自動保存完了")


def _load_config():
    """保存済みの設定をロード。存在しなければデフォルトで初期化"""
    if not SAVED_CONFIG_PATH.exists():
        # デフォルト初期化
        for dev_id, dev in DEFAULT_DEVICES.items():
            state = DeviceState(dev["type"], dev["hostname"])
            # PCはデフォルトIP/GWを適用
            if dev.get("type") == "pc" and dev.get("ip"):
                state.interfaces["eth0"]["ip"] = dev["ip"]
                if dev.get("gateway"):
                    state.gateway = dev["gateway"]
                    state.routes[0]["gw"] = dev["gateway"]
            device_sessions[dev_id] = state
            ifaces = {name: {'ip': info['ip'], 'prefix': info.get('prefix', 24)}
                      for name, info in state.interfaces.items() if info.get('ip')}
            icmp_engine.register_device(dev_id, state.hostname, ifaces)
        return

    try:
        with open(SAVED_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Config] ロードエラー: {e} — デフォルトで初期化")
        for dev_id, dev in DEFAULT_DEVICES.items():
            state = DeviceState(dev["type"], dev["hostname"])
            device_sessions[dev_id] = state
        return

    for dev_id, dev_data in data.get("devices", {}).items():
        state = DeviceState(dev_data["type"], dev_data["hostname"])
        # インタフェースIPを復元
        for ifname, iinfo in dev_data.get("interfaces", {}).items():
            if ifname in state.interfaces:
                state.interfaces[ifname].update({
                    "ip":     iinfo.get("ip", ""),
                    "prefix": iinfo.get("prefix", 24),
                    "status": iinfo.get("status", "up"),
                })
            else:
                state.interfaces[ifname] = iinfo
        # ルートを復元（全デバイス共通）
        if dev_data.get("routes"):
            state.routes = dev_data["routes"]
        # PCのゲートウェイを復元
        if dev_data["type"] == "pc":
            if dev_data.get("gateway"):
                state.gateway = dev_data["gateway"]
        # VLANを復元
        if dev_data.get("vlans") and hasattr(state, "vlans"):
            state.vlans = dev_data["vlans"]
        # Si-R IPsec設定を復元
        if dev_data["type"] in ("sir", "srs") and dev_data.get("ipsec_tunnels"):
            state.ipsec_tunnels = dev_data["ipsec_tunnels"]
            state.ipsec_enabled = dev_data.get("ipsec_enabled", False)
            state.ike_enabled   = dev_data.get("ike_enabled", False)
        device_sessions[dev_id] = state
        ifaces = {name: {'ip': info.get('ip',''), 'prefix': info.get('prefix',24)}
                  for name, info in state.interfaces.items() if info.get('ip')}
        icmp_engine.register_device(dev_id, state.hostname, ifaces)

    # リンクを復元
    for link in data.get("links", []):
        a, b = link.get("a"), link.get("b")
        if a and b:
            vnet.add_link(a, b)

    print(f"[Config] 設定をロードしました: {len(data.get('devices',{}))} devices, "
          f"{len(data.get('links',[]))} links")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global USE_OLLAMA
    USE_OLLAMA = await detect_ollama()
    mode = f"Ollama ({OLLAMA_MODEL})" if USE_OLLAMA else "ルールベース（オフライン）"
    print(f"""
╔══════════════════════════════════════════════════════════╗
║   ネットワークラボ エミュレーター 起動                   ║
╠══════════════════════════════════════════════════════════╣
║   URL:  http://localhost:8000                            ║
║   モード: {mode:<44}║
╚══════════════════════════════════════════════════════════╝
""")
    # 保存済み設定をロード（なければデフォルト初期化）
    _load_config()
    # 定期自動保存タスク（60秒ごと）
    auto_save_task = asyncio.create_task(_auto_save_loop())
    yield
    auto_save_task.cancel()
    try:
        await auto_save_task
    except asyncio.CancelledError:
        pass
    # シャットダウン時にも保存
    _save_config()

app = FastAPI(title="ネットワークラボ", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ══════════════════════════════════════════
# API エンドポイント
# ══════════════════════════════════════════

@app.get("/api/protocol-log/{device_id}")
async def get_protocol_log(device_id: str):
    """蓄積されたプロトコルログを取得してクリア"""
    buf = proto_log_buffer.get(device_id, [])
    logs = []
    for m in buf:
        mt = m.get('type', '')
        if mt in ('vpc_log', 'vrrp_log', 'hsrp_log', 'sir_log', 'cisco_log', 'nxos_log', 'apresia_log'):
            # これらはオブジェクト全体を返す（フロントで役割情報を使うため）
            logs.append(m)
        elif m.get('message'):
            logs.append(m.get('message', ''))
    proto_log_buffer[device_id] = []  # 取得後クリア
    return {"logs": logs}


@app.post("/api/complete")
async def api_complete(req: dict):
    """Tab補完エンドポイント"""
    device_id = req.get("device_id", "")
    partial = req.get("partial", "")
    state = device_sessions.get(device_id)
    if not state or not partial:
        return {"completed": partial}
    from engine.rules import cli_completion
    completed = cli_completion.complete(partial, state)
    return {"completed": completed}


@app.get("/api/status")
async def get_status():
    return {
        "mode": "ollama" if USE_OLLAMA else "rules",
        "ollama_model": OLLAMA_MODEL if USE_OLLAMA else None,
        "devices": list(device_sessions.keys()),
    }

@app.post("/api/cli")
async def cli_command(body: dict):
    """
    CLIコマンドを処理して応答を返す
    body: { device_id, command, chat_history? }
    """
    device_id = body.get("device_id", "sir-a")
    command   = body.get("command", "").strip()

    # セッション取得/作成
    if device_id not in device_sessions:
        dev = DEFAULT_DEVICES.get(device_id, {"type": "cisco", "hostname": device_id})
        device_sessions[device_id] = DeviceState(dev["type"], dev["hostname"])

    state = device_sessions[device_id]

    # クリアコマンドはフロントエンドで処理
    if command.lower() in ("cls", "clear screen"):
        return {"output": "\x0c", "mode": state.mode, "hostname": state.hostname}

    # ── ICMP: ping / traceroute（実到達性判定）──
    icmp_out = handle_icmp(device_id, command, state)
    if icmp_out is not None:
        return {"output": icmp_out, "mode": state.mode, "hostname": state.hostname}

    # ── プロトコル動的show（エンジンが起動していればエンジンの出力を優先）──
    proto_output = await handle_protocol_show(device_id, command, state)
    if proto_output is not None:
        return {"output": proto_output, "mode": state.mode, "hostname": state.hostname}

    # ── プロトコル設定コマンド検出 ──
    await handle_protocol_config(device_id, command, state)

    # ルールベースで処理
    output = rule_engine.process(command, state)

    # IPアドレス・ルート変更があればicmp_engineに再登録
    c_low = command.lower().strip()
    if re.search(r'ip\s+address|ip\s+route|remote\s+\d+\s+ip\s+route|'
                 r'lan\s+\d+\s+ip\s+address|wan\s+\d+\s+ip\s+address', c_low):
        _register_icmp(device_id)

    # ルールで空応答かつOllamaあり → Ollamaで補完
    if USE_OLLAMA and output == "" and command.strip():
        system = build_system_prompt(state)
        ollama_out = await query_ollama(command, system)
        if ollama_out:
            output = ollama_out

    return {
        "output": output,
        "mode": state.mode,
        "hostname": state.hostname,
    }


# ══════════════════════════════════════════
# プロトコルコマンド処理
# ══════════════════════════════════════════
async def handle_protocol_config(device_id: str, command: str, state: DeviceState):
    """設定コマンドを検出してプロトコルエンジンを起動/更新"""
    c = command.lower().strip()
    orig = command.strip()
    hostname = state.hostname

    # ── NX-OS: コマンド実行ログ ──
    if state.device_type == 'nexus':
        hn = state.hostname
        if c in ('exit','end','quit') and state.mode == 'config':
            _nxos_log(device_id, nxos_msg.config_saved(hn))
        elif re.match(r'^hostname\s+(\S+)', c):
            m_hn = re.match(r'^hostname\s+(\S+)', c)
            if m_hn and m_hn.group(1) != hn:
                _nxos_log(device_id, nxos_msg.hostname_changed(hn, hn, m_hn.group(1)))
        elif c == 'no shutdown' or c == 'no shut':
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _nxos_log(device_id, nxos_msg.if_updown(hn, iface, 'up'))
        elif c == 'shutdown' and state.mode == 'config-if':
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _nxos_log(device_id, nxos_msg.if_updown(hn, iface, 'down'))
        elif re.match(r'^vpc\s+peer-link$', c) and state.mode == 'config-if':
            iface = getattr(state, 'current_if', '') or ''
            dom = vpc_engine.domains.get(device_id)
            if iface and dom:
                _nxos_log(device_id, nxos_msg.vpc_peer_link_up(hn, dom.domain_id, iface))
        elif re.match(r'^vrrp\s+(\d+)\s+ip', c):
            m_vr = re.match(r'^vrrp\s+(\d+)', c)
            iface = getattr(state, 'current_if', '') or ''
            if m_vr and iface:
                _nxos_log(device_id, nxos_msg.vrrp_state(
                    hn, int(m_vr.group(1)), iface, 'Init', 'Init'))
        elif re.match(r'^channel-group\s+(\d+)', c):
            m_ch = re.match(r'^channel-group\s+(\d+)', c)
            iface = getattr(state, 'current_if', '') or ''
            if m_ch and iface:
                _nxos_log(device_id, nxos_msg.lacp_bundle(
                    hn, int(m_ch.group(1)), [iface]))

    # ── APRESIA: コマンド実行ログ ──
    if state.device_type == 'apresia':
        hn = state.hostname
        if c == 'save':
            _apresia_log(device_id, apresia_msg.sys_config_saved())
            _save_config()
        elif re.match(r'^config\s+ports\s+(\S+)\s+state\s+enable', c):
            m_p = re.match(r'^config\s+ports\s+(\S+)', c)
            if m_p:
                _apresia_log(device_id, apresia_msg.port_up(m_p.group(1)))
        elif re.match(r'^config\s+ports\s+(\S+)\s+state\s+disable', c):
            m_p = re.match(r'^config\s+ports\s+(\S+)', c)
            if m_p:
                _apresia_log(device_id, apresia_msg.port_down(m_p.group(1)))
        elif re.match(r'^create\s+vlan\s+(\d+)', c):
            m_v = re.match(r'^create\s+vlan\s+(\d+)', c)
            if m_v:
                _apresia_log(device_id, apresia_msg.vlan_created(int(m_v.group(1))))
        elif re.match(r'^delete\s+vlan\s+(\d+)', c):
            m_v = re.match(r'^delete\s+vlan\s+(\d+)', c)
            if m_v:
                _apresia_log(device_id, apresia_msg.vlan_deleted(int(m_v.group(1))))
    # ── ASA: write memory / IKE negotiate ──
    if state.device_type == 'asa':
        if c in ('write memory', 'write', 'wr', 'copy running-config startup-config'):
            _save_config()
        elif re.match(r'^crypto\s+isakmp\s+enable', c):
            _trigger_ike_negotiation(device_id)
    # ── Cisco IOS: IKE negotiate ──
    if state.device_type in ('cisco', 'catalyst'):
        if (re.match(r'^crypto\s+isakmp\s+enable', c) or
                re.match(r'^crypto\s+isakmp\s+key', c) or
                re.match(r'^crypto\s+map\s+\S+\s+interface', c)):
            _trigger_ike_negotiation(device_id)
    if state.device_type in ('catalyst', 'cisco', 'srs', 'nexus'):
        prev_mode = state.mode
        if c in ('configure terminal', 'conf t', 'conf terminal',
                 'config t', 'configure') and prev_mode == 'exec':
            # conf t → CONFIG_I はexitで設定保存完了時に出るのが正しいが
            # エミュレーターではconf t時に出す
            pass  # exitで出す
        elif c in ('exit', 'end', 'quit') and prev_mode == 'config':
            # configモード終了 → %SYS-5-CONFIG_I
            _cisco_log(device_id, cisco_msg.sys_config_i(
                via='console', src='console'))
        elif c == 'write' or c == 'write memory' or c == 'wr':
            _cisco_log(device_id, cisco_msg.sys_config_i(
                via='console', src='console'))
            _cisco_log(device_id, cisco_msg._fmt('SYS', 5, 'CONFIG_I',
                'Configuration saved to NVRAM'))
            _save_config()
        # hostname変更
        elif re.match(r'^hostname\s+(\S+)', c):
            m_hn = re.match(r'^hostname\s+(\S+)', c)
            if m_hn:
                new_hn = m_hn.group(1)
                if new_hn != state.hostname:
                    _cisco_log(device_id, cisco_msg.sys_hostname_changed(
                        state.hostname, new_hn))
        # no shutdown → Link Up
        elif c == 'no shutdown' or c == 'no shut':
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _cisco_log(device_id, cisco_msg.link_changed(iface, 'up'))
        # shutdown → Link Down
        elif c == 'shutdown' and state.mode == 'config-if':
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _cisco_log(device_id, cisco_msg.link_changed(iface, 'down'))
        # switchport portfast → STP PortFast
        elif re.match(r'^spanning-tree\s+portfast', c):
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _cisco_log(device_id, cisco_msg.stp_portfast(iface))
        # spanning-tree bpduguard enable
        elif re.match(r'^spanning-tree\s+bpduguard\s+enable', c):
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                _cisco_log(device_id, cisco_msg._fmt('SPANTREE', 6, 'BPDUGUARD',
                    f'Interface {iface}: BPDU Guard is enabled'))
        # channel-group → EtherChannel
        elif re.match(r'^channel-group\s+(\d+)\s+mode\s+(\S+)', c):
            m_ch = re.match(r'^channel-group\s+(\d+)\s+mode\s+(\S+)', c)
            iface = getattr(state, 'current_if', '') or ''
            if m_ch and iface:
                ch_id = m_ch.group(1)
                _cisco_log(device_id, cisco_msg._fmt('EC', 5, 'BUNDLE',
                    f'Interface {iface} joined port-channel{ch_id}'))
        # vrrp → VRRP初期化
        elif re.match(r'^vrrp\s+\d+\s+ip\s+', c):
            m_vr = re.match(r'^vrrp\s+(\d+)\s+ip\s+', c)
            iface = getattr(state, 'current_if', '') or ''
            if m_vr and iface:
                _cisco_log(device_id, cisco_msg.vrrp_statechange(
                    int(m_vr.group(1)), iface, 'Init', 'Init'))
        # standby → HSRP初期化
        elif re.match(r'^standby\s+\d+\s+ip\s+', c):
            m_hsrp = re.match(r'^standby\s+(\d+)\s+ip\s+', c)
            iface = getattr(state, 'current_if', '') or ''
            if m_hsrp and iface:
                _cisco_log(device_id, cisco_msg.hsrp_statechange(
                    int(m_hsrp.group(1)), iface, 'Init', 'Init'))
    if state.device_type == 'sir':
        prev_mode = state.mode
        # configure/conf コマンドでconfigモードに移行した直後にログ
        if c in ('configure', 'conf', 'config', 'configure terminal', 'conf t') and prev_mode == 'exec':
            _sir_log(device_id, 'SYSTEM', f'configuration mode entered by admin (console)')
        # exit/endでexecモードに戻る直前
        elif c in ('exit', 'end', 'quit') and prev_mode == 'config':
            _sir_log(device_id, 'SYSTEM', f'configuration mode exited by admin')
        # saveコマンド
        elif c == 'save':
            _sir_log(device_id, 'SYSTEM', 'configuration saved by admin')
            _save_config()
        # hostnameコマンド
        elif c.startswith('hostname ') or c.startswith('sysname '):
            _sir_log(device_id, 'SYSTEM', f'hostname changed to {orig.split(None,1)[1] if " " in orig else ""}')
        # インタフェース Link Up シミュレーション
        elif re.match(r'^lan\s+\d+\s+ip\s+address', c) or re.match(r'^ether\s+\d+', c):
            iface = f'lan{c.split()[1]}' if c.startswith('lan') else f'ether{c.split()[1]}'
            _sir_log(device_id, 'LINK', f'{iface} Link Up (1Gbps full-duplex)')
        # RIP設定
        elif c.startswith('ip rip') and 'use' in c:
            # ip rip use use のみ（networkコマンドでは重複しない）
            _sir_log(device_id, 'RIP', 'RIPv2 started on all configured interfaces')
        # OSPF設定
        elif c.startswith('ospf use on') or (c.startswith('router ospf') and state.mode == 'config'):
            _sir_log(device_id, 'OSPF', 'OSPF process started')
        # BGP設定
        elif c.startswith('bgp use on') or c.startswith('router bgp'):
            _sir_log(device_id, 'BGP', 'BGP process started')
        # VRRP設定
        elif 'vrrp' in c and 'group' in c:
            _sir_log(device_id, 'VRRP', 'VRRP initialized')
        # IPsec/IKE ネゴシエーション
        elif c in ('ike use on', 'ipsec use on') or re.match(r'^remote\s+\d+\s+ap\s+\d+\s+ipsec\s+ike\s+preshared-key', c):
            _trigger_ike_negotiation(device_id)

    # ── prefix-list ──
    # Cisco: "ip prefix-list NAME seq 5 permit 10.0.0.0/8 ge 24 le 30"
    pl = re.match(
        r'^ip\s+prefix-list\s+(\S+)(?:\s+seq\s+(\d+))?\s+(permit|deny)\s+'
        r'([\d.]+)/(\d+)(?:\s+ge\s+(\d+))?(?:\s+le\s+(\d+))?', orig, re.I)
    if pl:
        name = pl.group(1)   # 大文字小文字保持
        seq = int(pl.group(2)) if pl.group(2) else None
        action = pl.group(3).lower()
        net = pl.group(4)
        prefix = int(pl.group(5))
        ge = int(pl.group(6)) if pl.group(6) else None
        le = int(pl.group(7)) if pl.group(7) else None
        filter_engine.add_prefix_list(device_id, name, action, net, prefix, seq, ge, le)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'filter_log',
                    'message': f'prefix-list {name}: {action} {net}/{prefix} 追加'})
        return

    # ── distribute-list ──
    dl = re.match(
        r'^distribute-list\s+(?:prefix\s+)?(\S+)\s+(in|out)', orig, re.I)
    if dl and getattr(state, '_routing_mode', None):
        list_name = dl.group(1)
        direction = dl.group(2).lower()
        proto = state._routing_mode
        filter_engine.set_distribute_list(device_id, proto, direction, list_name)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'filter_log',
                    'message': f'distribute-list {list_name} を {proto} {direction} に適用'})
        return

    # ── Si-R route-manage（経路制御）──
    sir_rm = re.match(
        r'^(?:ip\s+)?route-manage\s+(?:filter\s+)?(\S+)\s+(permit|deny)\s+'
        r'([\d.]+)/(\d+)', orig, re.I)
    if sir_rm:
        name = sir_rm.group(1)
        action = sir_rm.group(2).lower()
        net = sir_rm.group(3)
        prefix = int(sir_rm.group(4))
        filter_engine.add_prefix_list(device_id, name, action, net, prefix)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'filter_log',
                    'message': f'route-manage {name}: {action} {net}/{prefix}'})
        return
    # Si-R: 再配信フィルタ "redistribute <src> route-manage <name>"
    sir_redist_rm = re.match(
        r'^redistribute\s+(rip|ospf|bgp|static|connected)\s+route-manage\s+(\S+)',
        orig, re.I)
    if sir_redist_rm and getattr(state, '_routing_mode', None):
        source = sir_redist_rm.group(1).lower()
        list_name = sir_redist_rm.group(2)
        target = state._routing_mode
        filter_engine.set_redist_filter(device_id, target, source, list_name)
        count = await redistribute(device_id, target, source)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'redist_log',
                    'message': f'再配信(フィルタ付): {source} → {target} '
                               f'route-manage {list_name} ({count}経路)'})
        return

    # ── EtherChannel / LACP（Cisco/Catalyst）──
    # "interface range GigabitEthernet1/0/1 - 2" で複数ポート選択
    irange = re.match(
        r'^interface\s+range\s+(?:gigabitethernet|gi)\s*([\d/]+)\s*-\s*(\d+)',
        c, re.I)
    if irange:
        base = irange.group(1)
        end = int(irange.group(2))
        parts = base.split('/')
        start = int(parts[-1])
        prefix = '/'.join(parts[:-1])
        state._range_ports = [f'GigabitEthernet{prefix}/{i}'
                              for i in range(start, end + 1)]
        return
    # 単一インタフェース選択 "interface GigabitEthernet1/0/1"
    isingle = re.match(r'^interface\s+(?:gigabitethernet|gi)\s*([\d/]+)$', c, re.I)
    if isingle:
        state._range_ports = [f'GigabitEthernet{isingle.group(1)}']
        return
    # "channel-group N mode active/passive/on"
    cg = re.match(r'^channel-group\s+(\d+)\s+mode\s+(active|passive|on)', c)
    if cg:
        po_num = int(cg.group(1))
        mode = cg.group(2)
        ports = getattr(state, '_range_ports', [])
        for port in ports:
            lacp_engine.add_member(device_id, po_num, port, mode)
        peer = find_peer_by_link(device_id)
        if peer:
            peer_ch = lacp_engine.get_channel(peer, po_num)
            if peer_ch:
                ok = lacp_engine.negotiate(device_id, po_num, peer, po_num)
                lacp_engine.negotiate(peer, po_num, device_id, po_num)
                buf = proto_log_buffer.setdefault(device_id, [])
                result = 'bundled (成立)' if ok else 'independent (不成立)'
                buf.append({'type': 'lacp_log',
                            'message': f'EtherChannel Po{po_num}: LACP {result}'})
        return

    # ── SR-S LACP（link-aggregationコマンド系）──
    # "ether 1-2 type linkaggregation 1 1" — ポート割り当て
    sr_ether_lag = re.match(r'^ether\s+(\d+)[-–](\d+)\s+type\s+linkaggregation\s+(\d+)', c)
    if sr_ether_lag:
        start_p = int(sr_ether_lag.group(1))
        end_p   = int(sr_ether_lag.group(2))
        lag_num = int(sr_ether_lag.group(3))
        # SR-SのポートをGigabitEthernet1/0/N形式でlacp_engineに登録
        for p in range(start_p, end_p + 1):
            lacp_engine.add_member(device_id, lag_num,
                                   f'GigabitEthernet1/0/{p}', 'active')
        state._lag_current = lag_num
        return
    # "linkaggregation N mode active/passive/static" — グループ設定
    sr_lag_mode = re.match(r'^linkaggregation\s+(\d+)\s+mode\s+(active|passive|static|on)', c)
    if sr_lag_mode:
        lag_num = int(sr_lag_mode.group(1))
        mode = sr_lag_mode.group(2)
        if mode == 'static':
            mode = 'on'
        ch = lacp_engine.get_channel(device_id, lag_num)
        if ch:
            ch['mode'] = mode
            ch['protocol'] = 'static' if mode == 'on' else 'lacp'
        # 対向とネゴ
        peer = find_peer_by_link(device_id)
        if peer:
            peer_ch = lacp_engine.get_channel(peer, lag_num)
            if peer_ch:
                ok = lacp_engine.negotiate(device_id, lag_num, peer, lag_num)
                lacp_engine.negotiate(peer, lag_num, device_id, lag_num)
                buf = proto_log_buffer.setdefault(device_id, [])
                result = 'bundled (成立)' if ok else 'independent (不成立)'
                buf.append({'type': 'lacp_log',
                            'message': f'linkagg{lag_num}: LACP {result} (mode={mode})'})
        return
    # "no linkaggregation N" — グループ削除
    sr_no_lag = re.match(r'^no\s+linkaggregation\s+(\d+)', c)
    if sr_no_lag:
        lag_num = int(sr_no_lag.group(1))
        lacp_engine.channels.get(device_id, {}).pop(lag_num, None)
        return

    # ── NX-OS vPC 設定（Nexus 9000専用）──
    if state.device_type != 'nexus':
        pass  # nexus以外はスキップ（下に続く）
    else:
        # feature vpc
        if re.match(r'^feature\s+vpc', c):
            vpc_engine.enable_feature(device_id)
            return
        if re.match(r'^no\s+feature\s+vpc', c):
            vpc_engine.feature_enabled[device_id] = False
            return
        # vpc domain <id>
        m_vpc_domain = re.match(r'^vpc\s+domain\s+(\d+)', c)
        if m_vpc_domain:
            did = int(m_vpc_domain.group(1))
            vpc_engine.create_domain(device_id, did)
            state.mode = 'config-vpc-domain'
            return
        # vpc domain内のコマンド
        if state.mode == 'config-vpc-domain':
            d = vpc_engine.domains.get(device_id)
            # role priority <value>
            m_role_pri = re.match(r'^role\s+priority\s+(\d+)', c)
            if m_role_pri and d:
                vpc_engine.set_role_priority(device_id, int(m_role_pri.group(1)))
                return
            # peer-keepalive destination <ip> [source <ip>] [vrf <vrf>]
            m_ka = re.match(r'^peer-keepalive\s+destination\s+([\d.]+)(?:\s+source\s+([\d.]+))?(?:\s+vrf\s+(\S+))?', c)
            if m_ka:
                vpc_engine.set_keepalive(
                    device_id, m_ka.group(1),
                    m_ka.group(2) or '',
                    m_ka.group(3) or 'management'
                )
                return
            # peer-gateway
            if re.match(r'^peer-gateway', c):
                vpc_engine.set_peer_gateway(device_id, True)
                return
            if re.match(r'^no\s+peer-gateway', c):
                vpc_engine.set_peer_gateway(device_id, False)
                return
            # peer-switch
            if re.match(r'^peer-switch', c):
                vpc_engine.set_peer_switch(device_id, True)
                return
            # auto-recovery
            if re.match(r'^auto-recovery', c):
                if d: d.auto_recovery = True
                return
            # system-priority <value>
            m_sys_pri = re.match(r'^system-priority\s+(\d+)', c)
            if m_sys_pri and d:
                d.system_priority = int(m_sys_pri.group(1))
                return
            # system-mac <mac>
            m_sys_mac = re.match(r'^system-mac\s+(\S+)', c)
            if m_sys_mac and d:
                d.system_mac = m_sys_mac.group(1)
                return

        # vPC障害シミュレーション（peer-linkより先に判定）
        if re.match(r'^vpc\s+peer\s+failure', c):
            await vpc_engine.simulate_peer_failure(device_id)
            return
        if re.match(r'^vpc\s+peer-link\s+failure', c):
            await vpc_engine.simulate_peer_link_failure(device_id)
            return
        # vpc peer-link（インターフェースモード内）
        if re.match(r'^vpc\s+peer-link', c):
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                vpc_engine.set_peer_link(device_id, iface)
            return
        # vpc <id>（インターフェースモード内）
        m_vpc_member = re.match(r'^vpc\s+(\d+)$', c)
        if m_vpc_member:
            iface = getattr(state, 'current_if', '') or ''
            if iface:
                vpc_engine.add_vpc_member(device_id, iface, int(m_vpc_member.group(1)))
            return

    # ── vPC / NX-OS show コマンド ──
    if re.match(r'^show\s+vpc\s+peer-keepalive', c):
        return vpc_engine.format_show_vpc_keepalive(device_id)
    if re.match(r'^show\s+vpc\s+role', c):
        return vpc_engine.format_show_vpc_role(device_id)
    if re.match(r'^show\s+vpc\s+consistency', c):
        return vpc_engine.format_show_vpc_consistency(device_id)
    if re.match(r'^show\s+vpc\s+brief', c):
        return vpc_engine.format_show_vpc_brief(device_id)
    if re.match(r'^show\s+vpc$', c):
        return vpc_engine.format_show_vpc(device_id)

    # "vlan 10" → VLANデータベース作成
    m_vlan_db = re.match(r'^vlan\s+(\d+)$', c)
    if m_vlan_db:
        vid = int(m_vlan_db.group(1))
        vlan_engine.create_vlan(device_id, vid)
        state.vlans.setdefault(vid, {'name': f'VLAN{vid:04d}', 'status': 'active', 'ports': []})
        return
    # "vlan <id> name <name>"
    m_vlan_name = re.match(r'^vlan\s+(\d+)\s+name\s+(\S+)', orig)
    if m_vlan_name:
        vid, name = int(m_vlan_name.group(1)), m_vlan_name.group(2)
        vlan_engine.create_vlan(device_id, vid, name)
        state.vlans.setdefault(vid, {})['name'] = name
        return
    # "name <name>" (vlanコンフィグモード内)
    m_name = re.match(r'^name\s+(\S+)', orig)
    if m_name and state.mode == 'config-vlan':
        # 直前のvlan idを取得
        vid = getattr(state, '_current_vlan', None)
        if vid:
            vlan_engine.create_vlan(device_id, vid, m_name.group(1))
        return
    # "no vlan <id>"
    m_no_vlan = re.match(r'^no\s+vlan\s+(\d+)', c)
    if m_no_vlan:
        vid = int(m_no_vlan.group(1))
        vlan_engine.delete_vlan(device_id, vid)
        state.vlans.pop(vid, None)
        return

    # ── switchport 設定 ──
    iface = getattr(state, 'current_if', '') or ''
    # "switchport mode access"
    if re.match(r'^switchport\s+mode\s+access', c):
        if iface:
            p = vlan_engine._get_or_create_port(device_id, iface)
            p.mode = 'access'
        return
    # "switchport mode trunk"
    if re.match(r'^switchport\s+mode\s+trunk', c):
        if iface:
            vlan_engine.set_trunk(device_id, iface)
        return
    # "switchport access vlan <id>"
    m_sw_acc = re.match(r'^switchport\s+access\s+vlan\s+(\d+)', c)
    if m_sw_acc:
        vid = int(m_sw_acc.group(1))
        if iface:
            vlan_engine.set_access(device_id, iface, vid)
        return
    # "switchport trunk native vlan <id>"
    m_native = re.match(r'^switchport\s+trunk\s+native\s+vlan\s+(\d+)', c)
    if m_native:
        if iface:
            vlan_engine.set_native_vlan(device_id, iface, int(m_native.group(1)))
        return
    # "switchport trunk allowed vlan <list>"
    m_allowed = re.match(r'^switchport\s+trunk\s+allowed\s+vlan\s+(add\s+|remove\s+|except\s+)?(.+)', c)
    if m_allowed:
        op = (m_allowed.group(1) or '').strip()
        vlan_str = m_allowed.group(2).strip()
        if iface:
            if op == 'add':
                vlan_engine.set_trunk_allowed_add(device_id, iface, vlan_str)
            elif op == 'remove':
                vlan_engine.set_trunk_allowed_remove(device_id, iface, vlan_str)
            else:
                vlan_engine.set_trunk_allowed(device_id, iface, vlan_str)
        return

    # ── VRRP 設定（Cisco/Catalyst/Si-R/APRESIA）──
    # Cisco/Catalyst: "vrrp 1 ip 192.168.1.254"
    vrrp_ip = re.match(r'^vrrp\s+(\d+)\s+ip\s+([\d.]+)', c)
    if vrrp_ip:
        gid = int(vrrp_ip.group(1))
        vip = vrrp_ip.group(2)
        existing = vrrp_engine.vrrp.get(device_id, {}).get(gid)
        pri = existing.priority if existing else 100
        await vrrp_engine.vrrp_start(device_id, gid, vip, pri,
                                      interface=getattr(state,'current_if',''))
        # 対向装置と自動ネゴシエーション
        for peer_id in vnet.get_neighbors(device_id):
            peer_g = vrrp_engine.vrrp.get(peer_id, {}).get(gid)
            if peer_g:
                await vrrp_engine.vrrp_receive_advert(device_id, {
                    'type':'vrrp_advert','src_id':peer_id,'group_id':gid,
                    'vip':peer_g.vip,'priority':peer_g.priority,
                    'preempt':peer_g.preempt,'state':peer_g.state,
                })
        return
    # "vrrp 1 priority 110"
    vrrp_pri = re.match(r'^vrrp\s+(\d+)\s+priority\s+(\d+)', c)
    if vrrp_pri:
        gid, pri = int(vrrp_pri.group(1)), int(vrrp_pri.group(2))
        g = vrrp_engine.vrrp.get(device_id, {}).get(gid)
        if g:
            g.priority = pri
            # priority変更後に再選出
            for peer_id in vnet.get_neighbors(device_id):
                peer_g = vrrp_engine.vrrp.get(peer_id, {}).get(gid)
                if peer_g:
                    await vrrp_engine.vrrp_receive_advert(device_id, {
                        'type':'vrrp_advert','src_id':peer_id,'group_id':gid,
                        'vip':peer_g.vip,'priority':peer_g.priority,
                        'preempt':peer_g.preempt,'state':peer_g.state,
                    })
        return
    # "vrrp 1 preempt"
    if re.match(r'^vrrp\s+(\d+)\s+preempt', c):
        m = re.match(r'^vrrp\s+(\d+)\s+preempt', c)
        g = vrrp_engine.vrrp.get(device_id, {}).get(int(m.group(1)))
        if g: g.preempt = True
        return
    # "no vrrp 1 preempt"
    if re.match(r'^no\s+vrrp\s+(\d+)\s+preempt', c):
        m = re.match(r'^no\s+vrrp\s+(\d+)\s+preempt', c)
        g = vrrp_engine.vrrp.get(device_id, {}).get(int(m.group(1)))
        if g: g.preempt = False
        return

    # Si-R形式: "vrrp use on" / "lan 0 vrrp group 1 id 1 110 192.168.1.254"
    if re.match(r'^vrrp\s+use\s+on', c):
        return  # 有効化フラグ（次のコマンドで実設定）
    sir_vrrp = re.match(r'^lan\s+(\d+)\s+vrrp\s+group\s+(\d+)\s+id\s+(\d+)\s+(\d+)\s+([\d.]+)', c)
    if sir_vrrp:
        gid, vrid, pri, vip = (int(sir_vrrp.group(2)), int(sir_vrrp.group(3)),
                                int(sir_vrrp.group(4)), sir_vrrp.group(5))
        await vrrp_engine.vrrp_start(device_id, gid, vip, pri,
                                      interface=f'lan{sir_vrrp.group(1)}')
        for peer_id in vnet.get_neighbors(device_id):
            peer_g = vrrp_engine.vrrp.get(peer_id, {}).get(gid)
            if peer_g:
                await vrrp_engine.vrrp_receive_advert(device_id, {
                    'type':'vrrp_advert','src_id':peer_id,'group_id':gid,
                    'vip':peer_g.vip,'priority':peer_g.priority,
                    'preempt':peer_g.preempt,'state':peer_g.state,
                })
        return

    # ── HSRP 設定（Cisco/Catalyst専用）──
    # "standby 1 ip 192.168.1.254"
    hsrp_ip = re.match(r'^standby\s+(\d+)\s+ip\s+([\d.]+)', c)
    if hsrp_ip:
        gid, vip = int(hsrp_ip.group(1)), hsrp_ip.group(2)
        existing = vrrp_engine.hsrp.get(device_id, {}).get(gid)
        pri = existing.priority if existing else 100
        pre = existing.preempt if existing else False
        await vrrp_engine.hsrp_start(device_id, gid, vip, pri, pre,
                                      interface=getattr(state,'current_if',''))
        for peer_id in vnet.get_neighbors(device_id):
            peer_g = vrrp_engine.hsrp.get(peer_id, {}).get(gid)
            if peer_g:
                await vrrp_engine.hsrp_receive_hello(device_id, {
                    'type':'hsrp_hello','src_id':peer_id,'group_id':gid,
                    'vip':peer_g.vip,'priority':peer_g.priority,
                    'preempt':peer_g.preempt,'state':peer_g.state,
                })
        return
    # "standby 1 priority 110"
    hsrp_pri = re.match(r'^standby\s+(\d+)\s+priority\s+(\d+)', c)
    if hsrp_pri:
        gid, pri = int(hsrp_pri.group(1)), int(hsrp_pri.group(2))
        g = vrrp_engine.hsrp.get(device_id, {}).get(gid)
        if g:
            g.priority = pri
            for peer_id in vnet.get_neighbors(device_id):
                peer_g = vrrp_engine.hsrp.get(peer_id, {}).get(gid)
                if peer_g:
                    await vrrp_engine.hsrp_receive_hello(device_id, {
                        'type':'hsrp_hello','src_id':peer_id,'group_id':gid,
                        'vip':peer_g.vip,'priority':peer_g.priority,
                        'preempt':peer_g.preempt,'state':peer_g.state,
                    })
        return
    # "standby 1 preempt"
    if re.match(r'^standby\s+(\d+)\s+preempt', c):
        m = re.match(r'^standby\s+(\d+)\s+preempt', c)
        g = vrrp_engine.hsrp.get(device_id, {}).get(int(m.group(1)))
        if g: g.preempt = True
        return

    # ── VRRP/HSRP フェイルオーバーシミュレーション ──
    # "vrrp failover <group>" — 障害シミュレート
    vrrp_fail = re.match(r'^vrrp\s+failover\s+(\d+)', c)
    if vrrp_fail:
        await vrrp_engine.vrrp_failover(device_id, int(vrrp_fail.group(1)))
        return
    hsrp_fail = re.match(r'^standby\s+failover\s+(\d+)', c)
    if hsrp_fail:
        await vrrp_engine.hsrp_failover(device_id, int(hsrp_fail.group(1)))
        return

    # ── ARP 静的エントリ / クリア ──
    arp_static = re.match(r'^arp\s+([\d.]+)\s+([0-9a-fA-F.:]+)', c)
    if arp_static:
        ip = arp_static.group(1)
        mac = arp_static.group(2)
        arp_engine.add_static(device_id, ip, mac)
        return
    if re.match(r'^clear\s+(ip\s+)?arp', c):
        arp_engine.clear(device_id)
        return

    # ── IPフィルタ / ACL ──
    # Cisco標準ACL: "access-list 10 permit 192.168.1.0 0.0.0.255"
    # Cisco標準ACL: "access-list 10 deny host 10.0.0.5"
    std_acl = re.match(
        r'^access-list\s+(\d+)\s+(permit|deny)\s+'
        r'(?:(host)\s+([\d.]+)|([\d.]+)\s+([\d.]+)|(any))', orig, re.I)
    if std_acl:
        num = std_acl.group(1)
        action = std_acl.group(2).lower()
        if std_acl.group(3):  # host X
            src = f'host {std_acl.group(4)}'
        elif std_acl.group(5):  # X wildcard
            # ワイルドカードマスクをprefixに変換
            src = _wildcard_to_cidr(std_acl.group(5), std_acl.group(6))
        else:  # any
            src = 'any'
        ipfilter_engine.add_rule(device_id, num, action, 'ip', src, 'any')
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'acl_log',
                    'message': f'access-list {num}: {action} {src}'})
        return
    # Cisco拡張ACL: "access-list 100 permit tcp any host 10.0.0.1 eq 80"
    ext_acl = re.match(
        r'^access-list\s+(\d+)\s+(permit|deny)\s+(ip|tcp|udp|icmp)\s+'
        r'(\S+(?:\s+[\d.]+)?)\s+(\S+(?:\s+[\d.]+)?)(?:\s+eq\s+(\S+))?', orig, re.I)
    if ext_acl and int(ext_acl.group(1)) >= 100:
        num = ext_acl.group(1)
        action = ext_acl.group(2).lower()
        proto = ext_acl.group(3).lower()
        src = ext_acl.group(4)
        dst = ext_acl.group(5)
        port = ext_acl.group(6)
        ipfilter_engine.add_rule(device_id, num, action, proto, src, dst,
                                 dst_port=port)
        return
    # Si-R形式 ACL: "acl NAME permit ip src X/Y dst Z/W"
    sir_acl = re.match(
        r'^acl\s+(\S+)\s+(permit|deny)\s+(ip|tcp|udp|icmp)\s+'
        r'(?:src\s+)?(\S+)\s+(?:dst\s+)?(\S+)(?:\s+port\s+(\S+))?', orig, re.I)
    if sir_acl:
        name = sir_acl.group(1)
        action = sir_acl.group(2).lower()
        proto = sir_acl.group(3).lower()
        src = sir_acl.group(4)
        dst = sir_acl.group(5)
        port = sir_acl.group(6)
        ipfilter_engine.add_rule(device_id, name, action, proto, src, dst,
                                 dst_port=port)
        return
    # ACL適用: Cisco "ip access-group 10 in" / Si-R "lan 0 acl NAME in"
    acl_apply = re.match(r'^ip\s+access-group\s+(\S+)\s+(in|out)', c)
    if acl_apply:
        ipfilter_engine.apply_acl(device_id, 'lan0', acl_apply.group(2),
                                  acl_apply.group(1))
        return
    sir_acl_apply = re.match(r'^lan\s+(\d+)\s+acl\s+(\S+)\s+(in|out)', orig, re.I)
    if sir_acl_apply:
        ipfilter_engine.apply_acl(device_id, f'lan{sir_acl_apply.group(1)}',
                                  sir_acl_apply.group(3), sir_acl_apply.group(2))
        return

    # ── NTP サーバー設定 ──
    # "ntp server 192.168.1.1" (Cisco/Catalyst)
    ntp_srv = re.match(r'^ntp\s+server\s+([\d.]+)', c)
    if ntp_srv:
        ip = ntp_srv.group(1)
        if not hasattr(state, 'ntp_servers'):
            state.ntp_servers = []
        if not any(s['host'] == ip for s in state.ntp_servers):
            state.ntp_servers.append({'host': ip, 'port': 123})
        ntp_client.register(device_id, state.ntp_servers)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'ntp_cfg',
                    'message': f'NTPサーバーを設定: {ip}'})
        return
    # "ntp use on" / "ntp server X.X.X.X" (Si-R形式)
    sir_ntp = re.match(r'^ntp\s+server\s+([\d.]+)', c)
    if sir_ntp and state.device_type in ('sir','srs'):
        ip = sir_ntp.group(1)
        if not hasattr(state, 'ntp_servers'):
            state.ntp_servers = []
        if not any(s['host'] == ip for s in state.ntp_servers):
            state.ntp_servers.append({'host': ip, 'port': 123})
        ntp_client.register(device_id, state.ntp_servers)
        return

    # ── syslog / logging 設定（全機種対応）──
    # Cisco/Catalyst: "logging host 192.168.1.100"
    # Cisco/Catalyst: "logging trap informational"
    # Cisco/Catalyst: "logging facility local7"
    log_host = re.match(r'^logging\s+(?:host|server)\s+([\d.]+)(?:\s+(\d+))?', c)
    if log_host:
        ip = log_host.group(1)
        port = int(log_host.group(2)) if log_host.group(2) else 514
        existing = next((s for s in state.syslog_servers if s['host'] == ip), None)
        if not existing:
            state.syslog_servers.append({'host': ip, 'port': port,
                                         'facility': 'local7', 'level': state.logging_level})
        syslog_dispatcher.register(device_id, state.syslog_servers)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'syslog_cfg', 'message': f'syslog送信先を設定: {ip}:{port}'})
        return

    # APRESIA: "config syslog <ip>"
    ap_syslog = re.match(r'^config\s+syslog\s+([\d.]+)(?:\s+(\d+))?', c)
    if ap_syslog:
        ip = ap_syslog.group(1)
        port = int(ap_syslog.group(2)) if ap_syslog.group(2) else 514
        if not any(s['host'] == ip for s in state.syslog_servers):
            state.syslog_servers.append({'host': ip, 'port': port,
                                         'facility': 'local7', 'level': 'informational'})
        syslog_dispatcher.register(device_id, state.syslog_servers)
        return
        ip = log_host.group(1)
        port = int(log_host.group(2)) if log_host.group(2) else 514
        existing = next((s for s in state.syslog_servers if s['host'] == ip), None)
        if not existing:
            state.syslog_servers.append({'host': ip, 'port': port,
                                         'facility': 'local7', 'level': state.logging_level})
        # syslog_dispatcherにも即時登録
        syslog_dispatcher.register(device_id, state.syslog_servers)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'syslog_cfg', 'message': f'syslog送信先を設定: {ip}:{port}'})
        return
    log_trap = re.match(r'^logging\s+trap\s+(\S+)', c)
    if log_trap:
        state.logging_level = log_trap.group(1)
        for s in state.syslog_servers:
            s['level'] = log_trap.group(1)
        return
    log_fac = re.match(r'^logging\s+facility\s+(\S+)', c)
    if log_fac:
        for s in state.syslog_servers:
            s['facility'] = log_fac.group(1)
        return
    no_log_host = re.match(r'^no\s+logging\s+host\s+([\d.]+)', c)
    if no_log_host:
        state.syslog_servers = [s for s in state.syslog_servers
                                 if s['host'] != no_log_host.group(1)]
        return

    # Si-R: "syslog server 192.168.1.100" / "syslog host ..."
    sir_syslog = re.match(r'^syslog\s+(?:server|host)\s+([\d.]+)(?:\s+(\d+))?', c)
    if sir_syslog:
        ip = sir_syslog.group(1)
        port = int(sir_syslog.group(2)) if sir_syslog.group(2) else 514
        if not any(s['host'] == ip for s in state.syslog_servers):
            state.syslog_servers.append({'host': ip, 'port': port,
                                         'facility': 'local7', 'level': 'informational'})
        syslog_dispatcher.register(device_id, state.syslog_servers)
        return
    # APRESIA: "logging host ..." はCisco同様なので上で対応済み

    # ── SNMP 設定（全機種対応）──
    # Cisco: "snmp-server community public ro"
    snmp_comm = re.match(r'^snmp-server\s+community\s+(\S+)\s+(ro|rw|read-only|read-write)', c)
    if snmp_comm:
        name, perm = snmp_comm.group(1), snmp_comm.group(2)
        perm = 'ro' if perm in ('ro', 'read-only') else 'rw'
        if not any(c_['name'] == name for c_ in state.snmp_community):
            state.snmp_community.append({'name': name, 'perm': perm})
        return
    # Cisco: "snmp-server host 192.168.1.200 traps public"
    snmp_host = re.match(r'^snmp-server\s+host\s+([\d.]+)'
                         r'(?:\s+(traps?|informs?))?\s*(?:version\s+(1|2c|3)\s+)?(\S+)?', c)
    if snmp_host:
        ip = snmp_host.group(1)
        trap_type = snmp_host.group(2) or 'traps'
        version = snmp_host.group(3) or '2c'
        community = snmp_host.group(4) or 'public'
        if not any(h['host'] == ip for h in state.snmp_hosts):
            state.snmp_hosts.append({'host': ip, 'community': community,
                                     'version': version, 'traps': trap_type})
        # snmp_dispatcherに即時登録
        snmp_dispatcher.register(device_id, state.snmp_hosts)
        buf = proto_log_buffer.setdefault(device_id, [])
        buf.append({'type': 'snmp_cfg',
                    'message': f'SNMP trap送信先を設定: {ip} (community={community} v{version})'})
        return
    # Cisco: "snmp-server location ..."
    snmp_loc = re.match(r'^snmp-server\s+location\s+(.+)', orig)
    if snmp_loc:
        state.snmp_location = snmp_loc.group(1)
        return
    # Cisco: "snmp-server contact ..."
    snmp_con = re.match(r'^snmp-server\s+contact\s+(.+)', orig)
    if snmp_con:
        state.snmp_contact = snmp_con.group(1)
        return
    # Si-R: "snmp trap host 192.168.1.200 community public"
    sir_snmp_trap = re.match(r'^snmp\s+trap\s+host\s+([\d.]+)'
                              r'(?:\s+community\s+(\S+))?', c)
    if sir_snmp_trap:
        ip = sir_snmp_trap.group(1)
        community = sir_snmp_trap.group(2) or 'public'
        if not any(h['host'] == ip for h in state.snmp_hosts):
            state.snmp_hosts.append({'host': ip, 'community': community,
                                     'version': '2c', 'traps': 'traps'})
        return
    # Si-R: "snmp community public ro"
    sir_snmp_comm = re.match(r'^snmp\s+community\s+(\S+)\s+(ro|rw)', c)
    if sir_snmp_comm:
        name, perm = sir_snmp_comm.group(1), sir_snmp_comm.group(2)
        if not any(c_['name'] == name for c_ in state.snmp_community):
            state.snmp_community.append({'name': name, 'perm': perm})
        return
    # no snmp-server host
    no_snmp = re.match(r'^no\s+snmp-server\s+host\s+([\d.]+)', c)
    if no_snmp:
        state.snmp_hosts = [h for h in state.snmp_hosts if h['host'] != no_snmp.group(1)]
        return

    # ── 再配信 (redistribute) ──
    # "redistribute rip" / "redistribute static" / "redistribute connected"
    # / "redistribute ospf" / "redistribute bgp 65001" / "redistribute ospf 1"
    redist = re.match(
        r'^redistribute\s+(rip|ospf|bgp|static|connected)(?:\s+\d+)?'
        r'(?:\s+metric\s+(\d+))?', c)
    if redist:
        source = redist.group(1)
        metric = int(redist.group(2)) if redist.group(2) else None
        # 現在の設定モードが再配信先
        target = getattr(state, '_routing_mode', None)
        if target in ('rip', 'ospf', 'bgp'):
            count = await redistribute(device_id, target, source, metric)
            buf = proto_log_buffer.setdefault(device_id, [])
            buf.append({'type': 'redist_log',
                        'message': f'再配信: {source} → {target} '
                                   f'({count}経路を{target.upper()}に注入)'})
        return

    # ── スタティックルート / フローティングスタティック ──
    # Cisco: "ip route 0.0.0.0 0.0.0.0 192.168.1.1 [200]"
    # Cisco: "ip route 192.168.2.0 255.255.255.0 10.0.0.2"
    cisco_route = re.match(
        r'^ip\s+route\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+(\d+))?', c)
    if cisco_route:
        net = cisco_route.group(1)
        mask = cisco_route.group(2)
        next_hop = cisco_route.group(3)
        ad = int(cisco_route.group(4)) if cisco_route.group(4) else 1
        prefix = _mask_to_prefix(mask)
        rib_engine.add_static_route(device_id, hostname, net, prefix, next_hop, ad)
        await _emit_route_log(device_id, net, prefix, next_hop, ad)
        return
    # Cisco省略形 / Si-R: "ip route 0.0.0.0/0 192.168.1.1 [200]"
    sir_route = re.match(
        r'^ip\s+route\s+([\d.]+)/(\d+)\s+([\d.]+)(?:\s+(\d+))?', c)
    if sir_route:
        net = sir_route.group(1)
        prefix = int(sir_route.group(2))
        next_hop = sir_route.group(3)
        ad = int(sir_route.group(4)) if sir_route.group(4) else 1
        rib_engine.add_static_route(device_id, hostname, net, prefix, next_hop, ad)
        await _emit_route_log(device_id, net, prefix, next_hop, ad)
        return
    # Si-R: "lan <n> ip route <dst>/<mask> <gw> <distance>"
    sir_lan_route = re.match(
        r'^lan\s+\d+\s+ip\s+route\s+([\d.]+)/(\d+)\s+([\d.]+)(?:\s+(\d+))?', c)
    if sir_lan_route:
        net = sir_lan_route.group(1)
        prefix = int(sir_lan_route.group(2))
        next_hop = sir_lan_route.group(3)
        ad = int(sir_lan_route.group(4)) if sir_lan_route.group(4) else 1
        rib_engine.add_static_route(device_id, hostname, net, prefix, next_hop, ad)
        await _emit_route_log(device_id, net, prefix, next_hop, ad)
        return
    # ルート削除 "no ip route ..."
    no_route = re.match(r'^no\s+ip\s+route\s+([\d.]+)(?:/(\d+)|\s+([\d.]+))', c)
    if no_route:
        net = no_route.group(1)
        if no_route.group(2):
            prefix = int(no_route.group(2))
        elif no_route.group(3):
            prefix = _mask_to_prefix(no_route.group(3))
        else:
            prefix = 24
        rib_engine.remove_static_route(device_id, net, prefix)
        return

    # ── RIP ──
    # Si-R: "ip rip use use" / Cisco: "router rip"
    if re.match(r'^ip\s+rip\s+use\s+(on|use)', c) or c == 'router rip':
        state._rip_pending = True
        state._routing_mode = 'rip'
        if not hasattr(state, '_rip_networks'):
            state._rip_networks = []
        state._bgp_pending = False  # 他プロトコルのpendingをクリア
        return
    # "ip rip network 192.168.1.0/24" or "network 192.168.1.0"
    rip_net = re.match(r'^(?:ip\s+rip\s+network|network)\s+([\d.]+(?:/\d+)?)', c)
    if rip_net and (getattr(state, '_rip_pending', False) or
                    'rip' in getattr(state, '_routing_mode', '')):
        net = rip_net.group(1)
        if '/' not in net:
            net += '/24'
        if not hasattr(state, '_rip_networks'):
            state._rip_networks = []
        if net not in state._rip_networks:
            state._rip_networks.append(net)
        await rip_engine.start(device_id, hostname, state._rip_networks)
        return

    # ── OSPF ──
    ospf_m = re.match(r'^router\s+ospf\s+(\d+)', c)
    if ospf_m:
        state._routing_mode = 'ospf'
        state._ospf_process = int(ospf_m.group(1))
        state._ospf_networks = getattr(state, '_ospf_networks', [])
        # RIP/BGP の pending フラグをクリア（モード切替）
        state._rip_pending = False
        state._bgp_pending = False
        # ★ returnしない → RuleEngineがmode='config-router'に遷移する
    # Si-R: "ospf use on" / "ospf area <area>"
    if re.match(r'^ospf\s+use\s+on', c):
        state._routing_mode = 'ospf'
        state._ospf_process = getattr(state, '_ospf_process', 1)
        state._ospf_networks = getattr(state, '_ospf_networks', [])
        state._ospf_pending = True
        return
    # Si-R: "ospf area 0 ..." エリア定義
    sir_ospf_area = re.match(r'^ospf\s+area\s+(\S+)', c)
    if sir_ospf_area and getattr(state, '_ospf_pending', False):
        state._ospf_area = sir_ospf_area.group(1)
        return
    # Si-R: "lan <n> ip ospf use on" → インタフェースをOSPFに参加
    sir_lan_ospf = re.match(r'^lan\s+(\d+)\s+ip\s+ospf\s+use\s+on', c)
    if sir_lan_ospf:
        state._routing_mode = 'ospf'
        # lan0のIPからネットワークを推定
        iface = f'lan{sir_lan_ospf.group(1)}'
        ip = state.interfaces.get(iface, {}).get('ip', '192.168.1.1')
        prefix = state.interfaces.get(iface, {}).get('prefix', 24)
        # ネットワークアドレス算出
        net = _network_address(ip, prefix)
        if not hasattr(state, '_ospf_networks'):
            state._ospf_networks = []
        if net not in state._ospf_networks:
            state._ospf_networks.append(net)
        await ospf_engine.start(device_id, hostname,
                                 getattr(state, '_ospf_process', 1),
                                 state._ospf_networks,
                                 getattr(state, '_ospf_area', '0.0.0.0'))
        return
    # "network 10.0.0.0 0.0.0.255 area 0" (Cisco IOS形式)
    ospf_net = re.match(r'^network\s+([\d.]+)\s+([\d.]+)\s+area\s+(\S+)', c)
    if ospf_net and getattr(state, '_routing_mode', '') == 'ospf':
        net_ip = ospf_net.group(1)
        wildcard = ospf_net.group(2)
        area = ospf_net.group(3)
        state._ospf_area = area
        # ワイルドカードマスクからネットワークを算出
        import struct
        wc_int = struct.unpack('!I', bytes(int(x) for x in wildcard.split('.')))[0]
        mask_int = (~wc_int) & 0xffffffff
        prefix = bin(mask_int).count('1')
        net = _network_address(net_ip, prefix)
        # _network_addressは既に"net/prefix"形式で返す → そのまま使う
        net_str = net  # 例: '10.0.23.0/30'
        if not hasattr(state, '_ospf_networks'):
            state._ospf_networks = []
        if net_str not in state._ospf_networks:
            state._ospf_networks.append(net_str)
        # 即座にOSPFエンジンを起動・更新
        await ospf_engine.start(device_id, hostname,
                                 getattr(state, '_ospf_process', 1),
                                 state._ospf_networks,
                                 state._ospf_area)
        return
        net_ip = ospf_net.group(1)
        wildcard = ospf_net.group(2)
        area = ospf_net.group(3)
        # ワイルドカード→プレフィックス変換
        prefix = _wildcard_to_prefix(wildcard)
        net = f'{net_ip}/{prefix}'
        if not hasattr(state, '_ospf_networks'):
            state._ospf_networks = []
        if net not in state._ospf_networks:
            state._ospf_networks.append(net)
        # マルチエリア対応：area_networksに登録
        ospf_engine.add_network(device_id, net, area)
        await ospf_engine.start(device_id, hostname,
                                 getattr(state, '_ospf_process', 1),
                                 state._ospf_networks, area)
        return
    # "ip ospf priority <n>" / "ospf priority <n>" — DR選出優先度
    ospf_pri = re.match(r'^(?:ip\s+)?ospf\s+priority\s+(\d+)', c)
    if ospf_pri:
        ospf_engine.set_priority(device_id, int(ospf_pri.group(1)))
        return
    # "ip ospf cost <n>" — インタフェースコスト
    ospf_cost = re.match(r'^(?:ip\s+)?ospf\s+cost\s+(\d+)', c)
    if ospf_cost:
        ospf_engine.set_cost(device_id, int(ospf_cost.group(1)))
        return
    # "auto-cost reference-bandwidth <n>" — ref-bwでコスト自動計算
    auto_cost = re.match(r'^auto-cost\s+reference-bandwidth\s+(\d+)', c)
    if auto_cost and getattr(state, '_routing_mode', '') == 'ospf':
        ref_bw = int(auto_cost.group(1))
        # デフォルト1Gbps想定でコスト計算
        cost = max(1, ref_bw // 1000)
        ospf_engine.set_cost(device_id, cost)
        return
    # "passive-interface <iface>" / "passive-interface default"
    passive = re.match(r'^passive-interface\s+(\S+)', c)
    if passive and getattr(state, '_routing_mode', '') == 'ospf':
        iface = passive.group(1)
        if iface == 'default':
            ospf_engine.add_passive_interface(device_id, 'all')
        else:
            ospf_engine.add_passive_interface(device_id, iface)
        return
    # "no passive-interface <iface>"
    no_passive = re.match(r'^no\s+passive-interface\s+(\S+)', c)
    if no_passive and getattr(state, '_routing_mode', '') == 'ospf':
        ospf_engine.remove_passive_interface(device_id, no_passive.group(1))
        return

    # ── BGP ──
    bgp_m = re.match(r'^router\s+bgp\s+(\d+)', c)
    if bgp_m:
        state._routing_mode = 'bgp'
        state._bgp_as = int(bgp_m.group(1))
        await bgp_engine.start(device_id, hostname, state._bgp_as)
        return
    # Si-R: "bgp use on"
    if re.match(r'^bgp\s+use\s+on', c):
        state._routing_mode = 'bgp'
        state._bgp_pending = True
        return
    # Si-R: "bgp as <as>" 自AS番号
    sir_bgp_as = re.match(r'^bgp\s+as\s+(\d+)', c)
    if sir_bgp_as:
        state._routing_mode = 'bgp'
        state._bgp_as = int(sir_bgp_as.group(1))
        await bgp_engine.start(device_id, hostname, state._bgp_as)
        return
    # Si-R: "bgp neighbor <n> address <ip> remote-as <as>"
    sir_bgp_nbr = re.match(r'^bgp\s+neighbor\s+\d+\s+(?:address\s+)?(\S+)\s+remote-as\s+(\d+)', c)
    if sir_bgp_nbr and getattr(state, '_routing_mode', '') == 'bgp':
        remote_as = int(sir_bgp_nbr.group(2))
        peer_id = find_peer_by_link(device_id)
        if peer_id:
            peer_state = device_sessions.get(peer_id)
            peer_hostname = peer_state.hostname if peer_state else peer_id
            await bgp_engine.add_neighbor(device_id, peer_id, peer_hostname, remote_as)
        return
    # "neighbor 10.1.0.2 remote-as 65002" (Cisco IOS形式)
    bgp_nbr = re.match(r'^neighbor\s+([\d.]+)\s+remote-as\s+(\d+)', c)
    if bgp_nbr and getattr(state, '_routing_mode', '') == 'bgp':
        neighbor_ip = bgp_nbr.group(1)
        remote_as = int(bgp_nbr.group(2))
        # IPアドレスでピアを特定（複数リンクがある場合も正確に）
        peer_id = _find_peer_by_ip(device_id, neighbor_ip)
        if not peer_id:
            peer_id = find_peer_by_link(device_id)
        if peer_id:
            peer_state = device_sessions.get(peer_id)
            peer_hostname = peer_state.hostname if peer_state else peer_id
            await bgp_engine.add_neighbor(device_id, peer_id, peer_hostname, remote_as)
        return
    # Si-R: "bgp network <ip>/<prefix>" 広告
    sir_bgp_net = re.match(r'^bgp\s+network\s+([\d.]+(?:/\d+)?)', c)
    if sir_bgp_net and getattr(state, '_routing_mode', '') == 'bgp':
        net = sir_bgp_net.group(1)
        if '/' not in net:
            net += '/24'
        await bgp_engine.advertise_network(device_id, net)
        return
    # "network 192.168.1.0 mask 255.255.255.0" (Cisco IOS形式)
    bgp_net = re.match(r'^network\s+([\d.]+)(?:\s+mask\s+[\d.]+)?', c)
    if bgp_net and getattr(state, '_routing_mode', '') == 'bgp':
        net = bgp_net.group(1)
        if '/' not in net:
            net += '/24'
        await bgp_engine.advertise_network(device_id, net)
        return

    # ── STP/RSTP ──
    # Cisco: "spanning-tree mode rstp"
    stp_mode = re.match(r'^spanning-tree\s+mode\s+(rapid-pvst|pvst|rstp|mst|stp)', c)
    if stp_mode:
        mode = 'rstp' if stp_mode.group(1) in ('rapid-pvst', 'rstp') else 'stp'
        pri = getattr(state, '_stp_priority', 32768)
        await stp_engine.start(device_id, hostname, mode, pri)
        return
    # Si-R: "stp mode stp" / "stp mode rstp"
    sir_stp_mode = re.match(r'^stp\s+mode\s+(stp|rstp)', c)
    if sir_stp_mode:
        mode = sir_stp_mode.group(1)
        pri = getattr(state, '_stp_priority', 32768)
        await stp_engine.start(device_id, hostname, mode, pri)
        return
    # Cisco: "spanning-tree vlan <vlan-id> priority <priority>"
    stp_vlan_pri = re.match(r'^spanning-tree\s+vlan\s+(\d+)\s+priority\s+(\d+)', c)
    if stp_vlan_pri:
        vlan_id = int(stp_vlan_pri.group(1))
        priority = int(stp_vlan_pri.group(2))
        stp_engine.set_vlan_priority(device_id, vlan_id, priority)
        return
    # Cisco: "spanning-tree vlan <vlan-id>" (VLANのSTP起動)
    stp_vlan = re.match(r'^spanning-tree\s+vlan\s+(\d+)$', c)
    if stp_vlan:
        vlan_id = int(stp_vlan.group(1))
        pri = getattr(state, '_stp_priority', 32768)
        n = stp_engine.nodes.get(device_id)
        mode = n['mode'] if n else 'rstp'
        await stp_engine.start(device_id, hostname, mode, pri, vlan_id)
        return
    # Cisco: "spanning-tree priority <n>" (グローバル)
    stp_pri = re.match(r'^spanning-tree\s+priority\s+(\d+)', c)
    if stp_pri:
        state._stp_priority = int(stp_pri.group(1))
        n = stp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            await stp_engine.start(device_id, hostname, n['mode'], state._stp_priority)
        return
    # "spanning-tree guard root" (インターフェースモード) — Root Guard
    if re.match(r'^spanning-tree\s+guard\s+root', c):
        iface = getattr(state, 'current_if', '') or ''
        if iface:
            stp_engine.port_config.setdefault(device_id, {})
            stp_engine.port_config[device_id].setdefault(iface, {})
            stp_engine.port_config[device_id][iface]['root_guard'] = True
            n = stp_engine.nodes.get(device_id)
            if n and iface in n.get('ports', {}):
                n['ports'][iface]['root_guard'] = True
        return
    if re.match(r'^no\s+spanning-tree\s+guard\s+root', c):
        iface = getattr(state, 'current_if', '') or ''
        if iface:
            cfg = stp_engine.port_config.get(device_id, {}).get(iface, {})
            cfg['root_guard'] = False
            n = stp_engine.nodes.get(device_id)
            if n and iface in n.get('ports', {}):
                n['ports'][iface]['root_guard'] = False
        return

    # ── PortFast ──
    # "spanning-tree portfast" (インターフェースモード)
    if re.match(r'^spanning-tree\s+portfast$', c):
        iface = getattr(state, 'current_if', '') or ''
        if iface:
            stp_engine.set_portfast(device_id, iface, True)
        return
    # "spanning-tree portfast default" (グローバル)
    if re.match(r'^spanning-tree\s+portfast\s+default', c):
        stp_engine.set_portfast_default(device_id, True)
        return
    if re.match(r'^no\s+spanning-tree\s+portfast\s+default', c):
        stp_engine.set_portfast_default(device_id, False)
        return
    # ── BPDUGuard ──
    # "spanning-tree bpduguard enable" (インターフェースモード)
    if re.match(r'^spanning-tree\s+bpduguard\s+enable', c):
        iface = getattr(state, 'current_if', '') or ''
        if iface:
            stp_engine.set_bpduguard(device_id, iface, True)
        return
    if re.match(r'^spanning-tree\s+bpduguard\s+disable', c):
        iface = getattr(state, 'current_if', '') or ''
        if iface:
            stp_engine.set_bpduguard(device_id, iface, False)
        return
    # "spanning-tree portfast bpduguard default" (グローバル)
    if re.match(r'^spanning-tree\s+portfast\s+bpduguard\s+default', c):
        stp_engine.set_bpduguard_default(device_id, True)
        return
    if re.match(r'^no\s+spanning-tree\s+portfast\s+bpduguard\s+default', c):
        stp_engine.set_bpduguard_default(device_id, False)
        return
    # Si-R: "stp domain priority <n>"
    sir_stp_pri = re.match(r'^stp\s+domain\s+priority\s+(\d+)', c)
    if sir_stp_pri:
        state._stp_priority = int(sir_stp_pri.group(1))
        n = stp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            await stp_engine.start(device_id, hostname, n['mode'], state._stp_priority)
        return
    # Si-R: "ether <n> stp use on"
    sir_ether_stp = re.match(r'^ether\s+\d+\s+stp\s+use\s+on', c)
    if sir_ether_stp:
        n = stp_engine.nodes.get(device_id)
        if not (n and n.get('enabled')):
            await stp_engine.start(device_id, hostname, 'rstp',
                                    getattr(state, '_stp_priority', 32768))
        return
    # "stp use on" / "spanning-tree" 単独
    if re.match(r'^(stp\s+use\s+on|spanning-tree)$', c):
        await stp_engine.start(device_id, hostname, 'rstp',
                                getattr(state, '_stp_priority', 32768))
        return


async def handle_protocol_show(device_id: str, command: str, state: DeviceState):
    """showコマンドでエンジンが起動していればエンジン出力を返す"""
    c = command.lower().strip()

    # ── NX-OS: show logging ──
    if re.match(r'^show\s+logging$', c) and state.device_type == 'nexus':
        logs = nxos_log_buffer.get(device_id, [])
        return nxos_msg.format_show_logging(state.hostname, logs)

    # ── APRESIA: show logging ──
    if re.match(r'^show\s+logging$', c) and state.device_type == 'apresia':
        logs = apresia_log_buffer.get(device_id, [])
        return apresia_msg.format_show_logging(state.hostname, logs)

    # ── Cisco/Catalyst: show logging ──
    if re.match(r'^show\s+logging$', c) and state.device_type in ('catalyst', 'cisco', 'srs'):
        logs = cisco_log_buffer.get(device_id, [])
        return cisco_msg.format_show_logging(logs, state.hostname)

    # ── Si-R: show logging syslog ──
    if re.match(r'^show\s+logging\s+(syslog)?', c) and state.device_type == 'sir':
        logs = sir_syslog.get(device_id, [])
        return sir_msg.format_show_logging(state.hostname, logs)

    # ── NX-OS vPC show コマンド（handle_protocol_configより先に処理）──
    if re.match(r'^show\s+vpc', c):
        if state.device_type == 'nexus' or vpc_engine.feature_enabled.get(device_id):
            if re.match(r'^show\s+vpc\s+peer-keepalive', c):
                return vpc_engine.format_show_vpc_keepalive(device_id)
            if re.match(r'^show\s+vpc\s+role', c):
                return vpc_engine.format_show_vpc_role(device_id)
            if re.match(r'^show\s+vpc\s+consistency', c):
                return vpc_engine.format_show_vpc_consistency(device_id)
            if re.match(r'^show\s+vpc\s+brief', c):
                return vpc_engine.format_show_vpc_brief(device_id)
            if re.match(r'^show\s+vpc', c):
                return vpc_engine.format_show_vpc(device_id)

    # ── pyATS/Genie 風コマンド ──
    gl = re.match(r'^genie\s+learn\s+(\S+)', c)
    if gl:
        return genie_engine.format_learn(device_id, gl.group(1))
    gs = re.match(r'^genie\s+snapshot\s+(\S+)\s+(.+)', c)
    if gs:
        snap_name = gs.group(1)
        features = gs.group(2).replace(',', ' ').split()
        data = genie_engine.take_snapshot(device_id, snap_name, features)
        saved = ', '.join(data.keys()) if data else '(none)'
        return (f'Snapshot "{snap_name}" を保存しました。\n'
                f'  採取feature: {saved}\n'
                f'  比較: genie diff <before> <after>')
    gd = re.match(r'^genie\s+diff\s+(\S+)\s+(\S+)', c)
    if gd:
        return genie_engine.format_diff(device_id, gd.group(1), gd.group(2))
    if re.match(r'^genie\s+snapshots?$', c):
        return genie_engine.list_snapshots(device_id)
    if c in ('genie', 'genie help'):
        return (
            'pyATS/Genie 風コマンド:\n'
            '  genie learn <feature>          状態を構造化データで採取\n'
            '       features: ospf bgp rip interface routing stp arp\n'
            '  genie snapshot <name> <f1> <f2> ...  複数featureを保存\n'
            '  genie diff <before> <after>    2スナップショットを比較\n'
            '  genie snapshots                保存済み一覧\n'
            '\n例（実機pyATSのbefore/after比較を再現）:\n'
            '  genie snapshot before interface routing\n'
            '  (設定変更...)\n'
            '  genie snapshot after interface routing\n'
            '  genie diff before after'
        )

    # ── show running-config（プロトコル設定を反映した動的生成）──
    if re.match(r'^(show\s+(run(ning-config)?|start(up-config)?)|sh\s+run|wr\s+t)\b', c) \
       or c in ('show run', 'sh run'):
        # show interfaces statusにPort-channelを追加（LACPで生成された論理IF）
        lag_ifaces = lacp_engine.get_show_interfaces_lines(device_id, state.device_type)
        for ifname, info in lag_ifaces.items():
            if ifname not in state.interfaces:
                state.interfaces[ifname] = {
                    'ip': '', 'prefix': 0,
                    'status': info['status'],
                    'vlan': 'trunk', 'speed': '1000', 'duplex': 'full',
                    'desc': f'LACP {info["mode"]} ({",".join(m.replace("GigabitEthernet1/0/","Gi1/0/") for m in info["members"])})',
                    'type': 'EtherChannel',
                }
        return _build_running_config(device_id, state)

    # ── prefix-list 表示 ──
    pl_show = re.match(r'^show\s+ip\s+prefix-list(?:\s+(\S+))?', c)
    if pl_show:
        name = pl_show.group(1)
        return filter_engine.format_show_prefix_list(device_id, name)

    # ── syslog / SNMP 設定確認（state反映版）──
    if re.match(r'^show\s+logging', c):
        return _format_show_logging(state)
    if re.match(r'^show\s+snmp', c):
        return _format_show_snmp(state)

    # ── VLAN 表示 ──
    if re.match(r'^show\s+vlan\s+brief', c):
        return vlan_engine.format_show_vlan(device_id, brief=True)
    m_vlan_id = re.match(r'^show\s+vlan\s+id\s+(\d+)', c)
    if m_vlan_id:
        return vlan_engine.format_show_vlan_id(device_id, int(m_vlan_id.group(1)))
    if re.match(r'^show\s+vlan$', c):
        return vlan_engine.format_show_vlan(device_id)

    # ── show interfaces trunk（VLAN実装版）──
    if re.match(r'^show\s+interfaces?\s+trunk', c):
        out = vlan_engine.format_show_interfaces_trunk(device_id)
        return out

    # ── VRRP / HSRP 表示 ──
    if re.match(r'^show\s+vrrp\s+brief', c):
        return vrrp_engine.format_show_vrrp_brief(device_id)
    if re.match(r'^show\s+vrrp', c):
        return vrrp_engine.format_show_vrrp(device_id)
    if re.match(r'^show\s+standby\s+brief', c):
        return vrrp_engine.format_show_standby_brief(device_id)
    if re.match(r'^show\s+standby', c):
        return vrrp_engine.format_show_standby(device_id)

    # ── EtherChannel / linkaggregation 表示 ──
    if re.match(r'^show\s+etherchannel', c):
        return lacp_engine.format_etherchannel_summary(device_id)
    m_lag = re.match(r'^show\s+linkaggregation(?:\s+(\d+))?', c)
    if m_lag:
        group = int(m_lag.group(1)) if m_lag.group(1) else None
        return lacp_engine.format_show_linkaggregation(device_id, group)

    # ── ARP テーブル表示 ──
    if re.match(r'^show\s+(ip\s+)?arp', c):
        _register_icmp(device_id)
        # APRESIAはrules.py側の専用ハンドラに委譲
        if state.device_type == 'apresia':
            return None
        return arp_engine.format_show_arp(device_id, state.device_type)

    # ── ACL / パケットフィルタ表示 ──
    if re.match(r'^show\s+(ip\s+)?access-list', c) or re.match(r'^show\s+acl', c):
        return ipfilter_engine.format_show_acl(device_id, state.device_type)

    # ── 統合ルーティングテーブル（スタティック+動的、AD比較済み）──
    # スタティックルートか動的ルートがあればRIBエンジンの出力を使う
    if re.match(r'^show\s+ip\s+route\s+static', c):
        return rib_engine.format_show_static(device_id)
    if re.match(r'^show\s+ip\s+route$', c) or re.match(r'^show\s+ip\s+route\s', c):
        has_static = bool(rib_engine.nodes.get(device_id, {}).get('static_routes'))
        has_dynamic = any([
            rip_engine.nodes.get(device_id, {}).get('enabled'),
            ospf_engine.nodes.get(device_id, {}).get('enabled'),
            bgp_engine.nodes.get(device_id, {}).get('enabled'),
        ])
        if has_static or has_dynamic:
            return rib_engine.format_show_ip_route(device_id, state.hostname)
        # どちらもなければルールベースのデフォルト表示に任せる

    # RIP
    if re.match(r'^show\s+ip\s+rip\s+neighbor', c):
        n = rip_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return rip_engine.format_show_rip_neighbor(device_id)
    if re.match(r'^show\s+ip\s+rip\s+route', c):
        n = rip_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return rip_engine.format_show_rip_route(device_id)
    if re.match(r'^show\s+ip\s+rip\s+(protocol|status)', c):
        n = rip_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return rip_engine.format_show_rip_protocol(device_id)

    # OSPF
    if re.match(r'^show\s+ip\s+ospf\s+interface', c):
        return ospf_engine.format_show_ospf_interface(device_id)
    if re.match(r'^show\s+ip\s+ospf\s+neighbor', c):
        return ospf_engine.format_show_ospf_neighbor(device_id)
    if re.match(r'^show\s+ip\s+ospf\s+database', c):
        return ospf_engine.format_show_ospf_database(device_id)
    if re.match(r'^show\s+ip\s+ospf\s+route', c):
        n = ospf_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            if state.device_type == 'sir':
                return _format_ospf_route_sir(device_id)
            return ospf_engine.format_show_ospf_route(device_id)

    # BGP
    if re.match(r'^show\s+(ip\s+)?bgp\s+summary', c):
        n = bgp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return bgp_engine.format_show_bgp_summary(device_id)
    # Si-R: show ip bgp status (= summary相当)
    if re.match(r'^show\s+ip\s+bgp\s+status', c):
        n = bgp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return bgp_engine.format_show_bgp_summary(device_id)
    # Si-R: show ip bgp route / route summary
    if re.match(r'^show\s+ip\s+bgp\s+route', c):
        n = bgp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return bgp_engine.format_show_bgp_table(device_id)
    if re.match(r'^show\s+(ip\s+)?bgp$', c):
        n = bgp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return bgp_engine.format_show_bgp_table(device_id)

    # STP
    if re.match(r'^show\s+spanning-tree\s+summary', c):
        return stp_engine.format_show_spanning_tree_summary(device_id)
    m_stp_vlan = re.match(r'^show\s+spanning-tree\s+vlan\s+(\d+)', c)
    if m_stp_vlan:
        return stp_engine.format_show_spanning_tree_vlan(device_id, int(m_stp_vlan.group(1)))
    if re.match(r'^show\s+spanning-tree', c):
        n = stp_engine.nodes.get(device_id)
        if n and n.get('enabled'):
            return stp_engine.format_show_spanning_tree(device_id)

    # プロトコルログ表示
    if re.match(r'^show\s+(protocol\s+)?log', c) or c == 'show logging protocol':
        buf = proto_log_buffer.get(device_id, [])
        if buf:
            lines = ['=== Protocol Event Log ===']
            for m in buf[-20:]:
                msg_text = m.get('message', '')
                if msg_text:
                    lines.append(msg_text)
            return '\n'.join(lines)

    return None


def _format_show_logging(state) -> str:
    """show logging — syslog設定をstateから動的生成（全機種対応）"""
    servers = getattr(state, 'syslog_servers', [])
    level = getattr(state, 'logging_level', 'informational')

    if state.device_type in ('catalyst', 'cisco'):
        lines = [
            'Syslog logging: enabled (0 messages dropped, 0 messages rate-limited,',
            '                0 flushes, 0 overruns, xml disabled, filtering disabled)',
            '',
            f'    Console logging: level debugging, 42 messages logged',
            f'    Monitor logging: level debugging, 0 messages logged',
            f'    Buffer logging:  level {level}, 42 messages logged',
        ]
        if servers:
            lines.append('')
            lines.append('    Trap logging: level ' + level)
            for s in servers:
                lines.append(f'        Logging to {s["host"]} (udp port {s["port"]}), '
                              f'facility {s["facility"]}, 0 message lines logged')
        else:
            lines.append('')
            lines.append('    Trap logging: disabled')
        lines.extend(['', 'Log Buffer (8192 bytes):',
                      '*Jun 13 10:30:00: %LINK-3-UPDOWN: GigabitEthernet1/0/1, changed state to up'])
    elif state.device_type in ('sir', 'srs'):
        lines = [f'  syslog : {"enabled" if servers else "disabled"}',
                 f'  level  : {level}']
        for s in servers:
            lines.append(f'  server : {s["host"]} port {s["port"]}')
        if not servers:
            lines.append('  server : (未設定)')
    else:
        # APRESIA等
        lines = [f'Logging: {"enabled" if servers else "disabled"}',
                 f'Level:   {level}']
        for s in servers:
            lines.append(f'Host:    {s["host"]} (port {s["port"]})')
    return '\n'.join(lines)


def _format_show_snmp(state) -> str:
    """show snmp — SNMP設定をstateから動的生成（全機種対応）"""
    hosts = getattr(state, 'snmp_hosts', [])
    comms = getattr(state, 'snmp_community', [])
    loc   = getattr(state, 'snmp_location', '')
    cont  = getattr(state, 'snmp_contact', '')

    if state.device_type in ('catalyst', 'cisco'):
        lines = [f'Chassis: {state.hostname}']
        if cont:
            lines.append(f'Contact: {cont}')
        if loc:
            lines.append(f'Location: {loc}')
        lines.append('SNMP agent enabled')
        if comms:
            for c in comms:
                lines.append(f'Community: {c["name"]} ({c["perm"].upper()})')
        else:
            lines.append('Community: (未設定)')
        if hosts:
            lines.append('')
            lines.append('Trap hosts:')
            for h in hosts:
                lines.append(f'  {h["host"]}  {h["traps"]}  community: {h["community"]}  '
                              f'version: {h["version"]}')
        else:
            lines.append('Trap hosts: (未設定)')
    elif state.device_type in ('sir', 'srs'):
        lines = ['  SNMP agent : enabled']
        for c in comms:
            lines.append(f'  community  : {c["name"]} ({c["perm"]})')
        if not comms:
            lines.append('  community  : (未設定)')
        for h in hosts:
            lines.append(f'  trap host  : {h["host"]} community {h["community"]}')
        if not hosts:
            lines.append('  trap host  : (未設定)')
        if loc:
            lines.append(f'  location   : {loc}')
    else:
        lines = [f'SNMP: {"enabled" if comms or hosts else "disabled"}']
        for c in comms:
            lines.append(f'Community: {c["name"]} ({c["perm"]})')
        for h in hosts:
            lines.append(f'Trap Host: {h["host"]} (v{h["version"]} {h["community"]})')
    return '\n'.join(lines)


def _find_peer_by_ip(device_id: str, target_ip: str) -> str:
    """指定IPアドレスを持つ隣接装置のdevice_idを返す"""
    from engine.protocols import icmp_engine as _icmp
    for neighbor_id in vnet.get_neighbors(device_id):
        nbr_ips = _icmp.device_ips.get(neighbor_id, {}).get('ips', {})
        if target_ip in nbr_ips:
            return neighbor_id
    return ''


def find_peer_by_link(device_id: str):
    """vnetのリンクから最初の隣接device_idを返す"""
    neighbors = vnet.get_neighbors(device_id)
    return next(iter(neighbors), None) if neighbors else None


def _build_running_config(device_id: str, state) -> str:
    """プロトコルエンジンの状態を反映したrunning-configを生成"""
    is_cisco = state.device_type in ('catalyst', 'cisco')
    is_apresia = state.device_type == 'apresia'
    is_nexus = state.device_type == 'nexus'

    if is_apresia:
        return rule_engine.process('show running-config', state)

    # ── NX-OS (Nexus 9000) running-config ──
    if is_nexus:
        lines = ['!Command: show running-config', '!Running configuration last done at: ' +
                 state.startup_time.strftime('%a %b %d %H:%M:%S %Y'), '!Time: ' +
                 state.startup_time.strftime('%a %b %d %H:%M:%S %Y'), '',
                 'version 10.2(7)M', '']
        # feature
        if vpc_engine.feature_enabled.get(device_id):
            lines.append('feature vpc')
        lines.append('feature lacp')
        lines.append('')
        lines.append(f'hostname {state.hostname}')
        lines.append('')
        # VLANs
        for vid, vinfo in sorted(vlan_engine.vlans.get(device_id, {}).items()):
            lines.append(f'vlan {vid}')
            if vinfo.name and vinfo.name != f'VLAN{vid:04d}':
                lines.append(f'  name {vinfo.name}')
        if vlan_engine.vlans.get(device_id):
            lines.append('')
        # vPC domain
        lines.extend(vpc_engine.get_running_config(device_id))
        # OSPF
        on = ospf_engine.nodes.get(device_id)
        if on and on.get('enabled'):
            lines.append(f'router ospf {on["process_id"]}')
            for net in on.get('networks', []):
                lines.append(f'  network {net} area {on["area_id"]}')
            lines.append('')
        # BGP
        bn = bgp_engine.nodes.get(device_id)
        if bn and bn.get('local_as'):
            lines.append(f'router bgp {bn["local_as"]}')
            for sid, sess in bn.get('sessions', {}).items():
                peer_ip = bn.get('_neighbor_ips', {}).get(sid, sess.neighbor_id)
                lines.append(f'  neighbor {peer_ip} remote-as {sess.remote_as}')
            lines.append('')
        lines.append('! end')
        return '\n'.join(lines)

    lines = []

    if is_cisco:
        lines.append('Building configuration...')
        lines.append('')
        lines.append('Current configuration : ' + str(random.randint(1500, 4000)) + ' bytes')
        lines.append('!')
        lines.append('version 17.3')
        lines.append('!')
        lines.append(f'hostname {state.hostname}')
        lines.append('!')
        # インタフェース（state.interfacesの全ポートを表示）
        info = icmp_engine.device_ips.get(device_id, {})
        ip_map = {}
        for ifname, iinfo in info.get('interfaces', {}).items():
            if iinfo.get('ip'):
                ip_map[ifname] = iinfo
        # state.interfaces から全インタフェースを出力
        state_ifaces = getattr(state, 'interfaces', {})
        if state_ifaces:
            for ifname, iinfo in state_ifaces.items():
                lines.append(f'interface {ifname}')
                ip = iinfo.get('ip', '')
                if ip:
                    mask = _prefix_to_mask(iinfo.get('prefix', 24))
                    lines.append(f' ip address {ip} {mask}')
                elif iinfo.get('vlan') and iinfo.get('vlan') not in ('', 'trunk'):
                    lines.append(' switchport mode access')
                    lines.append(f' switchport access vlan {iinfo["vlan"]}')
                elif iinfo.get('vlan') == 'trunk':
                    lines.append(' switchport mode trunk')
                else:
                    lines.append(' no ip address')
                # STP portfast / bpduguard
                pcfg = stp_engine.port_config.get(device_id, {}).get(ifname, {})
                stp_n = stp_engine.nodes.get(device_id, {})
                port_stp = stp_n.get('ports', {}).get(ifname, {})
                if pcfg.get('portfast') or port_stp.get('portfast'):
                    lines.append(' spanning-tree portfast')
                if pcfg.get('bpduguard') or port_stp.get('bpduguard'):
                    lines.append(' spanning-tree bpduguard enable')
                if pcfg.get('root_guard') or port_stp.get('root_guard'):
                    lines.append(' spanning-tree guard root')
                # LACP channel-group
                for ch_id, ch in lacp_engine.channels.get(device_id, {}).items():
                    if ifname in [m.interface for m in ch.get('members', [])]:
                        m_obj = next((m for m in ch['members'] if m.interface == ifname), None)
                        mode = m_obj.mode if m_obj else 'active'
                        lines.append(f' channel-group {ch_id} mode {mode}')
                        break
                status = iinfo.get('status', '')
                if status in ('notconnect', 'down', 'disabled'):
                    lines.append(' shutdown')
                else:
                    lines.append(' no shutdown')
                lines.append('!')
        else:
            # フォールバック: ICMPのIP情報のみ
            for ifname, iinfo in ip_map.items():
                lines.append(f'interface {ifname}')
                mask = _prefix_to_mask(iinfo.get('prefix', 24))
                lines.append(f' ip address {iinfo["ip"]} {mask}')
                lines.append(' no shutdown')
                lines.append('!')
        # ACL適用済みインタフェース
        # スタティックルート
        rib_node = rib_engine.nodes.get(device_id)
        if rib_node:
            for r in rib_node['static_routes']:
                mask = _prefix_to_mask(r.prefix)
                ad = f' {r.ad}' if r.ad != 1 else ''
                lines.append(f'ip route {r.network} {mask} {r.next_hop}{ad}')
        # RIP
        rn = rip_engine.nodes.get(device_id)
        if rn and rn.get('enabled'):
            lines.append('!')
            lines.append('router rip')
            lines.append(' version 2')
            for net in rn['networks']:
                lines.append(f' network {net.split("/")[0]}')
        # OSPF
        on = ospf_engine.nodes.get(device_id)
        if on and on.get('enabled'):
            lines.append('!')
            lines.append(f'router ospf {on["process_id"]}')
            for net in on['networks']:
                lines.append(f' network {net.split("/")[0]} 0.0.0.255 area {on["area_id"]}')
        # BGP
        bn = bgp_engine.nodes.get(device_id)
        if bn and bn.get('enabled'):
            lines.append('!')
            lines.append(f'router bgp {bn["local_as"]}')
            for nid, sess in bn['sessions'].items():
                peer_ip = '10.0.0.2'
                lines.append(f' neighbor {peer_ip} remote-as {sess.remote_as}')
            for net in bn.get('networks', []):
                lines.append(f' network {net.split("/")[0]} mask {_prefix_to_mask(int(net.split("/")[1]) if "/" in net else 24)}')
        # ACL
        acls = ipfilter_engine.acls.get(device_id, {})
        for name, rules in acls.items():
            lines.append('!')
            for r in rules:
                if r.protocol == 'ip' and r.dst == 'any' and not r.dst_port:
                    lines.append(f'access-list {name} {r.action} {r.src}')
                else:
                    portstr = f' eq {r.dst_port}' if r.dst_port else ''
                    lines.append(f'access-list {name} {r.action} {r.protocol} {r.src} {r.dst}{portstr}')
        # prefix-list
        plists = filter_engine.prefix_lists.get(device_id, {})
        for pname, entries in plists.items():
            lines.append('!')
            for ent in entries:
                gele = ''
                if ent.ge is not None:
                    gele += f' ge {ent.ge}'
                if ent.le is not None:
                    gele += f' le {ent.le}'
                lines.append(f'ip prefix-list {pname} seq {ent.seq} {ent.action} {ent.network}/{ent.prefix}{gele}')
        # LACP / EtherChannel（Port-channel）
        lag_lines = lacp_engine.get_running_config_lines(device_id, state.device_type)
        if lag_lines:
            lines.extend(lag_lines)
        # VRRP設定
        vrrp_groups = vrrp_engine.vrrp.get(device_id, {})
        for gid, g in sorted(vrrp_groups.items()):
            lines.append('!')
            lines.append(f'interface {g.interface or "GigabitEthernet0/0/0"}')
            lines.append(f' vrrp {gid} ip {g.vip}')
            lines.append(f' vrrp {gid} priority {g.priority}')
            if g.preempt:
                lines.append(f' vrrp {gid} preempt')
        # HSRP設定
        hsrp_groups = vrrp_engine.hsrp.get(device_id, {})
        for gid, g in sorted(hsrp_groups.items()):
            iface = g.interface or 'GigabitEthernet0/0/0'
            lines.append('!')
            lines.append(f'interface {iface}')
            lines.append(f' standby {gid} ip {g.vip}')
            lines.append(f' standby {gid} priority {g.priority}')
            if g.preempt:
                lines.append(f' standby {gid} preempt')
        # line con / line vty
        lines.append('!')
        lines.append('line con 0')
        lines.append(' exec-timeout 0 0')
        lines.append(' logging synchronous')
        lines.append(' transport preferred none')
        lines.append('line vty 0 4')
        lines.append(' login local')
        lines.append(' transport input ssh telnet')
        lines.append('line vty 5 15')
        lines.append(' login local')
        lines.append(' transport input ssh telnet')
        # syslog
        syslog = getattr(state, 'syslog_servers', [])
        if syslog:
            lines.append('!')
            lines.append(f'logging trap {getattr(state,"logging_level","informational")}')
            for s in syslog:
                port_str = f' {s["port"]}' if s.get('port', 514) != 514 else ''
                lines.append(f'logging facility {s.get("facility","local7")}')
                lines.append(f'logging host {s["host"]}{port_str}')
        # SNMP
        comms = getattr(state, 'snmp_community', [])
        hosts = getattr(state, 'snmp_hosts', [])
        loc   = getattr(state, 'snmp_location', '')
        cont  = getattr(state, 'snmp_contact', '')
        if comms or hosts or loc or cont:
            lines.append('!')
            if loc:
                lines.append(f'snmp-server location {loc}')
            if cont:
                lines.append(f'snmp-server contact {cont}')
            for c in comms:
                lines.append(f'snmp-server community {c["name"]} {c["perm"].upper()}')
            for h in hosts:
                lines.append(f'snmp-server host {h["host"]} {h["traps"]} '
                              f'version {h["version"]} {h["community"]}')
        lines.append('!')
        lines.append('end')
    else:
        # Si-R形式
        lines.append('# Si-R running configuration')
        lines.append(f'hostname {state.hostname}')
        state_ifaces = getattr(state, 'interfaces', {})
        info = icmp_engine.device_ips.get(device_id, {})
        # state.interfaces優先、なければICMP情報
        ifaces_src = state_ifaces if state_ifaces else info.get('interfaces', {})
        for ifname, iinfo in ifaces_src.items():
            ip = iinfo.get('ip', '')
            if ip and ifname.startswith('lan'):
                idx = ifname.replace('lan', '')
                lines.append(f'lan {idx} ip address {ip}/{iinfo.get("prefix",24)} 3')
            elif ip and ifname.startswith('wan'):
                idx = ifname.replace('wan', '')
                lines.append(f'wan {idx} ip address {ip}/{iinfo.get("prefix",24)}')
        rib_node = rib_engine.nodes.get(device_id)
        if rib_node:
            for r in rib_node['static_routes']:
                ad = f' {r.ad}' if r.ad != 1 else ''
                lines.append(f'ip route {r.network}/{r.prefix} {r.next_hop}{ad}')
        rn = rip_engine.nodes.get(device_id)
        if rn and rn.get('enabled'):
            lines.append('ip rip use on')
            for net in rn['networks']:
                lines.append(f'ip rip network {net}')
        on = ospf_engine.nodes.get(device_id)
        if on and on.get('enabled'):
            lines.append('ospf use on')
            for net in on.get('networks', []):
                lines.append(f'ip ospf network {net}')
            lines.append('lan 0 ip ospf use on')
        bn = bgp_engine.nodes.get(device_id)
        if bn and bn.get('local_as'):
            lines.append(f'router bgp {bn["local_as"]}')
            neighbor_ips = bn.get('_neighbor_ips', {})
            for sid, sess in bn.get('sessions', {}).items():
                # _neighbor_ipsからIPアドレスを取得、なければneighbor_idを使用
                peer_ip = neighbor_ips.get(sid, sess.neighbor_id)
                lines.append(f' neighbor {peer_ip} remote-as {sess.remote_as}')
            for net in bn.get('networks', []):
                lines.append(f' network {net}')
        # VRRP (Si-R形式)
        for gid, g in sorted(vrrp_engine.vrrp.get(device_id, {}).items()):
            lines.append(f'vrrp use on')
            iface_idx = '0'
            iface = g.interface or 'lan0'
            if iface.startswith('lan'):
                iface_idx = iface.replace('lan','')
            lines.append(f'lan {iface_idx} vrrp group {gid} id {gid} {g.priority} {g.vip}')
        # LACP / linkaggregation（SR-S形式）
        lag_lines = lacp_engine.get_running_config_lines(device_id, state.device_type)
        if lag_lines:
            lines.extend(lag_lines)
        # コンソール / リモートアクセス設定（Si-R形式）
        lines.append('consoleinfo authtype password')
        lines.append('telnetinfo authtype password')
        lines.append('terminal charset SJIS')
        lines.append('rebootlog use on')
        lines.append('syslog facility 1')
        # syslog送信先
        for s in getattr(state, 'syslog_servers', []):
            lines.append(f'syslog server {s["host"]}')
            if s.get('port', 514) != 514:
                lines.append(f'syslog port {s["port"]}')
        # SNMP
        for c in getattr(state, 'snmp_community', []):
            lines.append(f'snmp community {c["name"]} {c["perm"]}')
        for h in getattr(state, 'snmp_hosts', []):
            lines.append(f'snmp trap host {h["host"]} community {h["community"]}')
        if getattr(state, 'snmp_location', ''):
            lines.append(f'snmp location {state.snmp_location}')
        lines.append('save')
    return '\n'.join(lines)


def _prefix_to_mask(prefix: int) -> str:
    """プレフィックス長をサブネットマスクに変換"""
    mask = (0xffffffff << (32 - prefix)) & 0xffffffff
    return f'{(mask>>24)&0xff}.{(mask>>16)&0xff}.{(mask>>8)&0xff}.{mask&0xff}'


def handle_icmp(device_id: str, command: str, state: DeviceState):
    """ping / traceroute を実到達性判定で処理"""
    c = command.lower().strip()

    # ping
    m = re.match(r'^ping\s+([\d.]+)', c)
    if m:
        dest = m.group(1)
        _register_icmp(device_id)
        # ARP解決を試みる（同一セグメントならARPテーブルに記録）
        arp_engine.resolve(device_id, dest)
        result = icmp_engine.ping(device_id, dest, count=5)
        return _format_ping(dest, result, state.device_type)

    # traceroute / tracert
    m = re.match(r'^(?:traceroute|tracert)\s+([\d.]+)', c)
    if m:
        dest = m.group(1)
        _register_icmp(device_id)
        result = icmp_engine.traceroute(device_id, dest)
        return _format_traceroute(dest, result)

    return None


def _format_ping(dest: str, result: dict, device_type: str) -> str:
    """ping結果を実機風フォーマットで整形"""
    if result['reachable']:
        rtts = result['rtts']
        ttl = result['ttl']
        if device_type in ('catalyst', 'cisco'):
            # Cisco形式: 成功は感嘆符
            success = '!!!!!'
            mn, av, mx = min(rtts), sum(rtts)/len(rtts), max(rtts)
            return (f"Type escape sequence to abort.\n"
                    f"Sending 5, 100-byte ICMP Echos to {dest}, timeout is 2 seconds:\n"
                    f"{success}\n"
                    f"Success rate is 100 percent (5/5), "
                    f"round-trip min/avg/max = {mn:.0f}/{av:.0f}/{mx:.0f} ms")
        else:
            # Si-R / 汎用 形式
            lines = [f"PING {dest} ({dest}): 56 data bytes"]
            for i, r in enumerate(rtts):
                lines.append(f"64 bytes from {dest}: icmp_seq={i} ttl={result['ttl']} time={r} ms")
            lines += ["",
                f"--- {dest} ping statistics ---",
                f"5 packets transmitted, 5 packets received, 0% packet loss",
                f"round-trip min/avg/max = {min(rtts)}/{round(sum(rtts)/len(rtts),3)}/{max(rtts)} ms"]
            return '\n'.join(lines)
    else:
        # 到達不能
        reason = result.get('reason', 'unreachable')
        if device_type in ('catalyst', 'cisco'):
            return (f"Type escape sequence to abort.\n"
                    f"Sending 5, 100-byte ICMP Echos to {dest}, timeout is 2 seconds:\n"
                    f".....\n"
                    f"Success rate is 0 percent (0/5)")
        else:
            lines = [f"PING {dest} ({dest}): 56 data bytes"]
            for i in range(5):
                lines.append(f"Request timeout for icmp_seq {i}")
            lines += ["",
                f"--- {dest} ping statistics ---",
                f"5 packets transmitted, 0 packets received, 100% packet loss",
                f"({reason})"]
            return '\n'.join(lines)


def _format_traceroute(dest: str, result: dict) -> str:
    """traceroute結果を整形"""
    lines = [f"traceroute to {dest} ({dest}), 30 hops max, 40 byte packets"]
    hop_num = 1
    for hop in result['hops']:
        if hop.get('timeout'):
            lines.append(f" {hop_num}  * * *  ({hop['hostname']}: no route)")
        else:
            rtts = [round(hop_num * random.uniform(0.5, 2.0) + random.uniform(0, 3), 3)
                    for _ in range(3)]
            host = hop.get('hostname', hop.get('ip', '?'))
            ip = hop.get('ip', '?')
            lines.append(f" {hop_num}  {host} ({ip})  "
                         f"{rtts[0]} ms  {rtts[1]} ms  {rtts[2]} ms")
        hop_num += 1
    if not result['reachable'] and not any(h.get('timeout') for h in result['hops']):
        lines.append(f" {hop_num}  * * *  (destination unreachable)")
    return '\n'.join(lines)


def _mask_to_prefix(mask: str) -> int:
    """サブネットマスク(255.255.255.0)をプレフィックス長(24)に変換"""
    try:
        return sum(bin(int(o)).count('1') for o in mask.split('.'))
    except Exception:
        return 24


def _wildcard_to_prefix(wildcard: str) -> int:
    """Ciscoワイルドカードマスク(0.0.0.255)をプレフィックス長(24)に変換"""
    try:
        inv = '.'.join(str(255 - int(o)) for o in wildcard.split('.'))
        return sum(bin(int(o)).count('1') for o in inv.split('.'))
    except Exception:
        return 24


def _wildcard_to_cidr(network: str, wildcard: str) -> str:
    """Ciscoワイルドカードマスク(0.0.0.255)をCIDR表記(X/24)に変換"""
    try:
        # ワイルドカードのビット反転がサブネットマスク
        inv = '.'.join(str(255 - int(o)) for o in wildcard.split('.'))
        prefix = sum(bin(int(o)).count('1') for o in inv.split('.'))
        return f'{network}/{prefix}'
    except Exception:
        return f'{network}/32'


async def _emit_route_log(device_id: str, net: str, prefix: int,
                          next_hop: str, ad: int):
    """スタティックルート追加をログ表示"""
    route_type = 'フローティングスタティック (バックアップ)' if ad > 1 else 'スタティックルート'
    msg = f'ルート追加: {net}/{prefix} via {next_hop} [AD={ad}] — {route_type}'
    # protoバッファに記録
    buf = proto_log_buffer.setdefault(device_id, [])
    buf.append({'type': 'static_log', 'message': msg})
    if len(buf) > 50:
        buf.pop(0)


def _network_address(ip: str, prefix: int) -> str:
    """IPアドレスとプレフィックスからネットワークアドレスを算出"""
    try:
        octets = [int(x) for x in ip.split('.')]
        ip_int = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
        mask = (0xffffffff << (32 - prefix)) & 0xffffffff
        net_int = ip_int & mask
        net = f'{(net_int >> 24) & 0xff}.{(net_int >> 16) & 0xff}.{(net_int >> 8) & 0xff}.{net_int & 0xff}'
        return f'{net}/{prefix}'
    except Exception:
        return f'{ip}/{prefix}'


def _format_ospf_route_sir(device_id: str) -> str:
    """Si-R形式の show ip ospf route"""
    n = ospf_engine.nodes.get(device_id)
    if not n or not n.get('enabled'):
        return '<ERROR> OSPF is not configured.'
    lines = ['Destination/Mask     Next-Hop          Cost   Type      Area']
    for r in n.get('routes', []):
        net = f"{r['network']}/{r['prefix']}"
        via = 'directly connected' if r['via'] == 'direct' else r['next_hop']
        lines.append(f"{net:<21}{via:<18}{r['metric']:<7}Intra     {n['area_id']}")
    if not n.get('routes'):
        lines.append('(No OSPF routes)')
    return '\n'.join(lines)

@app.post("/api/device")
async def add_device(body: dict):
    """装置を追加。PCの場合は ip / prefix / gateway を指定可能"""
    dev_id   = body.get("id")
    dev_type = body.get("type", "cisco")
    hostname = body.get("hostname", dev_id)
    if dev_id and dev_id not in device_sessions:
        state = DeviceState(dev_type, hostname)
        # PCの場合、オプションパラメータでIP/GWを上書き
        if dev_type == "pc":
            ip      = body.get("ip")
            prefix  = body.get("prefix", 24)
            gateway = body.get("gateway")
            if ip:
                state.interfaces["eth0"]["ip"]     = ip
                state.interfaces["eth0"]["prefix"] = int(prefix)
                # ルーティングテーブルのdirectルートも更新
                net_int = 0
                try:
                    o = [int(x) for x in ip.split('.')]
                    ip_int = (o[0]<<24)|(o[1]<<16)|(o[2]<<8)|o[3]
                    mask   = (0xffffffff << (32 - int(prefix))) & 0xffffffff
                    net_int = ip_int & mask
                except Exception:
                    pass
                net_str = (f'{(net_int>>24)&0xff}.{(net_int>>16)&0xff}.'
                           f'{(net_int>>8)&0xff}.{net_int&0xff}/{prefix}')
                state.routes = [r for r in state.routes if r['dest'] not in (
                    state.routes[1]['dest'], '192.168.1.0/24')]
                state.routes = [
                    {"dest": "0.0.0.0/0", "gw": gateway or state.gateway, "iface": "eth0", "metric": 0},
                    {"dest": net_str,      "gw": "0.0.0.0",               "iface": "eth0", "metric": 100},
                    {"dest": "127.0.0.0/8","gw": "0.0.0.0",              "iface": "lo",   "metric": 0},
                ]
            if gateway:
                state.gateway = gateway
        device_sessions[dev_id] = state
    if dev_id:
        _register_stub(dev_id)
        _register_icmp(dev_id)
    return {"ok": True, "id": dev_id}


def _register_icmp(device_id: str):
    """ICMPエンジンに装置のIP情報を登録。全デバイスで直結ネット・静的ルートをrib_engineに登録"""
    state = device_sessions.get(device_id)
    if not state:
        return
    interfaces = {}
    for ifname, info in state.interfaces.items():
        if info.get('ip'):
            interfaces[ifname] = {'ip': info['ip'],
                                  'prefix': info.get('prefix', 24)}
    icmp_engine.register_device(device_id, state.hostname, interfaces)

    def _net_addr(ip, prefix):
        try:
            octets = [int(x) for x in ip.split('.')]
            ip_int = (octets[0]<<24)|(octets[1]<<16)|(octets[2]<<8)|octets[3]
            mask_bits = (0xffffffff << (32 - prefix)) & 0xffffffff
            net_int = ip_int & mask_bits
            return (f'{(net_int>>24)&0xff}.{(net_int>>16)&0xff}.'
                    f'{(net_int>>8)&0xff}.{net_int&0xff}')
        except Exception:
            return None

    # 全デバイス: インタフェース直結ネットワークをrib_engineに登録
    for ifname, info in state.interfaces.items():
        ip = info.get('ip', '')
        prefix = info.get('prefix', 24)
        if ip and ip != '127.0.0.1':
            net = _net_addr(ip, prefix)
            if net:
                rib_engine.add_static_route(device_id, state.hostname,
                                            net, prefix, '0.0.0.0', 0)

    # PC: デフォルトゲートウェイをrib_engineに登録
    if state.device_type == 'pc':
        gw = getattr(state, 'gateway', '')
        if gw:
            rib_engine.add_static_route(device_id, state.hostname,
                                        '0.0.0.0', 0, gw, 1)

    # ルーター系: 静的ルート（remote N ip route / ip route）をrib_engineに登録
    for route in getattr(state, 'static_routes', []):
        dest   = route.get('dest', '')
        prefix = route.get('prefix', 0)
        gw     = route.get('gw', '0.0.0.0')
        if dest:
            rib_engine.add_static_route(device_id, state.hostname,
                                        dest, prefix, gw, 1)

@app.post("/api/save")
async def api_save():
    """全デバイス設定をファイルに保存"""
    _save_config()
    return {"ok": True, "path": str(SAVED_CONFIG_PATH)}

@app.post("/api/load")
async def api_load():
    """保存済み設定をロード（サーバー再起動不要）"""
    device_sessions.clear()
    _load_config()
    return {"ok": True, "devices": list(device_sessions.keys())}

@app.delete("/api/device/{dev_id}")
async def remove_device(dev_id: str):
    """装置を削除"""
    if dev_id in device_sessions:
        del device_sessions[dev_id]
    return {"ok": True}

@app.post("/api/config/validate")
async def api_validate_config(body: dict):
    """
    コンフィグテキストの事前チェック（インポート前の検証）。
    body: {"config": "<running-config text>"}

    チェック項目:
      - 存在確認（空でないか）
      - 2バイト文字（日本語等）が含まれていないか
      - 行数が1万行を超えていないか
      - 空白行が30%超で不要スペースが多くないか
    """
    config_text = body.get("config", "")
    result = validate_config_text(config_text)
    return result


@app.post("/api/device/{dev_id}/import")
async def import_device_config(dev_id: str, body: dict):
    """
    running-configテキストを受け取りデバイス状態に反映する。
    body: {"config": "<running-config text>", "type": "cisco", "reset": true}
      - type: デバイスタイプ上書き（省略時は既存デバイスのタイプを使用）
      - reset: true にすると状態をリセットしてからインポート
    """
    config_text = body.get("config", "")
    if not config_text:
        return JSONResponse(status_code=400, content={"ok": False, "error": "config is required"})

    # 既存セッションを取得、なければ作成
    if dev_id not in device_sessions:
        dev = DEFAULT_DEVICES.get(dev_id, {"type": body.get("type", "cisco"), "hostname": dev_id})
        device_sessions[dev_id] = DeviceState(dev["type"], dev["hostname"])

    state = device_sessions[dev_id]

    # デバイスタイプの決定（bodyで上書き可能）
    dev_type = body.get("type") or state.device_type

    # reset=true の場合、同タイプ・同ホスト名で状態をリセット
    if body.get("reset"):
        state = DeviceState(dev_type, state.hostname)
        device_sessions[dev_id] = state

    # running-config をインポート
    result = import_running_config(dev_type, config_text, rule_engine, state)

    # ICMPエンジンに再登録
    _register_icmp(dev_id)

    return {**result, "dev_id": dev_id, "device_type": dev_type, "hostname": state.hostname}


@app.patch("/api/device/{dev_id}/hostname")
async def update_hostname(dev_id: str, body: dict):
    """ホスト名を更新"""
    if dev_id in device_sessions:
        device_sessions[dev_id].hostname = body.get("hostname", dev_id)
    return {"ok": True}

# ── リンク管理（vnetに反映）──
@app.post("/api/link")
async def add_link(body: dict):
    """装置間リンクを作成（プロトコルパケットが流れるようになる）"""
    a = body.get("a")
    b = body.get("b")
    if a and b:
        vnet.add_link(a, b)
        _save_config()
    return {"ok": True, "neighbors": {a: list(vnet.get_neighbors(a)),
                                       b: list(vnet.get_neighbors(b))}}

@app.delete("/api/link")
async def remove_link(body: dict):
    """リンクを削除"""
    a = body.get("a")
    b = body.get("b")
    if a and b:
        vnet.remove_link(a, b)
        _save_config()
    return {"ok": True}

@app.post("/api/route/nexthop")
async def set_nexthop(body: dict):
    """next_hopの到達性を変更（フローティングスタティック切替テスト用）"""
    device_id = body.get("device_id")
    next_hop = body.get("next_hop")
    reachable = body.get("reachable", True)
    if device_id and next_hop:
        rib_engine.set_nexthop_reachable(device_id, next_hop, reachable)
        # ログ
        buf = proto_log_buffer.setdefault(device_id, [])
        state_str = '到達可能' if reachable else '到達不能（ダウン）'
        buf.append({'type': 'static_log',
                    'message': f'Next-hop {next_hop} が{state_str}に変化 → ルート再計算'})
    return {"ok": True}


@app.post("/api/links/sync")
async def sync_links(body: dict):
    """フロントエンドの全リンクをvnetに同期"""
    links = body.get("links", [])
    # 既存リンクをクリアして再構築
    for dev_id in list(vnet.links.keys()):
        vnet.links[dev_id] = set()
    for l in links:
        a, b = l.get("a"), l.get("b")
        if a and b:
            vnet.add_link(a, b)
    # WebSocket未接続の装置にもスタブコールバックを登録
    # （これがないとプロトコルパケットが転送先で処理されない）
    for dev_id in device_sessions:
        if dev_id not in vnet.ws_send_callbacks:
            _register_stub(dev_id)
    _save_config()
    return {"ok": True, "count": len(links)}


# CLIコマンドでプロトコルが起動したとき、表示用ログを溜めるバッファ
proto_log_buffer: Dict[str, list] = {}

# Si-R システムログバッファ（show logging syslog 用）
sir_syslog: Dict[str, list] = {}

# Cisco/Catalyst ログバッファ（show logging 用）
cisco_log_buffer: Dict[str, list] = {}

# NX-OS ログバッファ（show logging 用）
nxos_log_buffer: Dict[str, list] = {}

# APRESIA ログバッファ（show logging 用）
apresia_log_buffer: Dict[str, list] = {}

def _cisco_log(device_id: str, message: str):
    """Cisco/Catalyst システムログを追記してターミナルに出力"""
    buf = cisco_log_buffer.setdefault(device_id, [])
    buf.append({'type': 'cisco_log', 'message': message})
    if len(buf) > 200:
        buf.pop(0)
    pbuf = proto_log_buffer.setdefault(device_id, [])
    pbuf.append({'type': 'cisco_log', 'message': message})
    if len(pbuf) > 50:
        pbuf.pop(0)


def _nxos_log(device_id: str, message: str):
    """NX-OS システムログを追記"""
    buf = nxos_log_buffer.setdefault(device_id, [])
    buf.append({'type': 'nxos_log', 'message': message})
    if len(buf) > 200:
        buf.pop(0)
    pbuf = proto_log_buffer.setdefault(device_id, [])
    pbuf.append({'type': 'nxos_log', 'message': message})
    if len(pbuf) > 50:
        pbuf.pop(0)


def _apresia_log(device_id: str, message: str):
    """APRESIA システムログを追記"""
    buf = apresia_log_buffer.setdefault(device_id, [])
    buf.append({'type': 'apresia_log', 'message': message})
    if len(buf) > 200:
        buf.pop(0)
    pbuf = proto_log_buffer.setdefault(device_id, [])
    pbuf.append({'type': 'apresia_log', 'message': message})
    if len(pbuf) > 50:
        pbuf.pop(0)

def _sir_log(device_id: str, facility: str, message: str):
    """Si-R システムログを追記"""
    from datetime import datetime
    ts = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    buf = sir_syslog.setdefault(device_id, [])
    buf.append((ts, facility, message))
    if len(buf) > 200:
        buf.pop(0)
    state = device_sessions.get(device_id)
    hostname = state.hostname if state else device_id
    log_line = f'{ts} {hostname} Si-R G120 : [{facility}] {message}'
    pbuf = proto_log_buffer.setdefault(device_id, [])
    pbuf.append({'type': 'sir_log', 'message': log_line})
    if len(pbuf) > 50:
        pbuf.pop(0)


def _convert_to_sir_format(facility: str, raw_msg: str) -> str:
    """
    プロトコルエンジンの英語ログメッセージをSi-R形式に変換する
    マニュアル「Si-R G シリーズ メッセージ集」準拠
    """
    if not raw_msg:
        return ''
    m = raw_msg.lower()

    if facility == 'OSPF':
        if 'full' in m and ('neighbor' in m or 'nbr' in m or 'adjchg' in m):
            # OSPFネイバーFull確立
            import re as _re
            nbr = _re.search(r'(?:nbr|neighbor)\s+([\d.]+)', raw_msg, _re.I)
            iface = _re.search(r'on\s+(\S+)', raw_msg, _re.I)
            nbr_id = nbr.group(1) if nbr else 'unknown'
            iface_str = iface.group(1) if iface else 'lan0'
            return f'neighbor {nbr_id} on {iface_str} state: Full'
        elif 'spf' in m and 'calc' in m:
            return 'SPF calculation done'
        elif 'dr' in m and ('elected' in m or 'changed' in m):
            return 'DR elected'
        elif 'started' in m or 'process' in m:
            return 'OSPF started'
        elif 'down' in m:
            return 'neighbor down'

    elif facility == 'RIP':
        if 'learned' in m or 'update' in m or 'rdbupdate' in m:
            import re as _re
            net = _re.search(r'([\d.]+/\d+)', raw_msg)
            via = _re.search(r'via\s+(\S+)', raw_msg, _re.I)
            net_str = net.group(1) if net else 'unknown'
            via_str = via.group(1) if via else 'unknown'
            return f'learned {net_str} via {via_str}'
        elif 'started' in m:
            return 'RIPv2 started'
        elif 'expired' in m or 'timeout' in m or 'delete' in m:
            import re as _re
            net = _re.search(r'([\d.]+/\d+)', raw_msg)
            return f'route {net.group(1) if net else "unknown"} expired'

    elif facility == 'BGP':
        if 'established' in m or 'up' in m:
            import re as _re
            peer = _re.search(r'neighbor\s+(\S+)', raw_msg, _re.I)
            asn = _re.search(r'as(\d+)', raw_msg, _re.I)
            peer_str = peer.group(1) if peer else 'unknown'
            asn_str = asn.group(1) if asn else 'unknown'
            return f'peer {peer_str} (AS{asn_str}) state: Established'
        elif 'down' in m:
            return 'peer down'

    elif facility == 'VRRP':
        import re as _re
        vrid_m = (_re.search(r'Group\s+(\d+)', raw_msg) or
                  _re.search(r'Grp\s+(\d+)', raw_msg) or
                  _re.search(r'VRID\s+(\d+)', raw_msg) or
                  _re.search(r'group\s+(\d+)', raw_msg, _re.I))
        vrid_str = vrid_m.group(1) if vrid_m else '1'
        if 'master' in m:
            return f'VRID {vrid_str}: state changed to Master'
        elif 'backup' in m:
            return f'VRID {vrid_str}: state changed to Backup'
        elif 'init' in m:
            return f'VRID {vrid_str}: Initialized'


    elif facility == 'STP':
        if 'topology' in m:
            return 'topology change detected'
        elif 'root' in m:
            return 'root bridge changed'
        elif 'forwarding' in m:
            return 'port state changed to Forwarding'
        elif 'blocking' in m:
            return 'port state changed to Blocking'

    return raw_msg[:80] if raw_msg else ''

def _register_stub(device_id: str):
    """WebSocket未接続でもプロトコルパケットがルーティングされるようスタブ登録"""
    if device_id not in proto_log_buffer:
        proto_log_buffer[device_id] = []
    state = device_sessions.get(device_id)
    hostname = state.hostname if state else device_id

    async def stub_send(msg: dict):
        mt = msg.get('type', '')
        if mt in ('rip_log', 'ospf_log', 'bgp_log', 'stp_log',
                  'vrrp_log', 'hsrp_log', 'vpc_log',
                  'rip_table', 'bgp_state', 'stp_topology_change'):
            buf = proto_log_buffer.setdefault(device_id, [])
            buf.append(msg)
            if len(buf) > 50:
                buf.pop(0)
            # Si-R の場合: プロトコルログをSi-R形式に変換してsyslogにも記録
            if state and state.device_type == 'sir':
                facility_map = {
                    'rip_log': 'RIP', 'ospf_log': 'OSPF',
                    'bgp_log': 'BGP', 'stp_log': 'STP',
                    'vrrp_log': 'VRRP', 'hsrp_log': 'VRRP',
                }
                fac = facility_map.get(mt)
                if fac:
                    raw_msg = msg.get('message', '')
                    # 既存の英語メッセージからSi-R形式に変換
                    # OSPF neighbor Fullなどの主要パターン
                    sir_msg_text = _convert_to_sir_format(fac, raw_msg)
                    if sir_msg_text:
                        _sir_log(device_id, fac, sir_msg_text)
            # Cisco/Catalyst/NX-OS: cisco_log_bufferにも追記
            elif state and state.device_type in ('catalyst', 'cisco', 'srs'):
                raw_msg = msg.get('message', '')
                if raw_msg:
                    _cisco_log(device_id, raw_msg)
            elif state and state.device_type == 'nexus':
                raw_msg = msg.get('message', '')
                if raw_msg:
                    _nxos_log(device_id, raw_msg)

    if device_id not in vnet.ws_send_callbacks:
        vnet.register(device_id, stub_send, hostname)
    elif hostname:
        vnet.ws_send_callbacks[f'_hostname_{device_id}'] = hostname
    # デバイスタイプを登録（ログフォーマッター用）
    if state:
        vnet.ws_send_callbacks[f'_type_{device_id}'] = state.device_type

    # syslogターゲットをdispatcherに同期
    if state and getattr(state, 'syslog_servers', []):
        syslog_dispatcher.register(device_id, state.syslog_servers)

@app.get("/api/topology")
async def get_topology():
    """現在のトポロジー情報を返す"""
    devices = {}
    for dev_id, state in device_sessions.items():
        dev = DEFAULT_DEVICES.get(dev_id, {})
        devices[dev_id] = {
            "type":     state.device_type,
            "hostname": state.hostname,
            "mode":     state.mode,
            "color":    dev.get("color", "#808080"),
            "vrrp_state": state.vrrp.get("state") if hasattr(state, 'vrrp') else None,
        }
    return {"devices": devices}

# ══════════════════════════════════════════
# WebSocket（プロトコルシミュレーション）
# ══════════════════════════════════════════
connected_ws: Dict[str, WebSocket] = {}
vrrp_tasks:   Dict[str, asyncio.Task] = {}
rip_tasks:    Dict[str, asyncio.Task] = {}

@app.websocket("/ws/{device_id}")
async def websocket_endpoint(ws: WebSocket, device_id: str):
    await ws.accept()
    connected_ws[device_id] = ws
    state = device_sessions.get(device_id)

    # vnetにこの装置の表示用コールバックを登録
    # （プロトコルエンジンのログ/テーブル更新がこのWebSocket経由でフロントに届く）
    async def ws_send(msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    vnet.register(device_id, ws_send)

    try:
        await ws.send_json({"type": "connected", "device_id": device_id,
                            "mode": state.mode if state else "exec",
                            "hostname": state.hostname if state else device_id})
        while True:
            data = await ws.receive_json()
            await handle_ws_message(device_id, data, ws)
    except WebSocketDisconnect:
        pass
    finally:
        connected_ws.pop(device_id, None)
        vnet.unregister(device_id)
        # タイマー停止
        for task_dict in (vrrp_tasks, rip_tasks):
            if device_id in task_dict:
                task_dict[device_id].cancel()
                task_dict.pop(device_id, None)

async def handle_ws_message(device_id: str, msg: dict, ws: WebSocket):
    t = msg.get("type")
    state = device_sessions.get(device_id)
    if not state:
        return

    if t == "vrrp_start":
        # VRRPタイマー開始
        if device_id in vrrp_tasks:
            vrrp_tasks[device_id].cancel()
        vrrp_tasks[device_id] = asyncio.create_task(
            vrrp_loop(device_id, msg.get("group", 1), msg.get("vip", "192.168.1.254"),
                      int(msg.get("priority", 100)))
        )

    elif t == "vrrp_failover":
        # フェイルオーバー手動トリガー
        await trigger_vrrp_failover(device_id)

    elif t == "rip_start":
        if device_id in rip_tasks:
            rip_tasks[device_id].cancel()
        rip_tasks[device_id] = asyncio.create_task(
            rip_loop(device_id, msg.get("networks", []))
        )

    elif t == "ping":
        result = simulate_ping(msg.get("destination", "8.8.8.8"), state)
        await ws.send_json({"type": "ping_result", "result": result})

    elif t == "link_event":
        # リンクアップ/ダウンイベント
        event = msg.get("event", "up")
        iface = msg.get("interface", "GigabitEthernet1/0/1")
        log_msg = f"%LINK-3-UPDOWN: Interface {iface}, changed state to {event}"
        await ws.send_json({"type": "log", "message": log_msg,
                            "level": "info" if event == "up" else "warn"})
        if event == "down" and state.vrrp.get("state") == "Master":
            await asyncio.sleep(0.5)
            await trigger_vrrp_failover(device_id)

# ── VRRPループ ──
async def vrrp_loop(device_id: str, group: int, vip: str, priority: int):
    state = device_sessions.get(device_id)
    if not state:
        return
    # 優先度が高ければMaster
    new_state = "Master" if priority >= 100 else "Backup"
    state.vrrp["state"] = new_state
    state.vrrp["vip"] = vip
    state.vrrp["priority"] = priority

    ws = connected_ws.get(device_id)
    if ws:
        await ws.send_json({"type": "vrrp_state", "state": new_state, "vip": vip,
            "message": f"VRRP: lan0 vrid {group} state -> {new_state} (pri:{priority})"})

    # 1秒ごとにAdvertisement送信（Masterのみ）
    while True:
        await asyncio.sleep(1)
        if state.vrrp.get("state") != "Master":
            break
        ws = connected_ws.get(device_id)
        if ws:
            try:
                await ws.send_json({"type": "vrrp_advert", "group": group,
                                    "vip": vip, "priority": priority, "silent": True})
            except Exception:
                break

async def trigger_vrrp_failover(device_id: str):
    state = device_sessions.get(device_id)
    ws    = connected_ws.get(device_id)
    if not state or not ws:
        return
    old = state.vrrp.get("state", "Master")
    new = "Backup" if old == "Master" else "Master"
    state.vrrp["state"] = new
    msg = (f"%VRRP-6-STATECHANGE: lan0 Grp {state.vrrp.get('vrid',10)} state {old} -> {new}")
    await ws.send_json({"type": "vrrp_state", "state": new,
                        "vip": state.vrrp.get("vip"), "message": msg})
    if new == "Master":
        await ws.send_json({"type": "log", "level": "info",
            "message": f"Gratuitous ARP sent for {state.vrrp.get('vip')} "
                       f"(00:00:5e:00:01:{state.vrrp.get('vrid',10):02x})"})

# ── RIPループ ──
async def rip_loop(device_id: str, networks: list):
    state = device_sessions.get(device_id)
    if not state:
        return
    ws = connected_ws.get(device_id)
    if ws:
        await ws.send_json({"type": "rip_log",
            "message": f"RIPv2 開始: 広告ネットワーク {', '.join(networks)}"})
    # 30秒ごとにUpdateシミュレーション
    count = 0
    while True:
        await asyncio.sleep(30)
        count += 1
        ws = connected_ws.get(device_id)
        if not ws:
            break
        # ルート学習シミュレーション
        new_routes = []
        for net in networks:
            new_routes.append({"net": net, "metric": 1, "via": "direct"})
        # 他の装置から学習
        for other_id, other_state in device_sessions.items():
            if other_id != device_id and other_state.device_type == "sir":
                for r in other_state.rip_table:
                    if r["fp"] == "*C":
                        new_metric = r["metric"] + 1
                        if new_metric < 16:
                            new_routes.append({
                                "net": r["net"], "metric": new_metric,
                                "via": other_state.hostname
                            })
        if new_routes:
            state.rip_table = [
                {"fp": "*C" if r["via"]=="direct" else "*R",
                 "net": r["net"], "gw": "0.0.0.0" if r["via"]=="direct" else f"192.168.1.254",
                 "metric": r["metric"], "time": "none" if r["via"]=="direct" else f"00:{count%60:02d}",
                 "iface": "lan0"}
                for r in new_routes
            ]
            try:
                await ws.send_json({"type": "rip_update",
                    "message": f"RIP: Update #{count} — {len(new_routes)}エントリ",
                    "table": state.rip_table})
            except Exception:
                break

# ── ping シミュレーション ──
def simulate_ping(dest: str, state: DeviceState) -> str:
    rtts = [round(random.uniform(0.3, 3.0), 3) for _ in range(5)]
    lines = [f"PING {dest} ({dest}): 56 data bytes"]
    for i, r in enumerate(rtts):
        lines.append(f"64 bytes from {dest}: icmp_seq={i} ttl=64 time={r} ms")
    lines += ["",
        f"--- {dest} ping statistics ---",
        f"5 packets transmitted, 5 packets received, 0% packet loss",
        f"round-trip min/avg/max = {min(rtts)}/{round(sum(rtts)/len(rtts),3)}/{max(rtts)} ms"]
    return "\n".join(lines)

# ── Ollamaシステムプロンプト ──
def build_system_prompt(state: DeviceState) -> str:
    device_map = {
        "sir":      "Si-R G120（富士通/エフサステクノロジーズ）",
        "srs":      "SR-S324TR1（富士通/エフサステクノロジーズ）セキュアスイッチ",
        "catalyst": "Cisco Catalyst 9300 IOS-XE 17.9",
        "cisco":    "Cisco ISR4321 IOS 17.9",
    }
    model = device_map.get(state.device_type, state.device_type)
    return f"""あなたは{model}（ホスト名:{state.hostname}）のCLIエミュレーターです。
実機と同じCLI出力を返してください。
現在モード: {state.mode}
プロンプトは出力に含めない。マークダウン不使用。プレーンテキストのみ。"""

# ══════════════════════════════════════════
# 静的ファイル配信
# ══════════════════════════════════════════
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)

@app.get("/")
async def root():
    index = static_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html が見つかりません</h1>")

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
