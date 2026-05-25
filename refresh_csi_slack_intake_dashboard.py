#!/usr/bin/env python3
"""
Cloud Success Insights — Slack Intake Dashboard Refresher
==========================================================
Pulls live data from your Slack List and rewrites CSI_Intake_Dashboard.html.

SETUP (one-time):
  1. Open your Slack list in a browser (salesforce.enterprise.slack.com)
  2. Open DevTools → Network tab → filter by "lists.records"
  3. Scroll the list to trigger a request
  4. Right-click any "lists.records.list" request → Copy → Copy as cURL
  5. From the cURL output:
       a. Copy the FULL "cookie:" header value → SLACK_COOKIE
       b. Find the "token" field in --data-raw → the xoxc-... value → SLACK_TOKEN
       c. From the URL, find "_x_csid=..." value → SLACK_CSID (optional)
  6. Set environment variables and run:

       export SLACK_COOKIE='d=xoxd-...; b=...; ...'   # full cookie string
       export SLACK_TOKEN='xoxc-...'                   # token from form data
       export SLACK_CSID='e7RNrPkDl-0'                 # optional, from URL
       python3 refresh_dashboard.py

The script will:
  - Fetch all list rows via lists.records.list (paginated)
  - Decode opaque column keys and option IDs using the embedded schema
  - Compute TTR, Age, Buckets, Fiscal Year/Quarter, Is Overdue, Item ID
  - Build a Slack permalink for each row
  - Rewrite WorkItems_Dashboard_Ready.csv
  - Inject fresh JSON into CSI_Intake_Dashboard.html
"""

import os, sys, json, csv, re, math, time, datetime, uuid, urllib.request, urllib.parse

# ── CONFIG ────────────────────────────────────────────────────────────────────

# Full cookie string from DevTools Request Headers (the entire "cookie:" value)
SLACK_COOKIE = os.environ.get('SLACK_COOKIE', 'YOUR-FULL-COOKIE-STRING-HERE')

# xoxc token — from the "token" field in the multipart POST body (Copy as cURL)
SLACK_TOKEN  = os.environ.get('SLACK_TOKEN', 'xoxc-YOUR-TOKEN-HERE')

# Optional CSRF token from the URL query param _x_csid (leave blank to omit)
SLACK_CSID   = os.environ.get('SLACK_CSID', '')

# GitHub Pages hosting — pushes csi-data.js to gh-pages branch on every refresh.
# Supports both GHE (git.soma.salesforce.com) and github.com via env vars:
#   GHE_TOKEN  — PAT with repo scope
#   GIT_HOST   — override host (default: git.soma.salesforce.com)
#   GIT_REPO   — override repo slug (default: ankur-sah/Cloud-Success-Insights)
GHE_TOKEN = os.environ.get('GHE_TOKEN', '')
GIT_HOST  = os.environ.get('GIT_HOST',  'git.soma.salesforce.com')
GIT_REPO  = os.environ.get('GIT_REPO',  'ankur-sah/Cloud-Success-Insights')
GHE_REPO  = GIT_REPO   # keep old name for compat
GHE_API   = f'https://{GIT_HOST}/api/v3' if GIT_HOST != 'github.com' else 'https://api.github.com'

SLACK_ROUTE  = 'E7T5PNK3P:E7T5PNK3P'
LIST_FILE_ID = 'F09424HQ5JL'
WORKSPACE_ID = 'T01G0063H29'
LIST_BASE_URL = f'https://salesforce.enterprise.slack.com/lists/{WORKSPACE_ID}/{LIST_FILE_ID}'
API_BASE      = 'https://salesforce.enterprise.slack.com/api'

CSV_OUT = '/tmp/WorkItems_Dashboard_Ready.csv'

# ── HARDCODED SCHEMA (from files.info list_metadata.schema) ──────────────────
# Column key → human-readable column name
KEY_TO_NAME = {
    'name':           'Request Date',
    'ColNX9QIKZU':   'Request Source',
    'ColYK0S1TPR':   'Project Name / Subject',
    'ColL41VH5CL':   'Status',
    'Col19EO0JD1':   'Assignee',
    'ColLSEP53CQ':   'Description',
    'ColKKN2O98J':   'Team to route to',
    'ColGWCRCZSN':   'Subject Area',
    'Col2M7LQW1N':   'Requestor',
    'ColGF64ASAE':   'Planned ETA',
    'ColKWIB4FST':   'Priority (Stack Rank)',
    'Col86B6R0M4':   'Request Type',
    'ColTZZMX324':   'Size',
    'ColPQOA276M':   'Agentforce Request',
    'ColHPRKPYLZ':   'Requestor Business Unit',
    'ColEQO820QC':   "Jim's V2MOM Method",
    'Col09373H53NX': 'Expected Due Date',
    'Col093F3Q3W04': 'Priority',
    'Col09QJ6AMX2B': 'Message',
}

# Column key → {option ID → label}
KEY_TO_OPTS = {
    'ColNX9QIKZU': {
        'OptSXIW75GF': 'Slack Help Channel',
        'OptZM1Z6Y23': 'Manual Task Creation',
        'Opt1LE9NXM6': '',
    },
    'ColL41VH5CL': {
        'OptLX6KUPLG': 'In Progress',
        'OptBOAV0U10': 'Backlog',
        'OptD88BY1EB': 'Completed',
        'OptP7IJ5T1X': 'On Hold',
        'OptBYP1DV3X': 'Cancelled',
    },
    'Col19EO0JD1': {
        'OptLA8R4FDJ': '',
        'OptRUWCTAAY': 'Saumya Shaklya',
        'OptI66QGH5E': 'K Sai Karthik',
        'OptHGB4JXWN': 'Karan Bhatt',
        'Opt3TCVZKPL': 'Rajender Kuchan',
        'OptSAZGV05B': 'Aanchal Gupta',
        'OptYWUZSPVG': 'James Pae',
        'OptIK834C1O': 'Raman Makkar',
        'Opt86SLDYGW': 'Hitesh Yadav',
        'OptCO0XQZVF': 'Ankur Sah',
        'OptTP2N5RUG': 'Ankit Mani',
        'OptM6E3W830': 'Anjali Krishna Kasula',
        'OptQMDCT9LR': 'Shravan Kumar Das',
        'Opt52A3HWCJ': 'Mallikarjuna Reddy Challa',
        'OptK7S81PEU': 'Soumya Kundra',
        'OptT4KZMJEV': 'Sahanaa Pujar',
        'OptJUWXZHG8': 'Chandrakanth Mathsa',
        'OptRE83O78V': 'Ayan Saha',
        'OptPFRWUZRK': 'Satya Srikanth Kambhampati',
        'OptQBJW7D9E': 'Abbas Ali',
        'OptD7WEYWIQ': 'Daichi Yamazaki',
        'Opt0WOTGWT8': 'Rasik Bhirud',
        'OptAATQFQ62': 'Divya Madhadi',
    },
    'ColKKN2O98J': {
        'OptBGRJGFV7': 'Data Intelligence - Kamal Agarwal',
        'OptR6CBOQ2R': 'Digital Success, Training & Certifications - Ankur Sah',
        'OptBTLEONXK': 'Cloud Insights - Navneesh Kukkar',
    },
    'ColGWCRCZSN': {
        'OptYT31NLTO': 'Digital Channel Management',
        'OptREKWOZT3': 'Salesforce Help - Agentforce',
        'OptJC6JFM20': 'Salesforce Help - Content',
        'OptL3RI89VM': 'Training & Certification',
        'OptX8CYJ3QQ': 'Digital Success - UXCD',
        'Opt0SKDTNWO': 'Digital Success - Email',
        'OptICNJOW5W': 'Digital Success - Youtube',
        'OptSCYRROR3': 'Data Intelligence - Customer Support',
        'OptB6TRL6KJ': 'Data Intelligence - Guides, Architects, ILT',
        'Opt6QTLTTGY': 'Data Intelligence - Workforce Management',
        'OptK48U1XP6': 'Cloud Insights - Impact analysis',
        'Opt97Z2MM75': 'Cloud Insights - Deep dive analysis',
        'Opt7OZZIW5Z': '',
    },
    'ColKWIB4FST': {
        'OptAQIXVE58': '1', 'OptK9H4Q13O': '2', 'OptSECGGUHF': '3',
        'OptSQLTZ9HB': '4', 'Opt4MIJQ14J': '5', 'OptCP3K0K3Q': '',
    },
    'Col86B6R0M4': {
        'OptG1JC9JZM': 'Access Request (Request to grant access to dashboards)',
        'Opt44L0FMXP': 'New Asset Request (dashboard/ dataset)',
        'OptID3JZ8BU': 'Asset Enhancement (Enhancement to existing dashboard / dataset / process)',
        'OptJJX7TYBG': 'Bug or Reporting Issue',
        'OptQENAJT9H': 'I have a Question',
        'OptP5UJZ5XL': 'Raw Data Request',
        'Opt2UV0VITO': 'Deep Dive',
        'OptNF2IR8RD': '',
    },
    'ColTZZMX324': {
        'Opt591N3HZD': '', 'Opt6KDD7YSI': 'Medium',
        'OptM8DSBW49': 'Small', 'OptHY1RYA3I': 'Large',
    },
    'ColPQOA276M': {
        'Opt216XJ2VE': 'No', 'OptLELHFOQ2': 'Yes', 'OptQ7LR10J0': '',
    },
    'ColHPRKPYLZ': {
        'OptJZEX2L5G': '', 'OptCAQ0ABJG': 'Digital Success', 'OptS08YW88I': 'Cloud Success',
    },
    'ColEQO820QC': {
        'Opt8NLN33HD': 'M1: Agentforce Customer 0',
        'OptEYIN14T9': 'M2: Agentforce Adoption',
        'OptGB2FZDEH': 'M3: <$3.1B Attrition',
        'Opt7V8Y3JBG': 'M4: CRM Adoption',
        'OptMI2X6EQQ': 'M5: Year of Career',
        'Opt7ZMG2TCS': 'M6: Operational Excellence',
        'Opt8B8KV5GY': 'Others',
        'OptY6BMLQYI': '',
    },
    'Col093F3Q3W04': {
        'Opt632183AF': 'Customer Success Exec Team',
        'OptB08V6XR6': 'Digital Success Leadership Team',
        'OptI42OAMC0': 'Cloud Success Leadership Team',
        'OptMLZD24SK': 'Urgent',
        'OptWV4UY823': 'Standard',
        'OptHVUGBQTT': 'Low',
    },
}

# ─────────────────────────────────────────────────────────────────────────────


# ── SLACK API (multipart/form-data, same auth as browser) ────────────────────

def _make_multipart(fields):
    boundary = '----WebKitFormBoundary' + uuid.uuid4().hex[:16]
    body_parts = []
    for name, value in fields.items():
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        )
    body_parts.append(f'--{boundary}--\r\n')
    body = ''.join(body_parts).encode('utf-8')
    content_type = f'multipart/form-data; boundary={boundary}'
    return body, content_type


def slack_post(endpoint, payload):
    """POST to the Slack enterprise API using multipart/form-data + cookie auth."""
    params = {'slack_route': SLACK_ROUTE}
    if SLACK_CSID:
        params['_x_csid'] = SLACK_CSID
    url = f'{API_BASE}/{endpoint}?' + urllib.parse.urlencode(params)

    # xoxc token goes in the form body alongside the payload
    fields = {'token': SLACK_TOKEN}
    fields.update({k: str(v) for k, v in payload.items()})

    body, content_type = _make_multipart(fields)
    headers = {
        'Content-Type': content_type,
        'Cookie':       SLACK_COOKIE,
        'User-Agent':   'Mozilla/5.0',
        'Accept':       'application/json',
    }
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code} on {endpoint}: {e.read().decode()[:400]}')

    if not data.get('ok'):
        raise RuntimeError(f'Slack API error [{endpoint}]: {data.get("error")} — {data}')
    return data


def verify_auth():
    data = slack_post('auth.test', {})
    return data.get('user', '?'), data.get('team', '?')


# ── FETCH ROWS ────────────────────────────────────────────────────────────────

def _decode_field(key, field):
    """Extract a human-readable string value from a single Slack record field."""
    # Rich-text / display_text fields provide a pre-rendered 'text' key
    if 'text' in field and field['text'] is not None:
        return str(field['text']).strip()

    # Date fields
    if 'date' in field and field['date']:
        d = field['date']
        return d[0] if isinstance(d, list) else str(d)

    raw = field.get('value', '')
    if not raw:
        return ''

    # Multi-select: list of option IDs
    if isinstance(raw, list):
        opts = KEY_TO_OPTS.get(key, {})
        return ', '.join(opts.get(v, v) for v in raw if v)

    # Single select: option ID string
    if isinstance(raw, str) and raw.startswith('Opt'):
        opts = KEY_TO_OPTS.get(key, {})
        return opts.get(raw, raw)

    return str(raw).strip()


def fetch_all_rows():
    """Paginate through lists.records.list and return decoded rows."""
    rows   = []
    cursor = None
    page   = 0

    print('  Fetching rows', end='', flush=True)
    while True:
        payload = {'list_id': LIST_FILE_ID, 'limit': 200}
        if cursor:
            payload['cursor'] = cursor

        data    = slack_post('lists.records.list', payload)
        records = data.get('records') or data.get('rows') or data.get('items') or []
        page   += 1
        print('.', end='', flush=True)

        for rec in records:
            row = {}
            record_id = rec.get('id') or rec.get('record_id') or ''

            for field in rec.get('fields', []):
                key      = field.get('key', '')
                col_name = KEY_TO_NAME.get(key, key)
                row[col_name] = _decode_field(key, field)

            row['slack_url'] = f'{LIST_BASE_URL}?record_id={record_id}' if record_id else ''
            rows.append(row)

        meta   = data.get('response_metadata') or {}
        cursor = meta.get('next_cursor') or data.get('next_cursor') or ''
        if not cursor:
            break
        time.sleep(0.2)

    print(f' done — {len(rows)} rows.')
    return rows


# ── CSV FALLBACK ──────────────────────────────────────────────────────────────

def load_from_csv(path):
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            row.setdefault('slack_url', '')
            rows.append(row)
    print(f'  Loaded {len(rows)} rows from CSV.')
    return rows


# ── DATE / DERIVED COLUMNS ────────────────────────────────────────────────────

DATE_FMTS = [
    '%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d',
    '%Y - %m - %d', '%Y - %m-%d', '%Y -%m-%d',
    '%Y-%m-%dU',  # trailing U artifact
]

def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    # Reject strings that are clearly not dates (long text, Slack mentions)
    if len(s) > 20 or s.startswith('<@') or s.startswith('Hello') or s.startswith('Hi '):
        return None
    # Normalise single-digit day/month: 2025-8-4 → 2025-08-04
    s = re.sub(r'^(\d{4})-(\d{1})-(\d{1,2})$', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}", s)
    s = re.sub(r'^(\d{4})-(\d{2})-(\d{1})$', lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3).zfill(2)}", s)
    # Remove trailing non-date chars
    s = re.sub(r'[A-Z]+$', '', s).strip()
    for fmt in DATE_FMTS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


OPEN_STATUSES   = {'Backlog', 'In Progress', 'On Hold'}
CLOSED_STATUSES = {'Completed', 'Cancelled'}


def compute_derived(row):
    today    = datetime.date.today()
    req_date = _parse_date(row.get('Request Date'))
    eta_date = _parse_date(row.get('Expected Due Date'))
    status   = (row.get('Status') or '').strip()

    # Normalise Request Date to YYYY-MM-DD; blank out future dates (data entry errors)
    if req_date and req_date > today:
        req_date = None
    row['Request Date'] = req_date.strftime('%Y-%m-%d') if req_date else ''

    # Item ID — numeric prefix before ';' in Description
    desc = (row.get('Description') or '').strip()
    m_id = re.match(r'^(\d+)\s*;', desc)
    row['Item ID'] = m_id.group(1) if m_id else ''

    # TTR
    ttr = ''
    if status == 'Completed' and req_date and eta_date:
        d = (eta_date - req_date).days
        ttr = str(d) if d >= 0 else ''

    # Age
    age = ''
    if status in OPEN_STATUSES and req_date:
        age = str((today - req_date).days)

    def ttr_bucket(t):
        t = int(t)
        return ('0-1 days'  if t <= 1  else '2-3 days'  if t <= 3  else
                '4-7 days'  if t <= 7  else '8-14 days' if t <= 14 else '15+ days')

    def age_bucket(a):
        a = int(a)
        return ('0-7 days'   if a <= 7  else '8-14 days'  if a <= 14 else
                '15-30 days' if a <= 30 else '31-60 days'  if a <= 60 else '60+ days')

    # Week / Month / Quarter / Fiscal
    week = month = quarter = fy = fq = ''
    if req_date:
        monday  = req_date - datetime.timedelta(days=req_date.weekday())
        week    = monday.strftime('%Y-%m-%d')
        month   = req_date.strftime('%Y-%m')
        quarter = f"{req_date.year}-Q{math.ceil(req_date.month/3)}"
        m = req_date.month
        # Salesforce FY: Feb–Jan. FY starts Feb 1.
        fiscal_year = req_date.year if m == 1 else req_date.year + 1
        fiscal_q    = (1 if 2<=m<=4 else 2 if 5<=m<=7 else 3 if 8<=m<=10 else 4)
        fy = f'FY{fiscal_year}'
        fq = f'FY{fiscal_year}-Q{fiscal_q}'

    # Overdue
    if status in CLOSED_STATUSES:
        overdue = ''
    elif not eta_date:
        overdue = 'No ETA' if status in OPEN_STATUSES else ''
    elif status in OPEN_STATUSES:
        overdue = 'Overdue' if eta_date < today else 'On Track'
    else:
        overdue = ''

    row.update({
        'TTR (Days)':         ttr,
        'Age (Days Open)':    age,
        'TTR Bucket':         ttr_bucket(ttr) if ttr else '',
        'Age Bucket':         age_bucket(age) if age else '',
        'Week of Request':    week,
        'Month of Request':   month,
        'Quarter of Request': quarter,
        'Fiscal Year':        fy,
        'Fiscal Quarter':     fq,
        'Is Overdue':         overdue,
    })
    return row


# ── HTML INJECTION ────────────────────────────────────────────────────────────

JS_KEY_MAP = {
    'Request Date':             'date',
    'Project Name / Subject':   'subject',
    'Status':                   'status',
    'Assignee':                 'assignee',
    'Team to route to':         'team',
    'Subject Area':             'area',
    'Requestor':                'requestor',
    'Request Type':             'type',
    'Priority':                 'priority',
    'Size':                     'size',
    'Requestor Business Unit':  'bu',
    "Jim's V2MOM Method":       'v2mom',
    'Expected Due Date':        'eta',
    'Request Source':           'source',
    'TTR (Days)':               'ttr',
    'Age (Days Open)':          'age',
    'TTR Bucket':               'ttr_bucket',
    'Age Bucket':               'age_bucket',
    'Week of Request':          'week',
    'Month of Request':         'month',
    'Quarter of Request':       'quarter',
    'Fiscal Year':              'fy',
    'Fiscal Quarter':           'fq',
    'Is Overdue':               'overdue',
    'slack_url':                'url',
    'Item ID':                  'item_id',
}


def rows_to_js(rows):
    out = []
    for r in rows:
        obj = {}
        for full_key, js_key in JS_KEY_MAP.items():
            v = r.get(full_key, '') or ''
            if js_key == 'subject':
                v = v[:80]
            obj[js_key] = v
        out.append(obj)
    return out


def inject_and_push(js_objects):
    """
    Clone gh-pages, write fresh data to csi-data.js, commit and push via git.
    The HTML template is static (58KB); only csi-data.js changes each refresh.
    """
    import subprocess, shutil
    if not GHE_TOKEN:
        print('  ✗ GHE_TOKEN not set — skipping GitHub Pages push.')
        return

    repo_url = f'https://{GHE_TOKEN}@{GIT_HOST}/{GIT_REPO}.git'
    work_dir = '/tmp/csi-gh-pages'
    data_file = os.path.join(work_dir, 'csi-data.js')

    def run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:300]
            raise RuntimeError(f'git error: {msg}')
        return r.stdout.strip()

    # Always re-clone so token credentials are always fresh
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    print('  Cloning gh-pages...', end='', flush=True)
    run(['git', 'clone', repo_url, work_dir, '--branch', 'gh-pages', '--depth', '1'])
    print(' done.')

    # Write only the data file — escape backticks so they don't break JS template literals
    data_js = 'const RAW = ' + json.dumps(js_objects, separators=(',', ':')).replace('`', '\\u0060') + ';'
    with open(data_file, 'w', encoding='utf-8') as f:
        f.write(data_js)
    print(f'  csi-data.js: {len(data_js)/1024:.0f} KB')

    print('  Committing and pushing...', end='', flush=True)
    run(['git', '-C', work_dir, 'config', 'user.email', 'ankur.sah@salesforce.com'])
    run(['git', '-C', work_dir, 'config', 'user.name',  'Ankur Sah'])
    run(['git', '-C', work_dir, 'add', 'csi-data.js'])
    # Check if there's anything to commit (git diff --cached --quiet exits 0 if no changes)
    diff = subprocess.run(['git', '-C', work_dir, 'diff', '--cached', '--quiet'], capture_output=True)
    if diff.returncode == 0:
        print(' no changes.')
    else:
        try:
            run(['git', '-C', work_dir, 'commit', '-m', f'chore: refresh dashboard {datetime.date.today()}'])
            run(['git', '-C', work_dir, 'push', 'origin', 'gh-pages'])
            print(' done.')
            print(f'  ✓ Live → https://git.soma.salesforce.com/pages/ankur-sah/Cloud-Success-Insights/csi-intake-dashboard.html')
        except RuntimeError as e:
            print(f'\n  ✗ {e}')


def save_csv(rows, path):
    keys = list(JS_KEY_MAP.keys())
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(f'  CSV saved → {path} ({len(rows)} rows)')




# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print('\n╔══════════════════════════════════════════════════════════╗')
    print('║  Cloud Success Insights — Dashboard Refresh              ║')
    print('╚══════════════════════════════════════════════════════════╝\n')

    use_api = (SLACK_TOKEN and not SLACK_TOKEN.startswith('xoxc-YOUR') and
               SLACK_COOKIE and not SLACK_COOKIE.startswith('YOUR-FULL'))

    if use_api:
        print('▶ Step 1/3  Verifying Slack session...')
        try:
            user, team = verify_auth()
            print(f'  ✓ Signed in as {user} @ {team}')

            print('\n▶ Step 2/3  Fetching all list rows...')
            raw_rows = fetch_all_rows()

        except Exception as e:
            print(f'\n  ✗ API error: {e}')
            print('  Falling back to local CSV...\n')
            use_api = False

    if not use_api:
        print('ERROR: Slack credentials required. Cannot refresh without live data.')
        sys.exit(1)

    print(f'\n▶ Step 3/3  Computing derived columns ({len(raw_rows)} rows)...')
    rows = [compute_derived(r) for r in raw_rows]

    save_csv(rows, CSV_OUT)

    print('\n▶ Step 4/4  Injecting data and pushing to GitHub Pages...')
    inject_and_push(rows_to_js(rows))

    with_url = sum(1 for r in rows if r.get('slack_url'))
    with_id  = sum(1 for r in rows if r.get('Item ID'))
    print(f'\n✅  Done!  {len(rows)} tickets · {with_url} with Slack links · {with_id} with item IDs')


if __name__ == '__main__':
    main()
