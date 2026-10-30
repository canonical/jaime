"""Unit tests for jaime.projection (bounded, relevance-ranked prompt view)."""

from jaime.projection import build_projection

_REPORT = """# Incident Report
- incident: abc-123
- unit: postgresql/0
- status: blocked
- first-seen: 2026-01-01T00:00:00+00:00
- generated: 2026-01-01T00:05:00+00:00

## Executive summary
Unit is in **blocked** state.

**Recent errors/warnings:**
```
2026-01-01 00:00:00 ERROR cannot start: disk full
```

**Config changed from default by operator:**
- `max_connections`: `50` (default: `100`)

## Processes
- **patroni**: 0 running (expected 1-1) ✗

## Network ports
- `5432/tcp` → listening ✓

## Available actions
```yaml
restart: Restart the service
```

## Charm links

- docs: https://example.com/docs

## Disk usage
```
Filesystem     Size  Used Avail Use% Mounted on
/dev/sda1       20G   19G  1.0G  95% /
```

## Memory
RAM: 6.1G used / 7.7G total (1.2G available)

## Charm config
```
max_connections: 50  # changed from default (100)
port: 5432
```

## Recent unit logs
```
2026-01-01 00:00:00  ERROR cannot start: disk full
2026-01-01 00:00:05  INFO unrelated startup line
```
"""


class TestNoOp:
    def test_zero_or_negative_budget_is_unchanged(self):
        assert build_projection(_REPORT, 0) == _REPORT
        assert build_projection(_REPORT, -1) == _REPORT


class TestDeterminism:
    def test_same_inputs_byte_identical_output(self):
        a = build_projection(_REPORT, 400)
        b = build_projection(_REPORT, 400)
        assert a == b

    def test_differs_from_raw_report_even_at_huge_budget(self):
        # Tier 3 sections are always digested, regardless of budget, so a
        # huge budget is not the same as "unchanged" - only budget<=0 is.
        out = build_projection(_REPORT, 10**9)
        assert out != _REPORT
        assert "## Disk usage" in out
        assert "digested for the prompt" in out


class TestTier1NeverTruncatedByBudget:
    def test_journal_survives_a_tiny_budget(self):
        # ARCHITECTURE.md's per-source table assigns journalctl to Tier 1
        # (errors/warnings only); it is easy to mis-tier since it is not named
        # in the prose tier list, only in the detailed table.
        report = _REPORT + "\n## Journal (errors/warnings)\n```\nsome journal line\n```\n"
        out = build_projection(report, 1)
        assert "## Journal (errors/warnings)" in out
        assert "some journal line" in out

    def test_executive_summary_and_recent_logs_survive_a_tiny_budget(self):
        out = build_projection(_REPORT, 1)
        assert "## Executive summary" in out
        assert "cannot start: disk full" in out
        assert "## Recent unit logs" in out

    def test_available_actions_and_charm_links_survive_a_tiny_budget(self):
        # Not named in ARCHITECTURE.md's tier table, but pinned to Tier 1 here
        # because dropping either lets the model invent an action or a URL.
        out = build_projection(_REPORT, 1)
        assert "## Available actions" in out
        assert "## Charm links" in out


class TestTier2OmittedWithMarker:
    def test_tier2_section_named_when_it_does_not_fit(self):
        out = build_projection(_REPORT, 1)
        assert "`Processes` omitted" in out
        assert "## Processes" not in out

    def test_tier2_section_included_when_budget_allows(self):
        out = build_projection(_REPORT, 10**9)
        assert "## Processes" in out
        assert "patroni" in out


class TestTier3AlwaysDigestedNeverRaw:
    def test_disk_and_memory_are_digests_not_raw(self):
        out = build_projection(_REPORT, 10**9)
        assert "## Disk usage" in out
        assert "digested for the prompt" in out
        assert "## Memory" in out

    def test_charm_config_is_a_digest(self):
        out = build_projection(_REPORT, 10**9)
        assert "## Charm config" in out
        assert "digested for the prompt" in out


class TestCrossSectionDeduplication:
    def test_identical_log_line_kept_once(self):
        out = build_projection(_REPORT, 10**9)
        assert out.count("cannot start: disk full") == 1

    def test_unrelated_line_is_not_removed(self):
        out = build_projection(_REPORT, 10**9)
        assert "unrelated startup line" in out


class TestWellFormedOutput:
    def test_fence_markers_balance_at_every_budget(self):
        for budget in (1, 50, 200, 500, 10**9):
            out = build_projection(_REPORT, budget)
            assert out.count("```") % 2 == 0, budget

    def test_fences_are_never_deduplicated_away(self):
        # A regression guard: bare ``` fence lines are structurally identical
        # across every fenced section, so a naive line-level dedup would strip
        # a later section's closing fence the moment an earlier one was seen.
        out = build_projection(_REPORT, 10**9)
        idx = out.find("## Available actions")
        chunk = out[idx : idx + 120]
        assert chunk.count("```") == 2, chunk


class TestUnknownHeadingDefaultsToTier2:
    def test_unlisted_section_is_budget_gated_not_always_kept(self):
        report = _REPORT + "\n## Some Future Section\nnew content here\n"
        out = build_projection(report, 1)
        assert "`Some Future Section` omitted" in out
        assert "new content here" not in out
