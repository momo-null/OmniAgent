"""执行后端抽象基类（ExecutionBackend）。

设计目标（见 Master Spec §3）：把「模型调用的抽象工具」与「具体设备控制」解耦。
- 模型（大脑或本地执行器）永远只调 backend 中立的工具（type/click/observe/...）。
- HostBackend（本机 pyautogui）与 EmulatorBackend（Android uiautomator2/ADB）
  各自把同一套抽象工具翻译成自身设备的原语。
- tool-loop 通过 config.runtime.backend 选后端，模型侧 schema 也随之切换，
  换设备/换后端不需要改模型或 tool-loop 逻辑。

抽象工具集（方法）：
- execute_keyboard_action(action_type, params)  -> press/hotkey/type
- execute_mouse_action(action_type, params)     -> click/double_click/drag（屏幕归一化坐标）
- observe() / read_screen_text()                -> 感知（屏幕摘要 / OCR 文本）
- screenshot(save_path=None)                    -> 截图像素（喂 VLM / 大脑 vision）
- get_ui_tree()                                -> 结构化 UI 层级（dump_hierarchy，文本化决策）
- tap_by_id(resource_id)                        -> 按控件 id 可靠点击
- launch_app(package) / press_keycode(code)     -> 启动应用 / 按键码
- normalize_coordinate(x, y)                    -> 0~1 相对坐标 -> 设备绝对像素

M4 变更：本文件连同两个实现类已从内核 `omni_core/` 迁到顶层 `devices/` 包（L1 能力层）。
原 `tool_schemas @property`（向内核下发的工具 schema 清单）已删除——能力暴露的
唯一来源是 agent 外层 tool 插件层（`omni_core/tools/device_tool.py`）。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable


class ExecutionBackend(ABC):
    """所有执行后端的抽象基类。"""

    #: 后端标识，用于配置选路与日志
    name: str = "base"

    #: 设备逻辑分辨率（归一化坐标的目标尺寸），子类构造时填充
    screen_width: int = 0
    screen_height: int = 0

    # ------------------------------------------------------------------
    # 坐标归一化（子类可覆盖）
    # ------------------------------------------------------------------
    def normalize_coordinate(self, x: float, y: float) -> Tuple[int, int]:
        """0~1 相对坐标 -> 设备绝对像素。

        已为绝对坐标（超出 0~1 范围）则原样取整返回。
        """
        if 0 <= x <= 1 and 0 <= y <= 1 and self.screen_width and self.screen_height:
            return int(x * self.screen_width), int(y * self.screen_height)
        return int(round(x)), int(round(y))

    # ------------------------------------------------------------------
    # 执行原语（必须实现）
    # ------------------------------------------------------------------
    @abstractmethod
    def execute_keyboard_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        """键盘类：type / press / hotkey。"""

    @abstractmethod
    def execute_mouse_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        """指针类：click / double_click / drag（坐标经 normalize_coordinate）。"""

    @abstractmethod
    def observe(self) -> Dict[str, Any]:
        """当前屏幕摘要：返回可序列化 dict（如 active_window / ocr_text / ui_tree）。"""

    @abstractmethod
    def read_screen_text(self) -> Dict[str, Any]:
        """主动重新识别屏幕文字，返回 {"ocr_text": [...]}。"""

    # ------------------------------------------------------------------
    # 感知 / emulator 增强原语（有默认实现，host 可返回不支持）
    # ------------------------------------------------------------------
    def screenshot(self, save_path: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": False, "error": f"screenshot 不支持于后端 {self.name}"}

    def get_ui_tree(self) -> Dict[str, Any]:
        return {"ok": False, "error": f"get_ui_tree 不支持于后端 {self.name}"}

    def tap_by_id(self, resource_id: str) -> Dict[str, Any]:
        return {"ok": False, "error": f"tap_by_id 不支持于后端 {self.name}"}

    def launch_app(self, package: str) -> Dict[str, Any]:
        return {"ok": False, "error": f"launch_app 不支持于后端 {self.name}"}

    def press_keycode(self, code: int) -> Dict[str, Any]:
        return {"ok": False, "error": f"press_keycode 不支持于后端 {self.name}"}

    def wait(self, ms: int) -> Dict[str, Any]:
        """等待（后端中立，默认纯 sleep；子类可覆盖为设备同步等待）。"""
        import time
        time.sleep(max(0, int(ms)) / 1000.0)
        return {"ok": True, "waited_ms": int(ms)}

    # ------------------------------------------------------------------
    # 工具 schema —— M4 起**不再由此处声明**：
    # 能力暴露的唯一来源是 agent 外层 tool 插件层（omni_core/tools/device_tool.py），
    # 设备层只负责执行；历史上基类在此返回全局/按后端的 schema 清单，
    # 形成「设备 → 内核」的反向依赖，已删除。
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 通用感知/完成判定（去场景化 §9：内核只认 percept，不规定字段）
    # 强制后端实现：内核不预设任何感知形状（屏幕/结构化/终端…），
    # 由 backend 自己把 percept 文本化、自己定义完成判定。
    # ------------------------------------------------------------------
    @abstractmethod
    def text_of(self, percept: Dict[str, Any]) -> str:
        """把后端产出的 percept 转纯文本，供内核做完成判定/世界模型摘要。

        内核（tool_loop / WorldModel）只调此方法，永不读取 percept 内部字段。
        屏幕 backend 拼 ocr/ui_tree；结构化 backend 拼领域状态；coding 后端
        返回终端 stdout——形状完全由 backend 决定。
        """

    @abstractmethod
    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple:
        """通用完成判定：把 condition 与 text_of(percept) 比对。

        返回 (passed: bool, reason: str)。内核（tool_loop._verify）只调此方法，
        不直接读任何感知字段。各后端可自行决定命中语义（子串 / 列表元素 / 正则）。
        """


@runtime_checkable
class ExecutionModuleProtocol(Protocol):
    """执行模块契约（阶段 0.5 显式化；结构型，具体类 / 测试替身都满足即可）。

    内核只通过此契约使用执行模块：
    - ``backend_kind``：后端标识（"host" / "emulator"），内核据此收窄能力组；
    - ``text_of`` / ``verify_done``：去场景化 §9 完成判定抽象入口（由 backend 自定语义）；
    - 其余原语（observe / screenshot / tap_by_id …）按 agent 外层 tool 插件层调用。

    具体实现是 ``devices.factory.ExecutionModule``（按配置选后端 + 委派的薄壳）；
    测试替身（_FakeBackend 等）只需实现本契约即可注入内核。
    """

    backend_kind: str

    def text_of(self, percept: Dict[str, Any]) -> str: ...

    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple: ...
