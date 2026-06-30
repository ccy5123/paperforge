import itertools

from paperforge.utils import (
    clean_year,
    collision_suffixes,
    extract_dois,
    first_author_token,
    generate_filename,
    normalize_doi,
)


def test_collision_suffixes_sequence():
    got = list(itertools.islice(collision_suffixes(), 28))
    assert got[:3] == ["a", "b", "c"]
    assert got[25] == "z"
    assert got[26] == "aa"
    assert got[27] == "ab"


def test_normalize_doi_strips_prefixes_and_junk():
    assert normalize_doi("https://doi.org/10.1038/abc") == "10.1038/abc"
    assert normalize_doi("doi:10.1038/abc") == "10.1038/abc"
    assert normalize_doi("  10.1038/abc.  ") == "10.1038/abc"
    assert normalize_doi("") == ""


def test_extract_dois_finds_and_dedupes():
    text = "see 10.1038/x and 10.1038/x plus https://doi.org/10.1234/Y."
    assert extract_dois(text) == ["10.1038/x", "10.1234/Y"]


def test_extract_dois_ignores_short_prefix():
    # registrant code needs >= 4 digits, so this is not a DOI
    assert extract_dois("10.1/y") == []


def test_clean_year():
    assert clean_year("2021-03-01") == "2021"
    assert clean_year("Published 1998") == "1998"
    assert clean_year("n/a") == ""
    assert clean_year("") == ""


def test_first_author_token():
    assert first_author_token("Smith, John; Doe, A") == "Smith"
    assert first_author_token("Jane Roe and John Doe") == "Jane Roe"
    assert first_author_token("") == ""


def test_generate_filename():
    assert generate_filename("Smith, John", "2021-01") == "Smith2021.pdf"
    assert generate_filename("Vaswani", "2017") == "Vaswani2017.pdf"
    assert generate_filename("Vaswani", "") == "Vaswani.pdf"
    assert generate_filename("", "2017") == "2017.pdf"
    assert generate_filename("", "") == "Unknown.pdf"
    assert generate_filename("Unknown", "Unknown") == "Unknown.pdf"
    assert generate_filename("Łukasz", "2020").endswith("2020.pdf")


def test_generate_filename_folds_diacritics_to_ascii():
    # Diacritics are transliterated, not dropped: Könemann -> Konemann (not Knemann).
    assert generate_filename("Könemann, Hans", "1980") == "Konemann1980.pdf"
    assert generate_filename("Müller", "2021") == "Muller2021.pdf"
    assert generate_filename("Ångström", "2020") == "Angstrom2020.pdf"
    assert generate_filename("Gonçalo", "2018") == "Goncalo2018.pdf"
    assert generate_filename("Børseth", "2001") == "Borseth2001.pdf"   # ø has no NFKD form
    assert generate_filename("Łukasz", "2020") == "Lukasz2020.pdf"     # ł has no NFKD form
