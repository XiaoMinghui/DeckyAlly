import { useEffect, useRef, useState } from "react";
import { callable, definePlugin } from "@decky/api";
import {
  DialogButton,
  DropdownItem,
  Focusable,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { FaGamepad } from "react-icons/fa";

type Settings = { enabled: boolean; mode: "conditional"; delay_seconds: number };
type Status = {
  phase: string;
  reason?: string;
  settings?: Settings;
  settings_error?: string;
  host?: { product: string; os_version: string; kernel: string };
  monitor?: string;
  screen_locked?: boolean;
  last_check?: { time: number; health: string; reason: string } | null;
  last_result?: { time: number; outcome: string; reason: string; error?: string } | null;
  log_path?: string;
};

type LightingEffect = "static" | "pulse" | "spectrum" | "wave" | "flash" | "battery" | "off";
type LightingSettings = {
  enabled: boolean;
  color: string;
  brightness: number;
  effect: LightingEffect;
  speed: number;
};
type LightingState = LightingSettings & {
  available: boolean;
  configured: boolean;
  suspended: boolean;
  last_error: string;
  device: { path: string; zones: number; max_brightness: number; error: string };
};

const getStatus = callable<[], Status>("get_status");
const updateSettings = callable<[settings: Partial<Settings>], Status>("update_settings");
const setLockState = callable<[locked: boolean], Status>("set_lock_state");
const getLightingState = callable<[], LightingState>("get_lighting_state");
const updateLighting = callable<[settings: Partial<LightingSettings>], LightingState>("update_lighting");

const phases: Record<string, string> = {
  starting: "正在启动", idle: "已就绪，等待下次唤醒", disabled: "自动恢复已关闭", locked: "锁屏中，不执行检测",
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

const effectOptions = [
  { data: "static", label: "常亮" },
  { data: "pulse", label: "呼吸" },
  { data: "spectrum", label: "光谱循环" },
  { data: "wave", label: "彩虹波浪" },
  { data: "flash", label: "闪烁" },
  { data: "battery", label: "电量指示" },
  { data: "off", label: "关闭" },
];
const animatedEffects: LightingEffect[] = ["pulse", "spectrum", "wave", "flash"];
const presets = [
  { color: "#ff0000", label: "ROG 红" }, { color: "#00ffff", label: "青色" },
  { color: "#8000ff", label: "紫色" }, { color: "#00ff00", label: "绿色" },
  { color: "#ff8000", label: "橙色" }, { color: "#ff1493", label: "粉色" },
  { color: "#ffffff", label: "白色" }, { color: "#0066ff", label: "蓝色" },
];

const description = (reason?: string) => reason ? (reasons[reason] ?? reason) : "";
const when = (stamp: number) => new Date(stamp * 1000).toLocaleString();

function hueToHex(hue: number): string {
  const h = ((hue % 360) + 360) % 360;
  const x = 1 - Math.abs((h / 60) % 2 - 1);
  const [r, g, b] = h < 60 ? [1, x, 0] : h < 120 ? [x, 1, 0] : h < 180 ? [0, 1, x]
    : h < 240 ? [0, x, 1] : h < 300 ? [x, 0, 1] : [1, 0, x];
  return `#${[r, g, b].map((value) => Math.round(value * 255).toString(16).padStart(2, "0")).join("")}`;
}

function hexToHue(color: string): number {
  const r = parseInt(color.slice(1, 3), 16) / 255;
  const g = parseInt(color.slice(3, 5), 16) / 255;
  const b = parseInt(color.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min;
  if (!delta) return 0;
  const hue = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return Math.round((hue * 60 + 360) % 360);
}

function LightingSection() {
  const [state, setState] = useState<LightingState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const updateTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pendingPatch = useRef<Partial<LightingSettings>>({});

  useEffect(() => {
    let active = true;
    void getLightingState().then((value) => { if (active) setState(value); })
      .catch(() => { if (active) setError("无法读取灯光状态。"); });
    return () => { active = false; if (updateTimer.current) clearTimeout(updateTimer.current); };
  }, []);

  const save = async (patch: Partial<LightingSettings>) => {
    setBusy(true);
    setError("");
    try { setState(await updateLighting(patch)); }
    catch { setError("灯光设置失败，请查看设备状态。 "); }
    finally { setBusy(false); }
  };

  const change = (patch: Partial<LightingSettings>, delayed = false) => {
    if (!state) return;
    setState((current) => current ? { ...current, ...patch } : current);
    pendingPatch.current = { ...pendingPatch.current, ...patch };
    if (updateTimer.current) clearTimeout(updateTimer.current);
    const commit = () => {
      const next = pendingPatch.current;
      pendingPatch.current = {};
      updateTimer.current = undefined;
      void save(next);
    };
    if (delayed) updateTimer.current = setTimeout(commit, 180);
    else commit();
  };

  if (!state) return <PanelSection title="摇杆灯光"><PanelSectionRow>
    <div style={{ fontSize: 12 }}>{error || "正在读取灯光设备…"}</div>
  </PanelSectionRow></PanelSection>;

  const selectedEffect = effectOptions.find((option) => option.data === state.effect) ?? effectOptions[0];
  return <PanelSection title="摇杆灯光">
    {!state.available && <PanelSectionRow><div style={{ fontSize: 12, color: "#ffcc80", lineHeight: 1.6 }}>
      未检测到 Ally RGB 设备。{state.device.error ? ` ${state.device.error}` : ""}
      <div style={{ opacity: 0.75 }}>{state.device.path}</div>
    </div></PanelSectionRow>}
    <PanelSectionRow>
      <ToggleField label="启用灯光" checked={state.enabled} disabled={!state.available || busy}
        description="控制左右摇杆灯环；关闭不会修改 MCU 省电设置。"
        onChange={(enabled) => change({ enabled })} />
    </PanelSectionRow>
    {state.available && state.enabled && <>
      <PanelSectionRow>
        <SliderField label="颜色" value={hexToHue(state.color)} min={0} max={360} step={5}
          showValue={false} onChange={(hue) => change({ color: hueToHex(hue) }, true)} />
      </PanelSectionRow>
      <PanelSectionRow><div style={{ width: "100%", height: 12, borderRadius: 6, marginTop: -8,
        background: "linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)" }} /></PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: 12, marginBottom: 8 }}>预设颜色</div>
        <Focusable flow-children="horizontal" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {presets.map((preset) => <DialogButton key={preset.color} aria-label={preset.label}
            onClick={() => change({ color: preset.color })} style={{ minWidth: 54, width: 54, height: 42, padding: 0,
              background: preset.color, color: preset.color.toLowerCase() === "#ffffff" ? "#111" : "#fff",
              border: state.color.toLowerCase() === preset.color ? "3px solid white" : "1px solid rgba(255,255,255,.4)" }}>
            {state.color.toLowerCase() === preset.color ? "✓" : ""}
          </DialogButton>)}
        </Focusable>
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField label="亮度" value={state.brightness} min={0} max={100} step={5} valueSuffix="%"
          showValue onChange={(brightness) => change({ brightness: Math.round(brightness) }, true)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem label="效果" rgOptions={effectOptions} selectedOption={selectedEffect}
          strDefaultLabel={selectedEffect.label} menuLabel={selectedEffect.label} disabled={busy}
          onChange={(option) => change({ effect: option.data as LightingEffect })} />
      </PanelSectionRow>
      {animatedEffects.includes(state.effect) && <PanelSectionRow>
        <SliderField label="动画速度" value={state.speed} min={0} max={100} step={10} valueSuffix="%"
          showValue onChange={(speed) => change({ speed: Math.round(speed) }, true)} />
      </PanelSectionRow>}
    </>}
    {(error || state.last_error) && <PanelSectionRow><div style={{ color: "#ffcc80", fontSize: 12 }}>
      {error || state.last_error}
    </div></PanelSectionRow>}
    {state.available && <PanelSectionRow><div style={{ fontSize: 12, opacity: 0.7 }}>
      {state.device.zones} 个颜色通道 · 最大亮度 {state.device.max_brightness}
    </div></PanelSectionRow>}
  </PanelSection>;
}

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
    <LightingSection />
    <PanelSection title="唤醒手柄恢复">
      <PanelSectionRow><div style={{ lineHeight: 1.6 }}>
        <strong>{phases[status.phase] ?? status.phase}</strong>
        <div style={{ fontSize: 12, opacity: 0.75 }}>{description(status.reason)}</div>
        <div style={{ fontSize: 12, marginTop: 8 }}>后台自动处理，无需打开此面板。</div>
      </div></PanelSectionRow>
      {settings && <>
        <PanelSectionRow><ToggleField label="自动恢复" checked={settings.enabled} disabled={saving}
          description="休眠时不干预；唤醒后先等待 SteamOS 自行恢复，仅在输入链路连续检测异常时重启 InputPlumber。"
          onChange={(enabled) => { void save({ enabled }); }} /></PanelSectionRow>
        <PanelSectionRow><SliderField label="SteamOS 自恢复等待时间" value={settings.delay_seconds}
          min={3} max={15} step={1} disabled={saving} showValue valueSuffix=" 秒"
          onChange={(delay_seconds) => { void save({ delay_seconds: Math.round(delay_seconds) }); }} /></PanelSectionRow>
      </>}
      {(error || status.settings_error) && <PanelSectionRow><div style={{ color: "#ffcc80", fontSize: 12 }}>
        {error || "配置文件读取失败，自动恢复已关闭。"}
      </div></PanelSectionRow>}
    </PanelSection>
    <PanelSection title="最近处理">
      <PanelSectionRow><div style={{ fontSize: 12, lineHeight: 1.7 }}>
        {status.last_result ? <>
          <div>{when(status.last_result.time)}</div>
          <div>{phases[status.last_result.outcome] ?? status.last_result.outcome}</div>
          <div>{description(status.last_result.reason)}</div>
        </> : <div>尚无唤醒处理记录。</div>}
        {status.last_check && <div style={{ marginTop: 8 }}>最近检测：{description(status.last_check.reason)}</div>}
        <div style={{ marginTop: 8 }}>监听：{status.monitor === "logind_and_clock" ? "系统信号 + 时钟兜底"
          : status.monitor === "clock_fallback" ? "时钟兜底" : "尚未运行"}</div>
      </div></PanelSectionRow>
    </PanelSection>
    <PanelSection title="设备与诊断">
      <PanelSectionRow><div style={{ fontSize: 12, lineHeight: 1.7, overflowWrap: "anywhere" }}>
        <div>{status.host?.product || "设备信息加载中"}</div>
        {status.host && <div>SteamOS {status.host.os_version} · {status.host.kernel}</div>}
        <div style={{ marginTop: 8 }}>诊断自动保存：{status.log_path || "加载中"}</div>
        <div style={{ marginTop: 8 }}>0.2.0 实验版 · 新增摇杆灯光控制。</div>
      </div></PanelSectionRow>
    </PanelSection>
  </>;
}

export default definePlugin(() => {
  let lastLockState: boolean | undefined;
  let lastLockSync = 0;
  let syncingLockState = false;
  const syncLockState = (force = false) => {
    try {
      const detector = window.securitystore?.IsLockScreenActive;
      if (typeof detector !== "function") return;
      const locked = Boolean(detector.call(window.securitystore));
      const stale = Date.now() - lastLockSync >= 10000;
      if ((force || stale || locked !== lastLockState) && !syncingLockState) {
        syncingLockState = true;
        void setLockState(locked).then(() => { lastLockState = locked; lastLockSync = Date.now(); })
          .catch(() => undefined).finally(() => { syncingLockState = false; });
      }
    } catch { /* Older Steam clients may not expose the store. */ }
  };
  syncLockState(true);
  const lockTimer = setInterval(() => syncLockState(), 1000);
  const refreshLockState = () => syncLockState(true);
  window.addEventListener("focus", refreshLockState);
  document.addEventListener("visibilitychange", refreshLockState);
  return {
    name: "DeckyAlly",
    titleView: <div className={staticClasses.Title}>DeckyAlly</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      clearInterval(lockTimer);
      window.removeEventListener("focus", refreshLockState);
      document.removeEventListener("visibilitychange", refreshLockState);
    },
  };
});
