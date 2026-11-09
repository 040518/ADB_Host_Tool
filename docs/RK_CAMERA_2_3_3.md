# v2.3.3 RK3568 直接 SSH 摄像头兼容修复

日期：2026-11-08。

## 原因与修改

- 用户连接的是 RK3568 SSH 目标。只读检查确认 `/dev/video0` 为 rkisp_mainpath、`/dev/video1` 为 rkisp_selfpath，摄像头已识别，无其他采集或推流程序占用。RKAIQ 服务保持运行。
- RKISP 节点使用 V4L2 Video Capture Multiplanar / NV12；原直接 SSH 路径只按普通 USB 单平面设备交给 FFmpeg，缺少该接口的采集处理。
- RK3568 BusyBox setsid 仅支持 `-c`，不支持 Ubuntu GNU setsid 的 `-w`，使原长连接启动失败。
- SSH 采集命令根据节点实际格式自动选择：多平面节点使用 v4l2-ctl NV12 → rawvideo → FFmpeg MJPEG，普通 USB 仍使用原 FFmpeg V4L2 路径。
- 启动前检测 setsid 的 wait 能力，GNU 使用 -w，BusyBox 使用兼容参数。接收线程等待 stdout EOF，不再把 setsid 父进程提前退出误判为采集结束。

## 实机验收

| SSH 目标 | 分辨率 | 连续帧短测 | 界面实测 |
|---|---|---|---|
| RK3568 `/dev/video0` | 640×480 | 141 帧 | 24.8 FPS |
| RK3568 `/dev/video0` | 1280×720 | 140 帧 | 25.0 FPS |
| 香橙派 USB `/dev/video0` | 640×480 | 151 帧 | 30.0 FPS |

- 所有测试期间禁止调用 ADB；通过真实软件 ScreenshotPage、独立 SSH 二进制通道、JPEG 解码和 PNG 保存验证。
- 停止后自有 PID 文件清理完成，测试进程均退出码 0；未停止其他应用或更改驱动、固件、SSH 密码和系统启动项。
- 摄像头节点初始空闲，因此测试只按所选分辨率调整其采集输出；RKAIQ 继续运行。未做长时间稳定性验收，也未重新宣称 60 FPS。
- `tests/test_endpoints.py` 17 项通过，增加多平面路由与 BusyBox 父进程先退出、stdout 后到达的回归。
- 本地日志：`RK_CAMERA_PREFLIGHT.log`、`RK_CAMERA_2.3.3_TEST.log`、`RK_CAMERA_720P_2.3.3_TEST.log`、`ORANGE_CAMERA_2.3.3_TEST.log`。实际图片保留在被 Git 忽略的 `docs/screenshots/`。

交付：桌面 `ADB_Host_Tool_v2.3.3.exe`。连接 RK3568 后使用实时摄像头、SSH 目标、`/dev/video0`。

- 最终 EXE：54,713,499 字节；SHA256 `BAA70E4B3567C4C62904B6B0B6C8B2A949D59C676AFEB8D1AA9B104D42B0A5ED`。
- 主窗口启动、8 秒稳定运行、正常关闭和无残留均通过。正式工程源码与 EXE 已同步，原文件保留于 `delivery-backups/adbhost-camera-20261108/`。
