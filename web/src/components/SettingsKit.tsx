// 遮罩页（设置 / 技能与工具）的共享排版原语：桌面节奏 = 分组标题 + 「左标题/右控件」行
// + 发丝分隔，描述文案直接可见（原来藏在 Tooltip 里），保存状态是一行小字而不是绿色弹块。
import { ReactNode } from "react";
import {
  Box,
  ButtonBase,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { InfoIcon } from "./icons";

/** 控件旁的感叹号说明（长模型参数等仍用悬浮说明，不做成整段文案） */
export function LabelWithTip({ text, desc }: { text: string; desc?: string }) {
  return (
    <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      <Typography variant="inherit" component="span">{text}</Typography>
      {desc && (
        <Tooltip title={desc} arrow placement="top">
          <InfoIcon sx={{ fontSize: 15, color: "text.disabled", cursor: "help" }} />
        </Tooltip>
      )}
    </Box>
  );
}

export function Group({
  title, desc, children,
}: { title: string; desc?: ReactNode; children: ReactNode }) {
  return (
    <Box component="section" sx={{ mb: 4 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 500 }}>{title}</Typography>
      {desc && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>{desc}</Typography>
      )}
      <Box sx={{ mt: 0.5 }}>{children}</Box>
    </Box>
  );
}

/** 一行一个设置项：左侧标题+说明弹性占宽，右侧控件贴右缘；窄屏上下堆叠 */
export function SettingRow({
  title, desc, children,
}: { title: ReactNode; desc?: ReactNode; children?: ReactNode }) {
  return (
    <Box
      sx={{
        display: "flex",
        gap: 2,
        py: 1.5,
        borderTop: "1px solid",
        borderColor: "divider",
        flexDirection: { xs: "column", md: "row" },
        alignItems: { xs: "flex-start", md: "center" },
      }}
    >
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography variant="body2" sx={{ fontWeight: 500 }}>{title}</Typography>
        {desc && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25, lineHeight: 1.6 }}>
            {desc}
          </Typography>
        )}
      </Box>
      {children != null && (
        <Box sx={{ flexShrink: 0, display: "flex", justifyContent: { xs: "flex-start", md: "flex-end" }, width: { xs: "100%", md: "auto" } }}>
          {children}
        </Box>
      )}
    </Box>
  );
}

export interface SegOption<T extends string> {
  value: T;
  label: string;
  disabled?: boolean;
}

/** 分段选择器：整组互斥，选中项抬出一块亮底；≤4 个选项的枚举首选它而不是下拉框 */
export function Segmented<T extends string>({
  value, onChange, options, ariaLabel,
}: { value: T; onChange: (v: T) => void; options: SegOption<T>[]; ariaLabel: string }) {
  return (
    <Box role="radiogroup" aria-label={ariaLabel}
      sx={{ display: "flex", gap: 0.25, p: 0.25, borderRadius: 2, bgcolor: "rgba(255,255,255,0.05)" }}>
      {options.map((o) => {
        const selected = o.value === value;
        return (
          <ButtonBase
            key={o.value}
            role="radio"
            aria-checked={selected}
            disabled={o.disabled}
            onClick={() => onChange(o.value)}
            sx={{
              px: 1.25,
              py: 0.4,
              borderRadius: 1.5,
              fontSize: 12,
              fontWeight: 500,
              whiteSpace: "nowrap",
              color: selected ? "text.primary" : "text.secondary",
              bgcolor: selected ? "rgba(255,255,255,0.16)" : "transparent",
              "&:hover": o.disabled ? {} : { color: "text.primary" },
            }}
          >
            {o.label}
          </ButtonBase>
        );
      })}
    </Box>
  );
}

export function SwitchRow({
  title, desc, checked, onChange, disabled,
}: {
  title: ReactNode; desc?: ReactNode; checked: boolean;
  onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <SettingRow title={title} desc={desc}>
      <Switch size="small" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
    </SettingRow>
  );
}

/** 数值行：无边框右对齐输入，贴右缘——桌面设置里的数字都是这样摆的 */
export function NumberRow({
  title, desc, value, onChange, onBlur, min, max, step, disabled,
}: {
  title: ReactNode; desc?: ReactNode; value: number;
  onChange: (v: number) => void; onBlur?: () => void;
  min?: number; max?: number; step?: number; disabled?: boolean;
}) {
  return (
    <SettingRow title={title} desc={desc}>
      <TextField
        size="small" variant="outlined" disabled={disabled} value={value}
        type="number"
        onChange={(e) => onChange(Number(e.target.value))}
        onBlur={onBlur}
        inputProps={{ min, max, step, style: { textAlign: "right" } }}
        sx={{
          width: 132,
          "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
          "& .MuiOutlinedInput-root:hover .MuiOutlinedInput-notchedOutline": { borderColor: "text.disabled" },
          "& input": { fontSize: 13, py: 0.6 },
        }}
      />
    </SettingRow>
  );
}

/** 说明块：替代原来的蓝色 info Alert——暗色中性主题里色相弹块最显「网页感」 */
export function Hint({ children }: { children: ReactNode }) {
  return (
    <Box sx={{ mt: 1.5, px: 1.5, py: 1.25, borderRadius: 2, bgcolor: "rgba(255,255,255,0.04)",
               border: "1px solid", borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.7 }}>
        {children}
      </Typography>
    </Box>
  );
}

/** 保存状态：一行小字而不是绿色弹块；错误才用色并声明 role=alert */
export function StatusLine({ saved, error }: { saved?: string; error?: string }) {
  if (error) {
    return (
      <Typography variant="caption" role="alert" sx={{ display: "block", mt: 1, color: "error.main" }}>
        {error}
      </Typography>
    );
  }
  if (saved) {
    return (
      <Typography variant="caption" aria-live="polite" sx={{ display: "block", mt: 1, color: "text.disabled" }}>
        {saved}
      </Typography>
    );
  }
  return null;
}
