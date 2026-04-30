"""Test the FallbackSolver chain logic."""

import pytest

from lolo_sinoe.captcha import FallbackSolver
from lolo_sinoe.errors import CaptchaUnsolvable


class _FakeSolver:
    def __init__(self, name: str, *, fail: bool = False, answer: str = "OK") -> None:
        self.name = name
        self._fail = fail
        self._answer = answer
        self.calls = 0

    async def solve(self, image_bytes: bytes) -> str:
        self.calls += 1
        if self._fail:
            raise CaptchaUnsolvable(f"{self.name} configured to fail")
        return self._answer


async def test_first_solver_wins() -> None:
    a = _FakeSolver("A", answer="from-A")
    b = _FakeSolver("B", answer="from-B")
    chain = FallbackSolver([a, b])

    answer = await chain.solve(b"img")
    assert answer == "from-A"
    assert a.calls == 1
    assert b.calls == 0


async def test_falls_back_when_first_fails() -> None:
    a = _FakeSolver("A", fail=True)
    b = _FakeSolver("B", answer="from-B")
    chain = FallbackSolver([a, b])

    answer = await chain.solve(b"img")
    assert answer == "from-B"
    assert a.calls == 1
    assert b.calls == 1


async def test_raises_when_all_fail() -> None:
    a = _FakeSolver("A", fail=True)
    b = _FakeSolver("B", fail=True)
    chain = FallbackSolver([a, b])

    with pytest.raises(CaptchaUnsolvable):
        await chain.solve(b"img")


def test_empty_chain_rejected() -> None:
    with pytest.raises(ValueError):
        FallbackSolver([])


def test_chain_name_concatenates() -> None:
    a = _FakeSolver("first")
    b = _FakeSolver("second")
    chain = FallbackSolver([a, b])
    assert chain.name == "first+second"
