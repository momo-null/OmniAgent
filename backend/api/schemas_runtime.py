"""运行时 REST API 的输入模型与边界校验。"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omni_core.local import runtime_paths as paths


class ChatMessage(BaseModel):
    """一条对话历史消息。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "agent"]
    # 允许空 content：会话历史中存在「只有工具步骤、无口播文本」的合法记录
    # （增量 partial 落盘 content=""，见 chat_runtime._maybe_flush_partial）。
    # 空文本在注入模型历史时会被过滤（run_subtask_sdk 的 `if _c`），对模型无害；
    # 此处若强制 min_length=1 会让携带该类历史的会话请求整体 422。
    content: str = Field(default="", max_length=16000)


class ChatRequest(BaseModel):
    """统一 chat 入口的请求体。"""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    task_id: Optional[str] = None
    max_steps: int = Field(default=40, ge=1, le=2000)
    full_access: Optional[bool] = None
    # 模型路由（2026-09-29）：本次请求使用的模型选择，形如 "<provider_id>/<model_id>"。
    # 只接受该形状（不含 URL/空格），是否真实存在由 router 白名单二次校验。
    model: Optional[str] = Field(default=None, max_length=200,
                                 pattern=r"^[^\s/]+/[^\s/].*$")

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: Optional[str]) -> Optional[str]:
        """只允许安全的既有任务 ID。"""
        return paths.validate_identifier(value, "task_id") if value else None


class CreateTaskRequest(BaseModel):
    """创建尚未执行的任务。

    ``objective`` 允许为空：项目菜单「新建会话」先落实体（挂到项目下），
    首条消息在 ``/chat`` 里自动回填命名（与首消息建任务行为对齐）。
    """

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(default="", max_length=4000)
    done_when: str = Field(default="", max_length=2000)
    project_id: Optional[str] = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: Optional[str]) -> Optional[str]:
        """验证可选项目 ID。"""
        return paths.validate_identifier(value, "project_id") if value else None


class UpdateTaskRequest(BaseModel):
    """允许用户修改的任务字段。

    C1（knowledge-layering）：补 ``project_id``——内核白名单本就允许，此前 schema
    缺字段 + ``extra="forbid"`` 把「任务保存到项目」的 REST 通道堵死（422）。
    """

    model_config = ConfigDict(extra="forbid")

    objective: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    done_when: Optional[str] = Field(default=None, max_length=2000)
    state: Optional[Literal["pending", "running", "done", "failed", "aborted"]] = None
    full_access: Optional[bool] = None
    project_id: Optional[str] = None

    @field_validator("project_id")
    @classmethod
    def validate_update_project_id(cls, value: Optional[str]) -> Optional[str]:
        """验证可选项目 ID（与创建任务同规则）。"""
        return paths.validate_identifier(value, "project_id") if value else None


class CreateProjectRequest(BaseModel):
    """创建项目（C2）：slug 即唯一 id；显示名（别名）存项目元数据文件。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=80)
    display_name: Optional[str] = Field(default=None, max_length=120)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        """验证项目 ID。"""
        return paths.validate_identifier(value, "project_id")


class ProjectAliasRequest(BaseModel):
    """改项目显示名（C3）：别名不进目录名，slug 仍是唯一 id。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)


class SkillRequest(BaseModel):
    """技能执行或删除请求。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    skill_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        """验证任务 ID。"""
        return paths.validate_identifier(value, "task_id")
