# LM Studio Monitor · V2

本机 LM Studio 监控页面，面向 **Linux + NVIDIA GPU**。Python 3.10+ 标准库后端，原生 HTML/CSS/JavaScript 前端，无需安装运行依赖。Windows 适配留给独立分支。

## 启动

```bash
git clone https://github.com/blusssss/lmstudio-monitor.git
cd lmstudio-monitor
git switch v2
python3 server.py
```

浏览器打开 **http://127.0.0.1:8090**。监控不会自动启动 LM Studio、加载模型或发送推理请求。启动本机 LM Studio API 并加载模型后，页面自动连接：

```bash
~/.lmstudio/bin/lms daemon up
~/.lmstudio/bin/lms server start --bind 127.0.0.1 --port 1234
```

按 Ctrl+C 停止监控，订阅子进程会被关闭；LM Studio 由用户独立管理。

## 页面

- 首页：运行状态、实时解码、最近 TTFT、最近 Prefill 速度、GPU 利用率、显存、最近结束时上下文。TTFT 来自完成统计，Prefill 来自运行时；均显示更新时间。
- 趋势：按模型实例和 GPU 筛选；解码与资源分图、每图一个单位。可切换时间范围、资源指标和暂停绘图。
- **运行任务**：按实例引用、PID、slot、任务编号和采集会话区分，记录处理输入、生成、结束、取消、中断和错误。点击时间查看阶段耗时、Token、上下文与同期 GPU 曲线。
- **完成统计**：模型日志给出的输入/输出 Token、平均解码、TTFT 和总耗时。该来源未暴露稳定请求 ID，故单独显示，不按时间推测对应的并发任务。
- **模型与采集**：模型配置、KV 可用性、GPU 进程和各采集源状态。
- 支持统计 JSON 与脱敏诊断导出。页面不依赖外部 CDN，不保存提示词或回答。
- **清空历史**：一键删除所有实例的已结束任务、完成统计、历史模型选项、趋势与诊断记录，保留当前正在运行的任务和已加载模型。清空后继续采集新数据；其他打开的标签页会同步移除旧历史。

## 指标口径与边界

| 指标 | 来源及口径 |
|---|---|
| 实时解码 | Runtime `tg_3s`，约 3 秒更新，所选实例活跃任务合计。超过 12 秒未更新、订阅中断或服务离线时隐藏。 |
| Prefill | Runtime 实际计算的 Token 数、耗时和速度；通常结束时才出现，缓存复用时不同于完整输入 Token 数。 |
| Decode 耗时与平均速度 | Runtime `eval time`；运行时总耗时是 Prefill 与 Decode 的实测耗时之和。 |
| 阶段时间线 | 日志事件的观测时间。第一条速度日志可能晚于实际开始生成；不能代替精确的首 Token 时刻。 |
| TTFT / API 总耗时 | 完成统计来源；不与 Runtime 任务强行关联。排队与加载耗时无法可靠拆分时显示不可用。 |
| 上下文容量 | 任务开始附近可读取到的模型加载配置快照；旧记录缺少快照时不与新容量强行比较。 |
| 结束时上下文 | Runtime slot `n_tokens`，不代表生成中的精确占用。 |
| 生成中上下文估算 | 仅同时具备实际处理输入和已生成 Token 时显示；缓存复用可能低估总量。没有数据时不做字符数估算。 |
| KV Cache | 当前仅确认 GPU Offload 配置。实际分配量、有效占用、命中率尚不可用，不以显存差值代替。 |
| GPU | `nvidia-smi` 约每秒采集，整卡包含其他进程。进程显存不等于 KV 占用。 |
| CPU / RAM | Linux `/proc`；CPU 首次采样不可用，随后显示差分。 |

Runtime 适配已在本机 llama.cpp 2.41.0 验证。LM Studio 升级后应复核日志格式。订阅“已启动”与“已接收事件”分别标记。中途接入或断线期间的状态无法补回；未知终态记录为中断，不冒充成功完成。

## 配置

```bash
python3 server.py --host 127.0.0.1 --port 8090 \
  --lm-url http://127.0.0.1:1234 \
  --lms /path/to/lms \
  --db ./data/monitor.sqlite3
```

环境变量：`LM_URL`、`LMS_BIN`、`LM_API_TOKEN`。API 启用认证时，通过 `LM_API_TOKEN` 提供令牌，不写进源码。CLI 日志来自本机，所以 API 地址限制为本机。

另一台电脑访问可使用 SSH 转发：

```bash
ssh -L 8090:127.0.0.1:8090 用户名@主机地址
```

随后在该电脑打开 http://127.0.0.1:8090。也支持显式 `--host 0.0.0.0`，但程序没有登录认证，应只用于可信环境。

## 数据与升级

默认数据库为 `data/monitor.sqlite3`，目录自动创建。每 5 秒存储采样，保留 24 小时；任务和完成统计保留 7 天，各展示最近 200 条。过期数据在后续采样时清理。生成、结束和取消等状态变化会更新同一条任务，不重复计数。

V2 自动为 V1 数据库添加任务键和索引；旧请求保留在“完成统计”。采集器异常退出后再次启动，未结束任务标为中断。升级前可自行备份数据库；若需回退，建议使用备份副本。

清空历史会删除数据库中的历史记录，无法通过页面恢复；需要保留时请先导出。清空不会卸载模型或取消请求。

JSON 导出含模型与进程信息、任务统计及已加载历史，不含对话内容。脱敏诊断仅含版本、连接状态、采集源和错误代码。仓库不包含数据库、模型、凭据或本机测试输出。

## 验证与开发

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py telemetry.py observe_long_context.py
```

可选浏览器验收（开发依赖，非运行依赖）：

```bash
python3 -m pip install playwright==1.64.0
python3 -m playwright install chromium
# 先启动监控服务
python3 tests/browser_smoke.py
```

CI 对 Python 3.10/3.12/3.13 执行回归，并在无 GPU、LM Studio 离线环境执行 Chromium 验收。本机的真实推理测试需单独手动运行：

- `tests/integration_local.py`：需预先加载 `v2-validation`、配置至少 2 个并发 slot，发送一条 512 Token 请求和一条主动取消的请求。
- `tests/lifecycle_local.py`：**会关闭并重新启动本机 LM Studio daemon**，仅用于独占测试环境，验证推理中断、模型切换和日志恢复。
- `observe_long_context.py`：手动三轮长上下文负载，模型标识目前为 `qwen/qwen3-1.7b`，每轮最多输出 1536 Token。

`server.py` 负责采集、存储与 HTTP；`telemetry.py` 负责版本相关的 Runtime 任务解析；`static/` 是页面；`tests/` 包含脱敏样本和测试。具体验证结果见 [VALIDATION.md](VALIDATION.md)。
