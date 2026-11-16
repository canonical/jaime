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

# A budget one byte below the report size: the report does not fit, so the
# projection runs, but there is enough left over for every Tier 2 section.
_OVER_BUDGET = len(_REPORT.encode()) - 1


class TestNoOp:
    def test_zero_or_negative_budget_is_unchanged(self):
        assert build_projection(_REPORT, 0) == _REPORT
        assert build_projection(_REPORT, -1) == _REPORT


class TestDeterminism:
    def test_same_inputs_byte_identical_output(self):
        a = build_projection(_REPORT, _OVER_BUDGET)
        b = build_projection(_REPORT, _OVER_BUDGET)
        assert a == b

    def test_report_that_fits_the_budget_is_unchanged(self):
        # A report that already fits is a no-op even at a huge budget: there is
        # nothing to bound, so nothing is capped or digested.
        assert build_projection(_REPORT, len(_REPORT.encode())) == _REPORT
        assert build_projection(_REPORT, 10**9) == _REPORT

    def test_over_budget_report_is_projected(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
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
        # A large digestible (Tier 3) section makes the report exceed the
        # budget while leaving room for Tier 2: the digest is small however
        # many raw lines it covers.
        big = _REPORT + "\n## Network connections (listening + active)\n```\n" + "\n".join(
            f"tcp LISTEN 0 128 0.0.0.0:{1000 + i} 0.0.0.0:*" for i in range(200)
        ) + "\n```\n"
        out = build_projection(big, 2000)
        assert "## Processes" in out
        assert "patroni" in out


class TestTier3AlwaysDigestedNeverRaw:
    def test_disk_and_memory_are_digests_not_raw(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert "## Disk usage" in out
        assert "digested for the prompt" in out
        assert "## Memory" in out

    def test_charm_config_is_a_digest(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert "## Charm config" in out
        assert "digested for the prompt" in out

    def test_digest_marker_does_not_point_at_the_full_report(self):
        # The model never receives the full report, so the marker must not
        # imply it can consult one.
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert "full detail omitted" in out
        assert "full report" not in out


class TestCrossSectionDeduplication:
    def test_identical_log_line_kept_once(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert out.count("cannot start: disk full") == 1

    def test_unrelated_line_is_not_removed(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert "unrelated startup line" in out

    def test_line_truncated_from_a_section_still_appears_later(self):
        # The first occurrence sits past the Tier 1 per-section cap, so it is
        # not emitted; the later occurrence must still survive. Recording it as
        # seen before the cap would silently drop it from the prompt entirely.
        filler = "\n".join(f"2026-01-01 00:00:00 ERROR filler line {i}" for i in range(400))
        report = (
            "# Incident Report\n- unit: foo/0\n\n"
            "## Journal (errors/warnings)\n```\n" + filler + "\nUNIQUE-SIGNAL-LINE\n```\n\n"
            "## Recent unit logs\n```\nUNIQUE-SIGNAL-LINE\n```\n"
        )
        out = build_projection(report, len(report.encode()) - 1)
        assert "bytes omitted" in out
        assert out.count("UNIQUE-SIGNAL-LINE") == 1

    def test_digested_line_outside_preview_does_not_suppress_later_line(self):
        # A Tier 3 digest previews only a few lines; a line beyond the preview
        # is not emitted, so it must not suppress a later real occurrence.
        report = (
            "# Incident Report\n- unit: foo/0\n\n"
            "## Disk usage\n```\nline-1\nline-2\nline-3\nUNIQUE-DIGEST-LINE\n```\n\n"
            "## Recent unit logs\n```\nUNIQUE-DIGEST-LINE\n```\n"
        )
        out = build_projection(report, len(report.encode()) - 1)
        assert out.count("UNIQUE-DIGEST-LINE") == 1


class TestWellFormedOutput:
    def test_fence_markers_balance_at_every_budget(self):
        for budget in (1, 50, 200, 500, _OVER_BUDGET):
            out = build_projection(_REPORT, budget)
            assert out.count("```") % 2 == 0, budget

    def test_fences_are_never_deduplicated_away(self):
        # A regression guard: bare ``` fence lines are structurally identical
        # across every fenced section, so a naive line-level dedup would strip
        # a later section's closing fence the moment an earlier one was seen.
        out = build_projection(_REPORT, _OVER_BUDGET)
        idx = out.find("## Available actions")
        chunk = out[idx : idx + 120]
        assert chunk.count("```") == 2, chunk

    def test_capped_fenced_section_stays_balanced(self):
        # A Tier 1 section larger than the per-section cap must not lose its
        # closing fence to the truncation, or every later section renders as
        # part of that code block.
        report = _REPORT + "\n## Journal (errors/warnings)\n```\n" + "\n".join(
            f"2026-01-01 00:00:{i % 60:02d} ERROR failure line {i}" for i in range(400)
        ) + "\n```\n"
        out = build_projection(report, len(report.encode()) - 1)
        assert out.count("```") % 2 == 0
        assert "bytes omitted" in out


class TestConfigOptionDedupBySummaryVsCharmConfig:
    def test_changed_option_named_in_summary_does_not_repeat_in_charm_config(self):
        out = build_projection(_REPORT, _OVER_BUDGET)
        # The summary's bullet line survives (Tier 1, verbatim).
        assert "- `max_connections`: `50` (default: `100`)" in out
        # The differently-formatted repeat in the Charm config digest does not.
        assert "max_connections: 50" not in out

    def test_unchanged_option_still_appears_in_charm_config(self):
        # "port" was never named as changed in the summary, so it is not
        # touched by the by-name dedup and survives the digest normally.
        out = build_projection(_REPORT, _OVER_BUDGET)
        assert "port: 5432" in out

    def test_no_effect_when_summary_names_no_changed_options(self):
        report = _REPORT.replace(
            "**Config changed from default by operator:**\n- `max_connections`: `50` (default: `100`)\n\n", ""
        )
        out = build_projection(report, _OVER_BUDGET)
        assert "max_connections: 50" in out


class TestKubernetesEventsWarningKeptNormalCounted:
    _REPORT_WITH_EVENTS = _REPORT + (
        "\n## Kubernetes events\n"
        "```\n"
        "2026-01-01T00:00:00Z Warning BackOff Back-off restarting failed container ×14\n"
        "2026-01-01T00:00:01Z Normal Pulled Container image pulled\n"
        "2026-01-01T00:00:02Z Normal Started Started container\n"
        "2026-01-01T00:00:03Z Warning Unhealthy Readiness probe failed\n"
        "```\n"
    )

    def test_warning_lines_survive_verbatim(self):
        out = build_projection(self._REPORT_WITH_EVENTS, _OVER_BUDGET)
        assert "BackOff" in out
        assert "×14" in out
        assert "Unhealthy" in out

    def test_normal_lines_collapse_to_one_count_line(self):
        out = build_projection(self._REPORT_WITH_EVENTS, _OVER_BUDGET)
        assert "Pulled" not in out
        assert "Started container" not in out
        assert "2 Normal event(s) not shown individually." in out

    def test_unrecognized_line_shape_is_kept_not_dropped(self):
        report = _REPORT + "\n## Kubernetes events\n```\nsome garbled line\n```\n"
        out = build_projection(report, _OVER_BUDGET)
        assert "some garbled line" in out

    def test_survives_a_tiny_budget_like_any_tier1_section(self):
        out = build_projection(self._REPORT_WITH_EVENTS, 1)
        assert "BackOff" in out
        assert "2 Normal event(s) not shown individually." in out


class TestUnknownHeadingDefaultsToTier2:
    def test_unlisted_section_is_budget_gated_not_always_kept(self):
        report = _REPORT + "\n## Some Future Section\nnew content here\n"
        out = build_projection(report, 1)
        assert "`Some Future Section` omitted" in out
        assert "new content here" not in out
