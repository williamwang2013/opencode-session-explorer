import {
  call,
  ensureServer,
  openBrowser,
  pageUrl,
  readToken,
  resolveLang,
  translate,
} from "./runtime.js"

type Options = {
  lang?: string
  port?: number
  host?: string
  python?: string
  home?: string
  token?: string
  open?: boolean
}

type ToolInput = Record<string, unknown>

function oneLine(value: unknown, max: number): string {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .slice(0, max)
}

function todayLocal(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function clamp(value: unknown, min: number, max: number, fallback: number): number {
  const n = Number(value)
  if (!Number.isFinite(n)) return fallback
  return Math.min(Math.max(Math.trunc(n), min), max)
}

/**
 * OpenCode Session Explorer.
 *
 * The definition shape below (`id` + `setup(ctx)`) is the OpenCode V2 plugin
 * contract. It deliberately imports nothing from `@opencode/plugin`, so the
 * package runs on any OpenCode release that accepts object entrypoints and
 * needs no runtime dependencies of its own.
 */
export default {
  id: "opencode-session-explorer",

  async setup(ctx: any) {
    const opt: Options = (ctx?.options || {}) as Options
    const lang = resolveLang(opt.lang)
    const t = (key: string, vars?: Record<string, unknown>) => translate(lang, key, vars)
    const locale = lang === "zh" ? "zh-CN" : "en-US"
    const fmtTime = (value: unknown) =>
      new Date(Number(value) || 0).toLocaleString(locale, { hour12: false })

    await ctx.tool.transform((editor: any) => {
      editor.add({
        name: "session_search",
        description: t("tool.search.description"),
        input: {
          type: "object",
          properties: {
            query: { type: "string", description: t("tool.search.query") },
            session_id: { type: "string", description: t("tool.search.session_id") },
            kinds: { type: "string", description: t("tool.search.kinds") },
            roles: { type: "string", description: t("tool.search.roles") },
            limit: { type: "number", description: t("tool.search.limit") },
          },
          required: ["query"],
          additionalProperties: false,
        },
        async execute(input: ToolInput) {
          const p = new URLSearchParams()
          p.set("q", String(input.query ?? ""))
          p.set("limit", String(clamp(input.limit, 1, 200, 25)))
          if (input.session_id) p.set("session_id", String(input.session_id))
          if (input.kinds) p.set("kinds", String(input.kinds))
          if (input.roles) p.set("roles", String(input.roles))
          const json: any = await call("/api/search?" + p.toString(), opt, lang)
          if (!json.hits?.length) {
            return { content: t("tool.search.no_hits", { q: String(input.query ?? "") }) }
          }
          const lines = json.hits.map((hit: any) => {
            const title = hit.session_title || hit.session_id
            const kind = hit.kind + (hit.tool ? "/" + hit.tool : "")
            return (
              `- [${fmtTime(hit.time)}] (${kind}) ${title}\n` +
              `  ${oneLine(hit.snippet, 220)}\n` +
              `  session ${hit.session_id} · ts ${hit.time}`
            )
          })
          return {
            content:
              t("tool.search.result_header", {
                count: json.count,
                shown: json.hits.length,
                indexed: json.index?.indexed ?? 0,
              }) +
              "\n" +
              lines.join("\n") +
              "\n\n" +
              t("tool.search.hint"),
          }
        },
      })

      editor.add({
        name: "session_report",
        description: t("tool.report.description"),
        input: {
          type: "object",
          properties: {
            from: { type: "string", description: t("tool.report.from") },
            to: { type: "string", description: t("tool.report.to") },
          },
          required: ["from"],
          additionalProperties: false,
        },
        async execute(input: ToolInput) {
          const to = String(input.to || todayLocal())
          const p = new URLSearchParams()
          p.set("format", "report")
          p.set("save", "1")
          p.set("from", String(new Date(String(input.from) + "T00:00:00").getTime()))
          p.set("to", String(new Date(to + "T23:59:59").getTime()))
          const json: any = await call("/api/export?" + p.toString(), opt, lang)
          return {
            content: t("tool.report.saved", {
              path: json.path,
              days: json.days,
              tasks: json.tasks,
              size: (Number(json.size) / 1024).toFixed(1),
              preview: json.preview,
            }),
          }
        },
      })

      editor.add({
        name: "session_explorer",
        description: t("tool.explorer.description"),
        input: {
          type: "object",
          properties: {
            session_id: { type: "string", description: t("tool.explorer.session_id") },
            at: { type: "number", description: t("tool.explorer.at") },
          },
          additionalProperties: false,
        },
        async execute(input: ToolInput) {
          if (!(await ensureServer(opt, lang))) {
            return { content: t("tool.explorer.start_failed") }
          }
          const query: Record<string, string> = {}
          if (input.session_id) query.id = String(input.session_id)
          if (input.at) query.t = String(input.at)
          // 带令牌打开，页面会自动下发 Cookie，免去手动登录（与旧版行为一致）
          const token = readToken(opt)
          if (token) query.token = token
          const url = pageUrl(opt, query)
          if (opt.open !== false) await openBrowser(url)
          return { content: t("tool.explorer.opened", { url }) }
        },
      })
    })
  },
}
