# Pico 与 Savvy2 接入

本仓库提供两条接入路径。新 Pico 应用发送公共输入帧，可连通平台和模拟机器人。原 Pico 与 Savvy2 通过兼容层做协议转换、注册状态与录制器联调。Savvy2 原有运动闭环继续由已部署的本体控制栈负责。

## Pico Unity 到公共平台

将 [PicoInputPublisher.cs](../adapters/pico/PicoInputPublisher.cs) 加入 Unity 2022.3 或兼容的工程，挂到场景对象。PICO XR/OpenXR 插件须在 Unity XR InputDevices 提供头显和双手姿态，并报告 `isTracked`。没有有效追踪时，发送端清空按键、Grip、摇杆与 Trigger，帧内 `tracking=false`。

从新平台取得会话 ID 后，把地址与令牌传入组件。下例的 `publisher` 由当前场景持有，`sessionId` 从工作台或客户端创建会话的结果读取。

```csharp
publisher.Configure(serverOrigin, sessionId, token);
publisher.StartSending();
```

组件使用 `POST /v1/sessions/{id}/input`。每次只允许一条 HTTP 请求在途，失败后停止发送。默认 20 Hz，采用最新采样。切换设备需要创建新会话。发送端只发送输入，武装、Hold、释放通过独立平台操作完成。应用禁用时中止请求，机器人的失联处理由本地安全出口提供。

HTTP 仅供同设备 loopback 联调，跨机器地址使用 HTTPS 反向代理后的 origin，并显式配置 Bearer token。令牌从运行环境传入。Unity 层无法根据应用接口回执推断物理停止，此样例适用于模拟调试和协议验证。原 Pico 高频 UDP、Agora 数据通道和视频 SDK 仍依赖匹配的原工程及授权插件。

## TrackingData 转换

`rpa_sdk.legacy.legacy_pico_to_frame` 接受 Pico 的 JSON 对象或 UTF-8 bytes。支持紧凑数组版本和使用逗号分隔 pose 的版本。必须提供头显和双手完整姿态。转换验证序号、有限数、取值区间和四元数，并保留采样时刻。

| 原字段 | 公共字段 | 说明 |
| --- | --- | --- |
| `sampleSequence` | `sequence` | 会话内单调递增。旧包可显式使用源 `timeStampNs` 作序号 |
| `timeStampNs` | `capture_time_ns` | 保留发送端时间域，接收端另记到达时间 |
| `Head.pose`、`Controller.left/right.pose` | `poses.head/left/right` | 米和 `[qx,qy,qz,qw]`，保持原坐标系 |
| 每侧 `grip`、`trigger` | `grip`、`trigger` | Grip 在适配层阈值化，默认 0.5。Trigger 保留原始归一化量 |
| `axisX`、`axisY` | `sticks` | 左 X/Y、右 X/Y |
| `axisClick`、`primaryButton`、`secondaryButton`、`menuButton` | `buttons` | 键名有 `left_` 或 `right_` 前缀，值为 bool |
| 外部确认的追踪和 `appState.focus` | `tracking` | 原紧凑包缺少可靠的每手追踪与前台状态，默认 false |

原字段 `ArmHint`、`Body`、`Hand` 和 `sensorFrameIndex` 留在原始数据中，公共帧当前采用头显与双手位姿。诊断若需使用这些字段，保存原包作为单独数据流。

```python
from rpa_sdk.legacy import LegacyPicoReceiver, legacy_pico_to_frame

frame = legacy_pico_to_frame(packet)
with LegacyPicoReceiver(port=0, peer_port=known_fixture_port) as receiver:
    print(receiver.address)
    newest = receiver.read_latest(max_datagrams=8)
```

`LegacyPicoReceiver` 绑定本机，适用于回放与模拟桥。每轮读取有上限，校验来源端口，拒绝重复或倒退的序号与源时间。重建发送会话时重新创建接收器。带安全授权的接入端可显式提供独立验证的 `tracking_valid`，仅凭原包的 Head.status 不足以授权。反向 `frame_to_legacy_pico` 只生成经过校验的消息对象，发送与坐标映射由对接端负责。

## 原 Savvy2 Agent 与 Recorder

`LegacyAgentRegistry` 和 `make_legacy_server` 支持旧客户端的以下握手。

| 接口 | 含义 |
| --- | --- |
| `POST /api/register` | 登记 Pico 或机器人 ID、能力 |
| `GET/POST /api/poll?device_id=...` | 查看是否已知，刷新心跳，返回空命令列表 |
| `POST /api/report` | 保存旧连接和进程状态 |
| `registry.snapshot()` | 获取上述状态的副本 |

兼容服务只在 loopback 上监听，因为旧接口没有可用于跨网鉴权的凭据。Savvy2 Agent 可以在同设备以不带 `--script` 的方式对兼容服务注册。旧 Pico APK 可由同设备代理验证请求格式。已有 Agent 的 `running` 和 `stopped` 是进程生命周期状态。运行状态可能在未配置启动脚本时回报，物理运动许可仍需控制器反馈、标定、所有权及本体审核。

```python
from rpa_sdk.legacy import LegacyAgentRegistry, Savvy2RobotAdapter, make_legacy_server

registry = LegacyAgentRegistry()
adapter = Savvy2RobotAdapter("savvy2-example", registry=registry)
server = make_legacy_server(registry, port=0)
print(server.server_address)
try:
    server.serve_forever()
finally:
    server.server_close()
```

`Savvy2RobotAdapter` 可注册到平台读取状态。没有经过验证的机器人本地出口时，readiness 会拒绝运动，Hold/Release 回报未确认。现有 `cmd_start_teleop`、`cmd_stop_teleop`、`cmd_play_action`、`cmd_jog`、`cmd_stop_action` 继续走原 Agent 会话和本地控制锁。本平台的公共动作接口可在模拟机验证。

`Savvy2RecorderClient("http://127.0.0.1:7005")` 查询旧 Recorder。需要接管旧 Recorder 的独占写端时显式设 `allow_write=True`，由唯一客户端调用 `start(task_name, conn_id=...)`、`mark(mark, note)` 和 `stop(mark, note)`。可用 `transport` 及 `with_chassis`、`with_gripper`、`with_waist`、`with_head` 写入任务部件上下文。写操作检查当前 Episode 归属。旧 HTTP 接口没有原子 compare-and-stop，部署时须保证 Agent 和自定义客户端的控制互斥。写失败或超时后先查 `status()`，再人工对账。原 Recorder 使用自己的 Episode 格式，公共数采与清洗代码的支持格式见仓库 README。

## 既有能力映射

下表记录 Savvy2 典型部署中的默认接口，现场以授权配置为准。

| 领域 | 既有入口 | 公共 SDK 接入项 |
| --- | --- | --- |
| Pico 输入 | LAN UDP 8888，Agora Bridge loopback UDP 6002 | TrackingData 编解码、UDP 回放适配、Unity 公共帧发布 |
| 双臂与夹爪 | 本地命令/反馈 IPC 9401/9402，ROS Domain 42 | 本体接口、能力登记、应用侧保持语义。厂商驱动留在原部署 |
| 底盘与腰头 | 本地 IPC 9403/9404，ROS Domain 79/101 | 公共姿态、摇杆与部件能力。模式映射由本地驱动负责 |
| 动作 | 原 Agent 动作进程、本地 9411/9412 | 动作接口与会话互斥，先在模拟机验证 |
| 健康和故障 | 原 Agent 监控、控制器与设备状态 | 旧注册/心跳/进程状态观察。新增适配器单独映射硬件健康 |
| 视频 | 本地 MJPEG 8091，历史兼容 8090，原公网 Agora | 独立预览链路与相机角色。对接原部署的获授权流地址 |
| 录制 | 本地 HTTP 7005，遥测 6010 | 旧 Recorder 客户端、独立公共 Episode 管线 |
| 标定和限位 | 原本体部署参数、各关节限位、J6/J7 耦合和腰部工作空间 | 通过能力与本地预检读取或检查。标定保留在本体侧 |

## 真机出口的对接条件

实际本体适配器需在机器人本地检查设备与会话、版本、关节模型与坐标、追踪新鲜度、Grip 重锚、有效关节限制，以及各速度输出的独立超时。SDK 的 Hold 和 Release 等待本地出口停止确认并依确认结果保留或释放控制权。实际接口还需报告传感器时钟、故障、模式和当前目标，运动过程中要能从真机反馈判断是否失效。验证路径从日志回放、影子输出和单部件静止测试起步，再做受控现场验收。
