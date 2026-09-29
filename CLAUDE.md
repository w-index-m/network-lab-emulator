# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-vendor network device CLI/protocol emulator. It's not a "show text
generator" — several protocols run as real sockets/real packets (OSPF over
raw IP proto 89, BGP over TCP 179, RIPv2 over UDP 520, SNMP over UDP 161,
NETCONF/SSH-CLI/Telnet-CLI, gNMI over gRPC), so real clients (ncclient,
gnmic/pygnmi, snmpwalk, an actual ssh/telnet client) can connect to emulated
devices. See `README.md` for the full feature/protocol matrix and supported
device types (`device_type`: `catalyst`, `nexus`, `cisco`, `asa`, `sir`,
`srs`, `apresia`, `bigip`, `ipcom`, `pc`).

**Publicly deployed** at https://network-lab-emulator.onrender.com/ (Render).
That deployment doesn't have Ollama available, so its `network_ontology_query.py
--summarize`-style AI-summary features show "Ollamaが検出されませんでした。
ollama pull llama3 実行後、python app.py を再起動してください。" — expected
there, not a bug to chase from this sandbox.

**このプロジェクトは https://network-lab-emulator.onrender.com/ (Render) で
一般公開している。** そのデプロイ環境にはOllamaが無いため、
`network_ontology_query.py --summarize`のようなAI要約機能では
「Ollamaが検出されませんでした。ollama pull llama3 実行後、python app.py
を再起動してください。」という表示が出るのが正常(このサンドボックスから
追いかけて直すべきバグではない)。

## Commands

```bash
# Run the app
python app.py                                    # http://localhost:8000
NETLAB_AUTH_DISABLE=1 python app.py               # skip login (admin/admin by default)

# Full regression suite (~15 min, ~1350+ tests) — run this before every push
pytest tests/ -q -p no:cacheprovider

# Single file / single test
pytest tests/test_ospf_neighbor.py -v -p no:cacheprovider
pytest tests/test_ospf_neighbor.py::test_name -v -p no:cacheprovider

# Lint (ruff.toml present)
ruff check .
```

Real protocol listeners (raw OSPF sockets, TCP 830/22/23, UDP 161, etc.)
need elevated privileges to bind; without them the app still starts and CLI
emulation still works, just without the real-socket protocol agents.

## Architecture

### Two-layer CLI dispatch — read this before touching command handling

`app.py`'s `cli_command()` docstring (around the top of the function) spells
out the dispatch order; it is the single most important thing to understand
before changing how a command is parsed:

1. `handle_protocol_show(...)` (app.py) — dynamic `show` output backed by
   live protocol-engine state (RIP/OSPF/BGP/rib_engine/etc.)
2. `handle_protocol_config(...)` (app.py) — config commands that mutate
   protocol-engine state (adds a static route to `rib_engine`, starts
   `rip_engine`/`ospf_engine`, etc.)
3. `engine/rules.py`'s `RuleEngine.process(...)` — the catch-all for
   whatever the above two didn't handle: vendor-specific canned
   responses, mode transitions, completion, help text.

**The first layer to return non-None/truthy wins — layers below it never
run for that command.** This means:
- If you add a command handler in `app.py`, the equivalent code in
  `rules.py` (if any) becomes dead for that command. Check `app.py` first
  when a `rules.py` change "does nothing."
- Conversely, several vendor-specific CLI regexes in `handle_protocol_config`
  key off `state._routing_mode` (a side-channel attribute set by e.g.
  `router rip`/`router ospf`) rather than `state.device_type`, so a new
  device type's `router rip`/`network ...` commands can "just work" for
  free without reimplementing RIP/OSPF — see how `device_type == "ipcom"`
  was wired up in `engine/rules.py` (`_ipcom_process`) for the pattern:
  transition the mode, then let the already-executed `app.py` layer's
  side effects stand rather than re-deriving state locally. Duplicating
  state between the two layers (e.g. a device-specific `state.static_routes`
  list next to the shared `rib_engine`) has caused real duplicate/garbled
  output bugs in this codebase — prefer delegating to the shared engine.

### Device-specific CLI dialects: dedicated `_xxx_process` handlers

`RuleEngine.process()` dispatches entirely different CLI grammars (`pc`,
`asa`, `apresia`, `bigip`, `ipcom`) to a self-contained `_xxx_process(cmd, c,
state)` method before falling into the shared Cisco/IOS-style command tree
that `catalyst`/`cisco`/`srs`/`sir`/`nexus` share. When adding a device
whose CLI paradigm doesn't fit the Cisco `exec → config → config-if` mode
model, follow this pattern rather than bolting new device-type checks onto
the generic Cisco branch.

### Config sub-modes (Cisco-style devices)

`CONFIG_SUBMODES` in `engine/rules.py` is a single registry that both (a)
the "which modes accept config commands" allow-list and (b) `exit`'s
"which mode does this one return to" table are derived from. Previously
these had to be kept in sync by hand in two places, which was a real
source of "% Invalid input detected" bugs when adding a new sub-mode.
**Add exactly one line to `CONFIG_SUBMODES`** when introducing a new
config sub-mode; don't hand-edit the allow-list or the exit table
separately. `tests/test_config_submodes.py` exhaustively checks the
registry's internal consistency (every submode's parent is reachable, no
cycles, etc.) — device-specific handlers like `_ipcom_process` that manage
their own mode machine (not via `CONFIG_SUBMODES`) are exempt.

### Key modules

- `app.py` — FastAPI app, HTTP/WebSocket API, the two dispatch layers
  above, `DEFAULT_DEVICES`, device session management (`device_sessions:
  Dict[device_id, DeviceState]`).
- `engine/rules.py` — `DeviceState` (per-device mutable state,
  `__init__` branches by `device_type`) and `RuleEngine` (CLI command
  processing, ~11k lines, one `_xxx_process`/`_xxx_show_*` family per
  device type plus the shared Cisco-style tree).
- `engine/protocols.py` — the shared protocol engines (RIP/OSPF/BGP/EIGRP/
  STP/VRRP/etc. state machines and their `rib_engine`-style route tables).
  These are shared across device types; don't duplicate their state.
- `engine/real_*_agent.py`, `engine/snmp_udp_agent.py`,
  `engine/netconf_agent.py`, `engine/ssh_cli_agent.py`,
  `engine/telnet_cli_agent.py`, `engine/gnmi_agent.py` — the real-socket
  protocol listeners mentioned above.
- `static/index.html` — the browser CLI/topology UI. Device-type-specific
  frontend bits (prompt suffix in `getPrompt()`, interface-name templates
  in `defPort()`/`portOptions()`, the "add device" buttons, the vendor
  auto-detect regex in `_inferDeviceType()`) need updating in parallel
  with backend device-type additions, or the new device type won't be
  selectable/won't render its prompt correctly in the UI.
- `tools/` — operational scripts (Prometheus exporter, Grafana/Loki stack
  setup for both Linux `.sh` and Windows `.ps1`, syslog→Loki bridge,
  LogicMonitor mock, network ontology query tool, etc.) — see `docs/*.md`
  for the write-up of each.
- `docs/` — one markdown file per feature area, written with an actual
  live-verification transcript (real commands run against a real running
  instance, not just code review) rather than just a design description.
  Follow that pattern for new docs.

## Working conventions for this repo (established in this session)

- Development happens on `claude/affectionate-johnson-rz69wa`; push there
  directly unless told otherwise.
- **Run the full `pytest tests/ -q -p no:cacheprovider` suite before every
  push**, in the background if it's the ~15-minute full run, and confirm
  the only failures are the two known-flaky
  `tests/test_ipsec_dpd_timers.py::TestSirConfigParsing` tests (pass in
  isolation) — anything else is a real regression to fix, not to wave off.
- Don't run a live multi-service stack (the monitoring stack's `setup`,
  a live app.py instance binding real protocol ports) concurrently with
  the regression suite — the real BGP/OSPF/SNMP port binding in both
  collides and produces spurious `ERROR`/`ports already in use` failures
  that look like real breakage but aren't; stop one before running the
  other.
- Live-verify new features against a real running instance
  (`NETLAB_AUTH_DISABLE=1 python -m uvicorn app:app ...` + `curl`/`/api/cli`)
  before writing it up — this has repeatedly caught real bugs (duplicate
  route entries, a device-specific CLI quirk like IPCOM's `router ospf`
  taking no process-id, Windows-only `UnicodeEncodeError`s) that code
  review alone missed.

## ML / anomaly detection notes（機械学習・異常検知メモ）

PyTorch could not be installed in this sandbox early on (pulled in a
multi-GB CUDA/cuDNN toolkit that exhausted the ~2.6GB free disk space at
the time), so the existing ML-ish tools are plain NumPy:
`tools/anomaly_autoencoder.py` (reconstruction-error-based anomaly
detection) and `tools/link_capacity_forecast.py` (traffic forecasting).
This turned out to be enough — the data volume/complexity this emulator
produces doesn't need a deep-learning framework. `tools/network_ontology_query.py
--summarize` separately does AI *interpretation* of retrieved logs via
Ollama (not anomaly detection).

Re-checked later in the session (disk had since grown to ~27GB free):
disk space is no longer the blocker, but `pip install torch` from the
default PyPI index now pulls in the full CUDA/cuDNN wheel set regardless
(`nvidia-cudnn-cu13`, `cuda-toolkit`, `triton` ~248MB, etc. — CPU-only
PyPI wheels for CUDA-capable platforms don't seem to exist for this
Python/platform combo), and the official CPU-only wheel index
(`download.pytorch.org`) is blocked by this environment's egress policy
(403 on CONNECT). So PyTorch remains impractical here for a different
reason than before — decided to keep the NumPy implementation rather than
install the CUDA stack for a CPU-only, GPU-less sandbox.

このサンドボックスではPyTorchが当初インストールできなかった（数GB規模の
CUDA/cuDNNツールチェーンを引き込み、当時の空きディスク容量(~2.6GB)を
使い切るため）。そのため既存の機械学習系ツールはNumPyのみで実装している:
`tools/anomaly_autoencoder.py`（再構成誤差ベースの異常検知）と
`tools/link_capacity_forecast.py`（トラフィック予測）。結果的にこれで
十分だった — このエミュレータが生成するデータの量・複雑さは深層学習
フレームワークを必要としない。`tools/network_ontology_query.py
--summarize`は別途、取得したログをOllama経由でAI要約する機能
（異常検知ではない）。

セッション後半で再確認したところ(ディスク空き容量は~27GBまで増えていた)、
容量自体はもう制約にならないが、デフォルトのPyPIから`pip install torch`
すると依然としてCUDA/cuDNN一式(`nvidia-cudnn-cu13`, `cuda-toolkit`,
`triton`約248MB等)が付いてくる（このPython/プラットフォームの組み合わせ
向けにはCPU専用のPyPIホイールが無い模様）。かつCPU専用ホイールの配布元
`download.pytorch.org`はこの環境のegressポリシーでブロックされている
(CONNECTに403)。つまり別の理由でPyTorchは依然として実用的でない —
GPUの無いこのサンドボックスのためにCUDA一式を入れるより、NumPy実装を
維持する判断をした。

**PyTorch installed via a GitHub Release mirror workaround**: the user
downloaded the official CPU-only wheel
(`torch-2.6.0+cpu-cp311-cp311-linux_x86_64.whl`) from
`download.pytorch.org/whl/cpu/` on their own machine (not blocked there)
and uploaded it as a GitHub Release asset on this repo
(`w-index-m/network-lab-emulator`, release tag `torch-cpu-wheel`) —
`github.com`/`objects.githubusercontent.com` are *not* blocked by this
environment's egress policy, only `download.pytorch.org` is. That
Release asset URL is now pinned in `requirements-ml.txt` as a direct PEP
508 URL reference (`torch @ https://github.com/.../torch-2.6.0%2Bcpu-...whl`)
— install it with `pip install -r requirements-ml.txt`. It's intentionally
kept out of `requirements.txt`/`requirements-dev.txt` since the core app
and test suite don't need it (still true even now that torch is
installable) and the pin is Python 3.11/linux_x86_64-specific, not
portable to other environments without re-uploading a matching wheel.
Live-verified: `import torch; torch.nn.Linear(3,3)(torch.randn(3,3))`
runs successfully, `torch.__version__` == `2.6.0+cpu`.

**`tools/anomaly_autoencoder_torch.py`**: with torch now installable, the
user's stated goal was "use torch as a tech-stack foundation for trying
more complex models later" rather than an immediate feature need, so
rather than rewriting the working NumPy implementation, this adds a
parallel torch-backed twin of `tools/anomaly_autoencoder.py`'s
`DeviceAnomalyModel` (same public API: `fit`/`score`/`is_anomaly`/
`threshold`, same 1-hidden-layer/tanh architecture, same mean+k*std
threshold) using `torch.nn.Module`/`torch.optim.SGD` instead of hand-
written NumPy forward/backward passes. The NumPy version stays as the
default/torch-free implementation; this is the base to build a more
complex model (e.g. an LSTM for `link_capacity_forecast.py`-style time
series) on top of, once there's a concrete need. Live-verified via its
own `_demo()` (mirrors `anomaly_autoencoder.py`'s): reproduces the same
qualitative result (flags the CPU=55% case a fixed 80% threshold would
miss). `tests/test_anomaly_autoencoder_torch.py` mirrors
`tests/test_anomaly_autoencoder.py`'s test cases 1:1 against the torch
backend, `pytest.importorskip('torch')`'d so the file skips cleanly on
environments without `requirements-ml.txt` installed — 8/8 passed here.

**`tools/anomaly_autoencoder_torch.py`**: torchが導入可能になったことを
受けて、ユーザーの目的が「今すぐ機能として必要」ではなく「将来もっと
複雑なモデルを試すための技術スタックの土台」だったため、動いている
NumPy実装を書き換えるのではなく、`tools/anomaly_autoencoder.py`の
`DeviceAnomalyModel`と同じ公開API(`fit`/`score`/`is_anomaly`/
`threshold`、同じ1隠れ層/tanh構成、同じ平均+k*標準偏差しきい値)を
持つtorchバックエンド版を並行して追加した(`torch.nn.Module`/
`torch.optim.SGD`を使い、手書きNumPy版の前方伝播・逆伝播を置き換え)。
NumPy版はtorch不要のデフォルト実装として維持し、こちらは今後もっと
複雑なモデル(例: `link_capacity_forecast.py`的な時系列予測をLSTM化
する等)を試す際の土台という位置づけ。自身の`_demo()`
(`anomaly_autoencoder.py`のものと対応)で実際に動作確認済み:
固定80%閾値では見逃すCPU=55%のケースを同様に検知するなど、定性的に
同じ結果を再現。`tests/test_anomaly_autoencoder_torch.py`は
`tests/test_anomaly_autoencoder.py`のテストケースをそのままtorch
バックエンド向けに対応させたもので、`pytest.importorskip('torch')`
により`requirements-ml.txt`未導入の環境ではこのファイルごと
きれいにskipされる — ここでは8/8成功。

**PyTorchをGitHub Releaseミラー経由でインストール済み**: ユーザーが
`download.pytorch.org/whl/cpu/`（手元の環境ではブロックされていない）
から公式CPU専用ホイール
(`torch-2.6.0+cpu-cp311-cp311-linux_x86_64.whl`)をダウンロードし、この
リポジトリ(`w-index-m/network-lab-emulator`)のGitHub Releaseアセット
(タグ`torch-cpu-wheel`)としてアップロードした — `github.com`/
`objects.githubusercontent.com`はこの環境のegressポリシーでブロック
されて**いない**（ブロックされているのは`download.pytorch.org`のみ）。
そのReleaseアセットのURLを`requirements-ml.txt`にPEP 508形式の直接URL
参照としてピン留めしてある(`torch @ https://github.com/.../
torch-2.6.0%2Bcpu-...whl`) — `pip install -r requirements-ml.txt`で
導入できる。本体アプリ・テストスイートは(torchが導入可能になった今でも)
torchを必要としないため、意図的に`requirements.txt`/`requirements-dev.txt`
には含めていない。またこのピン留めはPython 3.11/linux_x86_64専用で、
他環境ではそのままでは使えない（対象環境向けのwhlを同じ手順で再度
アップロードしてURLを差し替える必要がある）。実際に動作確認済み:
`import torch; torch.nn.Linear(3,3)(torch.randn(3,3))`が正常に動作、
`torch.__version__` は `2.6.0+cpu`。

**Implemented**: `tools/syslog_anomaly_detector.py` applies exactly this —
a 1D simplification of `anomaly_autoencoder.py`'s mean+k*std threshold
approach to Loki-ingested syslog message counts per time bucket per
device, flags a spike, and can feed the flagged window into
`network_ontology_query.py`'s `summarize_logs_via_ollama()` for an AI
explanation (`--summarize`). Live-verified end-to-end against a real
running Loki + a real device generating a real syslog burst — see
`docs/syslog-anomaly-detection.md`.

**実装済み**: `tools/syslog_anomaly_detector.py`がまさにこれを実装した
— `anomaly_autoencoder.py`の平均+k*標準偏差しきい値方式を1次元
（装置ごと・時間バケットごとのsyslogメッセージ件数）に単純化して適用し、
急増を検知したら`network_ontology_query.py`の
`summarize_logs_via_ollama()`に渡してAI要約もできる（`--summarize`）。
実際に動いているLoki＋実装置が生成した実syslogバーストで
エンドツーエンド検証済み。詳細は`docs/syslog-anomaly-detection.md`参照。
