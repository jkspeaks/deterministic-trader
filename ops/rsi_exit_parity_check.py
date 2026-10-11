# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy"]
# ///
"""RSI-exit parity check for ops/placebo_backtest.py.

The live DIP exit stack includes "RSI >= 50 -> sell". Applied to placebo peers
(which are usually NOT oversold) it truncates trades that start at RSI >= 50.
This reruns the DIP-vs-same-sector-placebo comparison with and without that
exit to see how much of the exit-stack "edge" is that asymmetry. Run from the
repo root after ops/placebo_backtest.py (needs its price cache).
"""
import sys, pickle, json, numpy as np, pandas as pd
sys.path.insert(0,'ops'); import placebo_backtest as pb; import eval_selection as es
px=pickle.loads(open('.placebo_cache/prices.pkl','rb').read())
uni=json.load(open('data/universe.json')); sector={r['symbol']:r['sector'] for r in uni['cross_check']}
syms=[s for s in sorted(sector) if s in px]
dates=sorted(set().union(*(set(px[s].index) for s in syms))); N=len(dates)
IND={}
for s in syms:
    d=px[s].reindex(dates).ffill(limit=3)
    if d['Close'].isna().mean()>.5: continue
    IND[s]=pb.indicators(d)
syms=list(IND); sec=np.array([sector[s] for s in syms])
def stack(rsi_exit):
    pb.RSI_EXIT=rsi_exit
    return np.column_stack([pb.trade_returns(IND[s]) for s in syms])
R50=stack(50.0); Rno=stack(1e9)
rsi=np.column_stack([IND[s]['rsi'] for s in syms]); c=np.column_stack([IND[s]['c'] for s in syms])
sma200=np.column_stack([IND[s]['sma200'] for s in syms]); sma20=np.column_stack([IND[s]['sma20'] for s in syms])
with np.errstate(invalid='ignore'): sig=(rsi<=30)&(c>=.97*sma200)&(c<sma20)
rng=np.random.default_rng(1); rows=[]
for j in range(len(syms)):
    last=-99
    for i in np.flatnonzero(sig[:,j]):
        if i-last<=8 or not np.isfinite(R50[i,j]): continue
        last=i
        peers=np.flatnonzero((sec==sec[j])&~sig[i]&np.isfinite(R50[i])); peers=peers[peers!=j]
        if len(peers)<8: continue
        pk=rng.choice(peers,200)
        rows.append((dates[i],R50[i,j],np.nanmean(R50[i,pk]),Rno[i,j],np.nanmean(Rno[i,pk]),(rsi[i,pk]>=50).mean()))
e=pd.DataFrame(rows,columns='d s50 p50 sno pno pct50'.split())
print('share of placebo peers already RSI>=50 at entry:',e.pct50.mean())
for a,b,n in (('s50','p50','with RSI>=50 exit'),('sno','pno','no RSI exit')):
    x=e[a]-e[b]
    # eval_selection.clustered_bootstrap: the one implementation (S0).
    st=es.clustered_bootstrap(x.tolist(),[str(d) for d in e.d],boot=5000,seed=2)
    print(n, f'{x.mean():+.3%}', f"[{st['lo']:+.3%}, {st['hi']:+.3%}]", 'DIP mean',f'{e[a].mean():+.2%}','placebo',f'{e[b].mean():+.2%}')
