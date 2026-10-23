# Windows 验证记录

功能验证日期：2026-10-20。2026-10-23 整理为 Windows 专用版，保留以下 Windows 验证结果和边界。

## 已验证

- 2026-10-23 本机 Windows 上 34 项 unittest 全部通过，含原有 23 项及 11 项平台测试。
- Windows GetSystemTimes / GlobalMemoryStatusEx 实测成功：CPU 差分、约 32 GB 物理内存。
- NVIDIA GeForce RTX 5070 Ti 的 nvidia-smi 整卡和进程查询成功。
- WDDM 进程显存 N/A 使用样本回归验证，保持未知值，不伪造为零。
- 通过 start-windows.cmd 启动，验证首页、JS/CSS、状态、历史、诊断接口以及 SSE。
- CTRL_BREAK_EVENT 正常退出，启动器退出码 0，无异常堆栈。
- Python 语法编译和 git diff --check 通过。
- 本机 lms log stream --help 确认支持项目使用的日志订阅参数。

## 验证边界

- 初次验证时 LM Studio API 未启动；后续在用户已加载的 Bionic 模型 `qwen3.8-27b-gsq-rco-mtp` 上完成真实短请求验收，采集到 Runtime 完成任务、Prefill、Decode、Token、上下文及独立完成统计和 TTFT。32 Token 请求实测平均解码约 7.48 tok/s，TTFT 约 558 ms。
- 已修复旧 CLI 的 Bionic 服务发现和凭据目录兼容：使用发布的 SDK 端口，并仅为 CLI 子进程提供隔离 home 指针，使其正常读取应用自己的凭据；不复制凭据或修改全局用户配置。
- 取消和模型切换未在真实 Windows 模型上执行，本次不卸载或重启用户的模型服务。
- 最初短请求验证期间未观察到 `tg_3s`；后续真实 agent 运行中已收到实时速度日志，历史采样约 33–85 tok/s。无有效日志或速度过期时仍显示不可用，不通过估算伪造实时速度。
- 已修复趋势图在有效采样多于三个时漏画孤立点的问题，断开区间仍保留，不补零。
- 真实 Windows 多并发尚未验收；现有并发任务隔离由日志样本回归覆盖。
- 本机未安装 Playwright，此次未执行 Chromium 浏览器验收。
- CI 仅配置 Windows 的 Python 3.10/3.12/3.13 回归与 Chromium 验收，远端执行结果以 GitHub Actions 为准。
- 本次本地启动验证使用 Codex 提供的 Python；项目运行时仍仅依赖 Python 3.10+ 标准库。

## 使用

安装 Python 3.10+ 后，双击项目内的 `start-windows.cmd`。自定义 Python 路径可通过 `MONITOR_PYTHON` 指定。
访问 http://127.0.0.1:8090，在 LM Studio Developer 页面启动本地 API 后会自动连接。
详细配置见 README.md。
