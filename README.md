# 机器人渐进式自主平台

面向多控制端和多机器人类型的公共 SDK。Web 工位、Python 脚本和可选 XR 客户端使用同一输入接口。运行时管理工位分配、运动授权、保持与释放，并将操作者意图、控制目标、本体反馈和任务标记关联到同一会话。原始数据提交后可独立校验、清洗和导出。

## 十分钟跑通

需要 Python 3.9 或更新版本。模拟和数采用 Python 标准库，无须 ROS2、Unity 或真机。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
rpa serve --port 0 --episodes ./episodes --input-timeout-ms 5000
```

打开启动时打印的工作台地址。选择控制端和双臂或移动底盘模拟器，创建会话。授权后，在输入超时前发送一个追踪有效的中立输入帧。示例将模拟超时放宽到 5 秒，便于手动点选。默认 350 毫秒用于测试失联，连续输入可使用 Python 客户端或 XR 示例。模拟器停止并确认后释放工位。

无需启动服务的一键演示会创建一个模拟 Episode，并打印其 ID：

```bash
rpa demo --episodes ./episodes
EPISODE_DIR="./episodes/填入打印的episode_id"
python -m rpa_sdk.dataset verify "$EPISODE_DIR"
python -m rpa_sdk.dataset clean "$EPISODE_DIR" ./datasets/aligned-demo
```

也可直接使用源码运行：

```bash
PYTHONPATH=sdk python3 -m rpa_sdk demo --episodes ./episodes
PYTHONPATH=sdk python3 -m unittest discover -s tests -v
```

## 功能与接入范围

| 能力 | 公共 SDK | 可选接入 |
| --- | --- | --- |
| 多控制端 | Web 工作台、Python SDK、统一输入帧 | Pico Unity XR 示例、TrackingData 编解码和本机限量回放 |
| 多机器人 | `RobotAdapter`、能力发现、双臂和移动底盘模拟器 | `LegacyRobotAdapter` 读取兼容设备的注册、上报和录像状态 |
| 工位与会话 | 工位、控制端和机器人互斥，显式授权、保持、恢复和释放 | 可在 loopback 上观察旧设备的注册、心跳和状态报告 |
| 运动与安全 | 检查输入序号和追踪状态，支持失联保持、中立恢复和停止确认 | 真机集成由本地控制器负责标定、运动限制、看门狗和停止确认 |
| 任务与动作 | 关节关键帧预览，模拟器可应用离线终态 | 外部轨迹可按统一关键帧格式进行预检 |
| 视频与传感器 | 状态与能力接口，支持按相机角色记录 PNG/JPEG | 实时媒体、相机驱动和硬件同步由设备集成提供 |
| 数据采集 | 有界后台队列记录意图、控制目标、实际下发、实测反馈、图像和任务标记 | `LegacyRecorderClient` 接入本机 Recorder，也可离线导入已提交的兼容 v1 Episode |
| 清洗与训练导出 | SHA-256 校验、因果对齐、新鲜度掩码、A/B/C 质检和整条 Episode 切分 | 可用独立适配器将输出 JSONL 和图像转换到 LeRobot 等目标格式 |

## 渐进式自主

1. **人工遥操**：同一工位持有唯一控制权，控制端发意图，机器人侧做执行约束并回报真实结果。
2. **人工示范与数据回查**：任务配置、动作阶段、预检和录制状态进入 Episode。清洗和导出在原始数据之外写派生产物。
3. **动作验证**：关键帧先经格式和适配器边界检查，默认预览。模拟会话可应用终态，方便核对数据合同。
4. **策略接入**：外部策略可把候选轨迹写成同一动作格式，经预览和受控验证。当前版本未实现策略训练服务、在线自主执行、控制权自动切换或真机动作审批。

## API 与接入说明

- [统一帧、平台 REST、机器人适配和执行边界](docs/api.md)
- [可选 Pico Unity XR、旧设备协议和 Recorder 接入](docs/integrations.md)
- [Episode、校验、清洗和导出](docs/data.md)
- [平台迁移与兼容映射](docs/platform-migration.md)

两个模拟机器人分别声明 14 个关节和移动底盘能力。业务代码通过 `runtime.register_controller(...)` 与 `runtime.register_robot(...)` 注册新的控制方式和本体。设备侧 `apply` 返回命令与观测层的数据，`hold` 和 `release` 返回本机反馈确认。

## 部署与验证范围

内置 HTTP 服务是开发工作台，默认仅监听 loopback。跨机接入至少使用 TLS、身份和工位权限，并将本体看门狗与停止链留在控制器侧。参考服务的共享 Bearer token 只适合受控联调环境。进程重启后的持久化工位恢复、生产角色权限、标定及物理急停需要在部署层落实。

默认安装可独立运行模拟、录制和离线处理。现场配置、厂商驱动和真实采集数据由使用方管理。离线和 loopback 测试覆盖公共协议和数据链路，Unity 构建、实机动作和实时媒体需要在目标设备上单独验收。
