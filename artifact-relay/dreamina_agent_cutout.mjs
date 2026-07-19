#!/usr/bin/env node

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { constants as fsConstants, promises as fs } from "node:fs";
import path from "node:path";
import process from "node:process";

const AGENT_URL = "https://jimeng.jianying.com/ai-tool/home/?type=agentic";
const PROMPT = "你把人物抠出来，做成透明的png";
const DEFAULT_TIMEOUT_SECONDS = 600;

const EXIT = Object.freeze({
  OK: 0,
  USAGE: 2,
  BROWSER: 10,
  LOGIN_EXPIRED: 20,
  TIMEOUT: 21,
  NO_RESULT: 22,
  DOWNLOAD_FAILED: 23,
  WORKFLOW: 24,
});

class CutoutError extends Error {
  constructor(code, kind, message, details = undefined) {
    super(message);
    this.name = "CutoutError";
    this.code = code;
    this.kind = kind;
    this.details = details;
  }
}

class Deadline {
  constructor(timeoutMs) {
    this.expiresAt = Date.now() + timeoutMs;
  }

  remaining(capMs = Number.POSITIVE_INFINITY) {
    return Math.max(0, Math.min(capMs, this.expiresAt - Date.now()));
  }

  assert(stage) {
    if (this.remaining() <= 0) {
      throw new CutoutError(EXIT.TIMEOUT, "timeout", `timed out during ${stage}`);
    }
  }
}

function usage() {
  return `Usage:
  node dreamina_agent_cutout.mjs \\
    --browser /path/to/chrome \\
    --profile /path/to/persistent-profile \\
    --input /path/to/source.png \\
    --output /path/to/result.png \\
    [--probe] \\
    [--probe-upload] \\
    [--resume-workspace WORKSPACE_ID] \\
    [--timeout 600] \\
    [--diagnostics-dir /path/to/diagnostics]

Options:
  --browser          Chromium/Chrome executable.
  --profile          Persistent Chromium user-data directory with a valid Dreamina login.
  --input            One local source image.
  --output           Destination PNG. It must not already exist.
  --probe            Stop after confirming the Agent composer; do not submit.
  --probe-upload     Upload the input and enter the prompt, then stop without submitting.
  --resume-workspace Download an already submitted workspace without creating a new task.
  --timeout          Overall timeout in seconds (default: ${DEFAULT_TIMEOUT_SECONDS}).
  --diagnostics-dir  Failure screenshot and diagnostic JSON directory.
                     Default: <output-directory>/.dreamina-agent-diagnostics
  -h, --help         Show this help.

Exit codes:
  ${EXIT.USAGE}   invalid arguments or files
  ${EXIT.BROWSER}  browser/CDP startup failure
  ${EXIT.LOGIN_EXPIRED}  Dreamina web login is missing or expired
  ${EXIT.TIMEOUT}  overall timeout
  ${EXIT.NO_RESULT}  Agent completed or failed without a usable image
  ${EXIT.DOWNLOAD_FAILED}  original PNG download failed
  ${EXIT.WORKFLOW}  Dreamina page workflow changed or could not be driven
`;
}

function parseArgs(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "-h" || arg === "--help") return { help: true };
    if (!arg.startsWith("--")) {
      throw new CutoutError(EXIT.USAGE, "usage", `unexpected argument: ${arg}`);
    }
    const equals = arg.indexOf("=");
    const name = equals >= 0 ? arg.slice(0, equals) : arg;
    const inline = equals >= 0 ? arg.slice(equals + 1) : undefined;
    const allowed = new Set([
      "--browser",
      "--profile",
      "--input",
      "--output",
      "--timeout",
      "--diagnostics-dir",
      "--probe",
      "--probe-upload",
      "--resume-workspace",
    ]);
    if (!allowed.has(name)) {
      throw new CutoutError(EXIT.USAGE, "usage", `unknown option: ${name}`);
    }
    if (values.has(name)) {
      throw new CutoutError(EXIT.USAGE, "usage", `option supplied more than once: ${name}`);
    }
    if (name === "--probe" || name === "--probe-upload") {
      if (inline !== undefined) {
        throw new CutoutError(EXIT.USAGE, "usage", `${name} does not accept a value`);
      }
      values.set(name, true);
      continue;
    }
    let value = inline;
    if (value === undefined) {
      index += 1;
      value = argv[index];
    }
    if (value === undefined || value === "" || value.startsWith("--")) {
      throw new CutoutError(EXIT.USAGE, "usage", `missing value for ${name}`);
    }
    values.set(name, value);
  }

  for (const required of ["--browser", "--profile", "--input", "--output"]) {
    if (!values.has(required)) {
      throw new CutoutError(EXIT.USAGE, "usage", `missing required option: ${required}`);
    }
  }
  const exclusiveModes = ["--probe", "--probe-upload", "--resume-workspace"].filter((name) =>
    values.has(name)
  );
  if (exclusiveModes.length > 1) {
    throw new CutoutError(EXIT.USAGE, "usage", "probe and resume modes are mutually exclusive");
  }
  const resumeWorkspace = values.get("--resume-workspace") ?? "";
  if (resumeWorkspace && !/^\d{6,30}$/.test(resumeWorkspace)) {
    throw new CutoutError(EXIT.USAGE, "usage", "--resume-workspace must be a numeric workspace ID");
  }

  const timeoutRaw = values.get("--timeout") ?? String(DEFAULT_TIMEOUT_SECONDS);
  const timeoutSeconds = Number(timeoutRaw);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 30 || timeoutSeconds > 7200) {
    throw new CutoutError(EXIT.USAGE, "usage", "--timeout must be between 30 and 7200 seconds");
  }

  const output = path.resolve(values.get("--output"));
  return {
    help: false,
    browser: path.resolve(values.get("--browser")),
    profile: path.resolve(values.get("--profile")),
    input: path.resolve(values.get("--input")),
    output,
    probe: values.has("--probe"),
    probeUpload: values.has("--probe-upload"),
    resumeWorkspace,
    timeoutMs: Math.round(timeoutSeconds * 1000),
    diagnosticsDir: path.resolve(
      values.get("--diagnostics-dir") ?? path.join(path.dirname(output), ".dreamina-agent-diagnostics"),
    ),
  };
}

function safeUrl(raw) {
  try {
    const value = new URL(raw);
    const workspace = value.searchParams.get("workspace");
    value.search = "";
    value.hash = "";
    if (workspace && /^\d{6,30}$/.test(workspace)) value.searchParams.set("workspace", workspace);
    return value.toString();
  } catch {
    return "";
  }
}

function sanitizeText(value, limit = 800) {
  let text = String(value ?? "");
  text = text.replace(/(authorization|cookie|set-cookie)\s*[:=]\s*[^\s,;]+/gi, "$1=[redacted]");
  text = text.replace(/\b(Bearer)\s+[A-Za-z0-9._~+/=-]+/gi, "$1 [redacted]");
  text = text.replace(/([?&](?:code|state|token|signature|x-signature|sessionid)=)[^&#\s]+/gi, "$1[redacted]");
  return text.slice(0, limit);
}

function emit(level, event, fields = {}) {
  const record = {
    time: new Date().toISOString(),
    level,
    event,
  };
  for (const [key, value] of Object.entries(fields)) {
    if (/cookie|authorization|token|secret|header/i.test(key)) continue;
    if (typeof value === "string") record[key] = sanitizeText(value);
    else if (typeof value === "number" || typeof value === "boolean" || value === null) {
      record[key] = value;
    }
  }
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

async function exists(file) {
  try {
    await fs.access(file, fsConstants.F_OK);
    return true;
  } catch {
    return false;
  }
}

async function validatePaths(options) {
  let browserStat;
  let inputStat;
  try {
    [browserStat, inputStat] = await Promise.all([fs.stat(options.browser), fs.stat(options.input)]);
  } catch (error) {
    throw new CutoutError(EXIT.USAGE, "usage", `cannot access browser or input: ${error.message}`);
  }
  if (!browserStat.isFile()) throw new CutoutError(EXIT.USAGE, "usage", "--browser is not a file");
  if (!(browserStat.mode & 0o111)) {
    throw new CutoutError(EXIT.USAGE, "usage", "--browser is not executable");
  }
  if (!inputStat.isFile() || inputStat.size <= 0) {
    throw new CutoutError(EXIT.USAGE, "usage", "--input is not a non-empty regular file");
  }
  if (path.extname(options.output).toLowerCase() !== ".png") {
    throw new CutoutError(EXIT.USAGE, "usage", "--output must use a .png extension");
  }
  if (await exists(options.output)) {
    throw new CutoutError(EXIT.USAGE, "usage", "--output already exists");
  }
  if (await exists(path.join(options.profile, "SingletonLock"))) {
    throw new CutoutError(EXIT.BROWSER, "browser_start", "the Chromium profile is already in use");
  }
  await fs.mkdir(options.profile, { recursive: true, mode: 0o700 });
  await fs.mkdir(path.dirname(options.output), { recursive: true, mode: 0o700 });
  await fs.mkdir(options.diagnosticsDir, { recursive: true, mode: 0o700 });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate, deadline, stage, pollMs = 400, capMs = Number.POSITIVE_INFINITY) {
  const localExpiresAt = Date.now() + deadline.remaining(capMs);
  let lastValue;
  while (Date.now() < localExpiresAt) {
    deadline.assert(stage);
    lastValue = await predicate();
    if (lastValue) return lastValue;
    await sleep(Math.min(pollMs, Math.max(1, localExpiresAt - Date.now())));
  }
  return null;
}

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.closed = false;
    this.lastSocketError = "";
    socket.addEventListener("message", (event) => this.#onMessage(event));
    socket.addEventListener("error", (event) => {
      this.lastSocketError = sanitizeText(event?.message || event?.error?.message || "WebSocket error", 300);
    });
    socket.addEventListener("close", (event) => {
      const code = Number.isInteger(event?.code) ? event.code : 0;
      const reason = sanitizeText(event?.reason || this.lastSocketError, 300);
      this.#onClose(`CDP connection closed (code ${code}${reason ? `: ${reason}` : ""})`);
    });
  }

  static async connect(webSocketUrl, timeoutMs) {
    let socket;
    try {
      socket = new WebSocket(webSocketUrl);
    } catch (error) {
      throw new CutoutError(EXIT.BROWSER, "browser_start", `cannot create CDP socket: ${error.message}`);
    }
    await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new CutoutError(EXIT.BROWSER, "browser_start", "CDP socket open timed out")),
        timeoutMs,
      );
      socket.addEventListener(
        "open",
        () => {
          clearTimeout(timer);
          resolve();
        },
        { once: true },
      );
      socket.addEventListener(
        "error",
        () => {
          clearTimeout(timer);
          reject(new CutoutError(EXIT.BROWSER, "browser_start", "CDP socket open failed"));
        },
        { once: true },
      );
    });
    return new CdpClient(socket);
  }

  #onMessage(event) {
    let message;
    try {
      const raw = typeof event.data === "string" ? event.data : Buffer.from(event.data).toString("utf8");
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error.message || "CDP command failed"));
      else pending.resolve(message.result ?? {});
      return;
    }
    if (!message.method) return;
    const listeners = this.listeners.get(message.method);
    if (!listeners) return;
    for (const listener of [...listeners]) {
      try {
        listener(message.params ?? {}, message.sessionId);
      } catch {
        // Event observers must not terminate the browser workflow.
      }
    }
  }

  #onClose(message) {
    if (this.closed) return;
    this.closed = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(message));
    }
    this.pending.clear();
  }

  on(method, listener) {
    let listeners = this.listeners.get(method);
    if (!listeners) {
      listeners = new Set();
      this.listeners.set(method, listeners);
    }
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) this.listeners.delete(method);
    };
  }

  send(method, params = {}, sessionId = undefined, timeoutMs = 30000) {
    if (this.closed) return Promise.reject(new Error("CDP connection is closed"));
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify(payload));
    });
  }

  close() {
    this.closed = true;
    try {
      this.socket.close();
    } catch {
      // Best effort during shutdown.
    }
  }
}

async function clearRestoredSession(profile) {
  const defaultProfile = path.join(profile, "Default");
  await fs.rm(path.join(defaultProfile, "Sessions"), { recursive: true, force: true });
  await fs.mkdir(path.join(defaultProfile, "Sessions"), { recursive: true, mode: 0o700 });
  await Promise.all(
    ["Current Session", "Current Tabs", "Last Session", "Last Tabs"].map((name) =>
      fs.rm(path.join(defaultProfile, name), { force: true }),
    ),
  );

  const preferencesPath = path.join(defaultProfile, "Preferences");
  try {
    const preferences = JSON.parse(await fs.readFile(preferencesPath, "utf8"));
    preferences.profile = preferences.profile && typeof preferences.profile === "object"
      ? preferences.profile
      : {};
    preferences.profile.exit_type = "Normal";
    preferences.profile.exited_cleanly = true;
    const temporary = `${preferencesPath}.dreamina-${process.pid}`;
    await fs.writeFile(temporary, `${JSON.stringify(preferences)}\n`, { mode: 0o600 });
    await fs.rename(temporary, preferencesPath);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw new CutoutError(
        EXIT.BROWSER,
        "browser_start",
        `Dreamina browser preferences could not be prepared: ${sanitizeText(error.message, 300)}`,
      );
    }
  }
  emit("info", "restored_session_cleared");
}

async function startBrowser(options, deadline) {
  const activePortFile = path.join(options.profile, "DevToolsActivePort");
  await clearRestoredSession(options.profile);
  await Promise.all(
    ["DevToolsActivePort", "SingletonCookie", "SingletonLock", "SingletonSocket"].map((name) =>
      fs.rm(path.join(options.profile, name), { force: true }),
    ),
  );
  const args = [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-popup-blocking",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    `--user-data-dir=${options.profile}`,
    "--profile-directory=Default",
    "--window-size=1440,1000",
    "about:blank",
  ];
  const child = spawn(options.browser, args, {
    stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, HOME: path.dirname(options.profile) },
  });
  let stderrTail = "";
  child.stderr.on("data", (chunk) => {
    stderrTail = `${stderrTail}${chunk.toString("utf8")}`.slice(-8192);
  });
  let exited = false;
  let exitCode = null;
  child.once("exit", (code) => {
    exited = true;
    exitCode = code;
  });
  child.once("error", () => {
    exited = true;
    exitCode = -1;
  });

  try {
    const portInfo = await waitFor(
      async () => {
        if (exited) {
          throw new CutoutError(
            EXIT.BROWSER,
            "browser_start",
            `Chromium exited before CDP was ready (code ${exitCode})`,
            { stderr: sanitizeText(stderrTail, 1200) },
          );
        }
        try {
          const content = await fs.readFile(activePortFile, "utf8");
          const [portLine, browserPath] = content.trim().split(/\r?\n/);
          const port = Number(portLine);
          if (!Number.isInteger(port) || port <= 0 || !browserPath?.startsWith("/devtools/browser/")) {
            return null;
          }
          return { port, webSocketUrl: `ws://127.0.0.1:${port}${browserPath}` };
        } catch {
          return null;
        }
      },
      deadline,
      "browser startup",
      100,
      30000,
    );
    if (!portInfo) {
      throw new CutoutError(EXIT.BROWSER, "browser_start", "Chromium did not publish a CDP endpoint");
    }
    const cdp = await CdpClient.connect(portInfo.webSocketUrl, deadline.remaining(10000));
    emit("info", "browser_started", { pid: child.pid, cdp_port: portInfo.port });
    return {
      child,
      cdp,
      cdpPort: portInfo.port,
      stderrTail: () => stderrTail,
      exitState: () => ({ exited, exitCode, signalCode: child.signalCode }),
    };
  } catch (error) {
    if (!exited) child.kill("SIGTERM");
    await Promise.race([new Promise((resolve) => child.once("exit", resolve)), sleep(2000)]);
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    throw error;
  }
}

async function stopBrowser(runtime) {
  if (!runtime) return;
  try {
    await runtime.cdp.send("Browser.close", {}, undefined, 3000);
  } catch {
    // The process may already have exited.
  }
  runtime.cdp.close();
  if (runtime.child.exitCode === null && runtime.child.signalCode === null) {
    runtime.child.kill("SIGTERM");
    await Promise.race([
      new Promise((resolve) => runtime.child.once("exit", resolve)),
      sleep(3000),
    ]);
  }
  if (runtime.child.exitCode === null && runtime.child.signalCode === null) {
    runtime.child.kill("SIGKILL");
  }
}

async function attachPageTarget(cdp, targetId) {
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  await Promise.all([
    cdp.send("Page.enable", {}, sessionId),
    cdp.send("Runtime.enable", {}, sessionId),
    cdp.send("DOM.enable", {}, sessionId),
  ]);
  return sessionId;
}

async function createAgentPage(cdp, url = AGENT_URL) {
  const { targetId } = await cdp.send("Target.createTarget", { url });
  if (!/^[A-Fa-f0-9]{16,64}$/.test(targetId || "")) {
    throw new CutoutError(EXIT.BROWSER, "browser_start", "Chromium returned an invalid page target");
  }
  const sessionId = await attachPageTarget(cdp, targetId);
  emit("info", "agent_target_selected", { target_id: targetId });
  return { targetId, sessionId };
}

async function waitForAgentPage(cdp, sessionId, deadline) {
  let lastState = null;
  const ready = await waitFor(
    async () => {
      try {
        const state = await evaluate(
          cdp,
          sessionId,
          `({ready: document.readyState, url: location.href, hasBody: Boolean(document.body)})`,
        );
        lastState = state;
        const navigated = state?.url?.startsWith("https://jimeng.jianying.com/") ||
          state?.url?.startsWith("https://open.douyin.com/");
        return navigated && state.hasBody ? state : null;
      } catch {
        return null;
      }
    },
    deadline,
    "Agent page load",
    300,
    60000,
  );
  if (!ready) {
    throw new CutoutError(EXIT.TIMEOUT, "timeout", "Dreamina Agent page did not load", {
      last_url: safeUrl(lastState?.url),
      ready_state: sanitizeText(lastState?.ready, 40),
    });
  }
  emit("info", "agent_page_loaded", { url: safeUrl(ready.url) });
}

async function evaluate(cdp, sessionId, expression, returnByValue = true) {
  const response = await cdp.send(
    "Runtime.evaluate",
    {
      expression,
      awaitPromise: true,
      returnByValue,
      userGesture: true,
    },
    sessionId,
  );
  if (response.exceptionDetails) {
    const description = response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text || "unknown page error";
    const position = Number.isInteger(response.exceptionDetails.lineNumber)
      ? ` at ${response.exceptionDetails.lineNumber}:${response.exceptionDetails.columnNumber ?? 0}`
      : "";
    throw new Error(
      `Dreamina page JavaScript evaluation failed${position}: ${sanitizeText(description, 500)}`,
    );
  }
  return returnByValue ? response.result?.value : response.result;
}

const PAGE_HELPERS = String.raw`
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 &&
      rect.width > 1 && rect.height > 1 && rect.bottom > 0 && rect.right > 0 &&
      rect.top < innerHeight && rect.left < innerWidth;
  };
  const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const label = (el) => norm([
    el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
    el.getAttribute('data-testid'), el.className
  ].filter(Boolean).join(' '));
  const assetId = (source) => {
    try {
      const match = new URL(String(source || ''), location.href).pathname.match(/\/([a-f0-9]{32})(?:~|$)/i);
      return match ? match[1].toLowerCase() : '';
    } catch {
      return '';
    }
  };
  const composerCandidates = [...document.querySelectorAll('[contenteditable="true"]')]
    .filter(visible)
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const semantics = norm([
        el.getAttribute('role'), el.getAttribute('aria-label'), el.getAttribute('data-placeholder'),
        el.getAttribute('placeholder'), el.parentElement?.getAttribute('data-placeholder')
      ].filter(Boolean).join(' '));
      let score = Math.min(rect.width, 1000) + Math.min(rect.height, 300);
      if (/textbox/i.test(semantics)) score += 1000;
      if (/描述|告诉|输入|创作|想要|prompt/i.test(semantics)) score += 800;
      if (rect.width >= 300) score += 400;
      return {el, rect, score};
    })
    .sort((a, b) => b.score - a.score);
  const composer = composerCandidates[0]?.el || null;
`;

async function pageState(cdp, sessionId) {
  return evaluate(
    cdp,
    sessionId,
    String.raw`(() => {
      ${PAGE_HELPERS}
      const body = norm(document.body?.innerText);
      const exactLogin = [...document.querySelectorAll('button,a,[role="button"]')]
        .some((el) => visible(el) && /^(登录|扫码登录|立即登录)$/.test(norm(el.innerText || el.textContent || el.getAttribute('aria-label'))));
      const loginUrl = location.hostname !== 'jimeng.jianying.com' || /\/passport\/|\/login\b/.test(location.pathname);
      const images = [...document.images].map((img) => img.currentSrc || img.src).filter(Boolean);
      const promptMessageCount = [...document.querySelectorAll('div,p,span')].filter((el) => {
        if (!visible(el) || el === composer || composer?.contains(el) || el.contains(composer)) return false;
        if (norm(el.innerText || el.textContent) !== ${JSON.stringify(PROMPT)}) return false;
        return ![...el.children].some((child) => norm(child.innerText || child.textContent) === ${JSON.stringify(PROMPT)});
      }).length;
      return {
        url: location.href,
        title: document.title,
        body: body.slice(-5000),
        loggedOut: loginUrl || exactLogin,
        hasComposer: Boolean(composer),
        composerText: composer ? norm(composer.innerText || composer.textContent) : '',
        imageSources: images,
        imageAssetIds: [...new Set(images.map(assetId).filter(Boolean))],
        blobCount: images.filter((src) => src.startsWith('blob:')).length,
        promptMessageCount,
      };
    })()`,
  );
}

function assertLoggedIn(state) {
  if (state?.loggedOut) {
    throw new CutoutError(EXIT.LOGIN_EXPIRED, "login_expired", "Dreamina web login is missing or expired");
  }
}

async function waitForComposer(cdp, sessionId, deadline) {
  const state = await waitFor(
    async () => {
      const current = await pageState(cdp, sessionId);
      assertLoggedIn(current);
      return current.hasComposer ? current : null;
    },
    deadline,
    "Agent composer",
    500,
    120000,
  );
  if (!state) throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina Agent composer was not found");
  return state;
}

async function uploadInput(cdp, sessionId, input, baselineBlobCount, deadline) {
  const documentNode = await cdp.send(
    "DOM.getDocument",
    { depth: -1, pierce: true },
    sessionId,
  );
  const fileInputs = await cdp.send(
    "DOM.querySelectorAll",
    { nodeId: documentNode.root.nodeId, selector: 'input[type="file"]' },
    sessionId,
  );
  const nodeId = fileInputs.nodeIds?.[0];
  if (!nodeId) {
    throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina image file input was not found");
  }
  await cdp.send("DOM.setFileInputFiles", { nodeId, files: [input] }, sessionId);
  emit("info", "input_selected", { input_name: path.basename(input) });

  const uploaded = await waitFor(
    async () => {
      const state = await pageState(cdp, sessionId);
      assertLoggedIn(state);
      if (/上传失败|文件上传失败|图片上传失败/.test(state.body)) {
        throw new CutoutError(EXIT.WORKFLOW, "upload_failed", "Dreamina rejected the source image upload");
      }
      return state.blobCount > baselineBlobCount ? state : null;
    },
    deadline,
    "source image upload",
    500,
    120000,
  );
  if (!uploaded) {
    throw new CutoutError(EXIT.WORKFLOW, "upload_failed", "Dreamina did not show the uploaded source image");
  }
  await sleep(Math.min(2000, deadline.remaining()));
  emit("info", "input_uploaded");
}

async function dismissUploadNotice(cdp, sessionId, deadline) {
  const notice = await evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      const body = norm(document.body?.innerText);
      if (!/素材合规校验/.test(body)) return null;
      const button = [...document.querySelectorAll('button,[role="button"]')]
        .find((el) => visible(el) && norm(el.innerText || el.textContent) === '确认');
      if (!button) return null;
      const rect = button.getBoundingClientRect();
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    })()`,
  );
  if (!notice) return;
  await clickPoint(cdp, sessionId, notice);
  const dismissed = await waitFor(
    async () => {
      const state = await pageState(cdp, sessionId);
      return !/素材合规校验/.test(state.body) ? state : null;
    },
    deadline,
    "upload notice dismissal",
    200,
    10000,
  );
  if (!dismissed) {
    throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina upload notice could not be dismissed");
  }
  emit("info", "upload_notice_dismissed");
}

async function enterPrompt(cdp, sessionId) {
  const focused = await evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      if (!composer) return false;
      composer.focus();
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(composer);
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    })()`,
  );
  if (!focused) throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina composer could not be focused");
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", code: "Backspace" }, sessionId);
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace" }, sessionId);
  await cdp.send("Input.insertText", { text: PROMPT }, sessionId);
  const exact = await evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      return composer ? norm(composer.innerText || composer.textContent) === ${JSON.stringify(PROMPT)} : false;
    })()`,
  );
  if (!exact) {
    throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina composer did not contain the exact cutout prompt");
  }
  emit("info", "prompt_entered", { prompt: PROMPT });
}

async function findSubmitButton(cdp, sessionId) {
  return evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      if (!composer) return null;
      const c = composer.getBoundingClientRect();
      const ancestors = new Set();
      for (let el = composer; el && ancestors.size < 7; el = el.parentElement) ancestors.add(el);
      const candidates = [...document.querySelectorAll('button,[role="button"]')]
        .filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
        .map((el) => {
          const rect = el.getBoundingClientRect();
          const text = label(el);
          let score = 0;
          if (/发送|提交|开始生成|立即生成|send|submit/i.test(text)) score += 5000;
          if (/submit-button|send-button/i.test(String(el.className))) score += 4000;
          if ([...ancestors].some((ancestor) => ancestor.contains(el))) score += 1000;
          if (rect.left >= c.right - 160 && rect.top >= c.top - 40 && rect.bottom <= c.bottom + 140) score += 800;
          if (rect.width >= 20 && rect.width <= 72 && rect.height >= 20 && rect.height <= 72) score += 300;
          score -= Math.abs(rect.right - c.right) + Math.abs(rect.bottom - c.bottom);
          return {el, rect, text, score};
        })
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score);
      const best = candidates[0];
      if (!best) return null;
      return {
        x: best.rect.left + best.rect.width / 2,
        y: best.rect.top + best.rect.height / 2,
        label: best.text.slice(0, 120),
        score: best.score,
      };
    })()`,
  );
}

async function clickPoint(cdp, sessionId, point) {
  await cdp.send(
    "Input.dispatchMouseEvent",
    { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 },
    sessionId,
  );
  await cdp.send(
    "Input.dispatchMouseEvent",
    { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 },
    sessionId,
  );
}

function isWorkspaceUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.hostname === "jimeng.jianying.com" && parsed.pathname === "/ai-tool/generate" &&
      /^\d{6,30}$/.test(parsed.searchParams.get("workspace") || "");
  } catch {
    return false;
  }
}

async function submitOnce(cdp, sessionId, targetId, beforeSubmit, deadline) {
  const beforeTargets = await cdp.send("Target.getTargets");
  const baselineTargetIds = new Set(
    (beforeTargets.targetInfos || []).filter((target) => target.type === "page").map((target) => target.targetId),
  );
  const button = await findSubmitButton(cdp, sessionId);
  if (!button) throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina submit button was not found");
  let phase = "prepared";
  if (phase !== "prepared") {
    throw new CutoutError(EXIT.WORKFLOW, "workflow", "Dreamina submission state is invalid");
  }
  phase = "click_dispatched";
  // This is deliberately the only submit action in the script. Never retry it.
  await clickPoint(cdp, sessionId, button);
  emit("info", "prompt_clicked_once", { button_label: button.label });

  const workspaceSessions = new Map();
  const confirmation = await waitFor(
    async () => {
      try {
        const current = await pageState(cdp, sessionId);
        if (
          isWorkspaceUrl(current.url) &&
          current.composerText !== PROMPT &&
          (
            current.promptMessageCount > beforeSubmit.promptMessageCount ||
            current.body.includes(PROMPT)
          )
        ) {
          return {
            sessionId,
            targetId,
            url: current.url,
            method: current.url !== beforeSubmit.url ? "current_workspace" : "prompt_in_conversation",
            state: current,
          };
        }
      } catch {
        // The current tab may be replaced by a newly opened workspace.
      }
      const targets = await cdp.send("Target.getTargets");
      const workspaces = (targets.targetInfos || []).filter(
        (target) => target.type === "page" && !baselineTargetIds.has(target.targetId) && isWorkspaceUrl(target.url),
      );
      for (const workspace of workspaces) {
        let workspaceSessionId = workspaceSessions.get(workspace.targetId);
        if (!workspaceSessionId) {
          try {
            workspaceSessionId = await attachPageTarget(cdp, workspace.targetId);
            workspaceSessions.set(workspace.targetId, workspaceSessionId);
          } catch {
            continue;
          }
        }
        try {
          const current = await pageState(cdp, workspaceSessionId);
          if (
            current.composerText !== PROMPT &&
            (
              current.promptMessageCount > beforeSubmit.promptMessageCount ||
              current.body.includes(PROMPT)
            )
          ) {
            return {
              sessionId: workspaceSessionId,
              targetId: workspace.targetId,
              url: current.url,
              method: "new_workspace",
              state: current,
            };
          }
        } catch {
          // Keep waiting for this controlled workspace to finish loading.
        }
      }
      return null;
    },
    deadline,
    "Dreamina submission confirmation",
    500,
    45000,
  );
  if (!confirmation) {
    throw new CutoutError(
      EXIT.WORKFLOW,
      phase === "click_dispatched" ? "completion_unknown" : "workflow",
      "Dreamina did not confirm that the cutout prompt was submitted",
    );
  }
  phase = "task_confirmed";
  emit("info", "prompt_submission_confirmed", {
    method: confirmation.method,
    url: safeUrl(confirmation.url),
  });
  return confirmation;
}

async function resultState(cdp, sessionId, baselineSources) {
  return evaluate(
    cdp,
    sessionId,
    String.raw`(() => {
      ${PAGE_HELPERS}
      const body = norm(document.body?.innerText);
      const loggedOut = location.hostname !== 'jimeng.jianying.com' || /\/passport\/|\/login\b/.test(location.pathname) ||
        [...document.querySelectorAll('button,a,[role="button"]')]
          .some((el) => visible(el) && /^(登录|扫码登录|立即登录)$/.test(norm(el.innerText || el.textContent || el.getAttribute('aria-label'))));
      const baseline = new Set(${JSON.stringify([...baselineSources])});
      const candidates = [...document.images]
        .filter((img) => {
          const rect = img.getBoundingClientRect();
          return visible(img) && img.complete && img.naturalWidth >= 128 && img.naturalHeight >= 128 &&
            rect.width >= 160 && rect.height >= 90;
        })
        .map((img) => {
          const src = img.currentSrc || img.src || '';
          const stableAssetId = assetId(src);
          const rect = img.getBoundingClientRect();
          const nearby = norm(img.parentElement?.parentElement?.innerText || img.parentElement?.innerText || '');
          let score = Math.min(img.naturalWidth * img.naturalHeight / 10000, 500);
          if (/byteimg|dreamina|tos-cn-i/i.test(src)) score += 1000;
          if (/image-/i.test(String(img.className))) score += 400;
          if (/图片生成完成|已完成|抠图/.test(nearby)) score += 500;
          if (rect.width >= 250 || rect.height >= 250) score += 300;
          return {src, assetId: stableAssetId, score, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
        })
        .filter((item) => item.assetId && !baseline.has(item.assetId))
        .sort((a, b) => b.score - a.score);
      return {
        url: location.href,
        loggedOut,
        completed: /(\(?1\s*\/\s*1\)?\s*图片生成完成)|图片生成完成|抠图完成|处理完成/.test(body),
        failed: /图片生成失败|生成失败|抠图失败|处理失败|任务失败/.test(body),
        bodyTail: body.slice(-3000),
        candidate: candidates[0] || null,
      };
    })()`,
  );
}

async function waitForResult(cdp, sessionId, baselineSources, deadline) {
  let completedAt = null;
  const state = await waitFor(
    async () => {
      const current = await resultState(cdp, sessionId, baselineSources);
      if (current.loggedOut) {
        throw new CutoutError(EXIT.LOGIN_EXPIRED, "login_expired", "Dreamina web login expired after submission");
      }
      if (current.completed && !completedAt) completedAt = Date.now();
      if (current.candidate) return current;
      if (current.failed) {
        throw new CutoutError(EXIT.NO_RESULT, "no_result", "Dreamina Agent reported that the cutout failed");
      }
      if (completedAt && Date.now() - completedAt > 30000) {
        throw new CutoutError(EXIT.NO_RESULT, "no_result", "Dreamina Agent completed without a result image");
      }
      return null;
    },
    deadline,
    "Dreamina Agent result",
    1000,
  );
  if (!state) throw new CutoutError(EXIT.TIMEOUT, "timeout", "timed out waiting for Dreamina Agent result");
  emit("info", "result_ready", { url: safeUrl(state.url) });
  return state.candidate;
}

async function findAssetPoint(cdp, sessionId, expectedAssetId) {
  return evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      const expected = ${JSON.stringify(expectedAssetId)};
      const matches = [...document.images]
        .filter((img) => visible(img) && assetId(img.currentSrc || img.src) === expected)
        .map((img) => {
          const rect = img.getBoundingClientRect();
          return {rect, area: rect.width * rect.height};
        })
        .sort((a, b) => b.area - a.area);
      const best = matches[0];
      return best ? {
        x: best.rect.left + best.rect.width / 2,
        y: best.rect.top + best.rect.height / 2,
      } : null;
    })()`,
  );
}

async function findDownloadState(cdp, sessionId, expectedAssetId) {
  return evaluate(
    cdp,
    sessionId,
    `(() => {
      ${PAGE_HELPERS}
      const expected = ${JSON.stringify(expectedAssetId)};
      const matchingImages = [...document.images]
        .filter((img) => visible(img) && assetId(img.currentSrc || img.src) === expected)
        .map((img) => {
          const rect = img.getBoundingClientRect();
          return {width: rect.width, height: rect.height, area: rect.width * rect.height};
        })
        .filter((item) => item.width >= 250 || item.height >= 250)
        .sort((a, b) => b.area - a.area);
      if (!matchingImages.length) return null;
      const candidates = [...document.querySelectorAll('button,a,[role="button"]')]
        .filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
        .map((el) => {
          const rect = el.getBoundingClientRect();
          const text = label(el);
          const inDialog = Boolean(el.closest('[role="dialog"], [aria-modal="true"]'));
          let score = 0;
          const exact = /^下载$/.test(norm(el.innerText || el.textContent));
          const semantic = /下载|download/i.test(text);
          if (exact) score += 5000;
          if (semantic) score += 3000;
          if ((exact || semantic) && inDialog) score += 1000;
          return {rect, text, score};
        })
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score);
      const best = candidates[0];
      return best ? {
        assetId: expected,
        imageWidth: matchingImages[0].width,
        imageHeight: matchingImages[0].height,
        button: {
          x: best.rect.left + best.rect.width / 2,
          y: best.rect.top + best.rect.height / 2,
          label: best.text.slice(0, 120),
        },
      } : null;
    })()`,
  );
}

function awaitDownload(cdp, deadline) {
  return new Promise((resolve, reject) => {
    let guid = null;
    let suggestedFilename = null;
    const timer = setTimeout(() => {
      cleanup();
      reject(new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina download did not start or finish"));
    }, deadline.remaining(120000));
    const offBegin = cdp.on("Browser.downloadWillBegin", (params) => {
      if (guid !== null) return;
      guid = params.guid;
      suggestedFilename = path.basename(params.suggestedFilename || "");
    });
    const offProgress = cdp.on("Browser.downloadProgress", (params) => {
      if (!guid || params.guid !== guid) return;
      if (params.state === "completed") {
        cleanup();
        resolve({ guid, suggestedFilename, filePath: params.filePath || null });
      } else if (params.state === "canceled") {
        cleanup();
        reject(new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina download was canceled"));
      }
    });
    function cleanup() {
      clearTimeout(timer);
      offBegin();
      offProgress();
    }
  });
}

async function locateDownloadedFile(downloadDir, info, deadline) {
  return waitFor(
    async () => {
      const candidates = [];
      if (info.filePath) candidates.push(info.filePath);
      if (info.suggestedFilename) candidates.push(path.join(downloadDir, info.suggestedFilename));
      candidates.push(path.join(downloadDir, info.guid));
      try {
        for (const name of await fs.readdir(downloadDir)) {
          if (!name.endsWith(".crdownload")) candidates.push(path.join(downloadDir, name));
        }
      } catch {
        return null;
      }
      for (const candidate of [...new Set(candidates)]) {
        try {
          const stat = await fs.stat(candidate);
          if (stat.isFile() && stat.size > 8) return candidate;
        } catch {
          // Try the next candidate while Chrome finalizes the file.
        }
      }
      return null;
    },
    deadline,
    "download finalization",
    200,
    10000,
  );
}

async function validatePng(file) {
  const handle = await fs.open(file, "r");
  try {
    const header = Buffer.alloc(33);
    const { bytesRead } = await handle.read(header, 0, header.length, 0);
    const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
    if (bytesRead < 33 || !header.subarray(0, 8).equals(signature) || header.toString("ascii", 12, 16) !== "IHDR") {
      throw new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina download is not a valid PNG");
    }
    const width = header.readUInt32BE(16);
    const height = header.readUInt32BE(20);
    const colorType = header[25];
    if (width <= 0 || height <= 0 || ![3, 4, 6].includes(colorType)) {
      throw new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina PNG has no possible transparency channel");
    }
    return { width, height, colorType };
  } finally {
    await handle.close();
  }
}

async function downloadResult(cdp, sessionId, candidate, options, deadline) {
  const downloadDir = path.join(path.dirname(options.output), `.dreamina-download-${randomUUID()}`);
  await fs.mkdir(downloadDir, { mode: 0o700 });
  try {
    await cdp.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: downloadDir,
      eventsEnabled: true,
    });
    await clickPoint(cdp, sessionId, candidate);
    let downloadState = await waitFor(
      () => findDownloadState(cdp, sessionId, candidate.assetId),
      deadline,
      "result download button",
      300,
      6000,
    );
    if (!downloadState) {
      const secondPoint = await findAssetPoint(cdp, sessionId, candidate.assetId);
      if (secondPoint) {
        await clickPoint(cdp, sessionId, secondPoint);
        emit("info", "result_opened_second_click");
        downloadState = await waitFor(
          () => findDownloadState(cdp, sessionId, candidate.assetId),
          deadline,
          "result download button",
          300,
          24000,
        );
      }
    }
    if (!downloadState) {
      throw new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina result download button was not found");
    }
    emit("info", "result_asset_verified", {
      asset_id: downloadState.assetId,
      width: downloadState.imageWidth,
      height: downloadState.imageHeight,
    });
    const download = awaitDownload(cdp, deadline);
    await clickPoint(cdp, sessionId, downloadState.button);
    emit("info", "download_started", { button_label: downloadState.button.label });
    const info = await download;
    const downloaded = await locateDownloadedFile(downloadDir, info, deadline);
    if (!downloaded) {
      throw new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", "Dreamina download file was not created");
    }
    const png = await validatePng(downloaded);
    await fs.link(downloaded, options.output);
    await fs.unlink(downloaded);
    await fs.chmod(options.output, 0o600);
    emit("info", "download_completed", {
      output: options.output,
      width: png.width,
      height: png.height,
      png_color_type: png.colorType,
    });
    return png;
  } catch (error) {
    if (error instanceof CutoutError) throw error;
    throw new CutoutError(EXIT.DOWNLOAD_FAILED, "download_failed", `Dreamina result download failed: ${error.message}`);
  } finally {
    await fs.rm(downloadDir, { recursive: true, force: true });
  }
}

async function saveDiagnostics(cdp, sessionId, options, error, stage) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const prefix = path.join(options.diagnosticsDir, `dreamina-cutout-${stamp}`);
  const diagnostic = {
    time: new Date().toISOString(),
    stage,
    error_kind: error.kind || "internal",
    exit_code: Number.isInteger(error.code) ? error.code : EXIT.WORKFLOW,
    message: sanitizeText(error.message, 1000),
  };
  if (error.details?.stderr) diagnostic.browser_stderr = sanitizeText(error.details.stderr, 1200);
  if (error.details?.browser_exit) diagnostic.browser_exit = error.details.browser_exit;
  if (error.details?.last_url) diagnostic.last_url = safeUrl(error.details.last_url);
  if (error.details?.ready_state) diagnostic.ready_state = sanitizeText(error.details.ready_state, 40);
  if (error.details?.navigation_error) {
    diagnostic.navigation_error = sanitizeText(error.details.navigation_error, 300);
  }
  try {
    await fs.mkdir(options.diagnosticsDir, { recursive: true, mode: 0o700 });
  } catch {
    // The final write below reports a concise diagnostics failure.
  }
  if (cdp && sessionId) {
    try {
      const state = await pageState(cdp, sessionId);
      diagnostic.url = safeUrl(state.url);
      diagnostic.title = sanitizeText(state.title, 200);
      diagnostic.body_tail = sanitizeText(state.body, 4000);
    } catch {
      // Page state is best effort after a browser failure.
    }
    try {
      const shot = await cdp.send(
        "Page.captureScreenshot",
        { format: "png", fromSurface: true, captureBeyondViewport: false },
        sessionId,
        10000,
      );
      if (shot.data) {
        const screenshot = `${prefix}.png`;
        await fs.writeFile(screenshot, Buffer.from(shot.data, "base64"), { mode: 0o600 });
        diagnostic.screenshot = screenshot;
      }
    } catch {
      // The diagnostic JSON still records the failure if a screenshot is unavailable.
    }
  }
  try {
    const file = `${prefix}.json`;
    await fs.writeFile(file, `${JSON.stringify(diagnostic, null, 2)}\n`, { mode: 0o600 });
    emit("error", "diagnostics_saved", { diagnostic: file, screenshot: diagnostic.screenshot ?? null });
  } catch (diagnosticError) {
    emit("error", "diagnostics_failed", { message: diagnosticError.message });
  }
}

async function run(options) {
  const deadline = new Deadline(options.timeoutMs);
  let runtime = null;
  let sessionId = null;
  let targetId = null;
  let stage = "validation";
  try {
    await validatePaths(options);
    emit("info", "cutout_started", {
      input: options.input,
      output: options.output,
      timeout_seconds: options.timeoutMs / 1000,
    });
    stage = "startup";
    runtime = await startBrowser(options, deadline);
    stage = "page_load";
    const initialUrl = options.resumeWorkspace
      ? "https://jimeng.jianying.com/ai-tool/generate?workspace=" + options.resumeWorkspace
      : AGENT_URL;
    ({ sessionId, targetId } = await createAgentPage(runtime.cdp, initialUrl));
    await waitForAgentPage(runtime.cdp, sessionId, deadline);
    const initial = await waitForComposer(runtime.cdp, sessionId, deadline);
    if (options.resumeWorkspace) {
      const resumed = await waitFor(
        async () => {
          const state = await pageState(runtime.cdp, sessionId);
          assertLoggedIn(state);
          return isWorkspaceUrl(state.url) && state.body.includes(PROMPT) ? state : null;
        },
        deadline,
        "resumed workspace",
        500,
        120000,
      );
      if (!resumed) {
        throw new CutoutError(
          EXIT.NO_RESULT,
          "no_result",
          "Dreamina workspace does not contain the fixed cutout task",
        );
      }
      stage = "result_wait";
      const candidate = await waitForResult(runtime.cdp, sessionId, new Set(), deadline);
      stage = "result_download";
      const png = await downloadResult(runtime.cdp, sessionId, candidate, options, deadline);
      emit("info", "cutout_recovered", {
        output: options.output,
        width: png.width,
        height: png.height,
        workspace: options.resumeWorkspace,
        submitted: false,
      });
      return EXIT.OK;
    }
    if (options.probe) {
      emit("info", "probe_completed", { submitted: false });
      return EXIT.OK;
    }
    const baselineBlobCount = initial.blobCount;

    stage = "input_upload";
    await uploadInput(runtime.cdp, sessionId, options.input, baselineBlobCount, deadline);
    await dismissUploadNotice(runtime.cdp, sessionId, deadline);
    stage = "prompt_entry";
    await enterPrompt(runtime.cdp, sessionId);
    const beforeSubmit = await pageState(runtime.cdp, sessionId);
    assertLoggedIn(beforeSubmit);
    if (options.probeUpload) {
      emit("info", "probe_completed", {
        submitted: false,
        uploaded: true,
        prompt_entered: true,
      });
      return EXIT.OK;
    }

    stage = "single_submit";
    const submission = await submitOnce(
      runtime.cdp,
      sessionId,
      targetId,
      beforeSubmit,
      deadline,
    );
    ({ sessionId, targetId } = submission);
    stage = "result_wait";
    const baselineAssetIds = new Set([
      ...(beforeSubmit.imageAssetIds || []),
      ...(submission.state?.imageAssetIds || []),
    ]);
    const candidate = await waitForResult(
      runtime.cdp,
      sessionId,
      baselineAssetIds,
      deadline,
    );
    stage = "result_download";
    const png = await downloadResult(runtime.cdp, sessionId, candidate, options, deadline);
    emit("info", "cutout_completed", {
      output: options.output,
      width: png.width,
      height: png.height,
      submitted: true,
    });
    return EXIT.OK;
  } catch (rawError) {
    const error =
      rawError instanceof CutoutError
        ? rawError
        : new CutoutError(EXIT.WORKFLOW, "workflow", sanitizeText(rawError?.message || rawError));
    if (runtime) {
      error.details = {
        ...(error.details || {}),
        stderr: runtime.stderrTail().slice(-2000),
        browser_exit: runtime.exitState(),
      };
    }
    await saveDiagnostics(runtime?.cdp, sessionId, options, error, stage);
    emit("error", "cutout_failed", {
      stage,
      kind: error.kind,
      exit_code: error.code,
      message: error.message,
    });
    return error.code;
  } finally {
    await stopBrowser(runtime);
  }
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (rawError) {
    const error = rawError instanceof CutoutError ? rawError : new CutoutError(EXIT.USAGE, "usage", rawError.message);
    emit("error", "argument_error", { exit_code: error.code, message: error.message });
    process.stderr.write(usage());
    return error.code;
  }
  if (options.help) {
    process.stdout.write(usage());
    return EXIT.OK;
  }
  return run(options);
}

process.exitCode = await main();
