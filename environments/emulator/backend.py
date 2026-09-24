"""EmulatorBackend：Android 模拟器执行后端（uiautomator2 / ADB）。

把 ExecutionBackend 抽象工具翻译成 u2 原语（见 Master Spec §3）：

- type        -> set_fastinput_ime(True) + send_keys + set_fastinput_ime(False)
                 （IME 无关文本输入，根治本机输入法坑——这正是 M1 要解除的风险）
- click       -> d.click(x, y)（屏幕归一化坐标经 normalize_coordinate）
- observe     -> d.dump_hierarchy() 结构化 UI 树（文本化决策，不靠像素）+ 截图
- tap_by_id   -> d(resourceId=...).click()（按控件 id 可靠点击）
- get_ui_tree -> 原始 dump_hierarchy XML
- launch_app  -> d.app_start(package)
- press/press_keycode -> d.press(...) / shell input keyevent

设计要点：
- u2 懒导入（仅 EmulatorBackend 构造/连接时才 import），HostBackend 路径完全不碰 u2。
- connect() 懒连接：构造时不要求设备在线；首个操作或显式 connect() 时才连。
- 所有操作失败都返回结构化错误 dict，不抛异常打断 tool-loop。
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

# EasyOCR 懒导入：GPU 渲染的界面（如 Unity 自绘 UI）文字不在层级树中，
# 必须从截图做图像 OCR。复用 observer 的缓存 reader 避免重复初始化。
_EMULATOR_OCR_READER = None


def _get_ocr_reader():
    global _EMULATOR_OCR_READER
    if _EMULATOR_OCR_READER is None:
        import easyocr
        _EMULATOR_OCR_READER = easyocr.Reader(
            ["ch_sim"],
            gpu=True,
            model_storage_directory=os.path.expanduser("~/.EasyOCR/model"),
            verbose=False,
        )
    return _EMULATOR_OCR_READER

from utils import get_logger


class EmulatorBackend:
    kind = "emulator"
    #: 平台展示名（环境自报，供 system prompt 对齐语义；内核零硬编码）
    platform = "Android"

    def __init__(self, config: Optional[dict] = None):
        self.logger = get_logger("execution.emulator")
        self.config = config or {}
        rt = self.config.get("runtime", {})
        emu = rt.get("emulator", {})
        self.adb_serial = emu.get("adb_serial", "emulator-5554")
        res = emu.get("resolution") or self.config.get("execution", {}).get("screen_resolution")
        self.screen_width = (res or {}).get("width", 0)
        self.screen_height = (res or {}).get("height", 0)
        self._d = None  # u2 device handle
        self.logger.info(f"EmulatorBackend 初始化（serial={self.adb_serial}，未连接）")

    # --- 连接 ---------------------------------------------------------------
    def connect(self):
        """连接设备（幂等）。首次操作前或显式调用。"""
        if self._d is not None:
            return self._d
        import uiautomator2 as u2  # 懒导入：host 路径不依赖 u2
        self._d = u2.connect(self.adb_serial)
        # 默认命令超时放宽，避免复杂 UI 树拉取被掐
        try:
            self._d.set_new_command_timeout(60)
        except Exception:
            pass
        # 探测实际分辨率（归一化坐标用）
        try:
            w, h = self._d.window_size()
            self.screen_width, self.screen_height = int(w), int(h)
        except Exception as e:
            self.logger.warning(f"读取模拟器分辨率失败: {e}（使用配置值 {self.screen_width}x{self.screen_height}）")
        self.logger.info(f"已连接模拟器 {self.adb_serial} 分辨率 {self.screen_width}x{self.screen_height}")
        return self._d

    @property
    def d(self):
        if self._d is None:
            self.connect()
        return self._d

    # --- 键盘 ---------------------------------------------------------------
    def execute_keyboard_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        try:
            if action_type == "type":
                text = params.get("text", "")
                # IME 无关输入：切 fastinput 软键盘 -> 发文本 -> 切回
                self.d.set_fastinput_ime(True)
                self.d.send_keys(text)
                self.d.set_fastinput_ime(False)
                self.logger.info(f"模拟器输入文本: {text!r}")
            elif action_type == "press":
                key = params.get("key", "")
                self.d.press(key)
            elif action_type == "hotkey":
                keys = params.get("keys", [])
                if not isinstance(keys, list):
                    keys = [keys]
                for k in keys:
                    self.d.press(k)
            else:
                self.logger.warning(f"EmulatorBackend 未知键盘动作: {action_type}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"EmulatorBackend 键盘操作失败: {e}", exc_info=True)
            return False

    # --- 鼠标（指针/触摸）---------------------------------------------------
    def execute_mouse_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        try:
            x = params.get("x", 0)
            y = params.get("y", 0)
            if action_type in ("click", "double_click"):
                abs_x, abs_y = self.normalize_coordinate(x, y)
                if action_type == "click":
                    self.d.click(abs_x, abs_y)
                else:
                    self.d.double_click(abs_x, abs_y)
            elif action_type == "drag":
                from_x, from_y = params.get("from_x"), params.get("from_y")
                to_x = params.get("to_x", params.get("x_end", 0))
                to_y = params.get("to_y", params.get("y_end", 0))
                if from_x is None or from_y is None:
                    # 从当前点击位置起拖
                    sx, sy = self.normalize_coordinate(x, y)
                    ex, ey = self.normalize_coordinate(to_x, to_y)
                    self.d.drag(sx, sy, ex, ey)
                else:
                    sx, sy = self.normalize_coordinate(from_x, from_y)
                    ex, ey = self.normalize_coordinate(to_x, to_y)
                    self.d.drag(sx, sy, ex, ey)
            else:
                self.logger.warning(f"EmulatorBackend 未知鼠标动作: {action_type}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"EmulatorBackend 指针操作失败: {e}", exc_info=True)
            return False

    # --- 感知 ---------------------------------------------------------------
    def _hierarchy_texts(self, xml_str: str):
        """从 dump_hierarchy XML 抽取可见文本节点列表。"""
        texts = []
        try:
            root = ET.fromstring(xml_str)
            for node in root.iter("node"):
                t = (node.get("text") or "").strip()
                if t:
                    texts.append(t)
        except Exception:
            # 解析失败也返回原始串片段，避免完全丢失信息
            texts = re.findall(r'text="([^"]+)"', xml_str)
        return texts

    def observe(self) -> Dict[str, Any]:
        """当前屏幕快照：层级树文字 + 窗口名（快路径，原生 App 用）。

        不包含图像 OCR——GPU 渲染的界面文字需大脑主动调 ocr_screenshot 或 vision_describe。
        """
        try:
            xml = self.d.dump_hierarchy()
            texts = self._hierarchy_texts(xml)
            cur = self._current_app()
            return {
                "active_window": cur,
                "ocr_text": texts,
                "ui_tree": xml,
            }
        except Exception as e:
            return {"active_window": "", "ocr_text": [], "error": f"{type(e).__name__}: {e}"}

    def read_screen_text(self) -> Dict[str, Any]:
        """层级树文字快读（同 observe，快路径）。"""
        try:
            xml = self.d.dump_hierarchy()
            return {"ocr_text": self._hierarchy_texts(xml)}
        except Exception as e:
            return {"ocr_text": [], "error": f"{type(e).__name__}: {e}"}

    def ocr_screenshot(self) -> Dict[str, Any]:
        """图像 OCR：对当前屏幕跑 EasyOCR，返回文本列表（GPU，~0.7s 缓存后）。

        用于 GPU 渲染的界面/自定义 UI——文字不在层级树中，必须从截图识别。
        """
        try:
            import numpy as np
            from PIL import Image
            pil_img = self.d.screenshot(format="pillow")
            img = np.array(pil_img.convert("RGB"))
            reader = _get_ocr_reader()
            results = reader.readtext(img)
            texts = [r[1] for r in results if r[2] >= 0.5]
            return {"ocr_text": texts}
        except Exception as e:
            return {"ocr_text": [], "error": f"{type(e).__name__}: {e}"}

    def tap_text(self, text: str) -> Dict[str, Any]:
        """OCR 定位文字并点击其中心（EasyOCR 自带精确边界框）。

        第五轮真机暴露：VLM(som_marks) 给的坐标漂移大（列表项真实坐标与标注常有偏差，
        VLM 报 0.66 点空）。文字按钮用 OCR bbox 定位是确定性方案。
        """
        try:
            import numpy as np
            target = (text or "").strip()
            if not target:
                return {"ok": False, "error": "text 不能为空"}
            pil_img = self.d.screenshot(format="pillow")
            img = np.array(pil_img.convert("RGB"))
            reader = _get_ocr_reader()
            results = reader.readtext(img)
            best = None  # (bbox, matched_text, conf)，精确匹配优先于子串
            for bbox, t, conf in results:
                if conf < 0.4:
                    continue
                ts = t.strip()
                if ts == target:
                    best = (bbox, ts, conf)
                    break
                if best is None and (target in ts or ts in target):
                    best = (bbox, ts, conf)
            if best is None:
                seen = [r[1] for r in results if r[2] >= 0.4][:20]
                return {"ok": False, "error": f"屏幕未找到文字: {target}", "screen_texts": seen}
            bbox, matched, conf = best
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx, cy = sum(xs) / 4.0, sum(ys) / 4.0
            h, w = img.shape[:2]
            self.d.click(int(cx), int(cy))
            return {
                "ok": True,
                "matched": matched,
                "conf": round(float(conf), 3),
                "center": [round(cx / w, 3), round(cy / h, 3)],
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def tap_text_region(self, text: str, max_cy: float = 0.2, min_cy: float = 0.0,
                        min_conf: float = 0.4) -> Dict[str, Any]:
        """OCR 定位文字并点击，但只匹配中心 y 落在 [min_cy, max_cy]（归一化）区域的候选。

        用于避开与列表项同名的底部导航按钮：例如某列表顶部标签与底部导航按钮同名，
        普通 tap_text 可能误点底部导航。
        限定顶部区域即可稳定点到列表标签。
        """
        try:
            import numpy as np
            target = (text or "").strip()
            if not target:
                return {"ok": False, "error": "text 不能为空"}
            pil_img = self.d.screenshot(format="pillow")
            img = np.array(pil_img.convert("RGB"))
            reader = _get_ocr_reader()
            results = reader.readtext(img)
            h, w = img.shape[:2]
            best = None  # (bbox, matched_text, conf, cx, cy)
            for bbox, t, conf in results:
                if conf < min_conf:
                    continue
                ts = t.strip()
                # 精确匹配优先；否则子串
                if ts != target and target not in ts and ts not in target:
                    continue
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                cx, cy = sum(xs) / 4.0, sum(ys) / 4.0
                cy_n = cy / h
                if not (min_cy <= cy_n <= max_cy):
                    continue
                if best is None:
                    best = (bbox, ts, conf, cx, cy)
            if best is None:
                seen = [r[1] for r in results if r[2] >= min_conf][:20]
                return {"ok": False, "error": f"区域[{min_cy},{max_cy}]内未找到文字: {target}",
                        "screen_texts": seen}
            bbox, matched, conf, cx, cy = best
            self.d.click(int(cx), int(cy))
            return {
                "ok": True,
                "matched": matched,
                "conf": round(float(conf), 3),
                "center": [round(cx / w, 3), round(cy / h, 3)],
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _ocr_with_bbox(self, upscale: int = 1):
        """截图 + EasyOCR（带 bbox），返回 [(text, conf, cx, cy)]。

        upscale>1 时先对截图做整数倍放大再识别——列表项的小号数字/文字极小，
        原分辨率常漏检，2× 放大能显著找回细小文字与低对比条目名。
        """
        import numpy as np
        from PIL import Image
        pil_img = self.d.screenshot(format="pillow")
        img = np.array(pil_img.convert("RGB"))
        if upscale and upscale > 1:
            img = np.array(Image.fromarray(img).resize(
                (img.shape[1] * upscale, img.shape[0] * upscale), Image.BILINEAR))
        reader = _get_ocr_reader()
        out = []
        h, w = img.shape[:2]
        for bbox, t, conf in reader.readtext(img):
            if conf < 0.4:
                continue
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            out.append((t.strip(), float(conf), sum(xs) / 4.0, sum(ys) / 4.0, w, h))
        return out, w, h

    def collect_list(self, max_pages: int = 120, swipe_from_y: float = 0.82,
                     swipe_to_y: float = 0.18, swipe_x: float = 0.5,
                     wait_ms: int = 450) -> Dict[str, Any]:
        """一次性采集当前列表全部条目（纯文本），内部自动翻页去重。

        流程：截图→EasyOCR(bbox)→抽条目文本→上滑翻页→
        直到『连续两屏内容完全相同』（真正翻到底）或达 max_pages。返回全部采集到的条目
        （持久化由 tool_loop 接管）。

        关键：稳定停止条件是『本屏名字集合 == 上一屏名字集合』（整屏未移动=到底或卡住），
        而不是『无新名字』——后者会在翻页重叠时误判到底而提前结束（长列表漏采）。

        这是为『统计/列出某列表所有项』类任务设计的确定性采集宏：把"循环+翻页+去重"
        从 4B-vl 的不可靠循环里移到确定性代码，大幅提升枚举任务的可靠性。
        """
        try:
            seen: set = set()
            entries: List[Dict[str, Any]] = []
            pages = 0
            stable = 0
            last_names: set = set()
            while pages < max_pages:
                toks, w, h = self._ocr_with_bbox(upscale=1)
                # 候选条目（长度>=2 或含字母数字；不做领域词/结构过滤，噪声交给上层判断）
                cands = []  # (text, cy, cx)
                for text, conf, cx, cy, _, _ in toks:
                    if not text:
                        continue
                    if len(text) < 2 and not any(ch.isalnum() for ch in text):
                        continue
                    cands.append((text, cy, cx))
                cur_names = {name for name, _, _ in cands}
                # 去重累加（条目即纯文本，不解析等级/类型等场景结构）
                for name, _, _ in cands:
                    if name not in seen:
                        seen.add(name)
                        entries.append({"text": name})
                pages += 1
                # 稳定停止：本屏与上一屏名字集合完全相同（整屏未移动=到底/卡住）
                if cur_names and cur_names == last_names:
                    stable += 1
                    if stable >= 2:
                        # 疑似到底：再滑一次确认，防偶发滑动未生效导致的『假到底』提前结束
                        time.sleep(wait_ms / 1000.0)
                        self.d.drag(int(w * swipe_x), int(h * swipe_from_y),
                                    int(w * swipe_x), int(h * swipe_to_y))
                        time.sleep(wait_ms / 1000.0)
                        toks2, _, _ = self._ocr_with_bbox()
                        names2 = set()
                        for text, conf, cx, cy, _, _ in toks2:
                            if not text:
                                continue
                                continue
                            if len(text) < 2 and not any(ch.isalnum() for ch in text):
                                continue
                            names2.add(text)
                        if names2 and names2 != cur_names:
                            stable = 0
                            last_names = names2
                            continue
                        break
                else:
                    stable = 0
                last_names = cur_names
                # 翻页：上滑（手指从下往上）→ 列表向下滚动露出后续条目
                time.sleep(wait_ms / 1000.0)
                self.d.drag(int(w * swipe_x), int(h * swipe_from_y),
                            int(w * swipe_x), int(h * swipe_to_y))
                time.sleep(wait_ms / 1000.0)
            return {"ok": True, "entries": entries, "total": len(entries), "pages": pages}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "entries": [], "total": 0}

    def _current_app(self) -> str:
        try:
            info = self.d.app_current()
            return info.get("package") or info.get("activity") or str(info)
        except Exception:
            return ""

    # --- 截图 ---------------------------------------------------------------
    def screenshot(self, save_path: Optional[str] = None) -> Dict[str, Any]:
        try:
            if not save_path:
                from omni_core.tools.workspace import task_tmp_dir
                save_path = str(task_tmp_dir() / "emulator_screenshot.png")
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            self.d.screenshot(save_path)
            return {"ok": True, "path": save_path}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # --- 结构化 UI 树 -------------------------------------------------------
    def get_ui_tree(self) -> Dict[str, Any]:
        try:
            xml = self.d.dump_hierarchy()
            return {"ok": True, "ui_tree": xml, "node_count": xml.count("<node")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # --- 控件 / 应用 --------------------------------------------------------
    def tap_by_id(self, resource_id: str) -> Dict[str, Any]:
        try:
            el = self.d(resourceId=resource_id)
            if not el.exists:
                return {"ok": False, "error": f"控件不存在: {resource_id}"}
            el.click()
            return {"ok": True, "resource_id": resource_id}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def launch_app(self, package: str) -> Dict[str, Any]:
        try:
            self.d.app_start(package)
            return {"ok": True, "package": package}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def press_keycode(self, code: int) -> Dict[str, Any]:
        try:
            self.d.press(int(code))
            return {"ok": True, "code": int(code)}
        except Exception as e:
            # 退回 shell input keyevent
            try:
                self.d.shell(f"input keyevent {int(code)}")
                return {"ok": True, "code": int(code), "via": "shell"}
            except Exception as e2:
                return {"ok": False, "error": f"{type(e2).__name__}: {e2}"}

    # --- 设备侧 shell / 文件（adb；基于 u2 设备句柄，零额外 adb 路径配置）------
    def device_shell(self, command: str) -> Dict[str, Any]:
        """在设备上执行 shell 命令（``adb shell`` 语义，**不是宿主机命令**）。"""
        cmd = str(command or "").strip()
        if not cmd:
            return {"ok": False, "error": "command 不能为空"}
        try:
            res = self.d.shell(cmd)
            out, code = self._shell_result(res)
            return {"ok": True, "output": out, "exit_code": code}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def device_push(self, local_path: str, remote_path: str) -> Dict[str, Any]:
        """把**宿主机**文件推到设备（``local_path`` 相对路径基准 = 当前任务临时目录）。"""
        try:
            from omni_core.tools.workspace import resolve_path
            src = resolve_path(local_path)
            if not src.is_file():
                return {"ok": False, "error": f"本地文件不存在: {local_path}"}
            self.d.push(str(src), str(remote_path))
            return {"ok": True, "local": str(src), "remote": str(remote_path)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def device_pull(self, remote_path: str, local_path: str = "") -> Dict[str, Any]:
        """把设备文件拉到**宿主机**（缺省落当前任务临时目录，保留原文件名）。"""
        remote = str(remote_path or "").strip()
        if not remote:
            return {"ok": False, "error": "remote_path 不能为空"}
        try:
            from omni_core.tools.workspace import resolve_path, task_tmp_dir
            dst = resolve_path(local_path) if local_path else (task_tmp_dir() / os.path.basename(remote))
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.d.pull(remote, str(dst))
            return {"ok": True, "remote": remote, "local": str(dst)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def device_list_dir(self, remote_path: str = "/sdcard") -> Dict[str, Any]:
        """列举设备目录（``ls -1``；返回条目名列表）。"""
        remote = str(remote_path or "").strip() or "/sdcard"
        try:
            import shlex
            res = self.d.shell(f"ls -1 {shlex.quote(remote)}")
            out, _code = self._shell_result(res)
            items = [ln.strip() for ln in str(out).splitlines() if ln.strip()]
            if not items:
                return {"ok": False, "error": f"目录不存在或为空: {remote}", "path": remote}
            return {"ok": True, "path": remote, "count": len(items), "items": items}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _shell_result(res: Any) -> tuple:
        """归一 ``u2`` 的 shell 返回值（新版带 ``.output``/``.exit_code``，旧版为字符串）。"""
        if isinstance(res, (tuple, list)) and len(res) == 2:
            return str(res[0]), res[1]
        out = getattr(res, "output", None)
        if out is None:
            return str(res), None
        return str(out), getattr(res, "exit_code", None)

    # --- 去场景化（§9）：感知文本化 / 完成判定，emulator 以 OCR 为主 ----------
    def text_of(self, percept: Dict[str, Any]) -> str:
        """emulator 感知文本化：以 ocr_text（层级树/图像 OCR 文字）为主，
        保留 active_window 上下文。"""
        if not isinstance(percept, dict):
            return str(percept)
        parts = []
        if percept.get("active_window"):
            parts.append(f"[窗口] {percept['active_window']}")
        ocr = percept.get("ocr_text")
        if ocr:
            ocr_list = ocr if isinstance(ocr, list) else [str(ocr)]
            parts.append("[文字] " + " / ".join(ocr_list))
        return "\n".join(parts)

    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple:
        """复刻原 tool_loop._verify 的 OCR 完成判定（行为不变）：
        condition 按 '|' 拆多候选，逐个在 ocr_text 列表中做元素命中。"""
        if not condition:
            return False, "无可校验条件"
        ocr_text = percept.get("ocr_text") or []
        if isinstance(ocr_text, str):
            ocr_text = [ocr_text]
        candidates = [c.strip() for c in str(condition).split("|") if c.strip()]
        for c in candidates:
            if c in ocr_text:
                return True, f"OCR 命中条件: {c}"
        return False, f"未在屏幕找到完成条件: {condition}"

    # 注：M4 起本后端不再声明 tool_schemas —— 能力暴露的唯一来源是
    # agent 外层 tool 插件层（omni_core/tools/device_tool.py）。
