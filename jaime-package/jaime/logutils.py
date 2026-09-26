"""Log line utilities shared by the machine and k8s collectors.

Provides tail-bounding and structural deduplication: runs of lines that are
identical once variable tokens (timestamps, numbers, hex, paths) are stripped
get collapsed into one sample line plus an omission count. This keeps reports
small and prevents repeated noise (e.g. health-check errors firing every few
seconds) from drowning out the causal error.
"""

import re

_ERROR_RE = re.compile(r"(error|warning)", re.IGNORECASE)

# A single pathological line — a stack trace, a large SQL statement, a whole
# JSON document — would otherwise defeat a line-count bound. Every collected
# line is truncated to this many characters before it reaches the report.
MAX_LINE_CHARS = 2048


def truncate_line(line: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """Truncate a single line, marking that it was cut."""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + f" … (+{len(line) - max_chars} chars truncated)"


def cap_lines(lines: list[str], max_lines: int,
              max_chars: int = MAX_LINE_CHARS) -> list[str]:
    """Tail-bound to ``max_lines`` and truncate every line to ``max_chars``."""
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return [truncate_line(line, max_chars) for line in tail]


def filter_error_context(lines: list[str], max_lines: int,
                         context_window: int = 3) -> list[str]:
    """Keep error/warning lines plus a context window around each match.

    Fetching a log tail via the k8s API (``tailLines``) always returns the
    END of the window — with a noisy pod, the causal error at the START of
    the window falls off. This filter instead keeps only relevant lines from
    a wider fetched window, so the error that triggered the incident is never
    lost. Falls back to the last ``max_lines`` raw lines when nothing matches.
    """
    matched = [i for i, line in enumerate(lines) if _ERROR_RE.search(line)]
    if not matched:
        return lines[-max_lines:]
    include = set()
    for i in matched:
        lo = max(0, i - context_window)
        hi = min(len(lines), i + context_window + 1)
        include.update(range(lo, hi))
    result = [lines[i] for i in sorted(include)]
    return result[-max_lines:]


def tail_lines(text: str, max_lines: int) -> list[str]:
    """Return the last ``max_lines`` lines of a text blob."""
    lines = text.splitlines()
    return lines[-max_lines:] if len(lines) > max_lines else lines


def line_pattern(line: str) -> str:
    """Strip variable tokens from a log line to get its structural pattern."""
    s = re.sub(r'\d{4}-\d{2}-\d{2}T[\d:.+Z]+', 'TS', line)
    s = re.sub(r'\[\d+\]', '[N]', s)
    s = re.sub(r'\b0x[0-9a-fA-F]+\b', 'HEX', s)
    s = re.sub(r'\b[0-9a-fA-F]{8,}\b', 'HEX', s)
    s = re.sub(r'\b\d+\b', 'N', s)
    # normalize each path segment (word after /) so /sys/module/foo/uevent
    # and /sys/module/bar/uevent both become /P/P/P/P
    s = re.sub(r'(?<=/)\w[\w.-]*(?=[/\'"\s]|$)', 'P', s)
    # collapse variable identifier before ': LEVEL' (e.g. udevadm module names)
    s = re.sub(r'\b\w+: (Failed|Warning|Error)', r'TOKEN: \1', s)
    return s


def deduplicate_lines(lines: list[str], threshold: int = 3) -> list[str]:
    """Collapse runs of structurally identical lines or repeating block patterns."""
    result: list[str] = []
    i = 0
    n = len(lines)
    max_block = 8

    while i < n:
        collapsed = False
        for L in range(1, max_block + 1):
            if i + L * threshold > n:
                break
            pattern_block = [line_pattern(lines[i + k]) for k in range(L)]
            j = i + L
            while j + L <= n and [line_pattern(lines[j + k]) for k in range(L)] == pattern_block:
                j += L
            count = (j - i) // L
            if count >= threshold:
                result.extend(lines[i:i + L])
                omitted = count - 1
                if omitted > 0:
                    label = "lines" if L == 1 else "repetitions"
                    result.append(f"    … {omitted} similar {label} omitted")
                i = j
                collapsed = True
                break
        if not collapsed:
            result.append(lines[i])
            i += 1

    return result
