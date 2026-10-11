"""Watching the New campaign wizard's last click in a real browser (Plan M M181 part C, DEC-1611).

The Review screen's one button approves the new model (a goal's first campaign), registers the test plan, and only
then starts the download of the contact list. The browser journeys (`test_usability_journeys.py`,
`test_campaign_wizard_journey.py`) must prove that order, and that the plan is registered and frozen BEFORE the file
is asked for. Playwright's network events and routes do not see a download - Chromium hands it to its download
manager - so the page records it itself: `install(page, campaign_id)` wraps the page's own `fetch` (each POST of the
click, asked and then answered) and the click of a download link to the contact list, at which moment it reads the
server synchronously, before the browser has asked for the file. `recorded(page)` returns what it saw.

A download started any other way than a link's click is not seen, so the journeys' "asked for once" check fails
loudly rather than passing on nothing.
"""

from __future__ import annotations

from typing import Any, Final

WATCH: Final[str] = r"""(campaignPath) => {
  const log = (window.__lastClick = { order: [], atDownload: [] });
  const read = (path) => {
    const request = new XMLHttpRequest();
    request.open("GET", location.origin + path, false);
    request.send();
    return JSON.parse(request.responseText);
  };
  const fetch0 = window.fetch;
  window.fetch = async function (input, init) {
    const url = String(typeof input === "string" ? input : input.url);
    const method = String((init && init.method) || (typeof input === "string" ? "GET" : input.method)).toUpperCase();
    const what =
      method !== "POST" ? null : /\/approve$/.test(url) ? "approve the model" : url.endsWith(campaignPath + "/plan") ? "plan" : null;
    if (what) log.order.push(what + ": asked");
    const reply = await fetch0.apply(this, arguments);
    if (what) log.order.push(what + ": " + reply.status);
    return reply;
  };
  const click0 = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (this.hasAttribute("download") && /\/treat_list\.csv$/.test(this.href)) {
      log.order.push("file: asked");
      log.atDownload.push({ href: this.href, plans: read(campaignPath + "/plan"), view: read(campaignPath) });
    }
    return click0.apply(this, arguments);
  };
}"""


def install(page: Any, campaign_id: str) -> None:
    """Start watching, on the Review screen, just before the last click."""
    page.evaluate(WATCH, f"/campaigns/{campaign_id}")


def recorded(page: Any) -> dict[str, Any]:
    """`{order: [...], atDownload: [{href, plans, view}]}`: the calls in the order the page made them, and the
    server's `GET /campaigns/{id}/plan` and `GET /campaigns/{id}` read the moment each download started."""
    seen: dict[str, Any] = page.evaluate("window.__lastClick")
    return seen
