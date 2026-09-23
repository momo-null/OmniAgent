"""运行时 REST API 的输入模型与边界校验。"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omni_core.local import runtime_paths as paths


class ChatMessage(BaseModel):
    """一条对话历史消息。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "agent"]
    content: str = Field(min_length=1, max_length=16000)


class ChatRequest(BaseModel):
    """统一 chat 入口的请求体。"""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    task_id: Optional[str] = None
    max_steps: int = Field(default=40, ge=1, le=2000)
    full_access: Optional[bool] = None

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: Optional[str]) -> Optional[str]:
        """只允许安全的既有任务 ID。"""
        return paths.validate_identifier(value, "task_id") if value else None


class CreateTaskRequest(BaseModel):
    """创建尚未执行的任务。"""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    done_when: str = Field(default="", max_length=2000)
    project_id: Optional[str] = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: Optional[str]) -> Optional[str]:
        """验证可选项目 ID。"""
        return paths.validate_identifier(value, "project_id") if value else None


class UpdateTaskRequest(BaseModel):
    """允许用户修改的任务字段。"""

    model_config = ConfigDict(extra="forbid")

    objective: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    done_when: Optional[str] = Field(default=None, max_length=2000)
    state: Optional[Literal["pending", "running", "done", "failed", "aborted"]] = None
    full_access: Optional[bool] = None


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
