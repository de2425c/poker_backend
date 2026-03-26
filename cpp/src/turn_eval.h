#pragma once

#include <array>
#include <vector>

#include "turn_game.h"

// TurnEvaluator computes expected values by averaging equity over all 44 river cards.
// Unlike VectorEvaluator (which precomputes exact hand strengths for a known river),
// this class precomputes an equity matrix that represents win/tie/loss probabilities
// averaged across all possible river runouts.
class TurnEvaluator {
public:
    explicit TurnEvaluator(const std::array<std::vector<TurnHand>, 2>& hands,
                           const std::vector<int>& board_cards);

    // Compute showdown values for the player, weighted by opponent reach.
    // out_values[h] = sum over villain hands of (reach * equity * pot_adjustment)
    void showdown_values(int player,
                         const double* opp_weights,
                         double pot_total,
                         double contrib_player,
                         double* out_values) const;

    // Compute fold values (opponent folds, player wins pot minus their contribution).
    void fold_values(int player,
                     const double* opp_weights,
                     double value,
                     double* out_values) const;

    // Compute total valid opponent weight for each of player's hands (excluding blocked).
    void valid_opp_weights(int player, const double* opp_weights, double* out_values) const;

private:
    // Precompute equity matrix by averaging showdown outcomes over all rivers.
    void precompute_equities();

    std::array<std::vector<TurnHand>, 2> hands_;
    std::vector<int> board_cards_;  // 4 turn cards

    // equity_[player][hero_idx * opp_count + villain_idx] = hero's equity vs villain
    // Stored as flat array for cache efficiency.
    std::array<std::vector<double>, 2> equity_;

    // blocking_[player][hero_idx * opp_count + villain_idx] = true if hands share cards
    std::array<std::vector<bool>, 2> blocking_;

    std::array<int, 2> num_hands_{{0, 0}};
};
