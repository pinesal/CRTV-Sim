"""Exercise frozen first setup and legacy restart in an isolated local profile."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parents[1]
BUILD = PROJECT / 'dist/CRTV-Sim-Portable-0.9.48'
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
    return subprocess.Popen([str(BUILD/'CRTV-Sim.exe')],cwd=ROOT,env=env,creationflags=subprocess.CREATE_NO_WINDOW)


def stop(process):
    result=json.loads(api('/api/runtime/shutdown',{},timeout=90))
    assert result['shutdown']['safe_to_remove'],result
    process.wait(timeout=30)


def main():
    if ROOT.exists():
        raise RuntimeError('Remove the previous isolated smoke profile deliberately before rerunning')
    ROOT.mkdir(parents=True)
    for name in ('portable-defaults','assets','migrations','tools'):
        shutil.copytree(BUILD/name,ROOT/name,copy_function=os.link)
    shutil.copy2(BUILD/'pyproject.toml',ROOT/'pyproject.toml')
    # Keep the test short and avoid unnecessary act-break work on an empty library.
    base=ROOT/'portable-defaults/user-data/config/base.example.toml'
    # copytree hardlinked defaults: replace the inode before changing test values.
    text=base.read_text(encoding='utf-8').replace('schedule_horizon_days = 7','schedule_horizon_days = 1').replace('analyze_breakpoints = true','analyze_breakpoints = false')
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
                'video':False,'audio':False,'tails':False,'weather':False}
        api('/api/setup/preview',values,token)
        assert not (ROOT/'user-data/database/retrotv.sqlite3').exists()
        api('/api/setup/complete',values,token)
        status=json.loads(wait('/api/runtime/status'))
        settings=json.loads(api('/api/settings'))
        fields={field['path']:field['value'] for field in settings['fields']}
        assert fields['normalization.enabled'] is False
        assert fields['audio_normalization.enabled'] is False
        assert fields['normalization.canvas_width']==1920
        print('Frozen setup committed before scanning; Dashboard/settings available; version 0.9.48')
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


if __name__=='__main__': main()
