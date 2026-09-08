import { useEffect, useState } from "react";
import { callable, definePlugin } from "@decky/api";
import { PanelSection, PanelSectionRow, ToggleField, SliderField, staticClasses } from "@decky/ui";
import { FaGamepad } from "react-icons/fa";

type Settings = { enabled: boolean; mode: "always" | "conditional"; delay_seconds: number };
type Status = {
  phase: string;
  reason?: string;
  settings?: Settings;
  settings_error?: string;
  host?: { product: string; os_version: string; kernel: string };
  monitor?: string;
  last_check?: { time: number; health: string; reason: string } | null;
  last_result?: { time: number; outcome: string; reason: string; error?: string } | null;
  log_path?: string;
};

const getStatus = callable<[], Status>("get_status");
const updateSettings = callable<[settings: Partial<Settings>], Status>("update_settings");

const phases: Record<string, string> = {
  starting: "正在启动", idle: "已就绪，等待下次唤醒", disabled: "自动恢复已关闭",
  unsupported: "当前环境不支持自动恢复", sleeping: "系统正在休眠", waiting: "唤醒后等待设备恢复",
  recovering: "正在重启 InputPlumber", verifying: "正在复查输入链路",
  observed: "输入链路结构正常", attempted: "已尝试恢复，输入链路结构正常",
  unknown: "已完成检查，输入状态仍不确定", failed: "恢复未确认完成", skipped: "本次未执行恢复",
};
const reasons: Record<string, string> = {
  requires_steamos_ally_x_rc72la: "首版仅支持 SteamOS 下的 ROG Ally X（RC72LA）。",
  requires_root_and_systemctl: "需要 Decky 的 root 权限及 systemctl。",
  another_backend_running: "已有另一个 DeckyAlly 后台实例运行。",
  service_probe_failed: "无法读取 InputPlumber 服务状态。",
  service_not_active: "InputPlumber 服务未处于运行状态。",
  physical_device_missing: "未发现预期的内置手柄物理接口。",
  inputplumber_probe_failed: "无法读取 InputPlumber 的设备信息。",
  composite_device_missing: "未发现 Ally X 的合成输入设备。",
  ambiguous_composite_devices: "检测到多个 Ally X 合成设备，无法确定状态。",
  input_chain_incomplete: "输入来源或手柄模拟输出不完整。",
  physical_interfaces_lost: "物理接口比休眠前减少。",
  source_interfaces_lost: "输入来源比休眠前减少。",
  structure_ready_input_unverified: "设备结构已就绪；无法自动证明按键已恢复或游戏已重新接入。",
  service_missing_or_masked: "InputPlumber 服务不存在或被屏蔽。",
  service_not_managed_or_transitioning: "服务未运行或正在切换状态，本次不介入。",
  conflict_probe_failed: "无法检查其他手柄管理服务，本次不介入。",
  hhd_conflict: "检测到 HHD 正在运行，本次不介入。",
  runtime_state_invalid: "恢复记录损坏，已阻止自动重启；请检查后台日志。",
  cooldown: "处于 45 秒恢复冷却期。",
  restart_command_failed: "InputPlumber 重启命令失败，详情已记录。",
  recovery_error: "自动处理出现错误，详情已记录。",
};

const description = (reason?: string) => reason ? (reasons[reason] ?? reason) : "";
const when = (stamp: number) => new Date(stamp * 1000).toLocaleString();

function Content() {
  const [status, setStatus] = useState<Status>({ phase: "starting" });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await getStatus();
        if (active) setStatus(next);
      } catch {
        if (active) setError("无法连接插件后台，请检查 Decky 日志。");
      } finally {
        if (active) timer = setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  const save = async (patch: Partial<Settings>) => {
    setSaving(true);
    setError("");
    try { setStatus(await updateSettings(patch)); }
    catch { setError("设置保存失败，后台将沿用原设置。"); }
    finally { setSaving(false); }
  };

  const settings = status.settings;
  return <>
    <PanelSection title="唤醒手柄恢复">
      <PanelSectionRow>
        <div style={{ lineHeight: 1.6 }}>
          <strong>{phases[status.phase] ?? status.phase}</strong>
          <div style={{ fontSize: 12, opacity: 0.75 }}>{description(status.reason)}</div>
          <div style={{ fontSize: 12, marginTop: 8 }}>后台自动处理，无需打开此面板。</div>
        </div>
      </PanelSectionRow>
      {settings && <>
        <PanelSectionRow>
          <ToggleField label="自动恢复" checked={settings.enabled} disabled={saving}
            description="系统唤醒后自动检查并按所选策略尝试恢复。"
            onChange={(enabled) => { void save({ enabled }); }} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label="每次唤醒都尝试恢复" checked={settings.mode === "always"} disabled={saving}
            description="默认开启，以覆盖已连接但无响应的情况。会重启 InputPlumber，其管理的其他手柄也可能短暂断连。关闭后仅在连续检测到异常时恢复。"
            onChange={(enabled) => { void save({ mode: enabled ? "always" : "conditional" }); }} />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField label="唤醒后等待时间" value={settings.delay_seconds} min={3} max={15} step={1}
            disabled={saving} showValue valueSuffix=" 秒"
            onChange={(delay_seconds) => { void save({ delay_seconds: Math.round(delay_seconds) }); }} />
        </PanelSectionRow>
      </>}
      {(error || status.settings_error) && <PanelSectionRow>
        <div style={{ color: "#ffcc80", fontSize: 12 }}>{error || "配置文件读取失败，自动恢复已关闭。"}</div>
      </PanelSectionRow>}
    </PanelSection>
    <PanelSection title="最近处理">
      <PanelSectionRow>
        <div style={{ fontSize: 12, lineHeight: 1.7 }}>
          {status.last_result ? <>
            <div>{when(status.last_result.time)}</div>
            <div>{phases[status.last_result.outcome] ?? status.last_result.outcome}</div>
            <div>{description(status.last_result.reason)}</div>
          </> : <div>尚无唤醒处理记录。</div>}
          {status.last_check && <div style={{ marginTop: 8 }}>最近检测：{description(status.last_check.reason)}</div>}
          <div style={{ marginTop: 8 }}>监听：{status.monitor === "logind_and_clock" ? "系统信号 + 时钟兜底" : status.monitor === "clock_fallback" ? "时钟兜底" : "尚未运行"}</div>
        </div>
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="设备与诊断">
      <PanelSectionRow>
        <div style={{ fontSize: 12, lineHeight: 1.7, overflowWrap: "anywhere" }}>
          <div>{status.host?.product || "设备信息加载中"}</div>
          {status.host && <div>SteamOS {status.host.os_version} · {status.host.kernel}</div>}
          <div style={{ marginTop: 8 }}>诊断自动保存：{status.log_path || "加载中"}</div>
          <div style={{ marginTop: 8 }}>0.1.1 实验版 · 已修复真机发现的系统工具动态库冲突，恢复效果待复测。</div>
        </div>
      </PanelSectionRow>
    </PanelSection>
  </>;
}

export default definePlugin(() => ({
  name: "DeckyAlly",
  titleView: <div className={staticClasses.Title}>DeckyAlly</div>,
  content: <Content />,
  icon: <FaGamepad />,
}));
