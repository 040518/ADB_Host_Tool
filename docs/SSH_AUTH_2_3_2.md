# v2.3.2 RK3568 SSH 身份验证修复

日期：2026-11-08。

- 用户日志已到身份验证阶段，目标 SSH 服务可达。RK3568 有效配置允许 root、密码与公钥登录，禁止空密码；只核对 root 密码是否设置，未读取或导出密码哈希。
- Windows OpenSSH 使用电脑已有密钥可以正常登录 root；旧应用固定 `allow_agent=False, look_for_keys=False`，忽略已有密钥，仅尝试用户填写的密码，因密码认证未通过而失败。
- 改为默认尝试 SSH agent 和用户已有标准密钥；指定 keyfile 时只尝试指定密钥与提供的密码，避免尝试其他无关密钥。AppContext 同时传递配置中的 keyfile。
- 用修复后的 RemoteSession 和当前本机保存配置真实登录成功，`id` 返回 root；SFTP `/tmp` stat 通过。没有重设密码、安装新公钥、改变 sshd 配置或重启板卡。
- `tests/test_ssh_profiles.py` 11 项通过，新增默认密钥发现与指定密钥两个回归。
- 本机证据：`RK_SSH_AUTH_2.3.2.log`；测试与构建日志不纳入 Git。

交付版本：桌面 `ADB_Host_Tool_v2.3.2.exe`，使用现有目标配置连接即可。

- 最终 EXE 54,714,246 字节；SHA256 `35C7BCD16784889C5F48A38A3AC9F5C6127D34FA150128FE2586DB09F2992D0E`。
- 启动、显示 v2.3.2 主窗口、稳定运行 8 秒、正常关闭及无残留进程均通过。源码与 EXE 已同步正式工程，旧版备份于工作区 `delivery-backups/adbhost-auth-20261108/`。
