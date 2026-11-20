# 机器人渐进式自主平台

面向多控制端和多机器人类型的公共 SDK。将 Pico / Unity、Web 工位和脚本的输入收敛到同一接口，将工位分配、运动授权、停止确认、状态上报、示范采集与数据清洗接到同一个会话。项目从 Savvy2 的 Pico、平台和采集分工抽取通用能力，适用范围按下表标注。

## 十分钟跑通

需要 Python 3.9 或更新版本。模拟和数采用 Python 标准库，无须 ROS2、Unity 或真机。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
rpa serve --episodes ./episodes --input-timeout-ms 5000
```

打开 [本地工位](http://127.0.0.1:8765/)。选择 Web/Pico/Python 控制端、双臂或移动底盘模拟器，创建会话。授权后，在输入超时前发送一个跟踪有效且按钮中立的样本。示例将模拟超时放宽到 5 秒，便于手动点选。默认 350 毫秒用于测试失联，连续输入请使用 Pico 示例或 Python 客户端。模拟器停止并确认后释放工位。

无需启动服务的一键演示会创建一个模拟 Episode，并打印其 ID：

```bash
rpa demo --episodes ./episodes
EPISODE_DIR=./episodes/ep_填入打印的ID
python -m rpa_sdk.dataset verify "$EPISODE_DIR"
python -m rpa_sdk.dataset clean "$EPISODE_DIR" ./datasets/aligned-demo
```

也可直接使用源码运行：

```bash
PYTHONPATH=sdk python3 -m rpa_sdk demo --episodes ./episodes
PYTHONPATH=sdk python3 -m unittest discover -s tests -v
```

## 功能与接入范围

| 能力 | 公共 SDK | Savvy2 既有系统接入 |
| --- | --- | --- |
| 多控制端 | Web 工作台、Python SDK、Pico Unity XR 输入示例、统一输入帧 | Legacy TrackingData 双向编解码及限量读取 |
| 多机器人 | `RobotAdapter`、能力发现、双臂与移动底盘两种模拟适配 | Savvy2 观察适配器，可读取旧 Agent 注册/报告和录像状态 |
| 工位与会话 | 工位、控制端和机器人互斥。显式授权、Hold、恢复和释放 | 旧平台注册、轮询、上报提供 loopback 兼容路径。原平台页面、数据库、角色与工位不迁移 |
| 运动与安全 | 递增序号、跟踪丢失和新鲜度门、中立恢复、停止确认。最终限制在本体适配器内 | 双臂、夹爪、腰头、底盘、原 IK、重锚、标定与厂商驱动继续在原机器人栈运行 |
| 任务与动作 | 关节关键帧预览。模拟器可应用离线终态，适合校验数据和会话 | 原 Action Studio 执行仍由原 Agent/本体会话接管。无公共真机执行器 |
| 视频与传感器 | 状态与能力接口，数采支持独立 PNG/JPEG 与相机角色 | 兼容说明给出健康、MJPEG、Agora 对接位置。采集硬件和公网媒体服务继续由原部署提供 |
| 数据采集 | 记录意图、处理后命令、实际下发、实测反馈、图像和任务标记。独立有界写队列 | Savvy2 Recorder HTTP 客户端和 Savvy2 V1 已提交 Episode 导入 |
| 清洗与训练导出 | SHA-256 校验、因果对齐、新鲜度掩码、A/B/C 质检、整条 Episode 切分 | 输出可追溯 JSONL 和图像。LeRobot、ROS bag 或其他目标格式使用独立导出适配 |

## 渐进式自主

1. **人工遥操**：同一工位持有唯一控制权，控制端发意图，机器人侧做执行约束并回报真实结果。
2. **人工示范与数据回查**：任务配置、动作阶段、预检和录制状态进入 Episode。清洗和导出在原始数据之外写派生产物。
3. **动作验证**：关键帧先经格式和适配器边界检查，默认预览。模拟会话可应用终态，方便核对数据合同。
4. **策略接入**：外部策略可把候选轨迹写成同一动作格式，经预览和受控验证。当前版本未实现策略训练服务、在线自主执行、控制权自动切换或真机动作审批。

## API 与接入说明

- [统一帧、平台 REST、机器人适配和执行边界](docs/api.md)
- [Pico Unity、Savvy2 旧协议、Agent、Recorder 与媒体](docs/savvy2-pico.md)
- [Episode、校验、清洗和导出](docs/data.md)
- [平台迁移与兼容映射](docs/platform-migration.md)

两个模拟机器人分别声明 14 个关节和移动底盘能力。业务代码通过 `runtime.register_controller(...)` 与 `runtime.register_robot(...)` 注册新的控制方式和本体。设备侧 `apply` 返回命令与观测层的数据，`hold` 和 `release` 返回本机反馈确认。

## 部署与验证范围

内置 HTTP 服务是开发工作台，默认仅监听 loopback。跨机接入至少使用 TLS、身份和工位权限，并将本体看门狗与停止链留在控制器侧。参考服务的共享 Bearer token 只适合受控联调环境。进程重启后的持久化工位恢复、生产角色权限、标定及物理急停需要在部署层落实。

仓库不包含原私有仓库历史、现场配置、SDK 二进制、APK、密钥或数据。厂商 SDK 通过原工程的授权环境提供。离线和 loopback 测试覆盖通用链路，Unity 构建、实机动作和媒体端到端需要目标设备验收。
