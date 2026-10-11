"""Local, timestamped burned-in subtitle extraction (RapidOCR).

This transcribes displayed captions, not uncaptioned speech. It makes no
accuracy claim without an independently checked reference.
"""
from collections import Counter
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unicodedata

BASE = Path(__file__).resolve().parent
FPS = 4


def executable(name):
    local = BASE / "runtime" / "bin" / name
    return str(local) if local.is_file() else shutil.which(name)


def normalized(text):
    return ''.join(c.lower() for c in unicodedata.normalize('NFKC', text)
                   if not c.isspace() and c != '~' and not unicodedata.category(c).startswith('P'))


def caption_line(rows):
    # Caption-shaped, centrally placed text in the bottom 23% of the video.
    # This excludes small chart labels, edge logos and most lower thirds.
    candidates = [r for r in rows
                  if .15 <= r['h'] <= .50 and .2 <= r['x'] + r['w'] / 2 <= .8
                  and .02 <= r['y'] <= .85 and len(r['text'].strip()) >= 1]
    if not candidates:
        return None
    # Subtitle rows are usually near the middle of this crop. Do not favor
    # OCR confidence over spatial position (small logos often score 1.0).
    best = max(candidates, key=lambda r: r['h'] - 2 * abs(r['y'] + r['h']/2 - .53))
    return {'text': best['text'].strip(), 'confidence': best['confidence']}


def merge_frames(frames, fps=FPS):
    groups = []
    for frame in frames:
        candidate = frame.get('candidate') if 'candidate' in frame else caption_line(frame.get('rows', []))
        if candidate is None:
            continue
        # Small or displaced diagram labels can have very high OCR confidence.
        # Require the same subtitle geometry used for the main caption line.
        if candidate.get('spatial', 1) < .15:
            continue
        t = frame['time']
        key = normalized(candidate['text'])
        similar = groups and (key == groups[-1]['key'] or
            (min(len(key),len(groups[-1]['key'])) >= 5 and
             SequenceMatcher(None,key,groups[-1]['key']).ratio() >= .80))
        if groups and t - groups[-1]['last'] <= 1.5/fps and similar:
            groups[-1]['samples'].append(candidate)
            groups[-1]['last'] = t
        else:
            groups.append({'start': t, 'last': t, 'key': key, 'samples': [candidate]})
    # Suppress a single OCR flicker between two identical captions; never
    # replace a real short subtitle just because it only lasted one frame.
    i = 1
    while i < len(groups)-1:
        a, b, c = groups[i-1:i+2]
        if (a['key'] == c['key'] and len(b['samples']) == 1
                and c['start'] - a['last'] <= 2.5/fps):
            a['samples'].extend(c['samples'])
            a['last'] = c['last']
            del groups[i:i+2]
        else:
            i += 1
    segments = []
    for g in groups:
        # Confidence-weighted temporal consensus, using only observed text.
        votes = Counter()
        for s in g['samples']:
            votes[s['text']] += s['confidence'] ** 3
        text = votes.most_common(1)[0][0]
        segments.append({'start': g['start'], 'end': g['last'] + 1/fps,
                         'text': text,
                         'confidence': sum(s['confidence'] for s in g['samples']) / len(g['samples']),
                         'samples': len(g['samples'])})
    return segments


def _has_cjk(text):
    return any('\u3400' <= c <= '\u9fff' for c in text)


def _choose(result, shape):
    if result.boxes is None or result.txts is None:
        return None
    height, width = shape[:2]
    options = []
    for box, text, confidence in zip(result.boxes, result.txts, result.scores):
        left, top = box.min(axis=0)
        right, bottom = box.max(axis=0)
        h = (bottom-top)/height
        cx, cy = (left+right)/2/width, (top+bottom)/2/height
        if not (.17 <= h <= .78 and .30 <= cx <= .70 and .28 <= cy <= .68):
            continue
        spatial = h - 2.5*abs(cy-.48) - .3*abs(cx-.5)
        options.append((spatial, {'text':text.strip(), 'confidence':float(confidence),
            'spatial':float(spatial), 'box':[float(left),float(top),float(right),float(bottom)]}))
    return max(options,key=lambda r:r[0])[1] if options else None


def needs_refinement(candidate):
    return candidate is None or candidate['confidence'] < .995 or not _has_cjk(candidate['text'])


def _recognize_frame(img, engine):
    import cv2
    import numpy as np
    normal = _choose(engine(img, use_det=True), img.shape)
    def white(threshold):
        mask=((img.min(axis=2)>threshold)&(img.max(axis=2)-img.min(axis=2)<35)).astype('uint8')*255
        isolated=cv2.cvtColor(255-mask,cv2.COLOR_GRAY2BGR)
        return _choose(engine(isolated, use_det=True),img.shape)
    bright = white(225)
    candidate = normal
    if bright and (normal is None or bright['confidence'] >= normal['confidence']-.008
            or (bright['confidence'] >= .92 and bright['spatial'] > normal['spatial']+.08)):
        candidate = bright
    if needs_refinement(candidate):
        # A tighter luminance range separates white subtitle strokes from
        # light paper backgrounds; use observed candidates, never substitutions.
        strong = [x for x in (white(245), white(250)) if x]
        for option in strong:
            if (candidate is None or option['confidence'] > candidate['confidence']
                    or (_has_cjk(option['text']) and option['confidence'] >= .99
                        and option['confidence'] >= candidate['confidence']-.015)):
                candidate = option
        if not strong and candidate:
            cjk = sum('\u3400' <= c <= '\u9fff' for c in candidate['text'])
            if cjk <= 1 and any(c.isascii() and c.isalnum() for c in candidate['text']):
                candidate = None
        if candidate and candidate['confidence'] < .98:
            # Detector padding can distort a short line when it is resized by
            # the recognizer. Re-read two inner crops, retaining stronger evidence.
            left,top,right,bottom=candidate['box']
            for ratio in (.20,.25):
                crop=img[round(top+(bottom-top)*ratio):round(bottom-(bottom-top)*ratio),
                         max(0,round(left)):min(img.shape[1],round(right))]
                if crop.size == 0: continue
                result=engine(crop, use_det=False)
                if result.txts and result.scores[0] > candidate['confidence']+.01:
                    candidate={**candidate, 'text':result.txts[0].strip(),
                               'confidence':float(result.scores[0])}
    return candidate


def make_engine():
    from rapidocr import RapidOCR
    return RapidOCR(params={'Global.use_cls':False,'Global.log_level':'error',
        'Det.limit_side_len':960,'Det.limit_type':'max',
        'EngineConfig.onnxruntime.intra_op_num_threads':1,
        'EngineConfig.onnxruntime.inter_op_num_threads':1})


def recognize_frames(images, on_status=None):
    import cv2
    def worker(k):
        engine=make_engine()
        out=[]
        for index in range(k,len(images),4):
            img=cv2.imread(str(images[index]))
            candidate=_recognize_frame(img,engine)
            out.append({'time':index/FPS,'candidate':candidate})
            if on_status and k==0 and index%200==0:
                on_status(f'读取原片字幕…{index+1}/{len(images)} 帧')
        return out
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames=[r for part in pool.map(worker,range(4)) for r in part]
    return sorted(frames,key=lambda r:r['time'])


def extract(path, on_status=None):
    ffmpeg, ffprobe = executable('ffmpeg'), executable('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('画面字幕组件未安装；使用语音识别')
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format',
                            '-of', 'json', str(path)], capture_output=True, text=True, check=True)
    metadata = json.loads(probe.stdout)
    if not any(s['codec_type'] == 'video' for s in metadata['streams']):
        raise RuntimeError('输入为纯音频；使用语音识别')
    duration = float(metadata['format']['duration'])
    with tempfile.TemporaryDirectory(prefix='vs-captions-') as directory:
        if on_status:
            on_status('提取画面字幕…')
        subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-vf',
                        f'fps={FPS},crop=iw:ih*0.23:0:ih*0.77', '-q:v', '2',
                        str(Path(directory)/'frame-%06d.jpg')],
                       capture_output=True, check=True)
        images = sorted(Path(directory).glob('frame-*.jpg'))

        if on_status:
            on_status(f'读取原片字幕…共 {len(images)} 帧，识别在本机进行')
        frames = recognize_frames(images, on_status)
        segments = merge_frames(frames)
    coverage = sum(s['end']-s['start'] for s in segments) / max(duration, .01)
    if len(segments) < 15 or coverage < .55:
        raise RuntimeError('没有检测到持续的底部字幕；使用语音识别')
    reviews = []
    for i,s in enumerate(segments):
        reasons = []
        if s['confidence'] < .98:
            reasons.append('画面文字识别存在歧义')
        if s['samples'] == 1:
            reasons.append('字幕只在一个采样画面出现，请核对原片')
        if s['end']-s['start'] > 20:
            reasons.append('文字持续时间较长，可能是画面标签')
        if reasons:
            s['review_reasons'] = reasons
            reviews.append(i+1)
    return segments, {
        'language': 'zh', 'engine': '画面字幕 · RapidOCR',
        'review_count': len(reviews),
        'quality_notes': ['本结果来自原片画面字幕，可能不含口头语或无字幕对白。',
                          '识别完成不代表已经人工校对；准确率需独立对照稿评测。'],
        'quality_report': {'method': 'rapidocr-burned-captions', 'fps': FPS,
                           'caption_coverage': round(coverage, 4),
                           'review_segments': reviews, 'accuracy': None,
                           'duration': duration},
    }
