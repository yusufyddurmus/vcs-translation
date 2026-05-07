#!/usr/bin/env python3
"""
HyperTranslator CLI
───────────────────
Translate a GTA-style localization file (UTF-16-LE, [KEY] / text pairs)
through multiple random-language chains and save translation candidates to JSON.

Usage examples
──────────────
  # Translate all entries, 10 at a time, output to ./candidates/
  python hypertrans_cli.py translate ENGLISH.txt --batch 10

  # Only translate entries whose keys contain "HELP"
  python hypertrans_cli.py translate ENGLISH.txt --filter HELP --batch 5

  # Resume: skip entries already present in an existing candidates file
  python hypertrans_cli.py translate ENGLISH.txt --resume candidates/ENGLISH_candidates.json

  # Show a summary of a candidates file
  python hypertrans_cli.py info candidates/ENGLISH_candidates.json

  # List all keys in a candidates file
  python hypertrans_cli.py list candidates/ENGLISH_candidates.json
"""

import argparse
import json
import os
import re
import random
import sys
import time
from pathlib import Path

try:
    from deep_translator import GoogleTranslator
except ImportError:
    print("ERROR: deep_translator not installed.  Run:  pip install deep-translator")
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────
ITERATION_COUNTS = [3, 5, 20, 50, 100]
SEPARATOR = "\n"
CANDIDATES_VERSION = 2

MAX_RUNTIME_SECONDS = 60 * 60 * 5.5  # 5.5 hours

ALL_LANG_CODES = list(GoogleTranslator().get_supported_languages(as_dict=True).values())

# ── ANSI colours (disabled on Windows if colorama absent) ────────────────────
try:
    import colorama; colorama.init()
    _COLOR = True
except ImportError:
    _COLOR = sys.stdout.isatty()

def _c(code, text):
    if not _COLOR:
        return text
    RESET = "\033[0m"
    return f"{code}{text}{RESET}"

BOLD    = lambda t: _c("\033[1m",    t)
DIM     = lambda t: _c("\033[2m",    t)
CYAN    = lambda t: _c("\033[36m",   t)
GREEN   = lambda t: _c("\033[32m",   t)
YELLOW  = lambda t: _c("\033[33m",   t)
RED     = lambda t: _c("\033[31m",   t)
MAGENTA = lambda t: _c("\033[35m",   t)
BLUE    = lambda t: _c("\033[34m",   t)

# ── Helpers ──────────────────────────────────────────────────────────────────
def strip_gxt_tags(text: str) -> str:
    return re.sub(r'~[a-zA-Z0-9]+~', '', text).strip()
    
def is_translatable(text: str) -> bool:
    """Check if the text contains at least one alphabetical letter. 
    If it's just symbols/numbers (like '<' or '123'), it shouldn't be translated."""
    return bool(re.search(r'[a-zA-Z]', text))


def load_localization(filepath: str) -> list[tuple[str, str]]:
    """Return list of (key, original_text) pairs from a UTF-16-LE file."""
    entries = []
    with open(filepath, 'r', encoding='utf-16-le') as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('[') and line.endswith(']'):
            key  = line
            text = lines[i + 1].strip() if i + 1 < len(lines) else ""
            entries.append((key, text))
            i += 2 if text else 1
        else:
            i += 1
    return entries


def _translate_with_retry(source: str, target: str, text: str, max_retries: int = 8) -> str:
    """Attempt translation repeatedly with exponential backoff until it succeeds."""
    # Don't waste time/API calls on empty strings
    if not text or not text.strip():
        return text 

    delay = 2.0  
    attempts = 0
    
    while attempts < max_retries:
        try:
            t = GoogleTranslator(source=source, target=target)
            result = t.translate(text)
            
            # If we get a valid string back, we are good to go
            if result and result.strip():
                return result
            else:
                # If Google returns empty, treat it as an error so we back off
                raise ValueError("API returned an empty string.")
                
        except Exception as e:
            attempts += 1
            if attempts >= max_retries:
                # If we hit the max limit, raise the exception so the main batch loop 
                # can catch it, log "[Error: ...]", and move on to the next batch!
                raise RuntimeError(f"Failed after {max_retries} attempts: {e}")
            
            # Print a tiny, temporary visual indicator so you know it's stuck/retrying
            print(f"{RED('!')}", end="", flush=True)
            
            time.sleep(delay)
            delay = min(delay * 1.5, 60.0)

def translate_text(text: str, iterations: int) -> str:
    """Bounce text through `iterations` random languages then back to English."""
    # Don't waste API calls on symbols, numbers, or empty strings
    if not is_translatable(text):
        return text
    
    current      = text
    current_lang = 'en'
    available    = [l for l in ALL_LANG_CODES if l != 'en']
    langs        = random.sample(available, min(iterations, len(available)))

    for lang in langs:
        current      = _translate_with_retry(current_lang, lang, current)
        current_lang = lang

    # Final bounce back to English
    current = _translate_with_retry(current_lang, 'en', current)
    
    return current


def translate_batch(entries: list[tuple[str, str]], iterations: int) -> dict[str, str]:
    """
    Translate a batch of entries as a single newline-joined block.
    Splits the result back by newline count.

    entries : list of (key, cleaned_text)
    returns : {key: translated_text}
    """
    if len(entries) == 1:
        key, text = entries[0]
        return {key: translate_text(text, iterations)}

    joined     = SEPARATOR.join(text for _, text in entries)
    translated = translate_text(joined, iterations)

    # Split on newlines; pad / truncate to match entry count
    parts = translated.split(SEPARATOR)
    results = {}
    for i, (key, original_text) in enumerate(entries):
        results[key] = parts[i].strip() if i < len(parts) else translated.strip()
    return results


# ── Candidates file I/O ──────────────────────────────────────────────────────
def load_candidates(path: str) -> dict:
    """Load a candidates JSON file. Returns the root dict."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_candidates(path: str, data: dict):
    """Write candidates dict to JSON (pretty-printed, UTF-8)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def default_output_path(input_path: str, out_dir: str | None) -> str:
    stem = Path(input_path).stem
    directory = out_dir or os.path.join(
        os.path.dirname(os.path.abspath(input_path)), "candidates")
    return os.path.join(directory, f"{stem}_candidates.json")


# ── Progress display ─────────────────────────────────────────────────────────
class ProgressBar:
    def __init__(self, total: int, width: int = 40):
        self.total   = max(total, 1)
        self.width   = width
        self.current = 0
        self._last_len = 0

    def update(self, done: int, label: str = ""):
        self.current = done
        pct  = done / self.total
        fill = int(self.width * pct)
        bar  = "█" * fill + "░" * (self.width - fill)
        line = f"\r  {CYAN(bar)}  {YELLOW(f'{int(pct*100):3d}%')}  {DIM(label)}"
        print(line, end="", flush=True)
        self._last_len = len(line)

    def done(self):
        fill = self.width
        bar  = "█" * fill
        line = f"\r  {GREEN(bar)}  {GREEN('100%')}"
        print(line + " " * max(0, self._last_len - len(line)))


# ── Commands ─────────────────────────────────────────────────────────────────
def cmd_translate(args):
    input_path = args.input
    if not os.path.exists(input_path):
        print(RED(f"ERROR: File not found: {input_path}"))
        sys.exit(1)

    output_path = args.output or default_output_path(input_path, args.outdir)
    batch_size  = max(1, args.batch)
    iterations  = args.iterations or ITERATION_COUNTS
    dry_run     = args.dry_run

    print(BOLD(f"\n  HyperTranslator CLI"))
    print(DIM("  ─────────────────────────────────────────"))
    print(f"  Input   : {CYAN(input_path)}")
    print(f"  Output  : {CYAN(output_path)}")
    print(f"  Batch   : {YELLOW(str(batch_size))} entries per request")
    print(f"  Hops    : {YELLOW(str(iterations))}")

    # Load source file
    try:
        all_entries = load_localization(input_path)
    except Exception as e:
        print(RED(f"\nERROR reading input: {e}"))
        sys.exit(1)

    print(f"  Entries : {YELLOW(str(len(all_entries)))} loaded\n")

    # Apply key filter
    if args.filter:
        pattern = re.compile(args.filter, re.IGNORECASE)
        all_entries = [(k, t) for k, t in all_entries if pattern.search(k)]
        print(f"  Filter  : {MAGENTA(args.filter)}  →  {YELLOW(str(len(all_entries)))} entries match\n")

    if not all_entries:
        print(YELLOW("  No entries to translate."))
        return

    # Resume: load existing candidates and skip already-done keys
    existing_data: dict = {
        "version":    CANDIDATES_VERSION,
        "source":     os.path.abspath(input_path),
        "iterations": iterations,
        "entries":    {}
    }
    already_done: set[str] = set()

    # Build a quick lookup of the original stripped text for comparison
    original_clean = {k: strip_gxt_tags(t) for k, t in all_entries}

    if args.resume and os.path.exists(output_path):
        try:
            existing_data = load_candidates(output_path)
            entries = existing_data.get("entries", {})
            
            # Find and remove keys that failed to translate
            keys_to_remove = []
            for key, hops_dict in entries.items():
                orig_text = original_clean.get(key, "")
                
                # Check if ALL translations for this key match the original text 
                # (or if they contain the "[Error:" tag from failed requests)
                is_untranslated = all(
                    t_text == orig_text or "[Error:" in t_text 
                    for t_text in hops_dict.values()
                )
                
                # Only remove and retry if the string is actually translatable!
                # If it's just a symbol like '<', we WANT it to remain unchanged.
                if not hops_dict or (is_untranslated and is_translatable(orig_text)):
                    keys_to_remove.append(key)
            
            # Strip them out so they get re-queued
            for k in keys_to_remove:
                del entries[k]
                
            already_done = set(entries.keys())
            
            resume_msg = f"  Resume  : {GREEN(str(len(already_done)))} valid keys present."
            if keys_to_remove:
                resume_msg += f" Removed {YELLOW(str(len(keys_to_remove)))} untranslated keys to retry."
            print(resume_msg + "\n")
            
        except Exception as e:
            print(YELLOW(f"  Warning: could not read existing file ({e}), starting fresh\n"))

    todo = [(k, t) for k, t in all_entries if k not in already_done]

    if not todo:
        print(GREEN("  All entries already translated. Nothing to do."))
        return

    # Build work batches
    clean_todo = [(k, strip_gxt_tags(t)) for k, t in todo if strip_gxt_tags(t)]
    batches    = [clean_todo[i:i + batch_size]
                  for i in range(0, len(clean_todo), batch_size)]

    total_batches = len(batches)
    total_steps   = total_batches * len(iterations)

    print(f"  To do   : {YELLOW(str(len(clean_todo)))} entries  →  "
          f"{YELLOW(str(total_batches))} batch(es)  ×  "
          f"{YELLOW(str(len(iterations)))} hop-counts  =  "
          f"{YELLOW(str(total_steps))} requests\n")

    if dry_run:
        print(YELLOW("  --dry-run: no translations will be performed."))
        for i, batch in enumerate(batches, 1):
            print(f"    Batch {i:3d}: {[k for k, _ in batch]}")
        return

    pb = ProgressBar(total_steps)
    step = 0
    results: dict = existing_data.get("entries", {})
    errors = 0

    t0 = time.time()

    try:
        for b_idx, batch in enumerate(batches):
            # Graceful runtime cutoff for GitHub Actions
            elapsed = time.time() - t0
            if elapsed >= MAX_RUNTIME_SECONDS:
                print(f"\n\n  {YELLOW('Runtime limit reached.')} Saving progress and exiting safely…")

                existing_data["entries"] = results
                save_candidates(output_path, existing_data)

                mins, secs = divmod(int(elapsed), 60)

                print(f"\n  {GREEN('✓ Progress saved')} after {mins}m {secs}s")
                print(f"  Saved to   : {CYAN(output_path)}\n")

                return
            
            batch_keys = [k for k, _ in batch]
            for it in iterations:
                label = (f"batch {b_idx+1}/{total_batches}  "
                         f"keys={batch_keys[:3]}{'…' if len(batch_keys)>3 else ''}  "
                         f"hops={it}")
                pb.update(step, label)

                try:
                    translated = translate_batch(batch, it)
                    for key, text in translated.items():
                        if key not in results:
                            results[key] = {}
                        results[key][str(it)] = text
                except Exception as e:
                    errors += 1
                    for key, _ in batch:
                        if key not in results:
                            results[key] = {}
                        results[key][str(it)] = f"[Error: {e}]"

                step += 1
                time.sleep(0.05)

            # Checkpoint: save after every batch
            existing_data["entries"] = results
            save_candidates(output_path, existing_data)

    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW('Interrupted.')}  Saving partial results…")

    pb.done()

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    print(f"\n  {GREEN('✓ Done')}  in {mins}m {secs}s")
    print(f"  Translated : {GREEN(str(len(results)))} entries")
    if errors:
        print(f"  Errors     : {RED(str(errors))}")
    print(f"  Saved to   : {CYAN(output_path)}\n")


def cmd_info(args):
    path = args.candidates
    if not os.path.exists(path):
        print(RED(f"ERROR: Not found: {path}"))
        sys.exit(1)
    data = load_candidates(path)
    entries = data.get("entries", {})
    iterations = data.get("iterations", [])
    print(BOLD(f"\n  Candidates file: {path}"))
    print(DIM("  ─────────────────────────────────────────"))
    print(f"  Source     : {CYAN(data.get('source', '?'))}")
    print(f"  Version    : {data.get('version', '?')}")
    print(f"  Hop counts : {YELLOW(str(iterations))}")
    print(f"  Entries    : {YELLOW(str(len(entries)))}")

    complete = sum(1 for v in entries.values()
                   if len(v) == len(iterations))
    print(f"  Complete   : {GREEN(str(complete))} / {len(entries)}")
    print()


def cmd_list(args):
    path = args.candidates
    if not os.path.exists(path):
        print(RED(f"ERROR: Not found: {path}"))
        sys.exit(1)
    data = load_candidates(path)
    entries = data.get("entries", {})
    print(BOLD(f"\n  Keys in {path}:\n"))
    for key in sorted(entries.keys()):
        hops = sorted(int(k) for k in entries[key].keys())
        print(f"  {CYAN(key):40s}  {DIM(str(hops))}")
    print(f"\n  Total: {YELLOW(str(len(entries)))}\n")


# ── Argument parser ──────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hypertrans_cli",
        description="HyperTranslator — batch localization translation via language-bounce chains",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ── translate ──
    t = sub.add_parser("translate", help="Translate a localization file")
    t.add_argument("input",
                   help="Path to the UTF-16-LE localization file (e.g. ENGLISH.txt)")
    t.add_argument("-b", "--batch", type=int, default=10, metavar="N",
                   help="Number of entries to send per translation request (default: 10)")
    t.add_argument("-o", "--output", metavar="FILE",
                   help="Output JSON file path (default: <indir>/candidates/<stem>_candidates.json)")
    t.add_argument("--outdir", metavar="DIR",
                   help="Directory for output (overridden by --output)")
    t.add_argument("--filter", metavar="REGEX",
                   help="Only translate keys matching this regex (case-insensitive)")
    t.add_argument("--iterations", type=int, nargs="+", metavar="N",
                   default=None,
                   help=f"Hop counts to use (default: {ITERATION_COUNTS})")
    t.add_argument("--resume", action="store_true",
                   help="Skip keys already present in the output file")
    t.add_argument("--dry-run", action="store_true",
                   help="Print what would be done without translating")

    # ── info ──
    i = sub.add_parser("info", help="Show summary of a candidates file")
    i.add_argument("candidates", help="Path to a _candidates.json file")

    # ── list ──
    ls = sub.add_parser("list", help="List all keys in a candidates file")
    ls.add_argument("candidates", help="Path to a _candidates.json file")

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    dispatch = {
        "translate": cmd_translate,
        "info":      cmd_info,
        "list":      cmd_list,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()