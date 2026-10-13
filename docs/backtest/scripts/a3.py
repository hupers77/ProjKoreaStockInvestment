import pandas as pd, numpy as np
b=pd.read_pickle('base.pkl')
b['per']=np.where(b.date<'20260701','A','B')
b['good']=(b.mfe10>=8)&(b.mae10>=-5)
rules={'R0 전체':b.final>-1,'R1 눌림(S06<=2)':b.S06<=2,'R2 눌림+U 5일 -5↓':(b.S06<=2)&(b.d5<=-5),
 'R3 눌림+외국인매도(S09<=1)':(b.S06<=2)&(b.S09<=1),'R4 눌림+업종약세(S17<=1)':(b.S06<=2)&(b.S17<=1),
 'R5 눌림+U5일-5↓+(S09<=1|S17<=1)':(b.S06<=2)&(b.d5<=-5)&((b.S09<=1)|(b.S17<=1)),
 'X1 U>=70':b.final>=70,'X2 U 5일 +10↑':b.d5>=10,'X3 S19=10 & 5일 +5%↑':(b.S19>=10)&(b.r5>=5)}
rows=[]
for k,m in rules.items():
  for per in ['A','B','all']:
    d=b[m & ((b.per==per) if per!='all' else True)]
    d=d[d.f10.notna()]
    # 에피소드: 같은 종목 연속 신호(5거래일 이내 재등장)는 1건으로
    dd=d.sort_values(['ticker','date']); dts=sorted(b.date.unique()); pos={x:i for i,x in enumerate(dts)}
    pi=dd.date.map(pos); new=(dd.ticker!=dd.ticker.shift())|((pi-pi.shift())>5)
    r={'rule':k,'per':per,'n':len(d),'ep':int(new.sum()),'dates':d.date.nunique()}
    for h in (1,3,5,10): r[f'f{h}']=d[f'f{h}'].mean(); r[f'w{h}']=(d[f'f{h}']>0).mean()*100
    r['x5']=d.x5.mean(); r['mae10']=d.mae10.mean(); r['mfe10']=d.mfe10.mean(); r['good']=d.good.mean()*100
    # 에피소드 첫 신호만
    e=dd[new.values]; r['ep_f5']=e.f5.mean(); r['ep_f10']=e.f10.mean()
    rows.append(r)
T=pd.DataFrame(rows); T.to_pickle('rules_final.pkl')
pd.set_option('display.width',250)
print(T.round(2).to_string())
