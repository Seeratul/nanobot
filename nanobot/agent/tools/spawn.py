"""Native subagent spawning with optional model profiles and bounded nesting."""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import (
    BooleanSchema, NumberSchema, StringSchema, tool_parameters_schema,
)
from nanobot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import ToolContext


class SpawnTool(Tool):
    """Root calls can run in the background; child calls return to their parent."""

    def __init__(self, manager: "SubagentManager", parent_task_id: str | None = None):
        self._manager = manager
        self._parent_task_id = parent_task_id

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        manager = ctx.subagent_manager
        if manager is None:
            raise RuntimeError("SpawnTool requires an initialized subagent manager")
        return cls(manager=manager)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def parameters(self) -> dict[str, Any]:
        fields = dict(
            task=StringSchema("The bounded task for the subagent to complete"),
            label=StringSchema("Optional short display label"),
            temperature=NumberSchema(
                description="Optional temperature; defaults to the selected preset.",
                minimum=0.0, maximum=2.0,
            ),
            wait=BooleanSchema(
                description=(
                    "Return the result directly rather than in the background. "
                    "Nested calls always return directly to their parent, even if false."
                ),
                default=False,
            ),
        )
        required = ["task"]
        if self._manager.subagent_profiles:
            fields["model_preset"] = StringSchema(
                "Choose an explicitly allowed native subagent preset; shared limits apply.",
                enum=self._manager.allowed_spawn_presets(self._parent_task_id),
            )
            required.append("model_preset")
        return tool_parameters_schema(**fields, required=required)

    @property
    def description(self) -> str:
        text = (
            "Spawn a native subagent for a bounded task. "
            "Use wait=true when its result must inform this turn. "
            "Inspect existing projects first; use a dedicated subdirectory when helpful. "
        )
        if self._manager.subagent_profiles:
            limits = ", ".join(
                f"{name}: {self._manager.subagent_profiles[name].max_concurrent}"
                for name in self._manager.allowed_spawn_presets(self._parent_task_id)
            )
            text += "Select model_preset. Shared concurrent limits: " + limits + ". Excess work queues. "
        if self._parent_task_id is not None:
            text += (
                "Emit independent spawn calls together in one tool-call batch to run "
                "them concurrently. Child results return inline only to you, in stable "
                "call order; they are not sent to the main chat."
            )
        else:
            text += "Background results return to the originating chat/session."
        return text

    @property
    def concurrency_safe(self) -> bool:
        return True

    async def execute(
        self,
        task: str,
        label: str | None = None,
        temperature: float | None = None,
        wait: bool = False,
        model_preset: str | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.agent.subagent import SubagentPolicyError

        request_ctx = current_request_context()
        if request_ctx is None or request_ctx.runtime is None:
            return ToolResult.error("Error: spawn requires an active model runtime")
        actual_parent = request_ctx.attributes.get("subagent_task_id")
        if actual_parent != self._parent_task_id:
            return ToolResult.error("Error: spawn parent capability does not match this execution")
        origin_channel = request_ctx.channel
        origin_chat_id = request_ctx.chat_id
        session_key = request_ctx.session_key or f"{origin_channel}:{origin_chat_id}"
        method = self._manager.run_inline if wait or actual_parent is not None else self._manager.spawn
        try:
            return await method(
                task=task,
                runtime=request_ctx.runtime,
                model_preset=model_preset,
                parent_task_id=actual_parent,
                label=label,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                session_key=session_key,
                origin_message_id=request_ctx.message_id,
                temperature=temperature,
                workspace_scope=current_workspace_scope(),
            )
        except SubagentPolicyError as error:
            return ToolResult.error("Error: " + str(error))
