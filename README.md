# LM Studio Monitor · V1

本机 LM Studio 的中文监控页面。仅使用 Python 3.10+ 标准库、原生 HTML/CSS/JS，无需 pip/npm 安装。第一版针对 Linux + NVIDIA + LM Studio 原生 v1 API，已在本机 llama.cpp 2.41.0 验证。

## 启动

```bash
cd lmstudio-monitor
python3 server.py
```

打开 http://127.0.0.1:8090 。LM Studio 未启动时仍采集 GPU 和系统指标。监控程序不会自动启动 LM Studio、加载模型或发送推理请求。

LM Studio 需要启用本机 API，默认 http://127.0.0.1:1234 。已有 CLI 可使用：

```bash
~/.lmstudio/bin/lms daemon up
~/.lmstudio/bin/lms server start --bind 127.0.0.1 --port 1234
```

模型可在 LM Studio 中加载。监控程序自动连接日志，断开后重试。命令行配置：

```bash
python3 server.py --host 127.0.0.1 --port 8090 --lm-url http://127.0.0.1:1234
```

环境变量 `LM_URL`、`LMS_BIN` 可覆盖本机 API 和 CLI 路径。API 启用认证时，通过环境变量 `LM_API_TOKEN` 提供令牌；不要写入源码。当前 CLI 日志始终读取本机 daemon，因此 API 地址限制为本机。

从其他电脑访问，推荐 SSH 转发（替换主机地址）：

```bash
ssh -L 8090:127.0.0.1:8090 yangdi@主机地址
```

然后在该电脑打开 http://127.0.0.1:8090 。也可显式使用 `--host 0.0.0.0` 在可信局域网提供访问；第一版页面没有登录认证。服务默认为 loopback，不修改防火墙。

## 指标口径

- 实时解码：Runtime `tg_3s`，约 3 秒一次。页面按模型活跃 slot 合计，趋势按所有活跃实例合计；12 秒未更新标为失效并停止展示速度。
- Prefill：Runtime `prompt eval` 的实际处理速度；请求结束才可获取。缓存复用使实际处理 Token 与 API 输入 Token 不一定相同。
- TTFT、平均解码、输入/输出 Token、总耗时和结束原因：模型 output 日志。只记录采集器在线期间完成的请求。
- 上下文：配置容量来自模型 API；占用为最后结束时 Runtime slot `n_tokens`。不等同于生成中的精确实时占用；并发时展示最近结束的 slot。
- KV：只展示已确认的 GPU Offload 配置；实际字节数、有效占用、命中率显示不可用。
- GPU：`nvidia-smi` 约每秒采集整卡利用率、显存、温度、功耗、频率及进程显存。其他进程的数据会计入整卡，不能全部归因于模型。
- CPU / RAM：Linux `/proc`；首次 CPU 采样不可用，第二次起显示差分。
- 连接状态：日志“连接已启动”表示订阅进程已启动；“事件已接收”表示实际收到事件。页面支持 SSE 自动重连。

## 数据与保留

`data/monitor.sqlite3`（可通过 `--db` 修改）每 5 秒保存一份采样，保留 24 小时；请求统计保留 7 天，页面展示最新 100 条。过期记录在采样时清理。日志不落盘；提示词和模型回答不进入数据库和监控 API。JSON 导出包含当前统计、进程信息及已加载的历史曲线。

页面不依赖外部 CDN。运行时日志字段属于版本相关适配，升级 LM Studio 后应复核；尚未验证所有并发、取消和 GUI 聊天场景。首次接入正在执行的请求时，可能要等待下一条 Runtime 速度事件。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py
~/.lmstudio/.internal/utils/node --check static/app.js
```

测试包括真实 Runtime 日志回放、slot 结束与新任务隔离、请求持久化和输出排除、采样保留和缺失值处理。

## Windows 后续开发交接

当前提交是经过 Linux + NVIDIA 实机验证的 V1 基线，尚未完成 Windows 适配，不保证在 Windows 直接完整运行。页面、HTTP/SSE 服务、SQLite 和大部分日志解析可以复用。建议下一位开发者按以下顺序处理：

1. 将 `Monitor.system_read()` 的 Linux `/proc` 访问替换为跨平台实现，例如 psutil。
2. 为 `lms` / `lms.exe` 和 `nvidia-smi` 添加 Windows 路径发现，并允许环境变量覆盖；当前 `LMS_BIN` 已支持覆盖。
3. 验证 Windows 进程终止和日志自动重连，必要时处理无控制台启动和子进程输出编码。
4. 复核 Windows LM Studio 的原生 v1 API 与 Runtime 日志字段；不能将 SSE 分片数当成 Token 数。
5. 添加 Windows 启动脚本，并在实际浏览器验收布局与交互。当前 GPU 采集只覆盖 NVIDIA。

不需要从 Linux 复制模型、数据库或 LM Studio 配置。Windows PC 应自行安装 LM Studio、Python 和对应 GPU 驱动，并加载本机模型。

`observe_long_context.py` 是手动观察用脚本：会连续发送三轮请求，每轮最多输出 1536 Token。只在模型已加载、容量足够且明确需要制造负载时运行；模型标识目前为 `qwen/qwen3-1.7b`。

本仓库不包含运行数据库、模型文件、凭据或本机测试结果。`VALIDATION.md` 是开发时验证记录；记录中的服务状态为当时状态，交接时本机 LM Studio 和监控服务已停止。
