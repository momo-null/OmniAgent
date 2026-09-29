// LLM 返回内容的 Markdown 渲染。
//
// 为什么用 react-markdown 而不是 marked+innerHTML：
// 它把 markdown 编译成 React 元素、**不产生 dangerouslySetInnerHTML**，
// 原生 HTML 标签默认不渲染 ⇒ LLM 输出（可能携带网页注入内容）无法构造 XSS。
//
// 样式遵循对话区简约约定：透明底、无卡片感；仅代码块/行内代码用浅灰底等宽区分。
//
// 稳定性约定（关键）：组件表与插件表必须是**模块级常量**。若写在组件内部，
// 每次渲染都会生成新的函数引用，react-markdown 把它当成新的元素 type，
// 于是整棵子树被卸载重挂、DOM 节点被替换——用户在回复里点下的选区/插入点会
// 随之丢失并回落到块首（表现为「点击内容跳到最开始」，而纯 Typography 的
// 用户消息不受影响）。
import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { HTMLAttributes } from "react";
import { Box, Typography } from "@mui/material";

type P = HTMLAttributes<HTMLElement> & { node?: unknown };

const MD_COMPONENTS = {
  p: ({ node, ...props }: P) => (
    <Typography variant="body2" component="p" sx={{ fontSize: 14, my: 0.5 }} {...props} />
  ),
  h1: ({ node, ...props }: P) => <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600, mt: 1, mb: 0.5 }} {...props} />,
  h2: ({ node, ...props }: P) => <Typography variant="subtitle1" component="h4" sx={{ fontWeight: 600, mt: 1, mb: 0.5 }} {...props} />,
  h3: ({ node, ...props }: P) => <Typography variant="subtitle2" component="h5" sx={{ fontWeight: 600, mt: 0.75, mb: 0.5 }} {...props} />,
  ul: ({ node, ...props }: P) => <Box component="ul" sx={{ my: 0.5, pl: 3, fontSize: 14, "& li": { my: 0.25 } }} {...props} />,
  ol: ({ node, ...props }: P) => <Box component="ol" sx={{ my: 0.5, pl: 3, fontSize: 14, "& li": { my: 0.25 } }} {...props} />,
  blockquote: ({ node, ...props }: P) => (
    <Box component="blockquote" sx={{ my: 0.5, pl: 1, borderLeft: 2, borderColor: "divider", color: "text.secondary", fontSize: 14 }} {...props} />
  ),
  a: ({ node, ...props }: P) => <Box component="a" target="_blank" rel="noreferrer" sx={{ color: "primary.main" }} {...props} />,
  table: ({ node, ...props }: P) => (
    <Box
      component="table"
      sx={{
        my: 0.5, borderCollapse: "collapse", fontSize: 13,
        "& td, & th": { border: "1px solid", borderColor: "divider", px: 0.75, py: 0.25, textAlign: "left" },
      }}
      {...props}
    />
  ),
  // 行内代码：浅灰底等宽；代码块本体交给 pre 渲染（此处仅透传带 language- 的块级 code）
  code: ({ className, children, ...props }: HTMLAttributes<HTMLElement> & { node?: unknown }) => {
    if (/language-/.test(className || "")) {
      return <code className={className} {...props}>{children}</code>;
    }
    return (
      <Box component="code" sx={{ bgcolor: "rgba(0,0,0,0.05)", px: 0.5, borderRadius: 0.5, fontFamily: "monospace", fontSize: 13 }}>
        {children}
      </Box>
    );
  },
  pre: ({ node, ...props }: P) => (
    <Box component="pre" sx={{ bgcolor: "rgba(0,0,0,0.04)", p: 1, borderRadius: 1, overflow: "auto", fontSize: 13, my: 0.5, "& code": { fontFamily: "monospace" } }} {...props} />
  ),
};

const REMARK_PLUGINS = [remarkGfm];

function MarkdownInner({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      components={MD_COMPONENTS}
    >
      {children}
    </ReactMarkdown>
  );
}

// 文本不变时复用上次的渲染结果：SSE 每 0.3s 轮询会让父级高频重渲染，
// memo 让已完成、内容不再变化的回复彻底停止重解析。
export default memo(MarkdownInner);
