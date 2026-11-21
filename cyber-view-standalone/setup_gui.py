"""
gNMI で装置を見るための設定画面(コマンドを打たずに使う)

  python setup_gui.py            → http://localhost:8092/ を開く
  (Windows は start-nirvana-gnmi.bat をダブルクリック)

画面で入れるのは装置の IP(と必要ならポート・ユーザー・パスワード)だけ。ボタンを押すと:
  1. gnmi-quickstart.sh up(gnmic と Prometheus の用意・設定・起動・確認)
     Windows では WSL の中で動かす(gnmic は Linux 版しか無いため)
  2. ニルヴァーナ(server.py)の設定を書いて、8091 番で起動する
  3. 結果と、うまくいかないときの原因の目安を画面に出す

標準ライブラリだけで動く(ニルヴァーナ本体は requirements.txt が要る)。
パスワードは画面からこの機械の中だけで受け渡し、ファイルには gnmic の設定(本人だけ読める)にしか書かない。
"""
import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUICKSTART = HERE / 'gnmi-quickstart.sh'
STATE_DIR = Path(os.environ.get('NIRVANA_SETUP_DIR', Path.home() / '.nirvana-gnmi'))
GUI_VERSION = '2'
NIRVANA_PORT = int(os.environ.get('NIRVANA_PORT', '8091'))
ANSI = re.compile(r'\x1b\[[0-9;]*m')
PORT_RE = re.compile(r'^[A-Za-z][A-Za-z\-]*\d+(?:/\d+)*(?:\.\d+)?$')

_lock = threading.Lock()
_nirvana = {'proc': None}


# ── 入力の確かめ ───────────────────────────────────────
def parse_form(d):
    """{'target','ports','user','password'} を確かめて返す。おかしければ ValueError(日本語)"""
    targets = [t.strip() for t in str(d.get('target', '')).split(',') if t.strip()]
    if not targets:
        raise ValueError('装置の IP を入れてください')
    for t in targets:
        host, _, port = t.partition(':')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f'IP アドレスの形ではありません: {t}') from None
        if port and not (port.isdigit() and 0 < int(port) < 65536):
            raise ValueError(f'ポート番号がおかしいです: {t}')
    ports = [p.strip() for p in str(d.get('ports') or 'Gi1/0/1').split(',') if p.strip()]
    for p in ports:
        if not PORT_RE.match(p):
            raise ValueError(f'ポート名の形ではありません: {p}(例 Gi1/0/1)')
    user = str(d.get('user') or 'admin').strip()
    if not re.match(r'^[\w.@-]{1,64}$', user):
        raise ValueError('ユーザー名に使えない文字があります')
    password = str(d.get('password') or '')
    if not password:
        raise ValueError('パスワードを入れてください')
    return {'targets': targets, 'ports': ports, 'user': user, 'password': password}


# ── gnmi-quickstart.sh を動かす(Windows は WSL の中で) ──
def on_windows():
    return os.name == 'nt'


WSL = ['wsl', '--cd', '~', '-e']


def quickstart_cmd(action, form=None):
    """(argv, stdin のバイト列 or None, 追加の環境変数) を返す"""
    args = [action]
    env = {'GNMI_FROM_GUI': '1'}
    if form:
        args += ['--target', ','.join(form['targets']), '--ports', ','.join(form['ports']),
                 '--user', form['user'], '--wait', '90']
        env['GNMI_PASSWORD'] = form['password']
    if on_windows():
        # Windows の git は改行を CRLF にすることがあるので、中身を LF にして標準入力で渡す。
        # 文字列で渡すと Windows の Python が改行を CRLF に戻すので、バイト列で渡す
        script = QUICKSTART.read_bytes().replace(b'\r\n', b'\n')
        env['WSLENV'] = ':'.join(filter(None, [os.environ.get('WSLENV', ''), 'GNMI_FROM_GUI/u',
                                                'GNMI_PASSWORD/u' if form else '']))
        # --cd ~: 呼び出し元の Windows フォルダへ移ろうとして失敗する(chdir ... failed)のを避ける
        return WSL + ['bash', '-s', '--'] + args, script, env
    return ['bash', str(QUICKSTART)] + args, None, env


def run_quickstart(action, form=None, timeout=300):
    argv, stdin, extra = quickstart_cmd(action, form)
    env = dict(os.environ, **extra)
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, env=env)
        out = ANSI.sub('', (p.stdout or b'').decode('utf-8', 'replace') + (p.stderr or b'').decode('utf-8', 'replace'))
        return p.returncode == 0, out
    except FileNotFoundError:
        return False, ('WSL が見つかりません。PowerShell で「wsl --install」を実行して Ubuntu を入れてください'
                       if on_windows() else 'bash が見つかりません')
    except subprocess.TimeoutExpired:
        return False, f'{timeout} 秒たっても終わりませんでした'


def stop_manual():
    """前に手で起動した gnmic / Prometheus を止める(ポートの取り合いを解く)"""
    sh = 'pkill -f "gnmic --config" ; pkill -f "prometheus --config" ; sleep 1 ; echo stopped'
    argv = WSL + ['bash', '-c', sh] if on_windows() else ['bash', '-c', sh]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return True, (p.stdout or '').strip() or '止めました'
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ── ニルヴァーナ本体 ───────────────────────────────────
def nirvana_config(form):
    """装置ごとに、選んだポートの先に「相手」を置いてリンクにする(相手は SNMP を持たない)"""
    lines = ['title: gNMI', 'poll_interval: 10', 'devices:']
    links = []
    for n, t in enumerate(form['targets'], 1):
        host = t.split(':')[0]
        lines += [f'  - id: sw{n}', f'    hostname: SW{n}', '    type: catalyst', f'    mgmt_ip: {host}',
                  '    interfaces:']
        for p in form['ports']:
            lines.append(f'      - {{name: "{expand_if(p)}"}}')
        for k, p in enumerate(form['ports'], 1):
            peer = f'sw{n}p{k}'
            lines += [f'  - id: {peer}', f'    hostname: "{p} の先"', '    type: pc', '    snmp: false',
                      '    interfaces:', '      - {name: eth0}']
            links.append(f'  - {{a: sw{n}, a_if: "{expand_if(p)}", b: {peer}, b_if: eth0}}')
    lines += ['links:'] + links
    lines += ['sources:', '  prometheus:', '    url: http://127.0.0.1:9090', '    flavor: gnmic',
              '    gnmic_job: gnmic']
    return '\n'.join(lines) + '\n'


def expand_if(n):
    for short, long in (('Gi', 'GigabitEthernet'), ('Te', 'TenGigabitEthernet'), ('Tw', 'TwoGigabitEthernet'),
                        ('Fa', 'FastEthernet'), ('Fo', 'FortyGigabitEthernet'), ('Hu', 'HundredGigE')):
        if n[:2].lower() == short.lower() and len(n) > 2 and n[2].isdigit():
            return long + n[2:]
    return n


def port_open(port, host='127.0.0.1'):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def start_nirvana(form):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = STATE_DIR / 'nirvana.yaml'
    cfg.write_text(nirvana_config(form), encoding='utf-8')
    old = _nirvana['proc']
    if old and old.poll() is None:
        old.terminate()
        try:
            old.wait(10)
        except subprocess.TimeoutExpired:
            old.kill()
    elif port_open(NIRVANA_PORT) and _kill_recorded_nirvana():
        pass     # 前回この画面から起動したもの(画面を閉じても残っていた)を止めた
    if port_open(NIRVANA_PORT):
        return False, (f'{NIRVANA_PORT} 番はほかのプログラムが使っています。前に手で起動したニルヴァーナを止めてから、'
                       'もう一度「開始」を押してください')
    log = open(STATE_DIR / 'nirvana.log', 'w', encoding='utf-8')  # noqa: SIM115
    _nirvana['proc'] = subprocess.Popen([sys.executable, str(HERE / 'server.py'), '-c', str(cfg),
                                         '--port', str(NIRVANA_PORT)], cwd=str(HERE), stdout=log, stderr=log)
    (STATE_DIR / 'nirvana.pid').write_text(str(_nirvana['proc'].pid), encoding='utf-8')
    for _ in range(40):
        if _nirvana['proc'].poll() is not None:
            tail = (STATE_DIR / 'nirvana.log').read_text(encoding='utf-8', errors='replace')[-800:]
            hint = ('\n部品が足りません。この画面を閉じて「python -m pip install -r requirements.txt」を一度実行してください'
                    if 'ModuleNotFoundError' in tail else '')
            return False, f'ニルヴァーナが起動できませんでした:\n{tail}{hint}'
        if port_open(NIRVANA_PORT):
            return True, f'ニルヴァーナを起動しました(設定 {cfg})'
        time.sleep(0.5)
    return False, 'ニルヴァーナの起動を待ちきれませんでした(nirvana.log を見てください)'


def _kill_recorded_nirvana():
    """前回この画面から起動したニルヴァーナ(pid を記録してある)を止める。止めたら True"""
    f = STATE_DIR / 'nirvana.pid'
    try:
        pid = int(f.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 15)
    except OSError:
        f.unlink(missing_ok=True)
        return False
    for _ in range(20):
        if not port_open(NIRVANA_PORT):
            break
        time.sleep(0.25)
    f.unlink(missing_ok=True)
    return True


def stop_nirvana():
    p = _nirvana['proc']
    if p and p.poll() is None:
        p.terminate()
        (STATE_DIR / 'nirvana.pid').unlink(missing_ok=True)
        return 'ニルヴァーナを止めました'
    if _kill_recorded_nirvana():
        return 'ニルヴァーナを止めました'
    return 'ニルヴァーナは動いていません(この画面から起動したもの以外は止めません)'


# ── 状態 ───────────────────────────────────────────────
def prom_query(q):
    url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': q})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as r:
        return json.loads(r.read().decode())['data']['result']


def status():
    s = {'gnmic': port_open(9804), 'prometheus': port_open(9090), 'nirvana': port_open(NIRVANA_PORT),
         'ports': [], 'nirvana_url': f'http://localhost:{NIRVANA_PORT}/?view=links'}
    if s['prometheus']:
        try:
            for r in prom_query('max by (source, interface_name, oper_status) '
                                '(interfaces_interface_state_counters_in_octets)'):
                m = r['metric']
                s['ports'].append({'device': m.get('source', ''), 'port': m.get('interface_name', ''),
                                   'oper': m.get('oper_status', ''), 'in_octets': r['value'][1]})
        except Exception as e:  # noqa: BLE001
            s['prom_error'] = str(e)
    return s


# ── 画面 ───────────────────────────────────────────────
PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>gNMI 設定</title>
<style>
:root{--bg:#070d17;--card:#13213a;--fg:#dbe6f3;--mute:#8ba0b8;--acc:#4fd6e8;--ok:#5fe0a0;--ng:#ff5a6a;--warn:#f2a93b}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Meiryo","Hiragino Sans",sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border-radius:10px;padding:18px;margin-bottom:16px}
label{display:block;margin:10px 0 4px;color:var(--mute);font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:6px;border:1px solid #2a3d5c;background:#0b1526;color:var(--fg);font-size:16px}
input.big{font-size:20px}
details summary{cursor:pointer;color:var(--mute);margin-top:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
button{padding:10px 18px;border:0;border-radius:6px;font-size:15px;cursor:pointer;background:#2a3d5c;color:var(--fg)}
button.main{background:var(--acc);color:#04121f;font-weight:bold}
button:disabled{opacity:.5;cursor:wait}
#msg{white-space:pre-wrap;font-family:Consolas,monospace;font-size:12.5px;max-height:320px;overflow:auto;background:#0b1526;padding:10px;border-radius:6px;display:none}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;margin-right:6px;font-size:13px;background:#2a3d5c}
.on{background:var(--ok);color:#04121f}.off{background:var(--ng);color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}td,th{padding:4px 6px;text-align:left;border-bottom:1px solid #2a3d5c}
a{color:var(--acc)}#open{display:none;font-size:17px}
</style></head><body><main>
<h1>gNMI で装置を見る <small style="font-size:12px;opacity:.5">版 __VER__</small></h1>
<p class="sub">装置の IP を入れて「開始」を押すだけです。gnmic・Prometheus・ニルヴァーナを用意して起動します。</p>
<div class="card">
<label for="target">装置の IP(複数はカンマ区切り)</label>
<input id="target" class="big" placeholder="192.168.1.1" value="__TARGET__" autofocus>
<label for="password">パスワード(装置の gNMI 用のユーザー)</label>
<input id="password" type="password" autocomplete="current-password">
<details><summary>詳しい設定(ふつうは変えなくてよい)</summary>
<label for="ports">見るポート(カンマ区切り)</label><input id="ports" value="__PORTS__">
<label for="user">ユーザー</label><input id="user" value="__USER__">
</details>
<div class="row">
<button class="main" id="start">開始</button>
<button id="stop">停止</button>
<button id="free" title="前に手で起動した gnmic・Prometheus を止めます">手で起動したものを止める</button>
</div></div>
<div class="card"><div id="badges"></div><p><a id="open" target="_blank">ニルヴァーナの画面を開く ›</a></p>
<table id="ports_t"></table><pre id="msg"></pre></div>
</main><script>
const $=id=>document.getElementById(id);
function say(t){const m=$('msg');m.style.display='block';m.textContent=t;m.scrollTop=m.scrollHeight}
function busy(b){for(const id of ['start','stop','free'])$(id).disabled=b}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
async function refresh(){
  const s=await (await fetch('/api/status')).json();
  const b=(n,v)=>`<span class="badge ${v?'on':'off'}">${n} ${v?'動作中':'停止'}</span>`;
  $('badges').innerHTML=b('gnmic',s.gnmic)+b('Prometheus',s.prometheus)+b('ニルヴァーナ',s.nirvana);
  $('open').href=s.nirvana_url;$('open').style.display=s.nirvana?'inline':'none';
  $('ports_t').innerHTML=s.ports.length?'<tr><th>装置</th><th>ポート</th><th>状態</th><th>受信バイト</th></tr>'+
    s.ports.map(p=>`<tr><td>${p.device}</td><td>${p.port}</td><td>${p.oper}</td><td>${Number(p.in_octets).toLocaleString()}</td></tr>`).join(''):'';
}
$('start').onclick=async()=>{
  busy(true);say('準備しています(初回は gnmic・Prometheus の取得で数分かかることがあります)…');
  try{const r=await post('/api/up',{target:$('target').value,ports:$('ports').value,user:$('user').value,password:$('password').value});
    say((r.ok?'うまくいきました\\n\\n':'うまくいきませんでした\\n\\n')+r.log);
    if(r.ok&&r.nirvana_url)window.open(r.nirvana_url,'_blank');}
  catch(e){say('エラー: '+e)}busy(false);refresh();
};
$('stop').onclick=async()=>{busy(true);const r=await post('/api/down');say(r.log);busy(false);refresh()};
$('free').onclick=async()=>{busy(true);const r=await post('/api/free');say(r.log);busy(false);refresh()};
refresh();setInterval(refresh,10000);
</script></body></html>"""


def page(defaults):
    return (PAGE.replace('__TARGET__', html.escape(defaults.get('target', '')))
            .replace('__PORTS__', html.escape(defaults.get('ports', 'Gi1/0/1')))
            .replace('__USER__', html.escape(defaults.get('user', 'admin')))
            .replace('__VER__', GUI_VERSION))


def load_defaults():
    try:
        return json.loads((STATE_DIR / 'last.json').read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return {}


def do_up(body):
    try:
        form = parse_form(body)
    except ValueError as e:
        return {'ok': False, 'log': str(e)}
    with _lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / 'last.json').write_text(json.dumps(
            {'target': ','.join(form['targets']), 'ports': ','.join(form['ports']), 'user': form['user']}),
            encoding='utf-8')   # パスワードは保存しない
        ok, log = run_quickstart('up', form)
        if not ok:
            if 'すでに使われています' in log:
                log += '\n\n→「手で起動したものを止める」を押してから、もう一度「開始」を押してください'
            return {'ok': False, 'log': log}
        ok2, log2 = start_nirvana(form)
        return {'ok': ok2, 'log': log + '\n' + log2,
                'nirvana_url': f'http://localhost:{NIRVANA_PORT}/?view=links' if ok2 else None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/?'):
            return self._send(200, page(load_defaults()), 'text/html; charset=utf-8')
        if self.path == '/api/status':
            return self._send(200, json.dumps(status(), ensure_ascii=False))
        self._send(404, '{}')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            body = {}
        if self.path == '/api/up':
            return self._send(200, json.dumps(do_up(body), ensure_ascii=False))
        if self.path == '/api/down':
            ok, log = run_quickstart('down')
            return self._send(200, json.dumps({'ok': ok, 'log': log + '\n' + stop_nirvana()}, ensure_ascii=False))
        if self.path == '/api/free':
            ok, log = stop_manual()
            return self._send(200, json.dumps({'ok': ok, 'log': log}, ensure_ascii=False))
        self._send(404, '{}')


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False   # Windows の SO_REUSEADDR は二重起動を許してしまう


def main(argv=None):
    ap = argparse.ArgumentParser(description='gNMI 設定画面')
    ap.add_argument('--port', type=int, default=8092)
    ap.add_argument('--no-open', action='store_true', help='ブラウザを開かない')
    a = ap.parse_args(argv)
    if not QUICKSTART.exists():
        sys.exit(f'{QUICKSTART} がありません')
    if not on_windows() and not shutil.which('bash'):
        sys.exit('bash がありません')
    if port_open(a.port):
        # Windows では同じポートに2つ目が bind できてしまい、古い設定画面が答え続けることがある
        sys.exit(f'{a.port} 番はもう使われています。前に開いた設定画面(黒いウィンドウ)をすべて閉じてから、'
                 'もう一度起動してください')
    srv = _Server(('127.0.0.1', a.port), Handler)   # この PC の中からだけ開ける
    url = f'http://localhost:{a.port}/'
    print(f'設定画面: {url}(閉じるときはこの画面で Ctrl + C)  版 {GUI_VERSION}', flush=True)
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_nirvana()


if __name__ == '__main__':
    main()
