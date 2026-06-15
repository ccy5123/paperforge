from paperforge.utils import (
    clean_year,
    extract_dois,
    first_author_token,
    generate_filename,
    normalize_doi,
)


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
