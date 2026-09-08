import React, { useEffect, useState } from 'react';
import { getRelationships, recompute } from '../api';
import { Attributes, Empty, Loading, Quote, Verdict, formatNumber, percent } from './shared';

const KINDS = [
  ['', 'All'],
  ['CORROBORATED', 'Corroborated'],
  ['CONTRADICTED', 'Contradicted'],
  ['CONTEXTUALLY_DIFFERENT', 'Explained by context'],
  ['RELATED_BUT_NOT_COMPARABLE', 'Incompatible units / Unanchored'],
];

export default function Comparisons({ onNotify, onChanged }) {
  const [kind, setKind] = useState('CONTRADICTED');
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    setData(null);
    getRelationships({ relationship_type: kind, limit: 60 })
      .then(setData)
      .catch(() => setData({ total: 0, items: [], counts: {} }));
  };

  useEffect(load, [kind]);

  const rebuild = async () => {
    setBusy(true);
    try {
      const result = await recompute();
      onNotify(`Rebuilt ${formatNumber(result.relationships)} links from ${formatNumber(result.facts_compared)} facts.`);
      load();
      onChanged();
    } catch (error) {
      onNotify(error.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h2>Comparisons</h2>
          <p>
            Where two documents talk about the same measurement. Each row states what the
            system concluded and the reasoning that led there, so a wrong call can be
            argued with rather than guessed at.
          </p>
        </div>
        <button className="action quiet" onClick={rebuild} disabled={busy}>
          {busy ? 'Rebuilding' : 'Rebuild all links'}
        </button>
      </div>

      <div className="tabs">
        {KINDS.map(([value, label]) => (
          <button key={value || 'all'} aria-current={kind === value} onClick={() => setKind(value)}>
            {label}
            {data?.counts && value && (
              <span className="mono muted" style={{ marginLeft: 7, fontSize: 12 }}>
                {formatNumber(data.counts[value] || 0)}
              </span>
            )}
          </button>
        ))}
      </div>

      {kind === 'RELATED_BUT_NOT_COMPARABLE' && (
        <p className="note" style={{ marginBottom: 16 }}>
          <strong>About this category:</strong> These pairs discuss the same entity and metric, but cannot be mathematically compared because their units require an external conversion (e.g. currency without exchange rates) or their time periods are unanchored/inferred. Independent documents with completely different topics (e.g. Keystroke Biometrics vs Logistics) do not appear here because they share no metrics.
        </p>
      )}

      {!data ? (
        <Loading what="comparisons" />
      ) : !data.items.length ? (
        <Empty>Nothing of this kind was found in the documents loaded.</Empty>
      ) : (
        <div className="scroller">
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>First</th>
                <th>Second</th>
                <th>Verdict</th>
                <th>Conf.</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((relationship) => (
                <tr key={relationship.id}>
                  <td>
                    <button className="row-button" onClick={() => setOpen(relationship)}>
                      {relationship.fact_a?.predicate_label}
                    </button>
                  </td>
                  <td className="num">{relationship.fact_a?.value_raw}</td>
                  <td className="num">{relationship.fact_b?.value_raw}</td>
                  <td><Verdict kind={relationship.relationship_type} /></td>
                  <td className="num">{percent(relationship.confidence)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && <ComparisonDialog relationship={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function ComparisonDialog({ relationship, onClose }) {
  const factors = relationship.reconciliation_factors || {};

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        <header>
          <h3>{relationship.comparison_summary}</h3>
          <button className="action quiet" onClick={onClose}>Close</button>
        </header>

        <Verdict kind={relationship.relationship_type} />

        <div className="split" style={{ marginTop: 16 }}>
          <Side fact={relationship.fact_a} />
          <Side fact={relationship.fact_b} />
        </div>

        <div className="panel">
          <span className="label">Reasoning</span>
          <p className="prose" style={{ margin: '8px 0 0' }}>{relationship.reasoning}</p>
        </div>

        <h3>Decision factors</h3>
        <div className="scroller">
          <table>
            <tbody>
              {Object.entries(factors).map(([key, value]) => (
                <tr key={key}>
                  <td style={{ width: 230 }} className="muted">{key.replace(/_/g, ' ')}</td>
                  <td className="num">{String(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Side({ fact }) {
  if (!fact) return <div className="panel">Missing.</div>;
  return (
    <div className="panel">
      <p className="mono" style={{ margin: 0, fontSize: 17 }}>{fact.value_raw}</p>
      <Quote text={fact.evidence?.quote} value={fact.value_raw} />
      <Attributes
        rows={[
          ['Source', `${fact.document_name}, page ${fact.evidence?.page}`],
          ['Subject', fact.subject],
          ['Period', fact.time_period || 'not stated'],
          ['Basis', fact.scope],
          ['Confidence', percent(fact.confidence)],
        ]}
      />
    </div>
  );
}
