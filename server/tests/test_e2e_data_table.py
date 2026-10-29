"""E2E: data-table.js traits on the Studio Standings table (Playwright/Chromium).

Columns menu (hide/show, reorder, fit, reset), header drag-to-reorder,
layout persistence across a reload, hidden sort columns dropping out of the row order, and keyboard
access (tab order + Enter to sort, arrow-key menu navigation).

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    open_studio_tab,
    run_uvicorn_subprocess,
    seed_tournament,
    tournament_server_env,
    wait_perspective_ready,
    watch_page_errors,
)

WHITE_WIN, BLACK_WIN, DRAW = "1-0", "0-1", "1/2-1/2"

# Long names so the Engine column truncates at its default width (fit test).
ENGINE_A = "engine-with-a-deliberately-long-name-A"
ENGINE_B = "engine-with-a-deliberately-long-name-B"
ENGINES = (ENGINE_A, ENGINE_B)

# A: 3 wins, B: 1 win, 1 draw -> A leads on W and %; B leads on L.
GAMES = [
    ("1", ENGINE_A, ENGINE_B, WHITE_WIN),
    ("2", ENGINE_A, ENGINE_B, WHITE_WIN),
    ("3", ENGINE_B, ENGINE_A, BLACK_WIN),
    ("4", ENGINE_B, ENGINE_A, WHITE_WIN),
    ("5", ENGINE_A, ENGINE_B, DRAW),
]

DEFAULT_LABELS = ["Engine", "G", "W", "L", "D", "Pts", "%", "Elo", "Ordo"]
VIEWPORT = {"width": 1400, "height": 900}

HEAD = ".wb-standings-head-tbl"
MENU_BTN_CLASS = "dt-menu-btn"
MENU_BTN = f"{HEAD} .{MENU_BTN_CLASS}"
TH_LABEL_CLASS = "th-label"
TH_LABEL_SEL = f"{HEAD} th .{TH_LABEL_CLASS}"
# Scoped: Studio has several factory tables, each with its own menu.
MENU = ".wb-standings-table-wrap .dt-menu"

_LABELS = f"() => [...document.querySelectorAll('{TH_LABEL_SEL}')].map(e => e.textContent)"
_ROW_NAMES = (
    "() => [...document.querySelectorAll('.wb-standings-body-tbl tbody td.wb-eng-name')]"
    ".map(e => e.textContent)"
)
_CELLS_PER_ROW = (
    "() => [...document.querySelectorAll('.wb-standings-body-tbl tbody tr')]"
    ".map(tr => tr.cells.length)"
)
# Rendered px width of each header column, keyed by label.
_HEAD_WIDTHS = (
    f"() => Object.fromEntries([...document.querySelectorAll('{HEAD} th')]"
    f".map(th => [th.querySelector('.{TH_LABEL_CLASS}').textContent, th.getBoundingClientRect().width]))"
)
_NAME_TRUNCATED = (
    "() => { const td = document.querySelector('.wb-standings-body-tbl tbody td.wb-eng-name');"
    " return td.scrollWidth > td.clientWidth; }"
)


def _menu_item(key, cls):
    return f'{MENU} .dt-menu-col[data-key="{key}"] .{cls}'


def _th_at(label):
    """Header cell for `label`, by its position in the default layout."""
    return f"{HEAD} th:nth-child({DEFAULT_LABELS.index(label) + 1})"


# Label -> aria-sort for every shown header with an active sort arrow.
_SORTED_HEADERS = (
    f"() => Object.fromEntries([...document.querySelectorAll('{HEAD} th')]"
    ".filter(th => th.getAttribute('aria-sort') !== 'none')"
    f".map(th => [th.querySelector('.{TH_LABEL_CLASS}').textContent, th.getAttribute('aria-sort')]))"
)

# Text of the focused header label, or false when focus is elsewhere.
_FOCUSED_LABEL_TEXT = (
    "(cls) => document.activeElement.classList.contains(cls)"
    " && document.activeElement.textContent"
)


async def _open_standings(page, base):
    await open_studio_tab(page, base, "standings")
    await _wait_rows(page)


async def _wait_rows(page):
    await page.wait_for_function(
        "() => document.querySelectorAll('.wb-standings-body-tbl tbody tr').length === 2",
    )


async def _reload_standings(page):
    """Reload; the persisted perspective + Studio tab reopen Standings."""
    await page.reload()
    await wait_perspective_ready(page)
    await _wait_rows(page)


async def _open_menu(page):
    await page.click(MENU_BTN)
    await page.wait_for_selector(f"{MENU}:popover-open")


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "dt", list(ENGINES), GAMES)
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


@pytest.mark.asyncio
async def test_hide_column_persists_and_engine_locked(server, make_page):
    """Hiding W drops its header and cells and survives a reload; the
    always-on Engine column can't be hidden."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    assert await page.is_disabled(_menu_item("name", "dt-menu-toggle"))
    await page.click(_menu_item("wins", "dt-menu-toggle"))

    expected = [lbl for lbl in DEFAULT_LABELS if lbl != "W"]
    assert await page.evaluate(_LABELS) == expected
    assert await page.evaluate(_CELLS_PER_ROW) == [len(expected)] * 2

    await _reload_standings(page)
    assert await page.evaluate(_LABELS) == expected
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reorder_then_reset(server, make_page):
    """Moving G down swaps it past W; Reset restores the default layout."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    await page.click(f'{_menu_item("games", "dt-menu-move")}[data-dir="1"]')
    assert await page.evaluate(_LABELS) == ["Engine", "W", "G", *DEFAULT_LABELS[3:]]

    await page.click(_menu_item("draws", "dt-menu-toggle"))
    await page.click(f'{MENU} [data-action="reset"]')
    assert await page.evaluate(_LABELS) == DEFAULT_LABELS
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_reorder_skips_hidden_neighbor(server, make_page):
    """A shown column moves past hidden neighbors, so every move shows."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)

    await page.click(_menu_item("wins", "dt-menu-toggle"))
    await page.click(f'{_menu_item("games", "dt-menu-move")}[data-dir="1"]')
    assert await page.evaluate(_LABELS) == ["Engine", "L", "G", "D", "Pts", "%", "Elo", "Ordo"]
    assert_no_page_errors(errors)


# Header drag: pointer steps, and where on the target header the drag ends
# (past its midpoint, so the drop lands after it).
DRAG_STEPS = 8
DROP_FRAC = 0.8
DRAG_GHOST_CLASS = "dt-drag-ghost"
_DRAG_LEFTOVERS = f"() => document.querySelectorAll('.{DRAG_GHOST_CLASS}, .dt-drop-marker').length"
DEFAULT_SORT = {"%": "descending"}


async def _press_and_drag(page, label, onto):
    """Press `label`'s header and drag it over `onto`'s; the button stays down."""
    src = await page.locator(_th_at(label)).bounding_box()
    dst = await page.locator(_th_at(onto)).bounding_box()
    y = src["y"] + src["height"] / 2
    await page.mouse.move(src["x"] + src["width"] / 2, y)
    await page.mouse.down()
    await page.mouse.move(dst["x"] + dst["width"] * DROP_FRAC, y, steps=DRAG_STEPS)


@pytest.mark.asyncio
async def test_drag_header_reorders_and_persists(server, make_page):
    """Dragging G's header past D's midpoint drops it after D without
    sorting G; the order survives a reload."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    await _press_and_drag(page, "G", "D")
    await page.mouse.up()
    expected = ["Engine", "W", "L", "D", "G", *DEFAULT_LABELS[5:]]
    assert await page.evaluate(_LABELS) == expected
    assert await page.evaluate(_SORTED_HEADERS) == DEFAULT_SORT
    assert await page.evaluate(_DRAG_LEFTOVERS) == 0

    await _reload_standings(page)
    assert await page.evaluate(_LABELS) == expected
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_drag_header_escape_cancels(server, make_page):
    """Esc mid-drag drops nothing: the order stays, and the release doesn't
    sort."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    await _press_and_drag(page, "G", "D")
    await page.keyboard.press("Escape")
    await page.mouse.up()
    assert await page.evaluate(_LABELS) == DEFAULT_LABELS
    assert await page.evaluate(_SORTED_HEADERS) == DEFAULT_SORT
    assert await page.evaluate(_DRAG_LEFTOVERS) == 0
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_drag_first_header_copy_leaves_menu_button_out(server, make_page):
    """The first header hosts the columns-menu button; its drag copy shows
    the label only."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    await _press_and_drag(page, "Engine", "G")
    ghost = page.locator(f".{DRAG_GHOST_CLASS}")
    assert await ghost.locator(f".{TH_LABEL_CLASS}").text_content() == "Engine"
    assert await ghost.locator(f".{MENU_BTN_CLASS}").count() == 0
    await page.keyboard.press("Escape")
    await page.mouse.up()
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_fit_columns_widens_content_and_persists(server, make_page):
    """Fit sizes columns by content: the truncated long-name Engine column
    grows, the one-digit G column shrinks, and the widths survive a reload."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    assert await page.evaluate(_NAME_TRUNCATED), "precondition: Engine column truncates"
    before = await page.evaluate(_HEAD_WIDTHS)

    await _open_menu(page)
    await page.click(f'{MENU} [data-action="fit"]')
    after = await page.evaluate(_HEAD_WIDTHS)
    assert after["Engine"] > before["Engine"]
    assert after["G"] < before["G"]

    await _reload_standings(page)
    reloaded = await page.evaluate(_HEAD_WIDTHS)
    for label, width in after.items():
        assert reloaded[label] == pytest.approx(width, abs=1), label
    assert_no_page_errors(errors)


# Header labels of columns with any truncated body cell.
_TRUNCATED_COLS = f"""() => {{
  const labels = [...document.querySelectorAll('{TH_LABEL_SEL}')].map((e) => e.textContent);
  const out = new Set();
  for (const tr of document.querySelectorAll('.wb-standings-body-tbl tbody tr')) {{
    [...tr.cells].forEach((td, i) => {{ if (td.scrollWidth > td.clientWidth) out.add(labels[i]); }});
  }}
  return [...out].sort();
}}"""
# Narrow enough that the Standings content can't all fit.
NARROW_VIEWPORT = {"width": 900, "height": 900}
# Header px widths beyond rounding when comparing column widths.
WIDTH_TOL_PX = 1
# Sort arrow's right edge within its th's padding box (subpixel slack).
_ARROW_FITS = (
    "(th) => th.querySelector('.th-sort-arrow').getBoundingClientRect().right"
    f" <= th.getBoundingClientRect().right - parseFloat(getComputedStyle(th).paddingRight) + {WIDTH_TOL_PX}"
)


async def _fit(page):
    await _open_menu(page)
    await page.click(f'{MENU} [data-action="fit"]')


# fitPcts on content that fits: natural px widths, the available px, minPct.
FIT_NATURAL = [50, 100, 150]
FIT_AVAILABLE = 600
FIT_MIN_PCT = 5
FULL_PCT = 100
PCT_TOL = 1e-9
# Narrow columns under the 80px floor that the 100px extra lifts past it.
FIT_FLOORED_NATURAL = [20, 30, 200, 150]
FIT_FLOORED_AVAILABLE = 800
FIT_FLOORED_MIN_PCT = 10


async def _fit_pcts(page, natural, available, min_pct):
    """fitPcts with no sort-arrow room: minimum = natural."""
    return await page.evaluate(
        """async ([natural, available, minPct]) => {
            const { fitPcts } = await import('/ui/app/column-header.js');
            return fitPcts(natural, natural, available, minPct);
        }""",
        [natural, available, min_pct],
    )


@pytest.mark.asyncio
async def test_fit_spare_is_shared_evenly(server, make_page):
    """With room to spare, every column gets its natural width plus the same
    extra px, so the gaps between contents are even."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    await _open_standings(page, server)
    pcts = await _fit_pcts(page, FIT_NATURAL, FIT_AVAILABLE, FIT_MIN_PCT)
    extra = (FIT_AVAILABLE - sum(FIT_NATURAL)) / len(FIT_NATURAL)
    expected = [(w + extra) / FIT_AVAILABLE * FULL_PCT for w in FIT_NATURAL]
    assert pcts == pytest.approx(expected, abs=PCT_TOL)


@pytest.mark.asyncio
async def test_fit_floored_columns_get_no_extra_spare(server, make_page):
    """Columns the spare lifts past the minPct floor all get the same extra
    px; one under the floor isn't given the floor's slack on top."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    await _open_standings(page, server)
    pcts = await _fit_pcts(page, FIT_FLOORED_NATURAL, FIT_FLOORED_AVAILABLE, FIT_FLOORED_MIN_PCT)
    gaps = [p * FIT_FLOORED_AVAILABLE / FULL_PCT - w for p, w in zip(pcts, FIT_FLOORED_NATURAL)]
    assert gaps == pytest.approx([gaps[0]] * len(gaps), abs=PCT_TOL)


@pytest.mark.asyncio
async def test_fit_when_all_fits_truncates_nothing(server, make_page):
    """With room for everything, Fit gives each column its content width:
    no cell is truncated."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _fit(page)
    assert await page.evaluate(_TRUNCATED_COLS) == []
    assert_no_page_errors(errors)


# Per header column after Fit: [shown px, content px, content px with sort
# arrow room], measured on an auto-layout clone; the first column less the
# Columns button.
_FIT_GAPS = f"""() => {{
  const head = document.querySelector('{HEAD}');
  const body = document.querySelector('.wb-standings-body-tbl');
  const btn = head.querySelector('.{MENU_BTN_CLASS}');
  const btnPx = btn.getBoundingClientRect().width + (parseFloat(getComputedStyle(btn).marginRight) || 0);
  const probe = document.createElement('table');
  probe.className = `${{head.className}} ${{body.className}}`;
  Object.assign(probe.style, {{ position: 'absolute', tableLayout: 'auto', width: 'max-content' }});
  probe.append(head.tHead.cloneNode(true), body.tBodies[0].cloneNode(true));
  body.parentElement.append(probe);
  const widths = () => Array.from(probe.rows[0].cells, (c) => c.getBoundingClientRect().width);
  const content = widths();
  for (const th of probe.querySelectorAll('th.th-sortable:not(:has(.th-sort-arrow))')) {{
    const arrow = document.createElement('wa-icon');
    arrow.className = 'th-sort-arrow';
    arrow.setAttribute('name', 'caret-down');
    th.append(arrow);
  }}
  const withArrow = widths();
  probe.remove();
  return Array.from(head.rows[0].cells, (c, i) => [c.getBoundingClientRect().width, content[i], withArrow[i]]
    .map((w) => (i ? w : w - btnPx)));
}}"""


@pytest.mark.asyncio
async def test_fit_gaps_even_past_sort_arrow_room(server, make_page):
    """With room to spare, Fit leaves the same empty space past every
    column's content; only a column held at its sort-arrow room (so sorting
    it later doesn't truncate the label) may show more."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _fit(page)
    cols = await page.evaluate(_FIT_GAPS)
    gap = min(shown - content for shown, content, _ in cols)
    for shown, content, with_arrow in cols:
        assert (shown - content == pytest.approx(gap, abs=WIDTH_TOL_PX)
                or shown == pytest.approx(with_arrow, abs=WIDTH_TOL_PX)), cols
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_fit_on_overflow_truncates_only_wide_columns(server, make_page):
    """Without room, Fit water-fills: short columns keep their full width and
    only the widest give up space, so every truncated column is at least as
    wide as every untruncated one. Then sorting a column that wasn't sorted
    at fit time doesn't truncate its header (Fit reserved arrow room)."""
    _ctx, page = await make_page(viewport=NARROW_VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _fit(page)
    truncated = set(await page.evaluate(_TRUNCATED_COLS))
    widths = await page.evaluate(_HEAD_WIDTHS)
    assert truncated, "precondition: the content overflows at this width"
    kept = set(widths) - truncated
    assert min(widths[c] for c in truncated) + WIDTH_TOL_PX >= max(widths[c] for c in kept), widths

    # The th's own scrollWidth can't tell: its resize grip overhangs the
    # edge. The new arrow must end inside the cell's content box instead.
    await page.click(f'{_th_at("G")} .{TH_LABEL_CLASS}')
    assert await page.locator(_th_at("G")).evaluate(_ARROW_FITS)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_hidden_sort_column_drops_out_of_order(server, make_page):
    """Sorting W ascending puts B first; hiding W reverts to the deeper
    default (% desc, A first) and moves the arrow to %; showing W again
    restores its sort and arrow."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    w_label = f'{_th_at("W")} .{TH_LABEL_CLASS}'
    await page.click(w_label)  # W opens descending
    await page.click(w_label)  # -> ascending
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_B, ENGINE_A]

    await _open_menu(page)
    await page.click(_menu_item("wins", "dt-menu-toggle"))
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_A, ENGINE_B]
    assert await page.evaluate(_SORTED_HEADERS) == {"%": "descending"}

    await page.click(_menu_item("wins", "dt-menu-toggle"))
    assert await page.evaluate(_ROW_NAMES) == [ENGINE_B, ENGINE_A]
    assert await page.evaluate(_SORTED_HEADERS) == {"W": "ascending"}
    assert_no_page_errors(errors)


# Bottom edge of every header label's text box, in header order.
_LABEL_BOTTOMS = (
    f"() => [...document.querySelectorAll('{TH_LABEL_SEL}')]"
    ".map(e => e.getBoundingClientRect().bottom)"
)
LABEL_ALIGN_TOL_PX = 1


@pytest.mark.asyncio
async def test_header_labels_align_with_menu_button(server, make_page):
    """The columns button in the first header cell doesn't push that cell's
    label off the line the other labels sit on."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)

    bottoms = await page.evaluate(_LABEL_BOTTOMS)
    assert max(bottoms) - min(bottoms) <= LABEL_ALIGN_TOL_PX, bottoms
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_keyboard_tab_order_and_enter(server, make_page):
    """Tab goes menu button -> Engine label; Enter on a label sorts; Enter on
    the menu button opens the menu with focus inside, without sorting; Esc
    closes it and returns focus to the button."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    engine_th = _th_at("Engine")

    await page.focus(MENU_BTN)
    await page.keyboard.press("Tab")
    assert await page.evaluate(
        _FOCUSED_LABEL_TEXT, TH_LABEL_CLASS,
    ) == "Engine"

    await page.keyboard.press("Enter")
    assert await page.get_attribute(engine_th, "aria-sort") == "ascending"

    await page.focus(MENU_BTN)
    await page.keyboard.press("Enter")
    await page.wait_for_selector(f"{MENU}:popover-open")
    assert await page.get_attribute(engine_th, "aria-sort") == "ascending"
    assert await page.evaluate(f"() => document.querySelector('{MENU}').contains(document.activeElement)")

    await page.keyboard.press("Escape")
    await page.wait_for_selector(f"{MENU}:not(:popover-open)", state="attached")
    assert await page.evaluate(
        "(cls) => document.activeElement.classList.contains(cls)", MENU_BTN_CLASS,
    )
    assert_no_page_errors(errors)


_FOCUSED_FID = "() => document.activeElement.dataset.fid"


@pytest.mark.asyncio
async def test_columns_menu_arrow_keys(server, make_page):
    """Opening focuses the first enabled control (Engine's down arrow);
    Up/Down keep the column across rows, Left/Right step within a row,
    Home/End jump, Enter activates, Tab closes and moves on from the button
    to the next tab stop (the Engine label)."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    await _open_menu(page)
    assert await page.evaluate(_FOCUSED_FID) == "name:1"

    for key, fid in (
        ("ArrowDown", "games:1"),
        ("ArrowLeft", "games:-1"),
        ("ArrowLeft", "games:toggle"),
    ):
        await page.keyboard.press(key)
        assert await page.evaluate(_FOCUSED_FID) == fid, key

    await page.keyboard.press("Enter")
    assert "G" not in await page.evaluate(_LABELS)
    assert await page.evaluate(_FOCUSED_FID) == "games:toggle"

    await page.keyboard.press("End")
    assert await page.evaluate(_FOCUSED_FID) == "reset"
    await page.keyboard.press("Home")
    assert await page.evaluate(_FOCUSED_FID) == "name:1"

    await page.keyboard.press("Tab")
    await page.wait_for_selector(f"{MENU}:not(:popover-open)", state="attached")
    assert await page.evaluate(
        _FOCUSED_LABEL_TEXT, TH_LABEL_CLASS,
    ) == "Engine"
    assert_no_page_errors(errors)



# Builds a bare rowNav factory table, moves its cursor with ArrowDown x2,
# re-renders fresh row objects (as a live poll does), and returns the current
# row's data-id before and after the re-render.
_RERENDER_KEEPS_CURSOR = """async () => {
  const { createTable } = await import('/ui/app/data-table.js');
  const t = createTable({
    cls: 'probe', tableClass: 'wb-table', layoutKey: 'probe:layout', minPct: 5,
    columns: [{ key: 'n', label: 'N', width: 100, cell: (r) => r.n }],
    traits: { rowNav: {} },
    rowAttrs: (r) => ({ 'data-id': r.n }),
  });
  document.body.append(t.wrap);
  const rows = () => [1, 2, 3].map((n) => ({ n }));
  t.render(rows());
  t.bodyTable.focus();
  for (let i = 0; i < 2; i++) {
    t.bodyTable.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  }
  const before = t.currentRow()?.dataset.id ?? null;
  t.render(rows());
  return [before, t.currentRow()?.dataset.id ?? null];
}"""


@pytest.mark.asyncio
async def test_rerender_keeps_cursor_row(server, make_page):
    """A data re-render replaces every <tr>; the rowNav cursor stays on the
    row with the same data-id."""
    _ctx, page = await make_page(viewport=VIEWPORT)
    errors = watch_page_errors(page)
    await _open_standings(page, server)
    before, after = await page.evaluate(_RERENDER_KEEPS_CURSOR)
    assert before is not None and after == before, (before, after)
    assert_no_page_errors(errors)
