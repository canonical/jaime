"""Suggest mode for Jaime.

Calls the AI provider with the incident report as context and returns a
Suggestion object containing the LLM's diagnosis and commands. Nothing is
executed: act mode is gated in the charm and ships no execution code (see
``ARCHITECTURE.md``, Phase 8).
"""

import hashlib
import logging
import re

from jaime.incident import Suggestion

logger = logging.getLogger(__name__)

_SUGGEST_PROMPT_TEMPLATE = """\
You are Jaime, a Juju charm diagnostic assistant. You have been given an incident \
report for a Juju unit that is in an unhealthy state. Your job is to:

1. Diagnose the most likely root cause based on the report. You may mention \
investigation commands in plain text as part of the diagnosis.
2. Suggest the single most likely remediation command that fixes the issue. \
Do not include investigation commands in fenced code blocks. Do not offer \
alternatives or options in fenced code blocks. Only the command that will \
resolve the problem. If multiple steps are required, use && to chain them \
into one command.
3. Format the remediation command inside a fenced code block tagged with \
'bash', like this:
```bash
juju config postgresql some-key=true
```

The report may list the workload's available actions under "Available actions". \
If you suggest a `juju run <unit> <action>` command, the action MUST appear in \
that list — never invent an action that is not listed. If no suitable action \
exists in the report, suggest a juju config change or a documented operator \
command instead.

The report may list project resources under "Charm links" (documentation, \
source, issue tracker). Base your remediation on what those official sources \
say about the workload, and cite the relevant URL in your diagnosis. Never \
invent a URL or a procedure that is not in the report.

Keep your response concise and focused on the incident.

{additional_context_section}Incident report:
---
{report}
---

Reminder: give your diagnosis, then the single remediation command in one \
fenced code block tagged with 'bash'. Only suggest a `juju run` action that \
appears under "Available actions" in the report."""


_ASK_PROMPT_TEMPLATE = """\
You are Jaime, a Juju charm diagnostic assistant. An operator is asking \
follow-up questions about an incident on a Juju unit. The incident report and \
the previous conversation are below.

Answer the operator's latest question directly and concisely, using only the \
report and the previous conversation. If they do not contain what is needed to \
answer, say so and name what the operator should check, instead of guessing. \
Do not repeat earlier answers.

Only when the question asks how to fix or change something, give the single \
command that does it in a fenced code block tagged with 'bash'. Do not put \
investigation commands or alternatives in fenced code blocks. If you suggest a \
`juju run <unit> <action>` command, the action MUST appear under "Available \
actions" in the report. Never invent an action, a URL or a procedure that is \
not in the report.

Incident report:
---
{report}
---

{history_section}Operator question:
{question}

Reminder: answer from the report and the conversation only. Give a command, in \
one fenced code block tagged with 'bash', only if the question asks how to fix \
or change something."""

# Only the most recent turns reach the prompt, each answer is truncated, so the
# prompt stays bounded however long the conversation gets.
_HISTORY_MAX_TURNS = 10
_HISTORY_ANSWER_CHARS = 400


def build_history_section(history: list[dict] | None) -> str:
    """Render the most recent conversation turns as a compact section.

    Each history entry is a dict with ``question``, ``description`` and
    optionally ``id`` and ``commands``. A turn without a question is the
    incident's initial one-shot suggestion. Absent or empty history returns an
    empty string.
    """
    lines = []
    for turn in (history or [])[-_HISTORY_MAX_TURNS:]:
        question = (turn.get("question") or "").strip()
        answer = " ".join((turn.get("description") or "").split())
        if not question and not answer:
            continue
        if len(answer) > _HISTORY_ANSWER_CHARS:
            answer = answer[:_HISTORY_ANSWER_CHARS].rstrip() + "..."
        label = f"Turn {turn['id']}" if turn.get("id") else "Turn"
        if question:
            lines.append(f"{label} - operator: {question}")
        else:
            lines.append(f"{label} - initial suggestion (no question):")
        if answer:
            lines.append(f"  Jaime: {answer}")
        commands = [c for c in turn.get("commands") or [] if c]
        if commands:
            lines.append(f"  Suggested command: {'; '.join(commands)}")
    if not lines:
        return ""
    return "Previous conversation:\n" + "\n".join(lines) + "\n\n"


def build_suggest_prompt(report_content: str, additional_context: str = "") -> str:
    """Build the prompt to send to the AI provider for suggest mode."""
    extra = ""
    if additional_context:
        extra = (
            "## Operator-supplied context (HIGH PRIORITY)\n\n"
            "The operator has provided the following additional context. "
            "Treat this information as authoritative for this investigation "
            "and incorporate it into your diagnosis and remediation plan.\n\n"
            f"{additional_context}\n\n"
            "---\n"
        )
    return _SUGGEST_PROMPT_TEMPLATE.format(
        report=report_content,
        additional_context_section=extra,
    )


def build_ask_prompt(report_content: str, question: str, history: list[dict] | None = None) -> str:
    """Build the prompt for one conversational turn (`ask`)."""
    return _ASK_PROMPT_TEMPLATE.format(
        report=report_content,
        history_section=build_history_section(history),
        question=question,
    )


def parse_commands(llm_response: str) -> list[str]:
    """Extract shell commands from fenced ```bash ... ``` blocks in the LLM response.

    Returns a flat list of non-empty, non-comment command strings.
    """
    commands = []
    for block in re.findall(r"```bash\s*\n(.*?)```", llm_response, re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    return commands


def run_suggest(provider, report_content: str, additional_context: str = "") -> Suggestion:
    """Call the AI provider and return a Suggestion.

    Raises if the provider call fails, so the caller can surface the error.
    Returns None if provider is None.
    """
    if provider is None:
        return None
    prompt = build_suggest_prompt(report_content, additional_context)
    llm_response, usage = provider.generate(prompt)
    logger.info("AI provider returned suggestion successfully")
    logger.debug("AI provider suggestion response:\n%s", llm_response)
    commands = parse_commands(llm_response)
    context_hash = hashlib.sha256(additional_context.encode()).hexdigest() if additional_context else ""
    return Suggestion.from_llm(
        description=llm_response,
        commands=commands,
        context_hash=context_hash,
        usage=usage,
    )


def run_ask(provider, report_content: str, question: str, history: list[dict] | None = None) -> Suggestion:
    """Answer one operator question about an incident.

    ``history`` carries the prior conversation turns so the model sees the
    thread. Raises if the provider call fails. Returns None if provider is
    None.
    """
    if provider is None:
        return None
    prompt = build_ask_prompt(report_content, question, history)
    llm_response, usage = provider.generate(prompt)
    logger.info("AI provider answered an operator question")
    logger.debug("AI provider answer:\n%s", llm_response)
    return Suggestion.from_llm(
        description=llm_response,
        commands=parse_commands(llm_response),
        context_hash=hashlib.sha256(question.encode()).hexdigest(),
        usage=usage,
    )
