import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Box } from "@mui/material";
import type { SxProps, Theme } from "@mui/material";

type Props = {
  children: React.ReactNode;
  sx?: SxProps<Theme>;
  maxHeight?: number;
  thumbColor?: string;
  hoverThumbColor?: string;
};

// 自绘滚动条：隐藏原生滚动条（避免 Windows/macOS 覆盖式滚动条在静态态被系统样式覆盖），
// 用原生 overflow 承载滚轮/触摸滚动，叠加一个绝对定位的可拖拽滑块。颜色 100% 可控、跨系统一致。
export default function ScrollArea({ children, sx, maxHeight, thumbColor = "rgb(60,60,60)", hoverThumbColor = "rgb(90,90,90)" }: Props) {
  const innerRef = useRef<HTMLDivElement>(null);
  const [m, setM] = useState({ top: 0, view: 0, content: 0 });
  const [hover, setHover] = useState(false);
  const dragging = useRef(false);
  const startY = useRef(0);
  const startTop = useRef(0);

  const measure = useCallback(() => {
    const el = innerRef.current;
    if (!el) return;
    setM({ top: el.scrollTop, view: el.clientHeight, content: el.scrollHeight });
  }, []);

  useLayoutEffect(() => {
    measure();
    const el = innerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure]);

  // 内容变化（如 SSE 流式追加文本）时重新测量
  useEffect(() => {
    measure();
  }, [children, measure]);

  const onScroll = () => measure();

  const maxScroll = Math.max(0, m.content - m.view);
  const showThumb = maxScroll > 1 && m.view > 0;
  const thumbH = showThumb ? Math.max(24, (m.view / m.content) * m.view) : 0;
  const thumbTop = showThumb ? (m.top / maxScroll) * (m.view - thumbH) : 0;

  const onThumbDown = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragging.current = true;
    startY.current = e.clientY;
    startTop.current = innerRef.current?.scrollTop ?? 0;
    document.body.style.userSelect = "none";
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const el = innerRef.current;
      if (!el) return;
      const dy = e.clientY - startY.current;
      const ratio = maxScroll / Math.max(1, m.view - thumbH);
      el.scrollTop = startTop.current + dy * ratio;
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  });

  return (
    <Box sx={{ position: "relative", overflow: "hidden", ...(sx as object) }}>
      <div
        ref={innerRef}
        onScroll={onScroll}
        className="omni-scroll-area-inner"
        style={{
          height: "100%",
          maxHeight: maxHeight ? `${maxHeight}px` : undefined,
          width: "100%",
          overflowY: "scroll",
          overflowX: "hidden",
        }}
      >
        {children}
      </div>
      {showThumb && (
        <div
          onMouseDown={onThumbDown}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          style={{
            position: "absolute",
            top: thumbTop,
            right: 2,
            width: 5,
            height: thumbH,
            borderRadius: 3,
            background: hover ? hoverThumbColor : thumbColor,
            cursor: "pointer",
            zIndex: 5,
          }}
        />
      )}
    </Box>
  );
}
