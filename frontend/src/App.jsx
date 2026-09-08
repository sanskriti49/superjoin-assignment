import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getDocuments, getStats, uploadDocument } from './api';
import Cases from './components/Cases';
import Comparisons from './components/Comparisons';
import DocumentDetail from './components/DocumentDetail';
import Facts from './components/Facts';
import Overview from './components/Overview';
import { formatNumber } from './components/shared';

const TABS = [
  ['overview', 'Overview'],
  ['cases', 'The four cases'],
  ['comparisons', 'Comparisons'],
  ['facts', 'Facts'],
];

export default function App() {
  const [tab, setTab] = useState('overview');
  const [stats, setStats] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [openDocument, setOpenDocument] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState(null);

  const notify = useCallback((text) => {
    setMessage(text);
    setTimeout(() => setMessage(null), 5000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextStats, nextDocuments] = await Promise.all([getStats(), getDocuments()]);
      setStats(nextStats);
      setDocuments(nextDocuments);
      return nextStats;
    } catch {
      notify('Could not reach the API. Is the backend running on port 8000?');
      return null;
    }
  }, [notify]);

  useEffect(() => { refresh(); }, [refresh]);

  // The starter documents load in the background on a fresh database, so the
  // figures are polled until that finishes rather than showing a stale zero.
  useEffect(() => {
    if (!stats?.loading?.running) return undefined;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [stats?.loading?.running, refresh]);

  const upload = async (file) => {
    if (!file) return;
    setUploading(true);
    try {
      const result = await uploadDocument(file);
      notify(
        result.duplicate_of
          ? 'That document is already loaded.'
          : `Read ${formatNumber(result.extraction?.facts ?? 0)} facts from ${result.pages_processed} pages.`
      );
      await refresh();
      if (!result.duplicate_of) setOpenDocument(result.id);
    } catch (error) {
      notify(error.message);
    } finally {
      setUploading(false);
    }
  };

  const show = (nextTab) => {
    setOpenDocument(null);
    setTab(nextTab);
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="wordmark">
          <h1>Fact Knowledge Layer</h1>
          <p>Facts from PDFs, tied to the text they came from</p>
        </div>

        <nav className="nav">
          {TABS.map(([id, label]) => (
            <button key={id} aria-current={!openDocument && tab === id} onClick={() => show(id)}>
              {label}
              {id === 'facts' && stats && (
                <span className="count">{formatNumber(stats.facts)}</span>
              )}
              {id === 'comparisons' && stats && (
                <span className="count">{formatNumber(stats.relationships)}</span>
              )}
            </button>
          ))}
        </nav>

        <UploadControl onUpload={upload} busy={uploading} />

        <div className="sidebar-foot">
          <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API reference</a>
          <br />
          {stats ? `${stats.documents} documents, ${formatNumber(stats.pages)} pages` : ''}
        </div>
      </aside>

      <main className="main">
        {openDocument ? (
          <DocumentDetail
            documentId={openDocument}
            onBack={() => setOpenDocument(null)}
            onDeleted={() => { setOpenDocument(null); refresh(); }}
            onNotify={notify}
          />
        ) : tab === 'overview' ? (
          <Overview stats={stats} documents={documents} onOpenDocument={setOpenDocument} />
        ) : tab === 'cases' ? (
          <Cases onOpenDocument={setOpenDocument} />
        ) : tab === 'comparisons' ? (
          <Comparisons onNotify={notify} onChanged={refresh} />
        ) : (
          <Facts documents={documents} />
        )}
      </main>

      {message && <div className="toast">{message}</div>}
    </div>
  );
}

function UploadControl({ onUpload, busy }) {
  const input = useRef(null);
  const [dragging, setDragging] = useState(false);

  return (
    <div>
      <div
        className={`drop${dragging ? ' active' : ''}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          onUpload(event.dataTransfer.files?.[0]);
        }}
      >
        {busy ? (
          <span>Reading the document.</span>
        ) : (
          <>
            <span>Drop a PDF here</span>
            <br />
            <button
              className="action quiet"
              style={{ marginTop: 12 }}
              onClick={() => input.current?.click()}
            >
              Choose a file
            </button>
          </>
        )}
      </div>
      <input
        ref={input}
        type="file"
        accept="application/pdf"
        hidden
        onChange={(event) => {
          onUpload(event.target.files?.[0]);
          event.target.value = '';
        }}
      />
    </div>
  );
}
