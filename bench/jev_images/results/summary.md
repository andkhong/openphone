# Jev image benchmark: measured results

163 labelled screenshots (53 canvas, 110 labelled/app). Noul threshold 0.5.

## J1: screen state (7-way choice)

| method | all | canvas | labelled | n |
|---|---|---|---|---|
| jev_text | 71.8% | 24.5% | 94.5% | 163 |
| jev113_img768 | 95.1% | 100.0% | 92.7% | 163 |
| jev_img512 | 95.1% | 100.0% | 92.7% | 163 |
| jev_img768 | 95.1% | 100.0% | 92.7% | 163 |
| jev_img1024 | 95.1% | 100.0% | 92.7% | 163 |
| haiku | 92.6% | 96.2% | 90.9% | 163 |

Confusions (truth -> predicted, count >= 2):

- jev_text: loading->content 14, menu->content 13, dialog->content 10, game_over->content 5, content->dialog 4
- jev_img768: loading->content 3, content->dialog 3
- haiku: content->dialog 5, loading->content 3, loading->menu 2

## J2: locate a named target

Present targets: hit = the returned point (box centre) inside the true target box. Absent targets: correct = the method says not there (Haiku: present=false; elements: no match; Jev: gated by a threshold).

| method | present hit (all) | canvas | labelled | labelled excl. search box | n present |
|---|---|---|---|---|---|
| jev_img512 | 93.8% | 91.9% | 95.1% | 93.8% | 177 |
| jev_img768 | 96.0% | 93.2% | 98.1% | 97.5% | 177 |
| jev_img1024 | 96.0% | 94.6% | 97.1% | 96.3% | 177 |
| haiku | 91.0% | 100.0% | 84.5% | 95.1% | 177 |
| elements (today) | 45.2% | 0.0% | 77.7% | 98.8% | 177 |

Element bounds exist (accessibility node for the target): 103/177 present targets (all labelled ones, no canvas one). 'elements (today)' above is a naive label matcher over those nodes; when the right node is picked its bounds are exact by construction.

### Box confidence: present vs absent targets

| method | conf p50 present | conf p50 absent | AUC conf | AUC visible-noul | AUC conf x noul |
|---|---|---|---|---|---|
| jev_img512 | 0.83 | 0.14 | 0.974 | 0.988 | 0.987 |
| jev_img768 | 0.90 | 0.13 | 0.959 | 0.991 | 0.988 |
| jev_img1024 | 0.89 | 0.11 | 0.957 | 0.990 | 0.988 |

### Threshold sweep (Box confidence gate): end-to-end J2 accuracy over present + absent targets

Correct = present target: passes the gate and hits; absent target: rejected by the gate. 'false tap' = an absent target let through, or a present one let through that misses.

| method | gate | T | end-to-end acc | present hit+pass | absent rejected | false taps |
|---|---|---|---|---|---|---|
| jev_img512 | conf | 0.3 | 88.2% | 165/177 | 135/163 | 36 |
| jev_img512 | conf | 0.4 | 90.0% | 161/177 | 145/163 | 24 |
| jev_img512 | conf | 0.5 | 91.2% | 154/177 | 156/163 | 11 |
| jev_img512 | conf | 0.6 | 88.5% | 141/177 | 160/163 | 5 |
| jev_img512 | conf | 0.7 | 85.6% | 128/177 | 163/163 | 1 |
| jev_img512 | conf | 0.8 | 77.6% | 101/177 | 163/163 | 1 |
| jev_img512 | conf | 0.9 | 59.7% | 40/177 | 163/163 | 1 |
| jev_img512 | vis | 0.5 | 96.5% | 165/177 | 163/163 | 8 |
| jev_img512 | vis | 0.7 | 96.2% | 164/177 | 163/163 | 8 |
| jev_img512 | both | 0.5 | 93.2% | 154/177 | 163/163 | 4 |
| jev_img512 | both | 0.7 | 85.6% | 128/177 | 163/163 | 1 |
| jev_img768 | conf | 0.3 | 84.4% | 168/177 | 119/163 | 47 |
| jev_img768 | conf | 0.4 | 87.9% | 165/177 | 134/163 | 31 |
| jev_img768 | conf | 0.5 | 87.9% | 156/177 | 143/163 | 20 |
| jev_img768 | conf | 0.6 | 88.5% | 148/177 | 153/163 | 10 |
| jev_img768 | conf | 0.7 | 85.9% | 137/177 | 155/163 | 8 |
| jev_img768 | conf | 0.8 | 83.5% | 124/177 | 160/163 | 3 |
| jev_img768 | conf | 0.9 | 74.4% | 90/177 | 163/163 | 0 |
| jev_img768 | vis | 0.5 | 97.4% | 169/177 | 162/163 | 6 |
| jev_img768 | vis | 0.7 | 97.4% | 169/177 | 162/163 | 6 |
| jev_img768 | both | 0.5 | 93.5% | 156/177 | 162/163 | 1 |
| jev_img768 | both | 0.7 | 87.9% | 137/177 | 162/163 | 1 |
| jev_img1024 | conf | 0.3 | 84.4% | 169/177 | 118/163 | 46 |
| jev_img1024 | conf | 0.4 | 86.2% | 167/177 | 126/163 | 37 |
| jev_img1024 | conf | 0.5 | 87.6% | 160/177 | 138/163 | 25 |
| jev_img1024 | conf | 0.6 | 86.5% | 150/177 | 144/163 | 19 |
| jev_img1024 | conf | 0.7 | 87.4% | 140/177 | 157/163 | 6 |
| jev_img1024 | conf | 0.8 | 85.3% | 129/177 | 161/163 | 2 |
| jev_img1024 | conf | 0.9 | 73.5% | 87/177 | 163/163 | 0 |
| jev_img1024 | vis | 0.5 | 97.6% | 170/177 | 162/163 | 5 |
| jev_img1024 | vis | 0.7 | 97.6% | 170/177 | 162/163 | 5 |
| jev_img1024 | both | 0.5 | 94.7% | 160/177 | 162/163 | 1 |
| jev_img1024 | both | 0.7 | 88.8% | 140/177 | 162/163 | 1 |

Best threshold per gate: jev_img512 conf: T=0.5 (91.2%); jev_img512 vis: T=0.3 (96.5%); jev_img512 both: T=0.3 (96.2%); jev_img768 conf: T=0.6 (88.5%); jev_img768 vis: T=0.3 (97.4%); jev_img768 both: T=0.3 (97.1%); jev_img1024 conf: T=0.5 (87.6%); jev_img1024 vis: T=0.3 (97.6%); jev_img1024 both: T=0.3 (97.4%)

- haiku: end-to-end 95.0%, present hit 161/177, absent rejected 162/163, false taps 14

- baseline: end-to-end 71.5%, present hit 80/177, absent rejected 163/163, false taps 23

## J3: loading vs ready (positive = loading)

| method | accuracy | precision | recall | n |
|---|---|---|---|---|
| heuristic (today) | 87.1% | 100.0% | 22.2% | 163 |
| jev_text | 90.2% | 82.4% | 51.9% | 163 |
| jev113_img768 | 97.5% | 96.0% | 88.9% | 163 |
| jev_img512 | 96.9% | 92.3% | 88.9% | 163 |
| jev_img768 | 96.9% | 92.3% | 88.9% | 163 |
| jev_img1024 | 96.9% | 92.3% | 88.9% | 163 |
| haiku | 95.7% | 95.5% | 77.8% | 163 |

Loading/ready pairs: 15 (HTML) plus 12 canvas loading screens; every other image is a 'ready' negative.

## J4: landing check (positive = shows results for the asked query)

| method | accuracy | precision | recall | n |
|---|---|---|---|---|
| jev_text | 100.0% | 100.0% | 100.0% | 22 |
| jev113_img768 | 100.0% | 100.0% | 100.0% | 22 |
| jev_img512 | 100.0% | 100.0% | 100.0% | 22 |
| jev_img768 | 95.5% | 90.9% | 100.0% | 22 |
| jev_img1024 | 100.0% | 100.0% | 100.0% | 22 |
| haiku | 95.5% | 90.9% | 100.0% | 22 |

## J5: sensitive content (positive = financial or identity data visible)

| method | accuracy | precision | recall | n |
|---|---|---|---|---|
| jev_text | 97.5% | 90.9% | 76.9% | 163 |
| jev113_img768 | 99.4% | 100.0% | 92.3% | 163 |
| jev_img512 | 99.4% | 100.0% | 92.3% | 163 |
| jev_img768 | 99.4% | 100.0% | 92.3% | 163 |
| jev_img1024 | 99.4% | 100.0% | 92.3% | 163 |
| haiku | 98.8% | 92.3% | 92.3% | 163 |

## J6: real vs fake permission dialog

3-way accuracy (system / app_drawn / none) on all images, on the dialog set only, and binary 'real system dialog' precision/recall.

| method | 3-way all | 3-way dialog set | system P | system R | app-drawn called system |
|---|---|---|---|---|---|
| jev_text | 95.1% | 60.0% | 70.0% | 100.0% | 3/13 |
| jev113_img768 | 100.0% | 100.0% | 100.0% | 100.0% | 0/13 |
| jev_img512 | 100.0% | 100.0% | 100.0% | 100.0% | 0/13 |
| jev_img768 | 100.0% | 100.0% | 100.0% | 100.0% | 0/13 |
| jev_img1024 | 100.0% | 100.0% | 100.0% | 100.0% | 0/13 |
| haiku | 93.3% | 95.0% | 77.8% | 100.0% | 1/13 |

Dialog set: 7 real system dialogs, 13 app-drawn.

## Latency (wall clock per request from this Mac) and cost

Combined requests carry every job's questions (Jev: 4-5 questions + 2 per target; Haiku: one JSON answer for all jobs).

| method | requests | failures | p50 ms | p90 ms | mean input tokens | mean output tokens | $ per 1k requests |
|---|---|---|---|---|---|---|---|
| jev_text | 163 | 0 | 80 | 138 | 941 | 140 | 0.0395 |
| jev113_img768 | 163 | 0 | 94 | 133 | 1588 | 141 | 0.0667 |
| jev_img512 | 163 | 0 | 95 | 303 | 1312 | 220 | 0.0551 |
| jev_img768 | 163 | 0 | 92 | 298 | 1660 | 220 | 0.0697 |
| jev_img1024 | 163 | 0 | 100 | 295 | 2190 | 220 | 0.0920 |
| haiku | 163 | 0 | 997 | 1232 | 1410 | 91 | 0.1866 |

### Single-question latency (30 images x 3 repeats, jev-preview)

| question | size | n | p50 ms | p90 ms | repeat p50s | mean input tokens |
|---|---|---|---|---|---|---|
| state | 512 | 90 | 81 | 161 | 87 / 80 / 81 | 983 |
| state | 768 | 90 | 83 | 165 | 88 / 81 / 82 | 1332 |
| state | 1024 | 90 | 85 | 161 | 92 / 82 / 86 | 1861 |
| box | 512 | 90 | 80 | 156 | 95 / 78 / 78 | 709 |
| box | 768 | 90 | 83 | 163 | 90 / 80 / 83 | 1057 |
| box | 1024 | 90 | 83 | 164 | 91 / 83 / 82 | 1587 |

Failures: 0 of 540.

### Haiku single-job latency (30 images x 2 repeats)

| job | n | p50 ms | p90 ms | mean output tokens |
|---|---|---|---|---|
| state | 60 | 804 | 991 | 11 |
| locate | 60 | 838 | 1007 | 41 |

Haiku locate-only hit rate on these: 53/60.

### Haiku text-only 'what to tap' (pattern B's first half), 30 calls

p50 1699 ms, p90 2355 ms, mean output tokens 213.

## Totals

Jev requests: 1355 ($0.0811). Haiku calls: 313 ($0.0481). Prices: Jev $0.042/Mtok input, output free; Haiku 5.5 $0.10/$0.50 per Mtok.

