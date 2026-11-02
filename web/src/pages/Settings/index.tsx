import {
  CpuIcon,
  FaceSlightlySmilingIcon,
  InfoIcon,
  NetworkIcon,
  PaletteIcon,
  ShieldIcon,
  SlidersHorizontalIcon,
} from "../../components/icons";
import NavOverlay, { NavCategory } from "../../components/NavOverlay";
import AboutSection from "./AboutSection";
import AppearanceSection from "./AppearanceSection";
import CompanionSection from "./CompanionSection";
import GeneralSection from "./GeneralSection";
import ModelSection from "./ModelSection";
import OrchestrationSection from "./OrchestrationSection";
import SecuritySection from "./SecuritySection";

// 设置 = 盖住整窗的遮罩（外壳见 components/NavOverlay），改动即时生效、没有「保存」；
// Esc 或右上 ✕ 关闭回到原视图。这里只声明分类与它们的搜索词表。
const CATEGORIES: NavCategory[] = [
  {
    id: "appearance", label: "外观", icon: <PaletteIcon />, keywords:
      ["字号", "文字大小", "主题", "深色", "亮色", "图标", "浓度", "显示"],
    Section: AppearanceSection,
  },
  {
    id: "general", label: "通用", icon: <SlidersHorizontalIcon />, keywords:
      ["默认步数上限", "总预算", "max_steps", "自动蒸馏", "技能", "数据目录", "~/.omniagent"],
    Section: GeneralSection,
  },
  {
    id: "companion", label: "伙伴", icon: <FaceSlightlySmilingIcon />, keywords:
      ["角色卡", "人格", "助手名", "用户画像", "记忆", "注入", "character", "profile"],
    Section: CompanionSection,
  },
  {
    id: "security", label: "安全", icon: <ShieldIcon />, keywords:
      ["权限档位", "标准", "只读", "完全访问", "审批", "弹卡", "等待上限", "审计记录", "危险动作"],
    Section: SecuritySection,
  },
  {
    id: "model", label: "模型", icon: <CpuIcon />, keywords:
      ["在线提供方", "api key", "base_url", "本地模型", "gguf", "llama.cpp", "显存", "gpu",
        "端口", "线程", "上下文", "投影", "mmproj", "启动参数", "校验"],
    Section: ModelSection,
  },
  {
    id: "orchestration", label: "编排", icon: <NetworkIcon />, keywords:
      ["派发", "轮次", "并发", "子 agent", "分块步数", "长任务压缩", "上下文治理",
        "粘性压缩", "工具输出修剪", "重复失败", "无进展", "token"],
    Section: OrchestrationSection,
  },
  {
    id: "about", label: "关于", icon: <InfoIcon />, keywords:
      ["内核", "执行路径", "配置生效范围", "分层形态", "单主 agent"],
    Section: AboutSection,
  },
];

export default function Settings({ onClose }: { onClose: () => void }) {
  return <NavOverlay title="设置" categories={CATEGORIES} onClose={onClose} />;
}
