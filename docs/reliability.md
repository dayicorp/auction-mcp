# P3.4 运行时可靠性与故障诊断

三项门禁均为离线、跨工作目录、无浏览器流程。它们不会传入
`--run-live`，不会读取 Cookie、Token 或浏览器状态，也不会访问外部网络。

## 原始 stdio 混沌与压力

```bash
python scripts/runtime_chaos.py --mode chaos
python scripts/runtime_chaos.py --mode all
```

成功时输出 `RUNTIME_RELIABILITY_DIAGNOSTIC=<json>` 与
`RUNTIME_RELIABILITY: PASS`。`all` 的 `process_stress.lifecycle_count` 必须为
260，连同七个混沌场景总计 267 个真实 `server.py` 生命周期；
`processes_reaped` 必须等于 260，`network_blocked`、`external_cwd` 和
`stdout_protocol_clean` 必须为 true。Windows 用父进程 handle 数、Linux 用
`/proc/self/fd` 检查门禁自身资源增长；每个子进程仍必须被 `wait()` 回收。
Windows 的每个生命周期另有独立 Job Object，并以 Job 内核成员身份验证活跃
进程归零，避免系统复用 PID 后把无关进程误判为 MCP 子进程；异常路径关闭 Job
时用 `KILL_ON_JOB_CLOSE` 回收整棵树。父进程退出后最多用 1 秒等待 Job accounting
归零；超时会报告 Job 内真实 PID 并硬失败。Linux 使用每进程独立
session/process group，并在退出后验证进程组不存在。

常见错误：

- `network guard self-test failed`：`sitecustomize` 未生效或外部连接被放行，禁止绕过后重跑。
- `stdout protocol pollution`：server stdout 出现非 JSON-RPC 文本；将日志改到 stderr，不能放宽解析。
- `stderr exceeded 64 KiB`：诊断失控；压缩错误输出或修复重复异常，不能提高上限掩盖问题。
- `did not exit before timeout`：子进程未响应 EOF；检查 MCP 循环和资源关闭，不得增加无界等待。
- `parent process resource count leaked`：管道、线程或进程句柄未回收；检查所有异常路径的 `finish/abort`。
- `Windows Job Object retained` / `POSIX process groups survived`：MCP 进程树仍有活跃成员；不得用全局父 PID 快照替代 Job/process-group 身份验证。

## 覆盖率

```bash
python scripts/coverage_gate.py
```

`coverage_contract.json` 记录了基线提交、实测语句75.597%、分支66.25%和
综合72.881%，并冻结最低75.5%/66.2%/72.8%。`COVERAGE_DIAGNOSTIC` 同时报告当前全局三项覆盖率以及
`safety_core.py` 的语句/分支覆盖率。关键文件任一缺失行、缺失分支或低于
100% 都会失败；Live、登录和真实 Cookie 路径不在离线覆盖目标中，也不会
用排除标记伪装为已覆盖。

## 变异测试

```bash
python scripts/mutation_gate.py
```

门禁把每个变异应用到独立临时仓库并执行真实 pytest。类别包括条件反转、
边界删除、错误码替换、默认值改变、守门绕过和 Schema 漂移。
`MUTATION_DIAGNOSTIC` 的 `killed` 必须等于 `total`（当前为 12），
`survived` 必须为 0。源片段不唯一、测试超时或 pytest 基础设施退出都属于
门禁失败，不能当作“已杀死”。

## CI 对应关系

- 六个 `release-gate`：Windows/Linux × Python 3.10/3.12/3.14 clean-room。
- 两个 `runtime-stress`：Windows/Linux × Python 3.12，执行完整 267 生命周期。
- 一个 `mutation-coverage`：Linux × Python 3.12，先覆盖率后 12 个变异。

所有九个任务必须 success；压力或变异任务仅触发不代表完成。
六个 clean-room 任务还会分别输出 `RELEASE_DIAGNOSTIC` 与
`CLEANROOM_DIAGNOSTIC`；失败 JSON 的 `stage` 是精确失败阶段，成功时均为
`complete`，且 clean-room 必须报告 `temporary_environment_removed: true`。
