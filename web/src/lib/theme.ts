// Shared design tokens for waifu-companion.
//
// Every page should pull from here instead of hard-coding colors,
// radii, or font sizes inline. Keeps the visual language consistent
// across Home / Avatar / Pulse / Settings.
//
// Scoped CSS (hover/focus states, etc.) lives in <AppStyles /> below
// because inline styles can't reach those pseudo-classes.

import { useEffect } from 'react';
import type React from 'react';

export const tokens = {
  // Surfaces
  bgPage:    '#0b0d10',   // page background
  bgPanel:   '#161a20',   // section / card background
  bgPanelHi: '#1c2128',   // subtle inset panel (hint / info boxes)
  bgInput:   '#0d1015',   // input background
  bgNav:     '#0e1014',   // nav bar background

  // Lines + text
  border:    '#262a31',
  borderHi:  '#3a4150',   // hover / focused input border
  text:      '#e6e9ef',
  textMuted: '#9aa3b2',
  textDim:   '#6b7280',
  textInverse: '#0b0d10', // text on primary fills

  // Status
  primary:   '#3b82f6',
  primaryHi: '#4f8cff',
  success:   '#10b981',
  warn:      '#f59e0b',
  danger:    '#ef4444',

  // Geometry
  radius:    8,
  radiusSm:  6,
  radiusXs:  4,

  // Typography scale (px)
  fontPage:    28, // h1 page header
  fontSection: 15.5, // h2 section header
  fontBody:    13,
  fontLabel:   12.5,
  fontHint:    11.5,
} as const;

/// One-time global stylesheet injected once at app start. Hover /
/// focus / active states for inputs and buttons live here because
/// inline styles can't reach `:hover` / `:focus-visible`. Selectors
/// are deliberately permissive — they target naked elements + the
/// `.ws-btn` class — so any page using the theme picks them up.
export function AppStyles() {
  // Inject the rules once and remove on unmount.
  useEffect(() => {
    const id = 'waifu-companion-app-styles';
    if (document.getElementById(id)) return;
    const style = document.createElement('style');
    style.id = id;
    style.textContent = `
      /* Inputs + selects + textareas — anywhere on the app */
      input[type=text],
      input[type=password],
      input[type=number],
      input[type=search],
      select,
      textarea {
        transition: border-color 120ms ease, box-shadow 120ms ease, background 120ms ease;
      }
      input[type=text]:hover,
      input[type=password]:hover,
      input[type=number]:hover,
      input[type=search]:hover,
      select:hover,
      textarea:hover {
        border-color: ${tokens.borderHi};
      }
      input:focus-visible,
      select:focus-visible,
      textarea:focus-visible {
        border-color: ${tokens.primary};
        box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.22);
      }

      /* Themed buttons — opt in by setting className="ws-btn" */
      .ws-btn {
        transition: background 120ms ease, border-color 120ms ease, color 120ms ease, transform 80ms ease;
      }
      .ws-btn:not(:disabled):hover {
        border-color: ${tokens.borderHi};
        color: ${tokens.text};
      }
      .ws-btn--primary:not(:disabled):hover {
        background: ${tokens.primaryHi};
      }
      .ws-btn:not(:disabled):active {
        transform: translateY(1px);
      }
      .ws-btn:focus-visible {
        outline: 2px solid ${tokens.primary};
        outline-offset: 2px;
      }

      /* Inline code styling — consistent everywhere code appears */
      code {
        background: rgba(255,255,255,0.06);
        padding: 1px 6px;
        border-radius: ${tokens.radiusXs}px;
        font-size: 11.5px;
        color: ${tokens.text};
        font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      }

      /* Selection highlight */
      ::selection {
        background: rgba(59, 130, 246, 0.35);
        color: #fff;
      }

      /* Pulsing dot for the "<character> is thinking…" chat indicator */
      .ws-typing-dot {
        width: 7px; height: 7px; border-radius: 50%;
        background: ${tokens.primary};
        display: inline-block; flex-shrink: 0;
        animation: ws-typing-pulse 1.2s ease-in-out infinite;
      }
      @keyframes ws-typing-pulse {
        0%, 100% { opacity: 0.3; transform: scale(0.85); }
        50%      { opacity: 1;   transform: scale(1.1); }
      }
    `;
    document.head.appendChild(style);
    return () => {
      const existing = document.getElementById(id);
      if (existing) existing.remove();
    };
  }, []);
  return null;
}

/// Default input style. Use `monoInputStyle` for paths / URLs / code.
export const inputStyle: React.CSSProperties = {
  flex: '1 1 280px',
  minWidth: 220,
  background: tokens.bgInput,
  color: tokens.text,
  padding: '9px 12px',
  borderRadius: tokens.radiusSm,
  border: `1px solid ${tokens.border}`,
  fontSize: tokens.fontBody,
  fontFamily: 'inherit',
  outline: 'none',
};

export const monoInputStyle: React.CSSProperties = {
  ...inputStyle,
  fontFamily: 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace',
  fontSize: 12.5,
};
