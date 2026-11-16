# 学习通预设手势签到

## 云服务器部署与 iPhone 使用

给 Muse 的部署入口见 [部署交接说明](docs/MUSE_DEPLOY.md)。仓库包含 Dockerfile、Docker Compose、HTTPS 代理配置和手机网页。

默认手势为 `1235789`、`14789`。服务器持续运行，iPhone Safari 可登录、开始或停止监测、修改候选和查看运行记录。仓库上传的是源代码，实际访问地址由云服务器部署生成。

## 目标与范围

在 Windows 轮询本人课程中的手势签到，依次校验用户指定的 1235789、14789，匹配后提交。密码通过隐藏输入读取，仅在当前进程内存保存。当前仅处理课程手势签到，电脑需持续联网运行。

## 验收标准

候选按顺序尝试，成功即停止。全部返回的进行中手势活动均可处理。明确拒绝的候选和成功活动持久化，重启不会重复尝试。网络异常保留原候选，未知响应暂停活动，不能误报成功。多进程互斥，状态原子保存。

## 技术方案

Python 3.10+、requests、pycryptodome。协议字段参考公开项目，自行实现适配。预签到后校验候选，只有服务端明确通过校验才提交签到。配置每轮重载，状态保存到 work/state.json，日志保存到 work/events.log。

## 使用

双击「启动.cmd」。首次运行准备虚拟环境和依赖，随后输入学习通账号、密码。密码输入时不显示字符。Ctrl+C 停止。重启需要重新输入账号密码。

config.json 已预设两条轨迹，顺序为 1235789、14789。九宫格编号从上到下为 123 / 456 / 789。可编辑 gestures 修改候选，poll_seconds 修改轮询间隔，attempt_seconds 修改候选间隔。course_ids 为空时处理所有返回课程，也可填指定课程 ID。

```powershell
.\.venv\Scripts\python.exe src\main.py --check-config
.\.venv\Scripts\python.exe src\main.py --dry-run --once
.\.venv\Scripts\python.exe src\main.py --once
```

--dry-run 登录后只列出活动。--once 完成一轮后退出。默认轮询 30 秒，候选间隔 3 秒。所有候选不匹配时，添加新候选，下轮会处理新增项。未知响应或额外验证会暂停该活动，记录日志，需人工检查。处理完后可停止程序、删除对应活动的状态记录，再重新启动。

## 验证与限制

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

交付检查使用离线模拟。真实账号登录和真实课堂签到尚未验证。协议变动、验证码、频率限制或手势不在列表中都可能导致失败。其他签到类型和群聊签到暂未实现。日志不保存密码、Cookie、完整服务器响应。状态损坏时停止运行并保留文件。

协议参考 https://github.com/HEDBS/chaoxing-sign/blob/6be3193c66e9f05b8c4a7d316ad80629c2ee2b37/chaoxing_sign.py

2026-11-16 本机验证结果

- Windows Python 3.14 虚拟环境和依赖已准备。
- 18 项离线测试通过，覆盖候选顺序、成功即停、重启去重、账号隔离、超时重试、频率限制、额外校验、只读入口及 Windows 多实例互斥。
- 通过实际 cmd.exe 调用「启动.cmd --check-config」，启动入口和中文路径验证通过。
- 初次使用 MSYS2 创建的环境留在 work/msys2-venv；实际启动使用 .venv/Scripts/python.exe。
- 尚未使用真实账号登录或提交真实签到。后续需在本人课程的实际活动中验证登录协议及签到响应。

## iPhone 网页版

新增手机网页与服务器持续监测后端，支持登录、开始、停止、预设手势编辑和运行记录。部署方式见 [服务器部署说明](docs/服务器部署.md)。服务器需要持续运行，手机锁屏不影响已启动的监测。

28 项离线测试通过。当前尚未获得目标服务器的具体连接信息，公网部署与真实 iPhone 验证待完成。
