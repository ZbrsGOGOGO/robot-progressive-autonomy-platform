# 数据采集、清洗和训练导出

## 采集接口

接收端按调用场景创建独立 Episode。本机 Recorder 写原始事件，再离线清洗。高频执行器只派发事件给有界旁路队列。图像写入、哈希和提交在非实时线程运行。队列丢弃和设备错误要作为采集元数据或诊断事件保留。

~~~python
from rpa_sdk.dataset import EpisodeStore

store = EpisodeStore("./recordings")
started = store.start("session-example", "pick_demo", {
    "robot_type": "sim",
    "required_streams": ["intended", "commanded", "applied", "measured"],
    "clock_verified": False,
})
episode_id = started["episode_id"]
store.append(episode_id, "intended", {"sequence": 1, "position_m": [0.1, 0.0, 0.2]})
store.append(episode_id, "commanded", {"q_rad": [0.1]})
store.append(episode_id, "applied", {"q_rad": [0.09]})
store.append(episode_id, "measured", {"q_rad": [0.088]})
store.mark(episode_id, "approach", {"phase": 1})
result = store.stop(episode_id, outcome="success")
~~~

时间戳默认用采集机的单调时钟。传入 `t_mono_ns` 时，应先完成源时钟到同一单调时基的映射，原始源时间、接收时间、偏差、不确定性写入事件数据。每路时间不倒退，同一纳秒的多事件由索引保持到达次序。方法接受 JSON 对象，拒绝 NaN、无穷值和不合法的序号时间类型。

| 流 | 语义 | 常见字段 |
| --- | --- | --- |
| `intended` | 操作者、高层任务或策略的原始意图 | 位姿、来源、输入序号、追踪状态 |
| `commanded` | 模式映射、IK 与安全约束之后的控制目标 | `q_rad`、`position_m`、拒绝原因 |
| `applied` | 本体适配器确认已下发的目标 | `q_rad`、下发序号、驱动模式 |
| `measured` | 本体真实反馈 | 关节角、速度、关节 effort、`fault` |
| `images.<role>` | 各相机原始图像事件及独立媒体 | 角色、相对路径、采集/接收时间 |
| `marks` | 人工或任务阶段标记 | 标签和时间、可选 JSON 详情 |

例如 `applied.arm_a` 和 `applied.arm_b` 分别记录左右臂。任务若要求两路都到齐，应显式列出两路。把 `applied` 列为必需时，任一该类子流有效即可计入覆盖。未获得实际反馈时省略 `measured` 事件，清洗后的空值反映真实缺失。

有真实图像 bytes 时调用 `store.append_image(episode_id, "head", jpeg_bytes, t_mono_ns=stamp, metadata={...})`。支持 JPEG 和 PNG，验证文件签名和大小，图像解码完整性由相机适配或下游检查实现。无图像的仿真可单独写视觉状态事件。记录图片采集时间和来源不会自动获得硬件同步，只有现场证据支持时才设置 `clock_verified=True`。

## 目录与提交

~~~text
recordings/
  ep_<id>.inprogress/     正在录制或待排查的临时目录
  ep_<id>/                原子提交后出现的原始 Episode
    capture.json          起始会话、任务和元数据
    events.jsonl          原生节拍、每流索引、时间和载荷
    images/               可选原始图像
    manifest.json         结果标记和各流计数
    checksums.sha256      文件完整性校验
~~~

`stop` 刷新文件并同步，生成校验值后在同一文件系统内改名。已提交 Episode 重复 `stop` 返回原清单。中断时保留临时目录用于诊断，`abandon` 关闭写入而不提交。离线校验拒绝临时目录、坏哈希、目录外引用、符号链接、缺失内容与乱序。哈希校验保证字节一致性。

`status` 区分录制中、已提交、写入失败和重启后留存的未完成目录。业务的成功、失败、主动中止作为 `outcome` 保存。独立质量报告评估数据有效性。Recorder 侧错误可由 `stop(..., capture_error="recorder_queue_full")` 记录，设备错误可由实测流的 `fault` 标记。两类采集错误都会判为 C，完整记录的失败示范仍可单独质检。

## CLI 和质量

~~~bash
python -m rpa_sdk.dataset verify recordings/ep_<id>
python -m rpa_sdk.dataset clean recordings/ep_<id> aligned/pick-001 --hz 30 --max-age-ms 100 --require-stream applied --require-stream measured
python -m rpa_sdk.dataset export training/pick-v1 recordings/ep_<id1> recordings/ep_<id2> --allow-b
python -m rpa_sdk.dataset import-savvy2 /path/to/finalized/savvy2-episode recordings
~~~

路径中的 `<id>` 请替换成真实值。清洗不写源目录，导出到已存在的目录会拒绝。清洗同时生成 `frames.jsonl`、`manifest.json`、`quality.json`、被实际引用的媒体和校验清单。同一次 Episode 超过 1,000,000 帧时拒绝生成派生文件，可按原任务缩短采集窗口或使用更低导出帧率。

默认 30 Hz 对齐。每个切点取各路事件中时间不晚于切点的最后一条。缺失或超龄时写 `data: null`，保留来源索引、时间、年龄、有效性和新鲜样本标志。先校验原生事件，再派生对齐记录。清洗不生成未来填充，也不装作实测值齐全。质量等级规则是：

- A：必需流至少 90%，时钟已验证，未见记录到的控制器错误或来源降级。
- B：有必需流低于 90% 且不少于 50%，或缺少可信时钟、原始 Savvy2 质量最高为 B。
- C：无对齐帧、必需流低于 50%、检测到控制器故障，或采集注明不可用。

损坏、校验失败和时间倒退会直接拒绝校验。默认数据集只接受 A，明确加 `--allow-b` 才接纳 B，C 永远不发布。整条 Episode 按 seed 和 Episode ID 的 SHA-256 桶切分，目标比例约 80/10/10，小样本可能缺一组。生产评测需要依机器人、场景、工件和采集者分组时，使用业务侧确定的独立切分。

## 兼容 Savvy2 V1

适配器读取本地已完成 Savvy2 Episode 的 `events.jsonl`、`frames.jsonl`、原始元数据、质量清单和校验值。语义流映射覆盖 Pico 意图、双臂 IK/指令/实际下发/反馈、夹爪、底盘、腰头、任务标记和各角色原始 JPEG。关节角常见 deg 字段还转换为 `q_rad`，原载荷仍保留。相机路径按首次出现去重，时序优先使用机器人编码捕获时间，再回落到接收或原帧切点，并保存来源。源时钟不确定，不升级历史数据的质量等级。原始 Episode 保持原样，后加的人工 `annotations.json` 不作为原始校验数据导入。

统一导出产物是 JSONL、媒体和完整 Episode 划分。接 LeRobot 时，观测取真实双臂状态 q_rad，动作取实际下发 q_rad，左右臂按机型 profile 固定关节顺序，夹爪、底盘和腰头分别用任务定义的字段。SDK 不执行 RGB 解码、视频编解码或具体 LeRobot 版本的 Parquet 提交。训练平台可以在校验清单的约束下完成转换。

真实采集输出可能包含视频、控制轨迹、环境和设备元数据，应存放到 Git 之外，按团队权限共享。
