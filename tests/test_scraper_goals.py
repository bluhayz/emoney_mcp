"""Tests for get_goals parsing."""

import pytest
from helpers import make_mock_http_session


@pytest.fixture
def http_session():
    return make_mock_http_session(card_responses={2: "card2_goals"})


class TestGetGoals:
    @pytest.mark.asyncio
    async def test_goal_count(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        assert result["goal_count"] == 3

    @pytest.mark.asyncio
    async def test_retirement_goal_present(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        assert len(result["retirement_goals"]) == 1
        retirement = result["retirement_goals"][0]
        assert retirement["name"] == "Retirement"
        assert retirement["start_year"] == 2034
        assert retirement["end_year"] == 2084

    @pytest.mark.asyncio
    async def test_spending_goals_present(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        assert len(result["spending_goals"]) == 2

    @pytest.mark.asyncio
    async def test_parker_adventures_funded(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        parker = next(g for g in result["spending_goals"] if g["name"] == "Parker Adventures")
        assert parker["percent_funded"] == 100.0
        assert parker["on_track"] is True
        assert parker["total_cost"] == 291385

    @pytest.mark.asyncio
    async def test_education_goal_funded(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        edu = next(g for g in result["spending_goals"] if "Education" in g["name"])
        assert edu["percent_funded"] == 100.0
        assert edu["total_cost"] == 621857

    @pytest.mark.asyncio
    async def test_all_on_track_reflects_goals(self, http_session):
        from emoney_mcp.scraper import get_goals
        result = await get_goals(http_session)
        # Retirement has 0% funded so not on track
        assert result["all_on_track"] is False


class TestGoalTypeLabel:
    def test_education_label(self):
        from emoney_mcp.scraper import _goal_type_label
        assert _goal_type_label(0) == "Education"

    def test_retirement_label(self):
        from emoney_mcp.scraper import _goal_type_label
        assert _goal_type_label(1) == "Retirement"

    def test_other_spending_label(self):
        from emoney_mcp.scraper import _goal_type_label
        assert _goal_type_label(2) == "Other Spending"

    def test_unknown_label(self):
        from emoney_mcp.scraper import _goal_type_label
        assert _goal_type_label(99) == "Unknown"
