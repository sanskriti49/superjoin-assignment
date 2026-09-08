import React from 'react';

export const VERDICT_LABELS = {
  CORROBORATED: 'Corroborated',
  CONTRADICTED: 'Contradicted',
  CONTEXTUALLY_DIFFERENT: 'Explained by context',
  RELATED_BUT_NOT_COMPARABLE: 'Incompatible units / Unanchored',
};

export function Verdict({ kind }) {
  return (
    <span className="verdict" data-kind={kind}>
      {VERDICT_LABELS[kind] || kind}
    </span>
  );
}

export function useAnimatedDots(interval = 400, maxDots = 3) {
  const [dots, setDots] = React.useState(0);

  React.useEffect(() => {
    const timer = setInterval(() => {
      setDots((prev) => (prev + 1) % (maxDots + 1));
    }, interval);
    return () => clearInterval(timer);
  }, [interval, maxDots]);

  return '.'.repeat(dots);
}

export function Loading({ what = 'data' }) {
  const dotString = useAnimatedDots();
  return <p className="loading">Loading {what}{dotString}</p>;
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

/** A quote with the measured value marked, so the reader sees what was read. */
export function Quote({ text, value }) {
  if (!text) return null;
  const at = value ? text.indexOf(value) : -1;
  if (at === -1) return <blockquote className="quote">{text}</blockquote>;
  return (
    <blockquote className="quote">
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
