"""Retrieval over the local Ansible module documentation (RAG, sparse).

A from-scratch **BM25** index (the standard sparse-retrieval ranking function,
an evolution of TF-IDF) over the module list that `ansible-doc` exposes — so the
assistant can be grounded in the *real* modules installed in this container
(including collections pulled via ansible-galaxy), not the model's memory.

Pure Python + one `ansible-doc` subprocess for the corpus; the index is built
once and cached process-wide. The BM25 class itself has no Ansible dependency, so
it's unit-tested against a fixed toy corpus.
"""
from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from functools import lru_cache

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class BM25:
    """Okapi BM25 over a list of pre-tokenised documents."""

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus = corpus_tokens
        self.n = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in corpus_tokens]
        df: Counter = Counter()
        for doc in corpus_tokens:
            for term in set(doc):
                df[term] += 1
        # idf with the standard BM25 smoothing (+0.5), floored at 0.
        self.idf = {t: max(0.0, math.log(1 + (self.n - n + 0.5) / (n + 0.5))) for t, n in df.items()}

    def _score(self, q_terms: list[str], i: int) -> float:
        if not self.avgdl:
            return 0.0
        tf, dl = self.tf[i], self.doc_len[i]
        score = 0.0
        for t in q_terms:
            f = tf.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            score += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return score

    def top_k(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        q = tokenize(query)
        if not q or not self.n:
            return []
        scored = [(i, self._score(q, i)) for i in range(self.n)]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:k]


# --- Ansible module corpus --------------------------------------------------

@lru_cache(maxsize=1)
def _module_corpus() -> tuple[tuple[str, str], ...]:
    """(fqcn, short_description) for every installed module. Empty if ansible-doc
    is unavailable. Cached: the subprocess is slow and the set rarely changes."""
    try:
        out = subprocess.check_output(
            ["ansible-doc", "-l", "-t", "module"], text=True, timeout=90, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return ()
    docs: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line or line.startswith(" "):
            continue
        parts = line.split(None, 1)
        name = parts[0]
        if "." not in name:  # only fully-qualified names
            continue
        desc = parts[1].strip() if len(parts) > 1 else ""
        docs.append((name, desc))
    return tuple(docs)


def _parse_collection_list(text: str) -> list[tuple[str, str]]:
    """Parse `ansible-galaxy collection list` into (fqcn, description) entries."""
    docs: list[tuple[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-") or s.lower().startswith("collection"):
            continue
        parts = s.split()
        if len(parts) >= 2 and "." in parts[0]:
            docs.append((parts[0], f"installed Ansible collection (version {parts[1]})"))
    return docs


@lru_cache(maxsize=1)
def _collection_corpus() -> tuple[tuple[str, str], ...]:
    """Installed collections (so retrieval knows what's actually available, e.g.
    after `ansible-galaxy install`). Empty if the command isn't available."""
    try:
        out = subprocess.check_output(
            ["ansible-galaxy", "collection", "list"], text=True, timeout=60, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return ()
    # De-dup (a collection can show up under multiple paths).
    seen, docs = set(), []
    for name, desc in _parse_collection_list(out):
        if name not in seen:
            seen.add(name)
            docs.append((name, desc))
    return tuple(docs)


@lru_cache(maxsize=256)
def module_params(name: str) -> tuple[str, tuple[tuple[str, bool, str], ...]] | None:
    """Full parameter signature of one module via `ansible-doc -j`.

    Returns (short_description, ((param, required, type), ...)) or None if the
    module isn't documented here. This is what stops the model from inventing
    parameters — the retrieval layer feeds these real option names into the prompt.
    Cached per-module (the subprocess is ~1s) and only called for the handful of
    top-k hits, never the whole corpus.
    """
    import json
    if "." not in name:
        return None
    try:
        out = subprocess.check_output(
            ["ansible-doc", "-j", "-t", "module", name],
            text=True, timeout=20, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    # ansible-doc returns `{}` with rc=0 for a module it doesn't know (just a stderr
    # WARNING), so an empty dict means "not installed" → None, letting the web
    # fallback kick in.
    if not data or name not in data:
        return None
    entry = data.get(name) or {}
    doc = entry.get("doc") or {}
    short = ""
    sd = doc.get("short_description")
    if isinstance(sd, str):
        short = sd
    options = doc.get("options") or {}
    params: list[tuple[str, bool, str]] = []
    for pname, meta in options.items():
        if not isinstance(meta, dict):
            continue
        required = bool(meta.get("required"))
        ptype = str(meta.get("type") or "")
        params.append((pname, required, ptype))
    # Required params first, then alphabetical — keeps the prompt focused.
    params.sort(key=lambda p: (not p[1], p[0]))
    return short, tuple(params)


_WEB_DOC_CACHE: dict[str, tuple] = {}


def fetch_module_doc_web(name: str, *, timeout: float = 8.0) -> tuple[str, tuple[tuple[str, bool, str], ...]] | None:
    """Fetch a module's parameter list from docs.ansible.com (online fallback).

    Only used when a module isn't installed locally AND the user opted into online
    lookups. Returns the same shape as `module_params` so callers are uniform.
    Parsing is intentionally minimal (anchor ids + a 'required' marker heuristic)
    and tolerant: any failure returns None, never raises. Cached process-wide.
    """
    import re
    import urllib.request

    parts = name.split(".")
    if len(parts) != 3:
        return None
    if name in _WEB_DOC_CACHE:
        return _WEB_DOC_CACHE[name]
    ns, coll, mod = parts
    url = (f"https://docs.ansible.com/ansible/latest/collections/"
           f"{ns}/{coll}/{mod}_module.html")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "playforge/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return None

    # Parameter anchors: ...-module-parameter-<name>
    names = []
    seen = set()
    for p in re.findall(r"module-parameter-([a-z0-9_]+)", html):
        if p not in seen:
            seen.add(p)
            names.append(p)
    if not names:
        return None
    # short description from <title>: "name module – <desc> — Ansible ..."
    short = ""
    m = re.search(r"<title>[^–—-]*[–—-]\s*([^—<]+)", html)
    if m:
        import html as _html
        raw = m.group(1)
        # Cut at an em-dash entity / 'Ansible' suffix, then unescape HTML entities.
        raw = re.split(r"&mdash;|&#8212;|\bAnsible\b", raw)[0]
        short = _html.unescape(raw).strip().rstrip(" .—–-")[:120]
    # We can't reliably tell required from the HTML, so mark all optional.
    params = tuple((p, False, "") for p in names)
    result = (short, params)
    _WEB_DOC_CACHE[name] = result
    return result


def format_module_signature(name: str, max_params: int = 18, *, allow_web: bool = False) -> str | None:
    """A compact, prompt-ready signature line for a module, e.g.
    `ansible.builtin.copy — copy files. params: dest* (path), content (str), src (path), ...`.
    `*` marks required. None if the module isn't documented here. If `allow_web` and
    the module isn't installed locally, fall back to docs.ansible.com."""
    sig = module_params(name)
    if sig is None and allow_web:
        sig = fetch_module_doc_web(name)
    if sig is None:
        return None
    short, params = sig
    shown = params[:max_params]
    parts = []
    for pname, required, ptype in shown:
        star = "*" if required else ""
        tp = f" ({ptype})" if ptype else ""
        parts.append(f"{pname}{star}{tp}")
    more = "" if len(params) <= max_params else f", … (+{len(params) - max_params} more)"
    desc = f" — {short}" if short else ""
    return f"{name}{desc}. params: {', '.join(parts)}{more}"


@lru_cache(maxsize=1)
def _index() -> tuple[tuple[tuple[str, str], ...], BM25]:
    corpus = _module_corpus() + _collection_corpus()
    bm = BM25([tokenize(f"{name} {desc}") for name, desc in corpus])
    return corpus, bm


def search_modules(query: str, k: int = 6) -> list[dict]:
    """Top-k modules relevant to `query`, ranked by BM25. Empty if no corpus."""
    corpus, bm = _index()
    if not corpus:
        return []
    return [{"module": corpus[i][0], "description": corpus[i][1], "score": round(s, 3)}
            for i, s in bm.top_k(query, k)]


def available() -> bool:
    return bool(_module_corpus())
