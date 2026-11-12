#!/usr/bin/env python3
"""Build a synthetic opencode.db for screenshots and demos.

Creates a throwaway database with fake projects, sessions and messages so the
UI can be captured without ever touching a real OpenCode data directory.

    python scripts/demo-data.py ./demo-env

Then point the server at it:

    OPENCODE_DATA_DIR=./demo-env python src/py/server.py

Nothing in here is derived from a real user's data.
"""

import json
import os
import sqlite3
import sys
import time

DAY = 86400 * 1000

DDL = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, version TEXT,
    agent TEXT, model TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER,
    tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, data TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE todo (
    session_id TEXT, content TEXT, status TEXT, priority TEXT, position INTEGER
);
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, version TEXT,
    agent TEXT, model TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER,
    tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, data TEXT,
    time_created INTEGER, time_updated INTEGER
);
"""


def tool(name, title, inp, out, created, status="completed"):
    return {
        "type": "tool",
        "id": "prt_%s_%d" % (name, created),
        "name": name,
        "state": {
            "status": status,
            "title": title,
            "input": inp,
            "output": out,
            "metadata": {"truncated": False},
        },
        "time": {"created": created, "completed": created + 1200},
    }


def text(value):
    return {"type": "text", "text": value}


def reasoning(value, created):
    return {
        "type": "reasoning",
        "text": value,
        "time": {"created": created, "completed": created + 900},
    }


def user_msg(mid, sid, seq, created, body):
    return (mid, sid, "user", seq, json.dumps({"text": body, "time": {"created": created}}), created, created)


def asst_msg(mid, sid, seq, created, content, model="demo/model"):
    payload = {"content": content, "time": {"created": created}, "modelID": model}
    return (mid, sid, "assistant", seq, json.dumps(payload, ensure_ascii=False), created, created + 4000)


def build(target):
    os.makedirs(target, exist_ok=True)
    db_path = os.path.join(target, "opencode.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.executescript(DDL)

    now = int(time.time() * 1000)
    sessions = []
    messages = []
    todos = []

    def session(sid, title, directory, agent, model, cost, tin, tout, trea, cache_r,
                created, updated, parent=None):
        sessions.append((sid, parent, directory, title, "2.0.0", agent, model, cost,
                         tin, tout, trea, cache_r, 0, created, updated))

    # ---- project 1: demo/shop-api -------------------------------------------
    s1 = "ses_demo_orders0000000000000001"
    t1 = now - 3 * DAY
    session(s1, "Add cursor pagination to the orders endpoint", "demo/shop-api",
            "build", "demo/fast-coder", 0.42, 38210, 5210, 1180, 61000, t1, t1 + 42 * 60000)
    messages += [
        user_msg("msg_o1", s1, 1, t1,
                 "The /orders endpoint returns every order in a single page. "
                 "Add cursor-based pagination on (created_at, id) and keep the existing "
                 "offset/limit parameters working for older clients."),
        asst_msg("msg_o2", s1, 2, t1 + 20000, [
            reasoning(
                "Offset pagination must keep working, so the cursor path has to be additive. "
                "I need the current route handler and the query builder before touching anything.",
                t1 + 21000),
            text("I'll add a `cursor` parameter next to the existing `offset`/`limit` pair, "
                 "then extend the query builder. First, let me see how orders are queried today."),
            tool("bash", "rg -n \"orders\" src/routes src/db", {"command": "rg -n orders src/routes src/db"},
                 "src/routes/orders.ts:18:  const rows = await db.order.findMany({\n"
                 "src/db/schema.ts:64:export const order = sqliteTable(\"orders\", {", t1 + 24000),
        ]),
        asst_msg("msg_o3", s1, 3, t1 + 90000, [
            tool("read", "src/routes/orders.ts", {"filePath": "src/routes/orders.ts"},
                 "export async function listOrders(req, res) {\n"
                 "  const limit = Number(req.query.limit ?? 50)\n"
                 "  const offset = Number(req.query.offset ?? 0)\n"
                 "  const rows = await db.select().from(orders)\n"
                 "    .orderBy(desc(orders.createdAt), desc(orders.id))\n"
                 "    .limit(limit).offset(offset)\n"
                 "  res.json({ rows, total })\n"
                 "}", t1 + 91000),
            text("Two things to change: the route and the shared `listQuery` helper. "
                 "The cursor will be `created_at|id` encoded as base64 so clients can treat it as opaque."),
        ]),
        asst_msg("msg_o4", s1, 4, t1 + 240000, [
            reasoning("Encoding both fields keeps the ordering stable when two orders share a timestamp.",
                      t1 + 241000),
            tool("edit", "src/routes/orders.ts", {
                "filePath": "src/routes/orders.ts",
                "oldString": "  const offset = Number(req.query.offset ?? 0)",
                "newString": "  const offset = Number(req.query.offset ?? 0)\n"
                             "  const cursor = decodeCursor(req.query.cursor)",
            }, "Edit applied successfully.", t1 + 245000),
            text("Route now accepts `cursor`. Next I'll wire it into the query builder and add a test."),
        ]),
        user_msg("msg_o5", s1, 5, t1 + 30 * 60000,
                 "Also update the OpenAPI schema, and cover the cursor path with a test "
                 "(including the empty-page case)."),
        asst_msg("msg_o6", s1, 6, t1 + 31 * 60000, [
            tool("read", "openapi.yaml", {"filePath": "openapi.yaml"},
                 "  /orders:\n    get:\n      parameters:\n"
                 "        - name: limit\n          in: query\n"
                 "        - name: offset\n          in: query", t1 + 31 * 60000 + 3000),
            tool("edit", "openapi.yaml", {"filePath": "openapi.yaml"},
                 "Edit applied successfully.", t1 + 31 * 60000 + 9000),
            tool("bash", "npm test -- orders", {"command": "npm test -- orders"},
                 "PASS  src/orders.test.ts\n"
                 "  cursor pagination\n"
                 "    ✓ returns the next page for a cursor\n"
                 "    ✓ returns an empty page past the end\n"
                 "    ✓ keeps offset pagination working\n\n"
                 "Tests: 3 passed, 3 total", t1 + 35 * 60000),
        ]),
        asst_msg("msg_o7", s1, 7, t1 + 40 * 60000, [
            text("Done. Summary:\n"
                 "- `GET /orders` accepts an opaque `cursor` alongside `offset`/`limit`\n"
                 "- cursor = base64 of `created_at|id`, so pagination stays stable\n"
                 "- OpenAPI updated with the new parameter\n"
                 "- 3 tests, including the empty-page case\n\n"
                 "Note: `total` is only returned for offset pagination; computing it per "
                 "cursor page would cost a second full scan."),
        ]),
    ]
    todos += [
        (s1, "Add cursor parameter to the route", "completed", "high", 1),
        (s1, "Extend the query builder", "completed", "high", 2),
        (s1, "Update OpenAPI schema", "completed", "medium", 3),
        (s1, "Add cursor pagination tests", "completed", "medium", 4),
        (s1, "Document the trade-off for `total`", "in_progress", "low", 5),
    ]

    # ---- project 1: other sessions ------------------------------------------
    s2 = "ses_demo_flaky00000000000000002"
    t2 = now - 1 * DAY
    session(s2, "Fix flaky login test in CI", "demo/shop-api", "build",
            "demo/fast-coder", 0.18, 14200, 2100, 640, 22000, t2, t2 + 18 * 60000)
    messages += [
        user_msg("msg_f1", s2, 1, t2, "auth.test.ts fails about one run in five on CI. Find out why."),
        asst_msg("msg_f2", s2, 2, t2 + 20000, [
            reasoning("Flaky auth tests are usually clock or shared-state related. Check for "
                      "a fixed timestamp or a reused token cache.", t2 + 21000),
            tool("read", "src/auth.test.ts", {"filePath": "src/auth.test.ts"},
                 "test('rejects expired token', async () => {\n"
                 "  const token = sign({ exp: Date.now() + 1000 })\n"
                 "  await sleep(1500)\n"
                 "  expect(verify(token)).rejects.toThrow()\n"
                 "})", t2 + 30000),
            text("The test sleeps 1500ms against a 1000ms expiry, so it depends on scheduler "
                 "jitter. Freezing the clock removes the race."),
        ]),
    ]
    todos += [(s2, "Replace sleep with a fake clock", "in_progress", "high", 1)]

    s3 = "ses_demo_slowsearch00000000003"
    t3 = now - 5 * DAY
    session(s3, "Investigate slow product search query", "demo/shop-api", "plan",
            "demo/reasoner", 0.31, 9800, 3400, 2900, 15000, t3, t3 + 25 * 60000)
    messages += [
        user_msg("msg_s1", s3, 1, t3, "Product search takes 1.8s on the staging dataset. Where is the time going?"),
        asst_msg("msg_s2", s3, 2, t3 + 30000, [
            reasoning("Run EXPLAIN QUERY PLAN first; a missing index on the join is the usual cause.",
                      t3 + 31000),
            tool("bash", "sqlite3 staging.db \".timer on\" \"EXPLAIN QUERY PLAN ...\"",
                 {"command": "sqlite3 staging.db 'EXPLAIN QUERY PLAN SELECT ...'"},
                 "QUERY PLAN\n|--SCAN product USING INDEX idx_product_name\n"
                 "`--SEARCH category USING INTEGER PRIMARY KEY (rowid=?)", t3 + 40000),
            text("It scans the whole product index then joins category per row. An index on "
                 "`(category_id, name)` lets it filter before joining; I'd expect ~8x on this dataset."),
        ]),
    ]

    # ---- project 2: demo/web-console ----------------------------------------
    s4 = "ses_demo_filters000000000000004"
    t4 = now - 2 * DAY
    session(s4, "Extract table filters into a composable", "demo/web-console", "build",
            "demo/fast-coder", 0.22, 21500, 3800, 720, 31000, t4, t4 + 55 * 60000)
    messages += [
        user_msg("msg_t1", s4, 1, t4,
                 "Three tables duplicate the same filter state. Pull it into a composable "
                 "and keep the URL query in sync."),
        asst_msg("msg_t2", s4, 2, t4 + 40000, [
            reasoning("The URL is the source of truth today; the composable has to read and write it "
                      "without losing unrelated params.", t4 + 41000),
            tool("read", "src/views/OrdersTable.vue", {"filePath": "src/views/OrdersTable.vue"},
                 "<script setup>\nconst page = ref(1)\nconst status = ref('')\n"
                 "watch([page, status], syncQuery)\n</script>", t4 + 50000),
            text("I'll extract `useTableFilters` with `page`, `status`, `q`, and a `syncQuery` "
                 "watcher that merges into the existing route query."),
        ]),
    ]
    todos += [(s4, "Create useTableFilters", "completed", "high", 1),
              (s4, "Migrate the three tables", "in_progress", "high", 2)]

    s5 = "ses_demo_darkmode000000000000005"
    t5 = now - 4 * DAY
    session(s5, "Dark-mode flash on first paint", "demo/web-console", "build",
            "demo/fast-coder", 0.09, 8600, 1400, 310, 9000, t5, t5 + 12 * 60000)
    messages += [
        user_msg("msg_d1", s5, 1, t5, "Refreshing in dark mode flashes white for ~200ms."),
        asst_msg("msg_d2", s5, 2, t5 + 25000, [
            reasoning("The theme class is applied by JS after hydration, so the first paint uses the "
                      "default background. Set it in an inline script before the app boots.", t5 + 26000),
            text("Move the theme read into a tiny inline script in `index.html` so the "
                 "`data-theme` attribute exists before the first paint."),
        ]),
    ]

    # ---- project 3: demo/data-tools -----------------------------------------
    s6 = "ses_demo_csv00000000000000006"
    t6 = now - 6 * 3600 * 1000
    session(s6, "CSV import mis-parses quoted newlines", "demo/data-tools", "build",
            "demo/fast-coder", 0.14, 12400, 2600, 880, 17000, t6, t6 + 22 * 60000)
    messages += [
        user_msg("msg_c1", s6, 1, t6,
                 "A 120k-row CSV comes out with shifted columns after row 3,000. "
                 "Quoted fields contain embedded newlines."),
        asst_msg("msg_c2", s6, 2, t6 + 20000, [
            reasoning("Shifting after a specific row points at a partial read or a newline inside "
                      "quotes. Check the reader first.", t6 + 21000),
            tool("read", "src/importer.py", {"filePath": "src/importer.py"},
                 "for line in handle:\n    rows.append(line.strip().split(','))", t6 + 30000),
            text("`split(',')` can't handle quoted newlines. Switching to the csv module fixes it, "
                 "and it's also faster."),
        ]),
    ]

    s7 = "ses_demo_child00000000000000007"
    session(s7, "subagent: profile the CSV parser", "demo/data-tools", "explore",
            "demo/reasoner", 0.05, 4200, 900, 1200, 3000, t6 + 5 * 60000, t6 + 9 * 60000, parent=s6)
    messages += [
        user_msg("msg_p1", s7, 1, t6 + 5 * 60000, "Profile the parser on the 120k-row file and report timings."),
        asst_msg("msg_p2", s7, 2, t6 + 6 * 60000, [
            tool("bash", "python -m cProfile -s cumtime src/importer.py sample.csv",
                 {"command": "python -m cProfile -s cumtime src/importer.py sample.csv"},
                 "   ncalls  tottime  percall  cumtime\n"
                 "        1    4.912    4.912    4.912  <module>\n"
                 "   120000    3.881    0.000    3.881  split\n"
                 "        1    0.442    0.442    0.442  read", t6 + 7 * 60000),
            text("4.9s total, 3.9s of it in `str.split`. Reading with `csv.reader` drops the whole "
                 "thing under 1s on the same file."),
        ]),
    ]

    con.executemany("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sessions)
    con.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", messages)
    con.executemany("INSERT INTO todo VALUES (?,?,?,?,?)", todos)
    con.commit()
    con.close()
    print("demo database: %s" % db_path)
    print("sessions: %d  messages: %d" % (len(sessions), len(messages)))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "./demo-env")
