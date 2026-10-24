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
    def from_llm(
        cls, description: str, commands: list[str], context_hash: str = "", usage: UsageMetadata | None = None
    ) -> "Suggestion":
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


# Conversation depth kept per incident; the oldest turns are pruned beyond it.
MAX_TURNS = 20


@dataclasses.dataclass(frozen=True)
class Turn:
    """One question/answer turn in an incident's conversation.

    Attributes:
        id:           Per-incident turn number (1, 2, 3 ...), assigned by
                      ``Incident.append_turn``; 0 until the turn is appended.
        question:     The operator's question (empty for the legacy single shot).
        description:  The LLM's analysis and diagnosis text.
        commands:     Commands parsed from the LLM response.
        generated_at: ISO 8601 timestamp (UTC) when the turn was created.
        context_hash: SHA-256 of the additional-context string, or empty if none.
        usage:        Token usage and cost metadata from the LLM call, or None.
    """

    id: int
    question: str
    description: str
    commands: tuple[str, ...] = ()
    generated_at: str = ""
    context_hash: str = ""
    usage: UsageMetadata | None = None

    @classmethod
    def from_suggestion(cls, suggestion: "Suggestion", question: str = "", turn_id: int = 0) -> "Turn":
        """Wrap a Suggestion as a conversational turn."""
        return cls(
            id=turn_id,
            question=question,
            description=suggestion.description,
            commands=suggestion.commands,
            generated_at=suggestion.generated_at,
            context_hash=suggestion.context_hash,
            usage=suggestion.usage,
        )

    def to_dict(self) -> dict:
        d = {"id": self.id, "question": self.question, "description": self.description}
        if self.commands:
            d["commands"] = list(self.commands)
        if self.generated_at:
            d["generated_at"] = self.generated_at
        if self.context_hash:
            d["context_hash"] = self.context_hash
        if self.usage is not None:
            d["usage"] = self.usage.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Turn":
        usage = None
        if "usage" in d:
            usage = UsageMetadata.from_dict(d["usage"])
        return cls(
            id=d["id"],
            question=d.get("question", ""),
            description=d["description"],
            commands=tuple(d.get("commands", [])),
            generated_at=d.get("generated_at", ""),
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
        turns:           Append-only conversation of AI-generated turns. The
                         legacy single-suggestion shape is migrated to one turn
                         with an empty question.
        turn_seq:        Number of turns ever appended; the next turn id is this
                         plus one, so turn ids never repeat even after pruning.
    """

    id: str
    opened_at: str
    status_message: str = ""
    closed_at: str | None = None
    turns: tuple[Turn, ...] = ()
    turn_seq: int = 0

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

    @property
    def suggestion(self) -> "Suggestion | None":
        """The latest turn as a Suggestion (legacy single-shot view), or None."""
        if not self.turns:
            return None
        latest = self.turns[-1]
        return Suggestion(
            description=latest.description,
            commands=latest.commands,
            generated_at=latest.generated_at,
            context_hash=latest.context_hash,
            usage=latest.usage,
        )

    @property
    def next_turn_id(self) -> int:
        """The id for the next appended turn (per-incident, 1-based)."""
        return self.turn_seq + 1

    def attach_suggestion(self, suggestion: "Suggestion") -> "Incident":
        """Return a copy with the suggestion appended as a turn (legacy API)."""
        return self.append_turn(Turn.from_suggestion(suggestion))

    def append_turn(self, turn: Turn) -> "Incident":
        """Return a copy with the turn appended under the next turn id."""
        turn = dataclasses.replace(turn, id=self.next_turn_id)
        return dataclasses.replace(self, turns=self.turns + (turn,), turn_seq=self.turn_seq + 1)

    def prune_turns(self, max_turns: int = MAX_TURNS) -> "Incident":
        """Return a copy keeping only the most recent ``max_turns`` turns."""
        if max_turns < 0 or len(self.turns) <= max_turns:
            return self
        kept = self.turns[-max_turns:] if max_turns else ()
        return dataclasses.replace(self, turns=kept)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict:
        d = {"id": self.id, "opened_at": self.opened_at}
        if self.status_message:
            d["status_message"] = self.status_message
        if self.closed_at is not None:
            d["closed_at"] = self.closed_at
        if self.turns:
            d["turns"] = [t.to_dict() for t in self.turns]
        d["turn_seq"] = self.turn_seq
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        turns: tuple[Turn, ...] = ()
        if "turns" in d:
            turns = tuple(Turn.from_dict(t) for t in d["turns"])
        elif "suggestion" in d:
            # Legacy single-suggestion state: migrate to one turn.
            turns = (Turn.from_suggestion(Suggestion.from_dict(d["suggestion"]), turn_id=1),)
        turn_seq = d.get("turn_seq")
        if turn_seq is None:
            # Legacy state: continue numbering after the stored turns.
            int_ids = [t.id for t in turns if isinstance(t.id, int)]
            turn_seq = max(int_ids + [len(turns)])
        return cls(
            id=d["id"],
            opened_at=d["opened_at"],
            status_message=d.get("status_message", ""),
            closed_at=d.get("closed_at"),
            turns=turns,
            turn_seq=int(turn_seq),
        )
