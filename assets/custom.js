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
