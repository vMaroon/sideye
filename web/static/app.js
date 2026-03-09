/* Sideye — minimal JS utilities */

// Nothing global needed yet; page-specific scripts are in templates.
// This file exists for future shared utilities.

document.addEventListener('DOMContentLoaded', () => {
    // Auto-dismiss success messages after 5s
    document.querySelectorAll('.feedback-success').forEach(el => {
        setTimeout(() => el.style.opacity = '0', 5000);
    });
});
