"use strict";

const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const { spawn } = require("node:child_process");
const readline = require("node:readline");
const path = require("node:path");

const GITHUB_RELEASES = "https://github.com/SamuelMatos78/canvas-companion/releases";
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000;
const hasAppLock = app.requestSingleInstanceLock();

let backend = null;
let backendUrl = null;
let mainWindow = null;
let shuttingDown = false;
let checkingForUpdates = false;
let downloadInProgress = false;
let promptedVersion = null;
let manualUpdateCheck = false;

if (!hasAppLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      backendUrl = await startBackend();
      createWindow();
      installApplicationMenu();
      configureUpdater();
    } catch (error) {
      dialog.showErrorBox("Não foi possível abrir o Canvas Companion", error.message);
      app.quit();
    }
  });

  app.on("before-quit", (event) => {
    if (!backend || backend.exitCode !== null || !backend.pid) {
      backend = null;
      return;
    }
    event.preventDefault();
    if (shuttingDown) return;
    shuttingDown = true;

    const child = backend;
    const forceStopTimer = setTimeout(() => {
      if (child.exitCode === null) child.kill();
    }, 3000);
    child.once("close", () => {
      clearTimeout(forceStopTimer);
      if (backend === child) backend = null;
      app.quit();
    });
    if (!child.killed) child.kill();
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && backendUrl) createWindow();
  });

  ipcMain.on("app:close", () => {
    if (mainWindow) mainWindow.close();
  });
}

function backendExecutable() {
  const filename = process.platform === "win32" ? "Canvas-Companion.exe" : "Canvas-Companion";
  if (app.isPackaged) return path.join(process.resourcesPath, "backend", filename);
  return path.resolve(__dirname, "..", "dist", "executables", filename);
}

function startBackend() {
  const executable = backendExecutable();
  const child = spawn(executable, ["serve", "--port", "0", "--no-browser"], {
    cwd: app.getPath("userData"),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PYTHONUTF8: "1" }
  });
  backend = child;

  child.stderr.on("data", (chunk) => console.error(`[Canvas Companion backend] ${chunk.toString().trimEnd()}`));

  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => finish(new Error("A API local demorou demais para iniciar.")), 30000);
    const lines = readline.createInterface({ input: child.stdout });

    function finish(error, url) {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      lines.close();
      if (error) reject(error);
      else resolve(url);
    }

    child.once("error", (error) => {
      finish(new Error(`Não foi possível iniciar o componente local (${error.message}). Reinstale o app e tente novamente.`));
    });
    child.once("exit", (code) => {
      if (!settled && !shuttingDown) finish(new Error(`O componente local encerrou durante a inicialização (código ${code ?? "desconhecido"}).`));
    });
    lines.on("line", (line) => {
      console.log(`[Canvas Companion backend] ${line}`);
      const match = line.match(/disponível em (http:\/\/127\.0\.0\.1:\d+\/)/);
      if (match) finish(null, match[1]);
    });
  });
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 950,
    minWidth: 900,
    minHeight: 640,
    show: false,
    title: "Canvas Companion",
    backgroundColor: "#f3f6f3",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow = window;

  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url).catch((error) => console.error("Não foi possível abrir o link externo:", error));
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (new URL(url).origin !== new URL(backendUrl).origin) {
      event.preventDefault();
      shell.openExternal(url).catch((error) => console.error("Não foi possível abrir o link externo:", error));
    }
  });
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
    app.quit();
  });
  window.loadURL(backendUrl).catch((error) => {
    dialog.showErrorBox("Erro ao carregar o Canvas Companion", error.message);
    app.quit();
  });
}

function installApplicationMenu() {
  const template = [
    {
      label: "Canvas Companion",
      submenu: [
        { label: "Verificar atualizações", click: () => checkForUpdates(true) },
        { type: "separator" },
        { role: "quit", label: "Sair" }
      ]
    },
    {
      label: "Ajuda",
      submenu: [
        { label: "Abrir página de releases", click: () => shell.openExternal(GITHUB_RELEASES) },
        { label: "Sobre", click: () => dialog.showMessageBox(mainWindow, { type: "info", title: "Canvas Companion", message: `Canvas Companion v${app.getVersion()}`, detail: "Aplicativo local para cursos Canvas." }) }
      ]
    }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function configureUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.logger = console;

  autoUpdater.on("checking-for-update", () => { checkingForUpdates = true; });
  autoUpdater.on("update-not-available", () => {
    checkingForUpdates = false;
    if (manualUpdateCheck && mainWindow) {
      dialog.showMessageBox(mainWindow, { type: "info", title: "Canvas Companion", message: "Você já está usando a versão mais recente." });
    }
    manualUpdateCheck = false;
  });
  autoUpdater.on("update-available", (info) => {
    checkingForUpdates = false;
    manualUpdateCheck = false;
    if (promptedVersion === info.version || !mainWindow) return;
    promptedVersion = info.version;
    dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "Atualização disponível",
      message: `O Canvas Companion ${info.version} está disponível.`,
      detail: "Você pode continuar usando o app durante o download. Depois, reinicie para instalar a atualização.",
      buttons: ["Baixar atualização", "Mais tarde", "Ver release"],
      defaultId: 0,
      cancelId: 1,
      noLink: true
    }).then(({ response }) => {
      if (response === 0) downloadUpdate();
      else if (response === 2) shell.openExternal(`${GITHUB_RELEASES}/tag/v${encodeURIComponent(info.version)}`);
    });
  });
  autoUpdater.on("download-progress", (progress) => {
    if (mainWindow) mainWindow.setProgressBar(Math.max(0, Math.min(1, progress.percent / 100)));
  });
  autoUpdater.on("update-downloaded", (info) => {
    downloadInProgress = false;
    if (mainWindow) mainWindow.setProgressBar(-1);
    if (!mainWindow) return;
    dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "Atualização pronta",
      message: `A versão ${info.version} foi baixada.`,
      detail: "Instale agora ou continue usando o app; a atualização será aplicada quando ele for fechado.",
      buttons: ["Reiniciar e instalar", "Mais tarde"],
      defaultId: 0,
      cancelId: 1,
      noLink: true
    }).then(({ response }) => {
      if (response === 0) autoUpdater.quitAndInstall();
    });
  });
  autoUpdater.on("error", (error) => {
    checkingForUpdates = false;
    downloadInProgress = false;
    if (mainWindow) mainWindow.setProgressBar(-1);
    console.error("Erro ao verificar/baixar atualização:", error);
    if (manualUpdateCheck && mainWindow) {
      dialog.showMessageBox(mainWindow, { type: "error", title: "Erro de atualização", message: "Não foi possível verificar atualizações agora.", detail: error.message });
    }
    manualUpdateCheck = false;
  });

  checkForUpdates(false);
  setInterval(() => checkForUpdates(false), UPDATE_INTERVAL_MS).unref();
}

function checkForUpdates(manual) {
  if (!app.isPackaged || checkingForUpdates || downloadInProgress) return;
  manualUpdateCheck = manual;
  autoUpdater.checkForUpdates().catch((error) => {
    checkingForUpdates = false;
    console.error("Não foi possível verificar atualizações:", error);
    if (manual && mainWindow) {
      dialog.showMessageBox(mainWindow, { type: "error", title: "Erro de atualização", message: "Não foi possível verificar atualizações agora.", detail: error.message });
    }
    manualUpdateCheck = false;
  });
}

function downloadUpdate() {
  if (downloadInProgress) return;
  downloadInProgress = true;
  autoUpdater.downloadUpdate().catch((error) => {
    downloadInProgress = false;
    if (mainWindow) mainWindow.setProgressBar(-1);
    dialog.showMessageBox(mainWindow, { type: "error", title: "Erro ao baixar atualização", message: "Não foi possível baixar a atualização.", detail: error.message });
  });
}
