# Board Evaluation

## Classification
RESEARCH

## Summary
The board asks a process question: should all 10 features be decomposed into a task DAG upfront and then executed in sequencing order, or should each feature be decomposed and implemented before moving to the next? The SEQUENCING.md defines 6 dependency waves (F1 → {F2,F3} → F4 → {F5,F7,F8,F10} → F6 → F9). This is a planning methodology question with a clear answer: **wave-by-wave decompose-then-build** is the right flow, not a full upfront decomposition of all 10 features. Decomposing F5–F10 before F1–F4 exist produces stale, wrong task breakdowns because the actual interfaces (CodeProvider protocol, gateway shape) won't be known until earlier features are built. The key insight is that Pathly's task scheduler enforces ordering *within* a goal, not *between* features — the driver (you) manages feature sequencing manually.

## Key unknown / risk
Whether you intend to build one feature at a time (serial) or run waves in parallel within a single Pathly session — the answer changes the board setup but not the core recommendation.

## Recommended next steps
- Accept the **wave-by-wave** flow: decompose F1 → build F1 → then decompose F2+F3 together (same wave, can run in parallel) → build them → continue wave by wave
- Do NOT pre-decompose all 10 features — F2–F10 task breakdowns depend on the actual interfaces that emerge from building F1–F4
- Start with `/pathly create-feature 01-mcp-skeleton` and run `/pathly plan` to decompose only F1 into tasks now
- After F1 ships, decompose F2 and F3 simultaneously (they are independent of each other, both need only F1)

---

## Implementation flow recommendation (detailed)

### Option A: Decompose-all-first (NOT recommended)
Pre-plan all 10 features' task DAGs, then execute in order.

**Problem:** F2–F10 task details depend on the `CodeProvider` interface, gateway shape, and safe-null envelope that F1 actually produces. Planning them before F1 is built means the task breakdowns will be stale or wrong by the time you reach them. You'll redo the planning anyway.

### Option B: One-at-a-time, sequential (acceptable but slow)
Decompose F1, build it, then decompose F2, build it, etc.

**Works**, but leaves parallelism on the table. F2 (Graph engine) and F3 (LSP engine) are independent — they both only need F1. Building them sequentially doubles wall-clock time for that wave.

### Option C: Wave-by-wave decompose-then-build (RECOMMENDED)
1. Decompose + build **Wave 1**: F1 (alone — it's the root)
2. Decompose + build **Wave 2**: F2 + F3 in parallel (both need only F1, independent of each other)
3. Decompose + build **Wave 3**: F4 (needs F2 + F3 done)
4. Decompose + build **Wave 4**: F5, F7, F8, F10 in parallel (all need F4; F10 also needs F2 which is done)
5. Decompose + build **Wave 5**: F6 (needs F4 + F5)
6. Decompose + build **Wave 6**: F9 (ship gate — needs F2, F3, F5, F7)

**Why this wins:**
- You only plan what you can actually see — interfaces are real by the time you decompose dependents
- Within each wave, features are independent and can be parallelized (Pathly parallel boards or sequential if you prefer)
- Each wave produces a working, shippable state (safe-null contract means partial feature set still works)
- Fast feedback: F1 gives you the skeleton + contract; you'll know immediately if the protocol design is right before investing in all 9 downstream features

### Practical Pathly mechanics
- Pathly's task DAG enforces ordering *within* a feature goal, not *across* features
- You (the driver) manually advance from one feature to the next — `SEQUENCING.md` is your checklist, not an automated constraint
- Run `/pathly create-feature 01-mcp-skeleton` to stand up the first board
- After it ships, create F2 and F3 boards simultaneously and run them in parallel sessions if desired
