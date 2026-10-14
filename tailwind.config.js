// Build: scripts/build_css.sh (Tailwind v3 standalone CLI, no npm). Same config the Play CDN used in base.html.
module.exports = {
  darkMode: 'class',
  content: ['./app/templates/**/*.html', './app/static/**/*.js', './app/**/*.py'],
  safelist: [
    { pattern: /^(bg|text|border|ring)-(slate|emerald|rose|amber|indigo|cyan|sky|violet|red|green|yellow|blue)-(300|400|500|600)$/ },
    { pattern: /^(col-span|row-span)-\d+$/, variants: ['sm', 'md', 'lg', 'xl'] },
  ],
  theme: { extend: { fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui'] } } },
};
