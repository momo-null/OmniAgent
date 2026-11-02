// 全站图标唯一出处：字形全部来自 Lucide（MUI 自带的 Outlined 太方、缺弧度，20px 下显笨）。
//
// 为什么不直接散用 lucide 组件：调用点已经写满 `fontSize="small"` / `sx={{ color }}`，
// 换成 Lucide 的 `size` / `color` 要改上百处。这里用 MUI 的 SvgIcon 当壳，对外保持原来那套
// SvgIconProps API，内部塞 Lucide 字形，浓度开关（index.css 唯一的 .MuiSvgIcon-root
// { opacity: var(--omni-icon-opacity) }）也就继续命中。
//
// 关键一点：Lucide 组件作为 SvgIcon 的 children 渲染成**内层 svg**，它的 fill="none" /
// stroke="currentColor" 是挂在有几何的那个元素身上的表现属性，才不会被外层
// `.MuiSvgIcon-root { fill: currentColor }` 那条 CSS 糊成实心（挂外层必被盖掉）。
// 内层 svg 的 24×24 落在根 svg 的 viewBox="0 0 24 24" 里，尺寸仍由 fontSize 档位决定。
import { SvgIcon } from "@mui/material";
import type { SvgIconProps } from "@mui/material/SvgIcon";
import type { LucideIcon } from "lucide-react";
import {
  ArrowLeft, AudioLines, Brain, Bug, Check, ChevronDown, ChevronRight, ChevronUp,
  Circle, CircleAlert, CircleCheck, CircleDot, CircleStop, Clock, Copy, Cpu, Download,
  EllipsisVertical, Eraser, ExternalLink, FaceSlightlySmiling, File, FileText, Folder,
  FolderOpen, Globe, GitCompareArrows, House, Image, Info, Mic, MicOff, Network, Palette,
  PanelRight, Pencil, Play, Plus, RefreshCw, Save, Search, Send, Server, Settings, Shield,
  SlidersHorizontal, Square, SquareCheck, SquareMinus, Terminal, Trash, TriangleAlert,
  Volume2, Wrench, X, Zap,
} from "lucide-react";

// Lucide 的设计基准是 24 格 / 线宽 2；这套界面图标多在 20px 上用，2 会偏糊，收一档。
// 全站只有这一个线宽常量，想整体变粗变细改这里。
export const LUCIDE_STROKE = 1.75;

function lu(Icon: LucideIcon) {
  return function LucideSvgIcon(props: SvgIconProps) {
    return (
      <SvgIcon {...props}>
        <Icon size={24} strokeWidth={LUCIDE_STROKE} />
      </SvgIcon>
    );
  };
}

// —— 导航 / 通用 ——
export const HouseIcon = lu(House);
export const PlusIcon = lu(Plus);
export const SearchIcon = lu(Search);
export const SettingsIcon = lu(Settings);
export const XIcon = lu(X);
export const CheckIcon = lu(Check);
export const InfoIcon = lu(Info);
export const CopyIcon = lu(Copy);
export const DownloadIcon = lu(Download);
export const ExternalLinkIcon = lu(ExternalLink);
export const EllipsisVerticalIcon = lu(EllipsisVertical);
export const PanelRightIcon = lu(PanelRight);

// —— 展开 / 折叠 / 方向 ——
export const ChevronDownIcon = lu(ChevronDown);
export const ChevronUpIcon = lu(ChevronUp);
export const ChevronRightIcon = lu(ChevronRight);
export const ArrowLeftIcon = lu(ArrowLeft);
export const RefreshCwIcon = lu(RefreshCw);

// —— 会话与运行 ——
export const SendIcon = lu(Send);
export const SquareIcon = lu(Square);
export const PlayIcon = lu(Play);
export const CircleStopIcon = lu(CircleStop);
export const BrainIcon = lu(Brain);
export const TerminalIcon = lu(Terminal);
export const TriangleAlertIcon = lu(TriangleAlert);
export const BugIcon = lu(Bug);
export const ZapIcon = lu(Zap);
export const ClockIcon = lu(Clock);
export const GlobeIcon = lu(Globe);
export const MicIcon = lu(Mic);
export const MicOffIcon = lu(MicOff);
export const Volume2Icon = lu(Volume2);
export const AudioLinesIcon = lu(AudioLines);

// —— 文件 / 目录 ——
export const FolderIcon = lu(Folder);
export const FolderOpenIcon = lu(FolderOpen);
export const FileIcon = lu(File);
export const FileTextIcon = lu(FileText);
export const ImageIcon = lu(Image);
export const GitCompareArrowsIcon = lu(GitCompareArrows);
export const NetworkIcon = lu(Network);
export const ServerIcon = lu(Server);

// —— 编辑 / 删除 / 保存 ——
export const PencilIcon = lu(Pencil);
export const TrashIcon = lu(Trash);
export const EraserIcon = lu(Eraser);
export const SaveIcon = lu(Save);
export const WrenchIcon = lu(Wrench);

// —— 设置分类与状态 ——
export const SlidersHorizontalIcon = lu(SlidersHorizontal);
export const PaletteIcon = lu(Palette);
export const ShieldIcon = lu(Shield);
export const CpuIcon = lu(Cpu);
export const FaceSlightlySmilingIcon = lu(FaceSlightlySmiling);

// —— MUI 组件内置图标（theme.tsx 里喂给 Select / Checkbox / Radio / Alert 当默认槽位）——
// 勾选态不写 fontSize：Checkbox 会 cloneElement 把 size 塞进来，写死了就吃掉 size="small"。
export const SquareCheckIcon = lu(SquareCheck);
export const SquareMinusIcon = lu(SquareMinus);
export const CircleIcon = lu(Circle);
export const CircleDotIcon = lu(CircleDot);
export const CircleAlertIcon = lu(CircleAlert);
export const CircleCheckIcon = lu(CircleCheck);
