"""CER against an independent reference; no accuracy labels from confidence."""
import argparse
import json
from pathlib import Path
import unicodedata


def normalize(text):
    # Ignore typography while preserving numeric meaning: 27.75 != 2775,
    # 70% != 70 and 4-7 != 47. Thousands-separator commas remain optional.
    text = unicodedata.normalize('NFKC', text).lower()
    return ''.join(c for i,c in enumerate(text) if not c.isspace() and (
        not unicodedata.category(c).startswith('P') or c in '%‰' or
        (c in '.-/–—' and 0 < i < len(text)-1 and
         text[i-1].isdigit() and text[i+1].isdigit())))


def distance(ref, hyp):
    previous = list(range(len(hyp)+1))
    for i, a in enumerate(ref, 1):
        current = [i]
        for j, b in enumerate(hyp, 1):
            current.append(min(current[-1]+1, previous[j]+1,
                               previous[j-1]+(a != b)))
        previous = current
    return previous[-1]


def score(windows, segments):
    ordered = sorted(windows, key=lambda w:w['start'])
    if not ordered or any(w['end'] <= w['start'] for w in ordered):
        raise ValueError('Reference windows must have positive duration')
    if any(a['end'] > b['start'] for a,b in zip(ordered,ordered[1:])):
        raise ValueError('Reference windows overlap; do not double-count samples')
    rows = []
    for w in windows:
        ref = ''.join(s['text'] for s in w['segments'])
        # Midpoint selection applies identically to every candidate engine.
        selected = [s for s in segments if w['start'] <= (s['start']+s['end'])/2 < w['end']]
        hyp = ''.join(s['text'] for s in selected)
        rn, hn = normalize(ref), normalize(hyp)
        errors = distance(rn, hn)
        rows.append({'start': w['start'], 'end': w['end'], 'characters': len(rn),
                     'errors': errors, 'accuracy': 1-errors/max(1,len(rn)),
                     'reference': ref, 'candidate': hyp})
    n = sum(r['characters'] for r in rows)
    errors = sum(r['errors'] for r in rows)
    return {'metric': '1 - character error rate (Levenshtein / reference characters)',
            'normalization': 'NFKC, lowercase, remove whitespace and typography punctuation; retain percent signs and numeric decimals/ranges',
            'characters': n, 'errors': errors, 'accuracy': 1-errors/max(n,1),
            'seconds': sum(w['end']-w['start'] for w in windows),
            'scope': 'Only the independently checked reference windows, not general accuracy',
            'windows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', nargs='+', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    windows = [w for p in args.reference for w in json.loads(Path(p).read_text())['windows']]
    candidate = json.loads(Path(args.candidate).read_text())
    result = score(windows, candidate['segments'] if isinstance(candidate,dict) else candidate)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != 'windows'},ensure_ascii=False))
