// 全屏遮罩外壳：盖住整个窗口（含侧栏）的一层面板，默认纯底色（外观「遮罩背景」可切毛玻璃），
// 顶栏标题+搜索+Esc/✕，左栏分类导航，右栏分组表单。设置与技能工具共用这一套，两页只差分类数据。
import { ReactElement, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  Box,
  Divider,
  IconButton,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Modal,
  TextField,
  Typography,
} from "@mui/material";
import { SearchIcon, XIcon } from "./icons";

export interface NavCategory {
  id: string;
  label: string;
  icon: ReactNode;
  /** 分类名之外再列上这一栏里看得见的设置项，输入「压缩」就能落到编排 */
  keywords: string[];
  Section: () => ReactElement;
}

export default function NavOverlay({
  title, categories, onClose,
}: { title: string; categories: NavCategory[]; onClose: () => void }) {
  const [activeId, setActiveId] = useState<string>(categories[0].id);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return categories;
    return categories.filter((c) =>
      `${c.label} ${c.keywords.join(" ")}`.toLowerCase().includes(q));
  }, [query, categories]);

  // 当前分类被筛掉时自动落到第一个命中项
  useEffect(() => {
    if (matches.length > 0 && !matches.some((c) => c.id === activeId)) setActiveId(matches[0].id);
  }, [matches, activeId]);

  // 换分类回到顶部，否则下一栏从上一栏的滚动位置接着显示
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [activeId]);

  const active = matches.find((c) => c.id === activeId)
    ?? categories.find((c) => c.id === activeId)
    ?? categories[0];
  const Section = active.Section;

  return (
    <Modal
      open
      role="dialog"
      aria-modal="true"
      aria-labelledby="omni-overlay-title"
      onClose={(_, reason) => { if (reason !== "backdropClick") onClose(); }}
      BackdropProps={{
        sx: { bgcolor: "rgba(10,10,10,0.66)", backdropFilter: "blur(22px)", WebkitBackdropFilter: "blur(22px)" },
      }}
    >
      <Box
        sx={{
          position: "fixed", inset: 0, zIndex: 1300, display: "flex", flexDirection: "column",
          outline: "none",
          // 底色由外观偏好写进 CSS 变量：默认纯底（看不出是网页），毛玻璃是显式选项
          bgcolor: "var(--omni-overlay-bg)",
          backdropFilter: "blur(var(--omni-overlay-blur))",
          WebkitBackdropFilter: "blur(var(--omni-overlay-blur))",
        }}
        onKeyDown={(e) => {
          // 「/」聚焦搜索：输入框里打的「/」不算快捷键
          if (e.key !== "/" || (e.target as HTMLElement).closest("input, textarea")) return;
          e.preventDefault();
          searchRef.current?.focus();
        }}
      >
        {/* 顶栏 */}
        <Box sx={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 1.5, px: 2, py: 1.25 }}>
          <Typography id="omni-overlay-title" variant="subtitle1" sx={{ fontWeight: 600 }}>{title}</Typography>
          <Box sx={{ flex: 1 }} />
          <Box sx={{
            display: "flex", alignItems: "center", gap: 0.75, width: { xs: 168, sm: 240 }, height: 32,
            px: 1, borderRadius: 2, bgcolor: "action.hover",
          }}>
            <SearchIcon sx={{ fontSize: 16, color: "text.disabled", flexShrink: 0 }} />
            <TextField
              size="small" variant="standard" inputRef={searchRef} fullWidth
              value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索" inputProps={{ "aria-label": `在${title}中搜索` }}
              InputProps={{ disableUnderline: true }}
              sx={{ flex: 1, minWidth: 0, "& input": { fontSize: 13, padding: "2px 0" } }}
            />
            {query && (
              <Typography variant="caption" color="text.disabled" sx={{ flexShrink: 0 }}>
                {matches.length} 栏
              </Typography>
            )}
          </Box>
          <Box aria-hidden sx={{
            px: 0.75, py: 0.1, borderRadius: 0.75, border: "1px solid", borderColor: "divider",
            fontSize: 11, color: "text.disabled", lineHeight: 1.6, flexShrink: 0,
          }}>Esc</Box>
          <IconButton size="small" onClick={onClose} aria-label={`关闭${title}`}>
            <XIcon fontSize="small" />
          </IconButton>
        </Box>
        <Divider sx={{ borderColor: "divider" }} />

        <Box sx={{ flex: 1, minHeight: 0, display: "flex" }}>
          {/* 左栏分类 */}
          <Box component="nav" aria-label={`${title}分类`} sx={{
            width: 178, flexShrink: 0, minHeight: 0, overflowY: "auto", p: 1.25,
            borderRight: "1px solid", borderColor: "divider",
          }}>
            {matches.map((c) => {
              const selected = c.id === active.id;
              return (
                <ListItemButton
                  key={c.id} selected={selected} onClick={() => setActiveId(c.id)}
                  sx={{ borderRadius: 2, py: 0.7, mb: 0.25 }}
                >
                  <ListItemIcon sx={{ minWidth: 28, color: selected ? "text.primary" : "text.secondary" }}>
                    {c.icon}
                  </ListItemIcon>
                  <ListItemText primary={c.label} primaryTypographyProps={{ fontSize: "0.9286rem" }} />
                </ListItemButton>
              );
            })}
            {matches.length === 0 && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: 1, py: 1 }}>
                没有匹配的分类
              </Typography>
            )}
          </Box>

          {/* 右栏分组表单：窄屏贴左，宽屏把 760 列居中，两侧留等宽余量 */}
          <Box ref={scrollRef} sx={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
            <Box sx={{ maxWidth: 760, mx: "auto", px: { xs: 2, sm: 3 }, py: 2 }}>
              <Section />
            </Box>
          </Box>
        </Box>
      </Box>
    </Modal>
  );
}
