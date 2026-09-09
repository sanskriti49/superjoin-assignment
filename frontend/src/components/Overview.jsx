import React, { useEffect, useState } from 'react';
import { getSchema } from '../api';
import { Loading, VERDICT_LABELS, formatNumber, percent } from './shared';

const ORDER = ['CORROBORATED', 'CONTRADICTED', 'CONTEXTUALLY_DIFFERENT', 'RELATED_BUT_NOT_COMPARABLE'];

export default function Overview({ stats, documents, onOpenDocument, onShowComparisons }) {
  const [schema, setSchema] = useState(null);

  useEffect(() => {
    getSchema().then(setSchema).catch(() => setSchema(null));
  }, [stats?.facts]);

  if (!stats) return <Loading what="the knowledge layer" />;

  const counts = stats.relationship_counts || {};
  const widest = Math.max(1, ...ORDER.map((kind) => counts[kind] || 0));

  return (
    <div>
      <div className="page-head">
        <div>
          <h2>Overview</h2>
          <p>
            What the system has read, what it took from it, and where those readings
            agree or disagree with each other.
          </p>
        </div>
      </div>

      {stats.loading?.running && (
        <p className="note">
          Reading the starter documents: {stats.loading.loaded} of {stats.loading.total} done
          {stats.loading.current ? `, currently ${stats.loading.current}` : ''}. Figures below
          grow as it goes.
        </p>
      )}

      <div className="figures">
        <div className="figure">
          <span className="value">{formatNumber(stats.documents)}</span>
          <span className="name">documents</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(stats.pages)}</span>
          <span className="name">pages read</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(stats.facts)}</span>
          <span className="name">grounded facts</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(stats.distinct_metrics)}</span>
          <span className="name">distinct metrics</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(stats.relationships)}</span>
          <span className="name">cross-document links</span>
        </div>
        <div className="figure">
          <span className="value">{percent(stats.mean_fact_confidence)}</span>
          <span className="name">mean confidence</span>
        </div>
      </div>

      <h3>How the readings compare</h3>
      <div className="scroller">
        <table>
          <thead>
            <tr>
              <th style={{ width: '30%' }}>Verdict</th>
              <th style={{ width: '12%' }}>Count</th>
              <th>Share</th>
            </tr>
          </thead>
          <tbody>
            {ORDER.map((kind) => (
              <tr key={kind}>
                <td>
                  {onShowComparisons ? (
                    <button
                      className="row-button"
                      title={`Show the ${VERDICT_LABELS[kind].toLowerCase()} comparisons`}
                      onClick={() => onShowComparisons(kind)}
                    >
                      {VERDICT_LABELS[kind]}
                    </button>
                  ) : VERDICT_LABELS[kind]}
                </td>
                <td className="num">{formatNumber(counts[kind] || 0)}</td>
                <td>
                  <span
                    className="bar"
                    data-kind={kind}
                    style={{ width: `${((counts[kind] || 0) / widest) * 100}%` }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3>Documents</h3>
      <div className="scroller">
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Subject</th>
              <th>Pages</th>
              <th>Facts</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.id}>
                <td>
                  <button className="row-button" onClick={() => onOpenDocument(document.id)}>
                    {document.title || document.filename}
                  </button>
                  <div className="mono muted" style={{ fontSize: 12 }}>{document.filename}</div>
                </td>
                <td>{document.dominant_subject || 'not determined'}</td>
                <td className="num">{document.pages_processed}</td>
                <td className="num">{formatNumber(document.fact_count)}</td>
                <td className="mono" title={document.error_message || ''}>
                  {document.status === 'processing' ? 'reading…' : document.status}
                </td>
              </tr>
            ))}
            {!documents.length && (
              <tr>
                <td colSpan={5} className="muted">
                  Nothing loaded yet. Drop a PDF into the panel on the left, or drop two
                  that cover the same subject to see them compared.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {schema && (
        <>
          <h3>Metrics the documents introduced</h3>
          <p className="prose muted">
            No list of metrics is built into the system. Each name below exists because
            some document used those words. {formatNumber(schema.total_predicates)} names
            have appeared so far, {formatNumber(schema.shared_across_documents)} of them in
            more than one document, which is where comparison becomes possible.
          </p>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Unit family</th>
                  <th>Facts</th>
                  <th>Documents</th>
                </tr>
              </thead>
              <tbody>
                {schema.predicates.slice(0, 14).map((row) => (
                  <tr key={row.predicate}>
                    <td>{row.label}</td>
                    <td className="mono">{row.unit_family || 'unknown'}</td>
                    <td className="num">{row.fact_count}</td>
                    <td className="num">{row.document_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
