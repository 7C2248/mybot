"""读取图构建和回复提示词共用的角色档案。"""

from pathlib import Path

from agent.utils.language import normalize_language

_LANGUAGE_FILES = {"zh": "profile_cn.md", "en": "profile_en.md"}


def character_dir(character_name: str) -> Path:
    """角色专属目录：档案文档与 images/audio/model 资源文件夹。"""
    return Path(__file__).resolve().parents[2] / "Character" / character_name


def load_character_profile(character_name: str, language: str = "zh") -> str:
    """读取角色目录下的语言档案；缺失时回退到任一 profile_*.md。"""
    directory = character_dir(character_name)
    path = directory / _LANGUAGE_FILES[normalize_language(language)]
    if path.exists():
        return path.read_text(encoding="utf-8")
    candidates = sorted(directory.glob("profile_*.md"))
    if candidates:
        return candidates[0].read_text(encoding="utf-8")
    raise FileNotFoundError(f"未找到角色档案: {directory / 'profile_*.md'}")
