"""Operating mode and AI provider names shared by the charm logic."""

from enum import Enum


class Mode(str, Enum):
    OBSERVE = "observe"
    SUGGEST = "suggest"
    ACT = "act"


class Provider(str, Enum):
    NONE = "none"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    REMOTE = "remote"
