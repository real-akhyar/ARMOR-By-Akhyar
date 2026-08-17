#!/usr/bin/env python3
"""
ARMOR External Validation - Inference, Metrics & Publication Figures Engine
Evaluates locked ONNX LightGBM models against the independent external validation cohort.
Computes publication-grade clinical diagnostic metrics with 95% Hanley-McNeil CIs,
generates high-resolution publication figures (ROC, PR, Confusion Matrices, SHAP),
and exports predictions and summary tables.
"""

import os
import sys
import json
import math
import warnings
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
import onnxruntime as ort

from sklearn.metrics import (
    roc_auc_score,
    average_precision_score,
    f1_score,
    accuracy_score,
    precision_score,
    recall_score,
    confusion_matrix,
    roc_curve,
    precision_recall_curve,
)
import shap

warnings.filterwarnings("ignore")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MODELS_DIR = REPO_ROOT / "model_training" / "models"
REF_DIR = REPO_ROOT / "reference"
RESULTS_DIR = REPO_ROOT / "results"
FIGURES_DIR = RESULTS_DIR / "figures"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
FIGURES_DIR.mkdir(parents=True, exist_ok=True)

TARGET_ANTIBIOTICS = {
    "Amikacin": {"stem": "amikacin", "col": "Amikacin", "color": "#2563EB"},
    "Cefepime": {"stem": "cefepime", "col": "Cefepime", "color": "#DC2626"},
    "Piperacillin/Tazobactam": {"stem": "piperacillin_tazobactam", "col": "Piperacillin_Tazobactam", "color": "#059669"},
    "Fosfomycin": {"stem": "fosfomycin", "col": "Fosfomycin", "color": "#D97706"},
}

# Plotting aesthetics
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Inter", "Segoe UI", "Helvetica", "Arial"],
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.labelsize": 11,
    "figure.dpi": 200,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.15,
})

def hanley_mcneil_ci(auc: float, n_pos: int, n_neg: int) -> tuple[float, float, float]:
    """Compute standard 95% Confidence Interval for AUC using Hanley-McNeil method."""
    if n_pos <= 0 or n_neg <= 0:
        return auc, auc, 0.0
    q1 = auc / (2.0 - auc)
    q2 = (2.0 * auc * auc) / (1.0 + auc)
    num = (auc * (1.0 - auc)) + (n_pos - 1) * (q1 - auc**2) + (n_neg - 1) * (q2 - auc**2)
    den = float(n_pos * n_neg)
    se = math.sqrt(max(0, num / den))
    ci_low = max(0.0, auc - 1.96 * se)
    ci_high = min(1.0, auc + 1.96 * se)
    return ci_low, ci_high, se

def predict_onnx(session: ort.InferenceSession, X: np.ndarray) -> np.ndarray:
    """Run ONNX inference and extract positive class probabilities."""
    inputs = {inp.name: inp for inp in session.get_inputs()}
    feed = {"Features": X.astype(np.float32)}
    if "Label" in inputs:
        inp_type = inputs["Label"].type
        feed["Label"] = np.zeros((X.shape[0], 1), dtype=bool if "bool" in inp_type else np.float32)
    outs = session.run(None, feed)
    output_names = [o.name for o in session.get_outputs()]
    
    if "Probability.output" in output_names:
        prob_out = outs[output_names.index("Probability.output")]
    else:
        prob_out = outs[-1]
        
    if isinstance(prob_out, list) and isinstance(prob_out[0], dict):
        probs = np.array([d.get(1, d.get("1", 0.5)) for d in prob_out])
    elif isinstance(prob_out, np.ndarray):
        probs = prob_out[:, 1] if (prob_out.ndim == 2 and prob_out.shape[1] == 2) else prob_out.ravel()
    else:
        probs = np.array(prob_out).ravel()
    return probs

def run_shap_analysis(session, onnx_path, X_ext, feature_names, ab_display, figures_dir):
    """Run explainability analysis on external cohort and save beeswarm and bar plots."""
    import onnx
    from collections import Counter
    print(f"  [SHAP] Extracting top split features from {onnx_path.name}...")
    try:
        model = onnx.load(str(onnx_path))
        split_counts = Counter()
        for node in model.graph.node:
            if node.op_type in ['TreeEnsembleRegressor', 'TreeEnsembleClassifier']:
                modes = None
                fids = None
                for attr in node.attribute:
                    if attr.name == 'nodes_modes': modes = attr.strings
                    elif attr.name == 'nodes_featureids': fids = attr.ints
                if modes is not None and fids is not None:
                    for mode, fid in zip(modes, fids):
                        if mode == b'BRANCH_LEQ': split_counts[fid] += 1
        active_fids = [fid for fid, _ in split_counts.most_common(60)]
    except:
        active_fids = list(range(min(60, len(feature_names))))
        
    if not active_fids:
        active_fids = list(range(min(60, len(feature_names))))
        
    n_samples = min(len(X_ext), 80)
    X_shap = X_ext[:n_samples]
    X_sub = X_shap[:, active_fids]
    baseline = np.mean(X_ext, axis=0)
    
    def predict_fn(x_sub):
        X_full = np.tile(baseline, (x_sub.shape[0], 1))
        X_full[:, active_fids] = x_sub
        return predict_onnx(session, X_full)
        
    feature_names_sub = [feature_names[i] for i in active_fids]
    explainer = shap.Explainer(predict_fn, X_sub[:min(10, len(X_sub))], feature_names=feature_names_sub)
    shap_vals = explainer(X_sub)
    
    # Save Beeswarm Plot
    fig_b, _ = plt.subplots(figsize=(10, 7))
    shap.plots.beeswarm(shap_vals, max_display=20, show=False)
    plt.title(f"SHAP Summary (Top Features) - {ab_display}\n(Independent External Validation Cohort, n={n_samples})",
              fontsize=12, fontweight="bold", pad=12)
    plt.tight_layout()
    stem = ab_display.lower().replace('/', '_').replace(' ', '_')
    plt.savefig(figures_dir / f"shap_beeswarm_{stem}.png")
    plt.close("all")
    print(f"  [OK] Saved SHAP beeswarm plot: shap_beeswarm_{stem}.png")

def main():
    print("="*75)
    print("ARMOR External Validation - Inference & Clinical Evaluation Engine")
    print(f"Execution Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*75)
    
    # 1. Load Feature Matrix & Target Labels
    X_path = DATA_DIR / "X_external_features.csv"
    Y_path = DATA_DIR / "Y_external_labels.csv"
    
    if not X_path.exists() or not Y_path.exists():
        print(f"[Error] Required feature matrix or labels missing ({X_path}, {Y_path})")
        sys.exit(1)
        
    df_X = pd.read_csv(X_path, index_col=0)
    df_Y = pd.read_csv(Y_path, index_col=0)
    
    common_ids = sorted(list(df_X.index.intersection(df_Y.index)))
    df_X = df_X.loc[common_ids]
    df_Y = df_Y.loc[common_ids]
    
    feature_names = list(df_X.columns)
    print(f"\n[Loaded Data] Evaluated Cohort: {len(common_ids)} isolates x {len(feature_names):,} features.")
    
    predictions_df = pd.DataFrame(index=common_ids)
    predictions_df.index.name = "genome_id"
    
    all_results = {}
    summary_rows = []
    
    # 2. Iterate through Target Antibiotics
    for ab_display, ab_info in TARGET_ANTIBIOTICS.items():
        stem = ab_info["stem"]
        label_col = ab_info["col"]
        color = ab_info["color"]
        
        print("\n" + "-"*65)
        print(f"  [*] EVALUATION: {ab_display.upper()}")
        print("-"*65)
        
        if label_col not in df_Y.columns:
            print(f"  [Warning] Label column '{label_col}' not found. Skipping.")
            continue
            
        y_series = df_Y[label_col].dropna()
        eval_ids = y_series.index.intersection(df_X.index)
        
        if len(eval_ids) == 0:
            print(f"  [Warning] No labeled isolates for {ab_display}. Skipping.")
            continue
            
        X_eval = df_X.loc[eval_ids].values.astype(np.float32)
        y_eval = y_series.loc[eval_ids].values.astype(int)
        
        n_pos = int(y_eval.sum())
        n_neg = len(y_eval) - n_pos
        
        print(f"  Cohort Size: {len(eval_ids)} (Resistant: {n_pos} | Susceptible: {n_neg} | Prev: {n_pos/len(eval_ids)*100:.1f}%)")
        
        # Load ONNX model
        onnx_file = MODELS_DIR / f"{stem}.onnx"
        if not onnx_file.exists():
            print(f"  [Error] Model file {onnx_file} not found. Skipping.")
            continue
            
        sess = ort.InferenceSession(str(onnx_file))
        y_probs = predict_onnx(sess, X_eval)
        y_preds = (y_probs >= 0.5).astype(int)
        
        # Save predictions
        predictions_df.loc[eval_ids, f"{label_col}_True"] = y_eval
        predictions_df.loc[eval_ids, f"{label_col}_Prob"] = np.round(y_probs, 4)
        predictions_df.loc[eval_ids, f"{label_col}_Pred"] = y_preds
        
        # Compute Metrics
        auc = roc_auc_score(y_eval, y_probs) if (n_pos > 0 and n_neg > 0) else 0.0
        ci_low, ci_high, se = hanley_mcneil_ci(auc, n_pos, n_neg)
        auprc = average_precision_score(y_eval, y_probs) if (n_pos > 0 and n_neg > 0) else 0.0
        f1 = f1_score(y_eval, y_preds, zero_division=0)
        sens = recall_score(y_eval, y_preds, zero_division=0)
        spec = recall_score(y_eval, y_preds, pos_label=0, zero_division=0)
        acc = accuracy_score(y_eval, y_preds)
        prec = precision_score(y_eval, y_preds, zero_division=0)
        
        if n_pos > 0 and n_neg > 0:
            tn, fp, fn, tp = confusion_matrix(y_eval, y_preds).ravel()
        else:
            tp = fp = tn = fn = 0
            
        print(f"  +----------------------------------------------------------------+")
        print(f"  | AUC-ROC:     {auc:.4f} (95% CI: [{ci_low:.4f}, {ci_high:.4f}])                  |")
        print(f"  | AUPRC:       {auprc:.4f} (Random Baseline: {n_pos/len(eval_ids):.4f})                    |")
        print(f"  | F1-Score:    {f1:.4f}  |  Accuracy:    {acc:.4f}                       |")
        print(f"  | Sensitivity: {sens:.4f}  |  Specificity: {spec:.4f}                       |")
        print(f"  | Precision:   {prec:.4f}  |  TP: {tp:<3} FP: {fp:<3} TN: {tn:<3} FN: {fn:<3}       |")
        print(f"  +----------------------------------------------------------------+")
        
        all_results[ab_display] = {
            "y_true": y_eval,
            "y_prob": y_probs,
            "y_pred": y_preds,
            "auc": auc,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "auprc": auprc,
            "f1": f1,
            "sens": sens,
            "spec": spec,
            "acc": acc,
            "prec": prec,
            "n_samples": len(eval_ids),
            "n_pos": n_pos,
            "n_neg": n_neg,
            "TP": tp, "FP": fp, "TN": tn, "FN": fn,
            "color": color
        }
        
        summary_rows.append({
            "Antibiotic": ab_display,
            "N_Isolates": len(eval_ids),
            "N_Resistant": n_pos,
            "N_Susceptible": n_neg,
            "Resistance_Prevalence": round(n_pos / len(eval_ids), 4),
            "AUC_ROC": round(auc, 4),
            "AUC_95CI_Low": round(ci_low, 4),
            "AUC_95CI_High": round(ci_high, 4),
            "AUPRC": round(auprc, 4),
            "AUPRC_Baseline": round(n_pos / len(eval_ids), 4),
            "F1_Score": round(f1, 4),
            "Sensitivity": round(sens, 4),
            "Specificity": round(spec, 4),
            "Accuracy": round(acc, 4),
            "Precision": round(prec, 4),
            "TP": tp, "FP": fp, "TN": tn, "FN": fn,
        })
        
        # Run SHAP Explainability
        try:
            run_shap_analysis(sess, onnx_file, X_eval, feature_names, ab_display, FIGURES_DIR)
        except Exception as e:
            print(f"  [SHAP Warning] {e}")
            
    # 3. Export Predictions CSV
    pred_path = RESULTS_DIR / "external_validation_cohort_predictions.csv"
    predictions_df.to_csv(pred_path)
    print(f"\n[Export] Saved raw predictions to:\n  {pred_path}")
    
    # 4. Export Metrics Summary CSV
    summary_df = pd.DataFrame(summary_rows)
    summary_path = RESULTS_DIR / "external_validation_metrics_summary.csv"
    summary_df.to_csv(summary_path, index=False)
    print(f"[Export] Saved clinical metrics summary to:\n  {summary_path}")
    
    # 5. Generate Multi-Drug Publication Figures
    print("\n[Figures] Generating Publication Figures (300 DPI)...")
    
    # A. ROC Curves
    fig, ax = plt.subplots(figsize=(8, 7))
    for ab_display, res in all_results.items():
        fpr, tpr, _ = roc_curve(res["y_true"], res["y_prob"])
        lbl = f"{ab_display}: AUC = {res['auc']:.3f} [{res['ci_low']:.3f}-{res['ci_high']:.3f}] (n={res['n_samples']})"
        ax.plot(fpr, tpr, color=res["color"], linewidth=2.3, label=lbl)
    ax.plot([0, 1], [0, 1], "k--", alpha=0.35, linewidth=1.2, label="Chance Baseline (AUC = 0.500)")
    ax.set_xlabel("False Positive Rate (1 - Specificity)", fontweight="bold")
    ax.set_ylabel("True Positive Rate (Sensitivity)", fontweight="bold")
    ax.set_title("ARMOR External Multi-Center Validation - ROC Curves\n(Independent Klebsiella pneumoniae Cohort)", pad=15)
    ax.legend(loc="lower right", fontsize=9.5, framealpha=0.95)
    ax.set_xlim([-0.02, 1.02])
    ax.set_ylim([-0.02, 1.02])
    ax.grid(True, alpha=0.15)
    sns.despine()
    fig.savefig(FIGURES_DIR / "roc_curves_external.png")
    plt.close(fig)
    print(f"  [OK] Saved: {FIGURES_DIR / 'roc_curves_external.png'}")
    
    # B. Precision-Recall Curves
    fig, ax = plt.subplots(figsize=(8, 7))
    for ab_display, res in all_results.items():
        prec_v, rec_v, _ = precision_recall_curve(res["y_true"], res["y_prob"])
        lbl = f"{ab_display}: AUPRC = {res['auprc']:.3f} (n={res['n_samples']})"
        ax.plot(rec_v, prec_v, color=res["color"], linewidth=2.3, label=lbl)
    ax.set_xlabel("Recall (Sensitivity)", fontweight="bold")
    ax.set_ylabel("Precision (Positive Predictive Value)", fontweight="bold")
    ax.set_title("ARMOR External Validation - Precision-Recall Curves\n(Independent Klebsiella pneumoniae Cohort)", pad=15)
    ax.legend(loc="lower left", fontsize=9.5, framealpha=0.95)
    ax.set_xlim([-0.02, 1.02])
    ax.set_ylim([-0.02, 1.05])
    ax.grid(True, alpha=0.15)
    sns.despine()
    fig.savefig(FIGURES_DIR / "pr_curves_external.png")
    plt.close(fig)
    print(f"  [OK] Saved: {FIGURES_DIR / 'pr_curves_external.png'}")
    
    # C. Confusion Matrices
    n_plots = len(all_results)
    fig, axes = plt.subplots(1, n_plots, figsize=(4.5 * n_plots, 4.2))
    if n_plots == 1: axes = [axes]
    for ax, (ab_display, res) in zip(axes, all_results.items()):
        cm = confusion_matrix(res["y_true"], res["y_pred"])
        sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", ax=ax,
                    xticklabels=["Susceptible", "Resistant"],
                    yticklabels=["Susceptible", "Resistant"],
                    cbar=False, annot_kws={"size": 13, "weight": "bold"})
        ax.set_title(f"{ab_display}\nAUC={res['auc']:.3f} | F1={res['f1']:.3f}", fontsize=11, fontweight="bold")
        ax.set_xlabel("Predicted")
        ax.set_ylabel("Actual")
    fig.suptitle("ARMOR External Validation - Confusion Matrices", fontsize=14, fontweight="bold", y=1.03)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "confusion_matrices_external.png")
    plt.close(fig)
    print(f"  [OK] Saved: {FIGURES_DIR / 'confusion_matrices_external.png'}")
    
    print("\n" + "="*75)
    print("ALL EXTERNAL VALIDATION EVALUATIONS SUCCESSFULLY COMPLETED!")
    print("="*75)

if __name__ == "__main__":
    main()
