"""提示词和角色档案共用的语言选择。"""


def normalize_language(language: str = "zh") -> str:
    return "en" if (language or "zh").lower().startswith("en") else "zh"
