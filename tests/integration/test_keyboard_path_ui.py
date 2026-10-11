"""Plan M, M181 part A (DEC-1611): the keyboard path, in a real browser.

The usability test kit (`docs/ux/USABILITY_TEST.md`) assumes a person can reach every control without a mouse; this
file checks it in Chromium, with the keyboard only (`Tab`, `Shift+Tab`, `Enter`, `Escape`, the arrow keys), on the UI
served with the API answers recorded from the validated Hillstrom demo (`tests/fixtures/ui_replay.py`, so no seeded
store and no network are needed):

* the Overview (`#/`, and `#/overview` in the campaigns layout): Tab visits every control, link and button that is on
  screen, once and in the order of the page, and each one shows a focus style (an outline or a ring on it or on the
  wrapper that holds it); Shift+Tab walks back; nothing traps the focus;
* the Help (?) menu: reached by Tab, opened by Enter, entries reached by the arrow keys and by Tab, closed by Escape
  with focus back on the button;
* the New campaign wizard (`#/campaigns/new`), first step, when the tree has it (M177): the same walk.

A control the walk cannot reach, or that shows no focus style, fails the test unless it is in `KNOWN` below with a
reason, so a new keyboard defect fails and the list can only shrink.

The module skips, with the reason, when playwright or a Chromium is missing; the layout cases skip when the campaigns
layout (`ui/layout.js`, M174) or the wizard is not in the tree. Screenshots of a failure go to `$KEYBOARD_SHOTS`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.ui_replay import REPO_ROOT, ReplayServer, has_campaigns_layout

pytestmark = [pytest.mark.integration]

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed; the keyboard path needs a browser"
)

CHROMIUM: Final[Path] = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
VIEWPORT: Final[dict[str, int]] = {"width": 1440, "height": 900}
SETTLE_MS: Final[int] = 1500

KNOWN: Final[dict[str, str]] = {
    "help menu (campaigns): the first Tab closes the menu, so no entry can be reached by Tab": (
        "In the campaigns shell, after Enter opens the Help menu, the first Tab closes it and the focus drops (to the "
        "page or to the next control, 'Minfy, home'): no entry (tour, Glossary, Send feedback, What's new) can be "
        "reached by the keyboard. The cause is ui/chrome.js:629 (the focusin handler closes every menu that does not "
        "contain the new focus target, and finds the menu with `.closest('.menu')`) while ui/shell.js:78 (helpHtml) "
        "wraps the menu in `.sh-help`, so the focus is never seen as inside it. The classic bar does not do this. "
        "Same fix as the next entry. Found by running this file against the M174 tree."
    ),
    "help menu (campaigns): arrow keys do not move through the entries": (
        "In the campaigns shell the Help menu is wrapped in `.sh-help`, not `.menu`, so the arrow-key handler in "
        "ui/chrome.js (menuNavigation, which looks for `.menu`) ignores it; Enter opens the menu but neither the "
        "arrow keys nor Tab reach any entry (see the previous entry). One fix for both: add the class `menu` to the "
        "`.sh-help` wrapper in ui/shell.js (helpHtml), or let chrome.js:629 and menuNavigation match `.sh-help` too. "
        "Found by running this file against the M174 tree."
    ),
}
"""Keyboard defects that exist today: `"<page>: <description of the element>": reason`. Only the
campaigns layout's Help menu (M174 is merged separately; a tree without `ui/layout.js` never runs those cases)."""

FOCUSABLE: Final[str] = (
    "a[href], button, input:not([type=hidden]), select, textarea, summary, "
    "[tabindex]:not([tabindex='-1']), [contenteditable='true']"
)

SNAPSHOT: Final[str] = """
() => {
  const visible = (el) => {
    if (el.disabled || el.closest('[hidden], [inert]')) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    // content of a closed <details> is not focusable (its summary is)
    const details = el.closest('details:not([open])');
    if (details && !(el.tagName === 'SUMMARY' && el.parentElement === details)) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const label = (el) => {
    const text = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 40);
    const bits = [el.tagName.toLowerCase()];
    if (el.type && el.tagName === 'INPUT') bits.push('[' + el.type + ']');
    if (el.id) bits.push('#' + el.id.replace(/\\d{3,}/g, 'N'));
    return bits.join('') + (text ? ' "' + text + '"' : '');
  };
  // A focus style is a CHANGE between the focused and the unfocused element (or one of its 3 nearest ancestors, for a
  // ring drawn on the wrapper). A style that is there all the time (the shadow of a popup, the fill of the current
  // tab) is not one. Transitions are switched off so the change shows at once.
  const look = (node) => {
    const s = getComputedStyle(node);
    const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0
      ? [s.outlineStyle, s.outlineWidth, s.outlineColor].join(' ') : 'none';
    return [outline, s.boxShadow, s.borderTopColor, s.borderBottomColor, s.backgroundColor].join('|');
  };
  const ringed = (el) => {
    if (!document.getElementById('kb-still')) {
      const still = document.createElement('style');
      still.id = 'kb-still';
      still.textContent = '*, *::before, *::after { transition: none !important; animation: none !important; }';
      document.head.appendChild(still);
    }
    const nodes = [];
    for (let node = el, depth = 0; node && depth < 4; node = node.parentElement, depth += 1) nodes.push(node);
    const focused = nodes.map(look);
    el.blur();
    const plain = nodes.map(look);
    el.focus({ preventScroll: true });
    return focused.some((value, i) => value !== plain[i]);
  };
  const radios = new Set();
  const all = [...document.querySelectorAll(__SELECTOR__)].filter(visible).filter((el) => {
    // Tab visits one radio of a group: the checked one, else the first.
    if (el.tagName === 'INPUT' && el.type === 'radio' && el.name) {
      const group = [...document.querySelectorAll('input[type=radio]')].filter((r) => r.name === el.name && visible(r));
      const stop = group.find((r) => r.checked) || group[0];
      if (stop !== el) return false;
      if (radios.has(el.name)) return false;
      radios.add(el.name);
    }
    return true;
  });
  window.__kb = all;
  const active = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
  const ring = active ? ringed(active) : false;
  return {
    labels: all.map(label),
    focusLabel: document.activeElement && document.activeElement !== document.body ? label(document.activeElement) : null,
    focusIndex: all.indexOf(document.activeElement),
    ringed: ring,
  };
}
""".replace("__SELECTOR__", repr(FOCUSABLE))


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        kwargs: dict[str, Any] = {}
        if CHROMIUM.exists():
            kwargs["executable_path"] = str(CHROMIUM)
        try:
            instance = p.chromium.launch(**kwargs)
        except Exception as problem:  # no usable Chromium in this environment
            pytest.skip(
                f"Chromium cannot start here ({str(problem).splitlines()[0]}); set PLAYWRIGHT_BROWSERS_PATH"
            )
        yield instance
        instance.close()


@pytest.fixture(scope="module", params=["classic", "campaigns"])
def served(request: pytest.FixtureRequest) -> Iterator[tuple[str, ReplayServer]]:
    layout: str = request.param
    if layout == "campaigns" and not has_campaigns_layout():
        pytest.skip("the campaigns layout is not in this tree (ui/layout.js, Plan M M174)")
    with ReplayServer(ui_layout=layout) as server:
        yield layout, server


def open_page(browser: Any, server: ReplayServer, fragment: str, width: int = 1440) -> Any:
    """A page at `fragment`, drawn, with every request outside the replay server refused (no fonts, no network)."""
    context = browser.new_context(viewport={"width": width, "height": VIEWPORT["height"]})
    context.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(server.base) else route.abort(),
    )
    page = context.new_page()
    page.goto(f"{server.base}/ui/{fragment}", wait_until="load")
    page.wait_for_timeout(SETTLE_MS)
    return page


def snapshot(page: Any) -> dict[str, Any]:
    result: dict[str, Any] = page.evaluate(SNAPSHOT)
    return result


def walk(page: Any, limit: int = 300) -> tuple[list[str], list[str], list[str]]:
    """Tab from the top of the page. Returns (what is focusable, in page order; what Tab reached, in order; the
    reached ones that show no focus style)."""
    page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
    expected = snapshot(page)["labels"]
    reached: list[str] = []
    unringed: list[str] = []
    seen: set[int] = set()
    for _ in range(min(limit, len(expected) + 10)):
        page.keyboard.press("Tab")
        state = snapshot(page)
        index = state["focusIndex"]
        if index < 0:
            # Focus left the page's controls (the browser's own UI, or the document): the walk is over.
            break
        if index in seen:
            break  # wrapped round to the start
        seen.add(index)
        reached.append(state["labels"][index])
        if not state["ringed"]:
            unringed.append(state["labels"][index])
    return expected, reached, unringed


def check_walk(name: str, expected: list[str], reached: list[str], unringed: list[str]) -> None:
    assert expected, f"{name}: nothing on the page can take the focus"
    missed = [label for label in expected if label not in reached]
    out_of_order = reached != [label for label in expected if label in reached]
    problems = {
        "not reached by Tab": [m for m in missed if f"{name}: {m}" not in KNOWN],
        "no visible focus style": [u for u in unringed if f"{name}: {u}" not in KNOWN],
    }
    problems = {kind: items for kind, items in problems.items() if items}
    assert not problems, f"{name}: keyboard problems that are not in KNOWN: {problems}\nfocusable: {expected}"
    assert (
        not out_of_order
    ), f"{name}: Tab did not follow the page's order:\nexpected {expected}\nreached  {reached}"
    stale = [
        key for key in KNOWN if key.startswith(f"{name}: ") and key[len(name) + 2 :] not in missed + unringed
    ]
    assert not stale, f"{name}: KNOWN lists a keyboard defect that is gone; remove it: {stale}"


def overview_fragment(layout: str) -> str:
    return "#/overview" if layout == "campaigns" else "#/"


def test_tab_visits_every_control_on_the_overview_in_order_with_a_focus_ring(
    browser: Any, served: tuple[str, ReplayServer]
) -> None:
    layout, server = served
    page = open_page(browser, server, overview_fragment(layout))
    try:
        expected, reached, unringed = walk(page)
        # The skip link is the first stop, then the bar, then the page.
        assert reached and reached[0].startswith(
            "a"
        ), f"the first Tab stop is the skip link, not {reached[:1]}"
        assert "Skip to content" in reached[0]
        assert len(expected) >= 8, f"the Overview has more than {len(expected)} controls: {expected}"
        check_walk(f"overview ({layout})", expected, reached, unringed)
        # Shift+Tab walks back over the same controls.
        # The walk ended with the focus outside the page's controls, so Shift+Tab lands on the last one first.
        assert snapshot(page)["focusIndex"] < 0, "the walk should end outside the page's controls"
        back: list[str] = []
        for _ in range(3):
            page.keyboard.press("Shift+Tab")
            state = snapshot(page)
            assert state["focusIndex"] >= 0, "Shift+Tab left the page's controls too early"
            back.append(state["labels"][state["focusIndex"]])
        assert back == list(reversed(reached))[:3], f"Shift+Tab went {back}, not back over {reached[-3:]}"
    except AssertionError:
        _shot(page, f"overview-{layout}")
        raise
    finally:
        page.context.close()


def test_the_help_menu_opens_with_the_keyboard_and_closes_with_escape(
    browser: Any, served: tuple[str, ReplayServer]
) -> None:
    layout, server = served
    page = open_page(browser, server, overview_fragment(layout))
    try:
        button = page.locator("button[aria-label='Help']")
        assert button.count() == 1, "the Help (?) button is missing from the top bar"
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        for _ in range(60):
            page.keyboard.press("Tab")
            if page.evaluate(
                "() => document.activeElement && document.activeElement.getAttribute('aria-label') === 'Help'"
            ):
                break
        else:
            pytest.fail("Tab never reached the Help button")
        state = snapshot(page)
        assert state["ringed"], "the Help button shows no focus style"
        assert button.get_attribute("aria-expanded") == "false"

        page.keyboard.press("Enter")
        menu_id = button.get_attribute(
            "aria-controls"
        )  # "tn-help" in the classic bar, "sh-help" in the sidebar shell
        assert button.get_attribute("aria-expanded") == "true", "Enter did not open the Help menu"
        assert (
            page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "Help"
        ), "Enter moved the focus off Help"
        assert menu_id, "the Help button does not say which menu it opens (aria-controls)"
        menu = page.locator(f"#{menu_id}")
        assert menu.is_visible()
        entries = page.evaluate(
            "(id) => [...document.querySelectorAll(`#${id} a[href], #${id} button`)].map((e) => (e.innerText || e.getAttribute('aria-label') || '').trim())",
            menu_id,
        )
        assert (
            len(entries) >= 2
        ), f"the Help menu holds {entries}; it should hold at least the glossary and what is new"

        # The arrow keys move through the entries, in order (M173); where they do not, Tab must.
        arrows = f"help menu ({layout}): arrow keys do not move through the entries"
        page.keyboard.press("ArrowDown")
        moved = page.evaluate("(id) => Boolean(document.activeElement.closest('#' + id))", menu_id)
        if moved:
            assert arrows not in KNOWN, f"{arrows} is in KNOWN but they do now; remove it"
            key, first = "ArrowDown", True
        else:
            assert arrows in KNOWN, f"{arrows}: ArrowDown on the Help button did not move into the menu"
            key, first = "Tab", False
        visited: list[str] = []
        in_menu = (
            "(id) => { const e = document.activeElement; const m = document.getElementById(id);"
            " return Boolean(e && m && m.contains(e) && !m.closest('[hidden]') && m.getBoundingClientRect().height > 0"
            " && getComputedStyle(m).visibility !== 'hidden' && getComputedStyle(m).display !== 'none'); }"
        )
        for _ in entries:
            if not first:
                page.keyboard.press(key)
            first = False
            if not page.evaluate(in_menu, menu_id):
                break  # the focus left the open menu: the entries after this one cannot be reached this way
            visited.append(
                page.evaluate(
                    "() => (document.activeElement.innerText || document.activeElement.getAttribute('aria-label') || '').trim()"
                )
            )
            # Only an entry that is inside the open menu is checked for a focus style.
            assert snapshot(page)["ringed"], f"the Help entry {visited[-1]!r} shows no focus style"
            if key == "ArrowDown":
                page.keyboard.press("ArrowDown")
                first = True
        gap = f"help menu ({layout}): the first Tab closes the menu, so no entry can be reached by Tab"
        if visited == entries:
            assert gap not in KNOWN, f"{gap} is in KNOWN but it does not now; remove it"
        else:
            assert key == "Tab" and gap in KNOWN, f"{key} visited {visited}, the menu holds {entries}"
            assert visited == entries[: len(visited)], f"Tab visited {visited}, not the first of {entries}"
            page.focus("button[aria-label='Help']")
            if button.get_attribute("aria-expanded") != "true":
                page.keyboard.press("Enter")

        page.keyboard.press("Escape")
        assert button.get_attribute("aria-expanded") == "false", "Escape did not close the Help menu"
        assert not menu.is_visible()
        assert (
            page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "Help"
        ), "focus did not return to Help"
    except AssertionError:
        _shot(page, f"help-{layout}")
        raise
    finally:
        page.context.close()


def test_the_focus_check_tells_a_ring_from_a_style_that_is_always_there(
    browser: Any, served: tuple[str, ReplayServer]
) -> None:
    """The check compares the focused element with the unfocused one: a control with no focus style inside a box that
    always has a shadow (as every Help menu entry does) must not pass; one with an outline or a ring on focus must.
    """
    _layout, server = served
    page = open_page(browser, server, "#/", width=1440)
    try:
        page.evaluate("""() => {
              const style = document.createElement('style');
              style.textContent = '#kb-plain, #kb-bare:focus, #kb-outlined { outline: none; }'
                + ' #kb-outlined:focus-visible { outline: 3px solid rgb(200, 0, 0); }'
                + ' #kb-ringed:focus-visible { outline: none; box-shadow: 0 0 0 3px rgb(200, 0, 0); }'
                + ' .kb-pop { position: fixed; top: 80px; left: 80px; display: block; }';
              document.head.appendChild(style);
              const pop = document.createElement('div');
              pop.className = 'menu-pop kb-pop';
              pop.innerHTML = '<button id="kb-plain">bare</button> <button id="kb-outlined">outlined</button>'
                + ' <button id="kb-ringed">ringed</button>';
              document.body.appendChild(pop);
            }""")
        assert (
            page.evaluate("() => getComputedStyle(document.querySelector('.kb-pop')).boxShadow") != "none"
        ), ".menu-pop no longer has an always-on shadow; the self-test needs another shadowed container"
        results: dict[str, bool] = {}
        for ident in ("kb-plain", "kb-outlined", "kb-ringed"):
            page.evaluate("() => document.activeElement && document.activeElement.blur()")
            page.keyboard.press("Tab")  # make the browser treat the next focus as the keyboard's
            page.focus(f"#{ident}")
            assert page.evaluate("(id) => document.activeElement.id === id", ident)
            results[ident] = snapshot(page)["ringed"]
        assert results == {"kb-plain": False, "kb-outlined": True, "kb-ringed": True}, results
    finally:
        page.context.close()


def test_the_new_campaign_wizard_first_step_is_reachable_by_tab(
    browser: Any, served: tuple[str, ReplayServer]
) -> None:
    _layout, server = served
    if not any((REPO_ROOT / "ui" / "modules" / "campaigns").glob("wizard*.js")):
        pytest.skip(
            "the New campaign wizard is not in this tree yet (ui/modules/campaigns/wizard*.js, Plan M M177)"
        )
    page = open_page(browser, server, "#/campaigns/new")
    try:
        assert page.locator("h1").count() == 1, "the wizard did not draw"
        assert "campaign" in page.locator("h1").inner_text().lower()
        expected, reached, unringed = walk(page)
        # The bar's controls, then the step's own (its goal choices): the bar alone is fewer than twelve.
        assert len(expected) >= 12, f"the wizard's first step shows too few controls: {expected}"
        check_walk("wizard step 1", expected, reached, unringed)
    except AssertionError:
        _shot(page, "wizard-step-1")
        raise
    finally:
        page.context.close()


def _shot(page: Any, name: str) -> None:
    target = os.environ.get("KEYBOARD_SHOTS")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(target) / f"{name}.png"))
