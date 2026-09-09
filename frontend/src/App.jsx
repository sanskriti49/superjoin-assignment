import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

const TAB_IDS = TABS.map(([id]) => id);

/** Read the view out of the address bar, so a reload lands where you were. */
function readLocation() {
  const hash = window.location.hash.replace(/^#\/?/, '');
  const [head, rest] = hash.split('/');
  if (head === 'document' && rest) return { tab: 'overview', documentId: rest, verdict: null };
  return {
    tab: TAB_IDS.includes(head) ? head : 'overview',
    documentId: null,
    verdict: head === 'comparisons' ? rest || null : null,
  };
}

export default function App() {
  const [route, setRoute] = useState(readLocation);
  const [stats, setStats] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [messages, setMessages] = useState([]);
  const [queue, setQueue] = useState([]);
  const [offline, setOffline] = useState(false);

  // The address bar is the source of truth for the view, so Back, Forward and
  // a bookmarked link all work without a router.
  useEffect(() => {
    const sync = () => setRoute(readLocation());
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);

  const go = useCallback((tab, documentId = null, detail = null) => {
    if (documentId) window.location.hash = `#/document/${documentId}`;
    else window.location.hash = detail ? `#/${tab}/${detail}` : `#/${tab}`;
  }, []);

  const notify = useCallback((text, tone = 'info') => {
    const id = `${Date.now()}-${Math.random()}`;
    setMessages((current) => [...current, { id, text, tone }]);
    // A problem stays until it is read and dismissed; a confirmation does not
    // need to be dismissed at all.
    if (tone !== 'error') {
      setTimeout(() => setMessages((current) => current.filter((m) => m.id !== id)), 5000);
    }
  }, []);

  const dismiss = (id) => setMessages((current) => current.filter((m) => m.id !== id));

  const refresh = useCallback(async () => {
    try {
      const [nextStats, nextDocuments] = await Promise.all([getStats(), getDocuments()]);
      setStats(nextStats);
      setDocuments(nextDocuments);
      setOffline(false);
      return nextStats;
    } catch {
      setOffline(true);
      return null;
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // The starter documents load in the background on a fresh database, so the
  // figures are polled until that finishes rather than showing a stale zero.
  useEffect(() => {
    if (!stats?.loading?.running) return undefined;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [stats?.loading?.running, refresh]);

  const upload = useCallback(async (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const pdfs = files.filter((file) => /\.pdf$/i.test(file.name));
    const rejected = files.filter((file) => !pdfs.includes(file));
    rejected.forEach((file) => notify(`${file.name} is not a PDF, so it was skipped.`, 'error'));
    if (!pdfs.length) return;

    setQueue(pdfs.map((file) => ({ name: file.name, state: 'waiting' })));

    let lastAdded = null;
    for (let index = 0; index < pdfs.length; index += 1) {
      const file = pdfs[index];
      setQueue((current) =>
        current.map((item, position) => (position === index ? { ...item, state: 'reading' } : item)));
      try {
        const result = await uploadDocument(file);
        if (result.duplicate_of) {
          notify(`${file.name} is already loaded.`);
        } else {
          lastAdded = result.id;
          notify(
            `${file.name}: ${formatNumber(result.extraction?.facts ?? 0)} facts from `
            + `${result.pages_processed} pages.`
          );
        }
        setQueue((current) =>
          current.map((item, position) => (position === index ? { ...item, state: 'done' } : item)));
      } catch (error) {
        notify(`${file.name} could not be read: ${error.message}`, 'error');
        setQueue((current) =>
          current.map((item, position) => (position === index ? { ...item, state: 'failed' } : item)));
      }
      await refresh();
    }

    setTimeout(() => setQueue([]), 1200);
    // Land on the document that was just read, which is what the reader wants
    // to look at, but only when a single one was uploaded.
    if (lastAdded && pdfs.length === 1) go('overview', lastAdded);
  }, [go, notify, refresh]);

  const busy = queue.some((item) => item.state === 'reading' || item.state === 'waiting');

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="wordmark">
          <h1>Fact Knowledge Layer</h1>
          <p>Facts from PDFs, tied to the text they came from</p>
        </div>

        <nav className="nav">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              aria-current={!route.documentId && route.tab === id}
              onClick={() => go(id)}
            >
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

        <UploadControl onUpload={upload} queue={queue} busy={busy} />

        <div className="sidebar-foot">
          <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API reference</a>
          <br />
          {stats ? `${stats.documents} documents, ${formatNumber(stats.pages)} pages` : ''}
        </div>
      </aside>

      <main className="main">
        {offline && (
          <p className="note" role="alert">
            The API is not answering on port 8000. Start the backend, then{' '}
            <button className="row-button label" style={{ width: 'auto', display: 'inline' }}
                    onClick={refresh}>
              try again
            </button>.
          </p>
        )}

        {route.documentId ? (
          <DocumentDetail
            documentId={route.documentId}
            onBack={() => go('overview')}
            onDeleted={() => { go('overview'); refresh(); }}
            onNotify={notify}
            onShowFacts={() => go('facts')}
          />
        ) : route.tab === 'cases' ? (
          <Cases onOpenDocument={(id) => go('overview', id)} />
        ) : route.tab === 'comparisons' ? (
          <Comparisons
            key={route.verdict || 'all'}
            onNotify={notify}
            onChanged={refresh}
            stats={stats}
            initialKind={route.verdict}
          />
        ) : route.tab === 'facts' ? (
          <Facts documents={documents} onOpenDocument={(id) => go('overview', id)} />
        ) : (
          <Overview
            stats={stats}
            documents={documents}
            onOpenDocument={(id) => go('overview', id)}
            onShowComparisons={(kind) => go('comparisons', null, kind)}
          />
        )}
      </main>

      {messages.length > 0 && (
        <div className="toast" role="status">
          {messages.map((message) => (
            <div key={message.id} className="toast-line" data-tone={message.tone}>
              <span>{message.text}</span>
              <button className="row-button label" style={{ width: 'auto' }}
                      onClick={() => dismiss(message.id)} aria-label="Dismiss">
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function UploadControl({ onUpload, queue, busy }) {
  const input = useRef(null);
  const [dragging, setDragging] = useState(false);

  const summary = useMemo(() => {
    if (!queue.length) return null;
    const done = queue.filter((item) => item.state === 'done' || item.state === 'failed').length;
    const current = queue.find((item) => item.state === 'reading');
    if (!current) return `Read ${done} of ${queue.length}.`;
    return `Reading ${done + 1} of ${queue.length}: ${current.name}`;
  }, [queue]);

  return (
    <div>
      <div
        className={`drop${dragging ? ' active' : ''}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!busy) onUpload(event.dataTransfer.files);
        }}
      >
        {summary ? (
          <span>{summary}</span>
        ) : (
          <>
            <span>Drop PDFs here</span>
            <br />
            <button
              className="action quiet"
              style={{ marginTop: 12 }}
              onClick={() => input.current?.click()}
            >
              Choose files
            </button>
          </>
        )}
      </div>
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        hidden
        onChange={(event) => {
          onUpload(event.target.files);
          event.target.value = '';
        }}
      />
    </div>
  );
}
