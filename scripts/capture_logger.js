// Synology Photos request recorder. Inject into the Photos web app tab (DevTools console or a browser
// automation tool). It records every webapi/entry.cgi request with secrets removed, plus a short summary
// of the response, in window.__synoCaptures. Nothing leaves the page. Secrets are removed from requests AND
// responses (session ids, tokens, passwords, share passphrases and links); login calls are not recorded at all.
// Still read every capture before saving it anywhere: names of people, albums and folders are not secrets and stay.
// Read back:  JSON.stringify(window.__synoCaptures.filter(c => !/Browse\.(Timeline|Unit)|Thumbnail|Notification/.test(c.api)))
// Clear:      window.__synoCaptures.length = 0
(() => {
  if (window.__synoCaptureInstalled) return "already installed";
  window.__synoCaptureInstalled = true;
  window.__synoCaptures = [];
  const SECRET = /^(_sid|sid|synotoken|x-syno-token|passwd|password|passphrase|passphrase_password|sharing_passphrase|sharing_link|otp_code|device_id|did|_sharing_id|sharing_sid|cookie|account|token)$/i;
  // Recursively replace the value of every secret-named key in a parsed JSON value.
  const scrub = (v) => {
    if (Array.isArray(v)) return v.map(scrub);
    if (v && typeof v === "object") {
      const o = {};
      for (const [k, x] of Object.entries(v)) o[k] = SECRET.test(k) ? "***" : scrub(x);
      return o;
    }
    return v;
  };

  const toParams = (body, url) => {
    const out = {};
    // A file part (an upload) is recorded by its name, type and size only, never its content.
    const add = (k, v) => {
      out[k] = SECRET.test(k) ? "***"
        : typeof v === "string" ? v
        : (typeof Blob !== "undefined" && v instanceof Blob)
          ? "[file name=" + (v.name || "") + " type=" + (v.type || "") + " size=" + v.size + "]"
          : "[" + typeof v + "]";
    };
    try {
      new URL(url, location.href).searchParams.forEach((v, k) => add(k, v));
      if (typeof body === "string") new URLSearchParams(body).forEach((v, k) => add(k, v));
      else if (body instanceof URLSearchParams || body instanceof FormData) body.forEach((v, k) => add(k, v));
      else if (body && typeof body === "object" && !(body instanceof Blob)) Object.entries(body).forEach(([k, v]) => add(k, String(v)));
    } catch (e) { out.__parse_error = String(e); }
    return out;
  };

  const summarize = (text) => {
    try {
      const j = scrub(JSON.parse(text));
      const s = JSON.stringify(j);
      return s.length > 3000 ? s.slice(0, 3000) + "…" : s;
    } catch { return text ? "[non-JSON " + text.length + " chars]" : ""; }
  };

  const record = (url, body, status, responseText) => {
    if (!/webapi\/.*\.cgi/.test(url)) return;
    const p = toParams(body, url);
    if (/^SYNO\.API\.Auth$/.test(p.api || "")) return;  // login / logout: nothing worth recording, only secrets
    const entry = { t: new Date().toISOString(), cgi: (url.match(/webapi\/([^?]+)/) || [])[1], api: p.api, method: p.method,
                    version: p.version, multipart: body instanceof FormData, params: p, status,
                    response: summarize(responseText) };
    delete entry.params.api; delete entry.params.method; delete entry.params.version;
    window.__synoCaptures.push(entry);
    console.log("SYNOCAP", entry.api + "." + entry.method + " v" + entry.version);
  };

  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, url) { this.__url = url; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    this.addEventListener("loadend", () => {
      let txt = "";
      try {
        txt = (this.responseType === "" || this.responseType === "text") ? this.responseText
          : this.responseType === "json" ? JSON.stringify(this.response) : "";
      } catch {}
      record(String(this.__url), body, this.status, txt);
    });
    return send.apply(this, arguments);
  };

  const ofetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const res = await ofetch.apply(this, arguments);
    try {
      const ct = res.headers.get("content-type") || "";
      const txt = ct.includes("json") ? await res.clone().text() : "";
      record(url, init && init.body, res.status, txt);
    } catch {}
    return res;
  };
  return "synology capture installed";
})();
