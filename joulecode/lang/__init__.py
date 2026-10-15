"""Language back-ends. `get_language(cfg.language)` returns the matching one."""

from __future__ import annotations

from joulecode.lang.base import FunctionInfo, Language, LanguagePrompts

LANGUAGES = ("python", "php", "javascript", "typescript", "java")


def get_language(name: str = "python") -> Language:
    if name == "python":
        from joulecode.lang.python import PythonLanguage

        return PythonLanguage()
    if name == "php":
        from joulecode.lang.php import PhpLanguage

        return PhpLanguage()
    if name in ("javascript", "typescript"):
        from joulecode.lang.javascript import JavaScriptLanguage

        return JavaScriptLanguage(typescript=name == "typescript")
    if name == "java":
        from joulecode.lang.java import JavaLanguage

        return JavaLanguage()
    raise ValueError(f"Unknown language {name!r} (supported: {', '.join(LANGUAGES)})")


__all__ = ["LANGUAGES", "FunctionInfo", "Language", "LanguagePrompts", "get_language"]
