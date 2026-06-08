"""
Script 4 — concatenate rendered clips into one video (ffmpeg concat demuxer).

  python merge_clips.py --clips_dir ./clips/ --output final_30min.mp4 [--pattern "clip_*.mp4"] [--reencode]

Clips from batch_render share codec/params, so stream-copy concat is used by default (fast, lossless).
Use --reencode if clips were produced differently and copy-concat glitches.
"""
import argparse, glob, os, subprocess, sys, shutil


def ffmpeg_bin():
    return shutil.which("ffmpeg") or os.path.join(os.path.dirname(sys.executable), "ffmpeg")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips_dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--pattern", default="clip_*.mp4")
    ap.add_argument("--reencode", action="store_true", help="re-encode instead of stream-copy")
    a = ap.parse_args()

    clips = sorted(glob.glob(os.path.join(a.clips_dir, a.pattern)))
    if not clips:
        raise SystemExit(f"no clips matching {a.pattern} in {a.clips_dir}")
    print(f"[merge] {len(clips)} clips -> {a.output}")

    list_path = os.path.join(a.clips_dir, "_concat_list.txt")
    with open(list_path, "w") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")

    ff = ffmpeg_bin()
    if a.reencode:
        cmd = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", a.output]
    else:
        cmd = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", a.output]
    rc = subprocess.run(cmd).returncode
    os.remove(list_path)
    if rc != 0:
        raise SystemExit(f"ffmpeg concat failed (exit {rc}). Try --reencode if clips differ in codec/params.")

    # report duration
    probe = shutil.which("ffprobe") or os.path.join(os.path.dirname(sys.executable), "ffprobe")
    dur = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", a.output],
                         capture_output=True, text=True).stdout.strip()
    try:
        dur = f"{float(dur)/60:.1f} min"
    except ValueError:
        dur = "?"
    print(f"[merge] wrote {a.output} ({dur}, {len(clips)} clips)")


if __name__ == "__main__":
    main()
