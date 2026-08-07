"""Fixtures shared by the integration tests that build games by hand."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player


@pytest.fixture
async def player(session: AsyncSession) -> Player:
    """A player row to hang games off. The name never reaches Lichess."""
    row = Player(username_lower="tester", display_name="Tester")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
