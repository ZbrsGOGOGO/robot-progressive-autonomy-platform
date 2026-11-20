# 公共 SDK 接口

## 状态与所有权

`assigned → armed → live → hold → armed`。关停从已确认 Hold 进入 `closing`，确认 Release 后才成为 `released`。适配器无法确认保持或释放时进入 `fault` 并继续保留机器人、控制端和工位归属。恢复需要显式请求，并发送一个中立帧。追踪丢失和输入超时触发 Hold。链路中断没有自动回位动作。

参考进程每约 25 ms 调用一次 `Runtime.tick()`。SDK 的 350 ms 默认输入超时用于开发参考环境。本机收到合规、递增、追踪有效的帧才刷新。关闭服务前通过 `Runtime.shutdown()` 获取未确认的退出列表。自定义硬件集成必须在本体命令出口有独立超时，机器人确认语义不依赖工作台 HTTP。

默认运行时拒绝为非模拟机器人开启运动会话。经过独立验收的本地扩展可以配置 `Runtime(..., allow_physical_motion=True)` 并由适配器声明 `device_local_safety=true`。每次授权或恢复生成新的 `input_epoch`。物理输入必须回传当次 epoch，Hold 后旧授权失效。epoch 用于识别迟到指令，设备身份与数据授权仍由部署层负责。

## HTTP v1

以 `/v1` 为前缀，状态使用 GET，操作使用 POST 和 JSON。返回 JSON 对象。普通调用失败会返回 `{"error":"…"}`。400 为不合规请求，401 为令牌未通过，404 为未知对象，409 为状态或所有权冲突，503 表示服务不可用。

| 方法和路径 | 输入 | 输出 |
| --- | --- | --- |
| `GET /v1/status` | 无 | 控制端、机器人、工位会话与录制状态 |
| `GET /v1/robots/{robot_id}/health` | 无 | 能力、readiness、本体状态 |
| `POST /v1/sessions` | `controller_id, robot_id, workstation_id` | 归属与 session_id |
| `POST /v1/sessions/{id}/arm` | 空对象 | 等待中立首帧 |
| `POST /v1/sessions/{id}/input` | 公共输入帧 | 会话和本体回执 |
| `POST /v1/sessions/{id}/hold` | 空对象 | 确认后的保持状态 |
| `POST /v1/sessions/{id}/resume` | 空对象 | 重新预检，等待中立帧 |
| `POST /v1/sessions/{id}/close` | 空对象 | 保持、录制收尾、释放结果 |
| `POST /v1/sessions/{id}/record/start` | `task` | Recorder 创建的 Episode ID |
| `POST /v1/sessions/{id}/record/mark` | `label, details?` | 标记及本机时间 |
| `POST /v1/sessions/{id}/record/stop` | `outcome?` | 已提交清单。允许 complete/aborted/failed |
| `GET /v1/episodes/{episode_id}` | 无 | 录制中、临时或已提交状态和清单。会话保留 last_episode_id 以便对账 |
| `POST /v1/sessions/{id}/action` | `mode?, frames` | 预览或模拟结果。每个关键帧用 `t_s, joint_positions_rad` |

平台路由处理有界 JSON。控制端将超时视为状态未知，先读 `/v1/status` 再判断下一步；v1 不提供跨进程持久化命令队列和 commandId 幂等日志。收到重复输入序号会拒绝，重复释放返回已释放状态。不要把 `POST` 重试当成物理停止回执。

```json
{
  "schema": "rpa.input.v1",
  "sequence": 1,
  "capture_time_ns": 38461000000,
  "tracking": true,
  "poses": {
    "left": {"position": [0.1, 0.0, 0.2], "rotation": [0, 0, 0, 1]}
  },
  "grip": [false, false],
  "trigger": [0.0, 0.0],
  "sticks": [0.0, 0.0, 0.0, 0.0],
  "buttons": {}
}
```

姿态位置为米，四元数使用 xyzw。坐标按适配器明确声明；通用层不会猜测 Unity、ROS 或厂商坐标映射。Grip 和 Trigger 的顺序为左、右，摇杆用左 X/Y、右 X/Y。可选源时戳用发送端单调时间，接入后须递增。跨设备延迟和数据对齐需单独建立时钟映射。

## Python 工作端

```python
from rpa_sdk.client import PlatformClient

client = PlatformClient()
session = client.open_session("python-demo", "arm-demo", "station-01")["session_id"]
try:
    client.arm(session)
    client.input(session, {"sequence": 0, "tracking": True})
    client.hold(session)
finally:
    client.close(session)
```

真实应用应在受控线程更新输入，按会话保持序号。原子资源分配、控制和录制进度来自服务端回执，UI 可缓存展示但应标注失联和陈旧。处理端收到未知结果时可先检查 `robot_status` 与 `snapshot`。

## 接入一个本体

实现 `rpa_sdk.contracts.RobotAdapter`，在运行时执行 `runtime.register_robot(adapter)`。标识 `robot_id` 唯一，`robot_type` 表示机型。

| 方法 | 实现要点 |
| --- | --- |
| `capabilities()` | 返回 motion、simulation、parts、joint_count 和相机/SDK 信息 |
| `readiness()` | 结合驱动模式、反馈新鲜度、控制权、标定版本和故障，返回 ready |
| `apply(frame)` | 本体所有者应用授权帧。回报 commanded、applied、measured 等字典 |
| `hold()` | 确认停止新增目标与本体保持。速度型部件应归零 |
| `release()` | 本机确认无运动占用，再返回 confirmed=true |
| `snapshot()` | 返回状态、反馈与健康。只读 |
| `validate_action(frames)` | 可选适配器校验。至少检查关节维度、限位和能力 |

在进程外联调前验证坐标、单位、关节顺序、工作空间、腕部耦合、IK 失败、急停和本体看门狗。输入可能已在设备侧排队，物理 SDK 必须自行检查租约、源样本年龄和时间窗口，不能由 HTTP 成功推断硬件动作生效。
