#!/usr/bin/env python3
import pandas as pd, numpy as np, re, os
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

OUT='/home/nimrod_rotem/tmux-dashboard-original/charts'; os.makedirs(OUT, exist_ok=True)
df = pd.read_csv('txn.csv').rename(columns={'Unnamed: 0':'idx'})
def amt(x):
    s=str(x).replace('$','').replace(',','').replace('(','-').replace(')','').strip()
    try: return float(s)
    except: return np.nan
df['amt']=df['Amount'].map(amt); df['date']=pd.to_datetime(df['Date'],errors='coerce')
df['ym']=df['date'].dt.to_period('M')
D=(df['Description'].fillna('')+' || '+df['Full Description'].fillna('')).str.lower()

CARD_NO={'xxxx1371','xxxx2503','xxxx4045','xxxx8327','xxxx8616','xxxx9204'}
CARD_NAME={'xxxx8616':'Bluecard ...8616','xxxx2503':'GRABOMaybe ...2503','xxxx1371':'Chi ...1371',
           'xxxx4045':'CORP Travel ...4045','xxxx8327':'CORP Cash ...8327','xxxx9204':'Cash Rwds ...9204'}
INVEST_NO={'xxxx0261','xxxx1637','xxxx4265','xxxx1428'}
df['is_card']=df['Account #'].isin(CARD_NO); df['is_invest']=df['Account #'].isin(INVEST_NO)
df['is_debitcard']=(~df['is_card'])&(~df['is_invest'])&D.str.contains(r'checkcard|purchase on |purchase amzn|purchase pay|purchase hlu|purchase soothe|purchase hulu|debit card|dd \*door|dashpass')
df['is_spend']=df['is_card']|df['is_debitcard']          # real consumer purchases

def flow(row):
    s=D[row.name]; a=row['amt']
    if row['is_invest']: return 'Investment/Brokerage'
    if re.search(r'credit card bill payment|card bill payment',s): return 'Credit-card payment'
    if re.search(r'wire type|wire out|wire in|wire transfer|hib-',s): return 'Wire'
    if re.search(r'overdraft protection|online banking transfer|online transfer|agent assisted transfer|book out|zelle|venmo|transfer nimrod|electronic funds transfer|fid bkg svc|moneyline|journaled',s): return 'Transfer/P2P'
    if re.search(r'\bkeep the change\b',s): return 'Keep-the-Change'
    if re.search(r'irs |usataxpymt|franchise tax|withholding|foreign tax|non resident tax|federal tax',s): return 'Tax'
    if re.search(r'\bfee\b|service charge|margin interest|interest charged|finance charge',s): return 'Fee/Interest'
    if a>0: return 'Inflow'
    return 'Expense'
df['flow']=df.apply(flow, axis=1)

CATRULES=[
 ('Google Workspace/Ads', r'gsuite|g suite|google \*?ads|google adsx'),
 ('Software & SaaS', r'adobe|docusign|slack|wix\.com|dropbox|notion|github|zoom|starlink|openai|anthropic|twilio|cloudflare|namecheap|godaddy|linkedin|mureka|site booster|appsharp|helius|sequencing\.com|salesforce|x corp|twitter paid|grammarly|1password|iptorch|corporate filings'),
 ('Streaming & Media', r'netflix|paramount|hulu|prime video|youtubepre|youtube prem|\bspotify\b|disney|\bhbo\b|max\.com|google \*?chess|chess\.com|fitbit|take two|nintendo|playstation|xbox'),
 ('Dating/Personal apps', r'tinder|bumble|hinge|match\.com'),
 ('Education/Coaching', r'preply|italki|udemy|coursera|masterclass|\bypo\b'),
 ('Legal & Professional', r'oberheiden|attorney|legal|\bcpa\b|cpa-sale|ryan,'),
 ('Advertising', r'facebk|facebook ads|meta\*|fb ads'),
 ('Insurance', r'progressive ins|\bgeico\b|state farm|allstate|tesla insurance|\bnationwide\b|policygenius'),
 ('Health & Wellness', r'ageless|soothe|pharmacy|walgreens|\bcvs\b|dental|clinic|hospital|medical|prenuvo|removery|newsmile|smile_usa|\bymca\b|cymca|\bgym\b|fitness|top hill smoke'),
 ('Auto / EV / Gas', r'supercharg|tesla motors|\bmotors llc\b|bwnvt|chevron|shell oil|\bgas\b|\bfuel\b|fastrak|parking'),
 ('Travel & Hotels', r'the mira|hotel|marriott|hyatt|hilton|langham|hrcnewport|suiteness|airbnb|airlines|united air|delta air|air fran|iberia|gotogate|expedia|trip\.com|booking\.c|\bmtr\b|octopus|in \*ypo|resort|lounge'),
 ('Restaurants & Food', r'doordash|dd \*door|dashpass|ubereats|uber eats|grubhub|mcdonald|carls jr|jack in the|starbucks|restaurant|cafe|coffee|pizza|sushi|kitchen|grill|taqueria|chipotle'),
 ('Rideshare/Transit', r'\buber\b|lyft|taxi'),
 ('Groceries', r'costco|whole foods|trader joe|safeway|grocery|walmart|dollar general'),
 ('Shopping/Retail', r'amazon|amzn|target|best buy|lowes|home depot|\btjx\b|marshalls|getfpv|ebay|etsy|apple\.com/bill|apple store'),
 ('Vendors/Suppliers (biz)', r'shenzhen|fubang|alibaba|dhgate|freight|forwarder'),
]
def category(row):
    if not row['is_spend'] or row['amt']>=0: return None
    s=D[row.name]
    for name,pat in CATRULES:
        if re.search(pat,s): return name
    return 'Other/Uncategorized'
df['cat']=df.apply(category, axis=1)
def money(x,_=None): return f"${x:,.0f}"

R=[]; add=R.append
W='2026-01-10'; w=df[df['date']>=W]
add("="*72); add("A) LAST 6 MONTHS (2026-01-10..2026-07-11) — WHERE MONEY WENT, BY TYPE"); add("="*72)
g=w[w.amt<0].groupby('flow')['amt'].agg(['count','sum']).sort_values('sum')
for f,r in g.iterrows(): add(f"  {f:24} {int(r['count']):>4} tx   {money(r['sum']):>15}")
add(f"  {'-> TOTAL OUTFLOW':24} {int((w.amt<0).sum()):>4} tx   {money(w[w.amt<0].amt.sum()):>15}")
add(f"  (of which real merchant 'Expense' rows: {int((w.is_spend&(w.amt<0)).sum())} tx / {money(w[w.is_spend&(w.amt<0)].amt.sum())})")

add(""); add("B) CREDIT-CARD FEED STATUS — why itemized last-6-mo detail is MISSING")
for no in ['xxxx8616','xxxx2503','xxxx1371','xxxx4045','xxxx8327','xxxx9204']:
    gg=df[df['Account #']==no]
    if len(gg): add(f"   {CARD_NAME[no]:20} last synced {gg['date'].max().date()}  ({len(gg)} tx)")
ccp=df[df['flow']=='Credit-card payment']
add(f"   -> But card BILL PAYMENTS continue: {money(ccp[ccp.date>=W].amt.sum())} paid to issuers in last 6 mo = hidden spend")

add(""); add("="*72); add("C) EXPENSES BY CATEGORY — real merchant spend (cards+debit), 2024-01..2025-10"); add("="*72)
exp=df[(df['cat'].notna())&(df['date']>='2024-01-01')]
gc=exp.groupby('cat')['amt'].agg(['count','sum']).sort_values('sum')
for c,r in gc.iterrows(): add(f"  {c:26} {int(r['count']):>4} tx   {money(r['sum']):>12}")
add(f"  {'TOTAL merchant spend':26} {int(len(exp)):>4} tx   {money(exp.amt.sum()):>12}")

# ---- suspicious / review ----
add(""); add("="*72); add("D) SUSPICIOUS / NEEDS-REVIEW  (last 6 months)"); add("="*72)
big=w[(w.amt<0)&(w.flow.isin(['Wire','Transfer/P2P']))].copy()
big=big[big.amt<-15000].sort_values('amt')
add(" Large wires/transfers to confirm (counterparty + purpose):")
for _,r in big.iterrows():
    add(f"   {r['date'].date()} {money(r['amt']):>13}  {r['Account'][:20]:20} {str(r['Description'])[:52]}")
# named-person transfers any size
ppl=w[(w.amt<0)&D[w.index].str.contains(r'orchid bioscience|xiaoji|bryant')]
add(" Transfers to named parties (verify these are intended):")
for _,r in ppl.sort_values('amt').iterrows():
    add(f"   {r['date'].date()} {money(r['amt']):>13}  {str(r['Description'])[:55]}")

# duplicate same-day same-amount card charges (whole history)
dc=df[df['is_card']&(df.amt<0)]
dup=dc.groupby(['date','amt','Account #']).size(); dup=dup[dup>1]
add(f" Same-day duplicate CARD charges found (possible double-bills): {len(dup)} pairs, e.g.:")
for (dt,a,acc),n in dup.sort_values(ascending=False).head(6).items():
    ex=dc[(dc.date==dt)&(dc.amt==a)&(dc['Account #']==acc)]['Description'].iloc[0]
    add(f"   {dt.date()} x{n}  {money(a)}  {str(ex)[:40]}")

print("\n".join(R)); open(f'{OUT}/report.txt','w').write("\n".join(R))

# ---- recurring subscriptions (card, de-noised) ----
def norm(s):
    s=str(s).lower(); s=re.sub(r'[^a-z0-9 \*\.]',' ',s); s=re.sub(r'\bx+[0-9]*\b',' ',s)
    s=re.sub(r'\b[0-9]{3,}\b',' ',s); s=re.sub(r'\s+',' ',s).strip()
    for k in ['google gsuite','google tinder','google chess','google fitbit','google ads','adobe','paramount',
      'preply','tesla supercharger','tesla insurance','docusign','twilio','netflix','openai','github','notion',
      'hulu','prime video','youtubepre','linkedin','dropbox','zoom','mureka','starlink','namecheap','slack',
      'wix.com','x corp','twitter paid','sequencing.com','soothe','ageless','trip.com','site booster','helius','iptorch']:
        if k in s: return k
    return ' '.join(s.split()[:3])
NOISE=re.compile(r'refund|finance charge|late payment|interest charged|foreign transaction|credit balance|payment thank|autopay|online payment')
sub=df[df['is_card']&(df.amt<0)&(~D[df.index].str.contains(NOISE))].copy()
sub['m']=sub['Description'].map(norm)
gs=sub.groupby('m').agg(n=('amt','size'),months=('ym','nunique'),med=('amt','median'),tot=('amt','sum'),last=('date','max'))
subs=gs[(gs['months']>=3)&(gs['med']>-400)&(gs['med']<-3)].sort_values('med')
S=["","="*72,"E) RECURRING SUBSCRIPTIONS ON CARDS (>=3 months) — cancel what you don't use","="*72,
   f"{'service':24}{'~each':>9}{'#mo':>5}{'total':>9}  last seen  status"]
for m,r in subs.iterrows():
    status='likely-active' if r['last']>=pd.Timestamp('2025-05-01') else 'lapsed/older'
    S.append(f"{m[:24]:24}{r['med']:>9.2f}{int(r['months']):>5}{r['tot']:>9.0f}  {str(r['last'].date())}  {status}")
print("\n".join(S)); open(f'{OUT}/report.txt','a').write("\n".join(S))

# ================= CHARTS =================
plt.rcParams.update({'font.size':10,'axes.edgecolor':'#cccccc','axes.grid':True,'grid.color':'#eeeeee',
                     'axes.axisbelow':True,'figure.facecolor':'white','axes.facecolor':'white'})
ACC='#1f77b4'
# 1 card outflow by month per card
c1=df[df['is_card']&(df.amt<0)&(df.date>='2024-01-01')].copy()
c1['card']=c1['Account #'].map(lambda x:CARD_NAME[x])
piv=c1.pivot_table(index='ym',columns='card',values='amt',aggfunc=lambda v:-v.sum()).fillna(0)
piv.index=piv.index.astype(str)
ax=piv.plot(kind='bar',stacked=True,figsize=(13,5.5),width=0.82,
            color=['#1f77b4','#4c9be8','#2ca02c','#ff7f0e','#8c564b','#e377c2'][:piv.shape[1]])
ax.set_title('1 · Credit-card outflow by month, per card  (all feeds stop mid/late-2025 → blank after)',fontsize=12,weight='bold')
ax.set_ylabel('Spend ($)'); ax.set_xlabel(''); ax.yaxis.set_major_formatter(FuncFormatter(money)); ax.legend(fontsize=8,ncol=3)
plt.tight_layout(); plt.savefig(f'{OUT}/1_card_outflow_by_month.png',dpi=120); plt.close()
# 2 cc bill payments by month
cp=ccp[ccp.date>='2025-01-01'].groupby('ym')['amt'].sum().abs(); cp.index=cp.index.astype(str)
ax=cp.plot(kind='bar',figsize=(12,5),color=ACC,width=0.7)
ax.set_title('2 · Credit-card BILL PAYMENTS by month  (aggregate card spend — line items not in sheet)',fontsize=12,weight='bold')
ax.set_ylabel('$ paid to card issuers'); ax.set_xlabel(''); ax.yaxis.set_major_formatter(FuncFormatter(money))
for i,v in enumerate(cp.values): ax.text(i,v,f"${v/1000:.0f}k",ha='center',va='bottom',fontsize=8)
plt.tight_layout(); plt.savefig(f'{OUT}/2_cc_bill_payments_by_month.png',dpi=120); plt.close()
# 3 expenses by category
gc2=gc.sort_values('sum'); vals=[-v for v in gc2['sum']]
fig,ax=plt.subplots(figsize=(11,6)); ax.barh(list(gc2.index),vals,color=ACC)
ax.set_title('3 · Expenses by category  (real merchant spend, 2024-01..2025-10)',fontsize=12,weight='bold')
ax.xaxis.set_major_formatter(FuncFormatter(money))
for i,v in enumerate(vals): ax.text(v,i,f" ${v:,.0f}",va='center',fontsize=8)
plt.tight_layout(); plt.savefig(f'{OUT}/3_expenses_by_category.png',dpi=120); plt.close()
# 4 subscriptions
sc=subs.copy(); sc['permo']=sc['med'].abs(); sc=sc.sort_values('permo')
col=['#2ca02c' if l>=pd.Timestamp('2025-05-01') else '#bbbbbb' for l in sc['last']]
fig,ax=plt.subplots(figsize=(10,7)); ax.barh([i[:22] for i in sc.index], sc['permo'], color=col)
ax.set_title('4 · Recurring subscriptions ~monthly cost  (green=likely active, grey=older/lapsed)',fontsize=12,weight='bold')
ax.xaxis.set_major_formatter(FuncFormatter(lambda x,_:f"${x:,.0f}"))
for i,(v,last) in enumerate(zip(sc['permo'],sc['last'])): ax.text(v,i,f" ${v:,.0f}/mo · {str(last.date())[:7]}",va='center',fontsize=7.5)
plt.tight_layout(); plt.savefig(f'{OUT}/4_subscriptions.png',dpi=120); plt.close()
# 5 last-6mo outflow by type per month (money movement)
m6=w[w.amt<0].copy(); pv=m6.pivot_table(index='ym',columns='flow',values='amt',aggfunc=lambda v:-v.sum()).fillna(0)
pv.index=pv.index.astype(str)
ax=pv.plot(kind='bar',stacked=True,figsize=(12,5.5),width=0.8,colormap='tab20')
ax.set_title('5 · Last 6 months — outflow by type per month (mostly investments/transfers, not spending)',fontsize=12,weight='bold')
ax.set_ylabel('$'); ax.set_xlabel(''); ax.yaxis.set_major_formatter(FuncFormatter(money)); ax.legend(fontsize=8,ncol=3)
plt.tight_layout(); plt.savefig(f'{OUT}/5_last6mo_by_type.png',dpi=120); plt.close()
print("\n\n[charts written]", os.listdir(OUT))
