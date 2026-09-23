"""Saved and active model settings have separate versions and explicit activation."""

from copy import deepcopy
import hashlib
import threading

import yaml

from server.classes.api import ServiceError
from server.classes.settings import PUBLIC_NODES, NodeModelSettings
from server.services.files import atomic_write


class ModelSettingsService:
    def __init__(self, settings, gate):
        self.settings = settings
        self.gate = gate
        self.memory_gate = threading.Lock()
        self.lock = threading.RLock()
        self.active = None
        self.active_version = None
        try:
            self.active, self.active_version = self._read()
        except ServiceError:
            pass

    def _read(self):
        try:
            raw = self.settings.model_config.read_bytes()
            data = yaml.safe_load(raw)
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
                raise ValueError()
            if not isinstance(data.get("defaults", {}), dict):
                raise ValueError()
            return data, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, yaml.YAMLError):
            raise ServiceError("model_config_unavailable", "模型配置文件缺失或格式无效。", 503) from None

    def snapshot(self):
        with self.lock:
            if self.active is None:
                raise ServiceError("model_config_unavailable", "模型配置尚未就绪。", 503)
            return deepcopy(self.active), self.active_version

    def get(self):
        with self.lock:
            data, version = self._read()
            nodes = {}
            for name in sorted(PUBLIC_NODES & data["nodes"].keys()):
                merged = dict(data.get("defaults") or {}) | (data["nodes"][name] or {})
                public = {key: merged[key] for key in NodeModelSettings.model_fields if key in merged}
                try:
                    nodes[name] = NodeModelSettings.model_validate(public).model_dump()
                except ValueError:
                    # Existing custom/local options are retained on disk, never echoed.
                    continue
            return {"saved_version": version, "active_version": self.active_version,
                    "pending_changes": version != self.active_version, "nodes": nodes,
                    "effective_from": "next_run", "runtime_busy": self.gate.locked() or self.memory_gate.locked()}

    def save(self, request):
        with self.lock:
            data, version = self._read()
            if version != request.expected_version:
                raise ServiceError("version_conflict", "模型配置已变化，请重新读取后保存。", 409)
            for name, node in request.nodes.items():
                previous = data["nodes"].get(name) or {}
                if not isinstance(previous, dict):
                    raise ServiceError("model_config_unavailable", "模型节点配置格式无效。", 409)
                if node.provider == "llama_cpp" and not previous.get("model_path"):
                    raise ServiceError("local_model_not_configured", "请先在本机配置该节点的本地模型路径。", 409)
                data["nodes"][name] = previous | node.model_dump()
            atomic_write(self.settings.model_config, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
            return self.get()

    def apply(self, expected_version):
        if not self.gate.acquire(blocking=False):
            raise ServiceError("runtime_busy", "当前有对话、语音或记忆任务，结束后再应用设置。", 409)
        try:
            if not self.memory_gate.acquire(blocking=False):
                raise ServiceError("runtime_busy", "当前有后台记忆任务，结束后再应用设置。", 409)
            try:
                with self.lock:
                    data, version = self._read()
                    if version != expected_version:
                        raise ServiceError("version_conflict", "模型配置已变化，请重新读取。", 409)
                    self.active = deepcopy(data)
                    self.active_version = version
            finally:
                self.memory_gate.release()
        finally:
            self.gate.release()
        return self.get()
