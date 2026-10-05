"""Unit tests for jaime.logutils (log filtering and de-duplication)."""

from jaime.logutils import deduplicate_lines, filter_error_context, line_pattern


class TestLinePattern:
    def test_strips_timestamps_numbers_and_paths(self):
        a = "2026-10-04 19:13:48 WARNING unit.openbao/0.juju-log server.go:405 No relations found for tls-certificates-access"
        b = "2026-10-04 19:14:02 WARNING unit.openbao/0.juju-log server.go:405 No relations found for tls-certificates-access"
        assert line_pattern(a) == line_pattern(b)

    def test_distinct_messages_have_distinct_patterns(self):
        a = "2026-10-04 19:13:48 WARNING unit.openbao/0.juju-log server.go:405 Approle secret not yet created"
        b = "2026-10-04 19:13:48 WARNING unit.openbao/0.juju-log server.go:405 No relations found for tls-certificates-pki"
        assert line_pattern(a) != line_pattern(b)


class TestDeduplicateLines:
    def test_consecutive_run_collapsed(self):
        lines = ["2026-10-04 19:00:00 WARNING a boom"] * 5
        out = deduplicate_lines(lines)
        assert len(out) == 2
        assert "similar lines omitted" in out[1]

    def test_interleaved_cycle_collapsed_by_frequency(self):
        # A message that recurs every few lines but never as a clean run or
        # aligned block (the drift breaks block detection), so only the
        # frequency pass can collapse it.
        lines = [
            "2026-10-04 19:00:00 WARNING boom",
            "2026-10-04 19:00:01 INFO first",
            "2026-10-04 19:00:02 WARNING boom",
            "2026-10-04 19:00:03 INFO second",
            "2026-10-04 19:00:04 INFO third",
            "2026-10-04 19:00:05 WARNING boom",
        ]
        out = deduplicate_lines(lines)
        assert out[0] == lines[0]
        assert any("similar lines omitted" in line for line in out)
        # "boom" appears once as a sample; the INFO lines are kept verbatim.
        boom_samples = [line for line in out if "boom" in line]
        assert len(boom_samples) == 1

    def test_periodic_block_collapsed_by_runs(self):
        cycle = [
            "2026-10-04 19:00:00 WARNING a first",
            "2026-10-04 19:00:00 WARNING b second",
            "2026-10-04 19:00:00 WARNING c third",
        ]
        out = deduplicate_lines(cycle * 4)
        samples = [line for line in out if not line.strip().startswith("…")]
        assert len(samples) == 3
        assert any("3 similar repetitions omitted" in line for line in out)

    def test_one_off_line_never_collapsed(self):
        lines = ["2026-10-04 19:00:00 WARNING a unique", "2026-10-04 19:00:01 INFO quiet"]
        out = deduplicate_lines(lines)
        assert out == lines

    def test_order_of_first_occurrences_preserved(self):
        lines = [
            "2026-10-04 19:00:00 WARNING zeta",
            "2026-10-04 19:00:01 WARNING alpha",
            "2026-10-04 19:00:02 WARNING zeta",
            "2026-10-04 19:00:03 WARNING alpha",
            "2026-10-04 19:00:04 WARNING zeta",
        ]
        out = deduplicate_lines(lines)
        samples = [line for line in out if not line.strip().startswith("…")]
        assert "zeta" in samples[0]
        assert "alpha" in samples[1]


class TestFilterErrorContext:
    def test_keeps_warning_with_context_window(self):
        lines = ["INFO a", "INFO b", "WARNING boom", "INFO c", "INFO d"]
        out = filter_error_context(lines, max_lines=100, context_window=1)
        assert any("WARNING boom" in line for line in out)

    def test_falls_back_to_tail_when_no_match(self):
        lines = ["INFO a", "INFO b", "INFO c"]
        out = filter_error_context(lines, max_lines=100)
        assert out == lines
