from paperforge.config import Config
from paperforge.metadata import _surname, fetch_metadata


class FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data

    def json(self):
        return self._json


class FakeSession:
    """Routes a request to a canned response by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        for sub, resp in self.routes.items():
            if sub in url:
                return resp
        return FakeResp(404, {})


def test_surname():
    assert _surname("Ashish Vaswani") == "Vaswani"
    assert _surname("Madonna") == "Madonna"
    assert _surname("") == ""


def test_openalex_is_primary():
    sess = FakeSession({"openalex.org": FakeResp(200, {
        "authorships": [{"author": {"display_name": "Jane Q Roe"}}],
        "publication_year": 2019,
        "title": "A Title",
    })})
    md = fetch_metadata("10.1234/x", sess, Config(unpaywall_email="e@e.org"))
    assert (md.author, md.year, md.title) == ("Roe", "2019", "A Title")
    assert not any("crossref" in u for u in sess.calls)   # didn't need the fallback


def test_crossref_fallback_when_openalex_empty():
    sess = FakeSession({
        "openalex.org": FakeResp(200, {}),                 # empty -> fall through
        "crossref.org": FakeResp(200, {"message": {
            "author": [{"family": "Smith", "given": "A"}],
            "issued": {"date-parts": [[2008, 5]]},
            "title": ["Hello World"],
        }}),
    })
    md = fetch_metadata("10.1234/x", sess, Config(unpaywall_email="e@e.org"))
    assert (md.author, md.year, md.title) == ("Smith", "2008", "Hello World")


def test_never_raises_on_failure():
    md = fetch_metadata("10.1234/x", FakeSession({}), Config())   # all 404
    assert md.is_empty
