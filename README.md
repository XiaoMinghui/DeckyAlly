# DeckyAlly

ROG Ally X（2024，RC72LA）在 SteamOS 上使用的 Decky 插件。首个功能是**系统唤醒后自动尝试恢复内置手柄**，整个过程由 Python 后台执行，不需要按键、不需要打开 Decky 面板，也没有手动恢复按钮。

**当前版本：0.1.0 实验版。已完成本地自动化测试和前端构建，尚无 Ally X 真机验证。重启 InputPlumber 是否能解决你的故障仍未确认。**

## 默认行为

1. 插件加载后监听系统休眠/唤醒，不会因为安装、开机或打开面板就重启手柄服务。
2. 每次唤醒等待 5 秒，再采集物理设备、InputPlumber 服务及合成输入设备信息。
3. 默认在通过环境和服务检查后，重启一次 `inputplumber.service`。即使设备显示已连接，也会执行，以覆盖“已连接但所有按键无响应”的故障。
4. 重启后每隔 2 秒复查，最多 3 次。显示“输入链路结构正常”，不会把它当成按键或游戏已经恢复的证明。
5. 每轮至多请求一次重启，45 秒内不重复请求。冷却记录跨插件重载保留；重复的唤醒信号合并处理。

等待时间是开始检查前的延时，并非保证在 5 秒内恢复。探测、日志采集和服务重启也需要时间。InputPlumber 管理的外接手柄可能同时短暂断连；部分游戏可能无法正确处理重连。

## 支持范围

- Linux 下 `/etc/os-release` 的 `ID=steamos`。
- ASUS ROG Ally X，DMI 主板名 `RC72LA`，或产品名中对应的 RC72LA 标识；不包括初代 Ally 和 ROG Xbox Ally X。
- 已安装并运行 Decky Loader，插件以 `flags: ["root"]` 启动。
- 系统已有 InputPlumber 和 `systemctl`。不替换、不安装输入驱动或输入管理服务。
- 使用 `busctl` 读取 InputPlumber 设备结构。较旧或不兼容的 D-Bus 接口会显示“不确定”；默认模式仍可在服务检查通过后尝试重启。
- 优先使用 `dbus-monitor` 监听 logind，Linux BOOTTIME/MONOTONIC 时钟差作为兜底。没有 `dbus-monitor` 时只使用时钟兜底；小于约 0.75 秒的极短休眠可能无法被兜底检测。

服务不存在、被 masked、无法查询状态、检测到活跃的 `hhd*.service`，或设备/系统不匹配时不执行恢复。InputPlumber 为 active/failed 时可处理；若为 inactive，仅在本次后台运行曾记录到正常运行基线时允许恢复。过渡状态不介入。HHD 检查针对系统级 systemd 服务，不保证识别所有自定义启动方式。

## 安装

安装包：`artifacts/DeckyAlly-0.1.0.zip`，旁边提供 SHA-256 文件。包内已经包含前端构建产物和 Python 模块，**设备端无需 npm、pip 或编译**。

设备在身边后，将 ZIP 传到 Ally X 的下载目录。在桌面模式终端中执行以下命令（假定 Decky 安装在当前用户的 `~/homebrew`，ZIP 文件名保持不变）：

```bash
sudo mkdir -p "$HOME/homebrew/plugins"
sudo unzip -o "$HOME/Downloads/DeckyAlly-0.1.0.zip" -d "$HOME/homebrew/plugins"
sudo systemctl restart plugin_loader.service
```

ZIP 中已有 `DeckyAlly/` 顶层目录，解压后应存在 `~/homebrew/plugins/DeckyAlly/plugin.json`，不要再多套一层同名目录。以上命令会覆盖同名插件文件并重启 Decky；如果 Decky 使用了自定义位置，改为实际插件目录。也可以重启设备让 Decky 重新加载插件。

安装后默认启用后台自动恢复，无需先进入插件面板操作。未修改设置时第一次重启请求发生在下一次符合条件的系统唤醒后。

## 设置

Decky 面板仅用于提前配置和事后查看：

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| 自动恢复 | 开启 | 关闭后不再自动请求重启 |
| 每次唤醒都尝试恢复 | 开启 | 关闭后，要求连续 3 次结构异常才恢复；可能漏掉“设备结构正常但按键无响应” |
| 唤醒后等待时间 | 5 秒 | 可调 3–15 秒 |

配置修改会取消当前尚未完成的处理，下一次唤醒采用新设置。已经提交给 systemd 的重启请求可能继续完成。

## 日志与后续验证

插件使用 Decky 提供的目录，通常为：

- 配置：`~/homebrew/settings/DeckyAlly/settings.json`
- 诊断：`~/homebrew/logs/DeckyAlly/recovery.jsonl`
- 冷却记录：`~/homebrew/data/DeckyAlly/recovery.json`

实际诊断路径在面板中显示。诊断文件每份上限约 512 KiB，保留 3 份轮转备份，记录系统信息、最近正常基线、唤醒原因、处理前后设备状态、命令结果和有限的 InputPlumber 日志。不监听或记录具体按键内容，不上传数据。

以后设备可用时，优先验证：

1. 正常唤醒后是否仅发生一次恢复尝试，所有按键能否使用。
2. 故障出现时是否能自动恢复；不能恢复时，事后保存当前 `recovery.jsonl*`。
3. 游戏运行中、插电/电池、外接手柄以及短时间连续休眠唤醒的表现。
4. 关闭自动恢复、卸载插件、重载 Decky 后是否符合预期。

暂不自动执行 USB 重置、驱动解绑、MCU 省电设置变更、Steam 重启或整机重启。物理设备缺失时只尝试同一服务级恢复一次，失败就记录并停止。

从 Decky 卸载插件即可停止后续处理；不安装系统服务、system-sleep 钩子或修改系统只读分区。Decky 可能保留配置/诊断目录，便于排查与重装。若配置损坏，自动恢复默认关闭；若冷却记录损坏，会阻止恢复并在日志中记录原因。

## 开发

需要 Node.js 20+、npm 和 Python 3.11+。Python 后端只依赖标准库与 Decky 提供的 `decky` 模块。

```bash
npm ci --ignore-scripts
npm run typecheck
npm run build
python -m unittest discover -s tests -v
python scripts/package_plugin.py
python scripts/verify_package.py
```

`src/index.tsx` 是面板；`main.py` 只接入 Decky 生命周期；`py_modules/decky_ally/system.py` 封装只读探测与固定的服务重启操作；`recovery.py` 负责唤醒监听、策略与日志。后续功能在 `py_modules/decky_ally/` 增加独立模块，不需要扩大手柄恢复模块的职责。

Windows 可完成构建与模拟测试，无法验证 Linux 的真实 D-Bus、休眠恢复、驱动或游戏重连。自动化测试用模拟的设备/服务状态检查恢复决策，不会在开发电脑上重启 InputPlumber。

## 参考

- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) 与 [官方插件模板](https://github.com/SteamDeckHomebrew/decky-plugin-template)：插件 API、构建方式和生命周期。
- [AllyCenter](https://github.com/PixelAddictUnlocked/allycenter)：硬件插件组织方式；本项目没有直接复制其后端功能。
- [InputPlumber Ally X 配置](https://github.com/ShadowBlip/InputPlumber/blob/main/rootfs/usr/share/inputplumber/devices/50-rog_ally_x.yaml) 与 [CompositeDevice 接口](https://github.com/ShadowBlip/InputPlumber/blob/main/src/dbus/interface/composite_device.rs)：设备标识、来源/输出查询。
- [systemd logind 接口](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml)：`PrepareForSleep` 事件。

上游主分支与实际 SteamOS 所带版本可能不同，真机兼容性以日志和实测为准。
