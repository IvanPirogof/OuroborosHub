"""science_search through plugin.register with a fake PluginAPI and httpx.MockTransport (offline)."""
from __future__ import annotations

import email.utils
import importlib.util
import json
import os
import pathlib
import sys
import time
from typing import Any, Callable, Dict, List

import httpx
import pytest

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
MODULE = "science_search_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(MODULE, SKILL_DIR / "plugin.py", submodule_search_locations=[str(SKILL_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
P = plugin.P
transport = sys.modules[f"{MODULE}.transport"]


class FakeAPI:
    def __init__(self, state_dir: pathlib.Path | None, settings: Dict[str, str]):
        self.state_dir, self.settings, self.tools, self.unload = state_dir, settings, {}, None

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        assert len(name) <= 24 and name.replace("_", "").isalnum() and description and schema["type"] == "object"
        self.tools[name] = {"handler": handler, "timeout_sec": timeout_sec, "schema": schema}

    def on_unload(self, callback):
        self.unload = callback

    def get_state_dir(self):
        if self.state_dir is None:
            raise RuntimeError("no state directory on this host")
        return str(self.state_dir)

    def get_settings(self, keys):
        assert list(keys) == plugin.SETTINGS_KEYS
        return {k: v for k, v in self.settings.items() if k in keys}


class Backend:
    """Host-routed canned responses; a list of responders answers one request each, in order."""

    def __init__(self):
        self.requests: List[httpx.Request] = []
        self.sleeps: List[float] = []
        self.routes: Dict[tuple, Any] = {}

    def route(self, host: str, path: str, responder: Any) -> None:
        self.routes[(host, path)] = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        matches = [(len(path), key) for key in self.routes for host, path in [key]
                   if request.url.host == host and request.url.path.startswith(path)]
        if not matches:
            return httpx.Response(404, json={"error": f"no fixture for {request.url}"})
        responder = self.routes[max(matches)[1]]
        if isinstance(responder, list):
            responder = responder.pop(0) if len(responder) > 1 else responder[0]
        return responder(request)

    def sent(self, host: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


def reply(data: Any = None, status: int = 200, headers: Dict[str, str] | None = None, text: str | None = None) -> Callable:
    def responder(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(status, json=data, headers=headers)
    return responder


def raising(exc_type):
    def responder(request):
        raise exc_type("fixture failure", request=request)
    return responder


def crossref(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"status": "ok", "message-type": "work-list", "message": {"items": items, "total-results": len(items)}}


OPENALEX_WORK = {
    "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/ABC", "title": "Deep Widgets", "publication_year": 2021,
    "authorships": [{"author": {"display_name": "Ada Example"}}, {"author": {"display_name": "Bo Sample"}}],
    "primary_location": {"source": {"display_name": "Journal of Widgets"}, "landing_page_url": "https://example.org/w1"},
    "cited_by_count": 42, "abstract_inverted_index": {"Widgets": [0], "deep.": [2], "are": [1]},
    "open_access": {"oa_url": "https://example.org/w1.pdf"}, "referenced_works": ["https://openalex.org/W2"],
}
OPENALEX_OTHER = {**OPENALEX_WORK, "id": "https://openalex.org/W2", "doi": None, "title": "Widget Theory", "referenced_works": []}
CROSSREF_ITEM = {
    "DOI": "10.1000/abc", "title": ["Deep Widgets"], "author": [{"given": "Ada", "family": "Example"}],
    "issued": {"date-parts": [[2021, 5]]}, "container-title": ["Journal of Widgets"], "URL": "https://doi.org/10.1000/abc",
    "abstract": "<jats:p>Widgets are deep.</jats:p>", "is-referenced-by-count": 40,
}
CROSSREF_OTHER = {**CROSSREF_ITEM, "DOI": "10.1000/xyz", "title": ["Shallow Gadgets"], "abstract": None}
ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v2</id>
    <title>Deep  Widgets:\n a preprint</title>
    <summary>Widgets, deeply.</summary>
    <author><name>Ada Example</name></author><author><name>Bo Sample</name></author>
    <published>2021-01-02T00:00:00Z</published>
    <arxiv:doi>10.1000/abc</arxiv:doi>
    <arxiv:journal_ref>Widget Letters 3</arxiv:journal_ref>
  </entry>
</feed>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(plugin, "_transport", httpx.MockTransport(backend))
    monkeypatch.setattr(transport.time, "sleep", backend.sleeps.append)
    for source in list(P.INTERVAL):
        monkeypatch.setitem(P.INTERVAL, source, 0.0)
    holder: Dict[str, Any] = {"backend": backend, "state": tmp_path / "state"}

    def use(settings: Dict[str, str] | None = None, api: FakeAPI | None = None) -> FakeAPI:
        api = api or FakeAPI(holder["state"], settings or {})
        plugin.register(api)
        holder["api"] = api
        return api

    holder["use"] = use
    yield holder
    if "api" in holder:
        holder["api"].unload()


def call(env, tool, **kwargs):
    raw = env["api"].tools[tool]["handler"](None, **kwargs)
    assert isinstance(raw, str)
    return json.loads(raw)


def test_registration_timeouts_sit_above_deadlines_and_below_the_host_cap(env):
    api = env["use"]()
    assert set(api.tools) == {"literature_search", "work_lookup", "citations"}
    for name, tool in api.tools.items():
        assert plugin.DEADLINE_SEC[name] < tool["timeout_sec"] <= 300
    assert api.tools["literature_search"]["schema"]["required"] == ["query"]


def test_articles_merge_sources_dedupe_by_doi_and_report_statuses(env):
    backend, api = env["backend"], env["use"]()
    backend.route("api.openalex.org", "/works", reply({"meta": {"count": 2}, "results": [OPENALEX_WORK, OPENALEX_OTHER]}))
    backend.route("api.crossref.org", "/works", reply(crossref([CROSSREF_ITEM, CROSSREF_OTHER])))
    answer = call(env, "literature_search", query="deep widgets")
    assert answer["ok"] is True and answer["kind"] == "articles" and answer["count"] == 3
    first = answer["results"][0]
    assert first == {"source": "openalex", "id": "https://openalex.org/W1", "title": "Deep Widgets",
                     "authors": ["Ada Example", "Bo Sample"], "year": 2021, "venue": "Journal of Widgets", "doi": "10.1000/abc",
                     "url": "https://example.org/w1", "abstract": "Widgets are deep.", "citation_count": 42,
                     "open_access_url": "https://example.org/w1.pdf", "also_in": ["crossref"]}
    assert [r["source"] for r in answer["results"]] == ["openalex", "openalex", "crossref"]
    assert answer["results"][2]["doi"] == "10.1000/xyz" and answer["results"][2]["abstract"] is None
    assert answer["sources"] == {"openalex": {"status": "ok", "count": 2}, "crossref": {"status": "ok", "count": 2}}
    assert answer["keys_configured"] == {k: False for k in plugin.SETTINGS_KEYS}
    assert "settings_error" not in answer
    openalex = backend.sent("api.openalex.org")[0]
    assert openalex.headers["user-agent"] == "Ouroboros-science_search/0.1"
    assert openalex.url.params["search"] == "deep widgets" and openalex.url.params["per-page"] == "5"
    assert "mailto" not in openalex.url.params and "api_key" not in openalex.url.params
    crossref_request = backend.sent("api.crossref.org")[0]
    assert crossref_request.url.params["rows"] == "5" and crossref_request.url.params["offset"] == "0"
    assert "filter" not in crossref_request.url.params


def test_crossref_select_list_only_names_selectable_fields(env):
    """Crossref answers HTTP 400 to a select list with an unknown field; ``institution`` is one (verified live)."""
    backend = env["backend"]
    env["use"]()
    backend.route("api.crossref.org", "/works", reply(crossref([CROSSREF_ITEM])))
    call(env, "literature_search", query="widgets", sources=["crossref"])
    selected = backend.sent("api.crossref.org")[0].url.params["select"].split(",")
    assert "institution" not in selected
    assert set(selected) <= P.CROSSREF_SELECTABLE and set(selected) == set(P.CROSSREF_SELECT)


def test_configured_keys_are_sent_but_never_echoed(env):
    backend = env["backend"]
    env["use"]({"OPENALEX_API_KEY": "oa-secret", "SCIENCE_SEARCH_MAILTO": "owner@example.org",
                "SEMANTIC_SCHOLAR_API_KEY": "s2-secret"})
    # offset 6 + limit 3 is served as one OpenAlex page of 9 with the first 6 skipped
    backend.route("api.openalex.org", "/works", reply({"results": [OPENALEX_OTHER] * 6 + [OPENALEX_WORK]}))
    backend.route("api.crossref.org", "/works", reply(crossref([])))
    backend.route("api.semanticscholar.org", "/graph/v1/paper/search", reply({"data": [{
        "paperId": "p1", "title": "Deep Widgets", "authors": [{"name": "Ada Example"}], "year": 2021, "venue": "JW",
        "externalIds": {"DOI": "10.1000/ABC"}, "abstract": "Widgets.", "citationCount": 7, "url": "https://s2/p1"}]}))
    raw = env["api"].tools["literature_search"]["handler"](None, query="deep widgets", year_from=2019, year_to=2022, limit=3, offset=6)
    answer = json.loads(raw)
    assert answer["ok"] and set(answer["sources"]) == {"openalex", "crossref", "semantic_scholar"}
    assert answer["count"] == 1 and answer["results"][0]["also_in"] == ["semantic_scholar"]
    assert answer["sources"]["openalex"]["count"] == 1
    assert answer["keys_configured"] == {"OPENALEX_API_KEY": True, "NCBI_API_KEY": False, "SEMANTIC_SCHOLAR_API_KEY": True,
                                         "CORE_API_KEY": False, "SCIENCE_SEARCH_MAILTO": True}
    assert "secret" not in raw
    openalex = backend.sent("api.openalex.org")[0]
    assert openalex.url.params["api_key"] == "oa-secret" and openalex.url.params["mailto"] == "owner@example.org"
    assert openalex.url.params["filter"] == "from_publication_date:2019-01-01,to_publication_date:2022-12-31"
    assert openalex.url.params["per-page"] == "9" and openalex.url.params["page"] == "1"
    assert openalex.headers["user-agent"] == "Ouroboros-science_search/0.1 (mailto:owner@example.org)"
    crossref_request = backend.sent("api.crossref.org")[0]
    assert crossref_request.url.params["filter"] == "from-pub-date:2019,until-pub-date:2022" and crossref_request.url.params["offset"] == "6"
    s2 = backend.sent("api.semanticscholar.org")[0]
    assert s2.headers["x-api-key"] == "s2-secret" and s2.url.params["year"] == "2019-2022" and s2.url.params["limit"] == "3"


def test_redirects_are_never_followed_so_keys_stay_with_their_host(env):
    backend = env["backend"]
    env["use"]({"SEMANTIC_SCHOLAR_API_KEY": "s2-secret", "CORE_API_KEY": "core-secret"})
    backend.route("api.semanticscholar.org", "/graph/v1/paper/search",
                  reply(status=302, headers={"Location": "https://elsewhere.example/collect"}))
    backend.route("elsewhere.example", "/", reply({"data": []}))
    backend.route("api.openalex.org", "/works", reply(status=301, headers={"Location": "http://api.openalex.org/works"}))
    backend.route("api.crossref.org", "/works", reply(crossref([CROSSREF_ITEM])))
    answer = call(env, "literature_search", query="widgets")
    assert answer["ok"] and answer["sources"]["crossref"]["status"] == "ok"
    for source in ("semantic_scholar", "openalex"):
        assert answer["sources"][source]["status"] == "bad_response" and "redirect" in answer["sources"][source]["message"]
    assert answer["sources"]["semantic_scholar"]["http_status"] == 302
    assert backend.sent("elsewhere.example") == [] and len(backend.sent("api.openalex.org")) == 1
    assert all(r.url.host != "elsewhere.example" for r in backend.requests)
    backend.route("api.core.ac.uk", "/v3/search/works/", reply(status=307, headers={"Location": "https://elsewhere.example/"}))
    answer = call(env, "literature_search", query="widgets", kind="open_access", sources=["core"])
    assert answer["sources"]["core"]["status"] == "bad_response" and backend.sent("elsewhere.example") == []


def test_openalex_paging_preserves_unaligned_offsets(env):
    backend = env["backend"]
    env["use"]()

    def ranked(request: httpx.Request) -> httpx.Response:
        page, per_page = int(request.url.params["page"]), int(request.url.params["per-page"])
        first = (page - 1) * per_page
        return httpx.Response(200, json={"results": [{**OPENALEX_OTHER, "id": f"https://openalex.org/W{first + i}"}
                                                     for i in range(per_page)]})

    backend.route("api.openalex.org", "/works", ranked)
    for offset, limit in ((201, 25), (199, 5), (210, 25), (1000, 7), (0, 25), (175, 25), (176, 25)):
        backend.requests.clear()
        answer = call(env, "literature_search", query=f"ranks {offset} {limit}", sources=["openalex"], offset=offset, limit=limit)
        ranks = [int(r["id"].rsplit("W", 1)[1]) for r in answer["results"]]
        assert ranks == list(range(offset, offset + limit)), (offset, limit, ranks)
        pages = [(int(r.url.params["page"]), int(r.url.params["per-page"])) for r in backend.sent("api.openalex.org")]
        if offset + limit <= 200:
            assert pages == [(1, offset + limit)]
        else:
            assert len(pages) == (1 if offset % limit == 0 else 2) and all(size == limit for _, size in pages)


def test_source_failures_stay_typed_and_only_total_failure_fails_the_call(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works", [reply(status=503), reply({"results": [OPENALEX_WORK]})])
    backend.route("api.crossref.org", "/works", reply(status=429, headers={"Retry-After": "9999"}))
    answer = call(env, "literature_search", query="widgets")
    assert answer["ok"] is True and answer["count"] == 1
    assert answer["sources"]["openalex"] == {"status": "ok", "count": 1}
    assert answer["sources"]["crossref"]["status"] == "rate_limited"
    assert answer["sources"]["crossref"]["retry_after"] == 9999.0 and answer["sources"]["crossref"]["http_status"] == 429
    assert backend.sleeps == [0.5]  # one short retry before the 200; the 9999 s Retry-After is not waited for
    assert len(backend.sent("api.openalex.org")) == 2 and len(backend.sent("api.crossref.org")) == 1

    cases = {
        "server_error": [reply(status=500)], "unauthorized": [reply(status=403)], "timeout": [raising(httpx.ReadTimeout)],
        "network_error": [raising(httpx.ConnectError)], "bad_response": [reply(text="<html>not json</html>")],
        "bad_request": [reply(status=400)],
    }
    for expected, responders in cases.items():
        backend.requests.clear()
        backend.route("api.openalex.org", "/works", responders)
        backend.route("api.crossref.org", "/works", responders)
        answer = call(env, "literature_search", query=f"widgets {expected}")  # a new query: nothing cached
        assert answer["ok"] is False and answer["results"] == [] and answer["error"]["code"] == "all_sources_failed"
        assert {s["status"] for s in answer["sources"].values()} == {expected}, expected
        assert "not_found" not in json.dumps(answer["sources"])
        retried = expected in {"server_error", "timeout", "network_error"}
        assert len(backend.sent("api.openalex.org")) == (3 if retried else 1)


def test_retry_after_accepts_http_dates_and_rejects_non_finite_values(env):
    backend = env["backend"]
    env["use"]()
    later = email.utils.formatdate(time.time() + 3600, usegmt=True)
    backend.route("api.openalex.org", "/works", reply(status=429, headers={"Retry-After": later}))
    backend.route("api.crossref.org", "/works", reply(status=429, headers={"Retry-After": "1e999"}))
    raw = env["api"].tools["literature_search"]["handler"](None, query="widgets")
    assert "Infinity" not in raw and "NaN" not in raw
    answer = json.loads(raw)
    assert answer["sources"]["openalex"]["status"] == "rate_limited" and 3500 <= answer["sources"]["openalex"]["retry_after"] <= 3600
    assert len(backend.sent("api.openalex.org")) == 1 and backend.sleeps == [0.5, 1.5]  # the date was honoured, not retried
    assert answer["sources"]["crossref"]["status"] == "rate_limited" and "retry_after" not in answer["sources"]["crossref"]
    assert transport._retry_after({"retry-after": "not a date"}) is None
    assert transport._retry_after({"retry-after": "-5"}) == 0.0
    assert transport._retry_after({"retry-after": "1e12"}) == transport.RETRY_AFTER_MAX_SEC


def test_http_200_error_envelopes_are_bad_response_and_never_cached(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works", [reply({"error": "Invalid query parameters", "message": "search is malformed"}),
                                                 reply({"results": [OPENALEX_WORK]})])
    backend.route("api.crossref.org", "/works", [reply({"status": "error", "message-type": "validation-failure",
                                                        "message": [{"type": "select-not-available", "value": "x"}]}),
                                                 reply(crossref([CROSSREF_OTHER]))])
    answer = call(env, "literature_search", query="widgets")
    assert answer["ok"] is False and answer["error"]["code"] == "all_sources_failed"
    assert answer["sources"]["openalex"]["status"] == "bad_response" and "Invalid query" in answer["sources"]["openalex"]["message"]
    assert answer["sources"]["crossref"]["status"] == "bad_response" and "select-not-available" in answer["sources"]["crossref"]["message"]
    assert not list((env["state"] / "cache").glob("*.json"))  # nothing cached
    answer = call(env, "literature_search", query="widgets")  # the same query is requested again, not served from cache
    assert answer["ok"] and answer["sources"] == {"openalex": {"status": "ok", "count": 1}, "crossref": {"status": "ok", "count": 1}}
    assert len(backend.sent("api.openalex.org")) == 2 and len(backend.sent("api.crossref.org")) == 2
    for host, path, body in (("api.semanticscholar.org", "/graph/v1/paper/search", {"error": "bad key"}),
                             ("api.core.ac.uk", "/v3/search/works/", {"message": "quota"}),
                             ("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esearch.fcgi", {"esearchresult": {"ERROR": "Empty term"}}),
                             ("export.arxiv.org", "/api/query", None)):
        backend.route(host, path, reply(body) if body is not None else reply(text="<html>maintenance</html>"))
    assert call(env, "literature_search", query="w2", sources=["semantic_scholar"])["sources"]["semantic_scholar"]["status"] == "bad_response"
    assert call(env, "literature_search", query="w2", kind="open_access", sources=["core"])["sources"]["core"]["status"] == "bad_response"
    assert call(env, "literature_search", query="w2", sources=["pubmed"])["sources"]["pubmed"]["status"] == "bad_response"
    assert call(env, "literature_search", query="w2", kind="preprints")["sources"]["arxiv"]["status"] == "bad_response"
    assert not list((env["state"] / "cache").glob("*.json")) == [] or all(
        json.loads(p.read_text())["value"] not in ({"error": "bad key"}, {"message": "quota"}) for p in (env["state"] / "cache").glob("*.json"))


def test_ncbi_errors_are_not_mistaken_for_absence(env):
    backend = env["backend"]
    env["use"]()
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esummary.fcgi", reply({"error": "API rate limit exceeded"}))
    backend.route("api.openalex.org", "/works/pmid:123", reply(status=404))
    answer = call(env, "work_lookup", id="pmid:123")
    assert answer["ok"] is False and answer["error"]["code"] == "all_sources_failed"
    assert answer["sources"]["pubmed"]["status"] == "bad_response" and answer["sources"]["openalex"]["status"] == "not_found"
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esummary.fcgi",
                  reply({"result": {"uids": ["123"], "123": {"uid": "123", "error": "cannot get document summary"}}}))
    answer = call(env, "work_lookup", id="pmid:123")
    assert answer["error"]["code"] == "not_found" and answer["sources"]["pubmed"]["status"] == "not_found"


def test_invalid_arguments_are_refused_before_any_request(env):
    backend = env["backend"]
    env["use"]()
    for kwargs in ({"query": ""}, {"query": "x" * 501}, {"query": "a", "kind": "books"}, {"query": "a", "limit": 0},
                   {"query": "a", "limit": 26}, {"query": "a", "offset": -1}, {"query": "a", "sources": ["arxiv"]},
                   {"query": "a", "sources": "openalex"}, {"query": "a", "year_from": 2020, "year_to": 2019},
                   {"query": "a", "year_from": 20}, {"query": "a", "limit": True}):
        answer = call(env, "literature_search", **kwargs)
        assert answer["ok"] is False and answer["error"]["code"] == "invalid_argument", kwargs
        assert answer["sources"] == {} and set(answer["keys_configured"]) == set(plugin.SETTINGS_KEYS)
    assert call(env, "work_lookup", id="not an identifier")["error"]["code"] == "invalid_argument"
    assert call(env, "citations", doi="abc")["error"]["code"] == "invalid_argument"
    assert call(env, "citations", doi="10.1000/abc", mode="all")["error"]["code"] == "invalid_argument"
    assert backend.requests == []


def test_preprints_use_arxiv_and_crossref_posted_content(env):
    backend = env["backend"]
    env["use"]()
    backend.route("export.arxiv.org", "/api/query", reply(text=ARXIV_FEED))
    # The real posted-content items carry no container-title; the select list cannot ask for institution.
    backend.route("api.crossref.org", "/works", reply(crossref([
        {**CROSSREF_ITEM, "DOI": "10.1101/2021.01.02.425001", "container-title": [], "group-title": "Neuroscience",
         "publisher": "Cold Spring Harbor Laboratory"},
        CROSSREF_OTHER])))
    answer = call(env, "literature_search", query="deep widgets", kind="preprints", sources=["arxiv", "biorxiv"],
                  year_from=2020, year_to=2021, limit=2)
    assert answer["ok"] and answer["count"] == 2
    arxiv = answer["results"][0]
    assert arxiv == {"source": "arxiv", "id": "arXiv:2101.00001v2", "title": "Deep Widgets: a preprint",
                     "authors": ["Ada Example", "Bo Sample"], "year": 2021, "venue": "Widget Letters 3", "doi": "10.1000/abc",
                     "url": "http://arxiv.org/abs/2101.00001v2", "abstract": "Widgets, deeply."}
    assert answer["results"][1]["source"] == "biorxiv" and answer["results"][1]["venue"] == "Cold Spring Harbor Laboratory"
    query = backend.sent("export.arxiv.org")[0].url.params
    assert query["search_query"] == "all:deep AND all:widgets AND submittedDate:[202001010000 TO 202112312359]"
    assert query["start"] == "0" and query["max_results"] == "2"
    assert backend.sent("export.arxiv.org")[0].extensions["timeout"]["read"] == P.ARXIV_TIMEOUT_SEC
    assert backend.sent("api.crossref.org")[0].extensions["timeout"]["read"] == transport.REQUEST_TIMEOUT_SEC
    assert "type:posted-content" in backend.sent("api.crossref.org")[0].url.params["filter"]
    assert "member:246" in backend.sent("api.crossref.org")[0].url.params["filter"]
    lock = env["state"] / "throttle" / "arxiv.lock"
    assert lock.is_file() and float(lock.read_text()) > 0
    assert call(env, "literature_search", query="deep widgets", kind="preprints")["sources"] == {"arxiv": {"status": "ok", "count": 1}}
    backend.route("export.arxiv.org", "/api/query", reply(text='<!DOCTYPE feed [<!ENTITY x "y">]><feed/>'))
    answer = call(env, "literature_search", query="other", kind="preprints")
    assert answer["sources"]["arxiv"]["status"] == "bad_response"


def test_pubmed_core_and_default_source_selection(env):
    backend = env["backend"]
    env["use"]({"NCBI_API_KEY": "ncbi-secret"})
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esearch.fcgi", reply({"esearchresult": {"idlist": ["111", "222"]}}))
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esummary.fcgi", reply({"result": {"uids": ["111", "222"],
        "111": {"uid": "111", "title": "Deep Widgets", "authors": [{"name": "Example A"}], "pubdate": "2021 May 3",
                "fulljournalname": "Journal of Widgets", "articleids": [{"idtype": "doi", "value": "10.1000/ABC"}]},
        "222": {"uid": "222", "error": "cannot get document summary"}}}))
    backend.route("api.core.ac.uk", "/v3/search/works/", reply(status=401))
    answer = call(env, "literature_search", query="widgets", sources=["pubmed"])
    assert answer["ok"] and answer["results"] == [{"source": "pubmed", "id": "pmid:111", "title": "Deep Widgets",
        "authors": ["Example A"], "year": 2021, "venue": "Journal of Widgets", "doi": "10.1000/abc",
        "url": "https://pubmed.ncbi.nlm.nih.gov/111/", "abstract": None}]
    search, summary = backend.sent("eutils.ncbi.nlm.nih.gov")
    assert search.url.params["api_key"] == "ncbi-secret" and search.url.params["tool"] == "ouroboros_science_search"
    assert summary.url.params["id"] == "111,222" and summary.url.params["db"] == "pubmed"
    assert "ncbi-secret" not in json.dumps(answer)
    answer = call(env, "literature_search", query="widgets", kind="open_access", sources=["core"])
    assert answer["ok"] is False and answer["sources"]["core"]["status"] == "unauthorized"
    backend.route("api.openalex.org", "/works", reply({"results": [OPENALEX_WORK]}))
    answer = call(env, "literature_search", query="widgets", kind="open_access")
    assert list(answer["sources"]) == ["openalex"] and backend.sent("api.openalex.org")[-1].url.params["filter"] == "is_oa:true"
    assert answer["results"][0]["open_access_url"] == "https://example.org/w1.pdf"


@pytest.mark.parametrize("raw, expected", [
    ("10.1000/ABC", ("doi", "10.1000/abc")), ("https://doi.org/10.1000/abc", ("doi", "10.1000/abc")),
    ("doi:10.1000/abc", ("doi", "10.1000/abc")), ("W123", ("openalex", "W123")),
    ("https://openalex.org/W123", ("openalex", "W123")), ("PMC9876", ("pmcid", "9876")), ("pmcid:9876", ("pmcid", "9876")),
    ("12345678", ("pmid", "12345678")), ("PMID: 12345678", ("pmid", "12345678")), ("2101.00001", ("arxiv", "2101.00001")),
    ("arXiv:2101.00001v2", ("arxiv", "2101.00001v2")), ("https://arxiv.org/abs/2101.00001", ("arxiv", "2101.00001")),
    ("https://arxiv.org/pdf/2101.00001.pdf", ("arxiv", "2101.00001")), ("hep-th/9901001", ("arxiv", "hep-th/9901001")),
    ("", (None, "")), ("hello world", (None, "hello world")), ("10.1000/" + "x" * 300, (None, "10.1000/" + "x" * 300)),
])
def test_identifier_detection(raw, expected):
    assert plugin.detect_identifier(raw) == expected


def test_work_lookup_falls_through_providers(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works/doi:10.1000/abc", reply(status=404))
    backend.route("api.crossref.org", "/works/10.1000/abc", reply({"status": "ok", "message": CROSSREF_ITEM}))
    answer = call(env, "work_lookup", id="https://doi.org/10.1000/ABC")
    assert answer["ok"] and answer["id_kind"] == "doi" and answer["work"]["source"] == "crossref"
    assert answer["work"]["title"] == "Deep Widgets" and answer["work"]["citation_count"] == 40
    assert answer["sources"]["openalex"]["status"] == "not_found" and answer["sources"]["crossref"]["status"] == "ok"
    sent = len(backend.sent("api.crossref.org"))
    assert call(env, "work_lookup", id="10.1000/abc")["sources"]["crossref"] == {"status": "ok"}
    assert len(backend.sent("api.crossref.org")) == sent  # the successful lookup is cached; the 404 was not
    backend.route("api.openalex.org", "/works/doi:10.1000/none", reply(status=404))
    backend.route("api.crossref.org", "/works/10.1000/none", reply(status=404))
    answer = call(env, "work_lookup", id="10.1000/none")
    assert answer["ok"] is False and answer["error"]["code"] == "not_found" and answer["work"] is None
    backend.route("api.openalex.org", "/works/doi:10.1000/down", reply(status=500))
    backend.route("api.crossref.org", "/works/10.1000/down", reply(status=404))
    answer = call(env, "work_lookup", id="10.1000/down")
    assert answer["error"]["code"] == "all_sources_failed" and answer["sources"]["openalex"]["status"] == "server_error"

    backend.route("api.openalex.org", "/works/W1", reply(OPENALEX_WORK))
    assert call(env, "work_lookup", id="W1")["work"]["doi"] == "10.1000/abc"
    backend.route("export.arxiv.org", "/api/query", reply(text=ARXIV_FEED))
    answer = call(env, "work_lookup", id="arXiv:2101.00001v2")
    assert answer["work"]["source"] == "arxiv" and backend.sent("export.arxiv.org")[-1].url.params["id_list"] == "2101.00001v2"
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esummary.fcgi", reply({"result": {"uids": ["77"], "77": {
        "uid": "77", "title": "PMC paper", "authors": [], "pubdate": "2020", "source": "Open J",
        "articleids": [{"idtype": "pmid", "value": "123"}, {"idtype": "doi", "value": "10.1000/pmc"}]}}}))
    answer = call(env, "work_lookup", id="PMC77")
    assert answer["work"]["url"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC77/" and answer["work"]["doi"] == "10.1000/pmc"
    assert backend.sent("eutils.ncbi.nlm.nih.gov")[-1].url.params["db"] == "pmc"
    backend.route("eutils.ncbi.nlm.nih.gov", "/entrez/eutils/esummary.fcgi", reply({"result": {"uids": ["5"], "5": {"uid": "5", "error": "x"}}}))
    backend.route("api.openalex.org", "/works/pmid:5", reply(OPENALEX_WORK))
    answer = call(env, "work_lookup", id="pmid:5")
    assert answer["work"]["source"] == "openalex" and answer["sources"]["pubmed"]["status"] == "not_found"


def test_dois_with_reserved_characters_are_percent_encoded_in_paths(env):
    backend = env["backend"]
    env["use"]({"SEMANTIC_SCHOLAR_API_KEY": "s2"})
    doi = "10.1002/(sici)1097-4571(199806)49:8<693::aid-asi3>3.0.co;2-#?x=1"
    seen = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = (request.url.raw_path.decode(), dict(request.url.params))
        return httpx.Response(404)

    for host, path in (("api.openalex.org", "/works/"), ("api.crossref.org", "/works/"),
                       ("api.semanticscholar.org", "/graph/v1/paper/"), ("api.opencitations.net", "/index/v2/")):
        backend.route(host, path, capture)
    answer = call(env, "work_lookup", id=doi)
    assert answer["error"]["code"] == "not_found"
    encoded = "10.1002/%28sici%291097-4571%28199806%2949%3A8%3C693%3A%3Aaid-asi3%3E3.0.co%3B2-%23%3Fx%3D1"
    assert seen["api.openalex.org"][0].startswith(f"/works/doi:{encoded}?") and seen["api.openalex.org"][1] == {"select": P.OPENALEX_FIELDS}
    assert seen["api.crossref.org"] == (f"/works/{encoded}", {})
    assert seen["api.semanticscholar.org"][0].startswith(f"/graph/v1/paper/DOI:{encoded}?")
    call(env, "citations", doi=doi, mode="citing")
    assert seen["api.opencitations.net"][0] == f"/index/v2/citations/doi:{encoded}"
    call(env, "citations", doi=doi)
    assert seen["api.opencitations.net"][0] == f"/index/v2/citation-count/doi:{encoded}"
    assert P.doi_url("10.1000/a#b") == "https://doi.org/10.1000/a%23b"


def test_citations_modes_and_opencitations_fallback(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works/doi:10.1000/abc", reply(OPENALEX_WORK))
    backend.route("api.openalex.org", "/works", reply({"meta": {"count": 42}, "results": [OPENALEX_OTHER]}))
    backend.route("api.opencitations.net", "/index/v2/citation-count/doi:10.1000/abc", reply([{"count": "12"}]))
    count = call(env, "citations", doi="10.1000/abc")
    assert count["ok"] and count["counts"] == {"openalex": 42, "openalex_references": 1, "opencitations": 12}
    citing = call(env, "citations", doi="10.1000/ABC", mode="citing", limit=3)
    assert citing["ok"] and citing["total"] == 42 and citing["results"][0]["title"] == "Widget Theory"
    assert backend.sent("api.openalex.org")[-1].url.params["filter"] == "cites:W1"
    cited = call(env, "citations", doi="10.1000/abc", mode="cited")
    assert cited["ok"] and cited["total"] == 1 and cited["count"] == 1
    assert backend.sent("api.openalex.org")[-1].url.params["filter"] == "ids.openalex:W2"
    backend.route("api.openalex.org", "/works", reply(status=503))
    backend.route("api.opencitations.net", "/index/v2/citations/doi:10.1000/abc",
                  reply([{"citing": "omid:br/1 doi:10.1000/c1 pmid:1", "cited": "doi:10.1000/abc"}, {"citing": "omid:br/2"}]))
    fallback = call(env, "citations", doi="10.1000/abc", mode="citing")
    assert fallback["ok"] and fallback["total"] == 1 and fallback["results"] == [{"source": "opencitations", "id": "doi:10.1000/c1",
        "title": None, "authors": [], "year": None, "venue": None, "doi": "10.1000/c1", "url": "https://doi.org/10.1000/c1", "abstract": None}]
    assert fallback["sources"]["openalex"]["status"] == "server_error" and fallback["sources"]["opencitations"]["status"] == "ok"
    assert call(env, "citations", doi="10.1000/abc", mode="citing")["ok"]  # the successful fallback is cached
    backend.route("api.openalex.org", "/works/doi:10.1000/new", reply(status=404))
    backend.route("api.opencitations.net", "/index/v2/citations/doi:10.1000/new", reply(status=502))
    failed = call(env, "citations", doi="10.1000/new", mode="citing")
    assert failed["ok"] is False and failed["error"]["code"] == "all_sources_failed"
    assert failed["sources"] == {"openalex": {"status": "not_found", "http_status": 404, "message": failed["sources"]["openalex"]["message"]},
                                 "opencitations": {"status": "server_error", "http_status": 502, "message": failed["sources"]["opencitations"]["message"]}}


def test_missing_citation_fields_are_unavailable_not_zero(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works/doi:10.1000/bare", reply({"id": "https://openalex.org/W9"}))
    backend.route("api.opencitations.net", "/index/v2/citation-count/doi:10.1000/bare", reply(status=500))
    answer = call(env, "citations", doi="10.1000/bare")
    assert answer["ok"] is False and answer["error"]["code"] == "all_sources_failed" and answer["counts"] == {}
    assert answer["sources"]["openalex"]["status"] == "bad_response" and answer["sources"]["opencitations"]["status"] == "server_error"
    backend.route("api.openalex.org", "/works/doi:10.1000/text", reply({"id": "https://openalex.org/W9", "cited_by_count": "many",
                                                                        "referenced_works": "nope"}))
    backend.route("api.opencitations.net", "/index/v2/citation-count/doi:10.1000/text", reply([{"count": 3}]))
    answer = call(env, "citations", doi="10.1000/text")
    assert answer["ok"] and answer["counts"] == {"opencitations": 3} and answer["sources"]["openalex"]["status"] == "bad_response"
    backend.route("api.opencitations.net", "/index/v2/references/doi:10.1000/text", reply([{"cited": "doi:10.1000/r1"}]))
    answer = call(env, "citations", doi="10.1000/text", mode="cited")  # an unusable reference list falls back, not "empty"
    assert answer["ok"] and answer["total"] == 1 and answer["results"][0]["doi"] == "10.1000/r1"
    backend.route("api.openalex.org", "/works/doi:10.1000/zero", reply({"id": "https://openalex.org/W9", "cited_by_count": 0,
                                                                        "referenced_works": []}))
    backend.route("api.opencitations.net", "/index/v2/citation-count/doi:10.1000/zero", reply([{"count": 0}]))
    answer = call(env, "citations", doi="10.1000/zero")
    assert answer["ok"] and answer["counts"] == {"openalex": 0, "openalex_references": 0, "opencitations": 0}
    backend.route("api.opencitations.net", "/index/v2/citation-count/doi:10.1000/zero", reply([{"count": "n/a"}]))
    call(env, "citations", doi="10.1000/zero")  # served from cache now; a malformed count would be bad_response
    assert P._count("n/a") is None and P._count(True) is None and P._count(" 7 ") == 7


def test_successful_answers_are_cached_and_failures_are_not(env):
    backend = env["backend"]
    env["use"]()
    backend.route("api.openalex.org", "/works", reply({"results": [OPENALEX_WORK]}))
    backend.route("api.crossref.org", "/works", [reply(status=500), reply(status=500), reply(status=500), reply(crossref([CROSSREF_OTHER]))])
    first = call(env, "literature_search", query="widgets")
    assert first["sources"]["crossref"]["status"] == "server_error"
    sent = len(backend.sent("api.openalex.org"))
    second = call(env, "literature_search", query="widgets")
    assert second["ok"] and second["sources"]["crossref"]["status"] == "ok"
    assert len(backend.sent("api.openalex.org")) == sent  # served from the disk cache
    assert any((env["state"] / "cache").glob("*.json"))
    assert call(env, "literature_search", query="widgets", limit=6)["sources"]["openalex"]["status"] == "ok"
    assert len(backend.sent("api.openalex.org")) == sent + 1


def test_cache_temporaries_are_removed_on_failure(env, monkeypatch, tmp_path):
    cache = transport.Cache(tmp_path / "cache")
    monkeypatch.setattr(transport.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("no space")))
    cache.put("key", {"value": 1})
    assert list((tmp_path / "cache").iterdir()) == []
    monkeypatch.undo()
    stale = tmp_path / "cache" / "abandoned.123.tmp"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    cache.put("key", {"value": 1})
    assert sorted(p.suffix for p in (tmp_path / "cache").iterdir()) == [".json"]


def test_deadline_and_byte_bounds(env, monkeypatch):
    backend = env["backend"]
    env["use"]()
    monkeypatch.setitem(plugin.DEADLINE_SEC, "literature_search", 0)
    answer = call(env, "literature_search", query="widgets")
    assert answer["ok"] is False and {s["status"] for s in answer["sources"].values()} == {"deadline_exceeded"}
    assert backend.requests == []
    monkeypatch.setitem(plugin.DEADLINE_SEC, "literature_search", 240)
    big = {**OPENALEX_WORK, "abstract_inverted_index": {f"w{i}": [i] for i in range(300)}}
    backend.route("api.openalex.org", "/works", reply({"results": [{**big, "id": f"https://openalex.org/W{i}", "doi": None} for i in range(25)]}))
    backend.route("api.crossref.org", "/works", reply(crossref([])))
    monkeypatch.setattr(plugin, "RESULT_BYTE_CAP", 6000)
    raw = env["api"].tools["literature_search"]["handler"](None, query="widgets", limit=25)
    answer = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 6000 and answer["truncated"] is True and answer["abstracts_dropped"] is True
    assert 0 < len(answer["results"]) < 25 and all("abstract" not in r for r in answer["results"])
    assert answer["count"] == len(answer["results"]) and answer["results_before_truncation"] == 25
    assert answer["sources"]["openalex"]["count"] == 25
    backend.route("api.openalex.org", "/works/doi:10.1000/abc", reply(big))
    monkeypatch.setattr(plugin, "RESULT_BYTE_CAP", 1500)
    raw = env["api"].tools["work_lookup"]["handler"](None, id="10.1000/abc")
    answer = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 1500 and answer["ok"] and "abstract" not in answer["work"] and answer["abstracts_dropped"]
    monkeypatch.setattr(plugin, "RESULT_BYTE_CAP", 450)
    raw = env["api"].tools["work_lookup"]["handler"](None, id="10.1000/abc")
    answer = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 450 and answer["ok"] is False and answer["error"]["code"] == "response_too_large"
    assert answer["sources"]["openalex"]["status"] == "ok" and "work" not in answer


def test_deadline_holds_while_a_body_trickles_in(env, monkeypatch):
    backend = env["backend"]
    env["use"]()
    clock = {"now": 1000.0}
    monkeypatch.setattr(transport, "_now", lambda: clock["now"])
    monkeypatch.setitem(plugin.DEADLINE_SEC, "work_lookup", 5)

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b'{"id": "https://openalex.org/W1", ', b'"title": "slow", ', b'"cited_by_count": 1}'):
                clock["now"] += 3.0  # each chunk arrives after the per-read timeout would have been satisfied
                yield chunk

    backend.route("api.openalex.org", "/works/doi:10.1000/slow", lambda request: httpx.Response(200, stream=Trickle()))
    backend.route("api.crossref.org", "/works/10.1000/slow", reply({"status": "ok", "message": CROSSREF_ITEM}))
    answer = call(env, "work_lookup", id="10.1000/slow")
    assert answer["sources"]["openalex"]["status"] == "deadline_exceeded"
    assert answer["sources"]["crossref"]["status"] == "deadline_exceeded"  # nothing is started past the deadline
    assert answer["ok"] is False and answer["error"]["code"] == "all_sources_failed"
    assert len(backend.sent("api.openalex.org")) == 1 and backend.sent("api.crossref.org") == []


def test_settings_read_failures_are_reported_not_hidden(env):
    backend = env["backend"]

    class Locked(FakeAPI):
        def get_settings(self, keys):
            raise PermissionError("settings grant missing")

    env["use"](api=Locked(env["state"], {"SEMANTIC_SCHOLAR_API_KEY": "s2"}))
    backend.route("api.openalex.org", "/works", reply({"results": [OPENALEX_WORK]}))
    backend.route("api.crossref.org", "/works", reply(crossref([])))
    answer = call(env, "literature_search", query="widgets")
    assert answer["ok"] and answer["settings_error"]["code"] == "settings_unavailable"
    assert "settings grant missing" in answer["settings_error"]["message"]
    assert answer["keys_configured"] == {k: False for k in plugin.SETTINGS_KEYS}
    assert set(answer["sources"]) == {"openalex", "crossref"}  # the keyed default was not added
    assert "settings_error" in call(env, "work_lookup", id="bad id")  # even an invalid-argument answer says so


def test_without_a_state_directory_nothing_is_written_beside_the_code(env):
    backend = env["backend"]
    env["use"](api=FakeAPI(None, {}))
    for tool, kwargs in (("literature_search", {"query": "widgets"}), ("work_lookup", {"id": "10.1000/abc"}),
                         ("citations", {"doi": "10.1000/abc"})):
        answer = call(env, tool, **kwargs)
        assert answer["ok"] is False and answer["error"]["code"] == "state_dir_unavailable", tool
        assert answer["sources"] == {} and set(answer["keys_configured"]) == set(plugin.SETTINGS_KEYS)
    assert backend.requests == []
    assert not (SKILL_DIR / "_state").exists()
    env["api"].unload()
    assert plugin._state_dir() is None


def test_handlers_never_raise(env, monkeypatch):
    env["use"]()
    monkeypatch.setattr(plugin, "literature_search", lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    answer = call(env, "literature_search", query="widgets")
    assert answer == {"ok": False, "error": {"code": "internal_error", "message": "RuntimeError: boom"}}
