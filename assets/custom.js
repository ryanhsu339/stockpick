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
