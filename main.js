const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

let mainWindow = null;
let pyProcess  = null;
const PORT = 8000;

// FastAPIサーバー起動
function startPython() {
  const appDir = app.isPackaged
    ? path.join(process.resourcesPath, 'app')
    : path.join(__dirname);

  const pythonExe = process.platform === 'win32' ? 'python' : 'python3';
  pyProcess = spawn(pythonExe, ['app.py'], {
    cwd: appDir,
    env: { ...process.env, PORT: String(PORT) },
    stdio: 'pipe',
  });

  pyProcess.stdout.on('data', d => console.log('[Python]', d.toString()));
  pyProcess.stderr.on('data', d => console.error('[Python]', d.toString()));
  pyProcess.on('close', code => console.log('[Python] 終了:', code));
}

// サーバー起動を待つ
function waitForServer(url, maxRetry = 30, interval = 500) {
  return new Promise((resolve, reject) => {
    let count = 0;
    const check = () => {
      http.get(url, res => {
        if (res.statusCode < 500) resolve();
        else retry();
      }).on('error', () => retry());
    };
    const retry = () => {
      if (++count >= maxRetry) reject(new Error('Server timeout'));
      else setTimeout(check, interval);
    };
    check();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400, height: 900,
    title: 'ネットワークラボ エミュレーター',
    backgroundColor: '#000000',
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  mainWindow.loadURL(`http://localhost:${PORT}`);
  mainWindow.on('closed', () => { mainWindow = null; });
  // 外部リンクはブラウザで開く
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url); return { action: 'deny' };
  });
}

app.whenReady().then(async () => {
  startPython();
  try {
    await waitForServer(`http://localhost:${PORT}/api/status`);
  } catch(e) {
    console.error('サーバー起動タイムアウト');
  }
  createWindow();
});

app.on('window-all-closed', () => {
  if (pyProcess) pyProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', () => {
  if (pyProcess) pyProcess.kill();
});
