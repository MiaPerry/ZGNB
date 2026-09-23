# 配置指南

> 所有配置项均通过 `.env` 文件管理，复制 `.env.example` 后按需修改。

---

## 核心配置

### 数据模式

```ini
DATA_MODE=jnb
```

| 值 | 说明 | 依赖 |
|---|------|------|
| `jnb` | 接入 Tushare 真实行情数据 | 必须配置 `TUSHARE_TOKEN` + `TUSHARE_API_URL` |
| `websearch` | 纯 LLM 对话模式，不走行情接口 | 无需 Tushare 配置 |

---

## 数据层配置（仅 jnb 模式）

### Tushare API

```ini
TUSHARE_TOKEN=你的56位token
TUSHARE_API_URL=https://tt.xiaodefa.cn
TUSHARE_VERIFY_TOKEN_URL=
```

| 变量 | 必填 | 说明 |
|------|------|------|
| `TUSHARE_TOKEN` | 是（jnb） | Tushare Pro 的 56 位 Token，在 https://tushare.pro/user/token 获取 |
| `TUSHARE_API_URL` | 是（jnb） | 中转 API 地址，如 `https://tt.xiaodefa.cn` |
| `TUSHARE_VERIFY_TOKEN_URL` | 否 | 实时行情验证地址，一般不需要 |

**注意**：如果 `DATA_MODE` 不是 `jnb`，这些配置可以为空，程序不会报错。

---

## LLM 配置（可选）

```ini
LLM_API_KEY=你的API密钥
LLM_BASE_URL=https://api.minimaxi.com/v1/chat/completions
LLM_MODEL=MiniMax-M3
```

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_API_KEY` | **否** | 未配置时，系统只做意图识别+知识库检索，不生成回答 |
| `LLM_BASE_URL` | 否（有 Key 时填） | OpenAI 兼容格式的 API 地址 |
| `LLM_MODEL` | 否（有 Key 时填） | 模型名称，默认 `MiniMax-M3` |

**支持的 LLM 提供商**：目前支持 OpenAI 兼容格式的 API（MiniMax、OpenRouter、通义千问等）。

---

## 向量知识库配置（可选，默认关闭）

```ini
# KB_ENABLED=true  # 取消注释以启用
# KB_API_URL=http://localhost:8000
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KB_ENABLED` | `false` | 设为 `true` 开启向量知识库检索 |
| `KB_API_URL` | `http://localhost:8000` | 知识库 API 地址（参考 knowledge-base 项目） |

**知识库依赖**：
- Qdrant 向量数据库（localhost:6333）
- Ollama Embedding 模型（localhost:11434）
- FastAPI 知识库服务（localhost:8000）

**未开启知识库时的行为**：
- 意图识别 ✅ 正常
- 角色框架 ✅ 正常（career/life 用本地 prompt 文件）
- LLM 生成 ✅ 正常（配置了 Key 的话）
- 知识库检索 ❌ 跳过（不影响其他功能）

---

## 数据库配置

```ini
DATA_DIR=data
DB_PATH=data/stock_data.db
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATA_DIR` | `data` | 数据目录 |
| `DB_PATH` | `data/stock_data.db` | SQLite 数据库路径，支持绝对/相对路径 |

---

## 开发与生产隔离（us 分支）

公司、家里分别使用自己的开发 SQLite；正式自选、交易、备注、行情同步统一在线上操作。三份库不合并，不自动相互复制。前端开发仍访问本机后端；不要把本地开发请求改为正式写库。

- 开发：`DB_PATH=data/stock_data.db`（相对于项目根目录）。需要测试数据时，在本机显式初始化测试库或使用自己的离线快照，不从 Git 获取数据库。
- 生产：在服务器自己的 `.env` 中配置 `DB_PATH=/www/zgnb/data/stock_data.db`。该路径是示例，必须与现有服务实际路径一致。
- `data/` 下 SQLite 主库、WAL/SHM、备份均忽略；静态股票池 `data/nasdaq100.json` 仍受版本控制。自定义导出到其他目录时须自行确保不入 Git。
- 两台机器的 `.env` 均不入 Git。环境变量优先于 `.env`，需要检查 systemd 是否已有同名环境变量；修改后重启后端才对运行服务生效。
- **换机首次拉取删除受控数据库的提交前，先停本地写入并备份/移出本地库（含 WAL/SHM），或用下述工具导出一致性快照。** Git 可能删除旧的受控副本；拉取后在忽略路径下恢复自己的开发数据。
- 当前未推送的旧提交 `20aaf64` 仍包含超限数据库。忽略规则和停止跟踪不能清掉历史大对象；推送前须另行授权整理尚未推送的历史，保留功能与本地数据后重新检查待推送对象大小。本次没有改写历史。

### Yahoo 代理与只读检查

us 分支美股/港股同步直接使用 Yahoo Chart API，不依赖 Tushare Token；上面的 Tushare 模式说明不改变此同步入口。已有指标计算配置继续沿用。

| `YAHOO_PROXY` | 行为 |
|---|---|
| 没有设置 | 使用 `http://127.0.0.1:7890` |
| `http://127.0.0.1:7890` 或其他非空地址 | 使用指定代理，失败明确报错，不自动改走直连 |
| 空值 `YAHOO_PROXY=` | 显式直连，仅适用于服务器出口本身可访问 Yahoo 的情况 |

Yahoo 会话不会被全局 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`（含小写形式）覆盖或绕过；不会改动其他客户端的环境变量。代理地址、订阅和凭据不得入库或写入日志。

服务器必须有**自身可用的代理服务**：选择允许使用、实测能获取 Yahoo 行情的出口，仅监听回环地址，并配置开机启动。家里电脑的 Clash 不会自动提供给服务器，服务器的 `127.0.0.1` 指服务器本身。此工具包不安装代理或节点。

在项目根目录运行（服务器使用自身虚拟环境 Python）：

```bash
python scripts/check_yahoo_connection.py
# 服务器示例：/www/zgnb/venv/bin/python /www/zgnb/scripts/check_yahoo_connection.py
```

检查与生产共用会话和 Chart 解析，固定查询最近 30 天 AAPL，输出有效 K 线条数和实际首末日期。不连接数据库、不入库、不打印完整响应。退出码 `0` 为取得有效数据，`2` 为没有有效 K 线，`1` 为请求/解析失败。返回成功也要核对实际日期是否符合预期；不能用网页能打开代替该检查。

### 日常只发布功能

两台开发机都在自己的 us 检出目录使用仓库内入口。以下为 PowerShell 命令，目标与密钥必须替换成自己的配置：

```powershell
.\scripts\update_server.ps1 -DryRun -Server 'deploy@example.com' -SshKey "$env:USERPROFILE\.ssh\zgnb_deploy_key"
# 确认范围后，手工去掉 -DryRun 才会实际发布。
# 加 -Frontend 只发布前端，加 -Backend 只发布后端；不加则发布前后端。
```

- `-Server` 也可由本机 `ZGNB_DEPLOY_SERVER` 环境变量提供；`-SshKey`、`-RemoteDir`（默认 `/www/zgnb`）、`-ServiceName`（默认 `zgnb-api`）均可指定。SSH 主机指纹应预先通过可信渠道验证；脚本不会跳过检查。
- `-DryRun` 仅显示范围与目标，不构建、不打包、不联网、不重启。旧 `-Db` 无条件拒绝，包含 `-DryRun -Db` 也拒绝。
- 后端仅包含 `api/`、`modules/`、`scripts/` 的允许代码类型、规则、知识文档、运行时模板 `SKILL.md`、依赖声明与 `data/nasdaq100.json`。前端仅包含构建产物允许类型。
- 不上传 `.env`、其变体、数据库、备份、日志、密钥、代理订阅或节点文件。不清理远端其他文件；不会自动初始化、迁移或重置数据库。
- 服务器须已存在独立 `.env`、`venv/bin/python`（Python 3.10+）、所需运行依赖，以及后端服务；SSH 用户须有代码写权限和必要的服务重启权限。本脚本上传依赖声明，但不自动升级服务器依赖，新增依赖需单独核对安装。
- 构建、打包、上传、应用、重启、健康检查逐步检查退出码；任一失败停止。健康检查通过 SSH 请求 `127.0.0.1:8000`，不保存网站登录密码。
- 包先校验路径并完整暂存，再逐文件替换；这不是跨文件原子发布或自动回滚。应用/重启失败时可能已更新部分功能文件，应修复后重新发布，不能以覆盖数据库方式回滚。
- 本地临时目录及服务器 `.deploy/<发布ID>` 保留功能包便于排查，按输出路径另行管理；不会自动清理任何正式数据。旧外层入口只转发，其他电脑直接使用仓库内入口。

### 一次性初始化快照（与发布独立）

本地执行：

```bash
python scripts/export_database.py
# 可显式指定源库，或使用 --output 指向一个尚不存在的新快照文件：
python scripts/export_database.py --source data/stock_data.db
```

默认读取本机 `DB_PATH`；默认输出到源库旁 `backups/exports/`，名称带 UTC 时间戳和随机后缀。复用 SQLite backup API，包含已提交但尚在 WAL 的记录；只读打开源库，对快照执行完整性校验，输出路径、SHA-256、各表条数和最新行情日期。源库不存在、输出等于源库或输出已存在均失败，不覆盖旧快照。工具只导出，绝不上传。

线上尚未正式使用时，可以在另行确认后用该快照初始化一次。操作顺序：

1. 核对功能版本与数据库结构兼容，确认源数据和目标 `DB_PATH`，将快照上传到**非正式库路径**的暂存位置，校验 SHA-256 与 `PRAGMA integrity_check`。
2. 先为线上旧库制作可恢复的一致性备份。进入维护窗口，停止后端及所有可能写库的同步进程/定时任务，确认没有残留写入者。
3. 保留旧库及 WAL/SHM 的恢复副本；确认旧 WAL 的已提交内容已经安全备份/checkpoint，再隔离旧伴随文件。**不能将新主库与旧 WAL/SHM 混用，也不能在运行时直接覆盖主库。**
4. 单独切换快照为正式库并核对属主/权限；不覆盖生产 `.env` 和代理配置。启动服务，验证健康、自选/交易/备注、实际行情日期，再验证线上同步按钮及指标计算。
5. 线上正式使用后，不再用开发库覆盖正式库；后续表结构变化采用单独、显式迁移。

未完成服务器代理、真实 Chart 检查、初始化与线上同步验收前，只能称“代理支持与工具就绪”，不能称“线上行情更新已恢复”。服务器接入、实际部署、数据库上传和 Git 历史整理均不由这些本地验证自动执行。

---

## 配置示例

### 最小配置（纯对话，无需任何外部服务）

```ini
DATA_MODE=websearch
```

### 股票分析模式（需 Tushare）

```ini
DATA_MODE=jnb
TUSHARE_TOKEN=ba0930...fa15
TUSHARE_API_URL=https://tt.xiaodefa.cn
```

### 完整模式（股票 + LLM + 知识库）

```ini
DATA_MODE=jnb
TUSHARE_TOKEN=ba0930...fa15
TUSHARE_API_URL=https://tt.xiaodefa.cn
LLM_API_KEY=sk-cp-...ULLC
LLM_BASE_URL=https://api.minimaxi.com/v1/chat/completions
LLM_MODEL=MiniMax-M3
KB_ENABLED=true
KB_API_URL=http://localhost:8000
```
