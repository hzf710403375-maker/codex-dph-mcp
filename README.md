# Codex DPH MCP

本地 Codex → DeepSeek Harness 任务交接工具。Codex 负责规划和审核；独立 worker 执行编码、冻结 Git 快照、运行检查。通过 MCP 或 CLI 查询进度，审核不通过可创建返工。

状态：实验性本地开发工具。支持任务排队、独立执行、代码快照、检查、审核与返工。已在本地项目验证真实模型交接；公开包不包含原项目代码、业务资料和运行记录。

## 安装

需要 macOS、Git、Python 3.11+、uv，以及 DeepSeek 所需的 Node.js。

```sh
uv sync --extra dev
npm install --prefix .tools --ignore-scripts --no-audit --no-fund @deepseek-ai/dsh@0.2.2-rc.1
.venv/bin/codex-dph doctor
```

优先使用项目 `.tools` 中的官方版本，否则检查 PATH 中的 dsh。不会修改全局 DeepSeek。`supported=true` 表示参数定义匹配，不表示认证或模型调用已经成功。Python 依赖由 `uv.lock` 锁定。

## 首次使用

1. 目标项目须是有首个提交的 Git 仓库，主工作区干净，包括未跟踪文件。
2. 创建 `checks.json`，将检查 ID 映射到 argv 数组。例如：

```json
{"tests": ["/absolute/path/to/python", "-m", "pytest", "-q"]}
```

3. 注册目标项目，记下输出的 project_id：

```sh
.venv/bin/codex-dph register /absolute/path/to/project --checks /absolute/path/to/checks.json
```

4. 已在 Harness 桌面端保存模型设置和凭据时，直接复用：

```sh
./scripts/start-worker.sh
```

启动脚本默认每任务读取 `~/.dsh/profiles/desktop/cordis.patch.yml` 及 home 级模型覆盖，继承默认 provider、model、reasoningEffort 和支持的 provider 参数；显式启用 headless 默认关闭的 credentials-local，并指向原来的 `~/.dsh/.credentials.yaml`。不解析或复制密钥，不需要重新输入 Key。UI、项目列表和原聊天会话不随模型设置同步。任务状态仍使用独立 `.runtime/worker-dsh`，不会改写桌面 profile。

可用 `CODEX_DPH_DSH_CONFIG_HOME` 指定原 Harness 配置目录，`CODEX_DPH_DSH_SETTINGS_PROFILE` 指定设置来源（当前支持已有模型插件的平铺配置条目，默认 desktop）。在软件里保存模型设置后，下一任务重新读取，正在执行的任务不切换。缺少明确默认模型、未知补丁结构或内联密钥配置会报错，不静默改用其他模型。当前支持 agent-default-model、llm-pi-ai、llm-deepseek 三项；账号登录型 provider 和额外插件尚未验证。

无桌面配置的独立部署，也可由本机凭据管理提供 `DEEPSEEK_API_KEY` 后使用基础模式：

```sh
.venv/bin/codex-dph worker
```

不要将密钥写入聊天、任务 JSON 或 Git。Headless 不保证继承 Web 界面认证。如需自定义 DSH_HOME，在启动 worker 的终端设置。

5. 在 Codex MCP 配置中加入以下内容，替换为项目实际绝对路径：

```toml
[mcp_servers.codex_dph]
command = "/absolute/path/to/codex-dph-mcp/.venv/bin/codex-dph"
args = ["serve"]
```

worker 和 MCP 使用同一个状态目录。默认 `~/Library/Application Support/codex-dph`；可用 `--home /absolute/path` 或 `CODEX_DPH_HOME` 指定。MCP 不会自动启动 worker。

6. 按 [Codex 交接指引](docs/codex-handoff.md) 派单和审核。

## CLI 提交示例

保存为 `task.json`，替换 project_id、目标和范围：

```json
{
  "project_id": "注册得到的ID",
  "title": "修复输入校验",
  "objective": "空输入返回清晰错误，合法输入保持现有行为",
  "allowed_paths": ["src/**", "tests/**"],
  "acceptance_criteria": ["空输入返回清晰错误", "现有测试保持通过"],
  "check_ids": ["tests"],
  "timeout_seconds": 1800,
  "max_revision_rounds": 3
}
```

```sh
.venv/bin/codex-dph submit task.json --key unique-request-001
.venv/bin/codex-dph runs
.venv/bin/codex-dph status RUN_ID
.venv/bin/codex-dph artifacts RUN_ID
.venv/bin/codex-dph cancel RUN_ID
```

`base_commit` 可指定本仓库提交；默认提交时的 HEAD。下一任务可使用已通过任务的 candidate_commit。检查配置在提交时冻结。重复注册不覆盖已有检查；使用 `configure-checks PROJECT_ID checks.json` 更新后续任务的检查。

## 工作流和恢复

`queued → preparing → running → checking → awaiting_review → accepted`

Codex 读取任务、全部差异、实际检查证据；通过 submit_review 指定 candidate_commit，逐项填写 pass/fail。有问题则 changes_requested，再用 review_id 调用 revise_task。返工创建新 run 并复用工作区，受轮数限制。检查失败进入 blocked，可提交修复审核，但不能 accepted。

关闭 Codex 后，独立 worker 在电脑保持运行时继续编码，完成后等待审核。重新连接使用 list_runs 找回任务。

取消先进入 cancelling。只有 cancelled 表示受监督进程已停止。系统禁止进程查询或无法确认清理时进入 interrupted 并保留仓库锁；请在普通本机终端运行 worker。

worker 崩溃后不自动重放。停止 worker，检查并停止遗留进程，然后显式对账：

```sh
.venv/bin/codex-dph reconcile RUN_ID
```

此命令只取消旧记录、保留工作区；不恢复会话、不重复执行。任务进程或旧 PID 仍存活时拒绝解锁。

## 当前边界

- 面向单用户的可信本地项目；worktree 隔离源文件，但共享 Git 元数据。worker 禁用 Git hooks，执行器不额外获准写共享 Git 目录。
- 进程标识用于清理正常继承环境的后代；不能保证处理恶意清除标识的程序。执行器被明确要求不启动后台服务。
- 不自动合并、推送、部署、安装业务依赖或复制 .env。依赖和数据库须预先准备。
- 检查不继承 worker 的模型 API 环境变量，但同用户可信程序仍能访问用户目录。
- 原始日志可能含业务内容，尚无通用脱敏或自动历史清理。状态目录仅限当前用户访问。
- 默认每轮30分钟，最多三轮返工，累计执行上限两小时。没有严格金额限额。
- 单日志文件上限20 MiB，交付文件上限5 MiB。独立检查验证当前候选，尚未自动运行历史基准检查。
- 首期提供交接文档，未自动安装全局 Skill 或修改 Codex 配置。

## 验证

```sh
.venv/bin/pytest -q
```

测试使用临时 Git 仓库、独立子进程和真实 MCP stdio 客户端。进程查询受限时，在普通本机终端验证，不应把失败当通过。

更多信息见 [Codex 交接指引](docs/codex-handoff.md)、[安全与运行边界](SECURITY.md) 和 [公开包检查记录](PUBLICATION_CHECK.md)。

## 统一上下文包

每次提交任务时，服务自动冻结一份完整上下文包用于审计，再生成精简的执行视图交给执行器。返工保留历轮审核意见和对应版本、检查结果。Codex 可通过 `get_context` 分页读取完整包，或通过 `get_artifacts` 读取完整工件和发送量指标。CLI 可执行：

```sh
.venv/bin/codex-dph context RUN_ID
```

在目标项目提交 `.ai-workflow/context.json`，作为所有任务的公共背景：

```json
{
  "requirements_summary": "本项目用于管理本地开发任务",
  "design_decisions": ["沿用 SQLite 保存状态"],
  "constraints": ["不引入新依赖", "保持现有接口兼容"],
  "documents": ["docs/design.md"]
}
```

单项任务也可在 task.json 或 submit_task 的 spec 中添加 `context`，字段相同，例如：

```json
{
  "context": {
    "requirements_summary": "本轮只修复重复提交导致的重复执行",
    "design_decisions": ["复用现有幂等键机制"],
    "constraints": ["不改数据库表结构"],
    "documents": ["docs/idempotency.md"]
  }
}
```

上例是任务中的附加字段，仍须提供 project_id、title、objective 等原有字段。项目与任务的设计决策、约束、文档列表合并保留；两个层级的需求摘要分别保存。

若基准版本中存在，服务还会自动带上 `AGENTS.md` 和 `.ai-workflow/requirements.md`、`architecture.md`、`acceptance.md`。显式列出的文档必须存在于该 Git 版本，不能是符号链接。每份文档记录内容、来源提交、Git blob 和内容摘要；每次上下文包也有独立摘要。

文档内容在提交时冻结，之后主目录或执行器对文档的修改不会改写旧上下文；返工沿用最初设计背景。需求正式变化时新建任务。每份文档最多64 KiB，最多32份，整包含审核记录最多256 KiB；超过会报错，不静默截断。

执行视图最多32 KiB：完整保留需求摘要、约束、设计决策和最近一轮审核；优先内嵌根目录 `AGENTS.md` 与本任务明确引用的文档，放不下的文档保留版本、内容摘要和精确的 Git 读取命令。其他公共文档按需读取。旧审核保留版本、问题数量、失败检查数量，并利用剩余空间提供问题预览；完整记录保存在只读归档。旧问题不会自动标为已解决。必需字段本身超限时明确报错。

整份任务提示词最多64 KiB。`context_metrics` 记录完整包、执行视图和提示词的字节数，以及内嵌和按需文档数量；`execution_context_hash` 标识执行视图，`context_hash` 标识完整包。字节数不等于模型 token 数，也不直接代表费用下降比例。摘要整理使用确定性规则，不额外调用模型。

**聊天全文不会自动同步。**Codex 需要把重要讨论结论整理进任务 context 或已提交的项目文档。上下文包是双方可核对的任务资料，也不等于复用 DeepSeek 原会话。

进度查询与任务列表只返回上下文摘要、文档路径和审核轮数，完整内容在明确调用 get_context/get_artifacts 时获取。旧任务仍可运行；旧任务首次返工若需要构建上下文，将从最初输入提交读取，避免把执行器修改的设计当作原始要求。

## 发布与许可

本项目为独立开发的集成工具，不代表 OpenAI 或 DeepSeek 官方产品。DeepSeek Harness 与其他依赖需单独安装，其许可与服务条款由相应项目规定。

本项目采用 [MIT License](LICENSE)。依赖项目保留各自许可证；本仓库不包含第三方依赖的源代码或安装目录。
