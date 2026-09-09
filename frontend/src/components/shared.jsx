import React from 'react';

export const VERDICT_LABELS = {
  CORROBORATED: 'Corroborated',
  CONTRADICTED: 'Contradicted',
  CONTEXTUALLY_DIFFERENT: 'Explained by context',
  RELATED_BUT_NOT_COMPARABLE: 'Not comparable',
};

export function Verdict({ kind }) {
  return (
    <span className="verdict" data-kind={kind}>
      {VERDICT_LABELS[kind] || kind}
    </span>
  );
}

export function Loading({ what = 'data' }) {
  const [dotCount, setDotCount] = React.useState(0);

  React.useEffect(() => {
    const timer = setInterval(() => {
      setDotCount((prev) => (prev + 1) % 4);
    }, 400);
    return () => clearInterval(timer);
  }, []);

  return (
    <p className="loading">
      Loading {what}{'.'.repeat(dotCount)}
    </p>
  );
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

/** A quote with the measured value marked, so the reader sees what was read. */
export function Quote({ text, value }) {
  if (!text) return null;
  const at = value ? text.indexOf(value) : -1;
  if (at === -1) return <blockquote className="quote" style={{ whiteSpace: 'pre-wrap' }}>{text}</blockquote>;
  return (
    <blockquote className="quote" style={{ whiteSpace: 'pre-wrap' }}>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + value.length)}</mark>
      {text.slice(at + value.length)}
    </blockquote>
  );
}

export function Attributes({ rows }) {
  const shown = rows.filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!shown.length) return null;
  return (
    <dl className="attrs">
      {shown.map(([term, value]) => (
        <React.Fragment key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

export const percent = (value) => `${Math.round((value || 0) * 100)}%`;

export const formatNumber = (value) =>
  value === null || value === undefined ? '' : value.toLocaleString('en-US');

/**
 * A modal that behaves like one.
 *
 * Escape closes it, focus moves inside it when it opens and returns to
 * whatever opened it when it closes, and the page behind it stops scrolling.
 * Without those, a reader who opened a fact with the keyboard has no way back
 * out, and the page underneath quietly scrolls away beneath the overlay.
 */
export function Dialog({ title, onClose, children }) {
  const panel = React.useRef(null);

  React.useEffect(() => {
    const opener = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.focus();

    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = previousOverflow;
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, [onClose]);

  return (
    <div className="overlay" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

/** The header every dialog shares: a title and the way out. */
export function DialogHead({ children, onClose }) {
  return (
    <header>
      <h3>{children}</h3>
      <button className="action quiet" onClick={onClose} aria-label="Close (Escape)">
        Close
      </button>
    </header>
  );
}
