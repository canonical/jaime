"""Suggest and act modes for Jaime.

suggest: calls the AI provider with the incident report as context and returns
         a Suggestion object containing the LLM's diagnosis and commands.
         Nothing is executed.

act:     same as suggest, but also executes every command in the Suggestion.
         All executions are audited to the JSONL log.
"""

import hashlib
import logging
import re
import shlex
import subprocess

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

Keep your response concise and focused on the incident.

{additional_context_section}Incident report:
---
{report}
---"""


def build_suggest_prompt(report_content: str,
                         additional_context: str = "") -> str:
    """Build the prompt to send to the AI provider for suggest/act mode."""
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


def execute_command(command: str, timeout: int = 30) -> dict:
    """Execute a command and return a result dict.

    Uses shell=False by splitting the command string with shlex.
    Returns a dict with keys: command, returncode, stdout, stderr.
    """
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "returncode": -1, "stdout": "", "stderr": "timed out"}
    except Exception as e:
        return {"command": command, "returncode": -1, "stdout": "", "stderr": str(e)}


def run_suggest(provider, report_content: str,
                additional_context: str = "") -> Suggestion:
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


def run_act(
    provider,
    report_content: str,
    additional_context: str = "",
    dry_run: bool = False,
) -> tuple[Suggestion | None, list[dict]]:
    """Call the AI provider, build a Suggestion, and execute its commands.

    Returns (suggestion, execution_results).
    If dry_run is True, commands are parsed but not executed.
    """
    suggestion = run_suggest(provider, report_content, additional_context)
    if suggestion is None:
        return None, []

    results = []
    for cmd in suggestion.commands:
        if dry_run:
            logger.info("act dry-run: would execute: %s", cmd)
            results.append({
                "command": cmd,
                "returncode": None,
                "stdout": "",
                "stderr": "dry-run: not executed",
            })
        else:
            logger.info("act: executing command: %s", cmd)
            result = execute_command(cmd)
            logger.info("act: command %r exited %d", cmd, result["returncode"])
            results.append(result)

    return suggestion, results
