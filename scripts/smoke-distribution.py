"""Exercise frozen first setup and legacy restart in an isolated local profile."""
import json
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parents[1]
BUILD = PROJECT / 'dist/CRTV-Sim-Portable-0.9.55'
ROOT = PROJECT / '.test-tmp/Frozen distributión with spaces final'


def api(path, value=None, token=None, timeout=20):
    headers={'Content-Type':'application/json'}
    if token: headers['X-Setup-Token']=token
    data=json.dumps(value).encode() if value is not None else None
    with urlopen(Request('http://127.0.0.1:8765'+path,data,headers),timeout=timeout) as response:
        return response.read().decode()


def wait(path):
    last=None
    for _ in range(90):
        try: return api(path)
        except Exception as exc: last=exc; time.sleep(1)
    raise RuntimeError(f'{path} never became ready: {last}')


def launch():
    env={**os.environ,'RETROTV_PORTABLE_ROOT':str(ROOT)}
    return subprocess.Popen([str(ROOT/'CRTV-Sim.exe')],cwd=ROOT,env=env,creationflags=subprocess.CREATE_NO_WINDOW)


def stop(process):
    if process.poll() is not None:
        return
    settings=json.loads(api('/api/settings'))
    fields={field['path']:field['value'] for field in settings['fields']}
    configured_media=Path(fields['storage_roots.media']).resolve()
    if not configured_media.is_relative_to(ROOT):
        raise RuntimeError('Refusing shutdown: this Dashboard is not the isolated smoke profile')
    result=json.loads(api('/api/runtime/shutdown',{},timeout=90))
    assert result['shutdown']['safe_to_remove'],result
    process.wait(timeout=30)


def check_frozen_weather():
    """Render with the packaged code and tools, using an offline test forecast."""
    sys.path.insert(0, str(PROJECT / 'RetroWeather/src'))
    from retroweather.config import AppPaths, initialize
    paths = AppPaths.discover(ROOT / 'Weather test with spaces')
    initialize(paths)
    shutil.copy2(PROJECT / 'RetroWeather/tests/fixtures/normalized-weather.json', paths.latest_snapshot)
    settings = json.loads(paths.settings.read_text(encoding='utf-8'))
    settings['crtvsim_holidays'] = {'halloween': {'start': '01-01', 'end': '12-31'}}
    paths.settings.write_text(json.dumps(settings), encoding='utf-8')
    overlay = paths.media / 'graphics/holidays/halloween/overlays/test.png'
    overlay.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'RETROTV_PORTABLE_ROOT': str(ROOT),
           'PATH': str(Path(os.environ['SystemRoot']) / 'System32')}
    subprocess.run([
        str(ROOT / 'tools/ffmpeg/ffmpeg.exe'), '-hide_banner', '-loglevel', 'error',
        '-y', '-f', 'lavfi', '-i', 'color=black@0.0:s=640x480,format=rgba,'
        'drawbox=x=0:y=0:w=iw:h=ih:color=orange:t=8', '-frames:v', '1', str(overlay),
    ], env=env, check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    rendered = subprocess.run([
        str(ROOT / 'CRTV-Sim.exe'), '--retroweather', '--home', str(paths.home), 'render',
    ], cwd=ROOT, env=env, capture_output=True, timeout=300,
       creationflags=subprocess.CREATE_NO_WINDOW)
    assert rendered.returncode == 0, (rendered.returncode, rendered.stderr)
    pointer = json.loads((paths.output / 'current.json').read_text())
    version = paths.output / Path(pointer['manifest']).parent
    manifest = json.loads((version / 'channel.json').read_text(encoding='utf-8'))
    assert abs(manifest['duration_seconds'] - 120) <= 0.15, manifest
    metadata = json.loads((version / 'render-metadata.json').read_text())
    assert metadata['seasonal']['overlay'] == 'test.png', metadata
    assert (version / 'weather-channel.mp4').is_file()
    print('Frozen Weather rendered and published a complete two-minute holiday broadcast with bundled FFmpeg')


def create_test_episode(media):
    """Create disposable programming, never use the owner's media library."""
    video = media / 'Smoke TV/all-day/episodes/Smoke Series/S01E01.mp4'
    video.parent.mkdir(parents=True)
    subprocess.run([
        str(ROOT / 'tools/ffmpeg/ffmpeg.exe'), '-hide_banner', '-loglevel', 'error',
        '-y', '-f', 'lavfi', '-i', 'color=navy:s=320x240:r=1/10:d=1760',
        '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '1760',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '1',
        '-c:a', 'aac', '-b:a', '8k', '-shortest', str(video),
    ], check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    return video


def check_frozen_tv():
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = json.loads(api('/api/runtime/status'))
        if status['maintenance']['state'] == 'error':
            raise AssertionError(status['maintenance'])
        channel = next((c for c in status['channels'] if c['slug'] == 'smoke-tv'), None)
        if channel and status['maintenance']['state'] == 'idle':
            break
        time.sleep(1)
    else:
        raise AssertionError(('Synthetic channel did not become ready', status))
    guide = json.loads(api('/api/guide'))
    listing = next(c for c in guide['channels'] if c['slug'] == channel['slug'])
    assert listing['now']['asset_id'] is not None, listing
    assert status['maintenance']['guide_rendered'], status['maintenance']
    api('/api/runtime/start', {'channel': channel['slug']})
    for _ in range(90):
        status = json.loads(api('/api/runtime/status'))
        assert not status['tv_error'], status
        if (status['tv_state'] == 'playing' and status['now_playing']
                and status['now_playing']['asset_id'] is not None):
            assert status['now_playing']['entry_type'] == 'episode', status['now_playing']
            break
        time.sleep(1)
    else:
        raise AssertionError(('Scheduled playback never started', status))
    api('/api/runtime/tune', {'channel': '__guide__'})
    for _ in range(30):
        status = json.loads(api('/api/runtime/status'))
        assert not status['tv_error'], status
        if status['now_playing'] and status['now_playing']['entry_type'] == 'guide':
            break
        time.sleep(1)
    else:
        raise AssertionError(('Rendered Guide playback never started', status))
    api('/api/runtime/stop', {})
    print('Frozen channel scan, schedule creation, Guide listings/rendering, episode playback and Guide tuning passed')


def check_frozen_workshop():
    guide = json.loads(api('/api/guide'))
    channel = next(c for c in guide['channels'] if c['slug'] == 'smoke-tv')
    route = f"/api/assets/{channel['now']['asset_id']}/act-break-workshop"
    started = time.monotonic()
    state = json.loads(api(route, {'action': 'preview', 'offset_ms': 600000}))
    session = state['session_id']
    try:
        while state['state'] in {'queued', 'running'} and time.monotonic() - started < 30:
            time.sleep(.1)
            state = json.loads(api(route + '?session_id=' + session))
        assert state['state'] == 'ready', state
        assert state['result']['clip_token'] is None, state
        assert state['result']['offset_ms'] == 600000, state
        with urlopen('http://127.0.0.1:8765' + state['result']['frame_url'], timeout=10) as response:
            assert response.headers['Content-Type'].startswith('image/jpeg')
            assert response.read().startswith(b'\xff\xd8')
        print(f'Frozen workshop scrub returned an exact frame without video encoding ({time.monotonic()-started:.2f}s)')
    finally:
        api(route, {'action': 'close', 'session_id': session})


def main():
    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(('127.0.0.1',8765))==0:
            raise RuntimeError('Port 8765 is occupied; refusing to interact with an existing Dashboard')
    if ROOT.exists():
        raise RuntimeError('Remove the previous isolated smoke profile deliberately before rerunning')
    ROOT.mkdir(parents=True)
    for name in ('portable-defaults','assets','migrations','tools','_internal'):
        shutil.copytree(BUILD/name,ROOT/name)
    shutil.copy2(BUILD/'CRTV-Sim.exe',ROOT/'CRTV-Sim.exe')
    shutil.copy2(BUILD/'pyproject.toml',ROOT/'pyproject.toml')
    if (BUILD/'MICROSOFT-RUNTIME.json').is_file():
        shutil.copy2(BUILD/'MICROSOFT-RUNTIME.json',ROOT/'MICROSOFT-RUNTIME.json')
        shutil.copytree(BUILD/'licenses/microsoft',ROOT/'licenses/microsoft')
    check_frozen_weather()
    # Keep the test short and avoid unnecessary act-break work on an empty library.
    base=ROOT/'portable-defaults/user-data/config/base.example.toml'
    # copytree hardlinked defaults: replace the inode before changing test values.
    text=base.read_text(encoding='utf-8').replace('schedule_horizon_days = 7','schedule_horizon_days = 1').replace('analyze_breakpoints = true','analyze_breakpoints = false')
    text=text.replace('[player]', '[player]\nextra_args = ["--no-config", "--vo=null", "--ao=null", "--force-window=no"]')
    text=text.replace('foreground_on_start = true', 'foreground_on_start = false')
    base.unlink(); base.write_text(text,encoding='utf-8')
    media=ROOT/'Empty média'; media.mkdir()
    process=launch()
    try:
        html=wait('/setup')
        assert 'Welcome to CRTV Sim' in html
        token=re.search("const token='([^']+)'",html).group(1)
        assert not (ROOT/'user-data/database/retrotv.sqlite3').exists()
        values={'media':str(media),'timezone':'Europe/London','display':'modern',
                'resolution':'1080p','aspect':'preserve','policy':'proxy',
                'video':False,'audio':False,'tails':False,'weather':False,'runtime_terms':True}
        api('/api/setup/preview',values,token)
        assert not (ROOT/'user-data/database/retrotv.sqlite3').exists()
        video = create_test_episode(media)
        source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
        api('/api/setup/complete',values,token)
        status=json.loads(wait('/api/runtime/status'))
        settings=json.loads(api('/api/settings'))
        fields={field['path']:field['value'] for field in settings['fields']}
        assert fields['normalization.enabled'] is False
        assert fields['audio_normalization.enabled'] is False
        assert fields['normalization.canvas_width']==1920
        print('Frozen setup committed before scanning; Dashboard/settings available')
        check_frozen_tv()
        workshop_script = api('/static/act-break-workshop.js')
        assert 'Place Act Break' in workshop_script
        check_frozen_workshop()
        inventory = json.loads(api('/api/inventory'))
        assert inventory['cache']['ready'], inventory
        reviews = json.loads(api('/api/inventory/reviews'))
        assert isinstance(reviews, dict), reviews
        print('Frozen workshop controls, inventory cache and review endpoint available')
        assert hashlib.sha256(video.read_bytes()).hexdigest() == source_hash
        stop(process)
        overlay=ROOT/'user-data/config/portable.windows.settings.json'
        payload=json.loads(overlay.read_text())
        payload['normalization']['canvas_width']=1234
        payload['normalization']['canvas_height']=700
        overlay.write_text(json.dumps(payload),encoding='utf-8')
        (ROOT/'user-data/state/setup-completed.json').unlink()
        # Simulate an existing profile whose external media drive is unavailable.
        media.rename(ROOT/'Disconnected média')
        process=launch()
        wait('/api/runtime/status')
        settings=json.loads(api('/api/settings'))
        fields={field['path']:field['value'] for field in settings['fields']}
        assert fields['normalization.canvas_width']==1234
        assert fields['normalization.canvas_height']==700
        assert fields['storage_roots.media']==str(media)
        stop(process)
        print('Legacy restart without marker, unavailable library and custom dimensions preserved; Safe Stop passed')
    finally:
        if process.poll() is None:
            try: stop(process)
            except Exception: process.terminate(); process.wait(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',type=Path,default=BUILD)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--archive',type=Path,help='Extract and test the actual Windows ZIP')
    args=parser.parse_args()
    BUILD=args.build.resolve()
    ROOT=args.root.resolve()
    if not ROOT.is_relative_to((PROJECT/'.test-tmp').resolve()):
        raise ValueError('Smoke profiles must remain inside project .test-tmp')
    if args.archive:
        extracted=ROOT.parent/(ROOT.name+' archive extraction')
        if extracted.exists():
            raise ValueError('Archive extraction destination already exists')
        with zipfile.ZipFile(args.archive) as candidate:
            for name in candidate.namelist():
                path=Path(name)
                if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
                    raise ValueError('Unsafe archive member')
            candidate.extractall(extracted)
        builds=list(extracted.glob('*/CRTV-Sim.exe'))
        if len(builds)!=1:
            raise ValueError('Archive must contain exactly one portable application')
        BUILD=builds[0].parent
    main()
