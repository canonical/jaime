"""Incident and Suggestion models for Jaime."""

import dataclasses
import datetime
import uuid


@dataclasses.dataclass(frozen=True)
class UsageMetadata:
    """Token usage and cost metadata from a single LLM call.

    Attributes:
        prompt_tokens:      Number of tokens in the prompt.
        completion_tokens:  Number of tokens in the completion.
        total_tokens:       Total tokens (prompt + completion).
        cost_usd:           Cost in USD as reported by the provider, or None if unavailable.
        model:              Model name used for this call.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    model: str = ""

    def to_dict(self) -> dict:
        d = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
        }
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "UsageMetadata":
        return cls(
            prompt_tokens=d.get("prompt_tokens", 0),
            completion_tokens=d.get("completion_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            cost_usd=d.get("cost_usd"),
            model=d.get("model", ""),
        )


@dataclasses.dataclass(frozen=True)
class Suggestion:
    """An AI-generated suggestion attached to an incident.

    Attributes:
        description:   The LLM's analysis and diagnosis text.
        commands:      Commands parsed from the LLM response.
        generated_at:  ISO 8601 timestamp (UTC) when the suggestion was created.
        context_hash:  SHA-256 of the additional-context string, or empty if none.
        usage:         Token usage and cost metadata from the LLM call, or None.
    """

    description: str
    commands: tuple[str, ...]
    generated_at: str
    context_hash: str = ""
    usage: UsageMetadata | None = None

    @classmethod
    def from_llm(cls, description: str, commands: list[str],
                 context_hash: str = "",
                 usage: UsageMetadata | None = None) -> "Suggestion":
        """Create a Suggestion from LLM output."""
        return cls(
            description=description,
            commands=tuple(commands),
            generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            context_hash=context_hash,
            usage=usage,
        )

    def to_dict(self) -> dict:
        d = {
            "description": self.description,
            "commands": list(self.commands),
            "generated_at": self.generated_at,
            "context_hash": self.context_hash,
        }
        if self.usage is not None:
            d["usage"] = self.usage.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Suggestion":
        usage = None
        if "usage" in d:
            usage = UsageMetadata.from_dict(d["usage"])
        return cls(
            description=d["description"],
            commands=tuple(d.get("commands", [])),
            generated_at=d["generated_at"],
            context_hash=d.get("context_hash", ""),
            usage=usage,
        )


@dataclasses.dataclass(frozen=True)
class Incident:
    """An incident for a principal unit.

    Attributes:
        id:              Stable UUID assigned when the incident is first opened.
        opened_at:       ISO 8601 timestamp (UTC) when the incident was opened.
        status_message:  The workload status message ("reason") at incident open.
        closed_at:       ISO 8601 timestamp (UTC) when the incident was closed, or None.
        suggestion:      AI-generated suggestion, or None if not yet produced.
    """

    id: str
    opened_at: str
    status_message: str = ""
    closed_at: str | None = None
    suggestion: Suggestion | None = None

    @classmethod
    def open(cls, status_message: str = "") -> "Incident":
        """Create a new incident with a fresh UUID and the current UTC time.

        ``status_message`` is the workload's status message ("reason") at the
        time the incident opens. It is a snapshot: if the message changes
        during the episode (e.g. a retry loop re-setting the same status with
        a new message), the incident keeps the reason it was opened with.
        """
        return cls(
            id=str(uuid.uuid4()),
            opened_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            status_message=status_message,
        )

    def close(self) -> "Incident":
        """Return a closed copy of this incident with the current UTC time."""
        return dataclasses.replace(
            self,
            closed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    def attach_suggestion(self, suggestion: Suggestion) -> "Incident":
        """Return a copy of this incident with the suggestion attached."""
        return dataclasses.replace(self, suggestion=suggestion)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict:
        d = {"id": self.id, "opened_at": self.opened_at}
        if self.status_message:
            d["status_message"] = self.status_message
        if self.closed_at is not None:
            d["closed_at"] = self.closed_at
        if self.suggestion is not None:
            d["suggestion"] = self.suggestion.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        suggestion = None
        if "suggestion" in d:
            suggestion = Suggestion.from_dict(d["suggestion"])
        return cls(
            id=d["id"],
            opened_at=d["opened_at"],
            status_message=d.get("status_message", ""),
            closed_at=d.get("closed_at"),
            suggestion=suggestion,
        )
