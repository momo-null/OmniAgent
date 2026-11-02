import { useState } from "react";
import { Group, Segmented, SettingRow, SegOption } from "../../components/SettingsKit";
import {
  Appearance, FontSizeKey, IconToneKey, OverlayKey, FONT_PX,
  loadAppearance, saveAppearance,
} from "../../theme/appearance";

// 外观偏好只落本机（localStorage），点一下即生效，没有「保存」这一步。
type ThemeKey = "dark" | "light";
const FONT_OPTIONS = [
  { value: "compact" as FontSizeKey, label: `紧凑 ${FONT_PX.compact}` },
  { value: "standard" as FontSizeKey, label: `标准 ${FONT_PX.standard}` },
  { value: "large" as FontSizeKey, label: `舒适 ${FONT_PX.large}` },
];

const TONE_OPTIONS = [
  { value: "soft" as IconToneKey, label: "柔和" },
  { value: "standard" as IconToneKey, label: "标准" },
  { value: "crisp" as IconToneKey, label: "清晰" },
];

const OVERLAY_OPTIONS = [
  { value: "solid" as OverlayKey, label: "纯底色" },
  { value: "translucent" as OverlayKey, label: "毛玻璃" },
];

// 亮色挂灰：配色 token 化那轮才放开，先把入口摆出来而不是藏起来
const THEME_OPTIONS: SegOption<ThemeKey>[] = [
  { value: "dark", label: "深色" },
  { value: "light", label: "亮色", disabled: true },
];

export default function AppearanceSection() {
  const [pref, setPref] = useState<Appearance>(loadAppearance);
  const [themeKey, setThemeKey] = useState<ThemeKey>("dark");

  const patch = (p: Partial<Appearance>) => {
    const next = { ...pref, ...p };
    setPref(next);
    saveAppearance(next);
  };

  return (
    <Group title="外观" desc="只影响这台设备上的界面呈现，选中立即生效。">
      <SettingRow
        title="字号"
        desc="缩放正文与界面文字：聊天正文、消息列表、设置表单按此档等比重排。"
      >
        <Segmented ariaLabel="字号" value={pref.fontSize} options={FONT_OPTIONS}
          onChange={(v) => patch({ fontSize: v })} />
      </SettingRow>
      <SettingRow
        title="图标浓度"
        desc="线性图标的整体不透明度：越柔和，图标与深底反差越低，页面越安静。"
      >
        <Segmented ariaLabel="图标浓度" value={pref.iconTone} options={TONE_OPTIONS}
          onChange={(v) => patch({ iconTone: v })} />
      </SettingRow>
      <SettingRow
        title="遮罩背景"
        desc="设置与技能工具这两层全屏遮罩的底色：纯底色像原生窗口、背后不透字；毛玻璃才做半透明虚化。"
      >
        <Segmented ariaLabel="遮罩背景" value={pref.overlay} options={OVERLAY_OPTIONS}
          onChange={(v) => patch({ overlay: v })} />
      </SettingRow>
      <SettingRow
        title="主题"
        desc="亮色需要先全站颜色收敛成主题变量（当前配色按暗色校准），暂未开放。"
      >
        <Segmented ariaLabel="主题" value={themeKey} options={THEME_OPTIONS} onChange={setThemeKey} />
      </SettingRow>
    </Group>
  );
}
