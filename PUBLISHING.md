# 发布手册

当前状态：

- 包名 `opencode-session-explorer`，版本 `0.1.0`，官方源未被占用（2026-11-09 复查 404）
- 仓库 `williamwang2013/opencode-session-explorer`（public）
- 提交身份只在本仓库生效：`williamwang2013 <2614406@qq.com>`（全局 git 身份不影响）
- 打包：13 files / 约 57 kB
- 自检：`node scripts/check.mjs` 16 项
- CI：`.github/workflows/check.yml`，ubuntu + windows 双跑；无 `opencode.db` 时 HTTP 冒烟自动 skip
- 发布闸门：`scripts/prepublish.mjs`，占位符未改、版本已存在都会中止

---

## 1. 推送到 GitHub

### 1.1 建仓库

GitHub 网页 → New repository：

- Repository name: `opencode-session-explorer`
- Public
- 不要勾 Add a README / .gitignore / license

### 1.2 准备 classic PAT

`https://github.com/settings/tokens` → **Tokens (classic)** → Generate new token (classic)

- Note: `opencode-session-explorer publish`
- Expiration: 7 days（用完就删，不需要更长）
- Scopes 必须勾两个：
  - `repo`（推送代码）
  - `workflow`（推送 `.github/workflows/check.yml`，缺这个 scope 会被 GitHub 拒绝）

生成后只显示一次，存进文件，不要贴进聊天：

```powershell
Set-Content -Path "$HOME\.github-token" -Value "ghp_xxxx" -NoNewline
```

### 1.3 推送（token 不写进 remote）

用临时 credential 文件，避免 token 落在 `.git/config` 里：

```sh
printf 'https://x-access-token:TOKEN@github.com\n' > "$TEMP/gh-cred"
git -c credential.helper="store --file=$TEMP/gh-cred" push -u origin main
git -c credential.helper="store --file=$TEMP/gh-cred" push --tags
rm -f "$TEMP/gh-cred"
```

`origin` 保持 `https://github.com/williamwang2013/opencode-session-explorer.git`（不带 token）。

### 1.4 CI

push 后看 Actions 页。第一次失败一般出在 Node/Python 版本，贴那一步日志即可。

---

## 2. 发布到 npm

> 这台机器默认 registry 是 `registry.npmmirror.com`（只读镜像）。`package.json` 里已锁定
> `publishConfig.registry = https://registry.npmjs.org`，publish 会回官方源；
> **但登录必须显式指定 registry**，否则 token 不属于 npmjs。

### 2.1 登录

```sh
npm login --registry=https://registry.npmjs.org
npm whoami --registry=https://registry.npmjs.org
```

浏览器里授权一次，终端显示 `Logged in as <username>` 即可。`npm whoami` 输出的就是 npm 用户名（邮箱只是登录标识，不是用户名）。

### 2.2 空跑

```sh
npm publish --dry-run
```

先跑 `prepublishOnly`：占位符检查 → 官方源版本查重 → 16 项自检，然后列出将上传的文件。看到 `opencode-session-explorer-0.1.0.tgz` 且无报错才算准备好。

### 2.3 发布

```sh
npm publish
```

unscoped 包默认公开，`publishConfig.access=public` 也写着。发布不可撤销（72h 内才能 unpublish，名字收回后不可复用）。

### 2.4 验证

```sh
npm view opencode-session-explorer version --registry=https://registry.npmjs.org
npx opencode-session-explorer --no-open
```

### 2.5 升级

```sh
npm version patch
git push --follow-tags
npm publish
```

---

## 3. 切到新包

旧的 `~/.config/opencode/plugins/session-explorer.js` 必须先移除：工具名与新包完全相同，同时加载会重复注册。

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\opencode-plugin-backup" | Out-Null
Move-Item "$HOME\.config\opencode\plugins\session-explorer.js" "$HOME\opencode-plugin-backup\session-explorer.js" -Force
```

```sh
opencode plugin add opencode-session-explorer
opencode plugin list
```

数据不用迁移：`~/.config/opencode/session-explorer/`（索引、`config.json`、`reports/`）会被自动识别。换目录传 `options.home`。

回滚：`opencode plugin remove opencode-session-explorer`，再把备份文件移回原位。

---

## 4. 令牌吊销（用完必做）

删本地文件 ≠ 吊销。发布/推送完成后手动删除：

- GitHub：`https://github.com/settings/tokens` → 找到 Note 为 `opencode-session-explorer publish` 的那条 → Delete
- npm：`https://www.npmjs.com/settings/<username>/tokens` → 删除 `npm login` 生成的那条（类型通常显示为 Publish/Read-only 的自动 token）→ Delete

如果不确定有没有残留：GitHub 页面会列出最后使用时间，npm 页面会列最后使用 IP。

---

## 5. 曝光

- `https://opencode.ai/docs/ecosystem`：看页面底部的提交方式（PR 或 issue）
- `awesome-opencode` / `awesomeopencode.com` / `opencode.cafe`：一行条目 PR

英文一句话：

> **opencode-session-explorer** — Browse, search, export and report on your OpenCode session history. Local read-only web UI plus three agent tools (`session_search`, `session_report`, `session_explorer`). Filter by day, questions-only view, jump-to-hit search across tool output and reasoning, work-report export. Bilingual UI (EN/ZH). `opencode plugin add opencode-session-explorer`

发布前自查：

```sh
node scripts/check.mjs
npm pack --dry-run
npm publish --dry-run
```
