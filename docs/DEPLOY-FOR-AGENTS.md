# Deploy runbook — for AI agents

> 目标：在一台 Windows 10/11 机器上，把 Watch Sentinel 跑起来、配好 Telegram、验证"人形触发 → 连拍 → 相册"整条链路，并注册开机自启。
> 约定：所有命令在仓库根目录执行；路径用 PowerShell 语法；每一步都有可验证的输出，**不要跳过验证**。

---

## 0. 环境自检（先做，30 秒）

```powershell
python --version                 # 需要 >= 3.10
Get-PnpDevice -Class Camera | Where-Object Status -eq 'OK' | Select FriendlyName
Get-PnpDevice -Class Bluetooth | Where-Object Status -eq 'OK' | Select -First 1 FriendlyName
```

- 没有摄像头 → 停，告诉用户"这台机器没有可用摄像头"。
- 没有蓝牙适配器 → 仍可跑，但把 `bt.enabled` 设为 `false`（门禁只剩"电脑在用"）。
- `python` 不在 PATH → 用 `py -3` 替代。

---

## 1. 取代码 + 装依赖

```powershell
git clone https://github.com/fengye1003/watch-sentinel.git
cd watch-sentinel
python -m pip install -r requirements.txt
```

验证：`python -c "import cv2, numpy, starlette, uvicorn; print(cv2.__version__)"` 不报错。

> 装包慢/被墙时，先设代理：`$env:HTTPS_PROXY="http://127.0.0.1:7897"`。

## 2. 下载人脸模型（不入库，必须单独取）

```powershell
python scripts\fetch_model.py
```

验证：`Get-Item models\face_detection_yunet_2023mar.onnx` 存在且 **> 200 KB**（正常 232,589 B）。

> 失败就手动从 <https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet> 下载同名文件放进 `models\`。

## 3. 配 Telegram（可选）

1. 让用户提供 bot token，写入：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.watch-sentinel" | Out-Null
Set-Content "$env:USERPROFILE\.watch-sentinel\token.txt" "<TOKEN>" -NoNewline
```

2. 让用户先给 bot 发一条任意消息，然后取 chat id：

```powershell
curl.exe -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

从 `result[].message.chat.id` 里取数字（私聊就是用户 id）。

3. 写进配置（服务未启动时可直接改 `data/config.json`；启动后用 API）：

```powershell
curl.exe -X PATCH http://127.0.0.1:8099/api/config -H "Content-Type: application/json" `
  -d '{\"notify\":{\"chat_id\":\"<CHAT_ID>\"}}'
```

## 4. 启动

```powershell
.\start.ps1
```

验证：

```powershell
curl.exe -s http://127.0.0.1:8099/api/health     # {"ok":true,...}
```

失败先看 `data\server.err.log`。

## 5. 配置目标手机

```powershell
curl.exe -s http://127.0.0.1:8099/api/state | python -c "import sys,json; d=json.load(sys.stdin); print([x['name'] for x in d['bt']['devices']])"
```

把目标设备名写进去（支持 `Pixel·hello` / `Pixel.hello` 这类标点差异）：

```powershell
curl.exe -X PATCH http://127.0.0.1:8099/api/config -H "Content-Type: application/json" `
  -d '{\"bt\":{\"target_name\":\"<设备名>\"}}'
```

验证 `GET /api/state`：`bt.found=true`，`bt.connected` 与手机实际状态一致。

> ⚠️ 不要用 `Get-PnpDevice` 的 Status 判断连接 —— 那个字段对"已配对未连接"也报 OK。

## 6. 验收（三步，必须全过）

```powershell
# a) 演练：只测识别，不发 TG（让用户站在镜头前动 10 秒）
curl.exe -X POST http://127.0.0.1:8099/api/drill -H "Content-Type: application/json" -d '{\"seconds\":10}'
```

期望：`ok=true`，`would_trigger.human` 或 `change` 为 true，`sample` 里能看到 `faces>=1`。

```powershell
# b) 模拟告警：走完整链路（连拍 → 选最清晰 → 发相册）
curl.exe -X POST http://127.0.0.1:8099/api/telegram/simulate -H "Content-Type: application/json" -d '{\"reason\":\"human\"}'
```

期望：返回 `ok=true`；几秒后 `GET /api/state` 的 `tg.last_ok=true`；事件列表出现
`检测到人形活动 … 连拍 6 张取最清晰 3 张`；用户在 TG 里收到**一条 3 张图的相册**。

```powershell
# c) 门禁：确认当前不会误报
curl.exe -s http://127.0.0.1:8099/api/state | python -c "import sys,json; print(json.load(sys.stdin)['gate'])"
```

期望 `active=false`（手机连着或屏幕亮/未锁屏时）。

## 7. 可选：TOTP 登录保护

```powershell
python -c "import secrets,base64; print(base64.b32encode(secrets.token_bytes(20)).decode())"
```

把 secret 写 `%USERPROFILE%\.watch-sentinel\totp.json`：`{"secret":"..."}`，然后：

```powershell
curl.exe -X PATCH http://127.0.0.1:8099/api/config -H "Content-Type: application/json" `
  -d '{\"auth\":{\"enabled\":true,\"secret_file\":\"~/.watch-sentinel/totp.json\"}}'
```

**必须重启**后生效。之后所有 `/api/*` 需先 `POST /api/auth/login {"code":"123456"}` 拿 cookie。

> 注意：`auth.enabled` 打开后**无法从面板关闭**（会锁死），要关只能改 `data/config.json` + 重启。

## 8. 开机自启

```powershell
.\install-autostart.ps1
```

验证：

```powershell
Get-ScheduledTask -TaskName SentinelWatch,SentinelWatchdog | Select TaskName,State
```

两个都应为 `Ready`。任务是 `wscript.exe watchdog-hidden.vbs` → 隐藏窗口拉起 `start.ps1`。

## 9. 收尾自检清单

- [ ] `/api/health` 200
- [ ] `bt.found=true`，`bt.connected` 符合实况
- [ ] `presence.brightness` 有值（读不到会打印 error，见坑 4）
- [ ] `drill` 能识别到人脸
- [ ] `simulate` 后 TG 收到一条多图相册
- [ ] 自启两个任务 `Ready`
- [ ] `data/` 没有进 git（`git status` 干净）

---

## 会伪装成"程序坏了"的 6 个坑

| # | 现象 | 真因 / 处理 |
|---|---|---|
| 1 | ONNX 报 `Can't read ONNX file` | 路径含中文。代码已把模型复制到 `%LOCALAPPDATA%\sentinel\models\` 再加载；若仍失败，确认模型 >200 KB |
| 2 | 蓝牙"已配对"却总显示未连接 | `Get-PnpDevice` Status 不可靠；以 `bt.connected`（Win32 `fConnected`）为准 |
| 3 | 摄像头打开但读不到帧 | 某些机器 MSMF 有问题；默认 `dshow`，仍不行把 `camera.backend` 改成 `msmf` 试 |
| 4 | 亮度 `null` + `error` | HDR/外接屏的 dxva2 不支持；本实现走 WMI。WMI 也读不到时按"离家"处理（会报警） |
| 5 | 相册发不出去，报 400 | `sendMediaGroup` 需要 2–10 张；`keep=1` 时应自动降级 `sendPhoto`，确认没被改坏 |
| 6 | 每 5 分钟闪一个黑窗 | 自启不要用 `powershell -WindowStyle Hidden`，必须走 `wscript + VBS`（仓库已带） |

## 回滚

```powershell
.\uninstall-autostart.ps1
.\stop.ps1
Remove-Item -Recurse -Force data, models\*.onnx
```

（保留代码目录即可；`data/` 是运行时产物。）
