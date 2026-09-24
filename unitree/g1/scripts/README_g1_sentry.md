# G1 站岗哨兵 1.2.0（待真机验收）

## 实际功能与边界

Skill 使用 g1_sentry 卡片执行 start / stop / info；相机事件直接进入 SentryRules，不由语言模型逐帧判断。相机中心深度不是人体识别，也不能确认有人故意捂镜头。

默认只警告：0.15≤距离<0.8m 红灯闪两次并语音提示；0<距离<0.15m 提示疑似近物遮挡。至少3个独立样本覆盖0.2秒，冷却5秒，>1m满足同样条件后重新布防。当前同一事件不从近物升级成遮挡，两个场景应分开验证。0、非法值、重复时间和过期数据不构成有效触发。单次会话最多180秒；2秒无新帧则结束，不自动重连。

有现场负责人发放的短时单次许可时，才允许请求后撤或小角度转向。后撤参数 vx=-0.2m/s、duration=1s；转向 vyaw=0.3rad/s、duration=0.8s，是小角度转向，不是180度转身，不保证重新找到目标。每次调用前核对 standing、FSM801、姿态时间推进、最新有效距离；调用结束/取消后发送 stop_move。请求返回不等于物理位移成功，必须现场测量/观察。

## 文件

- install_g1_sentry.py：Skill定义和离线数据库安装器；新装默认禁用，备份并保留其他配置。
- sentry_rules.py：纯距离判定。
- sentry_service.py：独立 MCP 控制卡片，Python3.10+ / aiohttp；只监听127.0.0.1:18791。
- test_install_g1_sentry_local.py：20项安装器和判定测试。
- test_sentry_service.py：9项控制服务模拟测试；包括默认不动、过期许可、失败停止、并发采样、退出、断流。

## 部署约定

当前验证采用现有 agent-core 容器的虚拟环境运行独立脚本，不替换运动驱动。服务文件位于 /work/resource/g1-sentry/，使用环境变量 G1_TOKEN_FILE 指向权限600的本机token文件；凭证不得加入仓库。后端只允许回环地址，默认 https://127.0.0.1:15678，使用该机器已有自签证书端点。

启动命令（在机器人主机上）：

```sh
docker exec -d -e G1_TOKEN_FILE=/work/resource/g1-sentry/token phanthy-motus-agent-core-1 /work/.venv/bin/python /work/resource/g1-sentry/sentry_service.py
```

启动前检查该端口没有旧服务，不能重复启动。agent-core重启后需人工重新启动此独立服务；未配置开机自动运动。平台登记 MCP URL 为 http://127.0.0.1:18791/mcp。Skill的requiredTools仅为g1_sentry。

不要直接在运行中的数据库上使用离线安装器；线上通过平台技能API备份、停用、更新、读回。画布测试只将控制器连接到 g1_sentry；不要让旧指令并行操作 loco。技能开启不等于相机有新数据，必须查看样本数持续增加。camera_distance 返回 running 只是接口状态，不能证明相机采集正常。

## 运动许可

默认不存在 motion-permit.json，因此程序不请求任何移动。只由现场负责人确认机器人和场地、遥控器后，在服务同目录写入以下结构：

```json
{"expires_at": 0, "branch": "near", "supervised": true, "space_clear": true}
```

expires_at必须替换为当前Unix时间加不超过90秒；示例0无效。branch仅允许near或occlusion_suspected。许可在发出运动请求之前消耗；需要第二次运动必须再次现场确认并重新发放。此文件不暴露为模型工具。前置单点深度无法验证后方/转向空间；文件是现场测试许可，不是自动避障能力。不要在无人照看或公开拥挤场地开放运动。

## 验证及当前阻塞

本地运行：

```sh
python test_install_g1_sentry_local.py
python test_sentry_service.py
```

2026-09-24：20+9项本地测试通过；9项服务测试也在真机容器Python3.10.12通过（替身测试，不是物理运动测试）。发现并修复 asyncio.timeout 不兼容3.10的问题，改为asyncio.wait_for。

当天在线实际测试只收到一帧3.999m旧数据，随后2秒超时并回到idle；无警告、无运动。这证明断流退出生效，但未证明触发分支成功。相机info/start均返回running，ROS2仍发现g1_realsense发布端，但直接等待新距离消息未收到。须先恢复持续新帧，才可做靠近/遮挡和运动联调。不要把昨日的离线回放或单步运动记录当作新卡片全闭环验收。

后续：持续新帧→不移动近物警告→分开的疑似遮挡警告→退出/断流→现场单次后撤→现场单次小角度转向→画布Skill开启/退出→Master验收。每项保留日志和现场视频/截图，不能仅根据API200或ret=null宣称成功。

landing最终还需在技能平台创建/提交Skill，联系Master验收并提交有效截图。责任文档列G1 Master为黄昌龙；实际负责人以现场团队确认为准。未完成这些步骤，不得标记入职任务通过。
