import React, { useEffect, useState } from 'react';
import { getRelationships, recompute } from '../api';
import {
  Attributes, Dialog, DialogHead, Empty, Loading, Quote, Verdict, formatNumber, percent,
} from './shared';

const KINDS = [
  ['', 'All'],
  ['CORROBORATED', 'Corroborated'],
  ['CONTRADICTED', 'Contradicted'],
  ['CONTEXTUALLY_DIFFERENT', 'Explained by context'],
  ['RELATED_BUT_NOT_COMPARABLE', 'Not comparable'],
];

export default function Comparisons({ onNotify, onChanged, stats, initialKind }) {
  const [kind, setKind] = useState(initialKind || 'CONTRADICTED');
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
    // Rebuilding throws away every stored link and compares the whole layer
    // again, which takes a while on a large one. Worth asking first.
    const facts = stats?.facts ?? 0;
    const proceed = window.confirm(
      `Compare all ${formatNumber(facts)} facts again and replace every stored link? `
      + 'Nothing else changes, but this can take a moment.'
    );
    if (!proceed) return;
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
        <button
          className="action quiet"
          onClick={rebuild}
          disabled={busy || !(stats?.facts)}
          title="Compare every stored fact again"
        >
          {busy ? 'Rebuilding, this can take a moment' : 'Rebuild all links'}
        </button>
      </div>

      <div className="tabs">
        {KINDS.map(([value, label]) => (
          <button key={value || 'all'} aria-current={kind === value} onClick={() => setKind(value)}>
            {label}
            {data?.counts && (
              <span className="mono muted" style={{ marginLeft: 7, fontSize: 12 }}>
                {formatNumber(value
                  ? data.counts[value] || 0
                  : Object.values(data.counts).reduce((sum, n) => sum + n, 0))}
              </span>
            )}
          </button>
        ))}
      </div>

      {!data ? (
        <Loading what="comparisons" />
      ) : !data.items.length ? (
        <Empty>
          Nothing of this kind was found. Two documents have to cover the same metric,
          for the same subject, in units that can be placed on one scale before they can
          be compared at all.
        </Empty>
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
                    <button
                      className="row-button"
                      title="Show both readings and the reasoning"
                      onClick={() => setOpen(relationship)}
                    >
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

// The comparator's own factor names, said in words. The raw keys are still
// available through the API; a reader should not have to decode them here.
const FACTOR_LABELS = {
  period_established: 'Both figures state the same period',
  period_stated_in_both: 'Both sentences state a period',
  units_comparable: 'Units can be placed on one scale',
  values_match: 'The numbers agree within rounding',
  temporal_match: 'Same period',
  temporal_divergence: 'Different periods',
  scope_divergence: 'Different scope',
  qualifier_divergence: 'Different reporting basis',
  qualifier_one_sided: 'Only one states its basis',
  relative_difference: 'Gap between the figures',
  predicate_match_basis: 'How the metric names were matched',
  predicate_word_overlap: 'Share of the shorter name found in the longer',
  unit_a: 'Unit of the first figure',
  unit_b: 'Unit of the second figure',
};

// Both readings are already shown side by side above the table, so repeating
// their period, scope and basis underneath it says nothing new.
const HIDDEN_FACTORS = new Set([
  'subject_match', 'predicate_match',
  'fact_a_period', 'fact_b_period', 'fact_a_scope', 'fact_b_scope',
  'fact_a_qualifier', 'fact_b_qualifier',
]);

function readFactor(key, value) {
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (key === 'relative_difference') return `${(Number(value) * 100).toFixed(1)}%`;
  return String(value);
}

function ComparisonDialog({ relationship, onClose }) {
  const factors = relationship.reconciliation_factors || {};
  const rows = Object.entries(factors).filter(([key]) => !HIDDEN_FACTORS.has(key));

  return (
    <Dialog title={relationship.comparison_summary} onClose={onClose}>
      <DialogHead onClose={onClose}>{relationship.comparison_summary}</DialogHead>

      <Verdict kind={relationship.relationship_type} />

      <div className="split" style={{ marginTop: 16 }}>
        <Side fact={relationship.fact_a} />
        <Side fact={relationship.fact_b} />
      </div>

      <div className="panel">
        <span className="label">Reasoning</span>
        <p className="prose" style={{ margin: '8px 0 0' }}>{relationship.reasoning}</p>
      </div>

      <h3>Why it was decided that way</h3>
      <p className="prose muted" style={{ marginTop: 0, fontSize: 14.5 }}>
        These are the factors the comparison weighed. Disagreeing with the verdict means
        disagreeing with one of them.
      </p>
      <div className="scroller">
        <table>
          <tbody>
            {rows.map(([key, value]) => (
              <tr key={key}>
                <td style={{ width: 300 }} className="muted">
                  {FACTOR_LABELS[key] || key.replace(/_/g, ' ')}
                </td>
                <td className="num">{readFactor(key, value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Dialog>
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
