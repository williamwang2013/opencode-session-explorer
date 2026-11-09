import { spawn } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))

/** Package root (one level above src/). */
export const PKG_ROOT = dirname(HERE)
/** Absolute path of the Python backend. */
export const SERVER_PY = join(HERE, "py", "server.py")
/** Absolute path of the translation dictionaries. */
export const I18N_DIR = join(HERE, "i18n")

/**
 * @typedef {Object} Options
 * @property {string} [lang]     "auto" | "en" | "zh"
 * @property {number} [port]     HTTP port, default 8717
 * @property {string} [host]     Default 127.0.0.1
 * @property {string} [python]   Python interpreter override
 * @property {string} [home]     Data directory override
 * @property {string} [token]    Access token override
 * @property {boolean} [open]    Open the browser (default true)
 */

export const SUPPORTED_LANGS = ["en", "zh"]

const dictCache = new Map()

/** @returns {"en"|"zh"} */
export function resolveLang(preferred) {
  const raw = String(preferred || process.env.SESSION_EXPLORER_LANG || "").trim().toLowerCase()
  if (raw === "en" || raw === "zh") return raw
  if (raw) return raw.startsWith("zh") ? "zh" : "en"
  let locale = ""
  try {
    locale = Intl.DateTimeFormat().resolvedOptions().locale || ""
  } catch {
    locale = ""
  }
  const probe = (
    process.env.LC_ALL ||
    process.env.LC_MESSAGES ||
    process.env.LANG ||
    locale ||
    "en"
  ).toLowerCase()
  return probe.startsWith("zh") ? "zh" : "en"
}

/**
 * Read `src/i18n/<lang>.json`, falling back to English.
 * @param {"en"|"zh"} lang
 * @returns {Record<string,string>}
 */
export function loadDict(lang) {
  const cached = dictCache.get(lang)
  if (cached) return cached
  let dict = {}
  try {
    dict = JSON.parse(readFileSync(join(I18N_DIR, lang + ".json"), "utf8"))
  } catch {
    dict = {}
  }
  dictCache.set(lang, dict)
  return dict
}

/**
 * Translate a key with `{placeholder}` substitution.
 * @param {"en"|"zh"} lang
 * @param {string} key
 * @param {Record<string, unknown>} [vars]
 */
export function translate(lang, key, vars) {
  let out = loadDict(lang)[key]
  if (out == null) out = loadDict("en")[key]
  if (out == null) return key
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, (match, name) => (vars[name] != null ? String(vars[name]) : match))
  }
  return out
}

/** Data directory. Mirrors `_default_data_home()` in server.py. */
export function dataHome(override) {
  if (override) return override
  if (process.env.SESSION_EXPLORER_HOME) return process.env.SESSION_EXPLORER_HOME
  const legacy = join(homedir(), ".config", "opencode", "session-explorer")
  if (existsSync(join(legacy, "index")) || existsSync(join(legacy, "config.json"))) return legacy
  return join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "opencode-session-explorer")
}

/** @param {Options} [opt] */
export function configPath(opt) {
  return join(dataHome(opt?.home), "config.json")
}

/** Access token: plugin option first, then `config.json` in the data directory. */
export function readToken(opt) {
  if (opt?.token) return opt.token
  try {
    const cfg = JSON.parse(readFileSync(configPath(opt), "utf8"))
    return typeof cfg?.token === "string" ? cfg.token : ""
  } catch {
    return ""
  }
}

/** @param {Options} [opt] */
export function baseUrl(opt) {
  const fromEnv = process.env.SESSION_EXPLORER_URL
  if (fromEnv) return fromEnv.replace(/\/+$/, "")
  const host = opt?.host || process.env.SESSION_EXPLORER_HOST || "127.0.0.1"
  const port = opt?.port || Number(process.env.SESSION_EXPLORER_PORT) || 8717
  return `http://${host}:${port}`
}

function headers(opt) {
  const token = readToken(opt)
  return token ? { "X-Session-Token": token } : {}
}

/** @param {Options} [opt] */
export async function isUp(opt) {
  try {
    const r = await fetch(baseUrl(opt) + "/api/health", {
      signal: AbortSignal.timeout(900),
      headers: headers(opt),
    })
    return r.ok
  } catch {
    return false
  }
}

function interpreters(opt) {
  if (opt?.python) return [[opt.python, []]]
  if (process.platform === "win32") {
    return [
      ["pythonw", []],
      ["python", []],
      ["py", ["-3"]],
    ]
  }
  return [
    ["python3", []],
    ["python", []],
  ]
}

function spawnDetached(cmd, args, env) {
  return new Promise((resolve) => {
    let settled = false
    let child
    try {
      child = spawn(cmd, args, {
        cwd: PKG_ROOT,
        detached: true,
        stdio: "ignore",
        windowsHide: true,
        env,
      })
    } catch {
      resolve(false)
      return
    }
    child.once("error", () => {
      if (!settled) {
        settled = true
        resolve(false)
      }
    })
    child.once("spawn", () => {
      if (!settled) {
        settled = true
        child.unref()
        resolve(true)
      }
    })
  })
}

function serverEnv(opt, lang) {
  return {
    ...process.env,
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
    SESSION_EXPLORER_HOME: dataHome(opt?.home),
    SESSION_EXPLORER_HOST: opt?.host || process.env.SESSION_EXPLORER_HOST || "127.0.0.1",
    SESSION_EXPLORER_PORT: String(opt?.port || process.env.SESSION_EXPLORER_PORT || 8717),
    SESSION_EXPLORER_LANG: lang || resolveLang(opt?.lang),
  }
}

/**
 * Spawn a detached local server process. OpenCode can exit and the page keeps working.
 * @param {Options} [opt]
 * @param {"en"|"zh"} [lang]
 */
export async function startServer(opt, lang) {
  const env = serverEnv(opt, lang)
  for (const [cmd, prefix] of interpreters(opt)) {
    if (await spawnDetached(cmd, [...prefix, SERVER_PY], env)) break
  }
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 200))
    if (await isUp(opt)) return true
  }
  return false
}

/**
 * @param {Options} [opt]
 * @param {"en"|"zh"} [lang]
 */
export async function ensureServer(opt, lang) {
  if (await isUp(opt)) return true
  return startServer(opt, lang)
}

/**
 * Call the local HTTP API, starting the server on demand.
 * @param {string} path
 * @param {Options} [opt]
 * @param {"en"|"zh"} [lang]
 */
export async function call(path, opt, lang) {
  const L = lang || resolveLang(opt?.lang)
  if (!(await ensureServer(opt, L))) {
    throw new Error(translate(L, "tool.explorer.start_failed"))
  }
  let r
  try {
    r = await fetch(baseUrl(opt) + path, {
      signal: AbortSignal.timeout(180000),
      headers: headers(opt),
    })
  } catch (err) {
    throw new Error(translate(L, "tool.error.http", { status: String(err?.message || err) }))
  }
  if (r.status === 401) {
    throw new Error(translate(L, "tool.error.bad_token", { path: configPath(opt) }))
  }
  const text = await r.text()
  let json = null
  try {
    json = JSON.parse(text)
  } catch {
    json = null
  }
  if (!r.ok) {
    throw new Error(json?.error || translate(L, "tool.error.http", { status: r.status }))
  }
  if (json == null) throw new Error(translate(L, "tool.error.http", { status: r.status }))
  return json
}

/** Open a URL in the system default browser. */
export async function openBrowser(url) {
  if (process.platform === "win32") {
    await spawnDetached("cmd", ["/c", "start", "", url], process.env)
    return
  }
  if (process.platform === "darwin") {
    await spawnDetached("open", [url], process.env)
    return
  }
  await spawnDetached("xdg-open", [url], process.env)
}

/**
 * @param {Options} [opt]
 * @param {Record<string,string>} [query]
 */
export function pageUrl(opt, query) {
  const qs = new URLSearchParams(query || {}).toString()
  return baseUrl(opt) + "/" + (qs ? "?" + qs : "")
}
