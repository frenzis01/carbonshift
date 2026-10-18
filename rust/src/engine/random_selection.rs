//! Shared random assignment selection for simulation and online scheduling.

use rand::Rng;

use crate::engine::types::Flavour;

/// Determines which dimension of a random assignment is sampled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RandomSelectionMode {
    /// Keep the request at its earliest slot and sample its flavour.
    Flavour,
    /// Sample a feasible slot and use the most accurate flavour.
    Slot,
    /// Sample both a feasible slot and a flavour.
    Full,
}

/// Sample a slot and flavour according to a random policy mode.
pub fn select_random_assignment<R: Rng + ?Sized>(
    mode: RandomSelectionMode,
    earliest_slot: i32,
    latest_slot: i32,
    flavours: &[Flavour],
    flavour_probabilities: &[f64],
    rng: &mut R,
) -> Result<(i32, usize), String> {
    if earliest_slot > latest_slot {
        return Err("request has no feasible slot".to_string());
    }
    if flavours.is_empty() {
        return Err("at least one flavour is required".to_string());
    }
    if flavour_probabilities.len() != flavours.len() {
        return Err("flavour probability count does not match flavour count".to_string());
    }

    // Keep the unaffected dimension deterministic: flavour-only retains the
    // earliest slot, while slot-only always uses the minimum-error flavour.
    let slot = match mode {
        RandomSelectionMode::Flavour => earliest_slot,
        RandomSelectionMode::Slot | RandomSelectionMode::Full => {
            rng.gen_range(earliest_slot..=latest_slot)
        }
    };

    let flavour_index = match mode {
        RandomSelectionMode::Slot => flavours
            .iter()
            .enumerate()
            .min_by(|(_, left), (_, right)| left.error.total_cmp(&right.error))
            .map(|(index, _)| index)
            .expect("non-empty flavours checked above"),
        RandomSelectionMode::Flavour | RandomSelectionMode::Full => {
            let random_value = rng.gen_range(0.0..1.0);
            let mut cumulative_probability = 0.0;
            let mut selected_index = flavours.len() - 1;
            for (index, probability) in flavour_probabilities.iter().enumerate() {
                cumulative_probability += probability;
                if random_value < cumulative_probability {
                    selected_index = index;
                    break;
                }
            }
            selected_index
        }
    };

    Ok((slot, flavour_index))
}

/// Find the distribution closest to equal flavour probabilities whose
/// expected error equals `target_error`.
pub fn closest_to_uniform_flavour_probabilities(
    errors: &[f64],
    target_error: f64,
) -> Result<Vec<f64>, String> {
    if errors.is_empty() {
        return Err("at least one flavour is required".to_string());
    }
    if !target_error.is_finite() || errors.iter().any(|error| !error.is_finite()) {
        return Err("flavour errors and target error must be finite".to_string());
    }

    let min_error = errors.iter().copied().fold(f64::INFINITY, f64::min);
    let max_error = errors.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    if target_error < min_error || target_error > max_error {
        return Err(format!(
            "target error {target_error} is outside the flavour error range [{min_error}, {max_error}]"
        ));
    }

    if min_error == max_error {
        return Ok(vec![1.0 / errors.len() as f64; errors.len()]);
    }

    if target_error == min_error || target_error == max_error {
        let selected_error = if target_error == min_error {
            min_error
        } else {
            max_error
        };
        let matching_count = errors
            .iter()
            .filter(|error| **error == selected_error)
            .count();
        return Ok(errors
            .iter()
            .map(|error| {
                if *error == selected_error {
                    1.0 / matching_count as f64
                } else {
                    0.0
                }
            })
            .collect());
    }

    // Normalize the error range so the projection search behaves consistently
    // for percentages, fractions, and differently spaced flavour sets.
    let normalized_errors: Vec<f64> = errors
        .iter()
        .map(|error| (error - min_error) / (max_error - min_error))
        .collect();
    let normalized_target = (target_error - min_error) / (max_error - min_error);
    let uniform_mean = normalized_errors.iter().sum::<f64>() / normalized_errors.len() as f64;
    if (normalized_target - uniform_mean).abs() <= f64::EPSILON {
        return Ok(vec![1.0 / errors.len() as f64; errors.len()]);
    }

    // Project the uniform distribution along one slope, then measure its
    // expected error. Bisection finds the slope whose mean matches the target.
    let expected_normalized_error = |slope: f64| {
        project_probabilities_at_slope(&normalized_errors, slope)
            .iter()
            .zip(&normalized_errors)
            .map(|(probability, error)| probability * error)
            .sum::<f64>()
    };
    let (mut low, mut high) = if normalized_target > uniform_mean {
        (0.0, 1.0)
    } else {
        (-1.0, 0.0)
    };
    let mut bracketed = false;
    for _ in 0..64 {
        if expected_normalized_error(low) <= normalized_target
            && expected_normalized_error(high) >= normalized_target
        {
            bracketed = true;
            break;
        }
        if normalized_target > uniform_mean {
            high *= 2.0;
        } else {
            low *= 2.0;
        }
    }
    if !bracketed {
        return Err(
            "could not bracket the target error while calculating probabilities".to_string(),
        );
    }

    for _ in 0..100 {
        let midpoint = low + (high - low) / 2.0;
        if expected_normalized_error(midpoint) < normalized_target {
            low = midpoint;
        } else {
            high = midpoint;
        }
    }
    Ok(project_probabilities_at_slope(
        &normalized_errors,
        low + (high - low) / 2.0,
    ))
}

fn project_probabilities_at_slope(normalized_errors: &[f64], slope: f64) -> Vec<f64> {
    let mut scores: Vec<(usize, f64)> = normalized_errors
        .iter()
        .enumerate()
        .map(|(index, error)| (index, slope * error))
        .collect();
    scores.sort_by(|left, right| right.1.total_cmp(&left.1));

    let mut score_sum = 0.0;
    for active_count in 1..=scores.len() {
        score_sum += scores[active_count - 1].1;
        let intercept = (1.0 - score_sum) / active_count as f64;
        let next_score_is_inactive =
            active_count == scores.len() || intercept + scores[active_count].1 <= 0.0;
        if next_score_is_inactive {
            let mut probabilities = vec![0.0; scores.len()];
            for (index, score) in scores.iter().take(active_count) {
                probabilities[*index] = (intercept + score).max(0.0);
            }
            return probabilities;
        }
    }

    unreachable!("the final active set always normalizes to one")
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::SeedableRng;
    use rand::rngs::SmallRng;

    fn flavours() -> Vec<Flavour> {
        vec![
            Flavour {
                name: "Accurate".to_string(),
                error: 0.0,
                duration: 3.0,
            },
            Flavour {
                name: "Balanced".to_string(),
                error: 5.0,
                duration: 2.0,
            },
            Flavour {
                name: "Fast".to_string(),
                error: 10.0,
                duration: 1.0,
            },
        ]
    }

    #[test]
    fn probability_mix_matches_target_error() {
        let probabilities =
            closest_to_uniform_flavour_probabilities(&[0.0, 5.0, 10.0], 7.5).unwrap();
        assert!((probabilities[0] - 1.0 / 12.0).abs() < 1e-9);
        assert!((probabilities[1] - 1.0 / 3.0).abs() < 1e-9);
        assert!((probabilities[2] - 7.0 / 12.0).abs() < 1e-9);
    }

    #[test]
    fn flavour_mode_fixes_earliest_slot_and_samples_from_the_mix() {
        let flavours = flavours();
        let probabilities =
            closest_to_uniform_flavour_probabilities(&[0.0, 5.0, 10.0], 7.5).unwrap();
        let mut rng = SmallRng::seed_from_u64(123);

        for _ in 0..20 {
            let (slot, flavour_index) = select_random_assignment(
                RandomSelectionMode::Flavour,
                4,
                10,
                &flavours,
                &probabilities,
                &mut rng,
            )
            .unwrap();
            assert_eq!(slot, 4);
            assert!(flavour_index < flavours.len());
        }
    }

    #[test]
    fn slot_mode_samples_slots_and_always_selects_most_accurate_flavour() {
        let flavours = flavours();
        let probabilities = vec![1.0 / 3.0; 3];
        let mut rng = SmallRng::seed_from_u64(456);
        let mut sampled_slots = std::collections::HashSet::new();

        for _ in 0..100 {
            let (slot, flavour_index) = select_random_assignment(
                RandomSelectionMode::Slot,
                2,
                5,
                &flavours,
                &probabilities,
                &mut rng,
            )
            .unwrap();
            assert!((2..=5).contains(&slot));
            assert_eq!(flavour_index, 0);
            sampled_slots.insert(slot);
        }
        assert!(sampled_slots.len() > 1);
    }

    #[test]
    fn full_mode_samples_slot_and_flavour() {
        let flavours = flavours();
        let probabilities =
            closest_to_uniform_flavour_probabilities(&[0.0, 5.0, 10.0], 7.5).unwrap();
        let mut rng = SmallRng::seed_from_u64(789);
        let (slot, flavour_index) = select_random_assignment(
            RandomSelectionMode::Full,
            3,
            8,
            &flavours,
            &probabilities,
            &mut rng,
        )
        .unwrap();

        assert!((3..=8).contains(&slot));
        assert!(flavour_index < flavours.len());
    }
}
