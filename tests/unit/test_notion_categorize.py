"""The grocery keyword map. Collisions are the whole point of this file."""

from __future__ import annotations

import pytest

from Jarvis.integrations.notion import categorize

CATEGORIES = {"Produce", "Dairy", "Meat", "Pantry", "Frozen", "Household", "Other"}


@pytest.mark.parametrize(
    "item, category",
    [
        # --- keyword collisions: the longer keyword has to win -------------
        ("ice cream", "Frozen"),        # not Dairy, despite "cream"
        ("mint chocolate chip ice cream", "Frozen"),
        ("toilet paper", "Household"),  # not Pantry, despite the "oil" inside "toilet"
        ("eggplant", "Produce"),        # not Dairy, despite "egg"
        ("steak", "Meat"),
        ("sour cream", "Dairy"),
        ("frozen pizza", "Frozen"),
        ("paper towels", "Household"),
        ("peanut butter", "Pantry"),    # not Dairy, despite "butter"
        # --- plain lookups --------------------------------------------------
        ("milk", "Dairy"),
        ("2% milk", "Dairy"),
        ("bananas", "Produce"),
        ("chicken breast", "Meat"),
        ("rice", "Pantry"),
        ("dish soap", "Household"),
        ("MILK", "Dairy"),              # case-insensitive
        # --- fallback -------------------------------------------------------
        ("printer cartridge", "Other"),
        ("", "Other"),
    ],
)
def test_categorize(item, category):
    assert categorize(item) == category


@pytest.mark.parametrize("item", ["milk", "ice cream", "eggplant", "wombat"])
def test_categorize_only_returns_plan_categories(item):
    assert categorize(item) in CATEGORIES
