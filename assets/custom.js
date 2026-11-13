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

// The mousedown fix above deliberately keeps the search input focused
// through a suggestion tap (that's what makes the tap register at all --
// see above), but that means the input is still focused once the pick is
// made, so on mobile the on-screen keyboard just sits there over half
// the page instead of dismissing like it would after a normal selection.
// Blurring on the following click (selection is already committed to
// Dash by then) closes it; harmless on desktop, which has no on-screen
// keyboard to dismiss in the first place.
document.addEventListener("click", function (e) {
    if (e.target.closest("#company-suggestions, #company-suggestions-2, #manager-suggestions, #global-search-suggestions")) {
        if (document.activeElement && document.activeElement.blur) {
            document.activeElement.blur();
        }
    }
});

// Mobile only: tapping into a search box clears whatever ticker/manager
// is already in it, so typing a new one doesn't mean backspacing the old
// one first. Desktop leaves this alone (its own mouse-driven workflow --
// click, select-all, or just backspace -- is already fine without it).
// Setting el.value directly wouldn't be picked up by React (these are
// React-controlled inputs, tracked via its own synthetic value setter,
// not the raw DOM property) -- going through the native setter and then
// dispatching a real "input" event is the standard workaround, and also
// what makes Dash's own value-changed wiring (the ticker suggestions
// dropdown, in particular) see the clear too, same as if the user had
// selected-all-and-deleted it themselves.
document.addEventListener("focusin", function (e) {
    if (window.innerWidth > 768) return;
    const id = e.target.id;
    if (id !== "company-input" && id !== "company-input-2" && id !== "manager-input" && id !== "global-search-input") {
        return;
    }
    if (!e.target.value) return;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(e.target, "");
    e.target.dispatchEvent(new Event("input", {bubbles: true}));
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
// go, matching Robinhood/Apple Stocks-style scrubbing.
//
// The live-price pulse dot (_pulse_halo/_pulse_dot, normally pinned to
// the latest point -- see build_price_figure) is meant to follow the
// scrub the same way, but it's a regular Plotly trace marker, rendered
// in a layer that paints BEFORE (so, behind) the spike line's own layer
// -- Plotly always draws the crosshair spike on top of the data traces,
// and unlike a dcc.Graph's hoverData prop there's no supported way to
// change that per-trace paint order. Moving the marker's own SVG element
// later in the DOM (to paint after the spike) was tried and rejected: it
// sits in a nested, transformed coordinate group, and re-parenting it
// shifted its visual position instead of just its paint order. A plain
// HTML div overlaid on top of the whole chart sidesteps the problem
// entirely -- it paints after the SVG by construction, so it's always in
// front of the spike regardless of Plotly's own internal layering.
// Native markers are hidden (opacity 0) for the duration of the scrub so
// there's only ever one visible dot, and restored on release; the
// overlay reuses their existing border color (the period's up/down
// color, fixed for the whole chart, not scrub-point-relative) rather
// than recomputing it.
(function () {
    let activeGd = null;
    let restore = null; // {price, change, color, pulse: {x, y}} captured at gesture start
    let overlayDot = null;

    function pulseTraceIndices(gd) {
        const indices = [];
        for (let i = 0; i < gd._fullData.length; i++) {
            const name = gd._fullData[i].name || "";
            if (name === "_pulse_halo" || name === "_pulse_dot") indices.push(i);
        }
        return indices;
    }

    function setPulseOpacity(gd, indices, opacity) {
        if (!indices.length) return;
        Plotly.restyle(gd, {"marker.opacity": indices.map(() => opacity)}, indices);
    }

    function ensureOverlayDot(gd) {
        if (overlayDot && overlayDot.isConnected && overlayDot.parentNode === gd) return overlayDot;
        overlayDot = document.createElement("div");
        overlayDot.style.position = "absolute";
        overlayDot.style.width = "8px";
        overlayDot.style.height = "8px";
        overlayDot.style.borderRadius = "50%";
        overlayDot.style.pointerEvents = "none";
        overlayDot.style.zIndex = "5";
        overlayDot.style.transform = "translate(-50%, -50%)";
        overlayDot.style.display = "none";
        if (getComputedStyle(gd).position === "static") {
            gd.style.position = "relative";
        }
        gd.appendChild(overlayDot);
        return overlayDot;
    }

    function showOverlayDot(gd, dataX, dataY) {
        const dot = ensureOverlayDot(gd);
        const xaxis = gd._fullLayout.xaxis, yaxis = gd._fullLayout.yaxis, size = gd._fullLayout._size;
        let xPx, yPx;
        try {
            xPx = size.l + xaxis.d2p(dataX);
            yPx = size.t + yaxis.d2p(dataY);
        } catch (e) {
            return;
        }
        const dotIndices = [];
        for (let i = 0; i < gd._fullData.length; i++) {
            if (gd._fullData[i].name === "_pulse_dot") dotIndices.push(i);
        }
        const dotTrace = dotIndices.length ? gd._fullData[dotIndices[0]] : null;
        const borderColor = dotTrace && dotTrace.marker && dotTrace.marker.line ? dotTrace.marker.line.color : "var(--accent)";
        const fillColor = dotTrace && dotTrace.marker ? dotTrace.marker.color : "#fff";
        dot.style.left = xPx + "px";
        dot.style.top = yPx + "px";
        dot.style.backgroundColor = fillColor;
        dot.style.border = "1.5px solid " + borderColor;
        dot.style.display = "block";
    }

    function hideOverlayDot() {
        if (overlayDot) overlayDot.style.display = "none";
    }

    function movePulseDot(gd, x, y) {
        const indices = pulseTraceIndices(gd);
        if (!indices.length) return;
        Plotly.restyle(gd, {x: indices.map(() => [x]), y: indices.map(() => [y])}, indices);
    }

    // A server-driven figure refresh (the 15s price-chart-refresh
    // interval, a theme switch, a range/ticker change, ...) replaces the
    // whole figure via Plotly.react -- which resets marker.opacity back
    // to its server-side default (visible) on the fresh trace instances,
    // undoing the opacity:0 that start() applied to the traces it
    // replaced. Nothing else re-hides it for the rest of the gesture (a
    // plain finger-move only repositions the overlay dot, see scrubTo),
    // so the native latest-price dot would sit visible at the right edge
    // -- a second dot -- for however much of the drag is left. Hooking
    // Plotly's own "afterplot" event, which fires after every such
    // redraw, closes that gap immediately rather than waiting on the
    // next price-pulse-interval tick (which skips entirely while
    // __chartScrubbing, see dash_app.py, instead of re-asserting hidden).
    function ensureAfterplotHook(gd) {
        if (gd.__scrubAfterplotHooked) return;
        gd.__scrubAfterplotHooked = true;
        // Plotly.restyle() -- the very call this handler makes -- itself
        // fires "afterplot" synchronously, so without a hard reentrancy
        // lock this recurses into itself indefinitely (confirmed: an
        // opacity-state check alone wasn't enough to break the loop,
        // hit "Maximum call stack size exceeded" in testing). A plain
        // boolean, true only for the duration of our own restyle call,
        // makes the nested re-entry a same-tick no-op instead.
        let restyling = false;
        gd.on("plotly_afterplot", function () {
            if (restyling || !window.__chartScrubbing) return;
            const indices = pulseTraceIndices(gd);
            if (!indices.length) return;
            restyling = true;
            setPulseOpacity(gd, indices, 0);
            restyling = false;
        });
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
        // The 1D chart's previous close (see build_price_figure's meta),
        // else the window's first point -- same as the header at rest.
        const meta = gd.layout.meta || {};
        const base = meta.baseline || ys[0];
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
        showOverlayDot(gd, xs[idx], price);
        return true;
    }

    function start(e, clientX) {
        const gd = plotDivFor(e.target);
        if (!isScrubbable(gd)) return;
        ensureAfterplotHook(gd);
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
        // Read by the price-pulse-interval clientside callback (see
        // dash_app.py) so its every-900ms restyle skips this gesture
        // entirely, rather than relying on "is the hoverlayer non-empty
        // right now" as a proxy for "mid-scrub" -- Plotly briefly empties
        // the hoverlayer between successive Fx.hover() calls, and that
        // gap landing on the same tick as the interval let the pulse
        // restyle sneak through and flash the latest-price dot back to
        // visible mid-drag, alongside the scrub-following overlay dot
        // elsewhere on the chart -- reading as two dots at once.
        window.__chartScrubbing = true;
        // Hidden for the whole gesture (not re-toggled on every move) so
        // there's only ever one visible dot -- the overlay, drawn in
        // scrubTo below, painting in front of the spike line these
        // native markers would otherwise be stuck behind.
        setPulseOpacity(gd, pulseIndices, 0);
        if (!scrubTo(gd, clientX)) {
            window.__chartScrubbing = false;
            return;
        }
        activeGd = gd;
    }
    function move(clientX) {
        if (activeGd) scrubTo(activeGd, clientX);
    }
    function end() {
        window.__chartScrubbing = false;
        if (activeGd) {
            if (window.Plotly) Plotly.Fx.unhover(activeGd);
            if (restore && restore.pulse) movePulseDot(activeGd, restore.pulse.x, restore.pulse.y);
            setPulseOpacity(activeGd, pulseTraceIndices(activeGd), 1);
            hideOverlayDot();
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

// Plotly basic bundle: see _PLOTLY_BASIC_VERSION in dash_app.py. dcc.Graph
// only downloads window._dashPlotlyJSURL (normally the full 4.7MB
// plotly.min.js) when window.Plotly isn't already defined -- so this
// fetches the 1.2MB basic build in the background once the page is idle,
// and points that fallback URL at the same file in case a chart mounts
// before the background load has finished (e.g. a deep link straight to
// Companies). Keep the version in this URL in sync with the vendored file.
(function () {
    var PLOTLY_BASIC_URL = "/assets/vendor/plotly-basic-4.1.1.min.js";
    try {
        Object.defineProperty(window, "_dashPlotlyJSURL", {
            configurable: true,
            get: function () { return PLOTLY_BASIC_URL; },
            set: function () {},  // ignore dash-renderer's full-bundle URL
        });
    } catch (e) {}
    function preload() {
        if (window.Plotly || document.getElementById("plotly-basic-preload")) {
            return;
        }
        var script = document.createElement("script");
        script.id = "plotly-basic-preload";
        script.src = PLOTLY_BASIC_URL;
        script.async = true;
        document.head.appendChild(script);
    }
    window.addEventListener("load", function () {
        if ("requestIdleCallback" in window) {
            requestIdleCallback(preload, {timeout: 3000});
        } else {
            setTimeout(preload, 1500);
        }
    });
})();

// Hover tooltip for the S&P 500 heatmap's tiles, whose details live in their
// aria-label (see _heatmap_tile). Listens on document since the tiles
// are rebuilt by a callback; a tap fires mousemove too, so this also works on
// phones, where it's the only way to read a tile too small to label.
(function () {
    var tooltip = null;
    function hide() {
        if (tooltip) tooltip.style.display = "none";
    }
    document.addEventListener("mousemove", function (event) {
        var tile = event.target.closest ? event.target.closest(".heatmap-tile") : null;
        if (!tile) {
            hide();
            return;
        }
        if (!tooltip) {
            tooltip = document.createElement("div");
            tooltip.className = "heatmap-tooltip";
            document.body.appendChild(tooltip);
        }
        tooltip.textContent = tile.getAttribute("aria-label");
        tooltip.style.display = "block";
        // Flip to the cursor's other side rather than run off the viewport.
        var box = tooltip.getBoundingClientRect();
        var left = event.clientX + 14, top = event.clientY + 14;
        if (left + box.width > window.innerWidth - 8) left = Math.max(8, event.clientX - box.width - 14);
        if (top + box.height > window.innerHeight - 8) top = Math.max(8, event.clientY - box.height - 14);
        tooltip.style.left = left + "px";
        tooltip.style.top = top + "px";
    }, {passive: true});
    document.addEventListener("scroll", hide, {capture: true, passive: true});
})();

// Builds the Companies tab's ticker bar and S&P 500 heatmap from the data
// load_day_movers sends (_market_ticker_views, _sp500_heatmap_data), as plain
// elements rather than Dash components -- see the clientside callbacks there.
(function () {
    function el(tag, className, text) {
        var node = document.createElement(tag);
        node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function message(className, text) {
        var p = el("p", className, text);
        p.style.cssText = "color: var(--body-text); font-size: 13px; margin: 8px 0;";
        return p;
    }

    window.renderMarketTicker = function (container, views) {
        if (!container) return;
        if (!views || !views.length) {
            container.replaceChildren(el("p", "market-ticker-empty",
                "Couldn't load market prices right now -- try again shortly."));
            return;
        }
        // Every view twice, so there's always more bar to the right while it
        // slides; the slider below jumps back to the start once it reaches
        // the copy of the first view, which looks the same.
        var track = el("div", "market-ticker-track");
        track.id = "market-ticker-track";
        [false, true].forEach(function (copy) {
            views.forEach(function (view) {
                var node = el("div", "market-ticker-view");
                if (copy) node.setAttribute("aria-hidden", "true");
                node.appendChild(el("span", "market-ticker-label", view.title));
                view.items.forEach(function (item) {
                    var entry = el("span", "market-ticker-item");
                    entry.appendChild(el("span", "market-ticker-symbol", item.label));
                    entry.appendChild(el("span", "market-ticker-change market-ticker-" + item.direction, item.change));
                    node.appendChild(entry);
                });
                track.appendChild(node);
            });
        });
        container.replaceChildren(track);
    };

    window.renderSp500Heatmap = function (container, sectors) {
        if (!container) return;
        if (!sectors || !sectors.length) {
            container.replaceChildren(message("heatmap-empty",
                "Couldn't load the S&P 500 heatmap right now -- try again shortly."));
            return;
        }
        var map = el("div", "heatmap-map");
        sectors.forEach(function (sector) {
            var block = el("div", "heatmap-sector");
            Object.assign(block.style, sector.box);
            block.appendChild(el("div", "heatmap-sector-label", sector.name));
            var body = el("div", "heatmap-sector-body");
            sector.tiles.forEach(function (tile) {
                var node = el("div", "heatmap-tile");
                node.setAttribute("role", "img");
                node.setAttribute("aria-label", tile.details);
                Object.assign(node.style, tile.box);
                node.style.backgroundColor = tile.color;
                var text = el("div", "heatmap-tile-text");
                text.appendChild(el("span", "heatmap-tile-name", tile.name));
                text.appendChild(el("span", "heatmap-tile-pct", tile.pct));
                node.appendChild(text);
                body.appendChild(node);
            });
            block.appendChild(body);
            map.appendChild(block);
        });
        container.replaceChildren(map);
    };
})();

// Slides the Companies tab's ticker bar (see renderMarketTicker above) one view at
// a time: a 14s slide to the next view, then a 3s pause. The track holds every
// view twice, so on reaching the copy of the first view it jumps back to the
// real one unseen. Waits while the tab is hidden or reduced motion is on.
// load_day_movers replaces the track on each refresh; the new one picks up at
// the view the old one was on, rather than jumping back to the start.
(function () {
    var SLIDE_MS = 14000, PAUSE_MS = 3000;
    var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    var track = null, index = 0, timer = null;

    function moveTo(el, x, animate) {
        el.style.transition = animate ? "transform " + SLIDE_MS + "ms ease-in-out" : "none";
        el.style.transform = "translateX(" + (-x) + "px)";
    }

    function adopt(current) {
        var oldCount = track ? track.querySelectorAll(".market-ticker-view").length : 0;
        var views = current ? current.querySelectorAll(".market-ticker-view") : [];
        // Same views as before: stay on the one showing (folding a pending
        // wrap onto the real first view). Different ones: start over.
        index = views.length && views.length === oldCount ? index % (views.length / 2) : 0;
        track = current;
        if (track) moveTo(track, views.length ? views[index].offsetLeft : 0, false);
    }

    new MutationObserver(function () {
        var current = document.getElementById("market-ticker-track");
        if (current !== track) adopt(current);
    }).observe(document.body, {childList: true, subtree: true});

    function step() {
        var current = document.getElementById("market-ticker-track");
        if (current !== track) {
            adopt(current);
        } else if (track && track.offsetParent && !reducedMotion.matches) {
            var views = track.querySelectorAll(".market-ticker-view");
            var count = views.length / 2;
            if (count > 1) {
                index += 1;
                moveTo(track, views[index].offsetLeft, true);
                if (index === count) {
                    var wrapping = track;
                    setTimeout(function () {
                        if (track === wrapping) {
                            moveTo(wrapping, 0, false);
                            index = 0;
                        }
                    }, SLIDE_MS);
                }
                timer = setTimeout(step, SLIDE_MS + PAUSE_MS);
                return;
            }
        }
        timer = setTimeout(step, PAUSE_MS);
    }

    timer = setTimeout(step, PAUSE_MS);
})();
