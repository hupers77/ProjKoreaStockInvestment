import pandas as pd, numpy as np
b=pd.read_pickle('base.pkl')
b['per']=np.where(b.date<'20260701','상승기(2~6월)','하락기(7~9월)')
b['good']=(b.mfe10>=8)&(b.mae10>=-5)
print('good entry base rate', b.good.mean().round(3), b.groupby('per').good.mean().round(3).to_dict())
def ic(d,col,h):
    s=d.groupby('date').apply(lambda g: g[col].rank().corr(g[f'f{h}'].rank()) if g[col].notna().sum()>30 else np.nan).dropna()
    return f"{s.mean():+.3f}(t{s.mean()/s.std()*np.sqrt(len(s)):+.1f})"
for per,d in b.groupby('per'):
    print(per, d.date.nunique(),'일', 'avg f5', d.f5.mean().round(2))
    print('  ', {c: ic(d,c,5) for c in ['final','d5','S01','S03','S06','S07','S09','S17','S14']})
    print('  10d', {c: ic(d,c,10) for c in ['final','d5','S09','S17']})
# pullback analysis
b['pull']=b.S06<=2
for per,d in [('전체',b)]+list(b.groupby('per')):
    p=d[d.pull]
    print('\n',per,'눌림(S06<=2) n',len(p),'good',p.good.mean().round(3),'f5',p.f5.mean().round(2),'vs not',d[~d.pull].f5.mean().round(2))
    for col,bins in [('final',[0,50,60,65,101]),('S09',[-1,1,3,6,8,10]),('d5',[-100,-5,0,5,100]),('S17',[-1,1,4,7,10]),('S14',[-1,2,5,8])]:
        g=p.groupby(pd.cut(p[col],bins),observed=True).agg(n=('f5','size'),f5=('f5','mean'),f10=('f10','mean'),win5=('f5',lambda x:(x>0).mean()*100),good=('good','mean'),mae=('mae10','mean'))
        print('  ',col); print(g.round(2).to_string())
