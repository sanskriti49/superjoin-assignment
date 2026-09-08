import React, { useEffect, useMemo, useState } from 'react';
import { getFact, getFacts } from '../api';
import { Attributes, Empty, Loading, Quote, Verdict, formatNumber, percent } from './shared';

const PAGE_SIZE = 60;

export default function Facts({ documents, initialDocumentId }) {
  const [filters, setFilters] = useState({
    search: '',
    document_id: initialDocumentId || '',
    min_confidence: 0,
  });
  const [data, setData] = useState(null);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null);

  useEffect(() => setOffset(0), [filters]);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      getFacts({ ...filters, limit: PAGE_SIZE, offset })
        .then((result) => !cancelled && setData(result))
        .catch(() => !cancelled && setData({ total: 0, items: [] }));
    }, 180);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [filters, offset]);

  const update = (key) => (event) =>
    setFilters((current) => ({ ...current, [key]: event.target.value }));

  const pages = useMemo(
    () => (data ? Math.ceil(data.total / PAGE_SIZE) : 0),
    [data]
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <h2>Facts</h2>
          <p>
            Every row was read from a sentence on a page, and every row carries that
            sentence with it. Open one to see the text it came from.
          </p>
        </div>
      </div>

      <div className="filters">
        <label className="field">
          <span className="label">Search</span>
          <input
            value={filters.search}
            onChange={update('search')}
            placeholder="metric, value or wording"
          />
        </label>
        <label className="field">
          <span className="label">Document</span>
          <select value={filters.document_id} onChange={update('document_id')}>
            <option value="">All documents</option>
            {documents.map((document) => (
              <option key={document.id} value={document.id}>
                {document.title || document.filename}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">
            Minimum confidence {percent(Number(filters.min_confidence))}
          </span>
          <input
            type="range"
            min="0"
            max="0.95"
            step="0.05"
            value={filters.min_confidence}
            onChange={update('min_confidence')}
          />
        </label>
      </div>

      {!data ? (
        <Loading what="facts" />
      ) : !data.items.length ? (
        <Empty>Nothing matches those filters.</Empty>
      ) : (
        <>
          <p className="mono muted" style={{ fontSize: 13 }}>
            {formatNumber(data.total)} facts
          </p>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Value</th>
                  <th>Period</th>
                  <th>Subject</th>
                  <th>Source</th>
                  <th>Conf.</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((fact) => (
                  <tr key={fact.id}>
                    <td>
                      <button className="row-button" onClick={() => setSelected(fact.id)}>
                        {fact.predicate_label}
                      </button>
                    </td>
                    <td className="num">{fact.value_raw}</td>
                    <td className="num">
                      {fact.time_period_normalized || 'unstated'}
                      {fact.extraction_metadata?.period_inferred_from_document && (
                        <span className="flag">inferred</span>
                      )}
                    </td>
                    <td>{fact.subject}</td>
                    <td className="muted">
                      {fact.document_name}, p{fact.evidence.page}
                    </td>
                    <td className="num">{percent(fact.confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {pages > 1 && (
            <div className="action-row" style={{ marginTop: 20 }}>
              <button
                className="action quiet"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </button>
              <span className="mono muted" style={{ fontSize: 13 }}>
                page {Math.floor(offset / PAGE_SIZE) + 1} of {pages}
              </span>
              <button
                className="action quiet"
                disabled={offset + PAGE_SIZE >= data.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {selected && <FactDialog factId={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function FactDialog({ factId, onClose }) {
  const [fact, setFact] = useState(null);

  useEffect(() => {
    getFact(factId).then(setFact).catch(() => setFact(null));
  }, [factId]);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        {!fact ? (
          <Loading what="the fact" />
        ) : (
          <>
            <header>
              <h3>{fact.predicate_label}</h3>
              <button className="action quiet" onClick={onClose}>Close</button>
            </header>

            <p className="mono" style={{ fontSize: 20, margin: 0 }}>{fact.value_raw}</p>

            <Quote text={fact.evidence.quote} value={fact.value_raw} />

            <Attributes
              rows={[
                ['Subject', fact.subject],
                ['Period', fact.time_period_normalized || 'not stated'],
                ['Unit', `${fact.unit || 'none'} (${fact.unit_family || 'unknown'})`],
                ['Basis', fact.scope],
                ['Reported as', fact.qualifier],
                ['Source', `${fact.document_name}, page ${fact.evidence.page}`],
                ['Characters', `${fact.evidence.char_start} to ${fact.evidence.char_end}`],
                ['Read by', fact.extraction_method],
                ['Confidence', percent(fact.confidence)],
              ]}
            />

            <h3>Grounding</h3>
            <p className="prose" style={{ marginTop: 0 }}>
              {fact.grounding.quote_found_in_page && fact.grounding.offsets_match
                ? 'The quote was found in the stored page text at exactly the recorded character positions.'
                : 'The quote could not be matched against the stored page text.'}
            </p>

            {fact.relationships.length > 0 && (
              <>
                <h3>Compared with</h3>
                {fact.relationships.map((relationship) => {
                  const other = relationship.fact_a_id === fact.id
                    ? relationship.fact_b : relationship.fact_a;
                  return (
                    <div className="panel" key={relationship.id}>
                      <Verdict kind={relationship.relationship_type} />
                      <p style={{ margin: '10px 0 0' }}>
                        <span className="mono">{other?.value_raw}</span>{' '}
                        <span className="muted">
                          in {other?.document_name}, page {other?.evidence?.page}
                        </span>
                      </p>
                      <p className="prose muted" style={{ margin: '8px 0 0', fontSize: 14.5 }}>
                        {relationship.reasoning}
                      </p>
                    </div>
                  );
                })}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
