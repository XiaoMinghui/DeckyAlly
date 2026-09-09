# DeckyAlly

面向 SteamOS 上 ROG Ally X（2024，RC72LA）的 Decky 插件，目前提供摇杆灯光控制和休眠唤醒后的内置手柄恢复。

当前版本为 **0.2.0 实验版**。功能已完成本地模拟测试和前端构建；灯光效果以及手柄自动恢复仍需在 Ally X 真机继续验证。

## 摇杆灯光

灯光模块参考 [AllyCenter](https://github.com/PixelAddictUnlocked/allycenter) 的硬件接口，通过内核提供的 `/sys/class/leds/ally:rgb:joystick_rings` 控制摇杆灯环：

- 全色相选择，以及 ROG 红、青、紫、绿、橙、粉、白、蓝 8 个预设。
- 0–100% 亮度。
- 常亮、呼吸、光谱循环、彩虹波浪、闪烁、电量指示和关闭。
- 呼吸、光谱、波浪和闪烁效果支持 0–100% 动画速度。

第一次安装时，插件只读取当前硬件灯光状态，不覆盖系统设置。用户第一次修改灯光后，配置保存到 `~/homebrew/settings/DeckyAlly/lighting.json`，之后启动插件及系统唤醒后会恢复已保存的效果。动画完全由 Python 后台运行，关闭 Decky 面板不会停止。

“电量指示”会从系统中类型为 Battery 的电源设备读取容量，并用绿色（电量高）、黄色（约 50%）到红色（电量低）显示。关闭灯光只把 LED 亮度设为 0，不修改 `mcu_powersave`，以免影响现有手柄恢复问题的排查。

如果内核没有提供 `brightness`、`max_brightness`、`multi_intensity` 和 `multi_index`，面板会显示设备不可用并停止写入。当前实现兼容 `rgb`/`multi` 打包颜色通道和独立 red/green/blue 通道。

## 唤醒手柄恢复

手柄恢复由后台自动执行，不需要打开 Decky 或按手柄按钮：

1. 插件监听系统休眠/唤醒和 SteamOS 锁屏状态。
2. 锁屏期间不探测、不读取服务日志，也不重启服务。
3. 解锁后等待 8 秒，让 SteamOS 自行恢复。
4. 采集物理设备、InputPlumber 服务及合成设备信息。状态正常或无法确定时不介入。
5. 只有连续 3 次结构异常才重启一次 `inputplumber.service`，随后复查最多 3 次。
6. 每轮最多重启一次，并有 45 秒冷却期。

InputPlumber 没有提供“Steam 已收到实际按键”的健康状态，因此插件无法无侵入地确认按键是否真的恢复。如果设备结构完整但按键依然无响应，本版会保留诊断信息，不会打断看似正常的手柄。

## 支持范围

- `/etc/os-release` 中 `ID=steamos` 的 Linux 系统。
- ASUS ROG Ally X RC72LA；不包括初代 Ally 和 ROG Xbox Ally X。
- Decky Loader 使用 root 插件权限运行。
- 手柄恢复要求系统已有 InputPlumber、`systemctl` 和兼容的 D-Bus 接口。
- 灯光要求内核暴露 Ally 多色 LED class 设备。

若检测到活跃的 `hhd*.service`，手柄恢复不会重启 InputPlumber。灯光模块不安装或替换驱动，不写入 MCU 省电设置，不创建 systemd 服务或休眠钩子。

## 安装

构建后的安装包为 `artifacts/DeckyAlly-0.2.0.zip`，旁边带 SHA-256 校验文件。设备端无需 npm、pip 或编译。

将 ZIP 复制到 Ally X 的下载目录，在桌面模式执行：

```bash
sudo mkdir -p "$HOME/homebrew/plugins"
sudo unzip -o "$HOME/Downloads/DeckyAlly-0.2.0.zip" -d "$HOME/homebrew/plugins"
sudo systemctl restart plugin_loader.service
```

ZIP 已包含 `DeckyAlly/` 顶层目录，解压后应存在 `~/homebrew/plugins/DeckyAlly/plugin.json`。如果 Decky 使用自定义位置，请改为实际插件目录。

## 配置与日志

- 手柄恢复配置：`~/homebrew/settings/DeckyAlly/settings.json`
- 灯光配置：`~/homebrew/settings/DeckyAlly/lighting.json`
- 诊断日志：`~/homebrew/logs/DeckyAlly/recovery.jsonl`
- 恢复冷却记录：`~/homebrew/data/DeckyAlly/recovery.json`

诊断文件单份上限约 512 KiB，保留 3 份轮转备份。不记录具体按键内容，也不上传数据。

## 开发和验证

需要 Node.js 20+、npm 和 Python 3.11+：

```bash
npm ci --ignore-scripts
npm run typecheck
npm run build
python -m unittest discover -s tests -v
python scripts/package_plugin.py
python scripts/verify_package.py
```

代码结构：

- `src/index.tsx`：Decky 设置面板。
- `main.py`：Decky 生命周期与 RPC 接口。
- `py_modules/decky_ally/lighting.py`：灯光设备、持久化和后台效果。
- `py_modules/decky_ally/recovery.py`：唤醒监听和手柄恢复状态机。
- `py_modules/decky_ally/system.py`：系统及 InputPlumber 探测。

Windows 可以完成构建和模拟测试，不能验证真实 sysfs LED、D-Bus、休眠恢复或游戏重连。真机测试时建议依次检查所有静态颜色、不同亮度、每种动画、休眠唤醒后灯效恢复，以及灯效运行期间手柄输入是否正常。

## 参考

- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) 与 [Decky 插件模板](https://github.com/SteamDeckHomebrew/decky-plugin-template)
- [AllyCenter](https://github.com/PixelAddictUnlocked/allycenter)
- [InputPlumber 的 Ally X 配置](https://github.com/ShadowBlip/InputPlumber/blob/main/rootfs/usr/share/inputplumber/devices/50-rog_ally_x.yaml)
- [systemd logind 接口](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml)

上游主分支和实际 SteamOS 内置版本可能不同，兼容性以真机日志与测试结果为准。
