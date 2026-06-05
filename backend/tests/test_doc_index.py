"""Tests for the BM25 retriever (pure ranking logic, no Ansible needed)."""
from __future__ import annotations

from app.core import doc_index
from app.core.doc_index import BM25, tokenize


def test_tokenize_lowercases_and_splits():
    assert tokenize("Ansible.Builtin.APT!") == ["ansible", "builtin", "apt"]


def _corpus():
    return [
        tokenize("ansible.builtin.apt manage apt packages on debian ubuntu"),
        tokenize("ansible.builtin.yum manage yum packages on redhat centos"),
        tokenize("ansible.builtin.service start stop restart services"),
        tokenize("ansible.builtin.copy copy files to remote hosts"),
    ]


def test_bm25_ranks_relevant_doc_first():
    bm = BM25(_corpus())
    top = bm.top_k("install an apt package on debian", k=2)
    assert top, "expected at least one hit"
    assert top[0][0] == 0  # the apt doc


def test_bm25_rare_term_wins():
    bm = BM25(_corpus())
    top = bm.top_k("restart a service", k=1)
    assert top[0][0] == 2  # the service doc


def test_bm25_empty_query_returns_empty():
    assert BM25(_corpus()).top_k("") == []


def test_bm25_empty_corpus_returns_empty():
    assert BM25([]).top_k("anything") == []


def test_bm25_no_overlap_returns_empty():
    assert BM25(_corpus()).top_k("kubernetes helm chart") == []


def test_bm25_scores_descending():
    bm = BM25(_corpus())
    results = bm.top_k("manage packages", k=4)
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


def test_parse_collection_list():
    out = (
        "# /usr/local/lib/python3.12/site-packages/ansible_collections\n"
        "Collection        Version\n"
        "----------------- -------\n"
        "ansible.posix     1.5.4\n"
        "community.general 8.5.0\n"
        "\n"
    )
    docs = doc_index._parse_collection_list(out)
    names = [n for n, _ in docs]
    assert names == ["ansible.posix", "community.general"]
    assert "1.5.4" in dict(docs)["ansible.posix"]


def test_parse_collection_list_ignores_noise():
    assert doc_index._parse_collection_list("# path\nCollection Version\n--- ---\n") == []


_COPY_DOC_JSON = """{
  "ansible.builtin.copy": {
    "doc": {
      "short_description": "Copy files to remote locations",
      "options": {
        "dest": {"required": true, "type": "path"},
        "src": {"required": false, "type": "path"},
        "content": {"required": false, "type": "str"},
        "mode": {"required": false, "type": "raw"}
      }
    }
  }
}"""


def test_module_params_parses_required_and_types(monkeypatch):
    monkeypatch.setattr(doc_index.subprocess, "check_output", lambda *a, **k: _COPY_DOC_JSON)
    doc_index.module_params.cache_clear()
    short, params = doc_index.module_params("ansible.builtin.copy")
    assert short == "Copy files to remote locations"
    names = [p[0] for p in params]
    # required first → dest leads
    assert names[0] == "dest"
    assert ("dest", True, "path") in params
    assert ("content", False, "str") in params


def test_format_module_signature_marks_required(monkeypatch):
    monkeypatch.setattr(doc_index.subprocess, "check_output", lambda *a, **k: _COPY_DOC_JSON)
    doc_index.module_params.cache_clear()
    sig = doc_index.format_module_signature("ansible.builtin.copy")
    assert sig.startswith("ansible.builtin.copy — Copy files")
    assert "dest* (path)" in sig          # required marked with *
    assert "content (str)" in sig         # optional, no star


def test_module_params_none_on_missing(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(doc_index.subprocess, "check_output", _boom)
    doc_index.module_params.cache_clear()
    assert doc_index.module_params("no.such.module") is None
    assert doc_index.format_module_signature("no.such.module") is None


def test_module_params_rejects_non_fqcn():
    assert doc_index.module_params("ping") is None


def test_search_modules_empty_when_no_corpus(monkeypatch):
    monkeypatch.setattr(doc_index, "_module_corpus", lambda: ())
    doc_index._index.cache_clear()
    try:
        assert doc_index.search_modules("apt") == []
    finally:
        doc_index._index.cache_clear()  # don't poison a real index elsewhere


# --- Phase C: optional web fallback -----------------------------------------

_UFW_DOC_HTML = (
    "<html><head><title>community.general.ufw module – Manage firewall with UFW "
    "— Ansible Documentation</title></head><body>"
    "<span id='ansible-collections-community-general-ufw-module-parameter-rule'></span>"
    "<span id='ansible-collections-community-general-ufw-module-parameter-port'></span>"
    "<span id='ansible-collections-community-general-ufw-module-parameter-direction'></span>"
    "</body></html>"
)


class _FakeResp:
    status = 200
    def __init__(self, text): self._t = text
    def read(self): return self._t.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_fetch_module_doc_web_parses_params(monkeypatch):
    import urllib.request
    doc_index._WEB_DOC_CACHE.clear()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(_UFW_DOC_HTML))
    sig = doc_index.fetch_module_doc_web("community.general.ufw")
    assert sig is not None
    short, params = sig
    names = [p[0] for p in params]
    assert "rule" in names and "port" in names and "direction" in names
    assert "Manage firewall" in short


def test_fetch_module_doc_web_bad_name():
    assert doc_index.fetch_module_doc_web("notfqcn") is None
    assert doc_index.fetch_module_doc_web("too.short") is None


def test_fetch_module_doc_web_network_error_returns_none(monkeypatch):
    import urllib.request
    doc_index._WEB_DOC_CACHE.clear()
    def _boom(*a, **k): raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert doc_index.fetch_module_doc_web("community.general.unknownmod") is None


def test_format_signature_web_fallback(monkeypatch):
    import urllib.request
    doc_index._WEB_DOC_CACHE.clear()
    doc_index.module_params.cache_clear()
    # not installed locally → module_params None; web allowed → uses web
    monkeypatch.setattr(doc_index, "module_params", lambda n: None)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp(_UFW_DOC_HTML))
    sig = doc_index.format_module_signature("community.general.ufw", allow_web=True)
    assert sig and "community.general.ufw" in sig and "rule" in sig
    # without allow_web → None (offline default)
    assert doc_index.format_module_signature("community.general.ufw", allow_web=False) is None
