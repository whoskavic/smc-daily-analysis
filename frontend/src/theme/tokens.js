/**
 * theme/tokens.js — JS mirror of tokens.css, for libraries that can't read
 * CSS custom properties (lightweight-charts draws to <canvas>, xterm.js
 * builds its own color table) — both need literal color strings.
 *
 * jaga tetap sinkron dengan theme/tokens.css.
 */
export const tokens = {
  bg: "#0F1A1F",
  panel: "#1B2429",
  hover: "#273035",

  border: "#303030",
  borderStrong: "#3E3E3E",

  text: "#F6FEFD",
  text2: "#D2DAD7",
  muted: "#949E9C",

  accent: "#50D2C1",
  accentBg: "#0E3333",

  long: "#1FA67D",
  longBg: "#0B3226",
  short: "#ED7088",
  shortBg: "#34242E",

  warn: "#E2C275",

  radius: 5,
  radiusSm: 4,

  font: '"Inter", system-ui, -apple-system, "Segoe UI", sans-serif',
  fontMono: '"JetBrains Mono", ui-monospace, Consolas, monospace',
};

/**
 * Categorical palette for multi-series charts where each series is an
 * arbitrary asset (not a long/short/accent semantic) — e.g. Dashboard's
 * spot-wallet pie chart. Kept here (not inline in the component) so no
 * hex literal lives outside theme/. Built from the same muted, dark-
 * terminal hue family as the core tokens above.
 */
export const chartPalette = [
  tokens.accent, tokens.long, tokens.warn, "#8AB4D6",
  tokens.short, "#C58AF9", "#5FA8A0", "#D68A5F",
  "#9FD68A", "#D65F8A", "#5F8AD6", "#B5B98A",
];
