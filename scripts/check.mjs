#!/usr/bin/env node
/**
 * Offline smoke test for opencode-session-explorer.
 *
 *   node scripts/check.mjs           # fast checks (HTTP tests only if opencode.db exists)
 *   node scripts/check.mjs --deep    # also open a session (needs an existing index)
 *
 * Exits non-zero on any failure.
 */
import { execFileSync, spawn } from "node:child_process"
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { homedir, tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const read = (rel) => readFileSync(join(ROOT, rel), "utf8")
const json = (rel) => JSON.parse(read(rel))

const deep = process.argv.includes("--deep")
const PORT = Number(process.env.CHECK_PORT || 8791)
const BASE = `http://127.0.0.1:${PORT}`

let failures = 0
const pass = (name) => process.stdout.write(`  ok   ${name}\n`)
const skip = (name) => process.stdout.write(`  skip ${name}\n`)
const fail = (name, err) => {
  failures++
  process.stdout.write(`  FAIL ${name}\n       ${String(err && err.message ? err.message : err)}\n`)
}
const check = async (name, fn) => {
  try {
    await fn()
    pass(name)
  } catch (err) {
    fail(name, err)
  }
}
const assert = (cond, message) => {
  if (!cond) throw new Error(message)
}

// ---------------------------------------------------------------- packaging
await check("package.json metadata", () => {
  const pkg = json("package.json")
  assert(pkg.name === "opencode-session-explorer", "unexpected package name")
  assert(/^\d+\.\d+\.\d+/.test(pkg.version), "version must be semver")
  assert(pkg.type === "module", "type must be module")
  assert(pkg.exports?.["."], "missing exports['.']")
  for (const target of Object.values(pkg.exports)) {
    assert(existsSync(join(ROOT, target)), `export target missing: ${target}`)
  }
  for (const file of pkg.files) {
    if (file.startsWith("!")) continue // negation patterns do not exist on disk
    assert(existsSync(join(ROOT, file)), `files entry missing: ${file}`)
  }
  assert(existsSync(join(ROOT, pkg.bin["opencode-session-explorer"])), "bin target missing")
})

// -------------------------------------------------------------------- i18n
const en = json("src/i18n/en.json")
const zh = json("src/i18n/zh.json")

await check("i18n dictionaries are in sync", () => {
  const enKeys = Object.keys(en).sort()
  const zhKeys = Object.keys(zh).sort()
  const missingZh = enKeys.filter((k) => !zhKeys.includes(k))
  const missingEn = zhKeys.filter((k) => !enKeys.includes(k))
  assert(!missingZh.length, `missing in zh.json: ${missingZh.join(", ")}`)
  assert(!missingEn.length, `missing in en.json: ${missingEn.join(", ")}`)
  for (const [key, value] of Object.entries(en)) {
    assert(typeof value === "string" && value.trim(), `en.${key} is empty`)
  }
  for (const [key, value] of Object.entries(zh)) {
    assert(typeof value === "string" && value.trim(), `zh.${key} is empty`)
  }
})

await check("every UI key used by index.html exists", () => {
  const html = read("src/web/index.html")
  const used = new Set()
  for (const m of html.matchAll(/data-i18n(?:-html|-ph|-title)?="([^"]+)"/g)) used.add(m[1])
  // keys resolved at runtime through string concatenation
  for (const kind of ["text", "reasoning", "tool", "file", "marker"]) used.add(`ui.kind_${kind}`)
  for (const key of ["ui.loaded", "ui.loaded_all"]) used.add(key)
  const missing = [...used].filter((key) => !(key in en) || !(key in zh))
  assert(!missing.length, `unknown keys: ${missing.join(", ")}`)
})

await check("placeholders match between languages", () => {
  const holes = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",")
  for (const key of Object.keys(en)) {
    assert(holes(en[key]) === holes(zh[key]), `placeholder mismatch in ${key}`)
  }
})

// ------------------------------------------------------------------ python
const python =
  process.env.SESSION_EXPLORER_PYTHON || (process.platform === "win32" ? "python" : "python3")

await check("server.py compiles", () => {
  execFileSync(
    python,
    ["-c", "compile(open('src/py/server.py', encoding='utf-8').read(), 'server.py', 'exec')"],
    { cwd: ROOT, stdio: "pipe" },
  )
})

await check("server i18n helpers work", () => {
  const dir = mkdtempSync(join(tmpdir(), "se-i18n-"))
  // No non-ASCII in argv: compare against the dictionary file instead.
  const script = `
import importlib.util, json
spec = importlib.util.spec_from_file_location("se_server", "src/py/server.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
zh = json.load(open("src/i18n/zh.json", encoding="utf-8"))
en = json.load(open("src/i18n/en.json", encoding="utf-8"))
assert m.LANG == "zh", m.LANG
assert m.t("server.report_title") == zh["server.report_title"], "zh lookup failed"
assert m.t("__no_such_key__") == "__no_such_key__", "missing key must fall back to the key"
html = m.inject_i18n('<!doctype html><html lang="en"><head></head><body></body></html>', "zh")
assert 'lang="zh"' in html, "lang attribute not rewritten"
assert "__SE_I18N" in html, "dictionaries not injected"
assert en["server.login_label"] in html and zh["server.login_label"] in html, "both dictionaries must be injected"
print("ok")
`
  try {
    execFileSync(python, ["-c", script], {
      cwd: ROOT,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONDONTWRITEBYTECODE: "1",
        SESSION_EXPLORER_HOME: dir,
        SESSION_EXPLORER_LANG: "zh",
      },
      stdio: "pipe",
    })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

// ------------------------------------------------------------ plugin entry
await check("plugin registers three tools", async () => {
  const mod = await import(pathToFileURL(join(ROOT, "src/index.ts")).href)
  const plugin = mod.default
  assert(plugin?.id === "opencode-session-explorer", "plugin id must be opencode-session-explorer")
  assert(typeof plugin.setup === "function", "plugin must expose setup()")
  const tools = []
  const ctx = {
    options: { lang: "en" },
    tool: { transform: async (fn) => fn({ add: (def) => tools.push(def) }) },
  }
  await plugin.setup(ctx)
  assert(tools.length === 3, `expected 3 tools, got ${tools.length}`)
  const names = tools.map((t) => t.name).sort().join(",")
  assert(names === "session_explorer,session_report,session_search", `unexpected tools: ${names}`)
  for (const tool of tools) {
    assert(tool.description && tool.description.trim(), `${tool.name} has no description`)
    assert(tool.input?.type === "object", `${tool.name} input must be an object schema`)
    assert(typeof tool.execute === "function", `${tool.name} has no execute()`)
  }
})

// -------------------------------------------------------------- http smoke
const legacyHome = join(homedir(), ".config", "opencode", "session-explorer")
const tempHome = mkdtempSync(join(tmpdir(), "se-check-"))
const legacyIndexed = existsSync(join(legacyHome, "index", "session-index.db"))
const home =
  process.env.SESSION_EXPLORER_HOME || (deep && legacyIndexed ? legacyHome : tempHome)
const hasIndex = existsSync(join(home, "index", "session-index.db"))
const dbPath = join(
  process.env.OPENCODE_DATA_DIR || join(homedir(), ".local", "share", "opencode"),
  "opencode.db",
)

function readHomeToken(dir) {
  try {
    return JSON.parse(readFileSync(join(dir, "config.json"), "utf8")).token || ""
  } catch {
    return ""
  }
}

// A reused data directory may have an access token enabled; authenticate if so.
const token = readHomeToken(home)
const get = (path) => fetch(BASE + path, { headers: token ? { "X-Session-Token": token } : {} })

function startServer({ dir, port, tok = "" }) {
  const base = `http://127.0.0.1:${port}`
  const probe = () =>
    fetch(base + "/api/health", { headers: tok ? { "X-Session-Token": tok } : {} })
  return new Promise((resolve, reject) => {
    const child = spawn(python, [join(ROOT, "src/py/server.py")], {
      cwd: ROOT,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        SESSION_EXPLORER_HOME: dir,
        SESSION_EXPLORER_PORT: String(port),
        SESSION_EXPLORER_LANG: "en",
      },
      stdio: ["ignore", "pipe", "pipe"],
    })
    let log = ""
    child.stdout.on("data", (b) => (log += b))
    child.stderr.on("data", (b) => (log += b))
    const started = Date.now()
    const tick = async () => {
      if (Date.now() - started > 20000) return reject(new Error("server did not start: " + log))
      try {
        const r = await probe()
        if (r.ok) return resolve(child)
      } catch {
        /* keep waiting */
      }
      setTimeout(tick, 250)
    }
    tick()
  })
}

async function httpSmoke() {
  let server = null
  let authServer = null
  const authHome = mkdtempSync(join(tmpdir(), "se-auth-"))
  try {
    server = await startServer({ dir: home, port: PORT, tok: token })
    pass("server starts and answers /api/health")

    await check("health payload is complete", async () => {
      const h = await (await get("/api/health")).json()
      assert(h.ok === true, "ok flag missing")
      assert(typeof h.sessions === "number", "sessions missing")
      assert(h.index && typeof h.index.indexed === "number", "index state missing")
      assert(h.lang === "en", `lang should be en, got ${h.lang}`)
      assert(h.data_home === home, `data_home mismatch: ${h.data_home}`)
    })

    await check("index page is served with both dictionaries", async () => {
      const html = await (await get("/?lang=en")).text()
      assert(html.includes("window.__SE_I18N"), "injected i18n payload missing")
      assert(html.includes("/i18n.js"), "i18n.js script tag missing")
      assert(html.includes('lang="en"'), "html lang attribute not switched")
      assert(html.includes('data-i18n="ui.app_title"'), "static i18n markers missing")
      const zhPage = await (await get("/?lang=zh")).text()
      assert(zhPage.includes('lang="zh"'), "html lang attribute not switched for zh")
    })

    await check("i18n.js is served", async () => {
      const r = await get("/i18n.js")
      assert(r.ok, `status ${r.status}`)
      const js = await r.text()
      assert(js.includes("window.SE_I18N"), "runtime does not expose SE_I18N")
    })

    await check("session list responds", async () => {
      const j = await (await get("/api/sessions?limit=5")).json()
      assert(Array.isArray(j.sessions), "sessions is not an array")
      assert(Array.isArray(j.dirs), "dirs is not an array")
    })

    await check("work report export responds", async () => {
      const today = new Date()
      const from = new Date(today.getTime() - 7 * 86400000).toISOString().slice(0, 10)
      const to = today.toISOString().slice(0, 10)
      const ms = (d, end) => String(new Date(d + (end ? "T23:59:59" : "T00:00:00")).getTime())
      const body = await (
        await get(`/api/export?format=report&from=${ms(from)}&to=${ms(to, true)}`)
      ).text()
      assert(body.includes("# "), "report body looks empty")
    })

    if (deep && hasIndex) {
      await check("first session opens (index-backed)", async () => {
        const list = await (await get("/api/sessions?limit=1")).json()
        const id = list.sessions?.[0]?.id
        assert(id, "no sessions available to open")
        const j = await (await get("/api/session?id=" + encodeURIComponent(id))).json()
        assert(j.session?.id === id, "session payload mismatch")
        const parts = await (
          await get(`/api/parts?session_id=${encodeURIComponent(id)}&limit=5`)
        ).json()
        assert(Array.isArray(parts.messages), "messages is not an array")
      })
    }

    // -------------------------------------------------------- access token
    writeFileSync(join(authHome, "config.json"), JSON.stringify({ token: "check-token" }), "utf8")
    const authBase = `http://127.0.0.1:${PORT + 1}`
    authServer = await startServer({ dir: authHome, port: PORT + 1, tok: "check-token" })
    pass("server starts with an access token enabled")

    await check("token gates the app page", async () => {
      const anon = await fetch(authBase + "/", { redirect: "manual" })
      const anonBody = anon.status >= 300 ? "" : await anon.text()
      assert(!anonBody.includes("__SE_I18N"), "anonymous request received the app page")
      const ok = await fetch(authBase + "/?token=check-token&lang=zh", { redirect: "manual" })
      assert(ok.status === 200, `token page status ${ok.status}`)
      const cookie = ok.headers.get("set-cookie") || ""
      assert(cookie.includes("se_token="), "visited with ?token= but no auth cookie was set")
      const html = await ok.text()
      assert(html.includes("__SE_I18N"), "authorised page missing the i18n payload")
    })

    await check("login page is localised and rejects a bad token", async () => {
      const page = await (await fetch(authBase + "/login?lang=zh")).text()
      assert(page.includes('data-i18n="server.login_label"'), "login page has no i18n markers")
      assert(page.includes('lang="zh"'), "login page lang not switched")
      const bad = await fetch(authBase + "/login", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ token: "wrong-token" }),
        redirect: "manual",
      })
      const body = await bad.text()
      assert(body.includes("Incorrect token."), "bad token did not produce the localised error")
    })
  } catch (err) {
    fail("http smoke test", err)
  } finally {
    if (server) server.kill()
    if (authServer) authServer.kill()
    rmSync(authHome, { recursive: true, force: true })
  }
}

if (existsSync(dbPath)) await httpSmoke()
else skip("http smoke test (no opencode.db on this machine)")
rmSync(tempHome, { recursive: true, force: true })

if (!deep) process.stdout.write("\n(tip: pass --deep on a machine with an existing index)\n")
process.stdout.write(failures ? `\n${failures} check(s) failed\n` : "\nall checks passed\n")
process.exit(failures ? 1 : 0)
