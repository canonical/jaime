"""Prompt projection: a bounded, relevance-ranked view of a report for the AI.

The report (``report.py``) is the persisted evidence artifact and is never
modified by this module. The projection is a pure function of (report text,
budget, version): the same inputs always produce byte-identical output. It is
applied only to the copy of the report text about to enter a prompt; the file
on disk is untouched.

Design: "Context evidence and prompt projection" in ``ARCHITECTURE.md``.

Tier model:

- Tier 1 - always included, individually bounded (a single large section
  cannot exhaust the whole budget, but the budget never drops it).
- Tier 2 - included while the budget allows, in report order; a section that
  would not fit is omitted with an explicit marker naming it.
- Tier 3 - a short digest only, regardless of budget; the raw content never
  reaches the prompt.
- Any heading not listed below defaults to Tier 2, so a future report section
  is budget-gated rather than silently bypassing the limit.

Two headings not named in ``ARCHITECTURE.md``'s tier table are pinned to
Tier 1 here: "Available actions" and "Charm links". The suggest prompt
explicitly forbids inventing an action or a URL; dropping either under budget
pressure would let the model do exactly that with no way to tell omission
from absence.

"Kubernetes events" is Tier 1 but not included verbatim: Warning event lines
are kept, Normal event lines are collapsed to one count line, regardless of
budget (the split is not itself budget-gated, the same as Tier 3 digesting).
"""

import re

# Bump when the tiering, the per-section cap or the digest format changes, so
# a recorded projection size/version pair in the audit log stays meaningful.
PROJECTION_VERSION = 1

# "Individually bounded": a Tier 1 section is never dropped, but a single
# section this large would still exhaust most budgets on its own.
_TIER_1_SECTION_CAP_BYTES = 4000

# A Tier 3 digest keeps at most this many content lines as a preview, plus a
# one-line count. Always small, so it is not itself budget-gated.
_TIER_3_PREVIEW_LINES = 3

_TIER_1_HEADINGS = frozenset(
    {
        "## Executive summary",
        "## Recent unit logs",
        "## Journal (errors/warnings)",
        "## Failed systemd units",
        "## Failed snap changes",
        "## Kubernetes events",
        "## Kubernetes pod",
        "## Available actions",
        "## Charm links",
    }
)
# Dynamic heading: "## Snap logs: `<service>` (failed)".
_TIER_1_PREFIXES = ("## Snap logs:",)

_TIER_3_HEADINGS = frozenset(
    {
        "## Disk usage",
        "## Memory",
        "## Network connections (listening + active)",
        "## Firewall rules (iptables — IPv4)",
        "## Firewall rules (ufw)",
        "## Firewall rules (nftables — IPv4)",
        "## Charm config",
    }
)


# "## Kubernetes events" lines are "<timestamp> <type> <reason> <message>
# [xN]" (see K8sApiClient.get_pod_events). Matched loosely: a line that does
# not parse is kept verbatim rather than risking signal loss on a parse miss.
_K8S_EVENT_TYPE_RE = re.compile(r"^\S+\s+(Warning|Normal)\b")


def _split_k8s_events(body: list[str]) -> list[str]:
    """Keep Warning event lines verbatim; collapse Normal lines to one count.

    ARCHITECTURE.md's per-source table tiers Kubernetes events as "Warning
    kept, Normal counted" rather than the whole section verbatim: a crashing
    container's Warning events are the signal, while routine Normal events
    (image pulled, container started) add bulk without diagnosing anything.
    """
    kept: list[str] = []
    normal_count = 0
    for line in body:
        match = _K8S_EVENT_TYPE_RE.match(line.strip())
        if match is None:
            kept.append(line)
            continue
        if match.group(1) == "Warning":
            kept.append(line)
        else:
            normal_count += 1
    if normal_count:
        kept.append(f"_{normal_count} Normal event(s) not shown individually._")
    return kept


_SUMMARY_CHANGED_OPTION_RE = re.compile(r"^- `([^`]+)`:")
_CHARM_CONFIG_OPTION_RE = re.compile(r"^(\S+):")


def _changed_option_names(body: list[str]) -> set[str]:
    """Option names the executive summary names as changed from default."""
    names = set()
    for line in body:
        match = _SUMMARY_CHANGED_OPTION_RE.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


def _drop_already_mentioned_options(body: list[str], already_mentioned: set[str]) -> list[str]:
    """Drop a "Charm config" line for an option the summary already named.

    The summary (`` - `name`: `value` (default: `default`) ``) and "Charm
    config" (``name: value  # changed from default (default)``) render the
    same changed option in different formats, so the exact-line dedup above
    does not catch it; this matches by option name instead.
    """
    if not already_mentioned:
        return body
    kept = []
    for line in body:
        match = _CHARM_CONFIG_OPTION_RE.match(line.strip())
        if match and match.group(1) in already_mentioned:
            continue
        kept.append(line)
    return kept


def _tier(heading: str) -> int:
    """Tier of a section heading; the preamble (no heading yet) is Tier 1."""
    if not heading or heading in _TIER_1_HEADINGS or heading.startswith(_TIER_1_PREFIXES):
        return 1
    if heading in _TIER_3_HEADINGS:
        return 3
    return 2


def _split_sections(report: str) -> list[tuple[str, list[str]]]:
    """Split a rendered report into ``(heading, body_lines)`` in report order.

    Splits only on a literal ``"## "`` heading line, which is the one level
    ``report.py`` uses for every section; the preamble (the ``# Incident
    Report`` title and its key/value lines) is the first entry, with an empty
    heading.
    """
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    body: list[str] = []
    for line in report.split("\n"):
        if line.startswith("## "):
            sections.append((heading, body))
            heading = line
            body = []
        else:
            body.append(line)
    sections.append((heading, body))
    return sections


def _render(heading: str, body: list[str]) -> str:
    chunk = ([heading] if heading else []) + body
    return "\n".join(chunk)


def _cap_bytes(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes``, with an omission marker.

    Cuts on a line boundary so the result stays valid Markdown; the marker
    states how many bytes were cut, never silently.
    """
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    size = 0
    for line in lines:
        line_size = len(line.encode()) + 1
        if size + line_size > max_bytes:
            break
        kept.append(line)
        size += line_size
    omitted = len(encoded) - size
    kept.append(f"_… {omitted} bytes omitted …_")
    return "\n".join(kept)


def _digest(heading: str, body: list[str]) -> str:
    """A short, bounded digest of a Tier 3 section: a line count plus a preview.

    Fence markers (``` ```) are not content and are excluded from both the
    count and the preview.
    """
    content = [line for line in body if line.strip() not in ("```",) and line.strip()]
    chunk = [heading, f"_{len(content)} lines, digested for the prompt; see the full report for detail._"]
    preview = content[:_TIER_3_PREVIEW_LINES]
    if preview:
        chunk.extend(["```", *preview, "```"])
    return "\n".join(chunk)


def _normalize(line: str) -> str:
    """A de-duplication key for ``line``, or ``""`` to exempt it from dedup.

    Collapses whitespace runs so differently-spaced renderings compare equal:
    the executive summary's error lines and the compacted timeline in "Recent
    unit logs" carry the same timestamp and message but different
    inter-field spacing, and normalising is what lets the cross-section
    de-duplication below catch that case.

    Code-fence markers (```, ```yaml, ...) are structural Markdown, not
    content, and are identical across every section by construction - treating
    them as dedup candidates silently drops a later section's closing fence
    the moment any earlier section's fence was already seen, corrupting the
    document. They are exempted (mapped to ""), never added to the seen set
    and never skipped.
    """
    stripped = line.strip()
    if stripped.startswith("```"):
        return ""
    return " ".join(line.split())


def build_projection(report: str, budget_bytes: int, version: int = PROJECTION_VERSION) -> str:
    """Return a bounded, relevance-ranked projection of ``report`` for a prompt.

    Pure: the same ``report`` and ``budget_bytes`` always produce the same
    output. ``budget_bytes <= 0`` is a no-op (returns ``report`` unchanged),
    since a non-positive budget cannot mean anything else. ``version`` is
    accepted so a caller recording it alongside the output need not import
    ``PROJECTION_VERSION`` separately, but it does not change behaviour today.
    """
    del version  # accepted for the caller's audit record; no behaviour change yet.
    if budget_bytes <= 0:
        return report

    sections = _split_sections(report)

    seen_lines: set[str] = set()

    def dedup(body: list[str]) -> list[str]:
        kept = []
        for line in body:
            key = _normalize(line)
            if key and key in seen_lines:
                continue
            if key:
                seen_lines.add(key)
            kept.append(line)
        return kept

    rendered: list[str | None] = [None] * len(sections)
    used_bytes = 0
    tier2_indices: list[int] = []
    changed_option_names: set[str] = set()

    # Tier 1 first: always included, each individually capped, never counted
    # against or truncated by the budget beyond its own cap. The executive
    # summary is always first in report order, so by the time "Charm config"
    # (Tier 3) is reached below, changed_option_names is already populated.
    for i, (heading, body) in enumerate(sections):
        tier = _tier(heading)
        if tier == 1:
            if heading == "## Executive summary":
                changed_option_names |= _changed_option_names(body)
            if heading == "## Kubernetes events":
                body = _split_k8s_events(body)
            text = _cap_bytes(_render(heading, dedup(body)), _TIER_1_SECTION_CAP_BYTES)
            rendered[i] = text
            used_bytes += len(text.encode()) + 1
        elif tier == 3:
            if heading == "## Charm config":
                body = _drop_already_mentioned_options(body, changed_option_names)
            text = _digest(heading, dedup(body))
            rendered[i] = text
            used_bytes += len(text.encode()) + 1
        else:
            tier2_indices.append(i)

    # Tier 2 next, in report order, first-fit against what remains of the
    # budget: a section that does not fit is skipped (not truncated) and
    # named in an omission marker placed where it would have been, so later,
    # smaller Tier 2 sections still get a chance.
    remaining = budget_bytes - used_bytes
    for i in tier2_indices:
        heading, body = sections[i]
        text = _render(heading, dedup(body))
        size = len(text.encode()) + 1
        if size <= remaining:
            rendered[i] = text
            remaining -= size
        else:
            label = heading.removeprefix("## ") if heading else "(preamble)"
            rendered[i] = f"_`{label}` omitted ({size} bytes) to fit the prompt budget._"

    return "\n".join(chunk for chunk in rendered if chunk is not None)
