# 设备与录制器接入

Pico Unity XR 示例将头显和手柄作为可选控制端，使用与 Web 工作台、Python 客户端相同的输入接口。兼容层提供 TrackingData 回放、本机设备状态观察和 Recorder 接入。实机运动由经验证的设备端适配器和控制器负责。

## Pico Unity 到公共平台

将 [PicoInputPublisher.cs](../adapters/pico/PicoInputPublisher.cs) 加入 Unity 2022.3 或兼容的工程，挂到场景对象。PICO XR/OpenXR 插件须在 Unity XR InputDevices 提供头显和双手姿态，并报告 `isTracked`。没有有效追踪时，发送端清空按键、Grip、摇杆与 Trigger，帧内 `tracking=false`。

取得会话 ID 后，把工作台地址和令牌传入组件。下例的 `publisher` 由当前场景持有，`sessionId` 来自创建会话的回执。

```csharp
publisher.Configure(serverOrigin, sessionId, token);
publisher.StartSending();
```

组件向 `POST /v1/sessions/{id}/input` 发送最新采样，默认 20 Hz，同一时刻最多一个请求在途。请求失败或组件禁用后停止发送。授权、保持与释放由会话操作独立完成，实机控制器负责处理失联。

同机联调可用 loopback HTTP。跨机使用配置好的 HTTPS origin 和 Bearer token，由部署环境传入令牌。该示例用于模拟调试、输入采样和协议验证，实机停稳须以本体回执确认。

## TrackingData 转换

`rpa_sdk.legacy.legacy_pico_to_frame` 接受 TrackingData JSON 对象或 UTF-8 bytes。姿态支持紧凑数组和逗号分隔字符串，头显及双手姿态必须完整。转换验证序号、数值范围和四元数，并保留源采样时间。

| 原字段 | 公共字段 | 说明 |
| --- | --- | --- |
| `sampleSequence` | `sequence` | 会话内单调递增。旧包可显式使用源 `timeStampNs` 作序号 |
| `timeStampNs` | `capture_time_ns` | 保留发送端时间域，接收端另记到达时间 |
| `Head.pose`、`Controller.left/right.pose` | `poses.head/left/right` | 米和 `[qx,qy,qz,qw]`，保持原坐标系 |
| 每侧 `grip`、`trigger` | `grip`、`trigger` | Grip 在适配层阈值化，默认 0.5。Trigger 保留原始归一化量 |
| `axisX`、`axisY` | `sticks` | 左 X/Y、右 X/Y |
| `axisClick`、`primaryButton`、`secondaryButton`、`menuButton` | `buttons` | 键名有 `left_` 或 `right_` 前缀，值为 bool |
| 外部确认的追踪和 `appState.focus` | `tracking` | 由可信接入方显式传入 `tracking_valid`，默认 `false`。`appState.focus` 为 `false` 时追踪无效 |

原字段 `ArmHint`、`Body`、`Hand` 和 `sensorFrameIndex` 留在原始数据中，公共帧当前采用头显与双手位姿。诊断若需使用这些字段，保存原包作为单独数据流。

```python
from rpa_sdk.legacy import LegacyPicoReceiver

with LegacyPicoReceiver(port=0) as receiver:
    print(receiver.address)
    # 在本机向上述地址发送回放数据后，读取最近的合规帧。
    newest = receiver.read_latest(max_datagrams=8)
```

`LegacyPicoReceiver` 绑定 loopback。每轮读取有上限，拒绝重复或倒退的序号与源时间。可以使用 `peer_port` 限定本机发送端口。发送端重启时重新创建接收器。`Head.status` 本身不足以确认实际追踪。反向 `frame_to_legacy_pico` 为有效追踪帧生成经过校验的消息对象，发送和坐标转换由调用方完成。

## 本机设备状态观察

`LegacyAgentRegistry` 和 `make_legacy_server` 支持以下旧设备协议。

| 接口 | 含义 |
| --- | --- |
| `POST /api/register` | 登记 `device_id`、`device_type`（`robot` 或 `pico`）和 `capabilities` |
| `GET/POST /api/poll?device_id=...` | 查看是否已知，刷新心跳，返回空命令列表 |
| `POST /api/report` | 保存已登记设备的 `state` 和 `conn_id` |
| `registry.snapshot()` | 获取上述状态的副本 |

兼容服务仅监听 IPv4 loopback。注册数据和进程状态用于观察，运动授权须通过本体能力、状态和会话预检。

```python
from rpa_sdk.legacy import LegacyAgentRegistry, LegacyRobotAdapter, make_legacy_server

registry = LegacyAgentRegistry()
adapter = LegacyRobotAdapter("example-robot", registry=registry)
server = make_legacy_server(registry, port=0)
print(server.server_address)
try:
    server.serve_forever()
finally:
    server.server_close()
```

`LegacyRobotAdapter` 的 `robot_type` 为 `robot`。可以注册到公共运行时读取状态，或绑定 Recorder 客户端后用 `recorder_status()` 查询录制。该适配器仅供观察，运动 readiness 为 `false`，保持与释放均返回未确认。

## 本机 Recorder

```python
from rpa_sdk.legacy import LegacyRecorderClient

# recorder_origin 是配置中的本机 HTTP origin，端口由实际服务提供。
client = LegacyRecorderClient(recorder_origin, recorder_id="lab-recorder", allow_write=False)
print(client.status())
```

Recorder 地址使用含端口的 HTTP loopback IP。构造客户端时显式提供目标实例的 `recorder_id`。`status()` 读取 `/rec/status`，核对响应的 `recorder` 与配置一致，并检查 `recording` 为布尔值。

独占控制同一 Recorder 的客户端可显式设置 `allow_write=True`，调用 `start(task_name, conn_id=...)`、`mark(mark, note)` 和 `stop(mark, note)`。录制参数可声明夹爪、底盘、腰部和头部等任务部件。标记和结束前会复查当前 Episode 归属。兼容协议的状态查询和停止请求分开，需在部署中避免其他写端同时操作。请求失败或超时后先查 `status()` 核对本次录制状态。已提交的数据按[兼容 v1 离线导入](data.md#离线导入兼容-v1-episode)处理。

## 真机出口的对接条件

本体适配器在机器人本地校验设备、会话、版本、关节模型、坐标、关节限位和输入新鲜度，并为速度输出设置独立超时。使用 Grip 接管时需要验证重锚行为。运行时依本地停止回执保持或释放控制权。设备端同时报告传感器时钟、故障、模式及当前目标。实际接入从日志回放、影子输出和单部件静止测试开始，再做受控现场验收。
