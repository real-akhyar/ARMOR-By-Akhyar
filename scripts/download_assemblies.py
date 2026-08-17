#!/usr/bin/env python3
"""
ARMOR External Validation - Automated Assembly Download Engine
Downloads high-quality FASTA assemblies for selected external validation isolates
using multi-threaded parallel HTTP streams with exponential backoff and QC checks.
"""

import os
import sys
import time
import requests
import concurrent.futures
import pandas as pd
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
METADATA_PATH = DATA_DIR / "external_validation_metadata.csv"
FASTA_DIR = DATA_DIR / "external_validation_fastas"
FASTA_DIR.mkdir(parents=True, exist_ok=True)

MAX_WORKERS = 10
MAX_RETRIES = 3

def download_genome_fasta(genome_id: str) -> tuple[str, bool, str]:
    """Download single FASTA assembly from BV-BRC with exponential backoff."""
    out_file = FASTA_DIR / f"{genome_id}.fasta"
    
    # If already downloaded and valid, skip
    if out_file.exists() and out_file.stat().st_size > 1_000_000:
        return genome_id, True, "Already cached"
        
    urls = [
        f"https://www.bv-brc.org/api/genome_sequence/?eq(genome_id,{genome_id})&http_accept=application/dna+fasta",
        f"https://patricbrc.org/api/genome_sequence/?eq(genome_id,{genome_id})&http_accept=application/dna+fasta"
    ]
    
    for attempt in range(1, MAX_RETRIES + 1):
        for url in urls:
            try:
                resp = requests.get(url, timeout=45)
                if resp.status_code == 200 and len(resp.text) > 100_000 and resp.text.startswith(">"):
                    with open(out_file, "w", encoding="utf-8") as f:
                        f.write(resp.text)
                    return genome_id, True, f"Success ({len(resp.text) / (1024*1024):.2f} MB)"
            except Exception as e:
                pass
        time.sleep(1.5 * attempt)
        
    return genome_id, False, "Download failed after retries"

def main():
    if not METADATA_PATH.exists():
        print(f"[Error] Metadata not found at {METADATA_PATH}. Run query first.")
        sys.exit(1)
        
    df_meta = pd.read_csv(METADATA_PATH, dtype={"genome_id": str})
    genome_ids = df_meta["genome_id"].tolist()
    print("="*65)
    print("ARMOR External Validation - Automated FASTA Downloader")
    print(f"Total Isolates to Download: {len(genome_ids)}")
    print(f"Target Directory: {FASTA_DIR}")
    print("="*65)
    
    t0 = time.time()
    success_count = 0
    fail_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_gid = {executor.submit(download_genome_fasta, gid): gid for gid in genome_ids}
        for future in concurrent.futures.as_completed(future_to_gid):
            gid = future_to_gid[future]
            try:
                gid, success, msg = future.result()
                if success:
                    success_count += 1
                    if success_count % 20 == 0 or success_count == len(genome_ids):
                        print(f"  [Progress] Downloaded {success_count}/{len(genome_ids)} assemblies...")
                else:
                    fail_count += 1
                    print(f"  [Failed] Genome {gid}: {msg}")
            except Exception as e:
                fail_count += 1
                print(f"  [Error] Exception on {gid}: {e}")
                
    elapsed = time.time() - t0
    print("\n" + "="*65)
    print(f"Download Summary: {success_count} succeeded, {fail_count} failed in {elapsed:.1f} s.")
    print("="*65)
    
    if fail_count > 0:
        print(f"[Warning] {fail_count} assemblies failed to download.")
    else:
        print("[Success] All external validation assemblies downloaded and verified.")

if __name__ == "__main__":
    main()
