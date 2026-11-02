import pytest

from src.newsroom.normalize import looks_like_handle, normalize_alias, raw_key


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  DAMAC   Properties ", "damac properties"),
        ("Emaar Properties PJSC", "emaar properties pjsc"),  # nothing is stripped
        ("Majid Al-Futtaim", "majid al futtaim"),
        ("Riyadh Holding Co.", "riyadh holding co"),
        ("White & Co", "white co"),
        ("bnw.developments", "bnw developments"),
        ("Retal_sa", "retal sa"),
        ("WaslGroup", "waslgroup"),  # spaces are never removed, so this stays different from "wasl group"
        ("دبي", "دبي"),
        ("أبوظبي", "ابوظبي"),  # alef with hamza -> alef
        ("مَكَّة", "مكة"),  # short vowels removed
        ("", ""),
    ],
)
def test_normalize_alias(text, expected):
    assert normalize_alias(text) == expected


def test_group_and_properties_are_not_merged():
    assert normalize_alias("Emaar Group") != normalize_alias("Emaar Properties")
    assert normalize_alias("Wasl Group") != normalize_alias("WaslGroup")


def test_raw_key_keeps_punctuation():
    # The ignore list compares original strings, so a handle never blocks the real company.
    assert raw_key("bnw.developments") != raw_key("BNW Developments")
    assert raw_key("UAE PASS") == raw_key("UAE Pass")


@pytest.mark.parametrize(
    "text",
    ["@Momah_SA", "Momah_SA", "www.example.com", "sustainablehomes.ae", "PanicSelling.com", "smartmove.dubai", "https://x.com/a"],
)
def test_handles_detected(text):
    assert looks_like_handle(text)


@pytest.mark.parametrize(
    "text",
    ["Stake", "BNW Developments", "Riyadh Holding Co.", "Brookfield Asset Management Ltd.", "Better Homes", "betterhomes", "Expo City Dubai"],
)
def test_real_names_not_treated_as_handles(text):
    assert not looks_like_handle(text)
