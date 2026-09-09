import React, { useCallback, useEffect, useState } from 'react';
import { deleteDocument, getDocument, getPage } from '../api';
import { Dialog, DialogHead, Empty, Loading, PageContentView, Quote, formatNumber, percent } from './shared';

// Long documents list hundreds of pages of facts. Showing a readable slice and
// letting the reader ask for the rest beats both a wall of tables and a silent
// cut at forty pages.
const PAGES_SHOWN = 12;

export default function DocumentDetail({ documentId, onBack, onDeleted, onNotify }) {
  const [detail, setDetail] = useState(null);
  const [page, setPage] = useState(null);
  const [failed, setFailed] = useState(false);
  const [showAllPages, setShowAllPages] = useState(false);

  const load = useCallback(
    () => getDocument(documentId).then(setDetail).catch(() => setFailed(true)),
    [documentId],
  );

  useEffect(() => {
    setDetail(null);
    setFailed(false);
    load();
  }, [load]);

  // A document opened straight after upload is often still being read. Polling
  // means the page fills in by itself instead of showing an empty document that
  // is not actually empty.
  useEffect(() => {
    if (detail?.document?.status !== 'processing') return undefined;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [detail?.document?.status, load]);

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

  if (failed) {
    return (
      <Empty>
        This document could not be loaded. It may have been removed.{' '}
        <button className="row-button label" style={{ width: 'auto', display: 'inline' }}
                onClick={onBack}>
          Back to overview
        </button>
      </Empty>
    );
  }
  if (!detail) return <Loading what="the document" />;

  const { document: meta, facts, issues } = detail;
  const byPage = facts.reduce((groups, fact) => {
    (groups[fact.evidence.page] ||= []).push(fact);
    return groups;
  }, {});
  const pageNumbers = Object.keys(byPage).map(Number).sort((a, b) => a - b);

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

      {meta.status === 'processing' && (
        <p className="note" role="status">
          Still reading this document. The figures above fill in as it goes.
        </p>
      )}

      {meta.status === 'failed' && (
        <p className="note" role="alert">
          This document could not be read: {meta.error_message || 'no reason was recorded'}.
        </p>
      )}

      <p className="note">
        Read as being about <strong>{meta.dominant_subject || 'no clear subject'}</strong>
        {meta.dominant_period ? <>, reporting on <strong>{meta.dominant_period}</strong></> : null}.
        Both were learned from the document, and are used only where a sentence does not
        state its own.
      </p>

      <h3>Facts by page</h3>
      {!facts.length ? (
        <Empty>
          Nothing measurable was found in this document. The numbers it does contain are
          listed below with the reason each was set aside.
        </Empty>
      ) : (
        pageNumbers
          .slice(0, showAllPages ? pageNumbers.length : PAGES_SHOWN)
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

      {pageNumbers.length > PAGES_SHOWN && !showAllPages && (
        <div className="action-row" style={{ marginTop: 16 }}>
          <button className="action quiet" onClick={() => setShowAllPages(true)}>
            {`Show the remaining ${pageNumbers.length - PAGES_SHOWN} pages`}
          </button>
        </div>
      )}

      {issues.length > 0 && (
        <>
          <h3>What was found and not kept</h3>
          <p className="prose muted" style={{ marginTop: 0, fontSize: 14.5 }}>
            Every number the extractor saw and decided against, with its reason. Routine
            filtering -- page numbers, list markers, bare years -- is counted above rather
            than listed here.
          </p>
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
                    <td className="mono muted" title={issue.detail || ''}>
                      {issue.reason_code.replace(/_/g, ' ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {page && (
        <PageModal page={page} onClose={() => setPage(null)} />
      )}
    </div>
  );
}

function PageModal({ page, onClose }) {
  const [viewMode, setViewMode] = useState('table'); // 'table' or 'raw'

  return (
    <Dialog
      title={`Page ${page.page_number} as stored`}
      onClose={onClose}
      style={{ maxWidth: 900, width: '95vw' }}
    >
      <DialogHead onClose={onClose}>
        {`Page ${page.page_number} as stored`}
      </DialogHead>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <p className="prose muted" style={{ margin: 0 }}>
          This is the canonical page text every evidence quote from this page is cut from.
        </p>
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            className={`action quiet ${viewMode === 'table' ? 'active' : ''}`}
            style={{ fontWeight: viewMode === 'table' ? 600 : 400, borderBottom: viewMode === 'table' ? '2px solid var(--ink)' : 'none' }}
            onClick={() => setViewMode('table')}
          >
            Formatted Table
          </button>
          <button
            className={`action quiet ${viewMode === 'raw' ? 'active' : ''}`}
            style={{ fontWeight: viewMode === 'raw' ? 600 : 400, borderBottom: viewMode === 'raw' ? '2px solid var(--ink)' : 'none' }}
            onClick={() => setViewMode('raw')}
          >
            Raw Text
          </button>
        </div>
      </div>

      <div style={{ maxHeight: '65vh', overflowY: 'auto', border: '1px solid var(--rule)', padding: '14px 18px', background: 'var(--paper-raised)' }}>
        {viewMode === 'table' ? (
          <PageContentView text={page.text} />
        ) : (
          <Quote text={page.text} />
        )}
      </div>
    </Dialog>
  );
}
