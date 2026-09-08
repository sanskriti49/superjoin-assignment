import React, { useEffect, useState } from 'react';
import { getCases } from '../api';
import { Attributes, Empty, Loading, Quote, Verdict, formatNumber, percent } from './shared';

const SHORT = {
  CASE_1: 'Corroborated',
  CASE_2: 'Contradiction',
  CASE_3: 'Explained by context',
  CASE_4: 'Failures',
};

export default function Cases({ onOpenDocument }) {
  const [cases, setCases] = useState(null);
  const [active, setActive] = useState(0);

  useEffect(() => {
    getCases().then((data) => setCases(data.cases)).catch(() => setCases([]));
  }, []);

  if (!cases) return <Loading what="the four cases" />;
  if (!cases.length) return <Empty>No cases could be built.</Empty>;

  const current = cases[active];

  return (
    <div>
      <div className="page-head">
        <div>
          <h2>The four cases</h2>
          <p>
            Each of these is a query against what is currently in the database, not a
            written-up example. Load different documents and different examples appear
            here, or none, if the documents have nothing in common.
          </p>
        </div>
      </div>

      <div className="tabs">
        {cases.map((entry, index) => (
          <button
            key={entry.case_number}
            aria-current={index === active}
            onClick={() => setActive(index)}
          >
            {index + 1}. {SHORT[entry.case_number]}
          </button>
        ))}
      </div>

      <h3 style={{ marginTop: 0 }}>{current.title}</h3>
      <p className="note">{current.question}</p>

      {current.case_number === 'CASE_4'
        ? <FailureCase data={current} />
        : <ComparisonCase data={current} onOpenDocument={onOpenDocument} />}
    </div>
  );
}

function ComparisonCase({ data, onOpenDocument }) {
  if (!data.available) return <Empty>{data.explanation}</Empty>;

  return (
    <div>
      <div style={{ display: 'flex', gap: 18, alignItems: 'baseline', marginBottom: 16 }}>
        <Verdict kind={data.relationship_type} />
        <span className="mono muted">
          confidence {percent(data.confidence)} · {formatNumber(data.alternatives)} pairs of this
          kind in total
        </span>
      </div>

      <div className="split">
        <FactPanel fact={data.fact_a} side="First reading" onOpenDocument={onOpenDocument} />
        <FactPanel fact={data.fact_b} side="Second reading" onOpenDocument={onOpenDocument} />
      </div>

      <div className="panel">
        <span className="label">What the comparison found</span>
        <div className="scroller" style={{ marginTop: 12 }}>
          <table>
            <tbody>
              {data.comparison.map((row) => (
                <tr key={row.label}>
                  <td style={{ width: 190 }} className="muted">{row.label}</td>
                  <td className="num">{row.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="prose" style={{ marginTop: 18, marginBottom: 0 }}>{data.reasoning}</p>
      </div>
    </div>
  );
}

function FactPanel({ fact, side, onOpenDocument }) {
  if (!fact) return <div className="panel">Missing.</div>;
  return (
    <div className="panel">
      <span className="label">{side}</span>
      <p style={{ margin: '8px 0 0', fontSize: 18 }}>
        {fact.predicate}{' '}
        <strong className="mono" style={{ fontSize: 17 }}>{fact.value_raw}</strong>
      </p>
      <p className="mono muted" style={{ margin: '4px 0 0', fontSize: 13 }}>
        normalized to {fact.value_normalized}
      </p>

      <Quote text={fact.quote} value={fact.value_raw} />

      <Attributes
        rows={[
          ['Source', (
            <button className="row-button" onClick={() => onOpenDocument(fact.document_id)}>
              {fact.document}, page {fact.page}
            </button>
          )],
          ['Subject', <>
            {fact.subject}
            {fact.subject_inferred && <span className="flag">from document</span>}
          </>],
          ['Period', <>
            {fact.time_period}
            {fact.period_inferred && <span className="flag">from document</span>}
          </>],
          ['Basis', fact.scope],
          ['Reported as', fact.qualifier],
          ['Confidence', percent(fact.confidence)],
        ]}
      />
    </div>
  );
}

function FailureCase({ data }) {
  return (
    <div>
      <p className="prose">
        The extractor records what it refuses and why. It discarded{' '}
        {formatNumber(data.routine_filter_count)} numbers that carried no unit, which is
        what page numbers, list markers and dates in running text look like. The rest are
        cases where a real quantity was found but something about it could not be settled.
      </p>

      <h3>Rejections, counted from the documents loaded</h3>
      {data.categories.length ? (
        data.categories.map((category) => (
          <div className="panel" key={category.reason_code}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
              <span className="mono">{category.reason_code}</span>
              <span className="mono muted">{formatNumber(category.count)}</span>
            </div>
            <p className="prose" style={{ margin: '10px 0 0' }}>{category.explanation}</p>
            {category.example && (
              <>
                <Quote text={category.example.context} value={category.example.candidate} />
                <p className="mono muted" style={{ margin: '8px 0 0', fontSize: 12.5 }}>
                  {category.example.document}, page {category.example.page}
                </p>
              </>
            )}
          </div>
        ))
      ) : (
        <Empty>Nothing was rejected in the documents loaded.</Empty>
      )}

      {data.weakest_facts.length > 0 && (
        <>
          <h3>The least certain facts it kept</h3>
          <p className="prose muted">
            These were stored rather than discarded, but the comparator will not draw a
            conclusion from them.
          </p>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Value</th>
                  <th>Confidence</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {data.weakest_facts.map((fact) => (
                  <tr key={fact.id}>
                    <td>{fact.predicate}</td>
                    <td className="num">{fact.value_raw}</td>
                    <td className="num">{percent(fact.confidence)}</td>
                    <td className="muted">{fact.document}, page {fact.page}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <h3>Failure modes found while building this</h3>
      {data.known_limits.map((limit) => (
        <div className="panel" key={limit.name}>
          <strong>{limit.name}</strong>
          <p className="prose" style={{ margin: '8px 0 0' }}>{limit.detail}</p>
          <p className="prose muted" style={{ margin: '8px 0 0' }}>
            <span className="label">What the system does</span><br />
            {limit.handling}
          </p>
        </div>
      ))}
    </div>
  );
}
