# Contiguous Time Model - Design Document

## Problem Statement

**Current limitation**: Il modello `time_capacity_tiers` assume che ogni richiesta debba essere completamente processata in un singolo time slot.

**Realtà**: Una richiesta può:
1. Iniziare in uno slot e continuare in quelli successivi (spanning contiguo)
2. Durare più di 30 minuti (> durata di un singolo slot)
3. Sfruttare meglio lo spazio residuo degli slot

---

## New Model: Contiguous Spanning

### Core Concept

Quando assegno un blocco di richieste a uno slot di partenza `t`:
- Le richieste vengono processate **sequenzialmente in ordine di arrivo**
- Se la durata totale eccede la capacità dello slot, **continua negli slot successivi**
- Il costo viene calcolato **proporzionalmente** al tempo speso in ogni slot

### Example

```
Scenario:
- Slot 0: 20 min residui
- Slot 1: 30 min residui  
- Slot 2: 30 min residui

Assegno blocco di 10 richieste @ 8min/richiesta con parallelism=1:
- Durata totale: 10 × 8 = 80 minuti

Spanning:
- Slot 0: Usa 20 min (riempie lo slot)
- Slot 1: Usa 30 min (riempie lo slot)
- Slot 2: Usa 30 min rimanenti

Costo totale:
  = carbon[0] × 20 × emission_factor[0]
  + carbon[1] × 30 × emission_factor[1]
  + carbon[2] × 30 × emission_factor[2]
```

---

## Complexity Analysis

### State Space

**Stato DP**: `D[block][error, residual_time_per_slot]`

Dove `residual_time_per_slot[t]` = tempo residuo nello slot `t`

**Problema**: Lo spazio di ricerca esplode!
- Prima: Stati ≈ B × E × (num_loads)^Δ × (num_times)^Δ
- Ora: Stati ≈ B × E × (slot_duration)^Δ ← **ENORME!**

Ogni slot può avere residuo da 0 a `slot_duration × parallelism`, quindi configurazioni ≈ O((slot_duration × parallelism)^Δ)

---

## Simplifications (User's Suggestion)

### Option 1: No Reordering (Sequential Processing)

**Assumption**: Le richieste vengono processate nell'ordine di arrivo dei blocchi.

**Benefit**: Non dobbiamo esplorare permutazioni, solo "da che slot partire".

**Implementation**:
- Manteniamo un vettore `residual_time[slot]` nello stato
- Quando assegniamo block `b` a slot `t`, riempiamo slot contigui partendo da `t`
- Non consideriamo riordinamenti

### Option 2: Check Only Last Block

**Assumption**: Consideriamo lo sforamento solo all'assegnamento dell'ultima richiesta.

**Issue**: Non chiaro come applicare questa semplificazione...

---

## Proposed Implementation

### Simplified Contiguous Model

**Key Idea**: 
- Tracciamo `residual_time[slot]` invece di `loads[slot]`
- Quando assegniamo un blocco, lo "spalmiamo" su slot contigui
- Costo = somma pesata dei contributi per slot

**DP State**:
```python
D[b][e][residual_times_tuple]
```

Dove `residual_times_tuple` è una tupla immutabile che rappresenta il tempo residuo in ogni slot.

**Transition**:
```python
for prev_state in D[b-1][e_prev]:
    residuals = list(prev_state.residual_times)
    
    for start_slot in range(delta):
        for strategy in strategies:
            # Calcola durata totale
            total_duration = block_size * strategy['duration'] / parallelism
            
            # Spalma su slot contigui partendo da start_slot
            remaining_dur = total_duration
            slot = start_slot
            cost_contribution = 0
            new_residuals = residuals.copy()
            
            while remaining_dur > 0 and slot < delta:
                # Quanto posso mettere in questo slot?
                available = new_residuals[slot]
                used = min(available, remaining_dur)
                
                # Calcola carico per capacity tier
                load_in_slot = used / slot_duration_minutes
                emission_factor = get_emission_factor(load_in_slot, tiers)
                
                # Contributo al costo
                cost_contribution += carbon[slot] * used * emission_factor
                
                # Aggiorna residuo
                new_residuals[slot] -= used
                remaining_dur -= used
                slot += 1
            
            # Se non tutto è stato assegnato, skip (deadline violation)
            if remaining_dur > 0:
                continue
            
            # Altrimenti aggiorna stato
            new_error = e_prev + strategy['error'] * block_size
            new_cost = prev_cost + cost_contribution
            
            D[b][new_error][tuple(new_residuals)] = new_cost
```

---

## State Space Explosion Problem

**Issue**: Anche con semplificazioni, lo spazio è enorme!

**Example**:
- Δ = 10 slots
- Slot duration = 30 min
- Parallelism = 4
- Residual capacity per slot: 0 to 120 minutes (granularità 1 minuto)
- Possibili configurazioni: 120^10 ≈ 6.2 × 10^20 stati!

### Solution: Discretization

**Idea**: Lavoriamo con **granularità ridotta**.

Invece di tracciare minuti esatti, tracciamo "unità di carico" (e.g., blocchi da 5 minuti):

```python
GRANULARITY = 5  # minutes

residual_capacity_units = slot_duration_minutes // GRANULARITY
# 30 min / 5 = 6 unità per slot

Possibili configurazioni: 6^10 ≈ 60 milioni (gestibile!)
```

---

## Alternative: First-Fit Sequential (Simple Heuristic)

**Ultra-simplified approach** (no DP):

```python
def first_fit_contiguous(blocks, strategies, carbon, delta, ...):
    """
    Assegna blocchi sequenzialmente in ordine di arrivo.
    Ogni blocco viene messo nel primo slot disponibile con spazio sufficiente.
    Se spanning necessario, usa slot contigui.
    """
    residuals = [slot_duration_minutes * parallelism] * delta
    assignments = []
    total_cost = 0
    
    for block in blocks:
        # Scegli strategia con costo minimo che rispetta error budget
        best_cost = float('inf')
        best_strategy = None
        best_start_slot = None
        
        for strategy in strategies:
            for start_slot in range(delta):
                # Prova ad assegnare partendo da start_slot
                cost, feasible = try_assign_contiguous(
                    block, strategy, start_slot, residuals, carbon, tiers
                )
                
                if feasible and cost < best_cost:
                    best_cost = cost
                    best_strategy = strategy
                    best_start_slot = start_slot
        
        # Commit assignment
        assign_contiguous(block, best_strategy, best_start_slot, residuals)
        total_cost += best_cost
    
    return total_cost, assignments
```

---

## Recommended Approach (Phase 1)

1. **Start with heuristics only** (no DP initially)
   - Greedy contiguous
   - First-fit contiguous
   - Best-fit contiguous

2. **Test on small instances** (Δ=5, parallelism=2)
   - Verify correctness
   - Compare with discrete model

3. **If needed, add DP with heavy pruning**
   - Use discretization (granularity 5-10 min)
   - Warm-start from greedy
   - Aggressive beam pruning (K=50)

---

## Implementation Plan

### Phase 1: Core Infrastructure (Heuristics)

1. `utils_cont.py`: Helper functions
   - `try_assign_contiguous()`: Tenta assegnamento spanning
   - `calculate_contiguous_cost()`: Calcola costo multi-slot
   - `get_residuals()`: Traccia tempo residuo

2. `greedy_cont.py`: Greedy contiguous
   - Per ogni blocco, trova (slot_start, strategy) con costo minimo
   - Usa spanning contiguo

3. `first_fit_cont.py`: First-fit contiguous
   - Assegna a primo slot disponibile

4. `best_fit_cont.py`: Best-fit contiguous
   - Assegna a slot con fit più stretto (minimizza frammentazione)

### Phase 2: DP (If Feasible)

5. `dp_cont.py`: DP contiguous con discretizzazione
   - Granularità configurabile
   - Pruning aggressivo

### Phase 3: Testing

6. `tests/comparison_cont.py`: Test comparativo
   - Confronta discrete vs contiguous
   - Misura miglioramento nello sfruttamento degli slot

---

## Expected Benefits

**Discrete model**:
```
Slot 0: [Block 1: 25 min] → 5 min sprecati
Slot 1: [Block 2: 28 min] → 2 min sprecati
Slot 2: [vuoto] → 30 min sprecati
```

**Contiguous model**:
```
Slot 0: [Block 1: 25 min][Block 2: 5 min] → 0 min sprecati
Slot 1: [Block 2: 23 min] → 7 min sprecati
Slot 2: [vuoto] → 30 min sprecati
```

**Improvement**: Migliore utilizzo della capacità, soprattutto con blocchi piccoli e slot frammentati.

---

## Open Questions

1. **Granularità ottimale**: 1min, 5min, 10min?
2. **Emission factor con spanning**: Media pesata? Per-slot?
3. **Deadline handling**: Se un blocco span attraversa la deadline, è valido?
4. **Parallelismo con spanning**: Modello semplificato lineare va bene?

---

## Next Steps

1. ✅ Documento di design (questo file)
2. ⏭️ Implementare `utils_cont.py`
3. ⏭️ Implementare `greedy_cont.py`
4. ⏭️ Test di validazione con esempi piccoli
5. ⏭️ Confronto discrete vs contiguous

---

**Status**: Design phase
**Complexity**: Alta (state space explosion)
**Recommended**: Start with heuristics, add DP only if critical
