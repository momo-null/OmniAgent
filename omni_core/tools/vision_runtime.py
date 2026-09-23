"""视觉能力实现（**L1 工具插件层**，已从 `omni_core/local/` 迁出）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.4）：
本文件是视觉/SoM 的**能力实现**，与 `devices/` 同属 L1，不在内核里。

职责边界：
- 本地 VLM 仅做「视觉工具」，控制权在大脑；大脑可自主选择用原生视觉还是调用本工具。
- 两套 SoM 对称陈列，agent 按界面类型选用：
  * ``som_ground``：基于 u2 结构化 UI 树编号（原生 App，有层级时）。
  * ``som_marks``：视觉 Set-of-Marks，把截图送本地视觉模型直接在像素层面标区域，
    返回归一化坐标（无结构化层级、自绘/Unity 渲染等界面时）。

**α 决策（SoM 状态归属）**：marks 的跨步状态**不在这里**，由 tool 插件
``omni_core/tools/vision_tool.py`` 自持（`som://last_result`），内核零感知。
因此本运行时的 `som_ground` / `visual_so_m` 只负责产出 marks，
`tap_mark(mark)` 接收一个具体的 mark 并执行落点——不持有任何跨步状态。

实现：
- ``LocalVision``：OpenAI 兼容多模态客户端，命中 model_hub 托管的本地
  视觉模型 server；``describe(image_path, prompt) -> str``。
- ``parse_ui_nodes`` / ``build_som``：u2 hierarchy XML -> 编号框（纯 PIL）。
- ``VisionRuntime``：把 ExecutionModule 与 LocalVision 粘起来。
"""
import base64
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from utils import get_logger

logger = get_logger("tools.vision")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _image_to_data_url(image_path: str) -> str:
    """把本地图片读为 base64 data URL（OpenAI 多模态消息用）。"""
    ext = os.path.splitext(image_path)[1].lower()
    mime = _MIME.get(ext, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _as_float(v: Any) -> Optional[float]:
    """宽松地把各种形式转成 float；失败返回 None。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().strip("%"))
        except ValueError:
            return None
    return None


def _image_size(image_path: str) -> tuple:
    """读取图片宽高；失败返回 (0, 0)（解析器退化为千分制判定）。"""
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return (0, 0)


def _truncate(text: str, limit: int = 800) -> str:
    """把回传大模型的工具结果限长，避免小模型 ctx 溢出。

    完整内容（如 SoM 原始 JSON）仅用于调试落盘，不进 LLM 上下文。
    """
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + f"...[截断，原长 {len(text)}]"


def parse_ui_nodes(xml_str: str) -> List[Dict[str, Any]]:
    """从 u2 ``dump_hierarchy`` XML 抽取可标注元素。

    Args:
        xml_str: u2 hierarchy XML 字符串。
    Returns:
        元素列表，每项 ``{bounds:[x1,y1,x2,y2], resource_id, text, class}``；
        仅保留面积 > 0 的节点。解析失败返回空列表（不抛异常）。
    """
    elements: List[Dict[str, Any]] = []
    if not xml_str:
        return elements
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        logger.warning("UI 树 XML 解析失败，返回空元素列表")
        return elements

    pat = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
    for node in root.iter("node"):
        bounds_raw = node.get("bounds", "")
        m = pat.search(bounds_raw)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue  # 零面积节点不参与标注
        elements.append({
            "bounds": [x1, y1, x2, y2],
            "resource_id": node.get("resource-id", "") or "",
            "text": (node.get("text") or "").strip(),
            "class": node.get("class", "").rsplit(".", 1)[-1],
        })
    return elements


def build_som(
    image_path: str,
    elements: List[Dict[str, Any]],
    out_path: Optional[str] = None,
) -> tuple:
    """在截图副本上给元素画编号框，返回 ``(标注图路径, marks)``。

    Args:
        image_path: 原始截图路径。
        elements: ``parse_ui_nodes`` 产出的元素列表（含 bounds）。
        out_path: 标注图保存路径；缺省落到 temp/som_<name>.png。
    Returns:
        ``(marked_path, marks)``，marks[i] = ``{id, ref, label, bounds, center}``。
    """
    from PIL import Image, ImageDraw

    if out_path is None:
        base = os.path.basename(image_path)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_path = os.path.join(root, "temp", f"som_{base}")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    marks: List[Dict[str, Any]] = []
    for i, el in enumerate(elements):
        x1, y1, x2, y2 = el["bounds"]
        mid = i + 1
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=2)
        draw.text((x1 + 2, y1 + 2), str(mid), fill=(255, 0, 0))
        label = el.get("text") or el.get("class") or ""
        marks.append({
            "id": mid,
            "ref": el.get("resource_id", ""),
            "label": label,
            "bounds": [x1, y1, x2, y2],
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
        })
    img.save(out_path)
    return out_path, marks


class LocalVision:
    """本地多模态 VLM 客户端（OpenAI 兼容 ``/chat/completions``）。

    设计要点：
    - 仅依赖 httpx；``client`` 可注入（测试用 mock），默认 httpx.Client。
    - 图片以 base64 data URL 随文本消息一并发送。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 120.0,
        disable_thinking: bool = True,
        client: Any = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.disable_thinking = disable_thinking
        if client is not None:
            self._cli = client
        else:
            import httpx

            self._cli = httpx.Client(
                timeout=timeout,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )

    def describe(self, image_path: str, prompt: str, max_tokens: int = 512) -> str:
        """对一张截图提问，返回本地 VLM 的文本回答。"""
        data_url = _image_to_data_url(image_path)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
        }
        # 部分视觉模型（如 Qwen3.x）默认开启思考链，答案会落在 reasoning_content 而
        # message.content 为空；执行层要直接答案，故默认关闭 thinking。该行为可由
        # vision.disable_thinking 配置关闭（非此类模型可设 false 跳过）。
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            resp = self._cli.post(f"{self.base_url}/chat/completions", json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"[LocalVision] HTTP {resp.status_code}: {resp.text[:800]}")
            data = resp.json()
            return data["choices"][0]["message"].get("content", "") or ""
        except Exception as e:
            logger.error("LocalVision.describe 失败: %s", e)
            raise

    def close(self):
        try:
            self._cli.close()
        except Exception:
            pass


class VisionRuntime:
    """把执行后端（截图 / UI 树）与本地 VLM 粘起来的运行时。

    由 ``tool_loop`` 在 ``runtime.vision.enabled`` 时构造并注入 ``runtime["vision"]``。
    """

    def __init__(self, exec_module: Any, cfg: Optional[dict] = None, client: Any = None):
        self.exec = exec_module
        self.cfg = cfg or {}
        self._client = client
        self.enabled = bool(self.cfg.get("enabled", False))
        self.model = self.cfg.get("model", "")
        self.base_url = self.cfg.get("base_url")  # 缺省从 model_hub 解析
        # 部分视觉模型默认开启思考链，执行层要直接答案；可由 vision.disable_thinking 关闭
        self.disable_thinking = bool(self.cfg.get("disable_thinking", True))
        self._vision: Optional[LocalVision] = None
        # 注意：这里**不**持有 SoM marks（α 决策：状态归 vision_tool 插件自持）

    # --- 解析 server 地址（缺省走 model_hub 实际端口）------------------------
    def _resolve_base_url(self) -> Optional[str]:
        if self.base_url:
            return self.base_url
        # 1) 优先走 model_hub（同一 ModelManager 实例时有效）
        try:
            from model_hub.manager import ModelManager

            mgr = ModelManager()
            name = (self.model or mgr.get_default_model() or "").strip().lower()
            for m in mgr.get_active_models():
                if not (m.get("process_alive") and m.get("healthy")):
                    continue
                # 大小写不敏感匹配（config 写模型名，model_hub 可能返回不同大小写）
                if not name or (m.get("name") or "").strip().lower() == name:
                    return f"http://127.0.0.1:{m['port']}"
        except Exception as e:
            logger.warning("从 model_hub 解析 VLM 端口失败: %s", e)
        # 2) 兜底：model_hub 仅维护进程内注册表，跨实例/跨进程启动的 server
        #    读不到 -> 直接探测本地候选端口上的 OpenAI 兼容 /health 端点。
        base = self._probe_local_vlm()
        if base:
            logger.info("探测到本地 VLM: %s", base)
            return base
        return None

    @staticmethod
    def _probe_local_vlm(candidates: Optional[range] = None) -> Optional[str]:
        """扫描本地候选端口，找到健康的 OpenAI 兼容视觉 server。"""
        import urllib.request

        if candidates is None:
            candidates = range(8080, 8121)  # llama.cpp 默认分配端口区间
        for port in candidates:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
                with urllib.request.urlopen(req, timeout=0.4) as r:
                    if r.status == 200:
                        return f"http://127.0.0.1:{port}"
            except Exception:
                continue
        return None

    def _ensure_vision(self) -> LocalVision:
        if self._vision is None:
            base = self._resolve_base_url()
            if not base:
                raise RuntimeError(
                    "[VisionRuntime] 无法解析本地 VLM 地址（base_url 未配置且无运行中的匹配模型）"
                )
            self._vision = LocalVision(
                base, self.model, client=self._client,
                disable_thinking=self.disable_thinking,
            )
        return self._vision

    # --- 工具实现 -----------------------------------------------------------
    def vision_describe(self, prompt: str, max_tokens: int = 512) -> Dict[str, Any]:
        """对当前截图做视觉理解，返回 VLM 文本。"""
        try:
            shot = self.exec.screenshot()
            path = shot.get("path")
            if not path:
                return {"ok": False, "error": "截图失败：" + str(shot.get("error", "未知"))}
            text = self._ensure_vision().describe(path, prompt, max_tokens)
            return {"ok": True, "screenshot": path, "description": _truncate(text)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def som_ground(self) -> Dict[str, Any]:
        """对当前界面做 SoM 标注：截图 + u2 结构化 UI 树 -> 编号框 + marks。"""
        try:
            shot = self.exec.screenshot()
            path = shot.get("path")
            if not path:
                return {"ok": False, "error": "截图失败：" + str(shot.get("error", "未知"))}
            tree = self.exec.get_ui_tree()
            xml = tree.get("ui_tree") if isinstance(tree, dict) else None
            if not xml:
                return {"ok": False, "error": "SoM 需要结构化 UI 树（emulator 后端）"}
            elements = parse_ui_nodes(xml)
            marked_path, marks = build_som(path, elements)
            return {
                "ok": True,
                "marked_image": marked_path,
                "marks": marks,
                "element_count": len(marks),
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def tap_mark(self, mark: Dict[str, Any]) -> Dict[str, Any]:
        """按一个具体的 SoM mark 落点。

        marks 的跨步状态由 `vision_tool` 插件自持（α 决策），本运行时不持有；
        调用方先从自己的状态里解析出 mark，再交给这里执行。

        结构化 mark（som_ground）映射到 resource-id；视觉 mark（som_marks）
        映射到归一化坐标（center），调用执行后端 click(x, y) 落点。
        """
        try:
            if not isinstance(mark, dict):
                return {"ok": False, "error": f"mark 不是对象: {mark!r}"}
            ref = mark.get("ref")
            if ref:
                return self.exec.tap_by_id(ref)
            center = mark.get("center")
            if center:
                # center 为归一化坐标 (0~1)，执行后端按设备归一化点击。
                # 注意：ExecutionModule 是委托壳，点击原语走 execute_mouse_action，
                # 而非不存在的 click() 方法（否则 AttributeError）。
                return self.exec.execute_mouse_action(
                    "click", {"x": center[0], "y": center[1]}
                )
            return {"ok": False, "error": f"mark {mark.get('id')} 既无 resource-id 也无坐标"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # --- 视觉 SoM（M2.5）：让本地视觉模型直接标区域，适合无结构化层级的界面（自绘/Unity 渲染等） ----------
    def visual_so_m(self, ask: str = "", max_tokens: int = 2048) -> Dict[str, Any]:
        """视觉 Set-of-Marks：把截图送本地视觉模型，让其在像素层面识别可交互区域并编号。

        与 ``som_ground``（基于 u2 结构化层级）互补：无结构化层级的界面（自绘/Unity 渲染）
        没有结构化 UI 树，必须用视觉 SoM。返回归一化坐标，由 ``tap_by_mark`` 落点。

        Returns:
            ``{"ok": True, "screenshot", "marked_image", "marks", "mark_count", "raw"}``
            marks[i] = ``{"id", "ref": "", "label", "center": [x, y](0~1)}``
        """
        try:
            shot = self.exec.screenshot()
            path = shot.get("path")
            if not path:
                return {"ok": False, "error": "截图失败：" + str(shot.get("error", "未知"))}
            prompt = (
                "这是一张设备截图。请识别画面中所有可交互/关键的元素"
                "（按钮、图标、链接、输入框等），并为每个元素分配一个编号。\n"
                "仅输出一个 JSON 数组，不要输出任何其他文字，格式：\n"
                "[{\"id\": 1, \"x\": 0.50, \"y\": 0.62, \"label\": \"开始按钮\"}, ...]\n"
                "其中 x, y 是该元素中心相对于整张图宽高的归一化坐标（0~1，小数）。\n"
            )
            if ask:
                prompt += f"重点：{ask}\n"
            text = self._ensure_vision().describe(path, prompt, max_tokens)
            img_w, img_h = _image_size(path)
            marks = self._parse_marks(text, img_w, img_h)
            marked_path = self._draw_visual_marks(path, marks)
            return {
                "ok": True,
                "screenshot": path,
                "marked_image": marked_path,
                "marks": marks,
                "mark_count": len(marks),
                "raw": _truncate(text),
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _parse_marks(
        text: str, img_w: int = 0, img_h: int = 0
    ) -> List[Dict[str, Any]]:
        """从 VLM 文本中抽取 JSON 数组 marks。

        真机暴露的两类容错（实测）：
        1. max_tokens 截断导致数组不闭合 -> 整体 loads 失败即全丢。
           现改为：整体解析失败后逐个抢救完整的 ``{...}`` 对象。
        2. 部分视觉模型会自发输出原生 grounding 字段 ``bbox_2d``（千分制/像素），
           且此时 ``x/y`` 字段常漂移不可信 -> 优先用 bbox 中心，按尺度归一化。
        """
        import json as _json
        import re as _re

        if not text:
            return []
        s = text.strip()
        start = s.find("[")
        if start == -1:
            logger.warning("visual_so_m: 未解析到 JSON 数组 -> %r", s[:200])
            return []
        end = s.rfind("]")
        items: List[Any] = []
        if end > start:
            try:
                items = _json.loads(s[start : end + 1])
            except _json.JSONDecodeError:
                items = []
        if not items:
            # 截断/畸形：逐个抢救完整的顶层对象
            for m in _re.finditer(r"\{[^{}]*\}", s[start:]):
                try:
                    items.append(_json.loads(m.group(0)))
                except _json.JSONDecodeError:
                    continue
            if items:
                logger.warning(
                    "visual_so_m: JSON 整体解析失败，抢救出 %d 个完整对象", len(items)
                )
            else:
                logger.warning("visual_so_m: JSON 解析失败 -> %r", s[start:start + 200])
                return []

        # 先收集 bbox 原始值，用于尺度判定（千分制 vs 像素）
        def _bbox_of(item: Dict[str, Any]) -> Optional[List[float]]:
            bb = item.get("bbox_2d") or item.get("bbox")
            if isinstance(bb, (list, tuple)) and len(bb) == 4:
                vals = [_as_float(v) for v in bb]
                if all(v is not None for v in vals):
                    return [float(v) for v in vals]  # type: ignore[arg-type]
            return None

        bboxes = [(_bbox_of(it) if isinstance(it, dict) else None) for it in items]
        max_coord = max((max(bb) for bb in bboxes if bb), default=0.0)
        # 尺度判定：<=1 已归一化；超出图像实际尺寸 -> 千分制；否则按像素
        if max_coord <= 1.0:
            sx = sy = 1.0
        elif img_w and img_h and max_coord <= max(img_w, img_h) and not any(
            bb and (bb[1] > img_h or bb[3] > img_h or bb[0] > img_w or bb[2] > img_w)
            for bb in bboxes
        ):
            sx, sy = float(img_w), float(img_h)
        else:
            sx = sy = 1000.0

        marks: List[Dict[str, Any]] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            bb = bboxes[i] if i < len(bboxes) else None
            if bb is not None:
                x = ((bb[0] + bb[2]) / 2.0) / sx
                y = ((bb[1] + bb[3]) / 2.0) / sy
            else:
                x = _as_float(item.get("x"))
                y = _as_float(item.get("y"))
                if x is None or y is None:
                    continue
                if x > 1.0 or y > 1.0:  # x/y 也可能给千分制
                    x, y = x / 1000.0, y / 1000.0
            marks.append({
                "id": int(item.get("id", i + 1)),
                "ref": "",
                "label": str(item.get("label", "")),
                "center": [min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)],
            })
        return marks

    def _draw_visual_marks(
        self, image_path: str, marks: List[Dict[str, Any]]
    ) -> Optional[str]:
        """在截图副本上按归一化中心画编号圈，便于观测；失败返回 None。"""
        try:
            from PIL import Image, ImageDraw

            img = Image.open(image_path).convert("RGB")
            W, H = img.size
            draw = ImageDraw.Draw(img)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out_path = os.path.join(root, "temp", "vsom_" + os.path.basename(image_path))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            r = max(12, min(W, H) // 40)
            for m in marks:
                cx, cy = m["center"]
                px, py = int(cx * W), int(cy * H)
                draw.ellipse([px - r, py - r, px + r, py + r], outline=(255, 0, 0), width=2)
                draw.text((px - 6, py - 8), str(m["id"]), fill=(255, 0, 0))
            img.save(out_path)
            return out_path
        except Exception as e:
            logger.warning("_draw_visual_marks 失败: %s", e)
            return None
