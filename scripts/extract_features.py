#!/usr/bin/env python3
"""
ARMOR External Validation - Multi-omic Feature Extraction Engine
Extracts multi-omic reference-projected features across all 39,876 feature space
for downloaded Klebsiella pneumoniae assemblies in parallel.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import concurrent.futures
import numpy as np
import pandas as pd
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REF_DIR = REPO_ROOT / "reference"
DATA_DIR = REPO_ROOT / "data"
FASTA_DIR = DATA_DIR / "external_validation_fastas"
METADATA_PATH = DATA_DIR / "external_validation_metadata.csv"

OUTPUT_X = DATA_DIR / "X_external_features.csv"
OUTPUT_Y = DATA_DIR / "Y_external_labels.csv"

# Load static reference mappings
with open(REF_DIR / "reference_feature_columns.json", "r") as f:
    FEATURE_COLUMNS = json.load(f)

with open(REF_DIR / "kmer_to_prot.json", "r") as f:
    KMER_LIST = json.load(f)
    KMER_TO_IDX = {km: idx for idx, km in enumerate(KMER_LIST)}

AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWY"
ACQUIRED_FOSA = ["fosa2", "fosa3", "fosa4", "fosa5", "fosa6", "fosa7", "fosa8"]

def to_wsl_path(win_path: Path) -> str:
    p = str(win_path.resolve()).replace("\\", "/")
    drive = p[0].lower()
    return f"/mnt/{drive}{p[2:]}"

PAN_REF_WSL = to_wsl_path(REF_DIR / "pan_genome_reference.fa")
CARD_DMND_WSL = to_wsl_path(REF_DIR / "card.dmnd")

def extract_features_single(fasta_path: Path) -> tuple[str, np.ndarray, int]:
    """Extract 39,876 feature vector for one genome assembly."""
    gid = fasta_path.stem
    fasta_wsl = to_wsl_path(fasta_path)
    
    detected_features = set()
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        tmp_wsl = to_wsl_path(tmp_path)
        
        # 1. Pangenome BLASTN
        blast_out = tmp_wsl + "/blast_pan.tsv"
        cmd_pan = f"blastn -query {PAN_REF_WSL} -db {fasta_wsl} -outfmt '6 qseqid pident' -max_target_seqs 1 -num_threads 2 -out {blast_out}"
        subprocess.run(["wsl", "-e", "bash", "-c", cmd_pan], capture_output=True, check=False)
        
        win_blast_out = tmp_path / "blast_pan.tsv"
        if win_blast_out.exists() and win_blast_out.stat().st_size > 0:
            df_pan = pd.read_csv(win_blast_out, sep="\t", names=["qseqid", "pident"])
            df_hits = df_pan[df_pan["pident"] >= 95.0]
            for hit in df_hits["qseqid"].unique():
                detected_features.add(f"pan__{hit}")
                
        # 2. Prodigal Translation
        prot_faa = tmp_wsl + "/proteins.faa"
        cmd_prod = f"prodigal -i {fasta_wsl} -a {prot_faa} -q"
        subprocess.run(["wsl", "-e", "bash", "-c", cmd_prod], capture_output=True, check=False)
        
        # 3. CARD DIAMOND RGI
        diamond_out = tmp_wsl + "/diamond_rgi.tsv"
        cmd_dmnd = f"diamond blastp -d {CARD_DMND_WSL} -q {prot_faa} --id 90 --query-cover 80 -f 6 qseqid sseqid pident qseq -o {diamond_out} -p 2 --quiet"
        subprocess.run(["wsl", "-e", "bash", "-c", cmd_dmnd], capture_output=True, check=False)
        
        rgi_proteins = []
        rgi_genes = set()
        
        win_dmnd_out = tmp_path / "diamond_rgi.tsv"
        if win_dmnd_out.exists() and win_dmnd_out.stat().st_size > 0:
            df_rgi = pd.read_csv(win_dmnd_out, sep="\t", names=["qseqid", "sseqid", "pident", "qseq"])
            for _, row in df_rgi.iterrows():
                target = str(row["sseqid"]).strip()
                rgi_genes.add(target)
                detected_features.add(f"rgi__{target}")
                
                prot_seq = str(row["qseq"]).strip().upper()
                if len(prot_seq) > 10 and all(aa in AMINO_ACIDS for aa in prot_seq):
                    rgi_proteins.append(prot_seq)
                    
        # 4. Protein 3-mers Frequency Profile (7,038 continuous bins)
        kmer_profile = np.zeros(len(KMER_LIST), dtype=np.float32)
        if rgi_proteins:
            counts = Counter()
            for seq in rgi_proteins:
                for i in range(len(seq) - 2):
                    kmer = seq[i:i+3]
                    if all(aa in AMINO_ACIDS for aa in kmer):
                        counts[kmer] += 1
            total = sum(counts.values())
            if total > 0:
                for km, c in counts.items():
                    if km in KMER_TO_IDX:
                        idx = KMER_TO_IDX[km]
                        kmer_profile[idx] = float(c / total)
                        detected_features.add(f"prot__{idx}")
                        
        # 5. Fosfomycin Mechanisms
        acquired_fosA = int(any(any(v in g.lower() for g in rgi_genes) for v in ACQUIRED_FOSA))
        if acquired_fosA:
            detected_features.add("fos__fosA3")
            detected_features.add("fos__fosA_acquired")
            
        fosA_pan = int(any(f.startswith("pan__") and "fos" in f.lower() for f in detected_features))
        if fosA_pan:
            detected_features.add("fos__fosA_pangenome")
            
        # 6. Assemble 39,876 Dense Vector
        vector = np.zeros(len(FEATURE_COLUMNS), dtype=np.float32)
        for col_idx, col_name in enumerate(FEATURE_COLUMNS):
            if col_name in detected_features:
                if col_name.startswith("prot__"):
                    try:
                        k_idx = int(col_name.split("prot__")[1])
                        vector[col_idx] = kmer_profile[k_idx]
                    except:
                        vector[col_idx] = 1.0
                else:
                    vector[col_idx] = 1.0
                    
    non_zeros = int((vector > 0).sum())
    return gid, vector, non_zeros

def main():
    print("="*70)
    print("ARMOR External Validation - Multi-omic Feature Extraction Engine")
    print(f"Reference Feature Dimensions: {len(FEATURE_COLUMNS):,} columns")
    print("="*70)
    
    if not METADATA_PATH.exists():
        print(f"[Error] Metadata not found: {METADATA_PATH}")
        sys.exit(1)
        
    df_meta = pd.read_csv(METADATA_PATH, dtype={"genome_id": str})
    fasta_files = [FASTA_DIR / f"{gid}.fasta" for gid in df_meta["genome_id"] if (FASTA_DIR / f"{gid}.fasta").exists()]
    
    print(f"Total Assemblies to Process: {len(fasta_files)}")
    t0 = time.time()
    
    results = {}
    completed = 0
    
    # Process with 6 concurrent worker threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        future_to_fa = {executor.submit(extract_features_single, fa): fa for fa in fasta_files}
        for future in concurrent.futures.as_completed(future_to_fa):
            fa = future_to_fa[future]
            try:
                gid, vec, non_zeros = future.result()
                results[gid] = vec
                completed += 1
                if completed % 10 == 0 or completed == len(fasta_files):
                    elapsed = time.time() - t0
                    rate = completed / elapsed
                    eta = (len(fasta_files) - completed) / rate if rate > 0 else 0
                    print(f"  [Progress] Processed {completed:3d}/{len(fasta_files)} genomes | Rate: {rate:.2f} gen/s | ETA: {eta:.0f}s")
            except Exception as e:
                print(f"  [Error] Failed to process {fa.name}: {e}")
                
    total_time = time.time() - t0
    print(f"\n[Extraction Complete] Processed {len(results)} genomes in {total_time/60:.2f} minutes.")
    
    # Assemble final DataFrame
    print("\n[Export] Assembling X_external_features DataFrame...")
    sorted_gids = sorted(list(results.keys()))
    feature_matrix = np.array([results[gid] for gid in sorted_gids], dtype=np.float32)
    
    df_X = pd.DataFrame(feature_matrix, index=sorted_gids, columns=FEATURE_COLUMNS)
    df_X.index.name = "genome_id"
    df_X.to_csv(OUTPUT_X)
    print(f"  Saved X matrix ({df_X.shape[0]} rows x {df_X.shape[1]} cols) to:\n    {OUTPUT_X}")
    
    # Assemble Y labels DataFrame
    df_meta_indexed = df_meta.set_index("genome_id")
    common_ids = [gid for gid in sorted_gids if gid in df_meta_indexed.index]
    
    label_cols = ["Amikacin", "Cefepime", "Piperacillin_Tazobactam", "Fosfomycin"]
    df_Y = df_meta_indexed.loc[common_ids, label_cols].copy()
    df_Y.to_csv(OUTPUT_Y)
    print(f"  Saved Y labels ({df_Y.shape[0]} rows x {df_Y.shape[1]} cols) to:\n    {OUTPUT_Y}")
    print("\n[Success] Feature extraction and label generation successfully completed!")

if __name__ == "__main__":
    main()
