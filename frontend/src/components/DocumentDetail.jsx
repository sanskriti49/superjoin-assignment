import React, { useEffect, useState } from 'react';
import { deleteDocument, getDocument, getPage } from '../api';
import { Empty, Loading, Quote, formatNumber, percent } from './shared';

export default function DocumentDetail({ documentId, onBack, onDeleted, onNotify }) {
  const [detail, setDetail] = useState(null);
  const [page, setPage] = useState(null);

  useEffect(() => {
    setDetail(null);
    getDocument(documentId).then(setDetail).catch(() => setDetail(null));
  }, [documentId]);

  const remove = async () => {
    if (!window.confirm('Remove this document and everything read from it?')) return;
    try {
      await deleteDocument(documentId);
      onNotify('Document removed.');
      onDeleted();
    } catch (error) {
      onNotify(error.message);
    }
  };

  const showPage = async (pageNumber) => {
    try {
      setPage(await getPage(documentId, pageNumber));
    } catch (error) {
      onNotify(error.message);
    }
  };

  if (!detail) return <Loading what="the document" />;

  const { document: meta, facts, issues } = detail;
  const byPage = facts.reduce((groups, fact) => {
    (groups[fact.evidence.page] ||= []).push(fact);
    return groups;
  }, {});

  return (
    <div>
      <div className="page-head">
        <div>
          <button className="row-button label" onClick={onBack}>Back to overview</button>
          <h2 style={{ marginTop: 6 }}>{meta.title}</h2>
          <p className="mono" style={{ fontSize: 13 }}>{meta.filename}</p>
        </div>
        <button className="action quiet" onClick={remove}>Remove</button>
      </div>

      <div className="figures">
        <div className="figure">
          <span className="value">{meta.pages_processed}</span>
          <span className="name">pages read</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(meta.fact_count)}</span>
          <span className="name">facts</span>
        </div>
        <div className="figure">
          <span className="value">{formatNumber(meta.routine_filters)}</span>
          <span className="name">numbers filtered out</span>
        </div>
        <div className="figure">
          <span className="value">{meta.processing_seconds ?? 0}s</span>
          <span className="name">to process</span>
        </div>
      </div>

      <p className="note">
        Read as being about <strong>{meta.dominant_subject || 'no clear subject'}</strong>
        {meta.dominant_period ? <>, reporting on <strong>{meta.dominant_period}</strong></> : null}.
        Both were learned from the document, and are used only where a sentence does not
        state its own.
      </p>

      <h3>Facts by page</h3>
      {!facts.length ? (
        <Empty>Nothing measurable was found in this document.</Empty>
      ) : (
        Object.keys(byPage)
          .map(Number)
          .sort((a, b) => a - b)
          .slice(0, 40)
          .map((pageNumber) => (
            <div className="panel" key={pageNumber}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
                <span className="label">Page {pageNumber}</span>
                <button className="row-button label" style={{ width: 'auto' }}
                        onClick={() => showPage(pageNumber)}>
                  View page text
                </button>
              </div>
              <div className="scroller" style={{ marginTop: 10 }}>
                <table>
                  <tbody>
                    {byPage[pageNumber].map((fact) => (
                      <tr key={fact.id}>
                        <td style={{ width: '38%' }}>{fact.predicate_label}</td>
                        <td className="num" style={{ width: 140 }}>{fact.value_raw}</td>
                        <td className="num muted" style={{ width: 120 }}>
                          {fact.time_period_normalized || 'unstated'}
                        </td>
                        <td className="num muted" style={{ width: 60 }}>
                          {percent(fact.confidence)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))
      )}

      {issues.length > 0 && (
        <>
          <h3>What was found and not kept</h3>
          <div className="scroller">
            <table>
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Page</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {issues.slice(0, 20).map((issue) => (
                  <tr key={issue.id}>
                    <td className="num">{issue.candidate_text}</td>
                    <td className="num">{issue.page_number}</td>
                    <td className="mono muted">{issue.reason_code}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {page && (
        <div className="overlay" onClick={() => setPage(null)}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <header>
              <h3>Page {page.page_number} as stored</h3>
              <button className="action quiet" onClick={() => setPage(null)}>Close</button>
            </header>
            <p className="prose muted">
              This is the exact text every evidence quote from this page is cut from.
            </p>
            <Quote text={page.text} />
          </div>
        </div>
      )}
    </div>
  );
}
