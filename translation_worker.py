#!/usr/bin/env python3

import argparse
import os
import time
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor

from supabase import create_client
from deep_translator import GoogleTranslator
from tqdm import tqdm

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

WORKER_ID = f"worker-{os.getpid()}"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 3))

# --- UPGRADE: Hardcoded target batches ---
TARGET_BATCHES = [1, 3, 5, 7, 10]
ITERATION_COUNTS = [3, 5, 20, 50, 100]

SEPARATOR = "\n"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ALL_LANG_CODES = list(
    GoogleTranslator().get_supported_languages(as_dict=True).values()
)

# ─────────────────────────────────────────────

def get_table(batch):
    return f"tasks_b{batch}"

def get_rpc(batch):
    return f"claim_tasks_b{batch}"

def strip_gxt_tags(text):
    return re.sub(r'~[a-zA-Z0-9_]+~', '', text).strip()

def is_translatable(text):
    return bool(re.search(r'[a-zA-Z]', text))

# ─────────────────────────────────────────────

def _translate(source, target, text, retries=8):
    delay = 2

    for attempt in range(retries):
        try:
            result = GoogleTranslator(
                source=source,
                target=target
            ).translate(text)

            if result:
                return result

        except Exception:
            pass

        time.sleep(delay)
        delay *= 1.5

    raise RuntimeError("Translation failed")

# ─────────────────────────────────────────────

def translate_text(text, iterations):
    current = text
    current_lang = "en"

    langs = random.sample(
        [l for l in ALL_LANG_CODES if l != "en"],
        min(iterations, len(ALL_LANG_CODES)-1)
    )

    for lang in langs:
        current = _translate(current_lang, lang, current)
        current_lang = lang

    return _translate(current_lang, "en", current)

# ─────────────────────────────────────────────

def upload_tasks(entries, batch):
    table = get_table(batch)
    CHUNK_SIZE = 100  # safe size for URL query parameter

    key_to_text = {k: strip_gxt_tags(t) for k, t in entries}
    all_keys = list(key_to_text.keys())

    existing_keys = set()
    for i in range(0, len(all_keys), CHUNK_SIZE):
        chunk = all_keys[i:i+CHUNK_SIZE]
        res = supabase.table(table)\
            .select("key")\
            .in_("key", chunk)\
            .execute()
        existing_keys.update(r["key"] for r in res.data)

    new_entries = [
        {"key": k, "original_text": key_to_text[k], "status": "pending"}
        for k in all_keys if k not in existing_keys
    ]

    skipped = len(entries) - len(new_entries)
    if skipped:
        print(f"⚠️  Skipped {skipped} already‑existing keys in {table}.")

    if not new_entries:
        print(f"✅ All keys already present in {table} — nothing to insert.")
        return

    for i in range(0, len(new_entries), CHUNK_SIZE):
        chunk = new_entries[i:i+CHUNK_SIZE]
        supabase.table(table).insert(chunk).execute()

    print(f"✅ Inserted {len(new_entries)} new tasks into {table}.")

# ─────────────────────────────────────────────

def claim_tasks(batch, batch_size):
    rpc = get_rpc(batch)

    res = supabase.rpc(rpc, {
        "batch_size": batch_size,
        "worker_id": WORKER_ID
    }).execute()

    return res.data or []

# ─────────────────────────────────────────────

def complete_task(task_id, result, batch):
    table = get_table(batch)

    supabase.table(table).update({
        "status": "done",
        "result": result,
        "updated_at": "now()"
    }).eq("id", task_id).execute()

# ─────────────────────────────────────────────

def process_task_group(tasks):
    keys = [t["key"] for t in tasks]
    texts = [t["original_text"] for t in tasks]

    results = {k: {} for k in keys}

    for it in ITERATION_COUNTS:
        try:
            joined = SEPARATOR.join(texts)
            translated = translate_text(joined, it)
            parts = translated.split(SEPARATOR)

            if len(parts) < len(texts):
                parts += [""] * (len(texts) - len(parts))

            parts = parts[:len(texts)]

            for k, t in zip(keys, parts):
                results[k][str(it)] = t.strip()

        except Exception as e:
            for k in keys:
                results[k][str(it)] = f"[Error: {e}]"

    return [(t["id"], results[t["key"]]) for t in tasks]

# ─────────────────────────────────────────────

def worker_loop():
    print(f"🚀 Multi-Batch Worker {WORKER_ID} Started")
    print(f"   Target Queue: Batches {TARGET_BATCHES}")
    
    # --- UPGRADE: Loop through each hardcoded batch ---
    for batch in TARGET_BATCHES:
        print("\n" + "="*60)
        print(f"🔄 SWITCHING TO BATCH {batch}")
        print("="*60)
        
        table = get_table(batch)

        try:
            total_res = supabase.table(table).select("id", count="exact").execute()
            total_tasks = total_res.count if total_res.count else 0
            
            done_res = supabase.table(table).select("id", count="exact").eq("status", "done").execute()
            completed = done_res.count if done_res.count else 0
        except Exception as e:
            print(f"⚠️ Could not fetch progress counts for batch {batch}: {e}")
            total_tasks = 100
            completed = 0

        pbar = tqdm(total=total_tasks, initial=completed, desc=f"Batch {batch} Progress", unit="line")

        while True:
            tasks = claim_tasks(batch, batch)

            if not tasks:
                pbar.write(f"\n🎉 Batch {batch} is complete! No pending tasks left in this table.")
                pbar.close()
                break # Break inner loop to move to the next batch in the queue

            pbar.write(f"\n📦 Claimed {len(tasks)} task(s), translating as one batch...")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future = executor.submit(process_task_group, tasks)
                task_results = future.result()

            task_dict = {t["id"]: t for t in tasks}

            for task_id, result in task_results:
                try:
                    complete_task(task_id, result, batch)
                    
                    original_text = task_dict[task_id]["original_text"]
                    key = task_dict[task_id]["key"]
                    
                    pbar.write(f"\n  ✓ Line {key} saved:")
                    pbar.write(f"      Original: \"{original_text}\"")
                    for iterations, translated_text in result.items():
                        pbar.write(f"      {iterations} hops: \"{translated_text}\"")
                    pbar.write("      " + "-" * 40)
                    
                except Exception as e:
                    pbar.write(f"  ❌ Could not save result for task {task_id}: {e}")
                
                pbar.update(1)

            try:
                done_res = supabase.table(table).select("id", count="exact").eq("status", "done").execute()
                actual_completed = done_res.count if done_res.count else pbar.n
                
                pbar.n = actual_completed
                pbar.refresh()
                
                pbar.write(f"\n📊 BATCH {batch} COMPLETE | Progress: {actual_completed} / {total_tasks} translated.\n")
                
            except Exception as e:
                pbar.write(f"  ⚠️ Could not sync final batch count with database: {e}")

    # --- UPGRADE: Grand finale message once all batches are done ---
    print("\n" + "🏆"*20)
    print(f" ALL TARGET BATCHES {TARGET_BATCHES} ARE FULLY TRANSLATED!")
    print(" Shutting down worker gracefully. Goodbye!")
    print("🏆"*20 + "\n")

# ─────────────────────────────────────────────

def export_results(output_file):
    final = {
        "version": 2,
        "iterations": ITERATION_COUNTS,
        "entries": {}
    }

    # --- UPGRADE: Export and merge all target batches into one file ---
    for batch in TARGET_BATCHES:
        print(f"📥 Exporting Batch {batch}...")
        table = get_table(batch)

        res = supabase.table(table)\
            .select("*")\
            .eq("status", "done")\
            .execute()

        for row in res.data:
            final["entries"][row["key"]] = row["result"]

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
        
    print(f"\n✅ All batches successfully exported to {output_file}")

# ─────────────────────────────────────────────

def load_localization(path):
    entries = []

    with open(path, "r", encoding="utf-16-le") as f:
        lines = f.readlines()

    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("[") and line.endswith("]"):
            key = line
            text = lines[i+1].strip()

            entries.append((key, text))

            i += 2
        else:
            i += 1

    return entries

# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- UPGRADE: Removed --batch from all commands ---
    init = sub.add_parser("init")
    init.add_argument("file")

    worker = sub.add_parser("worker")

    export = sub.add_parser("export")
    export.add_argument("--out", required=True)

    args = parser.parse_args()

    if args.cmd == "init":
        entries = load_localization(args.file)
        # Apply init file to all hardcoded batches
        for batch in TARGET_BATCHES:
            print(f"\n🚀 Initializing Batch {batch}...")
            upload_tasks(entries, batch)

    elif args.cmd == "worker":
        worker_loop()

    elif args.cmd == "export":
        export_results(args.out)

if __name__ == "__main__":
    main()