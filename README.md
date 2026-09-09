# DeckyAlly

DeckyAlly 是面向 SteamOS 和 ROG Ally X（2024，RC72LA）的 Decky Loader 插件。

## 功能

### 摇杆灯光

- 自定义颜色和预设颜色
- 调节亮度与动画速度
- 常亮、呼吸、光谱、波浪、闪烁和电量指示效果
- 休眠唤醒后自动恢复灯光设置

### 手柄唤醒恢复

- 在系统解锁后自动检查内置手柄状态
- 仅在输入链路持续异常时尝试重启 InputPlumber
- 后台自动运行，不需要打开 Decky 面板或使用手动按钮
- 保存诊断日志，便于排查未能自动恢复的问题

## 安装

从 [Releases](https://github.com/XiaoMinghui/DeckyAlly/releases) 下载最新 ZIP，解压到 Decky Loader 的插件目录，然后重启 Decky Loader。

当前版本仍处于实验阶段，灯光效果和手柄恢复需要在 ROG Ally X 真机上继续验证。

灯光功能参考了 [AllyCenter](https://github.com/PixelAddictUnlocked/allycenter) 的硬件接口。
