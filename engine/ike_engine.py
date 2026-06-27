"""
IKE/IPsec ネゴシエーションエンジン

対応デバイス間の IKE Phase1/Phase2 ネゴシエーションをシミュレートする。
  - Si-R ↔ Si-R
  - Si-R ↔ Cisco IOS
  - Si-R ↔ Cisco ASA
  - Cisco IOS ↔ Cisco IOS
両ルーターの設定を突き合わせて一致確認・状態遷移を行う。
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

DEFAULT_ENCRYPTION = "aes256"
DEFAULT_HASH       = "sha256"
DEFAULT_DH         = 14
DEFAULT_IKE_LT     = 86400
DEFAULT_SA_LT      = 28800
DEFAULT_MODE       = "main"
DEFAULT_PROTOCOL   = "esp"

# Cisco IOS 暗号名 → 正規化マッピング
_ENC_NORM = {
    "aes": "aes128", "aes 128": "aes128", "aes128": "aes128",
    "aes 256": "aes256", "aes256": "aes256", "aes-256": "aes256",
    "aes 192": "aes192", "aes192": "aes192",
    "3des": "3des", "des": "des",
    "esp-aes": "aes128", "esp-aes-256": "aes256", "esp-3des": "3des",
}
_HASH_NORM = {
    "sha": "sha1", "sha1": "sha1", "sha256": "sha256", "sha-256": "sha256",
    "sha512": "sha512", "md5": "md5",
    "esp-sha-hmac": "sha1", "esp-sha256-hmac": "sha256",
}
_DH_NORM = {
    "1": 1, "2": 2, "5": 5, "14": 14, "19": 19, "20": 20, "21": 21, "24": 24,
}

def _norm_enc(v: str) -> str:
    return _ENC_NORM.get(str(v).lower(), str(v).lower())

def _norm_hash(v: str) -> str:
    return _HASH_NORM.get(str(v).lower(), str(v).lower())

def _norm_dh(v) -> int:
    try:
        return int(v)
    except Exception:
        return _DH_NORM.get(str(v), DEFAULT_DH)


@dataclass
class NegotiationResult:
    success: bool
    phase: int
    reason: str
    logs: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════
# デバイス種別ごとの設定アダプタ
# ══════════════════════════════════════════════════════════

def _sir_tunnel_info(state, local_ip: str, remote_ip: str) -> Optional[dict]:
    """Si-R の ipsec_tunnels から該当トンネルを返す"""
    for t in getattr(state, 'ipsec_tunnels', {}).values():
        if t.get('local_ip') == local_ip and t.get('remote_ip') == remote_ip:
            return t
    return None

def _cisco_peer_info(state, local_ip: str, remote_ip: str) -> Optional[dict]:
    """
    Cisco IOS/ASA の crypto map + isakmp key から
    Si-R の tunnel dict 相当の dict を返す。
    """
    crypto = getattr(state, 'ipsec_crypto', {})
    peers  = getattr(state, 'ipsec_peers',  {})   # ASA tunnel-group

    # PSK: ASA は ipsec_peers[remote_ip]['preshared']
    #       IOS は ipsec_crypto['isakmp_keys'][remote_ip]
    psk = (peers.get(remote_ip, {}).get('preshared') or
           crypto.get('isakmp_keys', {}).get(remote_ip, ''))

    # IKE ポリシー（最初の 1 つを使用）
    ike_pols = crypto.get('isakmp_policies', {})
    ike_pol  = next(iter(ike_pols.values()), {}) if ike_pols else {}

    # Transform-set（最初の 1 つ）
    ts_map = crypto.get('transform_sets', {})
    ts     = next(iter(ts_map.values()), {}) if ts_map else {}

    # Crypto map で remote_ip を参照しているエントリを探す
    cmap_entry = {}
    for mapname, seqs in crypto.get('crypto_maps', {}).items():
        for seq, entry in seqs.items():
            if entry.get('peer') == remote_ip:
                cmap_entry = entry
                break

    # isakmp enable が設定されているか
    isakmp_enabled = bool(crypto.get('isakmp_enabled'))

    # transform の解析 (Phase2用, e.g. ["esp-aes-256","esp-sha-hmac"])
    transforms = ts.get('transforms', [])
    ph2_enc  = next((_norm_enc(t) for t in transforms if 'aes' in t or '3des' in t or 'des' in t), None)
    ph2_hsh  = next((_norm_hash(t) for t in transforms if 'sha' in t or 'md5' in t), None)

    # Phase1 用は IKE ポリシーから取得
    ph1_enc = _norm_enc(ike_pol.get('encryption', DEFAULT_ENCRYPTION))
    ph1_hsh = _norm_hash(ike_pol.get('hash', DEFAULT_HASH))

    return {
        'local_ip':   local_ip,
        'remote_ip':  remote_ip,
        'preshared':  psk,
        'encryption': ph1_enc,
        'hash':       ph1_hsh,
        'dh_group':   _norm_dh(ike_pol.get('group', DEFAULT_DH)),
        'ike_mode':   DEFAULT_MODE,
        'ike_lifetime': ike_pol.get('lifetime', DEFAULT_IKE_LT),
        'protocol':   DEFAULT_PROTOCOL,
        'ipsec_type': 'ike',
        'isakmp_enabled': isakmp_enabled,
        'cmap_entry': cmap_entry,
    }

def _get_local_ip(state) -> str:
    """デバイスの最初の有効 IP を返す（peer 探索用）"""
    for _, info in state.interfaces.items():
        ip = info.get('ip', '')
        if ip and ip != '127.0.0.1':
            return ip
    return ''


# ══════════════════════════════════════════════════════════
# ピア探索
# ══════════════════════════════════════════════════════════

def _find_peer(device_sessions: dict, local_id: str, remote_ip: str):
    """
    remote_ip をローカルIPとして持つ対向デバイスを探す。
    Si-R, Cisco IOS, ASA を対象にする。
    戻り値: (peer_id, peer_state) or (None, None)
    """
    for dev_id, state in device_sessions.items():
        if dev_id == local_id:
            continue
        dt = state.device_type
        if dt in ('sir', 'srs'):
            for t in getattr(state, 'ipsec_tunnels', {}).values():
                if t.get('local_ip') == remote_ip:
                    return dev_id, state
        elif dt in ('cisco', 'catalyst', 'asa'):
            for _, info in state.interfaces.items():
                if info.get('ip') == remote_ip:
                    return dev_id, state
    return None, None


# ══════════════════════════════════════════════════════════
# Phase 一致確認
# ══════════════════════════════════════════════════════════

def _phase1_check(local_d: dict, peer_d: dict) -> Tuple[bool, str]:
    l_psk = local_d.get('preshared', '')
    p_psk = peer_d.get('preshared', '')
    if not l_psk:
        return False, "Phase1 failed: preshared-key not configured (local)"
    if not p_psk:
        return False, "Phase1 failed: preshared-key not configured (peer)"
    if l_psk != p_psk:
        return False, "Phase1 failed: preshared-key mismatch"

    l_dh = _norm_dh(_get_val(local_d, 'dh_group', DEFAULT_DH))
    p_dh = _norm_dh(_get_val(peer_d,  'dh_group', DEFAULT_DH))
    if l_dh != p_dh:
        return False, f"Phase1 failed: DH group mismatch (local={l_dh}, peer={p_dh})"

    l_enc = _norm_enc(_get_val(local_d, 'encryption', DEFAULT_ENCRYPTION))
    p_enc = _norm_enc(_get_val(peer_d,  'encryption', DEFAULT_ENCRYPTION))
    if l_enc != p_enc:
        return False, f"Phase1 failed: encryption mismatch (local={l_enc}, peer={p_enc})"

    l_hsh = _norm_hash(_get_val(local_d, 'hash', DEFAULT_HASH))
    p_hsh = _norm_hash(_get_val(peer_d,  'hash', DEFAULT_HASH))
    if l_hsh != p_hsh:
        return False, f"Phase1 failed: hash mismatch (local={l_hsh}, peer={p_hsh})"

    l_mode = _get_val(local_d, 'ike_mode', DEFAULT_MODE)
    p_mode = _get_val(peer_d,  'ike_mode', DEFAULT_MODE)
    if l_mode != p_mode:
        return False, f"Phase1 failed: IKE mode mismatch (local={l_mode}, peer={p_mode})"

    return True, ""


def _phase2_check(local_d: dict, peer_d: dict) -> Tuple[bool, str]:
    l_proto = _get_val(local_d, 'protocol', DEFAULT_PROTOCOL).lower()
    p_proto = _get_val(peer_d,  'protocol', DEFAULT_PROTOCOL).lower()
    if l_proto != p_proto:
        return False, f"Phase2 failed: protocol mismatch (local={l_proto}, peer={p_proto})"
    return True, ""


def _get_val(d: dict, key: str, default):
    v = d.get(key)
    return v if v is not None else default


# ══════════════════════════════════════════════════════════
# メインネゴシエーション
# ══════════════════════════════════════════════════════════

def negotiate_ipsec(device_sessions: dict, initiator_id: str) -> Dict[str, NegotiationResult]:
    """
    initiator_id が持つ IPsec トンネルに対してネゴシエーションを実行。
    Si-R, Cisco IOS, ASA を対向として対応。
    """
    results = {}
    initiator = device_sessions.get(initiator_id)
    if not initiator:
        return results

    dt = initiator.device_type

    # ── Si-R / SR-S ──────────────────────────────────────
    if dt in ('sir', 'srs'):
        tunnels   = getattr(initiator, 'ipsec_tunnels', {})
        ike_ok    = getattr(initiator, 'ike_enabled', False)
        ipsec_ok  = getattr(initiator, 'ipsec_enabled', False)

        for tid, local_t in tunnels.items():
            key  = str(tid)
            logs = []

            remote_ip = local_t.get('remote_ip', '')
            local_ip  = local_t.get('local_ip', '')
            if not remote_ip or not local_ip:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL', 'phase2': 'LARVAL'})
                results[key] = NegotiationResult(False, 1, "Tunnel IP not configured", logs)
                continue

            logs.append(f"IKE: Initiating to {remote_ip} from {local_ip}")

            if not ike_ok:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL'})
                logs.append("Error: ike use on not set")
                results[key] = NegotiationResult(False, 1, "ike use on not set", logs)
                continue
            if not ipsec_ok:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL'})
                logs.append("Error: ipsec use on not set")
                results[key] = NegotiationResult(False, 1, "ipsec use on not set", logs)
                continue

            peer_id, peer_state = _find_peer(device_sessions, initiator_id, remote_ip)
            if not peer_id:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL'})
                logs.append(f"IKE: No response from {remote_ip}")
                results[key] = NegotiationResult(False, 1, f"Peer {remote_ip} not found", logs)
                continue

            # 対向の tunnel/crypto dict を取得
            pdt = peer_state.device_type
            if pdt in ('sir', 'srs'):
                peer_d = _sir_tunnel_info(peer_state, remote_ip, local_ip)
                peer_ike_ok = getattr(peer_state, 'ike_enabled', False)
                peer_ipsec_ok = getattr(peer_state, 'ipsec_enabled', False)
            else:  # Cisco IOS / ASA
                peer_d = _cisco_peer_info(peer_state, remote_ip, local_ip)
                peer_ike_ok   = bool(peer_d and peer_d.get('isakmp_enabled'))
                peer_ipsec_ok = True  # crypto map があれば OK

            if not peer_d:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL'})
                logs.append(f"IKE: No response from {remote_ip} — peer tunnel not configured")
                results[key] = NegotiationResult(False, 1, "Peer tunnel config missing", logs)
                continue

            if not peer_ike_ok:
                local_t.update({'status': 'wait', 'phase1': 'LARVAL'})
                logs.append(f"IKE: No response from {remote_ip} — peer IKE not enabled")
                results[key] = NegotiationResult(False, 1, "Peer IKE not enabled", logs)
                continue

            # Phase 1
            enc  = _norm_enc(_get_val(local_t, 'encryption', DEFAULT_ENCRYPTION))
            dh   = _norm_dh(_get_val(local_t, 'dh_group', DEFAULT_DH))
            mode = _get_val(local_t, 'ike_mode', DEFAULT_MODE)
            logs.append(f"IKE: Phase1 [{mode} mode] enc={enc} dh={dh} ...")
            ok1, r1 = _phase1_check(local_t, peer_d)
            if not ok1:
                local_t.update({'status': 'wait', 'phase1': 'DYING'})
                if pdt in ('sir', 'srs') and peer_d:
                    peer_d['phase1'] = 'DYING'
                logs.append(f"IKE: Phase1 FAILED — {r1}")
                results[key] = NegotiationResult(False, 1, r1, logs)
                continue

            lt = _get_val(local_t, 'ike_lifetime', DEFAULT_IKE_LT)
            local_t.update({'phase1': 'MATURE', 'ike_lifetime_remaining': lt})
            if pdt in ('sir', 'srs') and peer_d:
                peer_d.update({'phase1': 'MATURE', 'ike_lifetime_remaining': lt})
            logs.append(f"IKE: Phase1 established (lifetime={lt}s)")

            # Phase 2
            proto = _get_val(local_t, 'protocol', DEFAULT_PROTOCOL)
            logs.append(f"IKE: Phase2 [Quick mode] proto={proto.upper()} ...")
            ok2, r2 = _phase2_check(local_t, peer_d)
            if not ok2:
                local_t.update({'status': 'wait', 'phase2': 'DYING'})
                logs.append(f"IKE: Phase2 FAILED — {r2}")
                results[key] = NegotiationResult(False, 2, r2, logs)
                continue

            spi_seed = abs(hash(local_ip + remote_ip + str(tid)))
            spi_in   = f'0x{(spi_seed        & 0xffffffff):08x}'
            spi_out  = f'0x{((spi_seed >> 4) & 0xffffffff):08x}'
            sa_lt    = _get_val(local_t, 'sa_lifetime', DEFAULT_SA_LT)

            local_t.update({
                'status': 'established', 'phase2': 'MATURE',
                'spi_in': spi_in, 'spi_out': spi_out,
                'sa_lifetime_remaining': sa_lt, 'neg_time': time.time(),
            })
            if pdt in ('sir', 'srs') and peer_d:
                peer_d.update({
                    'status': 'established', 'phase2': 'MATURE',
                    'spi_in': spi_out, 'spi_out': spi_in,
                    'sa_lifetime_remaining': sa_lt, 'neg_time': time.time(),
                })
            # Cisco 側に IPsec SA を記録
            if pdt in ('cisco', 'catalyst', 'asa'):
                peer_state.ipsec_peers.setdefault(local_ip, {}).update({
                    'status': 'established',
                    'spi_in': spi_out, 'spi_out': spi_in,
                    'phase1': 'MATURE', 'phase2': 'MATURE',
                    'sa_lifetime_remaining': sa_lt,
                    'neg_time': time.time(),
                })

            logs.append(f"IKE: Phase2 established — SPI(in)={spi_in} SPI(out)={spi_out}")
            logs.append(f"IPsec: Tunnel {tid} UP ({'Si-R' if dt == 'sir' else 'SR-S'} ↔ {pdt.upper()})")
            results[key] = NegotiationResult(True, 2, "OK", logs)

    # ── Cisco IOS / ASA からのネゴシエーション開始 ──────
    elif dt in ('cisco', 'catalyst', 'asa'):
        crypto = getattr(initiator, 'ipsec_crypto', {})
        peers  = getattr(initiator, 'ipsec_peers',  {})

        # すべての peer を列挙
        all_peers = set(peers.keys())
        for mapname, seqs in crypto.get('crypto_maps', {}).items():
            for seq, entry in seqs.items():
                if entry.get('peer'):
                    all_peers.add(entry['peer'])

        local_ip = _get_local_ip(initiator)

        for remote_ip in all_peers:
            key  = remote_ip
            logs = []
            logs.append(f"IKE: Initiating to {remote_ip} from {local_ip}")

            if not crypto.get('isakmp_enabled'):
                logs.append("Error: crypto isakmp enable not set")
                results[key] = NegotiationResult(False, 1, "crypto isakmp enable not set", logs)
                continue

            local_d = _cisco_peer_info(initiator, local_ip, remote_ip)
            if not local_d or not local_d.get('preshared'):
                logs.append(f"Error: PSK not configured for {remote_ip}")
                results[key] = NegotiationResult(False, 1, "PSK not configured", logs)
                continue

            peer_id, peer_state = _find_peer(device_sessions, initiator_id, remote_ip)
            if not peer_id:
                logs.append(f"IKE: No response from {remote_ip}")
                results[key] = NegotiationResult(False, 1, f"Peer {remote_ip} not found", logs)
                continue

            pdt = peer_state.device_type
            if pdt in ('sir', 'srs'):
                peer_d = _sir_tunnel_info(peer_state, remote_ip, local_ip)
                peer_ike_ok = getattr(peer_state, 'ike_enabled', False)
            else:
                peer_d = _cisco_peer_info(peer_state, remote_ip, local_ip)
                peer_ike_ok = bool(peer_d and peer_d.get('isakmp_enabled'))

            if not peer_d or not peer_ike_ok:
                logs.append(f"IKE: No response — peer not ready")
                results[key] = NegotiationResult(False, 1, "Peer not ready", logs)
                continue

            enc  = _norm_enc(local_d.get('encryption', DEFAULT_ENCRYPTION))
            dh   = _norm_dh(local_d.get('dh_group', DEFAULT_DH))
            logs.append(f"IKE: Phase1 [main mode] enc={enc} dh={dh} ...")
            ok1, r1 = _phase1_check(local_d, peer_d)
            if not ok1:
                logs.append(f"IKE: Phase1 FAILED — {r1}")
                results[key] = NegotiationResult(False, 1, r1, logs)
                continue

            lt = local_d.get('ike_lifetime', DEFAULT_IKE_LT)
            logs.append(f"IKE: Phase1 established (lifetime={lt}s)")

            ok2, r2 = _phase2_check(local_d, peer_d)
            if not ok2:
                logs.append(f"IKE: Phase2 FAILED — {r2}")
                results[key] = NegotiationResult(False, 2, r2, logs)
                continue

            spi_seed = abs(hash(local_ip + remote_ip))
            spi_in   = f'0x{(spi_seed        & 0xffffffff):08x}'
            spi_out  = f'0x{((spi_seed >> 4) & 0xffffffff):08x}'
            sa_lt    = local_d.get('sa_lifetime', DEFAULT_SA_LT)

            initiator.ipsec_peers.setdefault(remote_ip, {}).update({
                'status': 'established', 'phase1': 'MATURE', 'phase2': 'MATURE',
                'spi_in': spi_in, 'spi_out': spi_out,
                'sa_lifetime_remaining': sa_lt, 'neg_time': time.time(),
            })
            if pdt in ('sir', 'srs') and peer_d:
                peer_d.update({
                    'status': 'established', 'phase1': 'MATURE', 'phase2': 'MATURE',
                    'spi_in': spi_out, 'spi_out': spi_in,
                    'sa_lifetime_remaining': sa_lt, 'neg_time': time.time(),
                })

            logs.append(f"IKE: Phase2 established — SPI(in)={spi_in} SPI(out)={spi_out}")
            logs.append(f"IPsec: SA UP ({dt.upper()} ↔ {pdt.upper()})")
            results[key] = NegotiationResult(True, 2, "OK", logs)

    return results
