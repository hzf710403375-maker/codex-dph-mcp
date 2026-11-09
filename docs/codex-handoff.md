# Codex 交接指引

项目已注册且 worker 已启动后，在 Codex 中按下述流程使用 codex_dph MCP。

1. 确认任务目标、范围和验收条件。Codex 规划和审核，执行器修改专用 worktree。
2. list_projects、executor_status 确认项目和接口。不要让用户把 API key 发到聊天。
3. 组织 spec：project_id、title、objective、allowed_paths、acceptance_criteria、check_ids。检查只能引用 CLI 已批准 ID。
4. 用唯一幂等键 submit_task。网络重试复用原键；输入变化用新键。
5. 保存 run_id，通过 get_run、read_events 查询，不重复启动。聊天重连用 list_runs 恢复。
6. awaiting_review 时先看任务、get_diff 的全部分页和独立检查日志，对照验收审查代码、新增文件及测试修改，再读执行器的说明，减少先入为主。get_artifacts 提供全部工件位置。
7. submit_review 必须指定 candidate_commit；criteria 按验收条件顺序填写 pass/fail。issues 提供位置、影响和修复预期。
8. 通过为 accepted；问题为 changes_requested，并用 review_id 调用 revise_task。返工后审核新快照，不直接修改执行工作区。
9. 检查失败且存在候选快照的 blocked 可退回修复。认证和环境问题先排除，不当作代码缺陷无限返工。
10. 取消后查询到 cancelled 再称已停止。interrupted 说明连续性不明，指导本机对账，不自动放宽权限。
11. accepted 只表示此候选提交通过审核；交付分支、版本和证据，不称已合并或部署。

## 上下文整理

派单前，把聊天中影响实现的结论写入 spec.context：requirements_summary、design_decisions、constraints，以及相关已提交文档的相对路径 documents。不要只写“按上面的讨论”。项目公共背景由已提交的 .ai-workflow/context.json 提供；服务还会自动收集约定的需求、架构、验收文档。

审核前可调用 get_context 核对完整背景、来源版本和摘要；这是完整审计包。执行器收到的是有预算限制的执行视图，通过 get_artifacts 的 context_metrics、execution_context_hash 和 context_archive 核对发送量及完整资料位置。未内嵌文档可用视图中精确的 Git 命令读取。最近一轮审核完整发送，旧审核按预算提供预览并保留完整归档。

返工不必手动复制历史审核，服务会从当前任务的真实历史累积；不要把旧问题预览当作已修复证明，仍须对照候选差异与完整历史检查。新需求或正式设计变化应新建任务，避免混入旧验收条件。
