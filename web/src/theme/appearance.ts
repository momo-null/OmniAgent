// 本机显示偏好（字号 / 图标浓度 / 遮罩底色）：只影响这台设备的界面呈现，不进后端配置。
// ~/.omniagent/config.yaml 是跨设备共享的引擎真源，字号属于「这块屏幕离我多远」，
// 因此存 localStorage，并在渲染前同步应用，避免首屏按默认字号闪一下。
export type FontSizeKey = "compact" | "standard" | "large";
export type IconToneKey = "soft" | "standard" | "crisp";
export type OverlayKey = "solid" | "translucent";

export interface Appearance {
  fontSize: FontSizeKey;
  iconTone: IconToneKey;
  overlay: OverlayKey;
}

// 根字号与 index.css 的 html{font-size} 同源：MUI typography.htmlFontSize=14，
// rem 变体（body1/body2…）按根字号等比缩放，所以改这一处即整站缩放。
export const FONT_PX: Record<FontSizeKey, number> = {
  compact: 13,
  standard: 14,
  large: 16,
};

export const ICON_OPACITY: Record<IconToneKey, number> = {
  soft: 0.6,
  standard: 0.75,
  crisp: 0.92,
};

// 遮罩（设置 / 技能与工具）面板底色：solid 是与内容区同源的纯底色（更像原生窗口，
// 看不出这是网页）；translucent 才做毛玻璃。solid 的 #121212 与 theme.background.default 同源。
export const OVERLAY: Record<OverlayKey, { bg: string; blur: string }> = {
  solid: { bg: "#121212", blur: "0px" },
  translucent: { bg: "rgba(18,18,18,0.72)", blur: "22px" },
};

const STORAGE_KEY = "omniagent.appearance";
const DEFAULTS: Appearance = { fontSize: "standard", iconTone: "standard", overlay: "solid" };

export function loadAppearance(): Appearance {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const p = JSON.parse(raw) as Partial<Appearance>;
    return {
      fontSize: p.fontSize && p.fontSize in FONT_PX ? p.fontSize : DEFAULTS.fontSize,
      iconTone: p.iconTone && p.iconTone in ICON_OPACITY ? p.iconTone : DEFAULTS.iconTone,
      overlay: p.overlay && p.overlay in OVERLAY ? p.overlay : DEFAULTS.overlay,
    };
  } catch {
    return DEFAULTS;
  }
}

export function applyAppearance(a: Appearance): void {
  const root = document.documentElement;
  root.style.fontSize = `${FONT_PX[a.fontSize]}px`;
  root.style.setProperty("--omni-icon-opacity", String(ICON_OPACITY[a.iconTone]));
  root.style.setProperty("--omni-overlay-bg", OVERLAY[a.overlay].bg);
  root.style.setProperty("--omni-overlay-blur", OVERLAY[a.overlay].blur);
}

export function saveAppearance(a: Appearance): void {
  applyAppearance(a);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(a));
  } catch {
    /* 隐私模式等写不进去：本次会话仍然生效，不做提示 */
  }
}
