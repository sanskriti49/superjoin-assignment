# Fact Knowledge Layer

Reads PDFs, pulls out the measured claims inside them, keeps each claim tied to the
exact words it came from, and then compares claims across documents to say where they
agree, where they conflict, and where a conflict is only apparent.

Built for the Superjoin VIT 2026 engineering intern assignment.

---

## 1. Setup and run

Requires Python 3.10 or newer and Node 18 or newer. The database is SQLite and needs
no setup.

```bash
./run.sh          # macOS and Linux
run.bat           # Windows
```

Then open <http://localhost:5173>. The API reference is at <http://localhost:8000/docs>.

On the first launch the six PDFs in `starter-datasets/` are read in the background,
which takes about 40 seconds. The interface is usable while that happens and the
figures fill in as documents finish.

### Running the parts separately

```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

### Tests

```bash
cd backend
python -m pytest tests -v
```

85 tests. They run against a temporary database with the starter loader switched off,
so they never touch the development data. The extraction tests use PDFs generated at
test time about invented companies, so nothing in the extractor can have been tuned to
the material it is checked against.

`tests/test_unseen_domains.py` goes further and runs the whole pipeline over invented
documents from healthcare, energy, public transit and higher education -- domains the
starter set never touches -- asserting what should hold for any document at all: a
counted noun becomes a fact, a multi-word name becomes the subject, a quote is a whole
sentence, a period is not taken from the figure being compared against, and two
documents about one subject can both contradict and corroborate each other. A rule that
works only because it was written while looking at these six PDFs passes the rest of
the suite and fails here.

### Configuration

Everything is optional; copy `backend/.env.example` to `backend/.env` to change any of
it. `DATABASE_URL` accepts a PostgreSQL URL. `LOAD_STARTER_DATASET=false` starts empty.
There are no credentials in the repository and none are needed to run it.

---

## 2. Video demo

`https://drive.google.com/file/d/1J0hZ8Cb5p728INNgeUEvqwwpArDWWQYr/view?usp=sharing`

Suggested walkthrough:

1. Overview: six documents, 511 pages, about 2,800 facts, roughly 680 cross-document
   links, and the metric vocabulary the documents introduced by themselves.
2. Upload a PDF and watch facts appear with their page numbers and quotes.
3. The four cases tab, which is a live query rather than a written-up example.
4. Open a fact, then open the stored page text behind it, and check the quote by eye.

---

## 3. Approach

### The problem, stated precisely

A useful fact is not just a number. It is a number plus what it measures, what it
measures it for, when, and on what basis. Two figures only conflict if all four of
those line up and the numbers still disagree. Most of the work is establishing those
four things well enough to know whether a comparison is even allowed.

### Shape of the system

```
PDF ─► ingestion ─► extraction ─► storage ─► comparison ─► API ─► web interface
       canonical    measured      facts +    verdicts +
       page text    quantities    page text  reasoning
```

**Ingestion** (`backend/app/pipeline/ingestion.py`) turns each page into one canonical
text and stores it. PDF text layers break lines at typeset width, which cuts sentences
in half. Lines are rejoined by measuring the page's own column width: a line running
close to full width that does not end a sentence was wrapped, so the next line
continues it. A short line was broken on purpose and stands alone. A line set in title
case with no terminal punctuation is a heading and never continues into the sentence
below it, which keeps a document's own title out of its first claim. One rule handles
both a two-column annual report and a slide of KPI tiles, because the width is measured
per page rather than assumed.

**Extraction** (`backend/app/pipeline/extraction.py`) looks for one thing: a number
carrying a currency, a magnitude word, a percent sign or a measurement noun. A number
without any of those is not a measurement, which is how page numbers, list markers and
section references are filtered out. Around 30,000 such numbers are discarded across
the starter set.

A measurement noun is recognised by shape rather than from a list. A unit is a physical
symbol (`MW`, `kWh`, `kg`), a spelled-out unit (`tonnes`, `days`), or any plural common
noun, because English marks a counted thing by making it plural. That is what lets a
document introduce "412,600 outpatient visits" or "84.2 million boardings" without a
line of code being added for healthcare or public transit. A modifier may stand between
the number and its noun; a preposition may not, so "page 17 for details" counts
nothing.

For each surviving quantity it reads the surrounding text four ways, in order of how
reliable each is:

| Layout | Example | Where the metric name is |
| --- | --- | --- |
| `prose` | "headline inflation moderated to 4.6 per cent" | before the reporting verb |
| `prose` | "the Auditor confirms ridership of 84.2 million" | after a verb of saying |
| `trailing` | "740 Mn express parcel shipments" | after the number |
| `next_line` | "₹8,142 Cr" then "FY24 revenue from services" | the caption below |
| `prev_line` | a table row under its header | the caption above |

The metric name is whatever the page calls it. There is no list of metrics anywhere in
the code, which is what lets the schema grow on its own: a document about shipping
introduces shipping metrics, and one about caviar introduces caviar metrics, with no
change to the system. `/api/facts/schema` shows the vocabulary the loaded documents
have produced.

A metric name is read from one clause. It may not reach back across an earlier figure
on the line, so "11.4 per cent in 2024, below the national benchmark of 13.1 per cent"
yields two metrics rather than one repeated; and it may not contain the subject's own
name, so "readmission rate for Northfield Regional Health System" and "readmission
rate" are one metric rather than two.

The subject, period and reporting basis come from the sentence when it states them, and
from the document as a whole when it does not. A fact that inherited either is flagged,
and the flag travels with it into every comparison and onto the screen.

**Whose figures are these?** Two documents can only be compared if they agree on what
they are about, so the document's subject is worth getting right. It is ranked from
what the document itself gives away, never from a list of known companies or countries:
the masthead at the top of the first page, mentions that keep company with figures
rather than sitting in headings, mentions repeated across pages, a legal suffix, a
title of the form "Review of X", and genre words trimmed off either end. The spellings
of one name are folded together on the same key the comparison stage uses, so
"DELHIVERY", "Delhivery Limited" and "Delhivery" count as one company. A regulator's
audit of a hospital comes out filed under the hospital, which is what makes its figures
comparable with the hospital's own.

The evidence quote is exactly the sentence the value sits in. The period and the
reporting basis are read back out of that quote, so a quote cropped at the previous
number would take both from the wrong sentence, and a period on the far side of "from"
or "compared with" belongs to the figure being compared against, not to this one.

**Grounding** is a hard rule rather than a score. A fact is stored only if its quote is
a literal substring of the stored page text at the recorded character offsets; anything
else is discarded. `/api/facts/{id}` re-checks it on read rather than trusting a flag
written at extraction time, and the page text is served so the check can be done by
hand. Across the starter set, all 2,843 stored facts pass.

**Normalization** (`backend/app/pipeline/normalizer.py`) puts values on a common
footing. Indian and western magnitudes meet, so `₹8,142 Cr` and `₹81,415 Mn` are the
same number. Accounting parentheses mean negative, but only when the brackets wrap the
numeral: `(452)` is minus 452, while `6.5 per cent (of GDP)` stays positive. Fiscal
years, quarters, month ends and plain dates all reduce to canonical keys. Company names
lose their legal suffixes so "Delhivery Limited" and "Delhivery" agree.

**Comparison** (`backend/app/pipeline/comparator.py`) decides the verdict:

| Verdict | When |
| --- | --- |
| Corroborated | Same metric, compatible units, values within 1.5 per cent, no conflicting period |
| Contradicted | Same metric and same stated period, values disagree, nothing explains it |
| Explained by context | Values disagree but the periods or the reporting basis differ |
| Not comparable | Units cannot be placed on one scale, or the pairing is too weak to judge |

Two names for the same metric are matched by how much of the shorter name the longer
one contains, after expanding any acronym the document defined itself. That is what
lets "real GDP growth" in one report meet "real gross domestic product growth" in
another without either spelling being written into the code.

### Decisions worth defending

**Rules, not a model, for the verdict.** Asking a model whether two figures contradict
gives an answer that changes between runs and cannot be argued with. Every verdict here
carries the factors that produced it, so a wrong call can be traced to the specific
factor that is wrong. A model is available for extraction (`USE_LLM=true` with a Groq,
OpenAI or Gemini key) but only proposes facts, and a proposal whose quote is not
literally on the page is thrown away. The system runs fully offline by default.

**Refusing is a result.** Rupees and dollars are never compared, because there are no
exchange rates in the system and a stale rate would fabricate a verdict. A conflict is
never claimed when either figure inherited its period from the document, because
"same period" has not been established. Both refusals are recorded with their reasons
rather than passed over.

**Rejections are stored.** The extractor writes down what it saw and would not keep,
with a reason code. That is what the fourth required case is built from, and it is real
data rather than a written-up anecdote.

**Facts are paired by the words in their metric names.** Comparing every fact with
every other is quadratic and almost entirely wasted. Facts are indexed by the words in
their predicate and only facts sharing a word are ever paired, which is also what makes
a new document cheap to add: its facts are looked up against the words they use, and
nothing already stored is recomputed.

### Brownie points

- **Large PDFs.** Pages are streamed one at a time, so memory does not grow with
  document length. `MAX_PAGES_PER_DOCUMENT` caps the work per upload, and
  `?wait=false` on the upload endpoint pushes processing to the background.
- **Many PDFs.** Word-level blocking keeps comparison near-linear in practice. All six
  starter documents, 511 pages, take about 40 seconds end to end.
- **Schema that evolves.** Metric names are minted from the text, never configured.
  `/api/facts/schema` is the vocabulary as it currently stands.
- **Incremental.** A new document is compared against the facts already stored. Nothing
  is rebuilt. `/api/relationships/recompute` exists as a deliberate reset.

### AI tools used

Claude Code (Opus) for most of the implementation, working against the starter PDFs as
a live test bed rather than writing to a spec. The whole extractor was rewritten twice
after measuring what it actually produced. Design decisions, the failure analysis and
the choice of what the system should refuse to do were driven by reading the real
output at each step.

---

## 4. Limitations and next steps

Honest about what does not work.

### What is weak

**Metric names are noisier than the numbers.** The extractor reads the noun phrase
before the reporting verb, which is right in clean prose and wrong when the sentence is
convoluted. "Global economy" as a metric name is really "global economic growth", and a
few names carry a stray leading word. Values, units and pages are far more reliable
than names.

**A change is read as a level.** In "EBITDA increased by Rs. 578 Cr to Rs. 127 Cr",
both numbers are captured under the same name. The first is a movement, the second is a
level, and nothing distinguishes them. Reading the verb would fix it.

**Tables lose their headers.** PDF text extraction flattens a table into lines. Where a
value's meaning lives in a column header several rows up, the neighbouring-line rule
gets the wrong caption or none, which is why "no metric label in context" is one of the
most common rejections. Positional extraction would recover these.

**Inherited context is a guess.** A sentence with no period of its own gets the
document's period. It is usually right. When it is wrong the fact is wrong, which is why
such facts can never produce a contradiction, and why the flag is on screen.

**Scanned PDFs produce nothing.** There is no OCR. A PDF with no text layer yields no
facts, and says so rather than failing.

**No currency conversion.** A rupee figure and a dollar figure are recorded as not
comparable.

### What is next, in order

1. **Read the verb.** Distinguishing "increased by" from "stood at" would remove the
   largest class of bad comparisons for a small amount of work.
2. **Positional extraction.** Using text coordinates rather than the flattened stream
   would recover table headers and, with them, a large number of currently rejected
   candidates.
3. **Cluster metric names.** Embedding the minted names and clustering them would let
   "core inflation" and "inflation excluding food and fuel" meet, which word overlap
   deliberately refuses to do today.
4. **Show the page image.** The character offsets are already stored; rendering the PDF
   and drawing a box around the evidence would make grounding visible rather than
   checkable.
5. **A period lattice.** Knowing that a quarter sits inside a fiscal year would turn
   several "not comparable" pairs into real reconciliations.

---

## 5. Additional notes

### The four required cases

They are not written down anywhere. `backend/app/services/showcase.py` queries the
relationships that exist and picks the clearest example of each kind, so the examples
change with the documents loaded and an absent case says so rather than inventing one.
The tests cover exactly that: with nothing loaded, case one reports itself unavailable.

What the starter set produces today. These are selected at query time, so a rerun
after changing the documents will show different pairs.

- **Corroborated.** India's real GDP growth for the year is written `6.5%` by the IMF
  and `6.5 per cent` by the RBI, in two separately published documents. Different
  wording, same figure once normalized.
- **Contradicted.** Industrial sector growth is 6.2 per cent in the Economic Survey and
  4.3 per cent in the RBI annual report for the same year. The system notices that one
  of the two names the basis it reports on and the other does not, and says so, which
  is why it calls this likely rather than certain.
- **Explained by context.** Two growth figures that look like a conflict until the
  periods are compared, at which point both stand.
- **Failures.** Live counts by reason code with an example of each, the least certain
  facts that were kept, and the failure modes listed above.

A note on the second case, because it matters more than the example itself. The
comparator is willing to call a contradiction, and that means it can be wrong. An
earlier run surfaced a pair where one figure excluded certain items from its basket and
the other did not, which the sentence said and the extractor did not capture. Adding
exclusion wording as a reporting basis fixed that pair. The general lesson is that most
false conflicts come from context the extractor failed to read, not from arithmetic,
which is why every fact carries flags for what it inherited rather than read.

### API

| Endpoint | What it does |
| --- | --- |
| `POST /api/documents/upload` | Take a PDF and process it |
| `GET /api/documents/{id}/pages/{n}` | The stored page text, for checking a quote |
| `GET /api/facts` | Search facts by document, subject, metric, confidence or wording |
| `GET /api/facts/{id}` | One fact, its links, and a fresh grounding check |
| `GET /api/facts/schema` | The metric vocabulary the documents produced |
| `GET /api/relationships` | Cross-document verdicts with their reasoning |
| `GET /api/showcase/cases` | The four cases, computed live |
| `GET /api/system/stats` | Counts, and progress of the first load |

### Layout

```
backend/app/
  pipeline/     ingestion, extraction, normalization, comparison
  services/     writing to the knowledge layer, first load, the four cases
  models/       documents, page text, facts, relationships, rejections
  routers/      the HTTP surface
  tests/        85 tests, isolated database, synthetic PDFs
frontend/src/   React interface, hand-written CSS
starter-datasets/  the six PDFs read on first launch
```
