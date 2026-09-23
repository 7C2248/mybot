"""LangGraph 全图共享状态；运行时默认值由节点读取或轮初重置提供。"""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]     # llm对话上下文，add_messages 是 reducer 函数
    iteration: int                              # 当前对话迭代次数（用于控制循环）
    need_event_judge: bool              # 事件判断开关，外部参数
    need_tts: bool                      # 语音生成开关，外部参数
    world_state: dict                           # 外部世界状态（系统时间、和风天气实时天气）
    character_state: dict                       # 角色状态（位置、情绪、身体、穿着、听觉范围）
    user_state: dict                            # 用户状态（位置、情绪、身体、穿着）

    # ---- RP 候选回复与独立检查（轮初重置） ----
    turn_id: str
    service_run_id: str                        # UI runs provide a stable turn ID; CLI leaves this empty
    service_reply_counted: bool                # Recovery must not undo a post-memory iteration reset
    draft_reply: str                            # 尚未提交的候选回复
    draft_reasoning: str                        # 候选回复对应的模型思维链
    draft_status: str
    draft_usage: dict | None                    # 候选回复最后生成调用的 token 用量
    check_feedback: str                         # check -> draft 修订意见
    check_rounds: int                           # 内容检查轮数
    check_status: str
    check_issues: list[dict]
    check_reply: str                            # 实际通过检查的文本
    reply_error: str
    retry_message_id: str                      # 失败后保留的用户消息 ID，用于重试去重

    # ---- 后台记忆交接：轮初不重置，和消息裁剪共同 checkpoint ----
    memory_pending_job: dict | None            # 尚未成功入队的不可变快照
    memory_active_job: int | None              # 已入队、尚未同步结果的任务
    memory_submitted_through: str              # 已成功投递到的消息 ID
    memory_processed_through: str              # 已完成整理到的消息 ID
    memory_processed_fingerprint: str
    memory_trimmed_through: str                # 实际裁剪到的消息 ID
    memory_last_applied_job: int               # 与 RemoveMessage 一起保存的结果确认
    memory_warning: str                        # 后台异常不变成本轮 reply_error
    memory_retrieval_enabled: bool
    memory_storage_enabled: bool
    memory_policy_version: int
