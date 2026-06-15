import pytest

from paperforge.inputs import load_records


def test_csv_with_doi_header(tmp_path):
    p = tmp_path / "refs.csv"
    p.write_text(
        "Title,DOI,Year,Authors\n"
        "Paper A,10.1038/aaa,2020,Smith J\n"
        "Paper B,https://doi.org/10.1234/bbb,2019-05,Doe A\n",
        encoding="utf-8",
    )
    recs = load_records(p)
    assert [r.doi for r in recs] == ["10.1038/aaa", "10.1234/bbb"]
    assert recs[0].title == "Paper A"
    assert recs[0].year == "2020"
    assert recs[0].author == "Smith J"
    assert recs[1].year == "2019"          # date cleaned to a bare year


def test_csv_without_doi_header_scans_cells(tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text("foo,bar\nhello,10.1038/zzz\n", encoding="utf-8")
    recs = load_records(p)
    assert [r.doi for r in recs] == ["10.1038/zzz"]


def test_txt_lines(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text(
        "10.1038/aaa\ngarbage line\nhttps://doi.org/10.1234/bbb\n",
        encoding="utf-8",
    )
    recs = load_records(p)
    assert [r.doi for r in recs] == ["10.1038/aaa", "10.1234/bbb"]


def test_legacy_xls_rejected(tmp_path):
    p = tmp_path / "old.xls"
    p.write_text("dummy", encoding="utf-8")
    with pytest.raises(ValueError):
        load_records(p)


def test_xlsx_roundtrip(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "refs.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["DOI", "Year", "Authors"])
    ws.append(["10.1038/aaa", 2020, "Smith J"])
    ws.append(["not-a-doi", 2019, "Doe A"])
    wb.save(p)

    recs = load_records(p)
    assert [r.doi for r in recs] == ["10.1038/aaa"]
    assert recs[0].year == "2020"
    assert recs[0].author == "Smith J"
