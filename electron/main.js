const { app, BrowserWindow, shell, nativeTheme } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

let mainWindow;
let backendProcess;
let frontendProcess;

const ROOT = path.join(__dirname, "..");
const BACKEND = path.join(ROOT, "backend");
const FRONTEND = path.join(ROOT, "frontend");
const VENV_PYTHON = path.join(BACKEND, "venv", "bin", "python3.12");

function loadEnv() {
  const envFile = path.join(BACKEND, ".env");
  if (fs.existsSync(envFile)) {
    fs.readFileSync(envFile, "utf-8")
      .split("\n")
      .filter(l => l && !l.startsWith("#") && l.includes("="))
      .forEach(l => {
        const [k, ...v] = l.split("=");
        if (k && v.length) process.env[k.trim()] = v.join("=").trim();
      });
  }
}

function startBackend() {
  loadEnv();
  backendProcess = spawn(VENV_PYTHON, ["-m", "uvicorn", "main:app",
    "--host", "127.0.0.1", "--port", "8000"], {
    cwd: BACKEND,
    env: { ...process.env },
    stdio: "pipe",
  });
  backendProcess.stdout.on("data", d => console.log("[backend]", d.toString().trim()));
  backendProcess.stderr.on("data", d => console.error("[backend]", d.toString().trim()));
}

function startFrontend() {
  frontendProcess = spawn("npm", ["run", "dev"], {
    cwd: FRONTEND,
    stdio: "pipe",
    shell: true,
  });
  frontendProcess.stdout.on("data", d => console.log("[frontend]", d.toString().trim()));
}

function waitForBackend(cb, retries = 20) {
  const http = require("http");
  http.get("http://127.0.0.1:8000/health", (res) => {
    if (res.statusCode === 200) cb();
    else setTimeout(() => waitForBackend(cb, retries - 1), 1000);
  }).on("error", () => {
    if (retries > 0) setTimeout(() => waitForBackend(cb, retries - 1), 1000);
  });
}

function createWindow() {
  const isDark = nativeTheme.shouldUseDarkColors;
  const bgColor = isDark ? "#0a0a0f" : "#fafafa";
  const themeParam = isDark ? "dark" : "light";

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: "hiddenInset",
    backgroundColor: bgColor,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
    icon: path.join(__dirname, "icon.png"),
  });

  // Open external links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) shell.openExternal(url);
    return { action: "deny" };
  });

  // Wait for servers to be ready before loading
  waitForBackend(() => {
    setTimeout(() => {
      mainWindow.loadURL(`http://localhost:3000?theme=${themeParam}`);
    }, 2000); // Give Next.js a moment after backend
  });

  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(() => {
  startBackend();
  startFrontend();
  // Give servers 3s to start then open window
  setTimeout(createWindow, 3000);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (backendProcess) backendProcess.kill();
  if (frontendProcess) frontendProcess.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (backendProcess) backendProcess.kill();
  if (frontendProcess) frontendProcess.kill();
});
