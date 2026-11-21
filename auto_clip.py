#!/usr/bin/env python3
"""Headless NeonClipping: give it a link, get captioned 9:16 clips, hand each one to Activepieces.

    python auto_clip.py "https://www.youtube.com/watch?v=..." --count 3 --delivery github-release

What it does
  1. Starts the normal NeonClipping server (app.py) in the background on a free local port.
  2. Calls its own /make pipeline (the same one the web page uses) and waits for the clips.
  3. Uploads every clip somewhere with a link (see --delivery).
  4. POSTs one JSON message per clip to your Activepieces "Catch Webhook" URL.

Why the clip is not sent inside the webhook: Activepieces Cloud rejects webhook bodies over 5 MB and a 1080x1920 clip is
usually 10-40 MB. So the webhook carries a video_url, and the flow downloads it (or hands the URL to a posting step).

Secrets are read from environment variables only and are never printed:
  ACTIVEPIECES_WEBHOOK_URL   the Catch Webhook URL (or pass --webhook)
  AP_WEBHOOK_SECRET          optional; sent as header X-Webhook-Secret so the flow can check it
  GITHUB_REPOSITORY, GH_TOKEN  for --delivery github-release (set automatically in GitHub Actions)
  S3_BUCKET, S3_KEY_ID, S3_SECRET, S3_ENDPOINT, S3_REGION   for --delivery s3 (Cloudflare R2, Backblaze B2, AWS ...)
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- small helpers
def log(*a):
    print(*a, flush=True)


def slug(text, fallback='clip', limit=40):
    s = re.sub(r'[^a-zA-Z0-9]+', '-', text or '').strip('-').lower()[:limit].strip('-')
    return s or fallback


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def local_json(method, url, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{method} {url} -> {e.code}: {e.read().decode(errors="replace")[:500]}') from None


# ---------------------------------------------------------------- NeonClipping server
class Server:
    def __init__(self, data_dir):
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.data = Path(data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.logfile = self.data / 'server.log'
        env = {**os.environ, 'NEONCLIPPING_PORT': str(self.port), 'NEONCLIPPING_DATA_DIR': str(self.data)}
        self.log_fh = open(self.logfile, 'w')
        self.proc = subprocess.Popen([sys.executable, 'app.py'], cwd=ROOT, env=env, stdout=self.log_fh, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError('NeonClipping server exited early:\n' + self.tail())
            try:
                if local_json('GET', self.base + '/healthz', timeout=3).get('ok'):
                    return
            except Exception:
                time.sleep(1)
        raise RuntimeError('NeonClipping server did not start in time:\n' + self.tail())

    def tail(self, n=40):
        try:
            return '\n'.join(self.logfile.read_text(errors='replace').splitlines()[-n:])
        except OSError:
            return '(no log)'

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_fh.close()


def make_clips(server, args):
    body = {
        'url': args.url, 'count': args.count, 'min_len': args.min_len, 'max_len': args.max_len,
        'mode': args.mode, 'layout': args.layout, 'caption_style': args.caption_style,
        'moment_profile': args.profile, 'cam_auto': args.layout in ('stack', 'bottom') or args.layout.startswith('pip_'),
    }
    if args.language:
        body['language'] = args.language
    job = local_json('POST', server.base + '/make', body)['job']
    log(f'Job started: {job}')
    last, deadline = None, time.time() + args.timeout
    while time.time() < deadline:
        st = local_json('GET', f'{server.base}/make/{job}')
        stage = st.get('stage') or st.get('message')
        if stage and stage != last:
            log(f"  [{int(st.get('progress', 0) * 100):3d}%] {stage}")
            last = stage
        if st.get('status') == 'done':
            return st
        if st.get('status') == 'error':
            raise RuntimeError(st.get('error') or 'NeonClipping reported an error')
        time.sleep(3)
    raise RuntimeError(f'Timed out after {args.timeout}s')


# ---------------------------------------------------------------- delivery (get a link for the file)
def deliver_none(path, name, args):
    return None


def deliver_github_release(path, name, args):
    repo = os.environ.get('GITHUB_REPOSITORY')
    if not repo or not shutil.which('gh'):
        raise RuntimeError('github-release delivery needs the gh CLI and GITHUB_REPOSITORY (both exist inside GitHub Actions).')
    tag = args.release_tag
    if subprocess.run(['gh', 'release', 'view', tag], capture_output=True).returncode != 0:
        subprocess.run(['gh', 'release', 'create', tag, '--title', 'Auto clips', '--notes', 'Clips made by auto_clip.py'],
                       check=False, capture_output=True)
    r = subprocess.run(['gh', 'release', 'upload', tag, str(path), '--clobber'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('gh release upload failed: ' + r.stderr[-300:])
    return f'https://github.com/{repo}/releases/download/{tag}/{name}'


def deliver_s3(path, name, args):
    try:
        import boto3
    except ImportError:
        raise RuntimeError('s3 delivery needs boto3: pip install boto3') from None
    need = ['S3_BUCKET', 'S3_KEY_ID', 'S3_SECRET']
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError('Missing environment variables: ' + ', '.join(missing))
    c = boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT') or None,
                     aws_access_key_id=os.environ['S3_KEY_ID'], aws_secret_access_key=os.environ['S3_SECRET'],
                     region_name=os.environ.get('S3_REGION', 'auto'))
    key = f'clips/{name}'
    c.upload_file(str(path), os.environ['S3_BUCKET'], key, ExtraArgs={'ContentType': 'video/mp4'})
    return c.generate_presigned_url('get_object', Params={'Bucket': os.environ['S3_BUCKET'], 'Key': key}, ExpiresIn=7 * 86400)


DELIVERY = {'none': deliver_none, 'github-release': deliver_github_release, 's3': deliver_s3, 'direct': deliver_none}


# ---------------------------------------------------------------- Activepieces webhook
def webhook_headers(extra=None):
    h = {'User-Agent': 'neonclipping-auto/1.0', **(extra or {})}
    if os.environ.get('AP_WEBHOOK_SECRET'):
        h['X-Webhook-Secret'] = os.environ['AP_WEBHOOK_SECRET']
    return h


def send(url, data, headers, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f'Webhook call failed ({last})')   # never include the URL: it is a secret


def post_json(url, payload):
    return send(url, json.dumps(payload).encode(), webhook_headers({'Content-Type': 'application/json'}))


def post_multipart(url, payload, video_path):
    """For self-hosted Activepieces (default limit 25 MB). Cloud rejects bodies over 5 MB."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in payload.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v if isinstance(v, str) else json.dumps(v)}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{video_path.name}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
    parts.append(video_path.read_bytes())
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    return send(url, b''.join(parts), webhook_headers({'Content-Type': f'multipart/form-data; boundary={boundary}'}))


# ---------------------------------------------------------------- main
def parse():
    p = argparse.ArgumentParser(description='Link in, captioned vertical clips out, sent to Activepieces.')
    p.add_argument('url', help='YouTube / Twitch / Kick video link')
    p.add_argument('--count', type=int, default=3, help='how many clips (1-12)')
    p.add_argument('--min-len', type=float, default=30)
    p.add_argument('--max-len', type=float, default=60)
    p.add_argument('--mode', choices=['fast', 'deep'], default='fast')
    p.add_argument('--layout', default='blur', help='blur, fill, stack, bottom, game, pip_tl, pip_tr, pip_bl, pip_br')
    p.add_argument('--caption-style', default='gta', choices=['gta', 'bold', 'clean', 'hormozi', 'mrbeast'])
    p.add_argument('--profile', default='balanced', choices=['balanced', 'action', 'dialogue'])
    p.add_argument('--language', default='', help='auto if empty; hinglish, hi, en ...')
    p.add_argument('--delivery', default='none', choices=list(DELIVERY),
                   help='how the clip gets a link: github-release, s3, direct (multipart upload, self-hosted Activepieces), none')
    p.add_argument('--webhook', default=os.environ.get('ACTIVEPIECES_WEBHOOK_URL', ''), help='Activepieces Catch Webhook URL')
    p.add_argument('--hashtags', default='', help='appended to post_caption, e.g. "#shorts #gaming"')
    p.add_argument('--release-tag', default='auto-clips')
    p.add_argument('--out', default='out', help='folder for the finished clips + manifest.json')
    p.add_argument('--data', default='data', help='working folder for the NeonClipping server')
    p.add_argument('--timeout', type=int, default=3 * 3600)
    p.add_argument('--allow-partial', action='store_true', help='exit 0 even if some clips failed')
    a = p.parse_args()
    a.count = max(1, min(12, a.count))
    return a


def main():
    args = parse()
    if args.delivery == 'direct' and not args.webhook:
        sys.exit('--delivery direct needs a webhook URL.')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    server = Server(args.data)
    try:
        server.wait_ready()
        log('Server ready. Making clips...')
        result = make_clips(server, args)
    except Exception as e:
        log('FAILED:', e)
        log('--- server log tail ---\n' + server.tail())
        server.stop()
        sys.exit(1)
    server.stop()

    source_title = result.get('title') or ''
    clips = [c for c in result.get('clips', []) if c.get('file') and not c.get('error')]
    failed = len(result.get('clips', [])) - len(clips)
    if not clips:
        sys.exit('No clips were produced. ' + str(result.get('stage') or ''))

    stamp = time.strftime('%Y%m%d-%H%M%S')
    manifest, errors = [], failed
    for i, c in enumerate(clips, 1):
        src = server.data / f"{c['file']}.mp4"
        title = (c.get('title') or '').strip() or (f'{source_title} - clip {i}' if source_title else f'Clip {i}')
        name = f'{stamp}-{i:02d}-{slug(title)}-{c["file"][:6]}.mp4'
        dest = out / name
        shutil.copy(src, dest)
        srt_src = server.data / f"{c['file']}.srt"
        if srt_src.exists():
            shutil.copy(srt_src, dest.with_suffix('.srt'))
        item = {
            'source_url': args.url, 'source_title': source_title, 'clip_index': i, 'clip_count': len(clips),
            'title': title, 'hook': c.get('hook', ''), 'score': c.get('score'), 'reason': c.get('reason', ''),
            'start': c.get('start'), 'end': c.get('end'), 'file_name': name,
            'size_mb': round(dest.stat().st_size / 1048576, 1),
            'post_caption': (title + ('\n\n' + args.hashtags if args.hashtags else '')).strip(),
            'video_url': None,
        }
        try:
            item['video_url'] = DELIVERY[args.delivery](dest, name, args)
        except Exception as e:
            errors += 1
            item['delivery_error'] = str(e)[:300]
            log(f'  clip {i}: delivery failed: {item["delivery_error"]}')
        if args.webhook and (item['video_url'] or args.delivery == 'direct'):
            try:
                status = post_multipart(args.webhook, item, dest) if args.delivery == 'direct' else post_json(args.webhook, item)
                item['webhook_status'] = status
                log(f'  clip {i}/{len(clips)}: sent to Activepieces (HTTP {status}) - {title}')
            except Exception as e:
                errors += 1
                item['webhook_error'] = str(e)
                log(f'  clip {i}: {e}')
        elif args.webhook:
            log(f'  clip {i}: not sent (no video_url). Pick a --delivery other than none.')
        manifest.append(item)

    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    log(f'Done: {len(clips)} clip(s) in {out}, {errors} problem(s).')
    if errors and not args.allow_partial:
        sys.exit(2)


if __name__ == '__main__':
    main()
