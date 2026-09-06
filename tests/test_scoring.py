"""
tests/test_scoring.py — unit tests for core/scoring.py.

Verifies rank_on_leaderboard ordering, format_time output, and is_personal_best logic.
No I/O, no mocks. Pure function tests.
"""

from __future__ import annotations

import pytest

from core.scoring import format_time, is_personal_best, rank_on_leaderboard


# ---------------------------------------------------------------------------
# rank_on_leaderboard
# ---------------------------------------------------------------------------

class TestRankOnLeaderboard:
    def test_empty_leaderboard_is_rank_1(self):
        assert rank_on_leaderboard(30000, []) == 1

    def test_faster_than_all_is_rank_1(self):
        lb = [{"elapsed_ms": 40000}, {"elapsed_ms": 50000}]
        assert rank_on_leaderboard(30000, lb) == 1

    def test_slower_than_one_is_rank_2(self):
        lb = [{"elapsed_ms": 25000}]
        assert rank_on_leaderboard(30000, lb) == 2

    def test_slower_than_two_is_rank_3(self):
        lb = [{"elapsed_ms": 20000}, {"elapsed_ms": 25000}]
        assert rank_on_leaderboard(30000, lb) == 3

    def test_equal_time_is_after_existing_entry(self):
        """Tied time ranks below the existing tie (new run is 'after')."""
        lb = [{"elapsed_ms": 30000}]
        assert rank_on_leaderboard(30000, lb) == 2

    def test_equal_time_two_existing(self):
        lb = [{"elapsed_ms": 30000}, {"elapsed_ms": 30000}]
        assert rank_on_leaderboard(30000, lb) == 3

    def test_mixed_leaderboard(self):
        lb = [
            {"elapsed_ms": 10000},
            {"elapsed_ms": 20000},
            {"elapsed_ms": 30000},
            {"elapsed_ms": 40000},
        ]
        assert rank_on_leaderboard(25000, lb) == 3  # faster than 30000 and 40000

    def test_single_entry_same_time(self):
        lb = [{"elapsed_ms": 60000}]
        assert rank_on_leaderboard(60000, lb) == 2

    def test_rank_1_with_many_slower_entries(self):
        lb = [{"elapsed_ms": t} for t in range(50000, 100000, 1000)]
        assert rank_on_leaderboard(1000, lb) == 1

    def test_last_place_with_many_faster_entries(self):
        lb = [{"elapsed_ms": t} for t in range(10000, 20000, 1000)]
        n = len(lb)
        assert rank_on_leaderboard(99999, lb) == n + 1

    def test_order_independent_of_list_order(self):
        """Rank should be the same regardless of leaderboard ordering."""
        lb_sorted = [{"elapsed_ms": 20000}, {"elapsed_ms": 30000}, {"elapsed_ms": 40000}]
        lb_reversed = list(reversed(lb_sorted))
        lb_shuffled = [{"elapsed_ms": 40000}, {"elapsed_ms": 20000}, {"elapsed_ms": 30000}]
        expected = rank_on_leaderboard(35000, lb_sorted)
        assert rank_on_leaderboard(35000, lb_reversed) == expected
        assert rank_on_leaderboard(35000, lb_shuffled) == expected


# ---------------------------------------------------------------------------
# format_time
# ---------------------------------------------------------------------------

class TestFormatTime:
    def test_zero(self):
        assert format_time(0) == "00:00.000"

    def test_one_second(self):
        assert format_time(1000) == "00:01.000"

    def test_one_minute(self):
        assert format_time(60000) == "01:00.000"

    def test_one_minute_one_second(self):
        assert format_time(61000) == "01:01.000"

    def test_one_minute_one_second_half(self):
        assert format_time(61500) == "01:01.500"

    def test_sub_second(self):
        assert format_time(500) == "00:00.500"

    def test_sub_second_small(self):
        assert format_time(1) == "00:00.001"

    def test_999_ms(self):
        assert format_time(999) == "00:00.999"

    def test_59_seconds_999_ms(self):
        assert format_time(59999) == "00:59.999"

    def test_hour_range(self):
        # 62 minutes 3 seconds 456 ms
        assert format_time(3723456) == "62:03.456"

    def test_exactly_two_minutes(self):
        assert format_time(120000) == "02:00.000"

    def test_minutes_zero_padded(self):
        result = format_time(5000)  # 5 seconds
        assert result.startswith("00:")

    def test_seconds_zero_padded(self):
        result = format_time(60001)  # 1 minute 0 seconds 1 ms
        assert ":00." in result

    def test_ms_zero_padded(self):
        result = format_time(60010)  # 1 minute 0 seconds 10 ms
        assert result.endswith(".010")

    def test_negative_clamped_to_zero(self):
        assert format_time(-100) == "00:00.000"

    def test_format_structure(self):
        """Output must always be exactly MM:SS.mmm (8 characters including colons/dot)."""
        for ms in [0, 1, 999, 1000, 61500, 3723456]:
            result = format_time(ms)
            assert len(result) >= 9, f"format_time({ms}) = {result!r} is too short"
            assert result[2] == ":", f"format_time({ms}) missing colon at pos 2"
            assert result[5] == ".", f"format_time({ms}) missing dot at pos 5"


# ---------------------------------------------------------------------------
# is_personal_best
# ---------------------------------------------------------------------------

class TestIsPersonalBest:
    def test_no_prior_runs_is_personal_best(self):
        assert is_personal_best("p1", 30000, []) is True

    def test_no_prior_runs_for_this_player(self):
        lb = [{"player_id": "p2", "elapsed_ms": 20000}]
        assert is_personal_best("p1", 30000, lb) is True

    def test_faster_than_existing_is_personal_best(self):
        lb = [{"player_id": "p1", "elapsed_ms": 35000}]
        assert is_personal_best("p1", 30000, lb) is True

    def test_equal_to_existing_is_not_personal_best(self):
        lb = [{"player_id": "p1", "elapsed_ms": 30000}]
        assert is_personal_best("p1", 30000, lb) is False

    def test_slower_than_existing_is_not_personal_best(self):
        lb = [{"player_id": "p1", "elapsed_ms": 25000}]
        assert is_personal_best("p1", 30000, lb) is False

    def test_only_considers_this_player(self):
        lb = [
            {"player_id": "p2", "elapsed_ms": 10000},
            {"player_id": "p3", "elapsed_ms": 5000},
        ]
        # p1 has no runs, so this is a personal best regardless of others' times.
        assert is_personal_best("p1", 99000, lb) is True

    def test_multiple_prior_runs_uses_fastest(self):
        lb = [
            {"player_id": "p1", "elapsed_ms": 50000},
            {"player_id": "p1", "elapsed_ms": 40000},
            {"player_id": "p1", "elapsed_ms": 30000},
        ]
        # New time 29999 < 30000 (best) → personal best
        assert is_personal_best("p1", 29999, lb) is True
        # New time 30000 == 30000 (best) → not personal best
        assert is_personal_best("p1", 30000, lb) is False
        # New time 35000 > 30000 (best) → not personal best
        assert is_personal_best("p1", 35000, lb) is False

    def test_mixed_players_leaderboard(self):
        lb = [
            {"player_id": "p1", "elapsed_ms": 60000},
            {"player_id": "p2", "elapsed_ms": 20000},
            {"player_id": "p1", "elapsed_ms": 45000},
        ]
        # p1's best is 45000. New time 44999 < 45000 → personal best.
        assert is_personal_best("p1", 44999, lb) is True
        # p2's best is 20000. New time 19999 < 20000 → personal best.
        assert is_personal_best("p2", 19999, lb) is True
        # p2's best is 20000. New time 20001 > 20000 → not personal best.
        assert is_personal_best("p2", 20001, lb) is False

    def test_single_entry_is_fastest(self):
        lb = [{"player_id": "p1", "elapsed_ms": 30000}]
        assert is_personal_best("p1", 1, lb) is True

    def test_player_id_must_match_exactly(self):
        lb = [{"player_id": "player-1", "elapsed_ms": 10000}]
        # "player-1" vs "player_1" — different IDs, should be personal best for the latter.
        assert is_personal_best("player_1", 99000, lb) is True
