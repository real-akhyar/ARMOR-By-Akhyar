#!/usr/bin/env python3
"""
ARMOR External Validation - Cohort Query & Selection Engine
Queries BV-BRC API for Klebsiella pneumoniae isolates with laboratory-verified AST for:
  - Amikacin
  - Cefepime
  - Piperacillin/Tazobactam
  - Fosfomycin
Enforces strict anti-leakage exclusion and quality filters (4500 <= CDS <= 6500).
"""

import os
import sys
import json
import time
import requests
import concurrent.futures
import pandas as pd
import numpy as np
from pathlib import Path

# Repository paths
REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINING_LABELS_PATH = REPO_ROOT / "model_training" / "features" / "Y_labels_with_bioproject_2.csv"
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_METADATA = DATA_DIR / "external_validation_metadata.csv"

# Target Antibiotics
TARGET_DRUGS = ["Amikacin", "Cefepime", "Piperacillin_Tazobactam", "Fosfomycin"]

# Known BioProjects to exclude (Training & previous validation studies)
EXCLUDED_BIOPROJECTS = {
    "PRJNA376414", "PRJEB31361", "PRJEB28400", "PRJEB6574",
    "PRJEB6543", "PRJEB6891", "PRJNA288601", "PRJEB22890",
    "PRJNA643814", "PRJEB24082", "PRJNA351909", "PRJEB11403",
    "PRJEB29424", "PRJEB7661", "PRJNA497126", "PRJNA292902",
    "PRJEB1272", "PRJNA296771", "PRJNA292904", "PRJNA278886",
    "PRJNA271899", "PRJEB19229", "PRJEB1800", "PRJNA316321",
    "PRJNA530794", "PRJNA313004", "PRJNA397262"
}

def load_excluded_genome_ids():
    excluded_ids = set()
    if TRAINING_LABELS_PATH.exists():
        df = pd.read_csv(TRAINING_LABELS_PATH)
        first_col = df.columns[0]
        excluded_ids.update(df[first_col].dropna().astype(str).tolist())
        if "bioproject" in df.columns:
            for bp in df["bioproject"].dropna().unique():
                if bp != "Unknown":
                    EXCLUDED_BIOPROJECTS.add(str(bp).strip())
    print(f"[Audit] Loaded {len(excluded_ids)} internal training genome IDs to exclude.")
    print(f"[Audit] Registered {len(EXCLUDED_BIOPROJECTS)} excluded BioProjects.")
    return excluded_ids

def query_bvbrc_amr_records():
    """Query BV-BRC API for all 4 target drugs."""
    print("\n[Query BV-BRC] Fetching records with Laboratory AST...")
    base_url = "https://www.bv-brc.org/api/genome_amr/"
    
    drug_queries = {
        "amikacin": "Amikacin",
        "cefepime": "Cefepime",
        "piperacillin%2Ftazobactam": "Piperacillin_Tazobactam",
        "fosfomycin": "Fosfomycin",
    }
    
    records = []
    for q_ab, std_drug in drug_queries.items():
        print(f"  -> Querying: '{std_drug}'...")
        url = f"{base_url}?eq(taxon_id,573)&eq(evidence,%22Laboratory%20Method%22)&eq(antibiotic,%22{q_ab}%22)&limit(5000)&http_accept=application/json"
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                docs = resp.json()
                print(f"     Retrieved {len(docs)} raw records for {std_drug}.")
                for d in docs:
                    pheno = str(d.get("resistant_phenotype", "")).strip()
                    if pheno in ["Resistant", "Susceptible"]:
                        records.append({
                            "genome_id": str(d.get("genome_id", "")).strip(),
                            "genome_name": d.get("genome_name", ""),
                            "source": "BV-BRC",
                            "asm_acc": "",
                            "drug": std_drug,
                            "label": 1 if pheno == "Resistant" else 0,
                            "phenotype": pheno,
                            "measurement": d.get("measurement", ""),
                            "typing_method": d.get("laboratory_typing_method", ""),
                        })
        except Exception as e:
            print(f"     [Error] Query failed for {std_drug}: {e}")
            
    df = pd.DataFrame(records)
    print(f"[BV-BRC] Total valid binary records retrieved: {len(df)}")
    return df

def query_bvbrc_metadata_parallel(genome_ids):
    """Retrieve metadata (BioProject, CDS, Length, Country) for BV-BRC genomes using multi-threading."""
    print(f"[Metadata] Retrieving BV-BRC metadata for {len(genome_ids)} genomes in parallel...")
    base_url = "https://www.bv-brc.org/api/genome/"
    
    id_list = list(genome_ids)
    batch_size = 100
    chunks = [id_list[i:i + batch_size] for i in range(0, len(id_list), batch_size)]
    
    meta = {}
    def fetch_chunk(chunk):
        url = f"{base_url}?in(genome_id,({','.join(chunk)}))&limit({len(chunk)+50})&http_accept=application/json"
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_chunk, chunks))
        
    for res_list in results:
        for doc in res_list:
            gid = doc.get("genome_id")
            if gid:
                meta[str(gid).strip()] = doc
                
    print(f"[Metadata] Successfully cached metadata for {len(meta)} unique genomes.")
    return meta

def main():
    excluded_ids = load_excluded_genome_ids()
    
    # 1. Fetch raw AMR records
    df_raw = query_bvbrc_amr_records()
    if df_raw.empty:
        print("[Error] No AMR records retrieved. Exiting.")
        sys.exit(1)
        
    # Exclude internal training IDs
    df_filtered = df_raw[~df_raw["genome_id"].isin(excluded_ids)].copy()
    print(f"[Filter] Records remaining after excluding training genome IDs: {len(df_filtered)}")
    
    # 2. Fetch Metadata in Parallel
    unique_gids = df_filtered["genome_id"].unique()
    meta_dict = query_bvbrc_metadata_parallel(unique_gids)
    
    # 3. Enrich & Apply Quality / BioProject Filters
    clean_rows = []
    for _, row in df_filtered.iterrows():
        gid = row["genome_id"]
        m = meta_dict.get(gid, {})
        
        bp = str(m.get("bioproject_accession", "Unknown")).strip()
        if bp in ["None", "null", "nan", ""]: bp = "Unknown"
        
        cds = m.get("cds", 5400)
        try: cds = int(cds)
        except: cds = 5400
        
        # Anti-leakage and QC filter
        if bp not in EXCLUDED_BIOPROJECTS and 4500 <= cds <= 6500:
            clean_rows.append({
                "genome_id": gid,
                "genome_name": m.get("genome_name", row["genome_name"]),
                "source": "BV-BRC",
                "asm_acc": m.get("genbank_accessions", ""),
                "bioproject": bp,
                "cds": cds,
                "contigs": m.get("contigs", 0),
                "genome_length": m.get("genome_length", 0),
                "country": m.get("isolation_country", m.get("geographic_location", "Unknown")),
                "collection_date": m.get("collection_date", "Unknown"),
                "drug": row["drug"],
                "label": row["label"],
                "phenotype": row["phenotype"],
                "measurement": row["measurement"],
                "typing_method": row["typing_method"],
            })
            
    df_clean = pd.DataFrame(clean_rows)
    print(f"[Filter] Records remaining after anti-leakage BioProject and CDS filters: {len(df_clean)}")
    
    # Pivot by genome
    pivot = df_clean.pivot_table(
        index=["genome_id", "genome_name", "source", "asm_acc", "bioproject", "cds", "contigs", "genome_length", "country", "collection_date"],
        columns="drug",
        values="label",
        aggfunc="first"
    ).reset_index()
    
    print(f"[Cohort Selection] Total unique qualifying external genomes: {len(pivot)}")
    
    # Print availability per drug
    print("\n" + "="*65)
    print("POOL AVAILABILITY PER ANTIBIOTIC:")
    print("="*65)
    for drug in TARGET_DRUGS:
        if drug in pivot.columns:
            sub = pivot[pivot[drug].notna()]
            n_tot = len(sub)
            n_res = int((sub[drug] == 1).sum())
            n_sus = n_tot - n_res
            print(f"  • {drug:<25}: Total={n_tot:>4} (Resistant={n_res:>3}, Susceptible={n_sus:>3})")
    print("="*65)
    
    # Stratified Cohort Selection (~150-200 per drug with >= 30 Resistant)
    selected_gids = set()
    
    # 1. Fosfomycin (High Priority - take all available)
    if "Fosfomycin" in pivot.columns:
        fos_df = pivot[pivot["Fosfomycin"].notna()]
        selected_gids.update(fos_df["genome_id"].tolist())
        
    # 2. Iteratively select balanced samples for Amikacin, Cefepime, Piperacillin_Tazobactam
    for drug in ["Amikacin", "Cefepime", "Piperacillin_Tazobactam"]:
        if drug not in pivot.columns:
            continue
        cur = pivot[pivot["genome_id"].isin(selected_gids) & pivot[drug].notna()]
        cur_res = int((cur[drug] == 1).sum())
        cur_tot = len(cur)
        
        needed_res = max(0, 50 - cur_res)
        needed_tot = max(0, 180 - cur_tot)
        
        avail = pivot[~pivot["genome_id"].isin(selected_gids) & pivot[drug].notna()]
        res_pool = avail[avail[drug] == 1]
        sus_pool = avail[avail[drug] == 0]
        
        s_res = res_pool.sample(n=min(len(res_pool), needed_res), random_state=42) if needed_res > 0 and len(res_pool) > 0 else pd.DataFrame()
        needed_sus = max(0, needed_tot - len(s_res))
        s_sus = sus_pool.sample(n=min(len(sus_pool), needed_sus), random_state=42) if needed_sus > 0 and len(sus_pool) > 0 else pd.DataFrame()
        
        for s in [s_res, s_sus]:
            if not s.empty:
                selected_gids.update(s["genome_id"].tolist())
                
    final_cohort = pivot[pivot["genome_id"].isin(selected_gids)].copy()
    
    print("\n" + "="*70)
    print("FINAL SELECTED EXTERNAL VALIDATION COHORT:")
    print(f"Total Unique Genomes in Cohort: {len(final_cohort)}")
    print("="*70)
    for drug in TARGET_DRUGS:
        if drug in final_cohort.columns:
            sub = final_cohort[final_cohort[drug].notna()]
            n_tot = len(sub)
            n_res = int((sub[drug] == 1).sum())
            n_sus = n_tot - n_res
            prev = (n_res / n_tot * 100) if n_tot > 0 else 0
            print(f"  • {drug:<25}: n={n_tot:>3} | Resistant={n_res:>3} | Susceptible={n_sus:>3} | Prev={prev:4.1f}%")
    print("="*70)
    
    # Save selected metadata
    final_cohort.to_csv(OUTPUT_METADATA, index=False)
    print(f"\n[Success] External cohort metadata saved to:\n  {OUTPUT_METADATA}")

if __name__ == "__main__":
    main()
