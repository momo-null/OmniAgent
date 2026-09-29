import { useMemo, useState } from "react";
import {
  Box,
  Button,
  ListSubheader,
  Menu,
  MenuItem,
  Typography,
} from "@mui/material";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import type { ModelProvider, ModelSlotCurrent } from "../types";

/**
 * 模型选择器（Chat 输入框底行）：按提供方分组的下拉菜单。
 *
 * - `value` 为 selection `"<provider_id>/<model_id>"`，空串 = 跟随配置（brain / runtime.agents）。
 * - 分组数据来自后端目录 ~/.omniagent/models.json，前端不预设任何厂商。
 */
export default function ModelPicker({
  value,
  providers,
  current,
  disabled,
  onChange,
}: {
  value: string;
  providers: ModelProvider[];
  current?: ModelSlotCurrent;
  disabled?: boolean;
  onChange: (selection: string) => void;
}) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);

  // 只显示模型名（显示名优先）；跟随配置时用后端解析出的实际模型 id
  const label = useMemo(() => {
    const [, mid] = value.split("/");
    if (mid) {
      const p = providers.find((x) => x.id === value.split("/")[0]);
      const m = p?.models.find((x) => x.id === mid);
      return m?.label || mid;
    }
    return current?.model || "默认模型";
  }, [value, providers, current]);

  const total = providers.reduce((n, p) => n + p.models.length, 0);

  // 关闭菜单并把触发按钮失焦：否则按钮保留 ：focus 外观，看起来像一直处于选中态
  const close = () => {
    (anchorEl as HTMLElement | null)?.blur?.();
    setAnchorEl(null);
  };

  return (
    <Box>
      <Button
        size="small"
        disabled={disabled}
        disableRipple
        onClick={(e) => setAnchorEl(e.currentTarget)}
        endIcon={<KeyboardArrowDownIcon sx={{ fontSize: 16 }} />}
        sx={{
          color: "text.primary",
          textTransform: "none",
          px: 1,
          minWidth: 0,
          fontSize: 12,
          "&:hover": { bgcolor: "transparent" },
          // 无焦点态外观：focus / focusVisible 一律保持普通样子
          "&:focus": { bgcolor: "transparent", color: "text.primary" },
          "&.Mui-focusVisible": { bgcolor: "transparent", color: "text.primary" },
        }}
        title={disabled ? "运行中不能切换模型" : "选择模型"}
      >
        {label}
      </Button>
      <Menu
        anchorEl={anchorEl}
        open={Boolean(anchorEl)}
        onClose={close}
        anchorOrigin={{ vertical: "top", horizontal: "left" }}
        transformOrigin={{ vertical: "bottom", horizontal: "left" }}
        slotProps={{ paper: { sx: { maxHeight: 340, width: 260 } } }}
      >
        <MenuItem
          selected={!value}
          onClick={() => { onChange(""); close(); }}
        >
          <Box>
            <Typography variant="body2">跟随配置默认</Typography>
            <Typography variant="caption" color="text.secondary">
              {current?.model || "（config: brain / runtime.agents）"}
            </Typography>
          </Box>
        </MenuItem>
        {total === 0 && (
          <MenuItem disabled>
            <Typography variant="caption" color="text.secondary">
              目录为空，请到设置 › 模型 添加提供方
            </Typography>
          </MenuItem>
        )}
        {providers.map((p) => [
          <ListSubheader key={`h-${p.id}`} sx={{ bgcolor: "transparent", lineHeight: "28px" }}>
            {p.label}
          </ListSubheader>,
          ...p.models.map((m) => (
            <MenuItem
              key={`${p.id}/${m.id}`}
              selected={value === `${p.id}/${m.id}`}
              onClick={() => { onChange(`${p.id}/${m.id}`); close(); }}
            >
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" noWrap>{m.label || m.id}</Typography>
                {(m.label && m.label !== m.id) && (
                  <Typography variant="caption" color="text.secondary" noWrap>{m.id}</Typography>
                )}
              </Box>
            </MenuItem>
          )),
        ])}
      </Menu>
    </Box>
  );
}
