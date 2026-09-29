// Global "jump to search" shortcut for the sidebar's cross-tracker search
// box (see global-search-input in dash_app.py) -- Cmd+K on Mac, Ctrl+K
// elsewhere, from anywhere on the page, matching the "⌘K" hint already
// shown next to the box. A plain assets/*.js file (not a clientside
// callback) since there's no natural Dash Input to hang a page-wide
// keydown listener off of -- Dash auto-loads and runs this once on load,
// the same way it does assets/custom.css.
document.addEventListener("keydown", function (e) {
    const isMac = navigator.platform.toUpperCase().indexOf("MAC") >= 0;
    const modifierHeld = isMac ? e.metaKey : e.ctrlKey;
    if (!modifierHeld || e.key.toLowerCase() !== "k") {
        return;
    }
    const input = document.getElementById("global-search-input");
    if (!input) {
        return;
    }
    e.preventDefault();
    input.focus();
    input.select();
});

// Ticker/manager/global-search typeahead dropdowns are shown purely via a
// CSS ":focus-within" rule on their wrapper (see custom.css) -- tapping a
// suggestion button on mobile blurs the search input first (on touchstart),
// which flips :focus-within false and hides the dropdown with display:none
// *before* the browser gets to fire the actual "click" on the button, so
// Dash's n_clicks callback never sees the tap (a keyboard Enter still
// works since that goes through n_submit, not this click path). Calling
// preventDefault() on mousedown for anything inside these dropdowns stops
// the browser's default focus-shift there, so the input never blurs, the
// dropdown never disappears mid-tap, and the click that follows on
// mouseup/touchend still fires and reaches Dash normally.
document.addEventListener("mousedown", function (e) {
    if (e.target.closest("#company-suggestions, #company-suggestions-2, #manager-suggestions, #global-search-suggestions")) {
        e.preventDefault();
    }
});

// Price chart scrub (mobile only, see fixedrange in dash_app.py's
// build_price_figure/build_compare_price_figure): a touch-drag across the
// chart is meant to slide the hover crosshair along and update the big
// price/change readout above it, never pan or zoom -- fixedrange already
// blocks any actual range change, but Plotly's own drag-capture rect
// (".nsewdrag", covering the whole plot area, there to catch pan/zoom/
// select drags) suppresses hover redraws for as long as the mouse/finger
// stays down on it, no matter what dragmode is set to or whether the
// axis is fixed -- even a hover triggered manually via Plotly.Fx.hover()
// while pressed gets swallowed. A finger-drag therefore produced nothing
// at all: no pan (expected, and still true below), but also no
// hover/spike update (not expected). custom.css sets pointer-events:none
// on that rect for these mobile figures so presses never reach it in the
// first place, and this drives Plotly's hover manually instead on every
// move while a press is held, gated on the axis's own fixedrange flag so
// it only ever activates for these mobile figures, never desktop (which
// keeps native zoom-drag + hover exactly as before, .nsewdrag included).
//
// The big price/change readout is updated the same way, reading straight
// out of the figure's own "Price" trace data (see build_price_figure) at
// the hovered index, rather than round-tripping through Dash's hoverData
// prop -- Plotly.Fx.hover()'s manual/programmatic path updates the
// on-chart spike/tooltip directly but doesn't reliably emit the
// "plotly_hover" event Dash listens for, so hoverData never actually
// updates when driven this way. The header's pre-drag text is captured
// once per gesture and restored on release, so it snaps back to the real
// latest price/change (not just whatever was last hovered) once you let
// go, matching Robinhood/Apple Stocks-style scrubbing. The live-price
// pulse dot (_pulse_halo/_pulse_dot, normally pinned to the latest point
// -- see build_price_figure) follows the same pattern: restyled to the
// scrubbed point on every move, restored to its real last-point position
// on release.
(function () {
    let activeGd = null;
    let restore = null; // {price, change, color, pulse: {x, y}} captured at gesture start

    function pulseTraceIndices(gd) {
        const indices = [];
        for (let i = 0; i < gd._fullData.length; i++) {
            const name = gd._fullData[i].name || "";
            if (name === "_pulse_halo" || name === "_pulse_dot") indices.push(i);
        }
        return indices;
    }

    function movePulseDot(gd, x, y) {
        const indices = pulseTraceIndices(gd);
        if (!indices.length) return;
        Plotly.restyle(gd, {x: indices.map(() => [x]), y: indices.map(() => [y])}, indices);
    }

    function plotDivFor(target) {
        const container = target && target.closest && target.closest("#price-chart");
        return container ? container.querySelector(".js-plotly-plot") : null;
    }

    function isScrubbable(gd) {
        return !!(gd && gd._fullLayout && gd._fullData && gd.layout && gd.layout.xaxis && gd.layout.xaxis.fixedrange);
    }

    // First trace that isn't one of the single-point "_pulse_*" live-dot
    // markers (see build_price_figure) -- its x-array is what every other
    // trace in the figure shares, used below to find the nearest point to
    // the pointer. "x unified" hovermode then pulls in every other
    // trace's own value at that x automatically, exactly like a normal
    // hover does, so it doesn't matter here which real trace is picked
    // for driving Plotly's own hover visuals. Reads from gd._fullData
    // (Plotly's fully processed traces), not gd.data (the raw figure as
    // passed in) -- past some size threshold Plotly.py itself encodes a
    // numeric array as {dtype, bdata} (a packed/base64 form) rather than
    // a plain array, and gd.data keeps whatever was passed in verbatim;
    // _fullData is always the decoded, indexable version.
    function anchorCurveIndex(gd) {
        for (let i = 0; i < gd._fullData.length; i++) {
            if ((gd._fullData[i].name || "").indexOf("_pulse") !== 0) return i;
        }
        return -1;
    }

    function nearestPointIndex(xs, targetMs) {
        let lo = 0, hi = xs.length - 1;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (new Date(xs[mid]).getTime() < targetMs) lo = mid + 1; else hi = mid;
        }
        if (lo > 0) {
            const dLo = Math.abs(new Date(xs[lo - 1]).getTime() - targetMs);
            const dHi = Math.abs(new Date(xs[lo]).getTime() - targetMs);
            return dLo <= dHi ? lo - 1 : lo;
        }
        return lo;
    }

    function indexForClientX(gd, curveIdx, clientX) {
        const xs = gd._fullData[curveIdx].x;
        if (!xs || !xs.length) return null;
        const size = gd._fullLayout._size;
        const rect = gd.getBoundingClientRect();
        let relPx = clientX - rect.left - size.l;
        relPx = Math.max(0, Math.min(size.w, relPx));
        try {
            const d = gd._fullLayout.xaxis.p2d(relPx);
            const targetMs = d instanceof Date ? d.getTime() : new Date(d).getTime();
            return isNaN(targetMs) ? Math.round((relPx / size.w) * (xs.length - 1))
                                    : nearestPointIndex(xs, targetMs);
        } catch (e) {
            return Math.round((relPx / size.w) * (xs.length - 1));
        }
    }

    function setHeader(priceText, changeText, color) {
        const priceEl = document.getElementById("stock-header-price");
        const changeEl = document.getElementById("stock-header-change");
        if (priceEl) priceEl.textContent = priceText;
        if (changeEl) {
            changeEl.textContent = changeText;
            changeEl.style.color = color;
        }
    }

    function scrubTo(gd, clientX) {
        if (!isScrubbable(gd)) return false;
        const anchorIdx = anchorCurveIndex(gd);
        if (anchorIdx === -1) return false;
        const idx = indexForClientX(gd, anchorIdx, clientX);
        if (idx === null) return false;

        Plotly.Fx.hover(gd, [{curveNumber: anchorIdx, pointNumber: idx}], "xy");

        // Only the single-stock $ chart has a "Price" trace and a big
        // price header (+ live-price pulse dot) to update -- Compare
        // mode's % overlay chart still gets its spike-line scrub above,
        // just not these.
        let priceIdx = -1;
        for (let i = 0; i < gd._fullData.length; i++) {
            if (gd._fullData[i].name === "Price") { priceIdx = i; break; }
        }
        if (priceIdx === -1) return true;
        const priceTrace = gd._fullData[priceIdx];
        const ys = priceTrace.y;
        const xs = priceTrace.x;
        if (!ys || idx >= ys.length) return true;

        const price = ys[idx];
        const base = ys[0];
        const change = price - base;
        const pct = base ? (change / base * 100) : 0;
        const up = change >= 0;
        const sign = up ? "+" : "";
        const arrow = up ? "▲" : "▼";
        setHeader(
            "$" + price.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}),
            arrow + " " + sign + change.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) +
                " (" + sign + pct.toFixed(2) + "%)",
            up ? "var(--up)" : "var(--down)",
        );
        movePulseDot(gd, xs[idx], price);
        return true;
    }

    function start(e, clientX) {
        const gd = plotDivFor(e.target);
        if (!isScrubbable(gd)) return;
        const priceEl = document.getElementById("stock-header-price");
        const changeEl = document.getElementById("stock-header-change");
        const pulseIndices = pulseTraceIndices(gd);
        restore = {
            price: priceEl ? priceEl.textContent : null,
            change: changeEl ? changeEl.textContent : null,
            color: changeEl ? changeEl.style.color : null,
            pulse: pulseIndices.length
                ? {x: gd._fullData[pulseIndices[0]].x[0], y: gd._fullData[pulseIndices[0]].y[0]}
                : null,
        };
        if (!scrubTo(gd, clientX)) return;
        activeGd = gd;
    }
    function move(clientX) {
        if (activeGd) scrubTo(activeGd, clientX);
    }
    function end() {
        if (activeGd) {
            if (window.Plotly) Plotly.Fx.unhover(activeGd);
            if (restore && restore.pulse) movePulseDot(activeGd, restore.pulse.x, restore.pulse.y);
            if (restore && restore.price !== null) setHeader(restore.price, restore.change, restore.color);
        }
        activeGd = null;
        restore = null;
    }

    document.addEventListener("mousedown", function (e) { start(e, e.clientX); });
    document.addEventListener("mousemove", function (e) { move(e.clientX); });
    document.addEventListener("mouseup", end);
    document.addEventListener("touchstart", function (e) {
        if (e.touches.length) start(e, e.touches[0].clientX);
    }, {passive: true});
    document.addEventListener("touchmove", function (e) {
        if (e.touches.length) move(e.touches[0].clientX);
    }, {passive: true});
    document.addEventListener("touchend", end);
})();
