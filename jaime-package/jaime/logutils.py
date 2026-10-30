"""Log line utilities shared by the machine and k8s collectors.

Provides tail-bounding and structural deduplication: runs of lines that are
identical once variable tokens (timestamps, numbers, hex, paths) are stripped
get collapsed into one sample line plus an omission count. This keeps reports
small and prevents repeated noise (e.g. health-check errors firing every few
seconds) from drowning out the causal error.
"""

import os
import re

_ERROR_RE = re.compile(r"(error|warning)", re.IGNORECASE)

# Bytes read from the end of a log file. A runaway guard: the collectors then
# bound what they keep by time and line count.
MAX_READ_BYTES = 16 * 1024 * 1024

# A single pathological line — a stack trace, a large SQL statement, a whole
# JSON document — would otherwise defeat a line-count bound. Every collected
# line is truncated to this many characters before it reaches the report.
MAX_LINE_CHARS = 2048


def truncate_line(line: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """Truncate a single line, marking that it was cut."""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + f" … (+{len(line) - max_chars} chars truncated)"


def cap_lines(lines: list[str], max_lines: int, max_chars: int = MAX_LINE_CHARS) -> list[str]:
    """Tail-bound to ``max_lines`` and truncate every line to ``max_chars``."""
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return [truncate_line(line, max_chars) for line in tail]


def filter_error_context(
    lines: list[str],
    max_lines: int,
    context_window: int = 3,
    pattern: re.Pattern = _ERROR_RE,
    window: str = "each",
    keep_matches: bool = True,
) -> list[str]:
    """Keep error/warning lines plus a context window, as the last ``max_lines``.

    Fetching a log tail via the k8s API (``tailLines``) always returns the
    END of the window — with a noisy pod, the causal error at the START of
    the window falls off. This filter instead keeps only relevant lines from
    a wider fetched window, so the error that triggered the incident is never
    lost. Falls back to the last ``max_lines`` raw lines when nothing matches.

    ``window`` puts the context window around ``"each"`` match or only the
    ``"last"`` one; ``keep_matches`` also keeps every matching line outside
    the window. ``pattern`` selects the matching lines.
    """
    matched = [i for i, line in enumerate(lines) if pattern.search(line)]
    if not matched:
        return lines[-max_lines:]
    include = set(matched) if keep_matches else set()
    for i in matched if window == "each" else matched[-1:]:
        lo = max(0, i - context_window)
        hi = min(len(lines), i + context_window + 1)
        include.update(range(lo, hi))
    result = [lines[i] for i in sorted(include)]
    return result[-max_lines:]


def read_tail(path: str, max_bytes: int = MAX_READ_BYTES) -> list[str]:
    """Lines from the last ``max_bytes`` of a file, without loading all of it.

    When the read starts mid-file, the first (partial) line is dropped. Raises
    ``OSError`` like ``open``.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode(errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def line_pattern(line: str) -> str:
    """Strip variable tokens from a log line to get its structural pattern."""
    s = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+Z]+", "TS", line)
    s = re.sub(r"\[\d+\]", "[N]", s)
    s = re.sub(r"\b0x[0-9a-fA-F]+\b", "HEX", s)
    s = re.sub(r"\b[0-9a-fA-F]{8,}\b", "HEX", s)
    s = re.sub(r"\b\d+\b", "N", s)
    # normalize each path segment (word after /) so /sys/module/foo/uevent
    # and /sys/module/bar/uevent both become /P/P/P/P
    s = re.sub(r'(?<=/)\w[\w.-]*(?=[/\'"\s]|$)', "P", s)
    # collapse variable identifier before ': LEVEL' (e.g. udevadm module names)
    s = re.sub(r"\b\w+: (Failed|Warning|Error)", r"TOKEN: \1", s)
    return s


def deduplicate_lines(lines: list[str], threshold: int = 3) -> list[str]:
    """Collapse repeated log lines to first occurrence plus a count.

    Two passes:

    1. Runs: consecutive lines that are structurally identical once variable
       tokens (timestamps, numbers, hex, paths) are stripped get collapsed,
       as do repeating block patterns.
    2. Frequency: a single-line pattern that recurs *anywhere* in the input
       (not just in a consecutive run) is collapsed to its first occurrence,
       followed by the total count. This handles timers that re-log the same
       warning every few seconds — e.g. a charm emitting the same block of
       warnings on every ``update-status`` cycle — where the runs are
       separated by the cycle's other messages, so pass 1 cannot see them as
       a run. Lines a run already summarised count with their run length, and
       a dropped occurrence takes its run marker with it, so every
       ``… N omitted`` marker follows the line it counts.

    Either pass only collapses a pattern that occurs at least ``threshold``
    times, so a one-off ambiguous match is never lost. The result preserves
    the order of first occurrences.
    """
    return _collapse_frequency(_collapse_runs(lines, threshold), threshold)


def _marker(omitted: int, block: bool) -> str:
    return f"    … {omitted} similar {'repetitions' if block else 'lines'} omitted"


def _collapse_runs(lines: list[str], threshold: int = 3) -> list[tuple[list[str], int]]:
    """Group consecutive runs or repeating blocks of structurally-identical lines.

    Returns ``(block_lines, repeats)`` groups in order: a plain line is
    ``([line], 1)``; a collapsed run is its first block with ``repeats`` >= 2.
    """
    patterns = [line_pattern(line) for line in lines]
    groups: list[tuple[list[str], int]] = []
    i = 0
    n = len(lines)
    max_block = 8

    while i < n:
        collapsed = False
        for L in range(1, max_block + 1):
            if i + L * threshold > n:
                break
            pattern_block = patterns[i : i + L]
            j = i + L
            while j + L <= n and patterns[j : j + L] == pattern_block:
                j += L
            count = (j - i) // L
            if count >= threshold:
                groups.append((lines[i : i + L], count))
                i = j
                collapsed = True
                break
        if not collapsed:
            groups.append(([lines[i]], 1))
            i += 1

    return groups


def _collapse_frequency(groups: list[tuple[list[str], int]], threshold: int = 3) -> list[str]:
    """Collapse single-line patterns that recur anywhere, keeping the first only.

    Multi-line blocks collapsed by the runs pass are atomic and kept as they
    are. A single-line pattern seen in ``threshold`` or more groups is emitted
    once, followed by the total number of omitted occurrences, run lengths
    included. Existing ``…`` marker lines in the input are carried through.
    """
    occurrences: dict[str, int] = {}
    totals: dict[str, int] = {}
    for block, repeats in groups:
        if len(block) == 1 and not block[0].strip().startswith("…"):
            pattern = line_pattern(block[0])
            occurrences[pattern] = occurrences.get(pattern, 0) + 1
            totals[pattern] = totals.get(pattern, 0) + repeats

    emitted: set[str] = set()
    result: list[str] = []
    for block, repeats in groups:
        if len(block) == 1 and not block[0].strip().startswith("…"):
            pattern = line_pattern(block[0])
            if occurrences[pattern] >= threshold:
                if pattern not in emitted:
                    emitted.add(pattern)
                    result.append(block[0])
                    result.append(_marker(totals[pattern] - 1, block=False))
                continue
        result.extend(block)
        if repeats > 1:
            result.append(_marker(repeats - 1, block=len(block) > 1))
    return result
