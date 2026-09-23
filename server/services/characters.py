"""An explicit registry of profiles and image resources rooted in Character/."""

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from server.classes.api import CharacterAsset, CharacterDetail, CharacterSummary, ServiceError
from server.services.files import atomic_write

_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


@dataclass(frozen=True)
class ImageResource:
    path: Path
    character_root: Path
    media_type: str


class CharacterCatalog:
    """A startup snapshot. Re-check image containment whenever a resource is opened."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._characters: dict[str, CharacterDetail] = {}
        self._resources: dict[str, ImageResource] = {}
        self._profiles: dict[tuple[str, str], Path] = {}
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            self._load()

    def _load(self):
        characters, resources, profile_paths = {}, {}, {}
        if not self.root.is_dir():
            return
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or not self._within(directory, self.root):
                continue
            character_root = directory.resolve()
            # A junction/alias must not register another character's resources as its own.
            if character_root.parent != self.root or character_root.name != directory.name:
                continue
            profiles = {}
            for profile in sorted(directory.glob("profile_*.md")):
                if not profile.is_file() or not self._within(profile, character_root):
                    continue
                language = profile.stem.removeprefix("profile_")
                language = "zh" if language == "cn" else language
                if language:
                    profiles[language] = profile.read_bytes().decode("utf-8")
                    profile_paths[(directory.name, language)] = profile
            if not profiles:
                continue
            assets = []
            image_root = directory / "images"
            if image_root.is_dir() and self._within(image_root, character_root):
                for path in sorted(image_root.iterdir()):
                    media_type = _IMAGE_TYPES.get(path.suffix.lower())
                    if not media_type or not path.is_file() or not self._within(path, image_root.resolve()):
                        continue
                    # The ID is stable across restarts; clients never submit filesystem paths.
                    identity = f"{directory.name}/{path.name}"
                    resource_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    resources[resource_id] = ImageResource(path, character_root, media_type)
                    assets.append(CharacterAsset(id=resource_id, name=path.name,
                                                 url=f"/api/resources/{resource_id}"))
            version_data = json.dumps({"profiles": profiles, "assets": [a.id for a in assets]},
                                      sort_keys=True, ensure_ascii=False)
            characters[directory.name] = CharacterDetail(
                id=directory.name, name=directory.name, languages=sorted(profiles),
                profiles=profiles, assets=assets,
                version=hashlib.sha256(version_data.encode("utf-8")).hexdigest(),
            )
        self._profiles = profile_paths
        self._resources = resources
        self._characters = characters

    def write_profile(self, character_id, language, text, expected_version):
        with self._lock:
            self._load()  # Detect edits made outside the API before checking the version.
            character = self.get(character_id)
            if character.version != expected_version:
                raise ServiceError("version_conflict", "角色档案已变化，请重新读取后保存。", 409)
            path = self._profiles.get((character_id, language))
            root = self.root / character_id
            if path is None or path.is_symlink() or not self._within(path, root):
                raise ServiceError("profile_not_found", "该语言档案不存在或不可写。", 404)
            atomic_write(path, text)
            self._load()
            return self.get(character_id)

    def voice_profile(self, character_id):
        character = self.get(character_id)
        root = self.root / character_id
        path = root / "tts.md"
        if path.is_file() and self._within(path, root):
            return path.read_bytes().decode("utf-8")
        return character.profiles.get("zh") or next(iter(character.profiles.values()))

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            return path.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError):
            return False

    def summaries(self) -> list[CharacterSummary]:
        return [CharacterSummary.model_validate(item.model_dump()) for item in self._characters.values()]

    def get(self, character_id: str) -> CharacterDetail:
        if character_id not in self._characters:
            raise ServiceError("character_not_found", "角色不存在。", 404)
        return self._characters[character_id]

    def resource(self, resource_id: str) -> ImageResource:
        resource = self._resources.get(resource_id)
        if resource is None or not resource.path.is_file():
            raise ServiceError("resource_not_found", "图片资源不存在。", 404)
        # Check both boundaries again in case a file or directory was replaced after startup.
        image_root = resource.character_root / "images"
        if (not self._within(resource.character_root, self.root)
                or not self._within(image_root, resource.character_root)
                or not self._within(resource.path, image_root.resolve())):
            raise ServiceError("resource_not_found", "图片资源不存在。", 404)
        return resource
