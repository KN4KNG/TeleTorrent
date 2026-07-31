def human(n):
    n=float(n or 0)
    for u in ['B','KB','MB','GB','TB']:
        if n<1024: return f'{n:.1f} {u}'
        n/=1024
    return f'{n:.1f} PB'
def bar(p):
    p=max(0,min(1,float(p or 0))); n=round(p*12)
    return '█'*n+'░'*(12-n)
def state_icon(s):
    s=str(s).lower()
    if 'down' in s: return '⬇️'
    if 'up' in s or 'seed' in s: return '⬆️'
    if 'pause' in s: return '⏸️'
    if 'error' in s: return '⚠️'
    return '•'
