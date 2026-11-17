# NLCL benchmark report (2026-11-17 15:57)

Held-out sentences only (never used for training). Correct = same resulting system state as the canonical sentence. Pre-registered hypotheses: `bench/HYPOTHESES.md`.

> **Note:** llm-direct and nlcl+teacher were NOT run: no language-model backend (set a free API key, see ':backend status', then run again).

## Fidelity

| metric | nlcl-untrained | nlcl-trained |
|---|---|---|
| interpretation accuracy (valid sentences) | 0.4476 | 0.4667 |
| executed something not meant (silent error rate) | 0.4103 | 0.2393 |
| precision when it executes | 0.4947 | 0.6364 |
| nonsense / out-of-scope refused | 1/12 | 9/12 |
| asked instead of executing | 22 | 40 |
| all paraphrases of an intent right | 0.0571 | 0.0857 |
| calibration error of the weight (ECE, lower is better) | 0.3816 | 0.1926 |
| weight separates right/wrong (AUROC) | 0.8966 | 0.9146 |
| median latency per sentence (ms) | 0.87 | 0.8 |

## Risk / coverage of the trained weight

Executing only sentences whose weight reaches the threshold (the rest are asked about):

| weight threshold | share executed | precision of what is executed | wrong executions |
|---|---|---|---|
| 0.0 | 0.889 | 0.471 | 55 |
| 0.35 | 0.658 | 0.636 | 28 |
| 0.5 | 0.641 | 0.64 | 27 |
| 0.6 | 0.573 | 0.716 | 19 |
| 0.7 | 0.547 | 0.75 | 16 |
| 0.8 | 0.538 | 0.762 | 15 |
| 0.9 | 0.47 | 0.745 | 14 |

## Learning curve (no model retraining: rules, evidence and weights in Prolog)

| training paraphrases per intent | rules learned | accuracy | silent errors | nonsense refused | ECE |
|---|---|---|---|---|---|
| 0 | 0 | 0.4476 | 0.4103 | 1/12 | 0.3816 |
| 1 | 1 | 0.4476 | 0.2479 | 9/12 | 0.2654 |
| 2 | 1 | 0.4476 | 0.2479 | 9/12 | 0.2358 |
| 3 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 4 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 5 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 6 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 7 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 8 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |
| 9 | 3 | 0.4667 | 0.2393 | 9/12 | 0.1926 |

## Terminal

| metric | nlcl-trained |
|---|---|
| same file-system effect as the meant command | 0.5758 |
| executed with the wrong effect | 0.0 |
| generated bash + PowerShell verified in a sandbox | 21/38 |
| files changed/deleted without showing a diff first | 0 |

## Determinism

Identical interpretation on every repeated run: nlcl: 117/117

## Cost

NLCL offline: 0 tokens, median 0.8 ms per sentence.

## What the trained interpreter learned (Prolog facts)

```prolog
% NLCL learned knowledge - written 2026-11-17 15:57:33
% (plain Prolog facts: edit, delete or load them in SWI-Prolog)

% rewrite/3
rewrite(['store\'s'],[store],1).
rewrite([get,rid,of],[delete],1).
rewrite([the,game],[],1).

% reliability/3
reliability(frame(behavior),18,1).
reliability(frame(connect_to),2,0).
reliability(frame(create_object),22,4).
reliability(frame(delete_object),5,0).
reliability(frame(move_object),0,1).
reliability(frame(rename_object),2,0).
reliability(frame(set_property),12,13).
reliability(frame(special),11,0).
reliability(intent(archive),3,0).
reliability(intent(copy),3,0).
reliability(intent(count_files),3,0).
reliability(intent(count_lines),3,0).
reliability(intent(delete),3,0).
reliability(intent(list_files),3,0).
reliability(intent(make_dir),3,0).
reliability(intent(move),3,0).
reliability(intent(replace_text),3,0).
reliability(intent(search_text),3,0).
reliability(intent(show_file),3,0).

% param/2
param(bias,-1.11072).
param(coverage,2.257904).
param(dry_errors,-2.4).
param(errors,-2.2).
param(frame_rel,0.858535).
param(fuzzy,-0.9).
param(inferred,-0.404714).
param(llm,0.4).
param(missing_paths,-1.8).
param(n_ops,-0.292096).
param(notes,-0.65517).
param(novel,-1.853353).
param(question,-0.6).
param(reliability,0.957997).
param(shell,1.213662).
param(unknown,-0.9).
param(verbless,-1.796907).
param(warnings,-0.25).
```
