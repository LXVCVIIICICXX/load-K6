#!/usr/bin/env python3
"""Безопасная чистка прогонов.

  python3 cleanup.py           — показать, что считается мусором (ничего не удаляя)
  python3 cleanup.py --apply   — удалить только это

Мусором считается ТОЛЬКО папка, в которой нечего показать: нет ни report.html, ни summary.json.
Всё остальное не трогается никогда. Идущие прогоны (изменённые за последние 15 минут)
исключаются тоже — иначе можно снести результат прямо во время замера.
"""
import os,sys,json,shutil,time

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import config
ROOT=config.ROOT
BASE=config.RESULTS
apply='--apply' in sys.argv

def scan():
    junk=[];keep=[]
    for stand in sorted(os.listdir(BASE)):
        sd=os.path.join(BASE,stand)
        if not os.path.isdir(sd): continue
        for run in sorted(os.listdir(sd)):
            rd=os.path.join(sd,run)
            if not os.path.isdir(rd): continue
            has_sum=os.path.isfile(os.path.join(rd,'summary.json'))
            has_rep=os.path.isfile(os.path.join(rd,'report.html'))
            fresh=(time.time()-os.path.getmtime(rd))<15*60
            if has_sum or has_rep:
                n='?'
                if has_sum:
                    try: n=json.load(open(os.path.join(rd,'summary.json')))['agg']['total']
                    except Exception: pass
                keep.append((f"{stand}/{run}",f"{n} запросов" if n!='?' else "есть отчёт"))
            elif fresh:
                keep.append((f"{stand}/{run}","идёт прямо сейчас — не трогаем"))
            else:
                junk.append((f"{stand}/{run}","пусто: ни отчёта, ни данных",rd))
    return junk,keep

junk,keep=scan()
print(f"Состоявшихся прогонов: {len(keep)} — НЕ ТРОГАЮТСЯ")
for n,c in keep: print(f"   сохранён  {n}  ({c})")
print(f"\nНесостоявшихся: {len(junk)}")
for n,why,_ in junk: print(f"   мусор     {n}  ({why})")
if not junk: sys.exit(0)
if not apply:
    print("\nНичего не удалено. Чтобы удалить только мусор:  python3 cleanup.py --apply")
    sys.exit(0)
for n,_,path in junk:
    shutil.rmtree(path); print(f"   удалён {n}")
