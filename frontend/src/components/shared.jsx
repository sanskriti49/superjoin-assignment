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

/**
 * Ensures metric titles are informative, descriptive, and concise (not overly verbose sentences),
 * replacing vague abbreviations like 'YoY', 'QoQ', or disclaimer snippets like 'Note Due to rounding off'.
 */
export function formatFactTitle(fact, contextFacts = []) {
  if (!fact) return '';
  let raw = (fact.predicate_label || '').trim();

  // 1. Strip leading noise like "s real GDP" -> "Real GDP", "Contrast Food Inflation" -> "Food Inflation"
  raw = raw.replace(/^s\s+/i, '');
  raw = raw.replace(/^(?:contrast|notably|similarly|subsequently)\s+/i, '');

  // 2. Cut verbose exclusion/disclaimer tails ("Revenue from services excludes revenue from traded goods" -> "Revenue from services")
  const cutMatch = raw.match(/^(.*?)\s+(?:excludes?|excluding|excl\.?|due to|as per|rounded to)\b/i);
  if (cutMatch && cutMatch[1].trim().length >= 4) {
    raw = cutMatch[1].trim();
  }

  // 3. Clean up trailing auxiliary verbs ("Headline inflation has" -> "Headline inflation")
  raw = raw.replace(/\s+(?:has|have|is|are|was|were)$/i, '');

  // 4. Check if label is still vague or a disclaimer
  const low = raw.toLowerCase();
  const isComparison = (
    low === 'yoy'
    || low === 'qoq'
    || low === 'mom'
    || low === 'yoy yoy'
    || low === 'y-o-y'
    || low === 'q-o-q'
    || low === 'performance'
    || low === 'growth'
    || low === 'change'
    || low === 'figure'
    || low === 'addition'
    || low === '000 tons'
    || low === 'tons'
    || low === 'mn'
  );

  const isDisclaimer = (
    low.startsWith('note')
    || low.startsWith('disclaimer')
    || low.startsWith('source')
    || low.startsWith('footnote')
  );

  // If it's a disclaimer note (e.g. "Note Due to rounding off totals may"), represent what was actually measured
  if (isDisclaimer) {
    if (fact.unit === '%' || fact.unit_family === 'ratio') {
      return fact.subject ? `${fact.subject} Segment Share (%)` : 'Segment Distribution (%)';
    }
    return fact.subject ? `${fact.subject} Distribution Share` : 'Distribution Share';
  }

  // If it's a vague comparison qualifier like "YoY" or "QoQ", enrich it
  if (isComparison) {
    const compLabel = low.includes('qoq') ? 'QoQ' : low.includes('mom') ? 'MoM' : 'YoY';

    // A. First look backwards in contextFacts (from the same page or list) for the parent metric
    if (Array.isArray(contextFacts) && contextFacts.length > 0) {
      const myIndex = contextFacts.findIndex((f) => f && f.id === fact.id);
      if (myIndex > 0) {
        for (let i = myIndex - 1; i >= 0; i--) {
          const candidate = contextFacts[i];
          if (!candidate) continue;
          const candRaw = (candidate.predicate_label || '').trim();
          const candLow = candRaw.toLowerCase();
          if (
            candRaw.length >= 4
            && !candLow.includes('yoy')
            && !candLow.includes('qoq')
            && !candLow.startsWith('note')
            && !candLow.startsWith('source')
            && candLow !== 'performance'
          ) {
            const cleanCand = candRaw.replace(/\s+(?:excludes?|due to).*$/i, '').trim();
            return `${cleanCand} (${compLabel})`;
          }
        }
      }
    }

    // B. Next check evidence quote for a metric noun phrase
    const quote = fact.evidence_quote || fact.evidence?.quote || '';
    if (quote) {
      const cleanQuote = quote
        .replace(/\(1\)|\(2\)|\(3\)|\(\d+\)|Note:.*$/gi, '')
        .replace(/[-−(]?\s*[\d,]+(?:\.\d+)?%?\)?/g, '')
        .replace(/YoY[:\s]*|QoQ[:\s]*|MoM[:\s]*/gi, '')
        .replace(/[₹$€£¥]/g, '')
        .trim();

      const candidates = cleanQuote.split(/[;\n/]/).map((s) => s.trim()).filter((s) => s.length >= 4);
      if (candidates.length > 0) {
        const best = candidates[0].replace(/^[-–—:,\s]+|[-–—:,\s]+$/g, '');
        if (best.length >= 4 && best.length <= 50 && !best.toLowerCase().startsWith('note')) {
          return `${best} (${compLabel})`;
        }
      }
    }

    // C. Fallback: qualify with subject
    const subjectPrefix = (fact.subject && fact.subject !== 'Unspecified') ? `${fact.subject} ` : '';
    if (low.includes('qoq')) return `${subjectPrefix}QoQ Growth`.trim();
    if (low.includes('mom')) return `${subjectPrefix}MoM Growth`.trim();
    return `${subjectPrefix}YoY Growth`.trim();
  }

  // If label is too short or just bare punctuation
  if (raw.length <= 3 && !/\d/.test(raw)) {
    return fact.subject ? `${fact.subject} Metric` : raw;
  }

  return raw;
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

/**
 * Renders page text intelligently: detects when multiple consecutive lines form
 * an actual multi-column table (matching column counts), and renders them in an
 * aligned grid/table, while presenting presentation titles, KPI blocks, and prose
 * with readable line formatting.
 */
export function PageContentView({ text }) {
  if (!text) return null;

  const rawLines = text.split('\n').map((ln) => ln.trim()).filter(Boolean);

  // Identify lines that look like table data rows or headers
  const parsedLines = rawLines.map((line) => {
    // A table header with multiple periods/quarters/categories
    const isPeriodHeader = /(?:Q[1-4]\s*(?:FY\d{2,4})?|FY\d{2,4}|YoY|QoQ|H[1-2])/i.test(line) && line.split(/\s+/).length >= 3;
    const numberMatches = line.match(/[-−(]?\s*[\d,]+(?:\.\d+)?%?\)?/g) || [];
    const hasManyNumbers = numberMatches.length >= 3;

    let cells = null;
    if (isPeriodHeader) {
      cells = line.split(/\s{2,}|\t/).filter(Boolean);
      if (cells.length < 3) cells = line.split(/\s+/);
    } else if (hasManyNumbers) {
      // Split leading text label from trailing numeric sequence
      const match = line.match(/^(.*?)((\s+[-−(]?\s*[\d,]+(?:\.\d+)?%?\)?)+)$/);
      if (match) {
        const label = match[1].trim();
        const vals = match[2].trim().split(/\s+/);
        cells = label ? [label, ...vals] : vals;
      } else {
        cells = line.split(/\s{2,}|\t/);
      }
    }

    return {
      raw: line,
      isTableCandidate: Boolean(cells && cells.length >= 3),
      cells: cells || [line],
    };
  });

  // Group into blocks: ONLY treat as table if at least 2 consecutive lines qualify
  const blocks = [];
  let i = 0;
  while (i < parsedLines.length) {
    if (parsedLines[i].isTableCandidate && i + 1 < parsedLines.length && parsedLines[i + 1].isTableCandidate) {
      // Collect consecutive table rows
      const tableRows = [];
      while (i < parsedLines.length && parsedLines[i].isTableCandidate) {
        tableRows.push(parsedLines[i].cells);
        i += 1;
      }
      blocks.push({ type: 'table', rows: tableRows });
    } else {
      blocks.push({ type: 'line', text: parsedLines[i].raw });
      i += 1;
    }
  }

  return (
    <div className="page-content-view" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {blocks.map((block, idx) => {
        if (block.type === 'line') {
          const isTitle = block.text.length < 60 && !/[.!?:;]$/.test(block.text) && !/^\d/.test(block.text);
          const isKPI = /^\d+(?:\.\d+)?%?$|^[-−(]?\s*[\d,]+(?:\.\d+)?%?\)?$/.test(block.text);

          return (
            <div
              key={idx}
              style={{
                fontSize: isTitle ? 16 : isKPI ? 20 : 14.5,
                fontWeight: isTitle ? 600 : isKPI ? 600 : 400,
                fontFamily: isKPI ? 'var(--mono)' : 'var(--serif)',
                color: isTitle ? 'var(--ink)' : isKPI ? 'var(--ink)' : 'var(--ink-soft)',
                padding: isTitle ? '6px 0 2px' : '2px 0',
                lineHeight: 1.5,
              }}
            >
              {block.text}
            </div>
          );
        }

        // Multi-row table block
        const maxCols = Math.max(...block.rows.map((r) => r.length));
        return (
          <div
            key={idx}
            className="scroller"
            style={{
              border: '1px solid var(--rule)',
              background: 'var(--paper-raised)',
              margin: '8px 0',
              boxShadow: '0 1px 2px rgba(0,0,0,0.02)',
            }}
          >
            <table style={{ minWidth: maxCols > 5 ? 700 : '100%', width: '100%', borderCollapse: 'collapse' }}>
              <tbody>
                {block.rows.map((row, rIdx) => {
                  const isHeader = rIdx === 0 && row.some((c) => /FY|Q\d|QoQ|YoY|Date|Year|Month/i.test(c));
                  return (
                    <tr
                      key={rIdx}
                      style={{
                        background: isHeader ? 'var(--paper-sunk)' : 'transparent',
                        borderBottom: '1px solid var(--rule)',
                      }}
                    >
                      {row.map((cell, cIdx) => (
                        <td
                          key={cIdx}
                          className={/\d/.test(cell) ? 'num' : ''}
                          style={{
                            padding: '8px 12px',
                            fontWeight: isHeader || (cIdx === 0 && !/\d/.test(cell)) ? 600 : 400,
                            fontSize: 13,
                            whiteSpace: 'nowrap',
                            textAlign: cIdx > 0 && /[\d%]/.test(cell) ? 'right' : 'left',
                          }}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
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
export function Dialog({ title, onClose, style, children }) {
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
        style={style}
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
