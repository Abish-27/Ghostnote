import os
import subprocess
from pathlib import Path
from typing import Dict, List
from werkzeug.utils import secure_filename
from flask import Flask, request, render_template, url_for
from spleeter.separator import Separator

app = Flask(__name__)

# Limit upload size (e.g., 100 MB)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Folders
UPLOAD_DIR = Path('uploads')
OUTPUT_ROOT = Path('static/output')
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

# Allowed input file types
ALLOWED = {'.mp3', '.wav', '.flac', '.m4a', '.ogg'}

# ---------- Spleeter separator cache ----------
_separators: Dict[str, Separator] = {}

def get_separator(preset: str) -> Separator:
    """
    preset: '2stems', '4stems', or '5stems'
    """
    if preset not in _separators:
        _separators[preset] = Separator(f'spleeter:{preset}')
    return _separators[preset]

# ---------- Mixing via FFmpeg ----------
def ffmpeg_mix(inputs: List[Path], output: Path) -> str:
    """
    Mix the given input stem WAVs into output WAV using ffmpeg amix.
    Returns '' on success or stderr text on failure.
    """
    n = len(inputs)
    if n == 0:
        return "No stems to mix."

    args = ['ffmpeg', '-y']
    for p in inputs:
        args += ['-i', str(p)]

    # Simple amix. For more polish, add loudnorm after amix.
    filtergraph = ''.join(f'[{i}:a]' for i in range(n)) + f'amix=inputs={n}:normalize=0[a]'
    args += ['-filter_complex', filtergraph, '-map', '[a]', str(output)]

    try:
        subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return ''
    except subprocess.CalledProcessError as e:
        return e.stderr.decode('utf-8', 'ignore')

# ---------- Helpers ----------
def normalize_instrument_name(name: str) -> str:
    n = (name or '').strip().lower()
    if n in {'voice', 'vocal'}:
        return 'vocals'
    return n

def pick_preset_single(instrument: str, karaoke_fast: bool) -> str:
    """
    Single instrument mode.
    """
    t = normalize_instrument_name(instrument)
    if t == 'piano':
        return '5stems'           # only model with a dedicated piano stem
    if karaoke_fast and t == 'vocals':
        return '2stems'           # vocals + accompaniment fast path
    return '4stems'

def pick_preset_multi(removals: List[str]) -> str:
    """
    Multi-removal mode.
    - If ONLY 'vocals' selected -> we could consider 2stems, but we keep 4stems by default
      for consistency. (You can change to 2stems if you want speed.)
    - If 'piano' is selected -> 5stems
    - Otherwise -> 4stems
    """
    rset = {normalize_instrument_name(x) for x in removals}
    if rset == {'vocals'}:
        return '4stems'           # change to '2stems' if you want faster karaoke when ONLY vocals
    if 'piano' in rset:
        return '5stems'
    return '4stems'

def expected_stems_for(preset: str) -> Dict[str, str]:
    if preset == '2stems':
        return {'vocals': 'vocals.wav', 'accompaniment': 'accompaniment.wav'}
    if preset == '4stems':
        return {'vocals': 'vocals.wav', 'drums': 'drums.wav', 'bass': 'bass.wav', 'other': 'other.wav'}
    if preset == '5stems':
        return {'vocals': 'vocals.wav', 'drums': 'drums.wav', 'bass': 'bass.wav', 'piano': 'piano.wav', 'other': 'other.wav'}
    raise ValueError(f'Unknown preset: {preset}')

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    # Validate file
    f = request.files.get('audio_file')
    if not f or not f.filename:
        return 'No file uploaded', 400

    filename = secure_filename(f.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED:
        return f'Unsupported file type: {ext}', 400

    # Read form fields
    is_multi = request.form.get('multi') in ('1', 'true', 'on')

    # Multi-removal selection
    removals_multi = [normalize_instrument_name(x) for x in request.form.getlist('remove_multi')]
    # Single instrument selection
    instrument = normalize_instrument_name(request.form.get('instrument', 'drums'))
    action = (request.form.get('action', 'remove') or 'remove').strip().lower()  # 'remove' or 'solo'
    karaoke_fast = request.form.get('karaoke') in ('1', 'true', 'on')

    if action not in {'remove', 'solo'}:
        return 'Invalid action', 400

    # Validate logical combinations
    if is_multi:
        if not removals_multi:
            return 'Choose at least one instrument to remove in multi-removal mode.', 400
        # In multi mode we always REMOVE (solo doesn't apply cleanly)
        action = 'remove'
        karaoke_fast = False  # ignore karaoke in multi mode
        preset = pick_preset_multi(removals_multi)
    else:
        # Single mode
        preset = pick_preset_single(instrument, karaoke_fast)
        if preset == '2stems' and instrument not in {'vocals', 'accompaniment'}:
            # Should not happen due to picker logic
            return "Fast Karaoke Mode (2-stems) only supports 'vocals' or 'accompaniment'.", 400

    # Save upload
    in_path = UPLOAD_DIR / filename
    f.save(in_path)

    base = os.path.splitext(filename)[0]
    out_dir = OUTPUT_ROOT / base
    out_dir.mkdir(parents=True, exist_ok=True)

    # Expected stems & file paths
    stems_files = expected_stems_for(preset)
    stems_paths = {name: (out_dir / wavname) for name, wavname in stems_files.items()}

    # Check if stems for this preset already exist
    need_separate = any((not p.exists() or p.stat().st_size == 0) for p in stems_paths.values())

    # Separate if needed
    try:
        if need_separate:
            separator = get_separator(preset)
            separator.separate_to_file(str(in_path), str(OUTPUT_ROOT))
        # Optional: remove uploaded file to save space
        try:
            in_path.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as e:
        return f'Spleeter failed ({preset}): {e}', 500

    # Validate stems exist
    missing = [name for name, p in stems_paths.items() if not p.exists() or p.stat().st_size == 0]
    if missing:
        present = [p.name for p in out_dir.glob('*.wav')]
        return f'Unexpected output for {preset}. Missing/empty stems: {missing}. Present: {present}', 500

    # Decide what to keep
    if is_multi:
        targets = set(removals_multi)
        # If using 2-stems (only in single mode by default), there is no direct drums/bass/piano/other.
        # Our pick_preset_multi never selects 2-stems unless you change it manually above.
        keep_paths = [p for stem, p in stems_paths.items() if stem not in targets]
        label = f"no_{'-'.join(sorted(targets))}"
    else:
        target_stem = instrument
        # Map instrument to available stems if needed
        if preset == '2stems' and target_stem not in {'vocals', 'accompaniment'}:
            return f"'{target_stem}' not available in 2-stems.", 400
        if target_stem not in stems_paths:
            return f"Target stem '{target_stem}' not present in {preset}.", 400

        if action == 'remove':
            keep_paths = [p for stem, p in stems_paths.items() if stem != target_stem]
            label = f'no_{target_stem}'
        else:  # solo
            keep_paths = [stems_paths[target_stem]]
            label = f'solo_{target_stem}'

    if len(keep_paths) == 0:
        return 'Nothing to mix. Try a different selection.', 400

    # Output filename
    mix_name = f'{base}_{label}.wav'
    mix_path = out_dir / mix_name

    # Mix with FFmpeg
    err = ffmpeg_mix(keep_paths, mix_path)
    if err:
        return f'FFmpeg mix failed: {err}', 500

    # Build response (replace the old multiline HTML return)
    mix_url = url_for('static', filename=f'output/{base}/{mix_name}')
    pretty_preset = {'2stems': '2', '4stems': '4', '5stems': '5'}.get(preset, preset)

    # Templated result page
    return render_template(
        'result.html',
        mix_url=mix_url,
        preset=pretty_preset,
        is_multi=is_multi,
        removals=sorted(set(removals_multi)) if is_multi else [],
        instrument=instrument,
        action=action,
        stems_dir=f'output/{base}',
        file_name=filename
    )


if __name__ == '__main__':
    import multiprocessing as mp
    mp.freeze_support()
    app.run(debug=True, use_reloader=False)
