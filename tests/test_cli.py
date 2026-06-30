import pytest

from paperforge.cli import _build_config, build_parser, parse_args


def test_bib_subcommand_is_bib_only():
    cfg = _build_config(parse_args(["bib", "dois.txt", "--email", "e@x.org"]))
    assert cfg.generate_bib is True
    assert cfg.download_pdfs is False


def test_download_subcommand_is_download_only():
    cfg = _build_config(parse_args(["download", "dois.txt"]))
    assert cfg.download_pdfs is True
    assert cfg.generate_bib is False


def test_all_subcommand_does_both():
    cfg = _build_config(parse_args(["all", "dois.txt"]))
    assert cfg.download_pdfs is True
    assert cfg.generate_bib is True


def test_bare_inputs_default_to_all_for_back_compat():
    args = parse_args(["dois.txt", "--email", "e@x.org"])
    assert args.command == "all"
    assert args.inputs == ["dois.txt"]


def test_flag_first_bare_invocation_still_defaults_to_all():
    args = parse_args(["--email", "e@x.org", "refs.xlsx", "10.1038/x"])
    assert args.command == "all"
    assert args.inputs == ["refs.xlsx", "10.1038/x"]


def test_download_options_available_on_download_and_all():
    args = parse_args(["download", "refs.csv", "--licenses", "cc-by", "--overwrite"])
    assert args.licenses == "cc-by"
    assert args.overwrite is True


def test_bib_subcommand_rejects_download_only_flags():
    # --licenses / --overwrite belong to the download phase, not bib.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bib", "dois.txt", "--licenses", "cc-by"])


def test_shared_options_on_every_subcommand():
    for cmd in ("bib", "download", "all"):
        args = parse_args([cmd, "dois.txt", "-o", "out", "--email", "e@x.org"])
        assert args.output == "out"
        assert args.email == "e@x.org"
