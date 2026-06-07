"""Tests for get_accounts and get_retirement_accounts parsing."""

import pytest
from helpers import load_fixture, make_mock_http_session


@pytest.fixture
def http_session():
    return make_mock_http_session(
        card_responses={9: "card9_networth", 1: "card1_accounts"},
    )


class TestGetAccounts:
    @pytest.mark.asyncio
    async def test_returns_net_worth(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        assert result["net_worth"] == 8470161.0

    @pytest.mark.asyncio
    async def test_returns_assets_and_liabilities(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        assert result["total_assets"] == 8905161.0
        assert result["total_liabilities"] == 435000.0

    @pytest.mark.asyncio
    async def test_account_groups_present(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        groups = result["account_groups"]
        group_names = [g["group"] for g in groups]
        assert "Taxable" in group_names
        assert "Tax Advantaged" in group_names
        assert "Debt" in group_names

    @pytest.mark.asyncio
    async def test_account_count(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        # 2 taxable + 7 tax advantaged + 1 debt = 10
        assert result["account_count"] == 10

    @pytest.mark.asyncio
    async def test_accounts_include_id(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        taxable = next(g for g in result["account_groups"] if g["group"] == "Taxable")
        acct = taxable["accounts"][0]
        assert acct["id"] == "4f6bbea8-8551-4c77-b22f-bb8003e0ac80"

    @pytest.mark.asyncio
    async def test_group_total_is_sum_of_accounts(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        for group in result["account_groups"]:
            expected = sum(a["balance"] for a in group["accounts"] if a["balance"] is not None)
            assert group["total"] == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_date_truncated_to_10_chars(self, http_session):
        from emoney_mcp.scraper import get_accounts
        result = await get_accounts(http_session)
        for group in result["account_groups"]:
            for acct in group["accounts"]:
                if acct["as_of"]:
                    assert len(acct["as_of"]) == 10


class TestGetRetirementAccounts:
    @pytest.fixture
    def http_session(self):
        return make_mock_http_session(
            card_responses={9: "card9_networth", 1: "card1_accounts"},
        )

    @pytest.mark.asyncio
    async def test_retirement_total(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        # annuities (1111408 + 527398 + 304258 + 50399 + 55558 + 14717) + oracle 401k 92583
        assert result["total_retirement_assets"] > 0

    @pytest.mark.asyncio
    async def test_annuities_bucketed(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        assert result["retirement_breakdown"]["annuities"] == pytest.approx(1638806.0)

    @pytest.mark.asyncio
    async def test_529_bucketed(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        assert result["retirement_breakdown"]["education_529"] == pytest.approx(304258.0)

    @pytest.mark.asyncio
    async def test_hsa_bucketed(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        # HSA account (14717) + TaxFreeHealthSavingsAsset matches "hsa" keyword
        assert result["retirement_breakdown"]["hsa"] > 0

    @pytest.mark.asyncio
    async def test_debt_not_in_retirement(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        names = [a["name"] for a in result["retirement_accounts"]]
        assert not any("Mortgage" in n for n in names)

    @pytest.mark.asyncio
    async def test_taxable_assets_positive(self, http_session):
        from emoney_mcp.scraper import get_retirement_accounts
        result = await get_retirement_accounts(http_session)
        assert result["total_taxable_assets"] > 0
