# 平台迁移与接口对应

## 旧工程入口

原 Web 编排中心的服务入口是 `teleop_platform/server.py`，依赖由同目录的 `requirements.txt` 声明。它使用 Flask、Flask-SocketIO 和 SQLite。Web 控制台由服务端渲染。Savvy2 独立树的 `teleop_platform/` 只保留 `action_models.py`、`action_executor.py` 两个公共动作模块，完整的旧控制台入口在原混合工程的独立平台分支。

Savvy2 本体控制入口位于 `savvy2_teleop/`，平台对接由 `savvy2_teleop/agent/savvy2_agent.py` 提供。Pico Unity 项目位于 `pico_teleop/savvy2_unity/`，其中 `PicoPlatformAgent.cs` 负责旧平台注册、轮询、回报，`CompactTrackingJsonWriter.cs` 负责 TrackingData 数据载荷。旧平台承担低频编排与状态。Pico 的局域网位姿以 UDP 直达机器人，公网由部署的 Agora 桥把数据送入机器人本机接收器。旧 Recorder 位于 `data_collection/savvy2_recorder.py`，已提交的录制可以作为离线资源接入。

本仓库提供新的工作台、控制面 API、运行时和兼容适配代码。旧网页、SQLite 数据、账号、分支历史、厂商 SDK、原部署地址和凭据继续由原工程管理。

## 接口对应

| 功能 | 旧系统入口 | 公共 SDK 入口与状态 |
| --- | --- | --- |
| 控制端与本体注册 | `POST /api/register` | 新运行时由 `register_controller`、`register_robot` 注册，`GET /v1/status` 展示能力和分配状态。旧注册在独立回环兼容服务中只用于观察。 |
| Agent 轮询与执行回报 | `/api/poll`、`POST /api/report` | `LegacyAgentRegistry` 保存注册与状态。兼容 `/api/poll` 总是返回空命令队列。新 API 通过同进程 `RobotAdapter` 确认结果。 |
| 设备、工位与连接 | `/api/devices`、`/api/ws/claim`、`/api/ws/assign`、`/api/ws/unassign`、`/api/ws/release`、`/api/connections`、`/api/connect`、`/api/disconnect` | `GET /v1/status`、`POST /v1/sessions`、会话的 `close` 操作。工位、控制端和机器人在活动会话中互斥。 |
| 授权、保持与恢复 | 旧连接命令与机器人本地状态机 | `arm`、`hold`、`resume`、`close` 均需显式请求，由适配器确认。旧 Savvy2 观察适配器只读，真机保持与释放仍依赖已验收的本体控制链。 |
| 高频输入 | Unity TrackingData，局域网 UDP 或 Agora 数据通道 | `POST /v1/sessions/{id}/input` 及 Python、Pico Unity 客户端使用 `rpa.input.v1`。HTTP 工作台用于功能验证和单帧调试。实际遥操节拍和时限需要单独测量。 |
| 健康、网络与告警 | `/api/health`、`/api/netstat`、`/api/alert`、`/api/jointtemp`、`/api/uplinktest` | `GET /v1/robots/{id}/health` 返回适配器就绪、能力和状态。旧遥测专用端点继续留在原服务。 |
| 录制参数及生命周期 | 平台 `/api/record_config`，本机 Recorder `/rec/status`、`/rec/start`、`/rec/mark`、`/rec/stop` | 新 Episode 用 `record/start`、`record/mark`、`record/stop`。旧 Recorder 通过回环客户端独立接入，默认只读。旧历史 Episode 先校验后离线导入。 |
| 动作播放与状态 | 旧平台动作模块、Agent `/api/action/report` | `POST /v1/sessions/{id}/action` 支持预检，仿真保持态支持执行。实体机器人动作仍需经原控制栈验收。 |
| 视频、语音、网络配置 | 旧相机预览、RTC、语音预设及 `/api/robot_wifi_*` | 新运行时传递观测数据与适配器能力。实时多路视频中继、双工语音、Wi-Fi 切换和网络探测暂未迁移。 |
| 账号、配额、审计、持续运行 | `/api/login`、`/api/logout`、`/api/me`、`/api/stats`、SQLite | v1 使用进程内设备注册和会话，一枚共享 Bearer Token 用于接入门禁，账号、角色和重启后的状态恢复需要独立实现。 |

会话操作的 HTTP 路径以 `/v1/sessions/{id}/` 为共同前缀。录制的 `task`、`label` 与 `outcome` 是新字段，迁移旧编排客户端时需要逐项映射。原 Agent 和 Pico 看到旧注册成功，只表示观察记录已建立。改旧 URL 无法完成控制协议迁移。

## 渐进切换顺序

1. 用模拟机器人执行工位分配、授权、发送中立首帧、输入超时保持、手动恢复和录制提交。检查会话、质量清单和原始事件，记录所用版本与配置。
2. 用原控制端的一段脱敏日志回放 TrackingData。校验姿态、序号、采样时刻、追踪有效性和坐标系。旧数据仅表明载荷语义，实际追踪状态由受信输入源确认。
3. 使用同机回环兼容服务验证旧 Agent 注册和状态上报。使用已提交的旧 Episode 验证导入、校验、对齐与清洗。旧 Recorder 写入仅用于本机独占会话，以免和现有采集流程抢占同一个 Episode。
4. 经设备方验收后，再为具体 ROS2/厂商 Runtime 实现运动适配。适配器提供反馈新鲜度、控制模式、坐标与限位检查，并实际确认保持和释放。平台失联后的保护由机器人本地执行。实机迁移保留原急停、原服务配置和回退入口。

v1 无逐操作员身份校验。持有同一令牌的客户端属于同一信任域，可以读取状态，操作 API 有权访问的全部会话。默认回环部署适用于单机测试。跨机器测试还需要可信网络、加密传输和明确的设备访问策略。平台的逻辑 `hold` 表示适配器回执，实体机器人急停和断电保护属于本地设备。
