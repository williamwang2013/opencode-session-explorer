#!/usr/bin/env node
/**
 * Runs automatically on `npm publish` (prepublishOnly).
 *
 * Guards against the two mistakes that are painful to undo:
 *   1. publishing with placeholder repository URLs
 *   2. republishing a version that already exists on the registry
 *
 * Publishing is irreversible: a version can be unpublished only within 72h and
 * the name cannot be reused afterwards, so these checks run first.
 */
import { execFileSync, spawnSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"))
const npm = process.platform === "win32" ? "npm.cmd" : "npm"

const problems = []

const fields = {
  "repository.url": pkg.repository?.url,
  homepage: pkg.homepage,
  "bugs.url": pkg.bugs?.url,
}
const missing = Object.entries(fields)
  .filter(([, value]) => !value)
  .map(([key]) => key)
const placeholders = Object.entries(fields)
  .filter(([, value]) => value && value.includes("YOUR-GITHUB-USER"))
  .map(([key]) => key)

if (missing.length) problems.push(`missing: ${missing.join(", ")}`)
if (placeholders.length) problems.push(`still points at YOUR-GITHUB-USER: ${placeholders.join(", ")}`)

if (problems.length) {
  process.stderr.write("\nCannot publish:\n")
  for (const problem of problems) process.stderr.write(`  - ${problem}\n`)
  const url = "https://github.com/<user>/" + pkg.name
  process.stderr.write(
    "\nFix (one line, works in bash and PowerShell):\n" +
      `  npm pkg set repository.type=git repository.url=git+${url}.git homepage=${url}#readme bugs.url=${url}/issues\n\n`,
  )
  process.exit(1)
}

// Refuse to republish a version that already exists.
// Query the publish registry explicitly: a local mirror (npmmirror etc.) would
// report 404 for everything and make this guard useless.
const registry = pkg.publishConfig?.registry || "https://registry.npmjs.org"
try {
  execFileSync(npm, ["view", `${pkg.name}@${pkg.version}`, "version", "--registry", registry], {
    cwd: ROOT,
    stdio: "pipe",
  })
  process.stderr.write(
    `\nCannot publish: ${pkg.name}@${pkg.version} already exists on the registry.\n` +
      "Bump it first: npm version patch\n\n",
  )
  process.exit(1)
} catch {
  /* 404 means the version is free — expected */
}

const check = spawnSync(process.execPath, [join(ROOT, "scripts/check.mjs")], { stdio: "inherit" })
process.exit(check.status ?? 1)
