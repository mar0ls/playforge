/* Tiny, dependency-free, XSS-safe Markdown → HTML renderer.
 *
 * Built in-house (not a CDN lib) so the app stays air-gapped. The golden rule:
 * EVERYTHING is HTML-escaped first, then we re-introduce ONLY a known-safe set of
 * tags from Markdown syntax. AI/chat output is untrusted, so no raw HTML ever
 * survives — this is the whole point.
 *
 * Supports: headings, **bold**, *italic*, `code`, ```fenced code```, links
 * (http/https/relative only), unordered/ordered lists, blockquotes, hr, paragraphs.
 * Exposed as window.renderMarkdown(text) -> html string.
 */
(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Inline: code spans first (so their content isn't further formatted), then bold/italic/links.
  function inline(s) {
    // `code`
    let out = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    // links [text](url) — only http(s) or relative, never javascript:
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
      if (/^(https?:\/\/|\/|#)/i.test(url)) {
        return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
      }
      return m; // leave suspicious URLs as plain text
    });
    // **bold** then *italic* (avoid clobbering ** with *)
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    return out;
  }

  function render(text) {
    const src = escapeHtml(text || "");
    const lines = src.split("\n");
    const html = [];
    let i = 0;
    let listType = null; // 'ul' | 'ol' | null
    let para = [];

    function flushPara() {
      if (para.length) { html.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
    }
    function closeList() {
      if (listType) { html.push(`</${listType}>`); listType = null; }
    }

    while (i < lines.length) {
      let line = lines[i];

      // fenced code block ```
      const fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        flushPara(); closeList();
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { body.push(lines[i]); i++; }
        i++; // skip closing fence
        html.push(`<pre class="md-code"><code>${body.join("\n")}</code></pre>`);
        continue;
      }

      // blank line → paragraph / list break
      if (/^\s*$/.test(line)) { flushPara(); closeList(); i++; continue; }

      // heading
      const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (h) { flushPara(); closeList(); const n = h[1].length; html.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }

      // horizontal rule
      if (/^\s*([-*_])\1\1+\s*$/.test(line)) { flushPara(); closeList(); html.push("<hr>"); i++; continue; }

      // blockquote
      const bq = line.match(/^\s*>\s?(.*)$/);
      if (bq) { flushPara(); closeList(); html.push(`<blockquote>${inline(bq[1])}</blockquote>`); i++; continue; }

      // unordered list
      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      if (ul) { flushPara(); if (listType !== "ul") { closeList(); html.push("<ul>"); listType = "ul"; } html.push(`<li>${inline(ul[1])}</li>`); i++; continue; }

      // ordered list
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ol) { flushPara(); if (listType !== "ol") { closeList(); html.push("<ol>"); listType = "ol"; } html.push(`<li>${inline(ol[1])}</li>`); i++; continue; }

      // plain text → accumulate into paragraph
      closeList(); para.push(line.trim()); i++;
    }
    flushPara(); closeList();
    return html.join("\n");
  }

  window.renderMarkdown = render;
})();
