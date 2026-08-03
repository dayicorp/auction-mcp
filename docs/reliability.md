# P3.4-P3.6 运行时、资源与发布制品可靠性诊断

三项门禁均为离线、跨工作目录、无浏览器流程。它们不会传入
`--run-live`，不会读取 Cookie、Token 或浏览器状态，也不会访问外部网络。

## 原始 stdio 混沌与压力

```bash
python scripts/runtime_chaos.py --mode chaos
python scripts/runtime_chaos.py --mode all
```

成功时输出 `RUNTIME_RELIABILITY_DIAGNOSTIC=<json>` 与
`RUNTIME_RELIABILITY: PASS`。`all` 的 `process_stress.lifecycle_count` 必须为
520，连同七个混沌场景总计 527 个真实 `server.py` 生命周期；
`processes_reaped` 必须等于 520，`network_blocked`、`external_cwd` 和
`stdout_protocol_clean` 必须为 true。Windows 用父进程 handle 数、Linux 用
`/proc/self/fd` 检查门禁自身资源增长；每个子进程仍必须被 `wait()` 回收。
Windows 的每个生命周期另有独立 Job Object，并以 Job 内核成员身份验证活跃
进程归零，避免系统复用 PID 后把无关进程误判为 MCP 子进程；异常路径关闭 Job
时用 `KILL_ON_JOB_CLOSE` 回收整棵树。父进程退出后最多用 1 秒等待 Job accounting
归零；超时会报告 Job 内真实 PID 并硬失败。Linux 使用每进程独立
session/process group，并在退出后验证进程组不存在。

`early_eof` 先完成真实 initialize 与 initialized notification，再对下一条 ping
写入半包并关闭 stdin。只有服务器已就绪后才开始10秒退出计时，因此冷解释器、
文件系统或杀毒扫描的启动抖动不会被误报为 EOF 泄漏；进程退出后仍必须完成
stdout/stderr 边界、Windows Job 或 POSIX process group 清空以及 guard 注销。

压力阶段每完成25个顺序生命周期、以及每个并发 worker 的20个生命周期，就输出
`RUNTIME_STRESS_CHECKPOINT`。它记录轮次、阶段、累计数、最慢生命周期和当前父
进程资源数，并再次要求 MCP 进程、I/O线程和 Job 句柄为零。若任一协议阶段超过
原30秒阈值，失败诊断会报告进程状态、Job内PID、资源数、线程名、stdout消息数
以及 stderr 长度和 SHA-256；不直接回显 stderr 内容，避免诊断本身泄露敏感数据。

P3.5 资源门禁先记录 `cold`，强制同时创建并关闭 8 个 ThreadPool worker 后
记录 `warmed`，再分别记录 `after_round_1` 和 `after_round_2`。每个阶段包含总
handle/fd 计数及三类可归属资源：Python线程、活跃MCP进程和打开的Windows Job
句柄。`warmup_resource_delta` 只用于归因一次性运行时初始化；两轮压力仍分别受
原有 `+8` 上限约束，且第二轮不得在 `warmed` 基线之上继续增长。每个计数阶段
执行三次有界采样，不以单个瞬时读数判定泄漏。

常见错误：

- `network guard self-test failed`：`sitecustomize` 未生效或外部连接被放行，禁止绕过后重跑。
- `stdout protocol pollution`：server stdout 出现非 JSON-RPC 文本；将日志改到 stderr，不能放宽解析。
- `stderr exceeded 64 KiB`：诊断失控；压缩错误输出或修复重复异常，不能提高上限掩盖问题。
- `did not exit before timeout`：子进程未响应 EOF；检查 MCP 循环和资源关闭，不得增加无界等待。
- `parent process resource count leaked`：管道、线程或进程句柄未回收；检查所有异常路径的 `finish/abort`。
- `continued growing after warmup`：资源已超过预热基线且第二轮继续增长；这是持续泄漏，不得归类为一次性初始化。
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
- 两个 `runtime-stress`：Windows/Linux × Python 3.12，执行完整 527 生命周期与四阶段资源诊断。
- 一个 `mutation-coverage`：Linux × Python 3.12，先覆盖率后 12 个变异。

所有九个任务必须 success；压力或变异任务仅触发不代表完成。
六个 clean-room 任务还会分别输出 `RELEASE_DIAGNOSTIC` 与
`ARTIFACT_DIAGNOSTIC`、`CLEANROOM_DIAGNOSTIC`；失败 JSON 的 `stage` 是精确
失败阶段，成功时均为 `complete`，且 clean-room 必须报告
`temporary_environment_removed: true`。

## 可复现制品与安装消费端

```bash
python scripts/verify_artifact.py
```

该门禁要求 `build==1.5.0`、`setuptools==83.0.0`、`wheel==0.47.0`，从显式
源码白名单创建两个互不复用的构建树。仓库内 PEP 517 后端固定 sdist 的
gzip/tar 时间、uid/gid、用户名、权限和成员顺序；wheel 与 sdist 的文件名和
SHA-256 必须分别一致。归档不得包含 tests、scripts、Live、缓存、浏览器状态或密钥类文件。
wheel 必须包含五个运行模块、四份 JSON 资源、MIT 许可证、Python >=3.10
元数据、三项运行依赖以及唯一 `auction-mcp = server:main` 入口。

构建审计后，门禁创建第三个 venv，直接安装 wheel 及其声明依赖并运行
`pip check`。安装后的 `server` 与 `auction_mcp_assets` 必须来自该 venv，不能
回退到源码仓库；随后从外部 cwd 调用控制台入口，在 socket 强制断网下完成真实
MCP initialize、tools/list、12工具 Schema、五个安全调用、stderr 和异常/超时
回收检查。

常见错误：

- `bytes are not reproducible`：检查构建时钟、文件顺序或后端漂移；不得只比较解包内容掩盖不可复现字节。
- `forbidden distribution members`：收紧 `MANIFEST.in` 或 package-data；不得用忽略规则跳过敏感制品。
- `source-checkout import leakage`：安装入口仍依赖仓库 `PYTHONPATH`；修复打包资源或模块声明。
- `installed consumer ... failed`：先看同一输出中的 `ARTIFACT_DIAGNOSTIC.stage`，再按安装、契约、协议或断网阶段定位。
