#!/usr/bin/env node
/**
 * Start the local Session Explorer without going through OpenCode.
 *
 *   npx opencode-session-explorer [--port 8717] [--lang en|zh] [--no-open]
 */
import { ensureServer, resolveLang, translate, dataHome, baseUrl, openBrowser, readToken } from "../src/runtime.js"

const HELP = `opencode-session-explorer

Usage: opencode-session-explorer [options]

Options:
  --port <n>       HTTP port (default 8717, env SESSION_EXPLORER_PORT)
  --host <addr>    Bind address (default 127.0.0.1)
  --lang <en|zh>   UI language (default: system locale)
  --python <path>  Python interpreter to use
  --home <dir>     Data directory (index, reports, config.json)
  --no-open        Do not open the browser
  -h, --help       Show this help
`

const opt = {}
const argv = process.argv.slice(2)
for (let i = 0; i < argv.length; i++) {
  const arg = argv[i]
  if (arg === "--port") opt.port = Number(argv[++i])
  else if (arg === "--host") opt.host = argv[++i]
  else if (arg === "--lang") opt.lang = argv[++i]
  else if (arg === "--python") opt.python = argv[++i]
  else if (arg === "--home") opt.home = argv[++i]
  else if (arg === "--no-open") opt.open = false
  else if (arg === "-h" || arg === "--help") {
    process.stdout.write(HELP)
    process.exit(0)
  }
}

const lang = resolveLang(opt.lang)
const ok = await ensureServer(opt, lang)

if (!ok) {
  const t = (key, vars) => translate(lang, key, vars)
  process.stderr.write(
    t("tool.explorer.start_failed") +
      "\n\nPython 3.9+ with SQLite FTS5 is required. Data: " +
      dataHome(opt.home) +
      "\n",
  )
  process.exit(1)
}

const url = baseUrl(opt)
const token = readToken(opt)
const openUrl = token ? `${url}/?token=${encodeURIComponent(token)}` : `${url}/`

process.stdout.write(translate(lang, "tool.explorer.opened", { url: url + "/" }) + "\n")
if (opt.open !== false) await openBrowser(openUrl)
