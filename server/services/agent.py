"""Graph adapter: public events, durable commit and conservative checkpoint repair."""

from copy import deepcopy
import json

from server.classes.api import ServiceError
from utils.daily_logger import get_logger, log_event

logger = get_logger("server.agent")

_PHASES = {"begin_turn": "preparing", "draft": "replying", "check": "reviewing",
           "world_state_update": "updating_state", "participant_state_in": "updating_state",
           "participant_state_out": "updating_state", "prepare_memory": "updating_memory"}
_STATE_FIELDS = {"world_state", "character_state", "user_state"}


def public_state(values):
    def string(value):
        return value if isinstance(value, str) else None
    result = {}
    for name, keys in (("character_state", ("location", "mood", "body", "clothing", "hearing")),
                       ("user_state", ("location", "mood", "body", "clothing"))):
        if name in values:
            source = values[name] if isinstance(values[name], dict) else {}
            result[name] = {key: string(source.get(key)) for key in keys}
    if "world_state" in values:
        # Project the graph's stored time. The shared preparation helper now
        # advances to wall-clock time, which would rewrite history during repair.
        world = values["world_state"] if isinstance(values["world_state"], dict) else {}
        time = world.get("time") if isinstance(world.get("time"), dict) else {}
        result["world_state"] = {"weather": string(world.get("weather")),
                                 "time": {key: string(time.get(key))
                                          for key in ("date", "weekday", "period")}}
    return result


def freeze_state(values):
    from langchain_core.messages import messages_to_dict
    result = dict(values)
    result["messages"] = messages_to_dict(result.get("messages", []))
    return json.loads(json.dumps(result, default=str))


def thaw_state(values):
    from langchain_core.messages import messages_from_dict
    result = deepcopy(values or {})
    result["messages"] = messages_from_dict(result.get("messages", []))
    return result


def _input(run):
    from langchain_core.messages import HumanMessage
    return HumanMessage(id=f"user_{run['user_message_id']}",
                        content=f"<timestamp>{run['input_created_at'].isoformat()}</timestamp>\n" + run["text"])


class AgentAdapter:
    def __init__(self, pool, checkpointer, catalog, models, *, builder=None, stopping=None):
        from agent.builder import build_rp_agent
        self.pool, self.checkpointer = pool, checkpointer
        self.catalog, self.models = catalog, models
        self.builder = builder or build_rp_agent
        self.stopping = stopping

    async def graph(self, run, **kwargs):
        return await self.builder(run["character_id"], pool=self.pool, checkpointer=self.checkpointer, **kwargs)

    async def repair(self, run, repo):
        """Never invoke a pending graph. Preserve known output and discard pending tasks."""
        from langchain_core.messages import AIMessage, RemoveMessage
        from langgraph.graph import END
        from langgraph.graph.message import REMOVE_ALL_MESSAGES
        graph = await self.graph(run, recovery_only=True)
        config = {"configurable": {"thread_id": run["graph_thread_id"]}}
        current = await graph.aget_state(config)
        values = dict(current.values or {})
        base = thaw_state(run["base_state"])
        snapshot = await repo.snapshot(run["id"])
        committed = next((m for m in snapshot["messages"] if m["role"] == "assistant"), None)
        expected = f"reply_{run['id']}"
        graph_reply = next((m for m in values.get("messages", []) if getattr(m, "id", None) == expected), None)
        # Defensive recovery of a checkpoint written by a previous adapter version.
        if committed is None and graph_reply is not None and run["status"] == "running":
            content = graph_reply.content
            if not isinstance(content, str) or not content.startswith("<timestamp>") or "</timestamp>\n" not in content:
                raise ServiceError("checkpoint_conflict", "检查点中的回复格式无法核对。", 409)
            text = content.split("</timestamp>\n", 1)[1]
            committed = await repo.commit_reply(run["id"], text, expected)
        if run["base_state"] is None:
            # The worker had not yet entered the graph; preserve the existing checkpoint.
            await repo.clear_recovery(run["thread_id"])
            return
        safe = base
        if committed:
            safe = values if values.get("service_run_id") == str(run["id"]) else base
            safe = dict(safe)
            messages = list(safe.get("messages", []))
            if not any(m.id == f"user_{run['user_message_id']}" for m in messages):
                messages.append(_input(run))
            if not any(m.id == expected for m in messages):
                messages.append(AIMessage(id=expected, content=f"<timestamp>{committed['created_at'].isoformat()}</timestamp>\n" + committed["text"]))
            safe["messages"] = messages
            if not safe.get("service_reply_counted"):
                safe["iteration"] = base.get("iteration", 0) + 1
                safe["service_reply_counted"] = True
        # Reset transient channels as well as messages, so partial draft/tool tasks cannot resume.
        reset = {"draft_reply": "", "draft_status": "committed" if committed else "pending",
                 "draft_reasoning": "", "draft_usage": None, "check_feedback": "", "check_rounds": 0,
                 "check_status": "pending", "check_issues": [], "check_reply": "", "reply_error": "",
                 "retry_message_id": "", "service_run_id": "", "turn_id": "",
                 "need_tts": False, "need_event_judge": True,
                 "memory_pending_job": None, "memory_active_job": None, "memory_warning": "",
                 "memory_submitted_through": "", "memory_processed_through": "",
                 "memory_processed_fingerprint": "", "memory_trimmed_through": "", "memory_last_applied_job": 0,
                 "world_state": {}, "character_state": {}, "user_state": {}, "iteration": 0}
        reset.update(safe)
        reset.update(draft_reply="", draft_reasoning="", draft_status="committed" if committed else "pending", check_reply="",
                     check_feedback="", reply_error="", service_run_id="", retry_message_id="",
                     messages=[RemoveMessage(id=REMOVE_ALL_MESSAGES), *safe.get("messages", [])])
        await graph.aupdate_state(config, None, as_node=END)
        await graph.aupdate_state(config, reset, as_node="reply_failed")
        if (await graph.aget_state(config)).next:
            raise RuntimeError("Checkpoint repair left pending tasks")
        async with repo.connection() as conn:
            from psycopg.types.json import Jsonb
            await conn.execute("UPDATE mybot_ui.threads SET state = state || %s WHERE id = %s",
                               (Jsonb(public_state(reset)), run["thread_id"]))
        await repo.clear_recovery(run["thread_id"])
        log_event(logger, "检查点修复完成", run_id=run["id"], thread_id=run["thread_id"],
                  reply_preserved=committed is not None)

    async def execute(self, run, repo):
        from config.model_config import model_config_scope
        data, model_version = self.models.snapshot()
        profile = self.catalog.get(run["character_id"])
        text = profile.profiles.get("zh") or next(iter(profile.profiles.values()))

        async def before(name, state):
            if self.stopping is not None and self.stopping.is_set():
                import asyncio
                raise asyncio.CancelledError()
            # Also detect loss of the advisory-lock connection before every node.
            async with repo.connection() as conn:
                await conn.execute("SELECT 1")
            phase = _PHASES.get(name)
            if name == "tools":
                messages = state.get("messages", [])
                calls = getattr(messages[-1], "tool_calls", []) if messages else []
                phase = "recalling" if any(c.get("name") == "memory_query" for c in calls) else "using_tools"
            if phase:
                await repo.publish(run["id"], "phase", {"phase": phase})

        async def commit(state, output):
            message = output["messages"][0]
            if message.id != f"reply_{run['id']}":
                raise RuntimeError("Unexpected graph reply ID")
            await repo.commit_reply(run["id"], state["draft_reply"].strip(), message.id)

        with model_config_scope(data):
            async def memory_source(state):
                return await repo.memory_messages(run['thread_id'], run['memory_policy_version'], state.get('memory_processed_through'))
            graph = await self.graph(run, character_profile=text, before_node=before, on_commit=commit,
                                     memory_retrieval_enabled=run['memory_retrieval_enabled'],
                                     memory_storage_enabled=run['memory_storage_enabled'], memory_source=memory_source)
            config = {"configurable": {"thread_id": run["graph_thread_id"]}, "recursion_limit": 200}
            if run["recovery_run_id"]:
                previous = await repo.recovery_context(run["recovery_run_id"])
                await self.repair(previous, repo)
            base = (await graph.aget_state(config)).values or {}
            if not base and run.get('import_state'):
                await graph.aupdate_state(config, thaw_state(run['import_state']), as_node='reply_failed')
                base = (await graph.aget_state(config)).values or {}
            if run["retry_of"] and run["base_state"] is not None:
                # Retries are only allowed for the most recent failed run.
                base = thaw_state(run["base_state"])
            await repo.save_base(run["id"], freeze_state(base), model_version, profile.version)
            run["base_state"] = freeze_state(base)
            await repo.publish(run["id"], "state.updated", {"retrieved_memories": []})
            inputs = {"messages": [_input(run)], "service_run_id": str(run["id"]),
                      "need_tts": False, "need_event_judge": run['memory_storage_enabled'],
                      'memory_retrieval_enabled': run['memory_retrieval_enabled'],
                      'memory_storage_enabled': run['memory_storage_enabled'],
                      'memory_policy_version': run['memory_policy_version']}
            if base.get('memory_policy_version', 1) != run['memory_policy_version']:
                async with repo.connection() as conn:
                    exists = await (await conn.execute("SELECT to_regclass('memory_service.results') AS name")).fetchone()
                    last = await (await conn.execute('SELECT COALESCE(MAX(job_id), 0) AS id FROM memory_service.results WHERE character_name = %s AND thread_id = %s',
                                                    (run['character_id'], run['graph_thread_id']))).fetchone() if exists['name'] else {'id': 0}
                inputs.update(memory_pending_job=None, memory_active_job=None, memory_warning='',
                              memory_processed_through='', memory_processed_fingerprint='', memory_submitted_through='',
                              memory_trimmed_through='', memory_last_applied_job=last['id'], iteration=0)
            if not run['memory_storage_enabled']:
                inputs.update(memory_pending_job=None, memory_active_job=None, memory_warning='')
            async for update in graph.astream(inputs, config, stream_mode="updates", durability="sync"):
                for name, delta in update.items():
                    if not isinstance(delta, dict):
                        continue
                    state = public_state(delta)
                    if state:
                        await repo.publish(run["id"], "state.updated", state)
                    if name == "tools":
                        from agent.classes.memory import MemorySearchResult
                        for message in delta.get("messages", []):
                            if getattr(message, "name", None) != "memory_query":
                                continue
                            try:
                                result = MemorySearchResult.model_validate_json(message.content)
                            except (ValueError, TypeError):
                                continue
                            if result.status in ("ok", "empty"):
                                await repo.publish(run["id"], "memory.retrieved", {"hits": [
                                    {"id": str(hit.memory_id), "memory": hit.text, "event_date": hit.event_date,
                                     "update_time": hit.update_time} for hit in result.hits]})
            final = (await graph.aget_state(config)).values or {}
            error = "reply_failed" if final.get("reply_error") else None
            if error:
                await self.repair(run, repo)
            await repo.finish(run["id"], error=error,
                              warning="memory_delayed" if final.get("memory_warning") else None)
